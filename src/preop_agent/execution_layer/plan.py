"""三模式 × 五工具的调度表 —— 「跑哪些工具」的唯一真源。

设计表依据（``06_路由决策流水线``）：:

    模式    工具集        依据
    ======  ============  ======================================
    急诊    ∅             「不预置择期工具」
    复杂    A B C D E     全量评估（MDT 需要完整依据）
    指南    A B           只需指南条目 + 影像量化

注意三者是**包含关系**而非并列关系：``指南 ⊂ 复杂``，``急诊 = ∅``。
所以工具实现按**工具**分文件，不按模式分文件——否则 A、B 会在两个地方
各有一份调用逻辑，形成两处真相。

``tuple`` 的顺序即**声明顺序**，也是拓扑序（上游在前）。真正的执行顺序由
``node.execution_order()`` 按 ``ToolSpec.depends_on`` 再排一次；两者必须一致，
由 ``tests/test_preop_graph.py::TestModePlans`` 机械校验。

新增工具时**必须**同步改 ``MODE_PLANS``：``TestModePlans`` 断言
「复杂模式 = 全部已登记工具」，漏改会直接测试失败——这是刻意设计的摩擦。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from ...models.preop.recommendation import RouteMode

__all__ = ["MODE_PLANS", "plan_for"]

#: 只读映射：防止运行期被改写（调度表是编译期事实，不是运行期配置）。
MODE_PLANS: Final[Mapping[RouteMode, tuple[str, ...]]] = MappingProxyType(
    {
        RouteMode.EMERGENCY: (),
        RouteMode.COMPLEX: (
            "guideline_engine",
            "imaging_quantifier",
            "anatomy_scorer",
            "memory_retriever",
            "risk_calculator",
        ),
        RouteMode.GUIDELINE: ("guideline_engine", "imaging_quantifier"),
    }
)


def plan_for(mode: RouteMode | None) -> tuple[str, ...]:
    """取某路由模式的工具计划。

    参数：
        mode: 已回填的路由模式。

    返回：
        该模式要执行的工具键元组（可能为空）。

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
