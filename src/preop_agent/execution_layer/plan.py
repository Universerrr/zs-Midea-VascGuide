"""三模式 × 五工具的调度表 —— 「跑哪些工具、跑不成怎么办」的唯一真源。

## 计划项不只是工具名

``MODE_PLANS`` 的值是 :class:`ToolPlanItem`，携带一个 :class:`Necessity`：

====================  ==========================================  ==========================
必要性                语义                                         跑不成时
====================  ==========================================  ==========================
``REQUIRED``          择期路径：这个产物是下游判据的前提              节点失败（fail-loud）
``BEST_EFFORT``       急诊路径：有输入就跑，没有就跳过并记录原因      记入 ``tool_skips`` 继续
====================  ==========================================  ==========================

为什么需要这个区分，而不是「统一都不抛」或「统一都抛」：

    ``VascGuide_V3.6.2`` 的 G05 原文是「急诊安全规则/通知先行；A指南与B影像按
    **可用性**快速调用；C/D/E按**需**调用；**不等待**非必要分析」。这三句话
    描述的是「产物之间的关系」，不是「跑或不跑」的开关 —— 只有 ``REQUIRED`` /
    ``BEST_EFFORT`` 两态才能同时表达「急诊不能因为缺 CTA 量化就不出结论」和
    「择期不能拿默认值冒充量化结果」。

为什么是两态而不是三态（``required`` / ``if_available`` / ``on_demand``）：

    ``if_available`` 与 ``on_demand`` 的行为完全一致（跑不了就跳过 + 记录），
    差别只在**跳过原因**——而原因由 ``MissingUpstreamError``（缺上游）或工具
    自身的输入检查（缺自身输入）自然给出。多造一个枚举值等于让同一个行为有
    两个名字。

模式与工具集（V3.6.2）::

    模式    工具集        必要性                  依据
    ======  ============  ======================  ==================================
    急诊    A B C D E     全 BEST_EFFORT           G05：按可用性/按需，不等非必要分析
    复杂    A B C D E     全 REQUIRED              MDT 需要完整依据，缺一项即不完整
    指南    A B           全 REQUIRED              只需指南条目 + 影像量化

三者仍是**包含关系**：``指南 ⊂ 复杂``。所以工具实现按**工具**分文件，不按模式分
文件——否则 A、B 会在两个地方各有一份调用逻辑，形成两处真相。

``tuple`` 的顺序即**声明顺序**，也是拓扑序（上游在前）。真正的执行顺序由
``node.execution_order()`` 按 ``ToolSpec.depends_on`` 再排一次；两者必须一致，
由 ``tests/test_preop_graph.py::TestModePlans`` 机械校验。

新增工具时**必须**同步改 ``MODE_PLANS``：``TestModePlans`` 断言
「复杂模式 = 全部已登记工具」，漏改会直接测试失败——这是刻意设计的摩擦。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ...models.preop.recommendation import RouteMode

__all__ = ["MODE_PLANS", "Necessity", "ToolPlanItem", "plan_for"]


class Necessity(StrEnum):
    """工具的调用必要性 —— 决定「跑不成」时是失败还是降级。

    成员值进审计（``tool_skips`` 的键集由它决定），改动须视为语义变更。
    """

    REQUIRED = "required"
    """必跑：失败即节点失败。择期路径用它守住「不拿默认值冒充结论」。"""

    BEST_EFFORT = "best_effort"
    """尽力：跑不成降级为「本次未获取」，原因进 ``tool_skips``，不阻断出口。"""


@dataclass(frozen=True, slots=True)
class ToolPlanItem:
    """一条工具调用计划：跑哪个工具 + 跑不成算不算失败。"""

    tool: str
    necessity: Necessity = Necessity.REQUIRED


def _required(tool: str) -> ToolPlanItem:
    return ToolPlanItem(tool, Necessity.REQUIRED)


def _best_effort(tool: str) -> ToolPlanItem:
    return ToolPlanItem(tool, Necessity.BEST_EFFORT)


#: 只读映射：防止运行期被改写（调度表是编译期事实，不是运行期配置）。
MODE_PLANS: Final[Mapping[RouteMode, tuple[ToolPlanItem, ...]]] = MappingProxyType(
    {
        # G05：安全规则/通知先行 → A 指南、B 影像按可用性快速调用 → C/D/E 按需。
        # 全部 BEST_EFFORT：急诊的价值是「在信息不全时也给出可执行的下一步」，
        # 任何一个工具缺席都不能换来一个空结论。
        RouteMode.EMERGENCY: (
            _best_effort("guideline_engine"),
            _best_effort("imaging_quantifier"),
            _best_effort("anatomy_scorer"),
            _best_effort("memory_retriever"),
            _best_effort("risk_calculator"),
        ),
        RouteMode.COMPLEX: (
            _required("guideline_engine"),
            _required("imaging_quantifier"),
            _required("anatomy_scorer"),
            _required("memory_retriever"),
            _required("risk_calculator"),
        ),
        RouteMode.GUIDELINE: (
            _required("guideline_engine"),
            _required("imaging_quantifier"),
        ),
    }
)


def plan_for(mode: RouteMode | None) -> tuple[ToolPlanItem, ...]:
    """取某路由模式的工具计划。

    参数：
        mode: 已回填的路由模式。

    返回：
        该模式的计划项元组（可能为空）。

    抛出：
        RuntimeError: ``mode`` 为 ``None`` —— 说明 case 还没过路由层。
            直接构造的 ``VascCase`` 该字段默认为 ``None``，此时执行任何工具
            都是无依据的。
        ValueError: 出现未登记的模式（枚举扩展后忘了补计划）。
    """
    if mode is None:
        raise RuntimeError(
            "execution_layer 需要已路由出模式的病例（route_result.mode）："
            "请在 router_layer 之后、且结论为「进入模式」时调用。"
            "（直接构造的 VascCase 该字段为 None，无模式即无计划。）"
        )
    try:
        return MODE_PLANS[mode]
    except KeyError:
        raise ValueError(f"路由模式 {mode!r} 没有登记工具计划，请补 MODE_PLANS。") from None
