"""工具A · 指南引擎 —— 病种 + 解剖分型 + 直径 → 指南推荐条目。

定位：
    把「病例落在哪一档」翻译成「指南怎么说」。这是唯一能产出**指南原文级**依据的
    工具，也是「病例锚定」在输出侧的落点：每条推荐都要能回溯到
    指南章节 → 推荐序号 → DOI。

输入契约（``SPEC.consumes``）：
    ``case.cl.diag_multi``       病种标签 —— 决定查哪一族条目
    ``case.c.maxd``              主动脉最大径（CTA 报告提取值）—— 决定 A01（小型）/
                                 A02（较大）档位。**不是** ``cl.aaa_diam``：后者是
                                 工具 B 的影像量化值，用报告值才能保证总有输入
                                 （B6 回落口径）
    ``route_result.cl_taaa_iv``  解剖分型 —— 决定条目子集。取值 符合 / 疑似 /
                                 不符合 / 不可评价。「不可评价」必须单独处理：
                                 资料不足 ≠ 不是 Ⅳ 型，不能按「否」选条目

输出契约：
    ``tuple[GuidelineEntry, ...]``，按相关度降序。每条至少含四个属性：

    ``section``             章节 / 推荐序号（如 ``§4.4; Rec 22``）
    ``recommendation``      推荐原文
    ``evidence_attribute``  证据属性枚举（指南直接来源 / 工程设计来源 /
                            工程条件·非 ESVS 统一阈值 / 临床扩展待审核）
    ``doi``                 文献 DOI

    空元组表示「没有命中条目」。**不得**用泛泛而谈的默认文案填充——
    「查不到」本身就是要如实上报的结论。

实现要点（验收标准）：
    1. 条目来源是 ``VascGuide_V3_6.xlsx`` 的 ``D1.1–D7.3`` 共 21 个 sheet /
       172 条病种规则（外部规格，只读）。``docs/`` 下的旧路由表与决策流程图
       均已作废，**不得回看**——它们与 V3.6 的坐标系不同，混用会产生两套判据。
    2. ``evidence_attribute`` 必须是枚举而非自由文本。规格已把「ESVS 原文」
       与「工程条件·非 ESVS 统一阈值」（如 D6 的 15mm/45°/7mm）分开标注；
       输出侧若合并成一段话，审计时无法判断哪条真是指南说的。
    3. 每条带 ``doi``。当前单一指南源为 ESVS 2024（Wanhainen et al.，
       ``10.1016/j.ejvs.2023.11.002``）。
    4. 条目库需要「结构化入库 + 检索接口」，而不是运行时读 Excel：
       规格文件在仓库外且会被修订，运行时依赖它会让审计无法复现。
    5. **已知缺口（待口径裁定，勿擅自补）**：``consumes`` 未含 ``CL.ILIAC_DIAM``，
       而设计表第 9 条要求髂瘤 40mm 单独分层。该值在 V3.1(2) 里已有落点
       （``case.cl.iliac_diam``），但「40mm 是否单列一档、算不算 ESVS 原文阈值」
       尚未裁定——补齐前不要在本工具里硬编码 40mm，也不要把直径判定搬回
       ``router.py``（``05_说明与待确认`` 第 17 条与 ``03_P1-P8判定映射`` 的
       P7 行对此**自相矛盾**，须先裁定）。
    6. 解剖分型 ``CL.TAAA_IV`` 是**判定产物**（不进 ``VascCase``），已由
       ``RouteResult.cl_taaa_iv`` 带出（2026-10-26 裁定，与 ``K.*`` 回填同路）。
       本工具**从 ``request.route_result`` 读**，不得从 ``c.aorta`` + ``c.maxd``
       重算——那是第二处判据。取「不可评价」时按资料不足处理：宁可少给条目，
       也不要按「不是 Ⅳ 型」错选子集。

模块结构（本包）：
    ``spec.py``   ``SPEC`` 定义（``run()`` 要引用 ``SPEC.label``，故不能在
                  ``__init__.py`` 里定义，否则循环导入）
    ``engine.py`` 本文件 —— 图装配 + 驱动 + 出口转换
    ``state.py``  节点图状态与产物类型
    ``nodes/``    21 个病种节点，按病种分文件（D1–D7 各 3 节点）+ ``common.py``
                  承载 R90 兜底 / R91 出口 / 跳转守卫（21 个 sheet 各 2 条，7 病种共用）
"""

from __future__ import annotations

from ..base import ToolRequest
from .registry import NODES, SPEC_NODE_SHEETS
from .state import GuidelineReport, GuidelineState, NodeOutcome

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

        if (
            outcome.terminal is None
            and outcome.next_node
            and outcome.next_node in NODES
            and outcome.next_node not in seen
        ):
            queue.insert(0, outcome.next_node)


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
    state = GuidelineState(case=request.case, route=request.route_result)
    _walk(state)

    return GuidelineReport(
        entries=state.entries,
        gaps=state.gaps,
        path=state.path,
        terminal=next((o.terminal for o in state.visited if o.terminal), None),
        escalation=next((o.escalation for o in state.visited if o.escalation), None),
        candidates=state.candidates,
    )
