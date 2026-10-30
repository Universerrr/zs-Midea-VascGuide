"""5 个确定性工具的公共契约 —— :class:`ToolSpec` 与 :class:`ToolRequest`。

定位：
    ``src/tools/`` 是 5 个**确定性算子**的唯一实现位置，一工具一模块。它们是
    「输入 → 产物」的纯计算，由代码写死调用（性质等同 ``import math``），
    **不是**交给 LLM 自行决定是否调用的 tool。

    ==================  =========================  ============================
    键                  实现模块                    产物承载
    ==================  =========================  ============================
    guideline_engine    guideline_engine/          指南条目（并入 FinalRecommendation）
    imaging_quantifier  imaging_quantifier.py      VascMeasurement
    anatomy_scorer      anatomy_scorer.py           AnatomyScore
    memory_retriever    memory_retriever.py         历史相似病例（并入 FinalRecommendation）
    risk_calculator     risk_calculator.py          RiskAssessment
    ==================  =========================  ============================

与编排层的关系：
    本包依赖 ``src/models/`` 与 ``src/rules/``（只为取 :class:`RouteResult` 类型）；
    ``src/preop_agent/execution_layer/`` 反过来依赖本包。方向固定为
    ``models/rules ← tools ← preop_agent``，不构成环——``rules`` 侧从不 import 本包。
    因此 :class:`ToolSpec` 定义在这里而不是编排层——工具元信息属于工具契约，
    不属于调度器。

治理红线：
    本包内所有模块必须保持**零 LLM 依赖**。``tests/test_preop_graph.py`` 的
    ``JUDGMENT_LAYER_DIRS`` 已把 ``src/tools`` 纳入 AST 守卫。

为什么工具由代码调度而非 LLM 选择：
    「跑哪些工具」由 ``execution_layer/plan.py`` 的 ``MODE_PLANS`` 决定，
    「传什么参数」由 :class:`ToolRequest` 从 case 里取。LLM 既不选择工具、
    也不传参——它只在 input（抽取）与 output（转译）两端出现。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..models.preop.case import VascCase
from ..rules.router import RouteResult

__all__ = [
    "MissingUpstreamError",
    "ToolRequest",
    "ToolSpec",
]


class MissingUpstreamError(RuntimeError):
    """请求的上游工具产物不存在 —— **不得静默降级**。

    工具之间的依赖是硬依赖（B → VascMeasurement → C → AnatomyScore → E）。
    上游缺失时若返回「默认值」或「空结果」，下游会产出一个看起来正常、
    实际无依据的评分，而这种产物一旦进入审计库就污染了整条证据链。
    """


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个确定性工具的元信息。

    ``blocked_reason`` 为 ``None`` 表示已实现；否则表示**缺什么**才能实现。
    实现一个工具 = 把它的 ``blocked_reason`` 置 ``None`` + 补齐模块内的 ``run()``，
    两者由 ``tests/test_preop_graph.py`` 的结构化测试一起守住。
    """

    key: str
    label: str
    consumes: tuple[str, ...]
    produces: str
    depends_on: tuple[str, ...] = ()
    blocked_reason: str | None = None

    @property
    def implemented(self) -> bool:
        """该工具是否已实现。"""
        return self.blocked_reason is None


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """一次工具调用的完整输入。

    ``route_result`` 是**路由结论整体**，不只取模式：有的工具需要判定产物
    （如 ``CL.TAAA_IV`` 解剖分型），而那些产物**不在** ``VascCase`` 里
    （它们是规则算出来的，不是病例事实）。只传 ``route_mode`` 会让这些工具
    要么读不到、要么被迫自己重算一遍——后者就是第二处判据。

    ``upstream`` 以**工具键**为键，只含本次运行中**已执行完成**的上游产物。
    取值请用 :meth:`require`，不要用 ``.get()`` —— 缺上游是错误，不是缺省。
    """

    case: VascCase
    route_result: RouteResult
    upstream: Mapping[str, Any] = field(default_factory=dict)
    node_inputs: Mapping[str, Any] = field(default_factory=dict)
    """病种域字段，按病种前缀分组（``{"D1": {"D1.DIAM_METHOD": "超声AP内-内"}}``）。

    为什么工具需要它：病种层规则有一半的判据读 ``D1.*`` / ``D3.*`` 这类字段
    （如 ``D1.SEX_THRESHOLD_SOURCE``「仅有 CTA 外径不可直接套用超声阈值」），
    而这些字段**不在** ``VascCase`` 里——后者是路由专用，装的是判定层要用的
    病例事实。没有本通道，D1 的阈值分层只能恒返回 UNKNOWN。

    与 ``upstream`` 的区别：``upstream`` 是**本次运行中其他工具的产物**（算出来的），
    ``node_inputs`` 是**外部录入的输入事实**（医生填的）。前者缺失是调度错误，
    后者缺失是资料不全——处理方式完全不同（抛错 vs 记缺口）。
    """

    def require(self, key: str) -> Any:
        """取上游工具产物。

        抛出：
            MissingUpstreamError: 该工具未在本模式计划内，或尚未执行。
        """
        try:
            return self.upstream[key]
        except KeyError:
            raise MissingUpstreamError(
                f"缺少上游工具产物 {key!r}；本次已完成：{sorted(self.upstream) or '无'}。"
                "请检查 MODE_PLANS 是否漏掉了该工具，或 depends_on 是否声明正确。"
            ) from None
