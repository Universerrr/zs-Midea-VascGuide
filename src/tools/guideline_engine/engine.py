"""工具A · 指南引擎 —— 病种 + 解剖分型 + 直径 → 病种层结论。

定位：
    把「病例落在哪一档」翻译成「规格这一档要求做什么」。这是唯一能产出
    **逐条可回溯到规格行**的结论的工具，也是「病例锚定」在输出侧的落点：
    每条结论都带 ``rule_id`` + ``source_row``（如 ``D3.1!10``），审计时可回查原文。

输入契约（``SPEC.consumes`` 逐项列出）：
    ``route_result.class_output``  病种标签（G08 的产物）—— 决定从哪几个入口进
    ``route_result.cl_taaa_iv``    解剖分型（判定产物，不重算）—— 决定条目子集
    ``case.*``                     判据要读的病例事实（见 ``spec.py``）
    ``request.node_inputs``        病种域字段（``D1.*`` / ``D3.*`` 等），按前缀分组

输出契约：
    :class:`~.state.GuidelineReport`，五个部分各回答一个问题：

    ``entries``    每条结论凭什么 —— ``rule_id`` / ``source_row`` / 三值 ``verdict``
    ``gaps``       还差什么资料 —— 输出层「待补关键资料」的唯一来源
    ``path``       实际走过哪些节点 —— 与 ``entries`` 粒度不同
    ``terminal``   本轮停在哪、为什么停
    ``candidates`` 术式维度（EVAR / OSR 各一行状态），喂给 ``FinalRecommendation``

    空报告表示「没有可用结论」。**不得**用泛泛而谈的默认文案填充——
    「查不到」本身就是要如实上报的结论。

实现要点（验收标准）：
    1. 条目来源是 ``docs/VascGuide_V3.6.2.xlsx`` 的 ``D1.1``–``D7.3`` 共 21 个 sheet /
       172 条病种规则（规格副本，只读）。``docs/`` 下曾有的旧路由表与决策流程图
       （``路由表1/2.xlsx``、``诊断决策路由.pdf``、``VascCase字段设计表.xlsx``）
       已于 2026-11-04 删除——它们与 V3.6 的坐标系不同，混用会产生两套判据。
       现行 ``docs/`` 只留规格副本、思维导图与 ``spec-diff``。
    2. 判据写在 ``nodes/`` 里、不运行时读 Excel：规格会被修订，运行时依赖它会让
       审计无法复现。行号只作为 ``source_row`` 存下来做凭据；对账由
       ``tests/test_guideline_engine.py::TestSpecRuleRows`` 在测试期完成。
    3. 三值求值：``True`` / ``False`` / ``None``。未命中（``False``）与无法判定
       （``None``）在审计里是两回事——前者是「查过，没有」，后者是「查不了」。
    4. 规格条目是**本项目转写**的判据与动作，不是 ESVS 推荐原文。要输出
       「章节 + 推荐序号 + DOI」需要另建结构化条目库（尚未实现），届时按
       ``source_row`` 关联，不必改判据。
    5. 解剖分型 ``CL.TAAA_IV`` 是**判定产物**（不进 ``VascCase``），已由
       ``RouteResult.cl_taaa_iv`` 带出。本工具**从 ``request.route_result`` 读**，
       不得从 ``c.aorta`` + ``c.maxd`` 重算——那是第二处判据。取「不可评价」时
       按资料不足处理：宁可少给条目，也不要按「不是 Ⅳ 型」错选子集。
    6. 髂瘤 40mm 的修复阈值归 ``nodes/common.py`` 的 ``ILIAC_REPAIR_MM``
       （``D7`` 判据消费），**不在本文件里再写一遍**。

模块结构（本包）：
    ``spec.py``   ``SPEC`` 定义（``run()`` 要引用 ``SPEC.label``，故不能在
                  ``__init__.py`` 里定义，否则循环导入）
    ``engine.py`` 本文件 —— 图装配 + 驱动 + 出口转换
    ``state.py``  节点图状态与产物类型
    ``registry.py`` 21 个节点 / 172 条规则的登记表
    ``nodes/``    21 个病种节点，按病种分文件（D1–D7 各 3 节点）+ ``common.py``
                  承载 R90 兜底 / R91 出口 / 跳转守卫与跨节点共用判据
"""

from __future__ import annotations

from dataclasses import replace

from ..base import ToolRequest
from .registry import NODES, SPEC_NODE_SHEETS
from .state import GuidelineReport, GuidelineState, Link, NodeOutcome

__all__ = ["run"]

#: ``G08`` 产出的病种标签 → 该病种的入口节点。
#:
#: 标签是 ``D1``–``D7``（``G08.R01``–``R07`` 的产物），节点是 ``Dn.1``。
#: ``Ⅳ型TAAA扩展`` 是**扩展标签**（``G04.R05`` 的产物），不另设第八病种，
#: 故不在此表内——它不改变入口。
_ENTRY_BY_LABEL: dict[str, str] = {
    "D1": "D1.1",
    "D2": "D2.1",
    "D3": "D3.1",
    "D4": "D4.1",
    "D5": "D5.1",
    "D6": "D6.1",
    "D7": "D7.1",
}


def _entry_nodes(state: GuidelineState) -> tuple[str, ...]:
    """按病种标签取入口节点。多标签可共存（``G08`` 允许多病种）。"""
    labels = state.route.class_output or ()
    return tuple(_ENTRY_BY_LABEL[label] for label in labels if label in _ENTRY_BY_LABEL)


def _walk(state: GuidelineState) -> None:
    """深度优先遍历病种节点，直到无下一节点或落到终态。

    为什么自己去重 ``seen``：规格允许跨病种跳转（``D6.3 → D4.3``），多标签病例
    可能从两条路走进同一节点。重复执行会让 ``path`` 出现环，也会让同一条结论
    出现两次——审计时无法分清「判了两次」还是「判了一次被复制」。
    """
    queue = list(_entry_nodes(state))
    seen: set[str] = set()

    while queue:
        node_id = queue.pop(0)
        if node_id in seen:
            continue
        seen.add(node_id)

        fn = NODES.get(node_id)
        if fn is None:
            # 节点未实现时不留空结论：``terminal`` 要能直接进主建议，
            # 计数取实时值，避免文案随开发进度漂移。
            missing_hint = (
                f"{node_id}：病种节点尚未实现"
                f"（规格 {len(SPEC_NODE_SHEETS)} 个，当前已实现 {len(NODES)} 个）"
            )
            state.visited.append(
                NodeOutcome(
                    node_id=node_id,
                    verdicts={},
                    entries=(),
                    terminal=f"未进入病种流程（{node_id} 尚未实现）",
                    gaps=(missing_hint,),
                )
            )
            continue

        outcome = fn(state)
        state.visited.append(outcome)

        if outcome.terminal is not None or not outcome.next_node:
            continue

        # 并行出口：规格写「A **并进入** B」时两个节点都要执行（如 ``D4.1.R05``
        # 「D4.2 与 D5.3 并行评估」）。只走 ``next_node`` 会让另一半静默消失——
        # 报告里出现「并行评估」四个字，实际只执行了一边。
        #
        # 只在 ``link is PARALLEL`` 时展开：``EITHER`` 的两个值互斥（走了急症就
        # 不走常规），``FOLLOW_UP`` 有先后（先做 A，B 是之后的事），都不该在这里
        # 一次走完。
        #
        # 非节点目标（``G03`` / ``G05`` / ``人工复核``）自然被 ``in NODES`` 滤掉，
        # 它们由 ``escalation``/``terminal`` 承载，不是病种节点。
        if outcome.link is Link.PARALLEL:
            parallel = [t for t in outcome.next_nodes if t in NODES and t not in seen]
            if parallel:
                queue[:0] = parallel
                continue

        target = outcome.next_node
        if target in NODES:
            if target not in seen:
                queue.insert(0, target)
            continue

        # 下一节点尚未实现：把缺口挂在**当前节点**上，让它看得见。
        #
        # 为什么不静默跳过：D1.1 判出「达阈值 → 转 D1.2」，而 D1.2 还没写。若
        # 直接 `continue`，输出里既没有 D1.2 的条目也没有任何提示，读者会以为
        # 流程到此为止——而实际上它断在一个未实现的分支上。
        #
        # 为什么不往 ``path`` 里塞一个假节点：``path`` 的语义是「**真正执行过**
        # 的节点」，未实现的节点没执行过，塞进去会让审计误以为跑过。
        note = (
            f"{target}：病种节点尚未实现"
            f"（规格 {len(SPEC_NODE_SHEETS)} 个，当前已实现 {len(NODES)} 个）"
        )
        state.visited[-1] = replace(
            outcome,
            gaps=(*outcome.gaps, note),
            # 补一个可读的终点：否则主建议会输出「病种流程未给出结论」，
            # 而实际情况是「有结论，但下一站还没修好」。
            terminal=outcome.terminal or f"待进入 {target}（该节点尚未实现）",
        )


def _exit_phrase(outcome: NodeOutcome) -> str:
    """把规格 M 列的「下一节点/终点」原样搬进结论句。

    为什么不再写「应回流 X」：规格里没有「回流」这个概念。D 层各表的 M 列写的是
    「下一节点/终点：G03、G05、D4.3」这类**去处的值域**，动作列才说明怎么选
    （择一 / 并行 / 后续）。用「回流」描述它，等于给一个不存在于规格的动作命名，
    再把「系统没执行这个动作」当成缺陷 —— 而规格从未要求工具A 自己去跳。

    ``_walk`` 只在 :data:`NODES` 内部跳转，G03/G05/G06/G08 不在其中，所以这些
    去处只能交给编排层/临床团队。措辞只陈述规格写了什么，不声称系统做了什么。
    """
    nodes = outcome.next_nodes
    if not nodes:
        return ""
    listed = "、".join(nodes)
    if len(nodes) == 1:
        return f"（下一节点：{listed}）"
    return f"（下一节点：{listed}；{outcome.link.value}）"


def _closing_advice(state: GuidelineState) -> str | None:
    """本次运行停在哪、为什么停——报告的 ``terminal``。

    先看末端节点自己的 ``terminal``；没有就用它**命中的那条规则**拼一句。

    为什么不能退回去找更早节点的 ``terminal``（修复前的写法）：14 条
    ``exit_kind="escalate"`` 且无 ``also_next`` 的规则（``D1.1.R01`` 去 G03、
    ``D4.3.R01`` 去 G05、``D4.1.R91`` 去 G08…）都只给去处、不给终点。
    此时上游节点还留着它的终点，于是一份「无感染证据 → 回 G08」的病例，
    报告末尾写的是上游那条分支的终点（例如「疑似快速增长，待复测复核」）——
    描述的是另一条分支，读起来像结论，其实什么都没说。
    """
    if not state.visited:
        return None

    last = state.visited[-1]
    if last.terminal:
        return last.terminal

    hit = next((entry for entry in last.entries if entry.verdict is True), None)
    if hit is None:
        return None
    return f"{last.node_id}：{hit.statement}{_exit_phrase(last)}"


def run(request: ToolRequest) -> GuidelineReport:
    """跑一遍病种流程，返回结论 + 缺口 + 路径。

    参数：
        request: ``case`` 提供临床字段，``route_result`` 提供病种标签
            （``class_output``）与解剖分型（``cl_taaa_iv``）。

    返回：
        :class:`GuidelineReport`。``entries`` 为主产物；``gaps`` 供输出层的
        「待补关键资料」段落直接消费。

    行为边界：
        · ``class_output`` 为空（未分到病种）→ 返回空报告，不编默认条目；
        · 病种节点未实现 → 记一条「未实现」缺口，继续处理其余标签；
        · 任何情况下都不写回 ``case`` —— 病种流程的产物属于本次运行。
    """
    state = GuidelineState(
        case=request.case,
        route=request.route_result,
        node_inputs=request.node_inputs,
    )
    _walk(state)

    return GuidelineReport(
        entries=state.entries,
        gaps=state.gaps,
        path=state.path,
        # 多病种标签时取**路径末端**的那个终点（见 ``_closing_advice``）。
        #
        # 为什么不取第一个：``G08`` 允许多标签，且标签顺序固定是 D1 → D7，
        # 于是「D1 无症状 AAA + D4 感染」这类病例的 D1 链先跑完，它的终点是
        # 「监测／随访」或「候选已列出」——那是**基础病种**的结论。真正决定本例
        # 处置的是随后跑出来的附加病种结论（感染要控制感染源，不是继续监测）。
        # 取末端节点即「本次运行实际停在哪里」，与 ``path`` 的语义一致。
        #
        # ``escalation`` 仍取第一个：它回答的是「要不要回流判定层」这件是非题，
        # 多个节点同时给出回流信号时，最早的那个已经足以定性。
        terminal=_closing_advice(state),
        escalation=next((o.escalation for o in state.visited if o.escalation), None),
        candidates=state.candidates,
    )
