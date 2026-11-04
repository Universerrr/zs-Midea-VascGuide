"""D1 · 无症状 AAA（``D1.1`` → ``D1.2`` → ``D1.3``）。

规格来源：``VascGuide_V3.6.2.xlsx`` 的 ``D1.1`` / ``D1.2`` / ``D1.3`` 三个 sheet，
共 31 条规则（10 + 11 + 10），全部实现。

## 三个节点各管什么

``D1.1`` 诊断与直径分层
    回答「这个瘤子现在要不要考虑手术」。核心是**性别 + 直径 + 测量方法**三件套：
    男性 ≥55 mm / 女性 ≥50 mm → ``D1.2`` 评估修复获益；未达 → 监测 + 随访间隔。

``D1.2`` 患者获益与手术风险
    回答「这个人耐不耐受手术、愿不愿意做」。规格 11 条里 9 条读 ``D1.OP_FITNESS``
    / ``D1.LIFE_EXPECTANCY`` / ``D1.FRAILTY`` / ``D1.PREFERENCE`` —— 全是医生
    评估结论，本系统不生成。节点的职责是**点名缺哪项**、**按已有结论分流**。

``D1.3`` 术式候选
    回答「EVAR 还是 OSR」。判据与 ``D3.2`` 同构（近端解剖 + 入路 + IFU + 远端
    密封区），但这里是**择期**：有时间补资料，所以缺项只落缺口、不阻断方向。

## 规格在 D1.1 埋的一处硬约束（本文件的实现难点）

``D1.SEX_THRESHOLD_SOURCE`` 原文::

    ESVS常规性别阈值以指南定义的标准测量为基础；若**仅有 CTA 外径，不可未经
    方法核对直接套用超声阈值**。

也就是说，光有 ``cl.aaa_diam`` 这个数字**不足以**判 55/50 mm 阈值——还必须知道
它是超声 AP 内-内还是 CTA 中心线外径（两者相差 2–4 mm，在临界值上会翻转结论）。
故本节点把「直径能否用于阈值判断」做成独立三值判据
:func:`_threshold_usable`：方法未知 → **不套阈值**，报缺口让医生补
``D1.DIAM_METHOD``。这是规格指定的行为，不是实现偷懒。

## 为什么 D1.1 值得先做

它是 172 条规则里**唯一一段只靠 ``VascCase`` 现成字段就能跑出真实结论**的分层逻辑：
性别（``p.sex``）、直径（``cl.aaa_diam``）、症状（``cl.symptom_aaa``）、
破裂状态（``cl.aaa_rupture``）、形态（``cl.saccular`` / ``cl.pseudo`` /
``n.inflammatory_ctx``）全在模型里。缺的只有测量方法、有效性、增长速率、家族史
四件，走 ``node_inputs`` 通道。

## D1.3 的两列候选为什么不走 ``evaluate_node`` 的出口竞争

``D1.3`` 的 ``EVAR`` 与 ``OSR`` **不是互斥选项**——规格 ``R91`` 要求「分别列出
EVAR/OSR 可行候选、条件性候选和排除原因」。若把 ``R01``（EVAR 候选）与 ``R02``
（OSR 候选）放进 ``RuleSlot``，先命中的那一列会遮蔽另一列，主建议就会写成
「OSR 候选」，而 EVAR 其实也可行。故这四条只做**展示性规则**（求值 + 留痕，
不抢出口），出口由 ``R90`` / ``R08`` / ``R03`` / ``R06`` / ``R04`` / ``R91`` 定。
"""

from __future__ import annotations

from dataclasses import replace

from ....rules.router import Verdict, and_, eq, ge, not_, or_, token
from ..state import (
    Candidate,
    CandidateStatus,
    GuidelineEntry,
    GuidelineState,
    Link,
    NodeFn,
    NodeOutcome,
)
from .common import (
    DisplayRule,
    RuleSlot,
    aaa_diameter,
    access_blocked,
    anatomy_available,
    at_repair_threshold,
    branch_involved,
    cta_evaluable,
    display_entries,
    evaluate_node,
    gap_if_missing,
    is_spec_uncertain,
    missing_fields,
    recorded,
    sex_threshold,
    spec_token,
    threshold_usable,
)

__all__ = ["NODES", "d1_1", "d1_2", "d1_3"]


# ---------------------------------------------------------------------------
# 字段读取：能直接读 VascCase 的不做包装，只有病种域字段走 node_inputs
# ---------------------------------------------------------------------------
def _threshold_usable(state: GuidelineState) -> Verdict:
    """本节点语境下的「阈值是否可用」—— 判据本体在 ``common.threshold_usable``。

    保留薄包装而不是让调用点改用公共名：本文件有 6 处调用，全部读作「这里的
    阈值判不判得了」；判据本体与字段清单（``D1.SEX_THRESHOLD_SOURCE`` /
    ``D1.DIAM_METHOD`` / ``D1.MEASURE_VALID``）的说明放在公共层一处即可。
    """
    return threshold_usable(state)


def _special_morphology(state: GuidelineState) -> Verdict:
    """是否为需单独评估的特殊形态/病因（囊状 / 假性 / 炎症性 / 感染性）。

    规格 ``D1.1.R03`` 的判据原文：「…且无感染性/炎症性/假性/需单独评估的囊状病变。
    **特殊形态/病因转 D4/D5，不套用本条**」。所以这个判断是阈值分层的前置条件，
    不能只放在 ``R07`` 里——否则特殊形态病例会先被 ``R05`` 判成「达阈值」。
    """
    return or_(
        eq(token(state.case.cl.saccular), "明确", "疑似"),
        eq(token(state.case.cl.pseudo), "明确", "疑似"),
        eq(token(state.case.n.inflammatory_ctx), "临床确认", "影像高度提示"),
        eq(token(state.case.n.infection_ctx), "医师明确", "高度怀疑"),
    )


def _d11_special_targets(state: GuidelineState) -> tuple[str, ...]:
    """``D1.1.R07`` 的去处 —— 规格 G 列**逐项指定**，不是同一句话的几种说法。

    规格原文：「囊状/假性病变→D5.3；炎症性 AAA→D5.3；遗传背景→D1.2 个体化阈值；
    感染或瘘→D4.1。**多标签可以并存**。」

    ===================  ==========================================
    证据                  去处
    ===================  ==========================================
    感染（医师明确/高度怀疑）  D4.1 感染分诊
    囊状 / 假性 / 炎症性     D5.3 特殊形态专科评估
    遗传背景               D1.2 个体化阈值
    ===================  ==========================================

    排序按临床紧迫度，第一个即主出口：感染要清创与抗感染，先于形态学评估；
    遗传只影响阈值口径，最后。两病因并存时（炎症 + 感染）两个节点都去。

    两条**不走本条**的路，写在这里免得被当成漏判：

    · 纯遗传背景 → ``R10``（同 P1、声明在后、判据更专）；
    · 纯主动脉肠/腔静脉瘘 → ``G08.R04`` 会把它分类成 D4，入口直接是 ``D4.1``，
      到不了 ``D1.1``。
    """
    targets: list[str] = []
    if eq(token(state.case.n.infection_ctx), "医师明确", "高度怀疑") is True:
        targets.append("D4.1")
    if (
        eq(token(state.case.cl.saccular), "明确", "疑似") is True
        or eq(token(state.case.cl.pseudo), "明确", "疑似") is True
        or eq(token(state.case.n.inflammatory_ctx), "临床确认", "影像高度提示") is True
    ):
        targets.append("D5.3")
    if _genetic_clue(state) is True:
        targets.append("D1.2")
    return tuple(targets)


def _genetic_clue(state: GuidelineState) -> Verdict:
    """遗传评估线索：确诊/疑似遗传背景，或首次发病 <60 岁。

    规格 ``D1.1.R10``：「确诊/疑似遗传背景或首次 AAA 发病<60 岁、明确主动脉瘤
    家族史、已记录综合征体征之一→保留遗传评估线索」。家族史与综合征体征在
    ``D1.FAMILY_HISTORY`` / ``gen.*`` 里，缺失时按 UNKNOWN 处理，不当阴性。
    """
    ctx = eq(token(state.case.n.genetic_ctx), "确诊", "疑似遗传背景")
    if ctx is True:
        return True

    onset = state.case.gen.aaa_age_onset
    young = True if onset is not None and onset < 60 else None

    family = state.node_field("D1.FAMILY_HISTORY")
    if family is None or is_spec_uncertain(family):
        by_family = None
    else:
        by_family = family != "阴性"

    return or_(ctx, young, by_family)


def _sex_threshold(state: GuidelineState) -> tuple[str, float] | None:
    """本节点语境下的「性别 + 阈值」—— 判据本体在 ``common.sex_threshold``。"""
    return sex_threshold(state)


# ---------------------------------------------------------------------------
# 规则判据
# ---------------------------------------------------------------------------
def _d11_r01(state: GuidelineState) -> Verdict:
    """P0-A：带症状或破裂证据的病例不该落在 D1 —— 退回判定层重判。

    规格原文的出口是分叉的（症状→D2.1；破裂→G03 并转 D3.1）。本实现统一走
    ``escalate G03``，理由：D1.1 的正常入口是 ``G08.R01`` 打出的 ``D1`` 标签
    （无症状 AAA）。能走进来说明**标签与症状/破裂状态冲突**——这种冲突该由判定层
    重新分流，而不是由病种节点替它选 D2.1 还是 D3.1（那是 ``G08`` 的职责）。
    在节点里二选一，等于把分类判据复制了一份。
    """
    return or_(
        eq(token(state.case.cl.symptom_aaa), "医师确认相关", "医师怀疑相关"),
        eq(token(state.case.cl.aaa_rupture), "明确", "疑似", "无法评价"),
    )


def _d11_r02(state: GuidelineState) -> Verdict:
    """P1：年化增长 ≥10 mm/年 → 标记疑似快速增长，待复测复核。

    规格明写「确认前不得将其作为已确认修复指征」，故出口是「待复核」而不是
    「进入 D1.2」。方法不可比时 ``D1.GROWTH_RATE`` 应当缺失或为「不可计算」，
    本规则自然返回 UNKNOWN——不猜。
    """
    rate = state.node_field("D1.GROWTH_RATE")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return None
    return ge(float(rate), 10.0)


def _threshold_ready(state: GuidelineState, sex_flag: str) -> tuple[float, float] | None:
    """阈值分层的公共前置：返回 ``(直径, 阈值)``，任一前置不满足则 ``None``。

    **三值语义的关键点**：前置不满足时调用方必须返回 UNKNOWN，不能返回 False。

        「测量方法不可用」意味着**判不了**是否达阈值；若写成 False（未达），
        病例会被推进监测路径——而它可能早就该手术了。三值原语 ``and_`` 会在这里
        短路成 False，所以不能直接用它拼前置条件，必须先显式判「前置是否就绪」。
    """
    sex = _sex_threshold(state)
    diam = aaa_diameter(state)
    if sex is None or diam is None or sex[0] != sex_flag:
        return None
    if _threshold_usable(state) is not True:
        return None
    if _special_morphology(state) is not False:
        return None
    return diam, sex[1]


def _d11_r03(state: GuidelineState) -> Verdict:
    """P3：男性 <55 mm → 不因直径单独触发择期修复，进入监测。"""
    ready = _threshold_ready(state, "男")
    return None if ready is None else ready[0] < ready[1]


def _d11_r04(state: GuidelineState) -> Verdict:
    """P3：女性 <50 mm → 同上。"""
    ready = _threshold_ready(state, "女")
    return None if ready is None else ready[0] < ready[1]


def _d11_r05(state: GuidelineState) -> Verdict:
    """P1：达到性别阈值 → 进入 D1.2 评估获益与风险。

    规格原文：「…→进入修复获益/风险评估 D1.2，**不等于自动手术指令**」。
    所以出口是 ``next=D1.2`` 而不是任何形式的「建议手术」。
    """
    sex = _sex_threshold(state)
    if sex is None:
        return None
    ready = _threshold_ready(state, sex[0])
    return None if ready is None else ready[0] >= ready[1]


def _d11_r07(state: GuidelineState) -> Verdict:
    """P1：不适用普通 AAA 单一路径的特殊形态/病因。

    判据仍是「有没有需要单独评估的病变」——**去哪几个节点由
    :func:`_d11_special_targets` 按病因决定**，在 :func:`d1_1` 里落到出口上。
    """
    return _special_morphology(state)


def _d11_r10(state: GuidelineState) -> Verdict:
    """P1：遗传评估线索 → 保留线索并转 D1.2 个体化阈值。"""
    return _genetic_clue(state)


def _d11_r90(state: GuidelineState) -> Verdict:
    """P2：关键诊断/测量/症状/破裂状态缺失、不可评价或冲突。

    规格原文：「不得按阴性或无症状处理；按缺失项核查」。这里只判**决定性**的
    三项：诊断状态冲突、破裂状态冲突、直径不可用。症状「未评价」不阻断——它
    已被 ``R01`` 判过（不是「确认/怀疑相关」即不命中），再阻断一次没有新信息。
    """
    if aaa_diameter(state) is None:
        return True
    return or_(
        eq(token(state.case.cl.aaa_reported), "证据冲突"),
        eq(token(state.case.cl.aaa_rupture), "证据冲突"),
        _threshold_usable(state) is False,
    )


def _d11_r91(state: GuidelineState) -> Verdict:
    """P3 兜底出口：无症状 + 破裂可评价且未见证据 → 监测与风险管理。

    不含直径条件：``R03`` / ``R04`` 已按性别分档判过，能走到这里说明性别未知或
    阈值方法不匹配——此时仍应给出「监测」这一方向，只是不指定间隔（由节点层的
    缺口说明差什么）。
    """
    return and_(
        eq(token(state.case.cl.symptom_aaa), "无当前症状"),
        eq(token(state.case.cl.aaa_rupture), "未见相关证据"),
    )


_D11_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D1.1.R01",
        priority="P0-A",
        source_row="D1.1!16",
        statement="当前症状医师确认/怀疑与 AAA 相关，或破裂明确/疑似/无法评价 → 退回 G03 重新分流",
        judge=_d11_r01,
        exit_kind="escalate",
        exit_value="G03",
        next_nodes=("D2.1", "G03", "D3.1"),
        link=Link.EITHER,
    ),
    RuleSlot(
        rule_id="D1.1.R02",
        priority="P1",
        source_row="D1.1!17",
        statement="年化增长 ≥10 mm/年 → 标记疑似快速增长，复测确认前不作为修复指征",
        judge=_d11_r02,
        exit_kind="terminal",
        exit_value="疑似快速增长（年化 ≥10 mm），待复测复核",
        next_nodes=("D1.1", "D1.2"),
        link=Link.FOLLOW_UP,
    ),
    RuleSlot(
        rule_id="D1.1.R05",
        priority="P1",
        source_row="D1.1!20",
        statement="男性 ≥55 mm / 女性 ≥50 mm → 进入 D1.2 评估获益与风险（不等于手术指令）",
        judge=_d11_r05,
        exit_kind="next",
        exit_value="D1.2",
    ),
    RuleSlot(
        rule_id="D1.1.R07",
        priority="P1",
        source_row="D1.1!22",
        statement="囊状/假性/炎症性/感染性病变 → 按病因转 D5.3 / D4.1 / D1.2 专项评估，"
        "不套用普通梭形阈值",
        judge=_d11_r07,
        exit_kind="next",
        # 默认主出口取 D5.3（形态/炎症是本条最常见的触发）；实际去向在 ``d1_1``
        # 里按病因收窄——规格是**分派**，不是「三个都去」。
        exit_value="D5.3",
        next_nodes=("D1.2", "D4.1", "D5.3"),
        # 「多标签可以并存」：炎症 + 感染同时成立时 D5.3 与 D4.1 都要执行。
        link=Link.PARALLEL,
    ),
    RuleSlot(
        rule_id="D1.1.R10",
        priority="P1",
        source_row="D1.1!28",
        statement="确诊/疑似遗传背景或首次发病 <60 岁 → 保留遗传评估线索，转 D1.2 个体化阈值",
        judge=_d11_r10,
        exit_kind="next",
        exit_value="D1.2",
    ),
    RuleSlot(
        rule_id="D1.1.R90",
        priority="P2",
        source_row="D1.1!15",
        statement="诊断/破裂状态冲突或直径不可用于阈值判断 → 不得按无症状处理，转人工复核",
        judge=_d11_r90,
        exit_kind="terminal",
        exit_value="人工复核（关键测量或诊断状态不可用）",
    ),
    RuleSlot(
        rule_id="D1.1.R03",
        priority="P3",
        source_row="D1.1!18",
        statement="男性最大外径 <55 mm → 不因直径单独触发择期修复，进入监测与风险管理",
        judge=_d11_r03,
        exit_kind="terminal",
        exit_value="监测／风险管理（未达修复阈值）",
    ),
    RuleSlot(
        rule_id="D1.1.R04",
        priority="P3",
        source_row="D1.1!19",
        statement="女性最大外径 <50 mm → 不因直径单独触发择期修复，进入监测与风险管理",
        judge=_d11_r04,
        exit_kind="terminal",
        exit_value="监测／风险管理（未达修复阈值）",
    ),
    RuleSlot(
        rule_id="D1.1.R91",
        priority="P3",
        source_row="D1.1!14",
        statement="无症状且破裂可评价且未见证据 → 监测与风险管理；达阈值转 D1.2，特殊形态转对应节点",
        judge=_d11_r91,
        exit_kind="terminal",
        exit_value="监测／风险管理（按性别与直径安排随访）",
        next_nodes=("D1.2", "D2.1", "D4.1", "D5.3"),
    ),
)


def _surveillance_interval(state: GuidelineState) -> str | None:
    """按性别 + 直径给超声随访间隔（``D1.1.R06`` 原文分档）。

    ==========  ==========================  ==========================
    直径(mm)     男性                         女性
    ==========  ==========================  ==========================
    30–39       每 3 年                      每 3 年
    40–49       每年                          40–44 每年
    ≥50         每 6 个月                     ≥45 每 6 个月
    ==========  ==========================  ==========================

    ``<30 mm`` 返回 ``None``：那不是 AAA，套用监测计划没有意义。
    """
    sex = token(state.case.p.sex)
    diam = aaa_diameter(state)
    if diam is None or sex not in ("男", "女") or diam < 30:
        return None
    if sex == "男":
        if diam < 40:
            return "每 3 年 1 次"
        if diam < 50:
            return "每年 1 次"
        return "每 6 个月 1 次"
    if diam < 40:
        return "每 3 年 1 次"
    if diam < 45:
        return "每年 1 次"
    return "每 6 个月 1 次"


def _d11_gaps(state: GuidelineState) -> tuple[str, ...]:
    """本节点缺什么 —— 只列**会影响结论**的项。

    为什么单独列 ``D1.DIAM_METHOD``：规格 ``D1.SEX_THRESHOLD_SOURCE`` 明写
    「仅有 CTA 外径不可未经方法核对直接套用超声阈值」。方法缺失时阈值判断整体
    返回 UNKNOWN，不把它说清楚，读者只会看到「人工复核」而不知道补什么。
    """
    gaps: list[str] = []
    usable = _threshold_usable(state)
    if usable is None:
        gaps.append("测量方法未核对（D1.DIAM_METHOD）：无法套用性别阈值")
    elif usable is False:
        gaps.append("测量方法为 CTA 外径，需专科确认阈值转换（不可直接套用超声阈值）")

    valid = state.node_field("D1.MEASURE_VALID")
    gap = gap_if_missing("D1.MEASURE_VALID（测量有效性：可比/不可比/需复测）", valid)
    if gap:
        gaps.append(gap)

    rate = state.node_field("D1.GROWTH_RATE")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        gaps.append("年化增长速率未知（缺既往可比直径或测量日期）")
    return tuple(gaps)


def d1_1(state: GuidelineState) -> NodeOutcome:
    """D1.1：诊断与直径分层 —— 达阈值转 D1.2，未达转监测。

    两段式：``evaluate_node`` 做分流（P0→P3 逐条求值），随后在节点层补两样
    ``RuleSlot`` 表达不了的东西——缺口清单，以及随访间隔。

    为什么 ``D1.1.R06`` 不进 ``RuleSlot``：它是**展示性规则**（给随访间隔），
    不参与分流。若进槽，它会以 P1 优先级抢先命中所有未达阈值的病例，使
    ``R03``/``R04``（P3 的监测出口）永远不执行——分档结论就丢了。
    """
    outcome = evaluate_node("D1.1", state, _D11_SLOTS)

    # R07 的规格值是**按病因分派**（D1.2 / D4.1 / D5.3），不是「三个都去」。
    # 槽位只能写一个静态值域，实际去哪几个取决于本病例检出了哪些病因，
    # 所以在节点层收窄；多病因并存时按并行展开（``engine._walk`` 依 link 处理）。
    if outcome.verdicts.get("D1.1.R07") is True:
        targets = _d11_special_targets(state)
        if targets:
            outcome = replace(
                outcome,
                next_node=targets[0],
                next_nodes=targets,
                link=Link.PARALLEL if len(targets) > 1 else Link.EITHER,
            )

    gaps = _d11_gaps(state)
    if gaps:
        outcome = replace(outcome, gaps=gaps)

    interval = _surveillance_interval(state)
    if interval is None or not outcome.terminal or "监测" not in outcome.terminal:
        return outcome

    entry = GuidelineEntry(
        node_id="D1.1",
        rule_id="D1.1.R06",
        source_row="D1.1!21",
        statement=f"超声监测间隔：{interval}（按性别与直径分档，结合寿命预期与意愿个体化）",
        verdict=True,
    )
    return replace(
        outcome,
        entries=(*outcome.entries, entry),
        terminal=f"{outcome.terminal}：{interval}复查",
    )


# ---------------------------------------------------------------------------
# D1.2 患者获益与手术风险
# ---------------------------------------------------------------------------
#
# 本节点 11 条规则里**没有一条**读可由程序计算的生理量：读的全是「医生怎么评估
# 这个人」的结论。系统不生成这些结论，只做两件事——**缺了就点名**、**有了就按
# 它分流**。所以判据一律写成「字段值 == 某个已明确的医生结论」，而不是「病人
# 能不能手术」；后者在规格里被反复交给医生（``D1.OP_FITNESS`` 的 G 列「不由
# 单一指标自动否决」、``D1.PROGNOSIS`` 的 G 列「只能读取授权医生综合判断」）。
_D12_ASSESSMENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("D1.LIFE_EXPECTANCY", "预期寿命：较长/有限/不确定/未评价"),
    ("D1.OP_FITNESS", "择期手术适宜性：可接受/高风险/当前不适合/未评价"),
    ("D1.PREFERENCE", "患者意愿：倾向监测/倾向修复/拒绝某方案/尚未讨论"),
    ("D1.PROGNOSIS", "长期预后：合理预期/有限（约2–3年或更短）/不确定/未评估"),
    ("D1.SURVEILLANCE_BENEFIT", "监测可获益性：有潜在修复获益/无明显获益/不确定/未评估"),
)

#: 优化清单字段 —— 缺它们**不阻断**分流，只影响「怎么优化」这一层建议。
_D12_OPTIMIZATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("D1.COMORBIDITY", "影响手术风险的合并症及控制状态"),
    ("D1.FRAILTY", "衰弱评估结果（量表名+分值+日期）"),
    ("D1.FRAILTY_TOOL", "衰弱量表/日常活动能力"),
    ("D1.CARDIOPULM_RISK", "心肺风险与功能储备"),
    ("D1.RENAL_RISK", "肾功能与造影剂相关风险"),
    ("D1.RISK_OPTIMIZATION", "可优化的围术期风险因素"),
)

#: ``D1.PROGNOSIS`` 表示「寿命有限」的两种写法（规格 F 列给的是前一个）。
_PROGNOSIS_LIMITED: tuple[str, ...] = ("有限", "有限（约2–3年或更短）")

#: ``R91`` 的出口 —— 它是一个**分叉**（进 D1.3 或监测/优化），规则只能写一句中性话。
_D12_R91_EXIT: str = "医生确认：进入 D1.3 术式候选 或 监测／优化／非手术出口"

#: 患者已明确选监测时 ``R91`` 的落点 —— 该分支被关闭，措辞必须跟着改。
_D12_R91_SURVEILLANCE: str = "医生确认：监测方案（患者倾向监测，D1.PREFERENCE）"


def _d12_value(state: GuidelineState, spec_id: str) -> object:
    """读一个 ``D1.*`` 评估字段；「未评估」「尚未讨论」等折成 ``None``。

    折成 ``None`` 而非保留原串，是关键的一步：``eq("未评估", "可接受")`` 会得到
    ``False``（被当成「确定不可接受」），而规格明写「未评估不等于……」——
    只有折成 ``None``，三值原语才会给出 UNKNOWN。
    """
    return spec_token(state.node_field(spec_id))


def _d12_unset(state: GuidelineState, spec_id: str) -> bool:
    """字段「还没有结论」—— 缺失或取值不确定。"""
    return _d12_value(state, spec_id) is None


def _life_unknown(state: GuidelineState) -> bool:
    """寿命维度**完全没有**结论 —— 两个字段任一有值就不算缺。

    为什么把 ``D1.LIFE_EXPECTANCY`` 与 ``D1.PROGNOSIS`` 合成一个维度：
    它们是同一件事的两种粒度（患者获益维度的粗枚举 / 长期预后的细化枚举），
    规格让 ``R01`` 读前者、``R07`` 读后者。若把它们当两件独立的必填项，
    「寿命未评价但长期预后明确有限」的病例会被 ``R90`` 拦在人工复核，
    而 ``R07`` 恰恰能对这个状态给出「不推荐常规择期修复」的明确结论。
    """
    return _d12_unset(state, "D1.LIFE_EXPECTANCY") and _d12_unset(state, "D1.PROGNOSIS")


def _at_threshold(state: GuidelineState) -> Verdict:
    """``D1.2`` 语境下的「是否达到性别阈值」—— 判据本体在 ``common.at_repair_threshold``。

    与 ``D1.1`` 的 :func:`_threshold_ready` 分开：那个还带「特殊形态已排除」
    前置（``D1.1`` 的分流需要它），``D1.2`` 不需要——能走到 ``D1.2`` 说明形态
    已处理过，再判一次会把「形态字段未填」的病例挡在修复评估之外。
    """
    return at_repair_threshold(state)


def _d12_r03(state: GuidelineState) -> Verdict:
    """P1：确诊或疑似遗传性主动脉病 → 遗传评估 + 高度专科中心 MDT 后进 D1.3。

    本规则只判「是不是遗传背景」，不判「具体阈值该收到多少」——规格 R03 说
    「确诊遗传病因→**个体化**修复直径阈值」，那个个体化值必须由遗传/主动脉
    MDT 给出，系统给不出也不该猜。
    """
    return eq(token(state.case.n.genetic_ctx), "确诊", "疑似遗传背景")


def _d12_r04(state: GuidelineState) -> Verdict:
    """P1：达阈值 + 医生认为风险可接受 + 患者参与共同决策 → 进 D1.3。

    四个条件对应规格 R04 的四个字段，逐条都能落到一个可读的申报值：

    · ``CL.AAA_DIAM`` 达性别阈值（且测量方法已核对，见 :func:`_at_threshold`）
    · ``D1.OP_FITNESS == 可接受`` ——「医生认为获益可能超过风险」的落点
    · ``D1.PREFERENCE == 倾向修复`` ——「患者参与共同决策」的落点
    · ``D1.LIFE_EXPECTANCY == 较长`` —— 寿命有限时不该走修复路径（``R01`` 另有出口）

    **不重复判** ``N.GENETIC_CTX``：``R03`` 在同一优先级更靠前，遗传病例已被它
    带走；在这里再判一次，只会让「遗传背景未评价」的普通病例也判不出来。
    """
    return and_(
        _at_threshold(state),
        eq(_d12_value(state, "D1.OP_FITNESS"), "可接受"),
        eq(_d12_value(state, "D1.PREFERENCE"), "倾向修复"),
        eq(_d12_value(state, "D1.LIFE_EXPECTANCY"), "较长"),
    )


def _d12_r05(state: GuidelineState) -> Verdict:
    """P2：关键风险或患者偏好未评价/冲突 → 不自动输出「适合/不适合手术」。

    窄口径：只判三件最关键的（手术适宜性 / 寿命 / 意愿）。寿命用
    :func:`_life_unknown` 合成判——否则「寿命未评价但预后明确有限」的病例会被
    这里拦下，而 ``R07`` 本可对那个状态给出明确结论。更宽的清单交给 ``R90``。
    """
    return or_(
        _d12_unset(state, "D1.OP_FITNESS"),
        True if _life_unknown(state) else None,
        _d12_unset(state, "D1.PREFERENCE"),
    )


def _d12_r90(state: GuidelineState) -> Verdict:
    """P2：关键风险/预期寿命/患者意愿/监测获益未评价或冲突 → 阻断常规放行。

    判据取规格 J 列点名的**四项**：「关键风险、预期寿命、患者意愿或监测获益」。
    F 列还列了 ``D1.FRAILTY`` 与 ``D1.COMORBIDITY``——它们是「风险」的构成成分，
    不是独立的分流条件：把「衰弱未评估」当作阻断，会让每个还没做老年评估的病人
    都停在人工复核，而这四条 P3 出口（寿命有限 / 当前不适合 / 停止监测 / 优化）
    恰恰正是「风险已明确但不宜修复」的那几类病例。故这两项只进缺口清单
    （``_D12_OPTIMIZATION_FIELDS``），不参与阻断。

    与 ``R05`` 的重叠（前三项）保留：两条规则的出口动作不同——``R05`` 要「建立
    待完成评估」，``R90`` 要「核查后回本节点」，合并会丢掉其中一个留痕。
    """
    return any(
        (
            _d12_unset(state, "D1.OP_FITNESS"),
            _life_unknown(state),
            _d12_unset(state, "D1.PREFERENCE"),
            _d12_unset(state, "D1.SURVEILLANCE_BENEFIT"),
        )
    )


def _d12_r01(state: GuidelineState) -> Verdict:
    """P3：医生评估预期寿命有限 → 不推荐常规择期修复（不以年龄单项自动终止）。

    判据只看 ``D1.LIFE_EXPECTANCY``，**不看** ``P.AGE``：规格 R01 的字段清单里
    有 ``P.AGE``，但 G 列明写「不以年龄单项自动终止」——年龄只作为记录项，
    不构成判据。把它写进判据就是规格自己禁止的那类误判。
    """
    return eq(_d12_value(state, "D1.LIFE_EXPECTANCY"), "有限")


def _d12_r07(state: GuidelineState) -> Verdict:
    """P3：长期预后有限 → 同上，但依据是另一个字段（``D1.PROGNOSIS``）。

    与 ``R01`` 分开：两者是**同一结论的两个来源**（患者获益维度的粗枚举 vs
    长期预后的细化枚举），任一成立都应给出非手术出口。写成一条会逼着二选一，
    而规格把两个字段都建了。
    """
    return eq(_d12_value(state, "D1.PROGNOSIS"), *_PROGNOSIS_LIMITED)


def _d12_r08(state: GuidelineState) -> Verdict:
    """P3：监测已无明显获益 + 修复不适宜/寿命有限 → **考虑**停止监测。

    「考虑」不是「停止」：规格 G 列明写「只有授权医生确认……才输出『考虑停止』；
    不得自动停止」。所以出口文案留「须授权医生确认」，系统不替医生停监测。
    """
    return and_(
        eq(_d12_value(state, "D1.SURVEILLANCE_BENEFIT"), "无明显获益"),
        or_(
            eq(_d12_value(state, "D1.OP_FITNESS"), "当前不适合"),
            eq(_d12_value(state, "D1.LIFE_EXPECTANCY"), "有限"),
            eq(_d12_value(state, "D1.PROGNOSIS"), *_PROGNOSIS_LIMITED),
        ),
    )


def _d12_r02(state: GuidelineState) -> Verdict:
    """P3：当前手术风险不适合 → 继续监测 + 转专科优化后复评。

    不判「是否终止监测」——那是 ``R08`` 的事，它比本规则更具体，排在本规则之前。
    """
    return eq(_d12_value(state, "D1.OP_FITNESS"), "当前不适合")


def _d12_r09(state: GuidelineState) -> Verdict:
    """P3：寿命允许且风险有讨论空间 → 进 D1.3 做 EVAR/OSR 并列比较。

    这是 ``R04`` 的降级版：``R04`` 要求「完全符合」（可接受 + 倾向修复），
    本规则只要「风险可讨论 + 寿命较长 + 患者没选监测」就放行到术式比较。
    规格 R09 的要点是「长期预期寿命 >10–15 年且适合手术者可讨论 OSR 耐久性
    优势，**不能单凭年龄自动选择**」——所以这里也不出现年龄判据。
    """
    return and_(
        eq(_d12_value(state, "D1.LIFE_EXPECTANCY"), "较长"),
        eq(_d12_value(state, "D1.OP_FITNESS"), "可接受", "高风险"),
        not_(eq(_d12_value(state, "D1.PREFERENCE"), "倾向监测")),
    )


def _d12_r06(state: GuidelineState) -> Verdict:
    """P3：存在可优化风险因素 → 优化清单 + 复评时间（不因单个指标否决修复）。

    「存在可优化因素」这个事实只能由医生记录：规格 ``D1.RISK_OPTIMIZATION`` 的
    G 列要求「列出具体因素、当前状态、优化责任人和是否会影响手术时机」——
    系统不猜有没有，只看医生填了没有。
    """
    return recorded(state, "D1.RISK_OPTIMIZATION")


def _d12_r91(state: GuidelineState) -> Verdict:
    """P3 兜底出口：评估已完成但未指向修复 → 方案由医生确认。

    恒 ``True``：能走到这里说明 ``R90`` 没拦（关键字段都有结论），此时规格 G 列
    要求「若预期获益不足/当前不适合→医生确认监测、优化后复评或停止监测」。
    系统不判「获益够不够」，只把已走过的分支整理成交给医生。
    """
    return True


_D12_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D1.2.R03",
        priority="P1",
        source_row="D1.2!12",
        statement="确诊/疑似遗传性主动脉病 → 遗传评估与高度专科中心 MDT，个体化阈值后进 D1.3",
        judge=_d12_r03,
        exit_kind="next",
        exit_value="D1.3",
    ),
    RuleSlot(
        rule_id="D1.2.R04",
        priority="P1",
        source_row="D1.2!13",
        statement="达修复阈值且医生认为获益可能大于风险、患者参与共同决策 → 进入术式候选比较 D1.3",
        judge=_d12_r04,
        exit_kind="next",
        exit_value="D1.3",
    ),
    RuleSlot(
        rule_id="D1.2.R05",
        priority="P2",
        source_row="D1.2!14",
        statement="关键风险或患者偏好未评价/冲突 → 不自动输出适合/不适合手术，建立待完成评估",
        judge=_d12_r05,
        exit_kind="terminal",
        exit_value="人工复核（待完成风险评估与共同决策）",
    ),
    RuleSlot(
        rule_id="D1.2.R90",
        priority="P2",
        source_row="D1.2!9",
        statement="关键风险/预期寿命/患者意愿/监测获益未评价或冲突 → 不自动批准修复或停止监测",
        judge=_d12_r90,
        exit_kind="terminal",
        exit_value="人工复核（补齐关键评估后回 D1.2）",
    ),
    RuleSlot(
        rule_id="D1.2.R01",
        priority="P3",
        source_row="D1.2!10",
        statement="医生评估预期寿命有限 → 不推荐常规择期修复；记录理由与替代管理（不以年龄单项终止）",
        judge=_d12_r01,
        exit_kind="terminal",
        exit_value="监测／最佳药物管理／医生确认的非手术出口（预期寿命有限）",
    ),
    RuleSlot(
        rule_id="D1.2.R07",
        priority="P3",
        source_row="D1.2!22",
        statement="长期预后有限（约 2–3 年或更短）→ 不推荐常规择期 AAA 修复，记录监测与患者目标",
        judge=_d12_r07,
        exit_kind="terminal",
        exit_value="监测／最佳药物管理／医生确认的非手术出口（长期预后有限）",
    ),
    RuleSlot(
        rule_id="D1.2.R08",
        priority="P3",
        source_row="D1.2!23",
        statement="监测不再可能改变治疗且修复不适宜/目标不支持 → 考虑停止监测（须授权医生确认）",
        judge=_d12_r08,
        exit_kind="terminal",
        exit_value="考虑停止监测（须授权医生确认并记录原因）",
    ),
    RuleSlot(
        rule_id="D1.2.R02",
        priority="P3",
        source_row="D1.2!11",
        statement="当前不适合修复 → 继续监测、转专科优化后复评；是否终止监测另行判断",
        judge=_d12_r02,
        exit_kind="terminal",
        exit_value="优化与复评（专科优化后回 D1.2）",
    ),
    RuleSlot(
        rule_id="D1.2.R09",
        priority="P3",
        source_row="D1.2!24",
        statement="寿命较长且风险可讨论 → 记录比较维度后进入 D1.3（OSR 耐久性优势须并列讨论）",
        judge=_d12_r09,
        exit_kind="next",
        exit_value="D1.3",
    ),
    RuleSlot(
        rule_id="D1.2.R06",
        priority="P3",
        source_row="D1.2!21",
        statement="存在可优化风险因素且不需立即修复 → 形成待医生确认的优化清单与复评时间",
        judge=_d12_r06,
        exit_kind="terminal",
        exit_value="待医生确认的优化清单与复评时间",
        next_nodes=("D1.2", "D1.3"),
    ),
    RuleSlot(
        rule_id="D1.2.R91",
        priority="P3",
        source_row="D1.2!8",
        statement="评估完成且未触发更高优先级规则 → 修复获益合理且患者接受则 D1.3，否则医生确认监测/优化/非手术",
        judge=_d12_r91,
        exit_kind="terminal",
        exit_value=_D12_R91_EXIT,
    ),
)


def _d12_gaps(state: GuidelineState) -> tuple[str, ...]:
    """本节点缺什么 —— 关键评估与优化清单分开列。

    为什么分两段：前 5 项缺任何一项都会让分流判不出来（``R05``/``R90`` 的阻断
    口径），后 6 项只影响「怎么优化」。混成一段会让读者分不清哪些是「必须补」。

    为什么遗传 MDT 也是一条缺口：``R03`` 的出口是 ``next=D1.3``，流程会继续往下
    走，``terminal`` 随即被 ``D1.3`` 覆盖——若不在这里留一条，报告里就再也看不到
    「本例需要遗传/主动脉 MDT 定个体化阈值」这个前置要求。
    """
    gaps = list(missing_fields(state, _D12_ASSESSMENT_FIELDS))
    gaps.extend(missing_fields(state, _D12_OPTIMIZATION_FIELDS))
    if _d12_r03(state) is True:
        gaps.append("遗传评估与主动脉 MDT（D1.2.R03）：个体化修复阈值须由 MDT 给出")
    return tuple(gaps)


def d1_2(state: GuidelineState) -> NodeOutcome:
    """D1.2：按医生已给出的评估结论分流，缺的项逐条点名。

    本节点**不产出术式候选**——那是 ``D1.3`` 的事。它只回答「能不能往下走」。
    """
    outcome = evaluate_node("D1.2", state, _D12_SLOTS)
    gaps = _d12_gaps(state)
    if gaps:
        outcome = replace(outcome, gaps=gaps)

    # ``R91`` 的出口是分叉，规则里只能写中性文案。患者已明确选监测时，落成
    # 具体那一支——否则报告会写成「或进 D1.3」，而那条路其实已经关了。
    if outcome.terminal == _D12_R91_EXIT and (
        eq(_d12_value(state, "D1.PREFERENCE"), "倾向监测") is True
    ):
        outcome = replace(outcome, terminal=_D12_R91_SURVEILLANCE)
    return outcome


# ---------------------------------------------------------------------------
# D1.3 术式候选（EVAR / OSR 两列）
# ---------------------------------------------------------------------------
#
# 与 D3.2 同构，差别在场景：急诊「不等资料也要给方向」，择期「等得起，缺项
# 只落缺口、不阻断方向」。所以本节点恒输出两列候选，缺什么写在理由里。
_D13_ANATOMY_FIELDS: tuple[tuple[str, str], ...] = (
    ("D1.NECK_THROMBUS_CALC", "瘤颈血栓/钙化/锥形形态（环周比例、厚度、位置）"),
    ("D1.BRANCH_VARIANT", "副肾动脉及重要分支变异（数量/开口/直径/供血区域）"),
)

_D13_ASSESS_FIELDS: tuple[tuple[str, str], ...] = (
    ("D1.LONG_TERM", "长期耐久性/再干预/随访负担/恢复负担比较"),
    ("D1.REPAIR_MOD_COMPARISON", "EVAR/OSR 个体化比较结果（可行性/风险/耐久性/偏好）"),
)


def _branch_involved(state: GuidelineState) -> Verdict:
    """本节点语境下的「分支受累」—— 判据本体在 ``common.branch_involved``。

    这里保留一个薄别名而不是让调用点改用公共名：``D1.3`` 的六处引用都读作
    「这个节点的分支条件」，「标准 EVAR 是否还标准」是同一个问题。别名让
    「用的是什么判据」与「用在哪」分开可读。
    """
    return branch_involved(state)


def _standard_evar_ready(state: GuidelineState) -> Verdict:
    """标准 EVAR 的全部必需条件（``D1.3.R05`` 的口径，比 ``R01`` 多远端密封区）。

    规格 R05：「仅当本次 CTA 可评价、近**远端**密封区/入路所有必需参数均符合
    指定器械地区有效 IFU，且临床团队确认，才能标记『标准 EVAR 候选』」。
    远端密封区（``D1.DISTAL_SEAL``）因此是它和 ``R01`` 的唯一分野。
    """
    return and_(
        cta_evaluable(state),
        anatomy_available(state),
        not_(access_blocked(state)),
        not_(_branch_involved(state)),
        eq(token(state.case.n.seal_ifu), "符合"),
        recorded(state, "D1.DISTAL_SEAL"),
    )


def _d13_r01(state: GuidelineState) -> Verdict:
    """P3：CTA 可评价、着陆区/入路满足 IFU、患者和医生接受 EVAR → 列为 EVAR 候选。

    分支受累是否决项：近端瘤颈累及肾/SMA/腹腔干时，标准 EVAR 的着陆区就不在
    肾下，``R03`` 已把这类病例送去 G06 做复杂规划——此时若本规则仍判成立，
    报告会同时出现「EVAR 候选」与「转复杂模式」两个互斥结论。
    """
    return and_(
        cta_evaluable(state),
        anatomy_available(state),
        not_(access_blocked(state)),
        not_(_branch_involved(state)),
        eq(token(state.case.n.seal_ifu), "符合"),
        not_(eq(_d12_value(state, "D1.PREFERENCE"), "倾向监测")),
    )


def _d13_r05(state: GuidelineState) -> Verdict:
    """P3：全部解剖参数与 IFU 已核验 → 标记「**标准** EVAR 候选」（含远端密封区）。"""
    return _standard_evar_ready(state)


def _d13_r02(state: GuidelineState) -> Verdict:
    """P1：总体风险可接受、长期获益与偏好支持 → 列为 OSR 候选（须 MDT 确认）。

    判据只取三个有申报值的条件。规格还要求「解剖上可安全开放夹闭/重建」，
    而 ``VascCase`` 的 27 个解剖字段**全是腔内解剖**（瘤颈/入路/髂动脉），
    没有一个描述开放手术关心的夹闭位置与肾内脏灌注——没有判据就不下结论，
    这正是该规则「须由术者/MDT 确认」的落点，不是实现缺口。
    """
    return and_(
        eq(_d12_value(state, "D1.OP_FITNESS"), "可接受"),
        eq(_d12_value(state, "D1.LIFE_EXPECTANCY"), "较长"),
        not_(eq(_d12_value(state, "D1.PREFERENCE"), "倾向监测")),
    )


def _d13_r09(state: GuidelineState) -> Verdict:
    """P3：两列候选必须分别列出 —— 这是**展示义务**，恒成立。

    规格 G 列：「必须分别输出 EVAR 与 OSR 候选状态、排除/限制原因和随访要求；
    不得因某一术式不适配而自动把另一术式设为首选」。本节点用
    :func:`_evar_candidate` / :func:`_osr_candidate` 履行它，本条只留痕。
    """
    return True


_D13_DISPLAY_RULES: tuple[DisplayRule, ...] = (
    DisplayRule(
        rule_id="D1.3.R01",
        source_row="D1.3!9",
        statement="CTA 解剖可评价、目标着陆区/入路满足指定器械 IFU、患者与医生接受 → EVAR 候选",
        judge=_d13_r01,
    ),
    DisplayRule(
        rule_id="D1.3.R02",
        source_row="D1.3!10",
        statement="总体风险/麻醉评估可接受且长期获益、耐久性、偏好支持 → OSR 候选（须术者/MDT 确认）",
        judge=_d13_r02,
    ),
    DisplayRule(
        rule_id="D1.3.R05",
        source_row="D1.3!19",
        statement="CTA 可评价 + 近远端密封区/入路全部参数符合有效 IFU → 标记「标准 EVAR 候选」",
        judge=_d13_r05,
    ),
    DisplayRule(
        rule_id="D1.3.R09",
        source_row="D1.3!23",
        statement="分别输出 EVAR 与 OSR 候选状态、排除/限制原因与随访要求，不互设首选",
        judge=_d13_r09,
    ),
)


def _d13_r90(state: GuidelineState) -> Verdict:
    """P2：关键解剖、IFU 版本或入路资料缺失 → 只输出草案，不批准具体术式。

    只判**完全没记录**的项。``seal_ifu = 未核验/未选择型号`` **不**算缺失：本系统
    按规格明令不在本节点选器械型号（``D1.3.R03``/``R06``），「未核验」是结构性
    常态而非缺陷，据它阻断会让所有病例都停在草案。

    端到端通常**不可达**：关键资料缺失时，判定层 ``G04.R13`` 已先拦到
    ``RouteKind.VERIFY``，``class_output`` 为空，工具A 根本不会启动。本规则防的是
    直接调用（单元测试、未来的调用方）与判定层口径放宽——不是死代码。
    """
    return any(
        (
            anatomy_available(state) is False,
            cta_evaluable(state) is False,
            token(state.case.n.seal_ifu) is None,
        )
    )


def _d13_r08(state: GuidelineState) -> Verdict:
    """P2：EVAR 明确不可行且手术风险当前不可接受 → 只输出「待专家评估/转诊」。

    「OSR 也不适配」这一半判不了（无开放解剖输入），所以本规则只在**两个方向
    都不通**时命中：腔内走不通 + 开刀现在不能做。此时规格要求「不自动选择某术式，
    也不宣称无手术价值」——出口是 MDT/转诊，不是「不可手术」。
    """
    evar_out = or_(
        access_blocked(state),
        eq(token(state.case.n.seal_ifu), "不符合"),
        anatomy_available(state) is False,
    )
    return and_(evar_out, eq(_d12_value(state, "D1.OP_FITNESS"), "当前不适合"))


def _d13_r03(state: GuidelineState) -> Verdict:
    """P3：复杂近端解剖/分支受累 → 转复杂 AAA 规划（G06），本节点不选型号。"""
    return _branch_involved(state)


def _d13_r06(state: GuidelineState) -> Verdict:
    """P3：标准 EVAR 不符合 IFU 但有复杂修复可能 → 转复杂 AAA 团队（G06）。

    与 ``D3.2.R02`` 判同一个字段，但出口不同：急诊判「评估 OSR」（等不起 FEVAR），
    择期判「转复杂规划」（等得起，FEVAR/BEVAR 与复杂开放修复都要比）。
    """
    return eq(token(state.case.n.seal_ifu), "不符合")


def _d13_r04(state: GuidelineState) -> Verdict:
    """P3：全部方案风险过高或患者拒绝 → 医生确认的非手术出口。

    「风险过高」用 ``OP_FITNESS = 当前不适合`` 承载；「患者拒绝」用
    ``PREFERENCE = 倾向监测`` 承载。**不得**由本规则宣布不可手术——规格 G 列：
    「不得由规则引擎单独宣布不可手术」。
    """
    return or_(
        eq(_d12_value(state, "D1.OP_FITNESS"), "当前不适合"),
        eq(_d12_value(state, "D1.PREFERENCE"), "倾向监测"),
    )


def _d13_r91(state: GuidelineState) -> Verdict:
    """P3 出口：两列候选已列出；复杂解剖转 G06，无可靠方案转 MDT/转诊。"""
    return True


_D13_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D1.3.R90",
        priority="P2",
        source_row="D1.3!8",
        statement="关键解剖/IFU 版本/入路/患者风险资料缺失或冲突 → 仅输出草案，不批准具体术式",
        judge=_d13_r90,
        exit_kind="terminal",
        exit_value="人工复核（草案：关键解剖或 IFU 资料缺失）",
    ),
    RuleSlot(
        rule_id="D1.3.R08",
        priority="P2",
        source_row="D1.3!21",
        statement="EVAR 与 OSR 均有重大限制或关键 IFU 未核验 → 待专家评估/转诊，不宣称无手术价值",
        judge=_d13_r08,
        exit_kind="terminal",
        exit_value="MDT／转诊／补充影像（不自动选术式）",
    ),
    RuleSlot(
        rule_id="D1.3.R03",
        priority="P3",
        source_row="D1.3!11",
        statement="复杂近端解剖/分支受累 → 进入复杂 AAA 规划，比较复杂开放修复与 FEVAR/BEVAR",
        judge=_d13_r03,
        exit_kind="escalate",
        exit_value="G06",
        next_nodes=("G06",),
        link=Link.EITHER,
    ),
    RuleSlot(
        rule_id="D1.3.R06",
        priority="P3",
        source_row="D1.3!20",
        statement="标准 EVAR 不符合 IFU 但存在复杂修复可能 → 转复杂 AAA 团队（不在本节点选型号）",
        judge=_d13_r06,
        exit_kind="escalate",
        exit_value="G06",
        next_nodes=("G06",),
        link=Link.EITHER,
    ),
    RuleSlot(
        rule_id="D1.3.R04",
        priority="P3",
        source_row="D1.3!12",
        statement="所有修复方案风险过高或患者拒绝 → 由授权医生确认监测/最佳药物管理/姑息方案",
        judge=_d13_r04,
        exit_kind="terminal",
        exit_value="医生确认的非手术出口（监测／最佳药物管理／姑息）",
    ),
    RuleSlot(
        rule_id="D1.3.R91",
        priority="P3",
        source_row="D1.3!7",
        statement="关键解剖可评价 → 分别列出 EVAR/OSR 可行候选、条件性候选与排除原因；无可靠方案转 MDT",
        judge=_d13_r91,
        exit_kind="terminal",
        exit_value="EVAR/OSR 候选已列出（复杂解剖→G06，无可靠方案→MDT/转诊）",
    ),
)


def _evar_candidate(state: GuidelineState) -> Candidate:
    """EVAR 这一列 —— 按「硬排除 → 未评估 → 标准候选 → 条件性」落档。

    顺序不是随手排的：硬排除先于数据阻断。入路双侧闭塞或 IFU 明确不符时，
    资料再全也是排除；反之瘤颈参数全缺时不叫排除，叫「未评估」（``R90`` 口径）。
    """
    anatomy = anatomy_available(state)
    blocked = access_blocked(state)
    ifu = token(state.case.n.seal_ifu)

    if blocked is True:
        return Candidate(
            "EVAR",
            CandidateStatus.EXCLUDED,
            "双侧股髂闭塞或严重迂曲，无法建立入路",
            ("D1.3.R01",),
        )
    if ifu == "不符合":
        return Candidate(
            "EVAR",
            CandidateStatus.EXCLUDED,
            "标准 EVAR 不符合 IFU；转复杂 AAA 规划比较 FEVAR/BEVAR",
            ("D1.3.R06",),
        )
    if _branch_involved(state) is True:
        return Candidate(
            "EVAR",
            CandidateStatus.EXCLUDED,
            "近端瘤颈累及重要分支；标准 EVAR 不适配，需 FEVAR/BEVAR 或复杂开放修复",
            ("D1.3.R03",),
        )
    if anatomy is False:
        return Candidate(
            "EVAR",
            CandidateStatus.UNASSESSED,
            "瘤颈长度/直径/角度均缺，无法判断解剖适配",
        )
    if _standard_evar_ready(state) is True:
        return Candidate(
            "EVAR",
            CandidateStatus.FEASIBLE,
            "解剖符合、入路无阻断、近远端着陆区与 IFU 均已核验；术者确认",
            ("D1.3.R05",),
        )

    # 到这里说明「有方向但缺条件」——逐项点名差什么，不合并成一句「资料不全」。
    reasons: list[str] = []
    if anatomy is None:
        reasons.append("瘤颈参数不全")
    if blocked is None:
        reasons.append("入路可行性未评估")
    if ifu == "符合":
        reasons.append("远端密封区（D1.DISTAL_SEAL）未评估")
    elif ifu in ("未核验", "未选择型号"):
        reasons.append("IFU 未核验（未选定型号，无法逐项核对）")
    else:
        reasons.append("IFU 未记录")
    return Candidate(
        "EVAR",
        CandidateStatus.CONDITIONAL,
        "；".join(reasons) or "待术者确认",
        ("D1.3.R01",),
    )


def _osr_candidate(state: GuidelineState) -> Candidate:
    """OSR 这一列 —— 至多到「需 MDT 评估」，不给「可行候选」。

    为什么封顶在 MDT：``D1.3.R02`` 要求「解剖上可安全开放夹闭/重建」，而
    ``VascCase`` 没有描述开放解剖的字段（见 ``_d13_r02`` 的说明）。没有判据就
    不下结论，这是该规则「须由术者/MDT 确认」的落点。

    为什么仍要列出这一行：规格 ``R91`` 要求「**分别**列出」。只给 EVAR 会让读者
    以为 OSR 已被排除，而实际它只是待专家评估——两者临床含义完全不同。
    """
    fitness = _d12_value(state, "D1.OP_FITNESS")
    if fitness is None:
        return Candidate("OSR", CandidateStatus.UNASSESSED, "择期手术适宜性未评价")
    if fitness == "当前不适合":
        return Candidate(
            "OSR",
            CandidateStatus.MDT,
            "当前手术风险不可接受；优化后复评",
            ("D1.3.R02",),
        )
    if fitness == "高风险":
        return Candidate(
            "OSR",
            CandidateStatus.MDT,
            "高危；开放解剖与麻醉评估无输入，须与 EVAR 并列比较",
            ("D1.3.R02",),
        )
    if _d12_value(state, "D1.LIFE_EXPECTANCY") == "有限":
        return Candidate(
            "OSR",
            CandidateStatus.MDT,
            "预期寿命有限，OSR 的长期耐久性优势不成立；由术者确认",
            ("D1.3.R02",),
        )
    return Candidate(
        "OSR",
        CandidateStatus.MDT,
        "风险可接受；开放解剖与长期耐久性比较无输入，须术者/MDT 确认",
        ("D1.3.R02",),
    )


def _d13_gaps(state: GuidelineState) -> tuple[str, ...]:
    """本节点缺什么 —— 只列会改变候选状态的项。

    ``D1.DISTAL_SEAL`` 单列而不并进 ``_D13_ANATOMY_FIELDS``：它是 ``R05``（标准
    EVAR 候选）与 ``R01``（EVAR 候选）的唯一分野，缺它时 ``R05`` 恒不成立，
    必须说清楚差在哪一格，否则读者只会看到「条件性候选」而不知道为什么。
    """
    gaps: list[str] = []

    if anatomy_available(state) is not True:
        gaps.append("EVAR 解剖参数不全（N.NECK_LEN / D1.NECK_DIAMETER / D1.NECK_ANGLE）")
    if access_blocked(state) is None:
        gaps.append("入路可行性未评估（D1.ILIAC_ACCESS_DETAIL：双侧髂股直径/迂曲/钙化）")
    if cta_evaluable(state) is None:
        gaps.append("N.CTA_STATUS：本次 CTA 对本节点任务的可评价性未记录")

    seal = token(state.case.n.seal_ifu)
    if seal is None:
        gaps.append("N.SEAL_IFU：器械近端密封区 IFU 未记录")
    elif seal in ("未核验", "未选择型号"):
        gaps.append("N.SEAL_IFU：未选定型号，无法逐项核对正式 IFU")

    if recorded(state, "D1.DISTAL_SEAL") is not True:
        gaps.append("D1.DISTAL_SEAL：远端髂动脉着陆区未评估，不确认整体 EVAR 适配")

    gaps.extend(missing_fields(state, _D13_ANATOMY_FIELDS))
    gaps.extend(missing_fields(state, _D13_ASSESS_FIELDS))
    return tuple(gaps)


def d1_3(state: GuidelineState) -> NodeOutcome:
    """D1.3：给出 EVAR / OSR 两列候选 + 把握程度 + 待补清单。

    三段式：``evaluate_node`` 判出口（数据阻断 / 复杂解剖 / 非手术 / 两列），
    ``display_entries`` 给四条术式规则留痕（不抢出口），候选在节点层归并。
    顺序不能反过来：候选的理由要能引用同一批判定，不能各算一遍。
    """
    outcome = evaluate_node("D1.3", state, _D13_SLOTS)
    extra_entries, extra_verdicts = display_entries("D1.3", state, _D13_DISPLAY_RULES)
    return replace(
        outcome,
        verdicts={**outcome.verdicts, **extra_verdicts},
        entries=(*outcome.entries, *extra_entries),
        gaps=_d13_gaps(state),
        candidates=(_evar_candidate(state), _osr_candidate(state)),
    )


#: 本模块提供的节点（键与规格 sheet 名一致）。
NODES: dict[str, NodeFn] = {
    "D1.1": d1_1,
    "D1.2": d1_2,
    "D1.3": d1_3,
}

#: 已实现的规则 → 规格源行。``D1.1.R06`` 与 ``D1.3`` 四条术式规则以展示性条目留痕。
RULE_ROWS: dict[str, str] = {
    **{slot.rule_id: slot.source_row for slot in _D11_SLOTS},
    "D1.1.R06": "D1.1!21",
    **{slot.rule_id: slot.source_row for slot in _D12_SLOTS},
    **{slot.rule_id: slot.source_row for slot in _D13_SLOTS},
    **{rule.rule_id: rule.source_row for rule in _D13_DISPLAY_RULES},
}
