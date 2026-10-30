"""编排层图装配 —— LangGraph ``StateGraph`` + SQLite Checkpoint。

本模块把四个节点串成线性流水线，并接上「可落盘、可回放」的检查点存储：

    START ─▶ input_layer ─▶ router_layer ─▶ execution_layer ─▶ output_layer ─▶ END

「全程可审计」这条红线在这里落地：
    · 每个节点都经 :func:`..state.audited` 包装，自动产出节点级审计记录
      （节点名 / 起止时间 / 输入哈希 / 输出哈希）；
    · 每次执行后，**完整 state**（含 ``route_result.trace`` 与 ``audit_trail``）
      被写入 SQLite；换一个独立连接即可读回同一份 state —— 这正是
      ``tests/test_preop_graph.py`` 的往返测试要验证的能力。

## 一个必须显式处理的坑：类型注册

LangGraph 的 ``JsonPlusSerializer`` 用 msgpack 序列化 state。对于**本项目自定义类型**
（pydantic 模型、dataclass、枚举），它当前会「放行但告警」：

    Deserializing unregistered type src.models.preop.case.VascCase from checkpoint.
    This will be blocked in a future version.

也就是说：**不注册也能跑，但未来某个 LangGraph 版本会直接把反序列化阻断**，
届时历史 checkpoint 全部读不回来，审计链在「升级」这个动作上断掉。
故本模块通过 :func:`build_checkpoint_serde` 显式声明全部允许类型——
这不是可选优化，而是审计红线要求的防御。

治理红线：本模块是确定性基础设施，不得引入任何 LLM 客户端。
"""

from __future__ import annotations

import itertools
import logging
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, get_args, get_type_hints

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from ..models.preop.case import VascCase
from ..rules.router import RouteResult, RuleTrace
from .execution_layer.node import execution_node
from .input_layer.node import input_node
from .output_layer.node import output_node
from .router_layer.node import router_node
from .state import PreopRunResult, PreopState, audited

__all__ = [
    "DEFAULT_CHECKPOINT_DB",
    "build_checkpoint_serde",
    "build_preop_graph",
    "run_preop",
    "sqlite_checkpointer",
    "thread_config",
]

#: 默认的检查点数据库路径。
#: 放在 ``.workbuddy/`` 下是刻意的 —— 该目录已被 ``.gitignore`` 忽略，
#: 运行期产生的二进制审计库不会被误提交进仓库。
DEFAULT_CHECKPOINT_DB: Path = Path(".workbuddy/state/preop_checkpoints.sqlite")

#: 本模块 logger。刻意不用 root logger：落盘策略的告警必须带有明确来源，
#: 便于在使用方日志里定位「是谁在说不落盘」。
logger = logging.getLogger(__name__)


# ===========================================================================
# 序列化白名单
# ===========================================================================
def _unwrap_annotation(annotation: Any) -> Iterator[Any]:
    """展开一个类型注解，产出它自身及所有类型参数。

    处理 ``X | None``（``get_args`` → ``(X, NoneType)``）、``list[X]``、
    ``dict[str, X]`` 等容器写法，使嵌套引用能被递归发现。
    """
    yield annotation
    for argument in get_args(annotation):
        yield from _unwrap_annotation(argument)


def _declared_types(roots: Iterable[Any]) -> list[type]:
    """把 ``roots`` 及其**嵌套引用**的项目自定义类型全部展开。

    为什么不写死一份清单：白名单最容易漏的从来不是顶层模型，而是
    **嵌在模型内部的类型** —— 例如 ``FinalRecommendation`` 里的
    ``DecisionStep`` / ``EvidenceItem``、``VascCase`` 里的
    ``FieldProvenance`` / ``UrgencyFlagAudit``。写死清单意味着每次新增模型
    都要记得回来补一行，迟早会漏（本轮就实测漏了前两个）。

    递归展开后，「凡是 state 里可达的自定义类型都被登记」成为**构造性事实**，
    而非依赖开发者自觉。
    """
    found: dict[str, type] = {}
    queue: list[Any] = list(roots)

    while queue:
        candidate = queue.pop()
        if not isinstance(candidate, type):
            continue

        key = f"{getattr(candidate, '__module__', '')}.{getattr(candidate, '__qualname__', '')}"
        if key in found:
            continue

        if issubclass(candidate, Enum):
            found[key] = candidate
        elif issubclass(candidate, BaseModel):
            found[key] = candidate
            for field in candidate.model_fields.values():
                queue.extend(_unwrap_annotation(field.annotation))
        elif is_dataclass(candidate):
            found[key] = candidate
            try:
                hints = get_type_hints(candidate)
            except (NameError, TypeError):
                # 注解无法解析时退化为字符串注解，不影响该类型本身已被登记。
                hints = dict(candidate.__annotations__)
            for hint in hints.values():
                queue.extend(_unwrap_annotation(hint))

    return [found[key] for key in sorted(found)]


def allowed_msgpack_types() -> list[type]:
    """列出允许被 checkpoint 反序列化的全部项目自定义类型。

    根集合覆盖三类，再由 :func:`_declared_types` 递归补全其嵌套引用：
        · pydantic 模型（``VascCase`` / 产物模型 / 审计留痕模型）；
        · dataclass（``RouteResult`` / ``RuleTrace`` / ``NodeAudit``）；
        · 枚举（``RouteMode`` / ``FieldWriter``）。

    枚举**显式列出**，不靠遍历某个模块的 ``vars()``：那种写法会把「凡定义在
    该模块里的类型」全塞进白名单，包括早已无人引用的死枚举 —— 白名单一旦宽于
    实际需要，checkpoint 反序列化面就被无谓放大，而缩小它的时机永远等不到。
    递归展平（:func:`_declared_types`）仍会自动发现嵌在模型字段里的枚举，
    显式登记只是为了「白名单内容一眼可见」，重复项按全限定名去重。
    """
    from ..models.audit import FieldProvenance, FieldWriter, UrgencyFlagAudit
    from ..models.preop.assessment import AnatomyScore, RiskAssessment
    from ..models.preop.measurement import VascMeasurement
    from ..models.preop.recommendation import FinalRecommendation, RouteMode
    from ..tools.guideline_engine.state import GuidelineEntry, GuidelineReport, NodeOutcome
    from .state import NodeAudit

    roots: list[Any] = [
        VascCase,
        VascMeasurement,
        AnatomyScore,
        RiskAssessment,
        FinalRecommendation,
        FieldProvenance,
        FieldWriter,
        UrgencyFlagAudit,
        RouteMode,
        RouteResult,
        RuleTrace,
        NodeAudit,
        GuidelineEntry,
        GuidelineReport,
        NodeOutcome,
    ]

    return _declared_types(roots)


def build_checkpoint_serde() -> JsonPlusSerializer:
    """构造显式声明了白名单的序列化器。

    为什么不能省：见模块文档「一个必须显式处理的坑」。
    """
    return JsonPlusSerializer(allowed_msgpack_modules=allowed_msgpack_types())


# ===========================================================================
# 图装配
# ===========================================================================
def build_preop_graph(checkpointer: SqliteSaver | None = None) -> Any:
    """装配并编译术前 Agent 图。

    参数：
        checkpointer: 检查点存储；``None`` 表示不落盘。

    注意：本函数是**低层装配 API**，不落盘时不会发出任何告警。若需要
    「默认落盘、显式放弃时告警」这一层策略，请走高层入口 :func:`run_preop`。

    返回：
        编译后的 LangGraph 应用（可 ``invoke``）。
    """
    graph: StateGraph = StateGraph(PreopState)

    # 节点名与四层目录名一致，便于把审计记录直接映射回归属层。
    graph.add_node("input_layer", audited("input_layer", input_node))
    graph.add_node("router_layer", audited("router_layer", router_node))
    graph.add_node("execution_layer", audited("execution_layer", execution_node))
    graph.add_node("output_layer", audited("output_layer", output_node))

    graph.add_edge(START, "input_layer")
    graph.add_edge("input_layer", "router_layer")
    graph.add_edge("router_layer", "execution_layer")
    graph.add_edge("execution_layer", "output_layer")
    graph.add_edge("output_layer", END)

    return graph.compile(checkpointer=checkpointer)


def thread_config(thread_id: str) -> dict[str, Any]:
    """构造 LangGraph 调用配置（一次执行 = 一个 thread）。"""
    return {"configurable": {"thread_id": thread_id}}


#: 进程内运行序号，参与派生 ``thread_id``（见 :func:`_new_run_id`）。
_run_counter = itertools.count(1)


def _new_run_id() -> str:
    """生成「**一次运行**」的标识：UTC 时间戳（微秒）+ 进程内序号。

    为什么需要它：``thread_id`` 在 LangGraph 里是**状态机线程**。对同一个 thread
    再调一次 ``invoke`` 是 **resume（续跑）**，而不是「新开一次运行」—— 于是带
    ``operator.add`` reducer 的 ``audit_trail`` 会把上一次的记录一并带回，审计链
    不变量 ``audit_trail[i].output_hash == audit_trail[i+1].input_hash`` 随之断裂。

    根因是「**审计身份**（这是哪个病例）」与「**运行身份**（这是第几次运行）」
    被压进了同一个键。本函数负责把后者显式表达出来。

    为什么带序号而不纯用时间戳：分区键一旦碰撞，两次运行会被**静默合并**成一条
    线程 —— 正是本函数要消除的那类缺陷。时间戳的读数粒度取决于系统时钟与 Python
    实现，不足以承担唯一性；序号保证**同一进程内绝不重复**，时间戳保证**跨进程
    可排序**。两者合起来才既唯一又可读：直接查库即可按 ``thread_id`` 看出运行先后。
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    return f"{stamp}-{next(_run_counter):04d}"


#: ``run_preop`` 的「未表态」哨兵。
#:
#: 为什么不用 ``None`` 作默认值：``None`` 需要承载一个**有意义的调用意图** ——
#: 「我明确声明本次不落盘」。而「调用方压根没表态」是另一回事，两者必须可区分。
#: 有了哨兵，语义才干净：
#:
#:     ``run_preop(case)``                     → 落盘到默认库（默认即安全）
#:     ``run_preop(case, checkpointer=None)``  → 显式放弃落盘，且出声告警
_PERSIST_BY_DEFAULT: Any = object()


def run_preop(
    case: VascCase,
    *,
    checkpointer: SqliteSaver | None | Any = _PERSIST_BY_DEFAULT,
    thread_id: str | None = None,
    node_inputs: Mapping[str, Any] | None = None,
) -> PreopRunResult:
    """执行一次完整术前流水线，返回最终 state（含运行期元信息）。

    「全程可审计」是红线，所以**默认落盘**：不传 ``checkpointer`` 时会自动打开
    :data:`DEFAULT_CHECKPOINT_DB` 并写入。要放弃落盘必须显式写
    ``checkpointer=None`` —— 此时函数会 ``logging.warning`` 出声，把过去的
    **静默 fail-open**（不落盘且毫无提示）改为 **fail-loud**。

    参数：
        case: 已结构化的术前输入病例。
        checkpointer: 检查点存储。
            · 不传 → 落盘到 :data:`DEFAULT_CHECKPOINT_DB`；
            · ``None`` → **显式**不落盘（并告警）；仅限测试 / what-if 试算 / 压测；
            · 传入 saver → 落盘到该 saver。
        node_inputs: 病种域字段（``{"D1": {"D1.DIAM_METHOD": "超声AP内-内"}}``）。
            这些是**专科评估时医生填**的细节，不在 ``VascCase`` 里（后者是路由专用）。
            进 :data:`..state.HASHED_KEYS` —— 同一病例填不同测量方法会得出不同结论，
            输入变了必须能被审计看出来。缺省为空：工具A 据此报「待补」。
        thread_id: 存储分区键 —— 区分「**审计身份**」与「**运行身份**」。
            · 不传（``None``）→ 派生 ``preop:{case_id}:{run_id}``，**一次运行占一条
              线程**。这样每次调用都从零开始写审计轨迹，链自闭合；而按前缀
              ``preop:{case_id}:`` 仍可检索出该病例的**全部运行史**。
            · 显式传入 → 以传入值为准。此时调用方是在**主动要求 resume**
              （续跑 / 回放同一线程），累积是预期行为，不属于本函数要防的缺陷。

    返回：
        :class:`..state.PreopRunResult` —— 图状态（``case`` / ``case_id`` /
        ``route_result`` / ``tool_outputs`` / ``final_recommendation`` /
        ``audit_trail``）**外加**两项运行期元信息：

            · ``audit_persisted``：本次是否落盘；
            · ``thread_id``：本次实际使用的线程标识。

        第二个键是必需的而非锦上添花 —— 派生标识里含时间戳，调用方无法预先算出，
        若不回传，自动派生的线程就成了「写进去但捞不出来」的死记录。
        两者都不进 ``PreopState``、不进哈希、不进 checkpoint。
    """
    # 一次运行 = 一个独立 thread。理由见 _new_run_id()。
    # 显式传入时以传入值为准：那是有意 resume，与本函数要防的「运行身份丢失」不同。
    resolved_thread_id = (
        thread_id if thread_id is not None else f"preop:{case.case_id}:{_new_run_id()}"
    )

    if checkpointer is _PERSIST_BY_DEFAULT:
        # 在**函数体内**读取模块级 DEFAULT_CHECKPOINT_DB —— 若写成默认参数会在
        # 定义时求值，测试就无法用 monkeypatch 把默认库导向 tmp_path。
        with sqlite_checkpointer(DEFAULT_CHECKPOINT_DB) as saver:
            result = build_preop_graph(checkpointer=saver).invoke(
                {"case": case, "node_inputs": node_inputs or {}}, thread_config(resolved_thread_id)
            )
        persisted = True
    else:
        if checkpointer is None:
            logger.warning(
                "run_preop 未落盘：本次执行不产生任何可事后追溯的审计记录"
                "（case_id=%s，thread_id=%s）。「全程可审计」是项目红线，"
                "只有测试 / what-if 试算 / 压测等非担责场景才应显式传 checkpointer=None。",
                case.case_id,
                resolved_thread_id,
            )
        result = build_preop_graph(checkpointer=checkpointer).invoke(
            {"case": case, "node_inputs": node_inputs or {}}, thread_config(resolved_thread_id)
        )
        persisted = checkpointer is not None

    # 运行期元信息在 invoke **之后**注入。LangGraph 只落盘「被节点写过的通道」，
    # 此刻注入的键不在任何节点的写入集合里，因此不会进入任何 checkpoint 快照。
    return PreopRunResult(
        **result,
        audit_persisted=persisted,
        thread_id=resolved_thread_id,
    )


# ===========================================================================
# 检查点存储
# ===========================================================================
@contextmanager
def sqlite_checkpointer(db_path: str | Path = DEFAULT_CHECKPOINT_DB) -> Iterator[SqliteSaver]:
    """打开一个 SQLite 检查点存储（上下文管理器，退出时关闭连接）。

    ``check_same_thread=False``：LangGraph 可能在非创建线程上读写连接。
    ``setup()``：建表；重复调用是幂等的，故已存在的库可安全复用。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        saver = SqliteSaver(conn, serde=build_checkpoint_serde())
        saver.setup()
        yield saver
    finally:
        conn.close()
