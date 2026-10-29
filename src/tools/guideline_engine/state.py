"""工具A 的状态与产物类型。

定位：
    病种层（``D1.1``–``D7.3``）的执行状态。与判定层（``src/rules/router.py``）
    的区别在于**产物不同**：判定层输出「去哪个模式」，病种层输出「这个病种
    下一步查什么、缺什么、谁必须签字」。

为什么状态机自己实现、不用 LangGraph：
    依赖方向是 ``models/rules ← tools ← preop_agent``。``StateGraph`` 属编排层，
    工具反向依赖它会成环。病种层的跳转本身很简单（节点间直连 + 三种特殊出口），
    一个 ``dict[str, NodeFn]`` 加 ``next_node`` 就够，不需要框架。

三种出口（与 ``NodeOutcome`` 的字段对应）::

    next_node   继续下一个病种节点（如 D3.1 → D3.2）
    escalation  回流判定层（``"G05"`` 急诊 / ``"G03"`` 重新筛查），本轮停止
    terminal    落到人工决策（术者确认 / 监测 / MDT），本轮停止

    三者互斥：同一节点最多给出一种。同时给多个出口会让「本次到底去哪」失去
    主语——这正是判定层 ``next_nodes`` 用序列而病种层用单值的原因：病种流程
    在任一时刻只走一条路。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from ...models.preop.case import VascCase
from ...rules.router import RouteResult, Verdict

__all__ = [
    "Candidate",
    "CandidateStatus",
    "GuidelineEntry",
    "GuidelineReport",
    "GuidelineState",
    "NodeFn",
    "NodeOutcome",
]


class CandidateStatus(StrEnum):
    """术式候选状态 —— 前四档有规格出处，第五档是本层补的。

    ``可行候选`` / ``条件性候选`` / ``排除`` **逐字**取自 ``D1.3.R91``：
    「分别列出 EVAR/OSR **可行候选**、**条件性候选**和**排除原因**」。
    这是规格全表唯一一处定义治疗方案清单形态的地方，措辞不得自创。

    ``需 MDT 评估`` 用于规格明令交由术者/MDT 裁决的场景：``D1.3.R02`` 写
    「EVAR 不适配不等于 OSR 自动可行，**须由术者/MDT 确认**」。OSR 在缺少
    开放解剖与麻醉评估输入时恒落此档。

    ``未评估`` 用于关键输入整块缺失、连档位都判不出的情况。为什么不能并进
    ``条件性候选``：那会让「只差一个 IFU 核验」与「连瘤颈参数都没有」看起来
    是同一件事，而后者根本不该给出术式方向（``D1.3.R90`` 的阻断口径）。
    """

    FEASIBLE = "可行候选"
    CONDITIONAL = "条件性候选"
    EXCLUDED = "排除"
    MDT = "需 MDT 评估"
    UNASSESSED = "未评估"


@dataclass(frozen=True, slots=True)
class Candidate:
    """一条术式候选 —— 规格「两列（EVAR/OSR）× 三档」的最小载体。

    为什么单开类型、不写成 ``GuidelineEntry`` 或裸字符串：

        · 与 ``entries`` 是**正交切面**。``entries`` 按 ``rule_id`` 排，回答
          「这条规则凭什么成立」；``candidates`` 按术式排，回答「EVAR 现在
          什么状态、OSR 什么状态」。混进同一条会让两种问题共用一格。
        · 裸字符串无法审计。``rule_ids`` 让每条候选能回到支撑它的规则，
          与 ``GuidelineEntry.source_row`` 同一条追溯链。
    """

    procedure: str
    status: CandidateStatus
    reason: str
    rule_ids: tuple[str, ...] = ()

    def render(self) -> str:
        """一行式呈现：``EVAR：条件性候选（IFU 未核验）``。

        渲染**只在这里一处**。输出层通过 ``getattr(obj, "render")`` 调用，
        不自己拼串——措辞两处维护必然漂移。
        """
        return f"{self.procedure}：{self.status}（{self.reason}）"


@dataclass(frozen=True, slots=True)
class GuidelineEntry:
    """病种层的一条结论 —— 必须能回溯到规格的具体行。

    ``source_row`` 形如 ``D3.1!9``（sheet 名 + Excel 行号），与判定层
    :class:`~src.rules.router.RuleSpec` 的 ``source`` 同一口径：改规则必须
    同步改它，否则「本结论出自规格哪一行」就无从核对。

    ``verdict`` 保留三值而不是只存命中的条目：未命中（``False``）与无法判定
    （``None``）在审计里是两回事——前者是「查过，没有」，后者是「查不了」。
    """

    node_id: str
    rule_id: str
    source_row: str
    statement: str
    verdict: Verdict
    next_node: str | None = None


@dataclass(frozen=True, slots=True)
class NodeOutcome:
    """一个病种节点的执行结果。

    ``gaps`` 是**本节点缺什么**的白名单式清单（字段 + 人话原因），直接喂给
    输出层的「待补关键资料」段落。空元组表示本节点输入齐备。

    ``candidates`` 是本节点给出的术式方向，喂给 ``FinalRecommendation.treatment_options``。
    与 ``terminal`` 的区别：``terminal`` 是「本轮停在哪里」单值，``candidates``
    是「每个术式各是什么状态」多值。只有做术式判断的节点（D3.2 / D1.3 / D7.3 …）
    才会填它，其余节点留空。
    """

    node_id: str
    verdicts: dict[str, Verdict]
    entries: tuple[GuidelineEntry, ...]
    next_node: str | None = None
    escalation: str | None = None
    terminal: str | None = None
    gaps: tuple[str, ...] = ()
    candidates: tuple[Candidate, ...] = ()


NodeFn = Callable[["GuidelineState"], NodeOutcome]


@dataclass
class GuidelineState:
    """一次工具A 运行的完整状态。

    ``visited`` 按执行顺序累积，是「决策路径」的唯一载体——不塞进
    :class:`GuidelineEntry`，因为路径回答的是「整体怎么走的」，而条目回答的是
    「这条结论凭什么」，两者粒度不同。
    """

    case: VascCase
    route: RouteResult
    visited: list[NodeOutcome] = field(default_factory=list)
    d3: Mapping[str, object] = field(default_factory=dict)
    """病种域字段通道（``D3.*`` 等）。

    为什么单开一个 dict 而不是往 ``VascCase`` 加字段：``VascCase`` 是**路由专用**
    （161 字段，进哈希），而病种域字段是工具A 的执行细节——``D3.ACCESS_PLAN``
    「左右股髂直径/钙化/迂曲」这类既不影响路由，也不需要进路由哈希。

    当前规格里这些字段**没有抽取来源**（G02 的抽取清单不含 ``D3.*``），所以生产
    调用时通常为空；各节点据此报「待补关键资料」，而不是编一个默认值。
    """

    @property
    def entries(self) -> tuple[GuidelineEntry, ...]:
        """全部节点产出的条目，按执行顺序展平。"""
        return tuple(e for o in self.visited for e in o.entries)

    @property
    def gaps(self) -> tuple[str, ...]:
        """全部节点报告的缺口（去重但保序）。"""
        seen: dict[str, None] = {}
        for outcome in self.visited:
            for gap in outcome.gaps:
                seen.setdefault(gap, None)
        return tuple(seen)

    @property
    def path(self) -> tuple[str, ...]:
        """走过的节点序列。"""
        return tuple(o.node_id for o in self.visited)

    @property
    def candidates(self) -> tuple[Candidate, ...]:
        """全部节点的术式候选，按术式去重。

        为什么**后者覆盖前者**而不是并存：病种流程是单向前进的（``D3.1 → D3.2``），
        越靠后的节点越接近决策。同一术式出现两次只可能是「前面给了草案、后面
        给了定稿」，并存会让下游不知道该读哪一条。

        为什么用 ``dict`` 而非排序：字典保插入序，同一术式重复赋值只改值不改位置，
        输出顺序因此稳定在「首次出现的顺序」——``final_recommendation`` 进哈希，
        顺序抖动会改变哈希。
        """
        merged: dict[str, Candidate] = {}
        for outcome in self.visited:
            for candidate in outcome.candidates:
                merged[candidate.procedure] = candidate
        return tuple(merged.values())


@dataclass(frozen=True, slots=True)
class GuidelineReport:
    """工具A 一次运行的完整产物 —— 比 ``tuple[GuidelineEntry, ...]`` 多三样东西。

    为什么不能只返回条目元组：
        · ``gaps`` 是输出层「待补关键资料」的唯一来源，丢了它等于把「查不了
          什么」吞掉，而这恰恰是急诊最需要交代的信息；
        · ``path`` 回答「怎么走到这一步的」（走过 D3.1 → D3.2），与「每条结论
          凭什么」（``entries`` 的 ``rule_id`` + ``source_row``）粒度不同；
        · ``terminal`` / ``escalation`` 决定本轮停在哪里、要不要回流判定层。

    条目仍然是主体：``entries`` 可单独消费（渲染、审计、论文表格），其余三项
    是它的**上下文**，不混进单条条目里。

    ``candidates`` 是术式维度：EVAR / OSR 各一行状态。它**不是**条目的一部分
    ——规格 ``D1.3.R91`` 要求「分别列出」两列，条目按 ``rule_id`` 排不出来这个
    结构。输出层把它渲染成 ``FinalRecommendation.treatment_options``。
    """

    entries: tuple[GuidelineEntry, ...]
    gaps: tuple[str, ...]
    path: tuple[str, ...]
    terminal: str | None = None
    escalation: str | None = None
    candidates: tuple[Candidate, ...] = ()

    @property
    def empty(self) -> bool:
        """是否一条结论都没有产出。"""
        return not self.entries
