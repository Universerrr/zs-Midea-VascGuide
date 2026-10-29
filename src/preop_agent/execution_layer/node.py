"""执行层节点 —— 按路由模式调度 5 个**确定性**工具的挂载点。

定位（交接文档第二节「数据流」）：
    本层是「决策产物」的唯一产地。``VascCase`` 是**输入**，本层产物**不回写**
    ``VascCase``：

    ==========  ==================  ==========================
    工具        产物                承载模型
    ==========  ==================  ==========================
    A 指南引擎   指南推荐条目          （并入 FinalRecommendation）
    B 影像量化器 容积 / 各段直径 / 钙化  VascMeasurement
    C 解剖评分器 解剖评分分值          AnatomyScore
    D 记忆检索器 历史病例 / 既往证据     （并入 FinalRecommendation）
    E 风险计算器 风险分值              RiskAssessment
    ==========  ==================  ==========================

调度规则：
    跑哪些工具由 ``plan.MODE_PLANS`` 决定，顺序由 ``SPEC.depends_on`` 决定。
    执行层**不再**「跑所有已实现的工具」——那会让指南模式连带跑 C/D/E。

    **跑不成怎么办由 ``Necessity`` 决定**（V3.6.2 起）：

    · ``REQUIRED``（复杂 / 指南）：任一工具失败即节点失败。择期路径的产物是
      MDT 判据的前提，缺一项就是「不完整的依据」，不能靠默认值补齐。
    · ``BEST_EFFORT``（急诊）：失败降级为「本次未获取」，原因写进 ``tool_skips``，
      节点继续。这是 G05「不等待非必要分析」的落地——急诊必须在信息不全时
      仍然给出可执行的下一步。

治理红线：
    5 个工具**全部是确定性算子**，不是「交给 LLM 自行决定是否调用的 tool」。
    它们由代码写死调用，性质等同于 ``import math``；本层不得引入任何 LLM 客户端。

当前状态：**5 个工具全部未实现**（:data:`IMPLEMENTED_TOOLS` 为空）。
    每个工具都在 ``src/tools/<tool>.py`` 里写明了**阻塞原因**——不是「待办」，
    而是缺什么具体资源。缺少资源的工具不应仓促实现，否则产生无法验证的产物。
    因此本节点当前返回空的 ``tool_outputs``；下游据此保持谨慎（人工复核）。
"""

from __future__ import annotations

from typing import Any

from ...tools.base import ToolSpec
from ..state import PreopState
from .plan import MODE_PLANS, Necessity, ToolPlanItem, plan_for
from .registry import (
    IMPLEMENTED_TOOLS,
    SPEC_BY_KEY,
    TOOL_REGISTRY,
    run_tool,
    unimplemented_tools,
)

__all__ = [
    "IMPLEMENTED_TOOLS",
    "MODE_PLANS",
    "TOOL_REGISTRY",
    "Necessity",
    "ToolPlanItem",
    "ToolSpec",
    "execution_node",
    "execution_order",
    "run_tool",
    "unimplemented_tools",
]


def execution_order(
    plan: tuple[ToolPlanItem, ...],
    implemented: frozenset[str],
) -> tuple[ToolPlanItem, ...]:
    """把计划排成拓扑序，并**只保留已实现**的工具。

    参数：
        plan: 某模式的计划项元组（``MODE_PLANS[mode]``）。
        implemented: 当前已实现的工具键集合（生产调用传 :data:`IMPLEMENTED_TOOLS`；
            测试可传全量集合以验证排序，无需实现任何工具）。

    返回：
        可执行的计划项元组，上游在前（每项保留原 ``necessity``）。

    抛出：
        RuntimeError: 计划里出现未登记的工具；依赖的工具不在计划内；
            依赖成环；或「工具已实现但上游未实现」（半截工具链）。
            —— 这四种情况都必须响亮失败，不允许静默跳过：静默跳过等于
            用缺失的上游算出一个看起来正常的产物。
    """
    by_key: dict[str, ToolPlanItem] = {item.tool: item for item in plan}
    ordered: list[ToolPlanItem] = []
    done: set[str] = set()

    def visit(item: ToolPlanItem, path: tuple[str, ...]) -> None:
        key = item.tool
        if key in done:
            return
        if key in path:
            raise RuntimeError("工具依赖成环：" + " → ".join((*path, key)))

        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            raise RuntimeError(f"计划里的工具 {key!r} 未在 TOOL_REGISTRY 登记。")

        for dep in spec.depends_on:
            if dep not in by_key:
                raise RuntimeError(
                    f"{spec.label} 依赖 {dep!r}，但 {dep!r} 不在当前模式的计划内"
                    f"（计划：{', '.join(by_key) or '空'}）。"
                )
            if dep not in implemented:
                raise RuntimeError(
                    f"{spec.label} 已实现，但上游 {dep!r} 未实现——半截工具链不得静默产出。"
                )
            visit(by_key[dep], (*path, key))

        done.add(key)
        ordered.append(item)

    for item in plan:
        if item.tool in implemented:
            visit(item, ())

    return tuple(ordered)


def execution_node(state: PreopState) -> dict[str, Any]:
    """执行层节点：按路由结论的模式取计划 → 拓扑排序 → 逐工具执行。

    参数：
        state: 至少含 ``case`` 与 ``route_result``（模式由后者给出）。

    返回：
        含 ``tool_outputs`` 与 ``tool_skips`` 的状态增量：

        · ``tool_outputs`` 以**工具键**为键；缺项即代表该工具未执行
          （与 ``..state`` 约定一致）；
        · ``tool_skips`` 以**工具键**为键、值为失败原因，只记 ``BEST_EFFORT``
          工具的失败。``REQUIRED`` 工具失败会直接抛出，不会出现在这里。

    非模式出口（人工复核 / 资料核实）**不跑任何工具**：那两个出口的含义就是
    「判定依据不足，先别算」，此时跑工具等于用不可信输入产出看似正常的产物。

    抛出：
        RuntimeError: state 里没有 ``route_result``（未经路由层）。
        任何异常: 当失败工具的必要性是 ``REQUIRED`` 时，原样抛出。
    """
    result = state.get("route_result")
    if result is None:
        raise RuntimeError(
            "execution_layer 需要 state['route_result']：请在 router_layer 之后调用。"
        )
    if result.mode is None:
        # REVIEW / VERIFY：无模式即无计划，返回空产物（不抛错，这是正常路径）。
        return {"tool_outputs": {}, "tool_skips": {}}
    plan = plan_for(result.mode)

    outputs: dict[str, Any] = {}
    skips: dict[str, str] = {}
    for item in execution_order(plan, IMPLEMENTED_TOOLS):
        try:
            outputs[item.tool] = run_tool(item.tool, state, upstream=outputs)
        except Exception as exc:
            if item.necessity is Necessity.REQUIRED:
                raise
            # 急诊：记下「为什么没拿到」，让下游能把它变成「待补关键资料」。
            skips[item.tool] = f"{type(exc).__name__}: {exc}"

    return {"tool_outputs": outputs, "tool_skips": skips}
