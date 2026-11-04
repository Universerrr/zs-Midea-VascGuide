"""D5 · 特殊主动脉病变（``D5.1`` → ``D5.2`` / ``D5.3``）。

规格来源：``VascGuide_V3.6.2.xlsx`` 的 ``D5.1`` / ``D5.2`` / ``D5.3`` 三个 sheet，
共 20 条规则（7 + 6 + 7），全部实现。

## 三个节点各管什么

``D5.1`` 特殊病变分诊
    回答「这是 PAU / IMH / IAAD 这类**壁结构病变**，还是囊状 / 假性 / 炎症性这类
    **形态与病因病变**，还是什么都不是」。前一类 → ``D5.2`` 判复杂性，后一类 →
    ``D5.3`` 定病因与修复口径。

``D5.2`` PAU / IMH / IAAD 的复杂性
    回答「这个壁结构病变有没有并发征象」——扩大、壁外血肿、假性瘤、栓塞、
    持续疼痛、内膜破口、器官灌注障碍。**无并发症才可以进监测**，任一征象成立即
    转专科修复评估。

``D5.3`` 囊状 / 假性 / 炎症性 AAA
    回答「按什么病因口径评估」——假性要查创伤/医源性/术后，炎症性要先鉴别感染
    并给出**排除瘤周炎症后的真实瘤径**。

## 七处必须先讲清的结构决定

**① 三个节点都不出术式，这是规格写的，不是没做完。**
    ``D5.1.R05`` 明写「不在 D5 分类节点直接决定术式」，``D5.3.R01``「不擅自设置
    统一直径阈值」、``D5.3.R02``「术式依解剖和患者风险决定」。所以本文件不产出
    :class:`~..state.Candidate`——给出候选反而会让「特殊形态需专科评估」看起来
    像「已定方案」。

**② 形态字段全在病例模型里，不另立一套 ``D5.*``。**
    ``CL.PAU`` / ``CL.IMH`` / ``CL.IAAD`` / ``CL.SACCULAR`` / ``CL.PSEUDO`` 与
    ``N.INFLAMMATORY_CTX`` / ``N.INFECTION_CTX`` 都已是 ``VascCase`` 字段
    （``G08.R05`` / ``G08.R10`` 的判据就是它们）。``D5.SUBTYPE`` 那张「本次亚型」
    清单在这里是**派生视图**，不是第二处真源——若照着它再判一次，改一处就会分叉。

**③ 模型的 ``LesionGrade`` 与规格的值不一样，读的时候必须按模型写。**
    规格 ``CL.PAU`` 允许值是「明确/疑似/未见/不可评价」，模型是
    「明确/**可疑**/**阴性**/不可评价」。照规格写 ``eq(x, "疑似")`` 会得到恒
    ``False``——不是「判不出」，是「永远判成没有」。本文件统一走
    :func:`_lesion_present` / :func:`_shape_present` 两个入口，值域只在那一处出现。

**④ ``R90`` 只在「没有任何亚型成立」时才阻断。**
    规格 ``D5.1.R90`` 的字面是「关键字段不可评价→不强行归类」。若照字面实现成
    「任一字段未知就阻断」，一个 ``PAU = 明确`` 而 ``CL.SACCULAR`` 没填的病例会被
    挡在人工复核——而它其实完全可以归类。所以先判「有没有明确亚型」：有则放行，
    没有再看有没有判不了的。这是「不强行归类」的原意。

**⑤ ``D5.1.R05``（累及分支）与 ``R01``（壁结构病变）同为 P3，靠声明顺序分工。**
    两者可以同时成立（PAU 累及肾动脉）。``R05`` 声明在前，出口是
    ``escalate G06`` **并** ``also_next D5.2``——把「转复杂路径」和「继续判复杂性」
    两件事都留下。若让 ``R01`` 先命中，病例会被静默送进 ``D5.2``，而「需要按
    复杂 AAA 规划」这条信息就丢了。

**⑥ ``D5.3`` 的五条 P1 是并列评估义务，但出口只能有一个。**
    ``R01`` / ``R02`` / ``R03`` / ``R04`` / ``R09`` 描述的是**不同病因口径下各自
    要做什么**，一个病例可以同时成立两条（囊状 + 炎症）。:class:`NodeOutcome`
    的出口是单值，所以按「最不能跳过的那条」排序：双标签门（``R04``）> 低阈值
    抗炎（``R09``）> 炎症（``R03``）> 假性（``R02``）> 囊状/PAU（``R01``）。
    被遮蔽的义务不丢：:func:`_d53_gaps` 按**字段**而非按命中的规则枚举缺口，
    所以「囊状成立了、炎症的 mantle sign 还没看」照样出现在待补清单里。

    炎症（``R03``）排在囊状（``R01``）之前是有意的：炎症性 AAA 的典型形态就是
    囊状/局灶，反过来排会让绝大多数炎症病例被归到「特殊形态」而丢掉真实瘤径与
    感染鉴别这两件最要紧的事——那两项恰恰是炎症性 AAA 与普通囊状瘤处理上唯一
    实质差别。

**⑦ 修复阈值口径与 ``D1.1`` 共用一份，但比较对象不同。**
    ``D5.3.R09`` 的限定词是「低于常规修复阈值」。55/50 这两个数字放在
    ``common.sex_threshold``，与 ``D1.1`` 同一份——各写一遍不会报错，只会让同一
    病例在两个节点得到相反的阈值判断。比较对象则**故意不同**：``D1.1`` 比的是
    ``CL.AAA_DIAM``，本条比的是 ``INF.AAA_TRUE_DIAM``（ESVS Rec 145：修复指征
    不得把瘤周炎症与壁水肿算入直径）。混用会让炎症性 AAA 被总径推进手术。
"""

from __future__ import annotations

from dataclasses import replace

from ....rules.router import Verdict, and_, eq, has, not_, or_, token
from ..state import GuidelineState, Link, NodeFn, NodeOutcome
from .common import (
    DisplayRule,
    RuleSlot,
    branch_involved,
    cta_evaluable,
    display_entries,
    evaluate_node,
    gap_if_missing,
    infection_typed,
    is_spec_uncertain,
    missing_fields,
    multi_field,
    sex_threshold,
    spec_token,
)
from .common import (
    malperfusion as _malperfusion,
)

__all__ = ["NODES", "d5_1", "d5_2", "d5_3"]

#: ``CL.PAU`` / ``CL.IMH`` / ``CL.IAAD`` 的**模型**阳性档（规格写「疑似」，模型写「可疑」）。
_LESION_POSITIVE: tuple[str, ...] = ("明确", "可疑")

#: ``CL.SACCULAR`` / ``CL.PSEUDO`` 的模型阳性档（规格写「否定」，模型写「否」）。
_SHAPE_POSITIVE: tuple[str, ...] = ("明确", "疑似")

#: ``N.INFLAMMATORY_CTX`` 里表示「炎症性 AAA 成立或高度可能」的三档。
_INFLAMMATORY_POSITIVE: tuple[str, ...] = ("临床确认", "影像高度提示", "可疑")

#: ``N.INFECTION_CTX`` 里表示「感染**未**排除」的三档（``无相关征象`` 是唯一的排除档）。
_INFECTION_UNEXCLUDED: tuple[str, ...] = ("医师明确", "高度怀疑", "仅非特异征象")

#: ``CL.INFECTION`` 里表示「感染已分类成立」的四个取值。
_INFECTION_POSITIVE: tuple[str, ...] = (
    "原生感染明确",
    "原生感染疑似",
    "移植物感染明确",
    "移植物感染疑似",
)

#: ``INF.*`` 证据明细里已有病例模型落点的字段（其余走 ``node_inputs`` 通道）。
_INF_MODEL_FIELDS: dict[str, str] = {
    "INF.MANTLE_SIGN": "mantle_sign",
    "INF.WALL_FIBROSIS": "wall_fibrosis",
    "INF.ORGAN_INVOLVEMENT": "organ_involvement",
    "INF.AAA_TRUE_DIAM": "aaa_true_diam",
    "INF.SYMPTOM_PATTERN": "symptom_pattern",
}


# ---------------------------------------------------------------------------
# 判据原语
# ---------------------------------------------------------------------------
def _lesion_present(value: object) -> Verdict:
    """``CL.PAU`` / ``CL.IMH`` / ``CL.IAAD`` 是否成立（明确或可疑）。

    「可疑」在这里是**阳性**：规格把 PAU/IMH/IAAD 的「疑似」列为 D5 入口条件
    （``G08.R05`` 同口径）。把可疑当未成立，壁结构病变会被静默降级成普通 AAA，
    而它恰恰是最需要单独评估的那一类。
    """
    return eq(token(value), *_LESION_POSITIVE)


def _shape_present(value: object) -> Verdict:
    """``CL.SACCULAR`` / ``CL.PSEUDO`` 是否成立（明确或疑似）。"""
    return eq(token(value), *_SHAPE_POSITIVE)


def _inflammatory(state: GuidelineState) -> Verdict:
    """炎症性 AAA 是否成立（临床确认 / 影像高度提示 / 可疑）。"""
    return eq(token(state.case.n.inflammatory_ctx), *_INFLAMMATORY_POSITIVE)


def _infection_excluded(state: GuidelineState) -> Verdict:
    """感染是否**已明确排除** —— 三值。

    ``True`` 已排除（``N.INFECTION_CTX = 无相关征象`` 且 ``CL.INFECTION = 无证据``）；
    ``False`` 未排除（含「仅非特异征象」——单独发热/CRP 升高不能确诊，也不能排除）；
    ``None`` 判不了（两处都没记录或都写着不确定）。

    回答「排除没排除」而不是「有没有感染」，是因为规格在 D5 反复要求「先鉴别
    感染」：这里需要的正是「能不能往下定炎症性 AAA」这个是非题。
    """
    ctx = token(state.case.n.infection_ctx)
    classified = token(state.case.cl.infection)
    if classified in _INFECTION_POSITIVE or ctx in _INFECTION_UNEXCLUDED:
        return False
    # 影像侧说「无相关征象」已足够排除，不要求分类字段也写「无证据」——
    # 那两处本来就是同一事实的两个粒度，要求同时有值只会制造假缺口。
    if ctx != "无相关征象" and classified != "无证据":
        return None
    # 但另一侧若明确写着「未评价」，两处就是**冲突**而不是互补：
    # 一侧说查过没有、另一侧说还没查。按三值口径只能给 UNKNOWN——
    # 给 True 会让「感染未排除」的标签被一个影像描述消掉。
    for other in (ctx, classified):
        if other is not None and is_spec_uncertain(other):
            return None
    return True


def _inf(state: GuidelineState, spec_id: str) -> object:
    """读 ``INF.*`` 证据明细 —— 优先病例模型，其次 ``node_inputs`` 通道。

    为什么不只用通道：``INF.MANTLE_SIGN`` / ``WALL_FIBROSIS`` /
    ``ORGAN_INVOLVEMENT`` / ``AAA_TRUE_DIAM`` / ``SYMPTOM_PATTERN`` 五个字段在
    ``VascCase.inf`` 里已有落点（``G04`` 的炎症派生要用它们），再要求调用方在
    ``node_inputs`` 里重复一份就是同一事实两处真源。只有 ``INF.IGG4`` /
    ``INF.PET_MRI`` 模型里没有，才真的走通道。
    """
    attr = _INF_MODEL_FIELDS.get(spec_id)
    if attr is not None:
        value = getattr(state.case.inf, attr)
        if value is not None:
            return value
    return state.node_field(spec_id)


def _inf_gaps(state: GuidelineState, fields: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    """``INF.*`` 字段的缺口 —— 与 :func:`_inf` 走同一条读取路径。

    不能直接用 ``missing_fields``：那个只读 ``node_inputs``，而 ``INF.*`` 的五个
    字段在病例模型里。两处读法不一致的后果很隐蔽——判定按模型读到了值，缺口
    却说「未记录」，于是报告同时出现「已确认 mantle sign」和「mantle sign 未记录」。
    """
    out: list[str] = []
    for spec_id, label in fields:
        gap = gap_if_missing(f"{spec_id}（{label}）", _inf(state, spec_id))
        if gap:
            out.append(gap)
    return tuple(out)


def _organ_involvement(state: GuidelineState) -> list[object] | None:
    """炎症累及周围结构（多选）——统一成 ``list``。"""
    value = _inf(state, "INF.ORGAN_INVOLVEMENT")
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return list(value)
    return [value]


# ---------------------------------------------------------------------------
# D5.1 特殊病变分诊
# ---------------------------------------------------------------------------
def _d51_r03(state: GuidelineState) -> Verdict:
    """P0-A：破裂或重大急性危险 —— 只接受**明确的本次事实**。

    判据里没有「可疑」血肿/可疑外溢：规格 ``D5.1`` 的 J 列写「已由**可靠**本次
    事实触发的 P0 危险警示不得被无关缺失取消」——反过来说，不可靠的征象不该
    触发 P0。可疑影像走 ``D5.2.R03`` 的「高危组合」档，加急但不等同急诊。
    """
    return or_(
        eq(token(state.case.cl.aaa_rupture), "明确", "疑似"),
        eq(token(state.case.c.extrav), "明确"),
        eq(token(state.case.c.hematoma), "明确"),
        has(multi_field(state, "D5.SYMPTOMS"), "栓塞", "分支灌注障碍"),
        _malperfusion(state),
    )


def _d51_r02(state: GuidelineState) -> Verdict:
    """P1：囊状 / 假性 / 炎症性 AAA 成立 → ``D5.3``。"""
    return or_(
        _shape_present(state.case.cl.saccular),
        _shape_present(state.case.cl.pseudo),
        _inflammatory(state),
    )


def _d51_r01(state: GuidelineState) -> Verdict:
    """P3：PAU / IMH / IAAD 任一成立 → ``D5.2`` 判复杂性。"""
    return or_(
        _lesion_present(state.case.cl.pau),
        _lesion_present(state.case.cl.imh),
        _lesion_present(state.case.cl.iaad),
    )


def _d51_r05(state: GuidelineState) -> Verdict:
    """P3：累及肾/内脏分支或超出 Ⅳ 型 TAAA 项目边界 → ``G04``/``G06`` 复杂路径。

    判据直接复用 ``common.branch_involved``：``D1.3`` / ``D2.3`` / ``D3.2`` 问的
    是同一个问题（「标准腔内修复还算不算标准」），三处各写一遍会分叉。
    """
    return branch_involved(state)


def _d51_r04(state: GuidelineState) -> Verdict:
    """P1（展示性）：囊状 / 假性 / 炎症性 → 必须核查病因背景。

    做成展示性规则而不是出口，是因为它与 ``R02`` 的出口**不是竞争关系**：
    ``R02`` 回答「去 D5.3」，本条回答「到了 D5.3 要查什么」。放进出口竞争，
    先命中的 ``R02`` 会让这条义务连痕迹都不留。
    """
    return _d51_r02(state)


_D51_MORPHOLOGY: tuple[tuple[str, str], ...] = (
    ("CL.PAU", "穿透性主动脉溃疡（PAU）"),
    ("CL.IMH", "主动脉壁内血肿（IMH）"),
    ("CL.IAAD", "孤立性腹主动脉夹层（IAAD）"),
    ("CL.SACCULAR", "囊状主动脉瘤"),
    ("CL.PSEUDO", "假性动脉瘤"),
    ("N.INFLAMMATORY_CTX", "炎症性 AAA 证据状态"),
)

_D51_FIELDS: tuple[tuple[str, str], ...] = (
    ("D5.SYMPTOMS", "本次症状与病变的时点关系（持续/复发疼痛、栓塞、灌注障碍）"),
    ("D5.LESION_LOCATION", "病变解剖段与分支关系（肾下/肾旁/内脏旁/髂段）"),
)


def _d51_unresolved_names(state: GuidelineState) -> list[str]:
    """列出「判不了」的形态/炎症字段 —— 只用于缺口文案，不参与判定。"""
    values = (
        state.case.cl.pau,
        state.case.cl.imh,
        state.case.cl.iaad,
        state.case.cl.saccular,
        state.case.cl.pseudo,
        state.case.n.inflammatory_ctx,
    )
    names: list[str] = []
    for (spec_id, label), value in zip(_D51_MORPHOLOGY, values, strict=True):
        raw = token(value)
        if raw is None or is_spec_uncertain(raw):
            names.append(f"{spec_id}（{label}）：{raw or '未记录'}")
    return names


def _d51_gaps(state: GuidelineState) -> tuple[str, ...]:
    """D5.1 缺什么 —— 亚型清单 + 病因/定位 + 感染鉴别 + 分支边界。"""
    gaps = list(missing_fields(state, _D51_FIELDS))
    if _d51_subtype_established(state) is not True:
        gaps.extend(_d51_unresolved_names(state))
    if _d51_r02(state) is True:
        cause = state.node_field("D5.CAUSE")
        if cause is None or is_spec_uncertain(cause) or cause in ([], ()):
            gaps.append("D5.CAUSE：病因/背景未记录（感染/创伤/医源性/既往修复/退变）")
    if _infection_excluded(state) is not True:
        gaps.append("感染证据未排除（N.INFECTION_CTX / CL.INFECTION）：按 D4.1 并行评估")
    if branch_involved(state) is True:
        gaps.append("累及肾/内脏分支或胸腹主动脉：按 G04/G06 复杂路径评估（本节点不决定术式）")
    if _d51_r02(state) is True and _d51_r01(state) is True:
        gaps.append(
            "同时存在壁结构病变与形态/病因病变：D5.2 复杂性评估未执行（本轮按 P1 先进入 D5.3）"
        )
    return tuple(gaps)


def _d51_subtype_established(state: GuidelineState) -> Verdict:
    """是否已有**明确成立**的特殊病变亚型（决定 R90 要不要阻断）。"""
    return or_(_d51_r01(state), _d51_r02(state))


def _d51_subtype_unresolved(state: GuidelineState) -> bool:
    """是否存在判不了的形态/炎症字段（只用于 R90 的阻断判断）。"""
    return bool(_d51_unresolved_names(state))


def _d51_r90(state: GuidelineState) -> Verdict:
    """P2：没有明确亚型、又有判不了的字段 → 不强行归类。

    与规格字面的差别写在模块文档第 ④ 条：字面是「任一关键字段不可评价即阻断」，
    实现是「**没有**任何亚型成立时才阻断」。差别只在 ``PAU = 明确`` 且
    ``CL.SACCULAR`` 未填这类组合上——那时归类依据已经充分，阻断只会多一次
    无意义的人工复核。
    """
    if _d51_subtype_established(state) is True:
        return False
    return bool(_d51_subtype_unresolved(state))


def _d51_r91(state: GuidelineState) -> Verdict:
    """P3：全部明确未见且影像足够 → 返回 ``G08`` 重新鉴别其他病因。"""
    return and_(
        cta_evaluable(state),
        eq(token(state.case.cl.pau), "阴性"),
        eq(token(state.case.cl.imh), "阴性"),
        eq(token(state.case.cl.iaad), "阴性"),
        eq(token(state.case.cl.saccular), "否"),
        eq(token(state.case.cl.pseudo), "否"),
        eq(token(state.case.n.inflammatory_ctx), "无当前支持证据"),
        _infection_excluded(state),
    )


_D51_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D5.1.R03",
        priority="P0-A",
        source_row="D5.1!13",
        statement="破裂或重大急性危险（活动性出血/急性器官或肢体灌注障碍）→ 回 G03/G05，不等待特殊病种完整分类",
        judge=_d51_r03,
        exit_kind="escalate",
        exit_value="G05",
        next_nodes=("G03", "G05"),
        link=Link.EITHER,
    ),
    RuleSlot(
        rule_id="D5.1.R02",
        priority="P1",
        source_row="D5.1!12",
        statement="囊状、假性或炎症性 AAA 明确/疑似 → D5.3（炎症与感染并存时 D4/D5 双标签）",
        judge=_d51_r02,
        exit_kind="next",
        exit_value="D5.3",
        next_nodes=("D5.3", "D4.1"),
        link=Link.PARALLEL,
    ),
    RuleSlot(
        rule_id="D5.1.R90",
        priority="P2",
        source_row="D5.1!10",
        statement="病变亚型不可评价或病因证据冲突 → 不强行归类，转影像/主动脉专科复核",
        judge=_d51_r90,
        exit_kind="terminal",
        exit_value="人工复核（特殊病变亚型或病因证据待核查）",
    ),
    RuleSlot(
        rule_id="D5.1.R05",
        priority="P3",
        source_row="D5.1!18",
        statement="累及肾/内脏分支或超出 Ⅳ 型 TAAA 项目边界 → G04/G06 复杂或相关主动脉路径（不在本节点决定术式）",
        judge=_d51_r05,
        exit_kind="escalate",
        exit_value="G06",
        # 规格 M 列：「下一节点/终点：G04、G06 / 动作：G04/G06/专科路径」——
        # 值域里没有 D5.2。修前写的 also_next="D5.2" 是代码自加的，规格没有。
        next_nodes=("G04", "G06"),
    ),
    RuleSlot(
        rule_id="D5.1.R01",
        priority="P3",
        source_row="D5.1!11",
        statement="PAU / IMH / IAAD 任一明确或疑似 → D5.2（同一病灶可同时带多个标签）",
        judge=_d51_r01,
        exit_kind="next",
        exit_value="D5.2",
    ),
    RuleSlot(
        rule_id="D5.1.R91",
        priority="P3",
        source_row="D5.1!9",
        statement="全部明确未见且影像足够 → 返回 G08 重新鉴别其他病因",
        judge=_d51_r91,
        exit_kind="escalate",
        exit_value="G08",
        next_nodes=("D5.2", "D5.3", "G08"),
        link=Link.EITHER,
    ),
)

_D51_DISPLAY_RULES: tuple[DisplayRule, ...] = (
    DisplayRule(
        rule_id="D5.1.R04",
        source_row="D5.1!17",
        statement="囊状/假性/炎症性 AAA → 明确感染、创伤、医源性及既往修复背景；感染阳性或疑似时并行 D4",
        judge=_d51_r04,
    ),
)


def d5_1(state: GuidelineState) -> NodeOutcome:
    """D5.1：分诊到 D5.2（壁结构病变）/ D5.3（形态与病因）/ G08（都不是）。"""
    outcome = evaluate_node("D5.1", state, _D51_SLOTS)

    # ``D5.1.R02`` 的第二条去处是**有条件的**：规格动作列写「D5.3；感染疑似并行
    # D4.1」，而本规则只判「囊状/假性/炎症」，不判感染。所以并行只在感染侧也有
    # 证据时展开——否则每个囊状病变都会多跑一遍感染分诊，报告里凭空多出一条
    # 「无感染证据」的结论，把「没查」写成了「查过没有」。
    if outcome.verdicts.get("D5.1.R02") is True:
        parallel = infection_typed(state) is True
        outcome = replace(
            outcome,
            next_nodes=("D5.3", "D4.1") if parallel else ("D5.3",),
            link=Link.PARALLEL if parallel else Link.EITHER,
        )

    extra_entries, extra_verdicts = display_entries("D5.1", state, _D51_DISPLAY_RULES)
    return replace(
        outcome,
        verdicts={**outcome.verdicts, **extra_verdicts},
        entries=(*outcome.entries, *extra_entries),
        gaps=_d51_gaps(state),
    )


# ---------------------------------------------------------------------------
# D5.2 PAU / IMH / IAAD 的复杂性
# ---------------------------------------------------------------------------
def _growth(state: GuidelineState) -> object:
    return spec_token(state.node_field("D5.LESION_GROWTH"))


def _bleed(state: GuidelineState) -> object:
    return spec_token(state.node_field("D5.PERIAORTIC_BLEED"))


def _embolism(state: GuidelineState) -> object:
    return spec_token(state.node_field("D5.EMBOLISM"))


def _recurrent_pain(state: GuidelineState) -> object:
    return spec_token(state.node_field("D5.PAIN_RECURRENT"))


def _intimal(state: GuidelineState) -> object:
    return spec_token(state.node_field("D5.INTIMAL_DISRUPTION"))


def _d52_lesion(state: GuidelineState) -> Verdict:
    """本次是否确有壁结构病变（决定复杂征象该不该被解读）。"""
    return _d51_r01(state)


def _d52_r03(state: GuidelineState) -> Verdict:
    """P1：高危组合（PAU 伴 IMH / 进行性扩大 / 壁外血肿）→ 加急专科评估。

    规格在这里特意写了半句话：「这本身**不等于**已破裂，也不自动诊断需要立即
    手术」。所以出口是「加急评估」而不是急诊模式——真正的 P0 由 ``D5.1.R03``
    与判定层 ``G03``/``G05`` 承接。
    """
    lesion = _d52_lesion(state)
    if lesion is not True:
        return lesion
    return or_(
        and_(_lesion_present(state.case.cl.pau), _lesion_present(state.case.cl.imh)),
        eq(_growth(state), "扩大"),
        eq(_bleed(state), "明确", "疑似"),
    )


def _d52_r02(state: GuidelineState) -> Verdict:
    """P1：任一复杂征象 → 主动脉专科评估是否修复。"""
    return or_(
        eq(_growth(state), "扩大"),
        eq(_bleed(state), "明确", "疑似"),
        eq(_embolism(state), "是", "疑似"),
        _malperfusion(state),
        eq(_recurrent_pain(state), "持续", "难治", "反复"),
        eq(_intimal(state), "明确", "疑似"),
        eq(token(state.case.cl.aaa_rupture), "明确", "疑似"),
    )


#: 各复杂征象字段「可用于判断」的取值集合 —— 之外的（含 ``不可比``）都算没评。
_D52_ASSESSED: dict[str, tuple[str, ...]] = {
    "D5.LESION_GROWTH": ("扩大", "稳定", "缩小"),
    "D5.PERIAORTIC_BLEED": ("明确", "疑似", "无"),
    "D5.EMBOLISM": ("是", "否", "疑似"),
    "D5.PAIN_RECURRENT": ("持续", "难治", "反复", "已缓解", "无"),
    "D5.INTIMAL_DISRUPTION": ("明确", "疑似", "未见"),
}


def _d52_unassessed(state: GuidelineState) -> list[str]:
    """列出「判不了」的复杂征象字段（只用于缺口与 R90，不参与阳性判断）。"""
    values = {
        "D5.LESION_GROWTH": _growth(state),
        "D5.PERIAORTIC_BLEED": _bleed(state),
        "D5.EMBOLISM": _embolism(state),
        "D5.PAIN_RECURRENT": _recurrent_pain(state),
        "D5.INTIMAL_DISRUPTION": _intimal(state),
    }
    names: list[str] = []
    for spec_id, value in values.items():
        if value is None:
            names.append(f"{spec_id}：未记录")
        elif value not in _D52_ASSESSED[spec_id]:
            # 「不可比」是规格单列的一档：有值，但不能拿来做前后比较。
            names.append(f"{spec_id}：{value}")
    if _malperfusion(state) is None:
        names.append("C.MALPERFUSION：器官/肢体灌注状态未评价")
    return names


def _d52_r90(state: GuidelineState) -> Verdict:
    """P2：任一复杂征象判不了 → 不得判为「无并发症」。

    只判「判不了」，不判「阳性」：阳性已由 P1 的 ``R02`` 接走，在这里再判一次
    只会让同一个事实出现两条出口。
    """
    return bool(_d52_unassessed(state))


def _d52_r01(state: GuidelineState) -> Verdict:
    """P1（规格 G 列写作 P3）：全部复杂征象明确未见 → 无并发症候选。

    规格这一行的类别列写 ``P1``、判断列写「【P3｜无并发症候选】」，两处不一致。
    取 ``P3`` 的依据是判断列本身：它要求「任何关键项未知/不可评价 → 走 R90，
    不当作阴性」——这是个**全阴性合取**，逻辑上必须排在 ``R90`` 之后。若给 ``P1``，
    「关键项还没评」的病例会被它先命中，直接得到「无并发症」这个最危险的结论。
    """
    return and_(
        eq(_growth(state), "稳定", "缩小"),
        eq(_bleed(state), "无"),
        eq(_embolism(state), "否"),
        not_(_malperfusion(state)),
        eq(_recurrent_pain(state), "已缓解", "无"),
        eq(_intimal(state), "未见"),
        not_(eq(token(state.case.cl.aaa_rupture), "明确", "疑似")),
    )


def _d52_r06(state: GuidelineState) -> Verdict:
    """P3（展示性）：腹主动脉 PAU/IMH/IAAD 无统一尺寸阈值，只保留数值与变化。

    做成展示性规则的理由：它是一条**取值口径**，不是一次分流。判据写「有没有
    留下具体数值/变化」，命中即表示「本节点按数值呈现、不按单一深度或厚度自动
    判修复」——这条留痕本身就是规格要求的输出。
    """
    return or_(
        _recorded(state, "D5.PAU_DEPTH"),
        _recorded(state, "D5.IMH_FEATURES"),
        _recorded(state, "D5.IAAD_FEATURES"),
        eq(_growth(state), "扩大", "稳定", "缩小"),
    )


def _recorded(state: GuidelineState, spec_id: str) -> Verdict:
    """病种域字段是否已记下明确内容（缺失 / 不确定 → ``None``）。"""
    value = state.node_field(spec_id)
    if value is None:
        return None
    if isinstance(value, str):
        return None if is_spec_uncertain(value) else True
    if isinstance(value, list | tuple):
        return True if value else None
    return True


def _d52_r91(state: GuidelineState) -> Verdict:
    """P3 出口：结构兜底。

    **端到端不可达**：``R01``（全阴性）、``R02``（任一阳性）、``R90``（判不了）
    三者的判据把输入空间分完了——任何一个字段要么阳性、要么阴性、要么判不了。
    保留它是为了「以后新增征象字段时不会漏掉兜底」，与 ``D2.2.R91`` 同性质。
    """
    return True


_D52_FIELDS: tuple[tuple[str, str], ...] = (
    ("D5.LESION_GROWTH", "病变是否扩大/进展（同方法影像比较）"),
    ("D5.PERIAORTIC_BLEED", "主动脉周围/壁外血肿或假性动脉瘤"),
    ("D5.EMBOLISM", "相关栓塞症状（远端肢体/脏器缺血）"),
    ("D5.PAIN_RECURRENT", "疼痛是否持续、难治或反复"),
    ("D5.INTIMAL_DISRUPTION", "内膜破口或溃疡样突起（与真性 PAU 分开标注）"),
)

#: 各亚型的形态细节字段 —— 只列本次存在的亚型，避免给 IAAD 病例要 PAU 深度。
_D52_MORPH_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "pau": (("D5.PAU_DEPTH", "PAU 最大深度/宽度/位置（保留原值，不设统一阈值）"),),
    "imh": (("D5.IMH_FEATURES", "IMH 最大厚度/主动脉直径/起止范围"),),
    "iaad": (("D5.IAAD_FEATURES", "IAAD 入口位置/近远端范围/真假腔/分支受累"),),
}


def _d52_gaps(state: GuidelineState) -> tuple[str, ...]:
    """D5.2 缺什么 —— 复杂征象清单 + 按亚型的形态细节。"""
    gaps = list(missing_fields(state, _D52_FIELDS))
    for attr, fields in _D52_MORPH_FIELDS.items():
        if token(getattr(state.case.cl, attr)) in _LESION_POSITIVE:
            gaps.extend(missing_fields(state, fields))
    gaps.extend(_d52_unassessed(state))
    # 去重保序：``_D52_FIELDS`` 与 ``_d52_unassessed`` 会覆盖同几个字段。
    return tuple(dict.fromkeys(gaps))


_D52_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D5.2.R03",
        priority="P1",
        source_row="D5.2!11",
        statement="PAU 伴 IMH、进行性扩大或其他高危组合 → 加急主动脉专科评估（不等于已破裂，也不自动等于立即手术）",
        judge=_d52_r03,
        exit_kind="terminal",
        exit_value="加急主动脉专科评估（是否修复由团队判断；有明确破裂/休克/急性缺血时按 P0 走 G03/G05）",
    ),
    RuleSlot(
        rule_id="D5.2.R02",
        priority="P1",
        source_row="D5.2!10",
        statement="任一复杂征象（扩大/壁外血肿/假性瘤/栓塞/持续疼痛/内膜破口/灌注障碍）→ 主动脉专科评估是否修复",
        judge=_d52_r02,
        exit_kind="terminal",
        exit_value="主动脉专科评估是否修复（术式依解剖与患者风险由团队决定；明确破裂/休克/急性缺血按 P0 走 G03/G05）",
    ),
    RuleSlot(
        rule_id="D5.2.R90",
        priority="P2",
        source_row="D5.2!8",
        statement="增长可比性、壁外血肿、症状或灌注状态关键项未知/冲突 → 不判为无并发症，复核影像与临床",
        judge=_d52_r90,
        exit_kind="terminal",
        exit_value="人工复核（复杂征象评估不完整，不得判为无并发症）",
    ),
    RuleSlot(
        rule_id="D5.2.R01",
        priority="P3",
        source_row="D5.2!9",
        statement="影像充分、无扩大/壁外血肿/假性瘤/栓塞/持续疼痛/灌注障碍 → 无并发症候选，由专科医生决定药物治疗与影像监测",
        judge=_d52_r01,
        exit_kind="terminal",
        exit_value="无并发症候选：专科医生确认最佳药物治疗 + 影像监测（随访间隔由团队定）",
    ),
    RuleSlot(
        rule_id="D5.2.R91",
        priority="P3",
        source_row="D5.2!7",
        statement="任一复杂征象或破裂疑虑 → 修复/急诊评估；全部未见且稳定 → 医生确认药物治疗与影像监测",
        judge=_d52_r91,
        exit_kind="terminal",
        exit_value="主动脉专科评估：修复指征与随访方案由团队决定",
    ),
)

_D52_DISPLAY_RULES: tuple[DisplayRule, ...] = (
    DisplayRule(
        rule_id="D5.2.R06",
        source_row="D5.2!16",
        statement="腹主动脉 PAU/IMH/IAAD 无指南统一尺寸阈值：保留具体数值与时间变化，不把单一深度/厚度写成自动修复条件",
        judge=_d52_r06,
    ),
)


def d5_2(state: GuidelineState) -> NodeOutcome:
    """D5.2：判 PAU/IMH/IAAD 有无并发征象，给出加急/修复评估/监测三档出口。"""
    outcome = evaluate_node("D5.2", state, _D52_SLOTS)
    extra_entries, extra_verdicts = display_entries("D5.2", state, _D52_DISPLAY_RULES)
    return replace(
        outcome,
        verdicts={**outcome.verdicts, **extra_verdicts},
        entries=(*outcome.entries, *extra_entries),
        gaps=_d52_gaps(state),
    )


# ---------------------------------------------------------------------------
# D5.3 囊状 / 假性 / 炎症性 AAA
# ---------------------------------------------------------------------------
def _mantle(state: GuidelineState) -> object:
    """瘤周套袖征的原始取值（含「无法评价」，故不走 ``spec_token``）。"""
    return token(_inf(state, "INF.MANTLE_SIGN"))


def _true_diameter(state: GuidelineState) -> float | None:
    """排除瘤周炎症与壁水肿后的真实瘤径（``INF.AAA_TRUE_DIAM``）。"""
    value = _inf(state, "INF.AAA_TRUE_DIAM")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _below_aortic_repair_threshold(state: GuidelineState) -> Verdict:
    """**真实瘤径**是否低于性别修复阈值 —— 三值。

    这里用的是 ``INF.AAA_TRUE_DIAM`` 而不是 ``cl.aaa_diam``：ESVS Rec 145 明写
    「用于修复指征时不将瘤周炎症组织或壁水肿算入动脉瘤直径」。炎症性 AAA 的
    直径之所以看起来「已经 56 mm 该做了」，往往正是那圈炎症撑出来的——用总径
    套阈值会把一批本该先做抗炎评估的病例直接推进手术。

    阈值本身取自 ``common.sex_threshold``（与 ``D1.1`` 同一份 55/50），本函数
    只做比较，不产出修复指征。
    """
    sex = sex_threshold(state)
    diam = _true_diameter(state)
    if sex is None or diam is None:
        return None
    return diam < sex[1]


def _inflammatory_symptoms(state: GuidelineState) -> Verdict:
    """炎症相关症状：持续/反复腹背痛、体重下降，或明显肾积水。"""
    pattern = spec_token(_inf(state, "INF.SYMPTOM_PATTERN"))
    return or_(
        eq(pattern, "持续/反复腹背痛", "体重下降"),
        has(_organ_involvement(state), "肾积水"),
    )


def _d53_r04(state: GuidelineState) -> Verdict:
    """P1：炎症性 AAA 与感染并存/冲突/感染未排除 → 保留双标签，并行 D4.1。

    「感染未排除」也包括**判不了**（``None``）：规格 J 列写「mantle sign、CRP/ESR
    升高均不能单独确诊非感染性炎症 AAA」，所以没有明确的排除证据时不能取消
    「感染疑似」这个标签。
    """
    if _inflammatory(state) is not True:
        return False
    return _infection_excluded(state) is not True


def _d53_r09(state: GuidelineState) -> Verdict:
    """P1：低于常规修复阈值但有明显炎症相关症状 → MDT 评估抗炎治疗路径。

    四个条件缺一不可：炎症成立（已排除感染）、真实瘤径低于性别阈值、症状/肾积水
    明显、影像有 mantle sign。药物种类与剂量由医生决定，本规则只把「该走抗炎
    讨论」这件事摆上桌。
    """
    return and_(
        _inflammatory(state),
        _infection_excluded(state),
        _below_aortic_repair_threshold(state),
        _inflammatory_symptoms(state),
        eq(_mantle(state), "明确存在", "可疑"),
    )


def _d53_r03(state: GuidelineState) -> Verdict:
    """P1：炎症性 AAA 成立（含可疑）→ 专科评估；先鉴别感染，真实瘤径排除瘤周炎症。"""
    return _inflammatory(state)


def _d53_r02(state: GuidelineState) -> Verdict:
    """P1：假性动脉瘤明确或疑似 → 查明创伤/医源性/术后/感染/瘘病因。"""
    return _shape_present(state.case.cl.pseudo)


def _d53_r01(state: GuidelineState) -> Verdict:
    """P1：囊状真性瘤或 PAU → 特殊形态评估，不套用梭形 AAA 的单一阈值。"""
    return or_(_shape_present(state.case.cl.saccular), _lesion_present(state.case.cl.pau))


def _d53_established(state: GuidelineState) -> Verdict:
    """是否已有明确的特殊形态/病因（决定 R90 要不要阻断）。"""
    return or_(_d53_r01(state), _d53_r02(state), _d53_r03(state))


def _d53_unresolved_names(state: GuidelineState) -> list[str]:
    """列出「判不了」的形态与炎症入口字段（只用于缺口与 R90）。"""
    names: list[str] = []
    for spec_id, label, value in (
        ("CL.SACCULAR", "囊状主动脉瘤", state.case.cl.saccular),
        ("CL.PSEUDO", "假性动脉瘤", state.case.cl.pseudo),
        ("CL.PAU", "穿透性主动脉溃疡（PAU）", state.case.cl.pau),
        ("N.INFLAMMATORY_CTX", "炎症性 AAA 证据状态", state.case.n.inflammatory_ctx),
    ):
        raw = token(value)
        if raw is None or is_spec_uncertain(raw):
            names.append(f"{spec_id}（{label}）：{raw or '未记录'}")
    return names


def _d53_r90(state: GuidelineState) -> Verdict:
    """P2：没有明确形态/病因、又有判不了的字段 → 不套用普通 AAA 阈值。

    与 ``D5.1.R90`` 同一条口径（见模块文档第 ④ 条）：先看有没有明确亚型，
    有则放行；一个都没有才阻断。
    """
    if _d53_established(state) is True:
        return False
    return bool(_d53_unresolved_names(state))


def _d53_r91(state: GuidelineState) -> Verdict:
    """P3：无特殊病变证据且普通 AAA 确认 → 回 ``D1.1`` 走常规路径。"""
    return and_(
        cta_evaluable(state),
        eq(token(state.case.cl.saccular), "否"),
        eq(token(state.case.cl.pseudo), "否"),
        eq(token(state.case.cl.pau), "阴性"),
        eq(token(state.case.n.inflammatory_ctx), "无当前支持证据"),
        _infection_excluded(state),
    )


_D53_SHARED_FIELDS: tuple[tuple[str, str], ...] = (
    ("D5.LESION_GROWTH", "病变是否扩大/进展（同方法影像比较）"),
    ("D5.LESION_LOCATION", "病变解剖段与分支关系"),
)

_D53_SACCULAR_FIELDS: tuple[tuple[str, str], ...] = (
    ("D5.SACCULAR_DEF", "囊状形态定义（球形/局灶累及部分周径）与感染排除"),
)

_D53_PSEUDO_FIELDS: tuple[tuple[str, str], ...] = (
    ("D5.PSEUDO_CAUSE", "假性瘤病因与壁连续性（术后吻合口/创伤/医源性/感染/溃疡进展）"),
)


def _d53_gaps(state: GuidelineState) -> tuple[str, ...]:
    """D5.3 缺什么 —— 按**字段**枚举，不按命中的规则枚举。

    为什么按字段：五条 P1 是并列义务，出口只有一个（见模块文档第 ⑥ 条）。
    若缺口跟着命中的规则走，「囊状成立 + 炎症证据未收」这种组合里，炎症那半边
    的义务会随出口一起消失。按字段枚举则两种义务都在清单里。
    """
    gaps: list[str] = []
    if _d53_established(state) is not True:
        gaps.extend(_d53_unresolved_names(state))

    inflammatory = _inflammatory(state)
    if inflammatory is not False:
        gaps.extend(
            _inf_gaps(
                state,
                (
                    ("INF.MANTLE_SIGN", "瘤周套袖征（mantle sign）"),
                    ("INF.WALL_FIBROSIS", "瘤壁增厚/瘤周或腹膜后纤维化"),
                    ("INF.ORGAN_INVOLVEMENT", "炎症相关周围结构受累（输尿管/肾积水/十二指肠等）"),
                    ("INF.AAA_TRUE_DIAM", "排除瘤周炎症与壁水肿后的真实瘤径"),
                    ("INF.SYMPTOM_PATTERN", "炎症相关症状（持续/反复腹背痛、体重下降）"),
                    ("INF.IGG4", "IgG4 相关疾病评估证据（未检测不等于阴性）"),
                    ("INF.PET_MRI", "FDG-PET/CT 或 MRI 炎症活动证据（未做不当阴性）"),
                ),
            )
        )
        if _true_diameter(state) is None:
            gaps.append(
                "INF.AAA_TRUE_DIAM：真实瘤径未分离——瘤周炎症/壁水肿算入直径时不得套用常规修复阈值（ESVS Rec 145）"
            )
    if _shape_present(state.case.cl.saccular) is not False:
        gaps.extend(missing_fields(state, _D53_SACCULAR_FIELDS))
    if _shape_present(state.case.cl.pseudo) is not False:
        gaps.extend(missing_fields(state, _D53_PSEUDO_FIELDS))
    if _infection_excluded(state) is not True:
        gaps.append(
            "感染未排除（N.INFECTION_CTX / CL.INFECTION）：按 D4.1 并行评估，不自动取消任一疑似标签"
        )
    gaps.extend(missing_fields(state, _D53_SHARED_FIELDS))
    return tuple(dict.fromkeys(gaps))


_D53_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D5.3.R04",
        priority="P1",
        source_row="D5.3!15",
        statement="炎症影像与感染证据并存/冲突或感染未排除 → 保留「炎症性AAA疑似」与「感染疑似」多标签，进入 D4.1 并行 MDT",
        judge=_d53_r04,
        exit_kind="terminal",
        exit_value="炎症与感染双标签并行评估（D4.1 感染分诊 + MDT；感染评估完成前不取消任一疑似标签）",
    ),
    RuleSlot(
        rule_id="D5.3.R09",
        priority="P1",
        source_row="D5.3!20",
        statement="炎症性 AAA 低于常规修复阈值但有严重/持续疼痛、体重下降或明显肾积水且影像有 mantle sign → MDT 评估抗炎治疗路径（先排除感染）",
        judge=_d53_r09,
        exit_kind="terminal",
        exit_value="炎症性 AAA（低于修复阈值但症状明显）：MDT 评估抗炎治疗路径，药物种类与剂量由医生决定",
    ),
    RuleSlot(
        rule_id="D5.3.R03",
        priority="P1",
        source_row="D5.3!14",
        statement="炎症性 AAA 确认/高度提示/可疑 → 先鉴别感染，真实瘤径排除瘤周炎症与壁水肿后由 MDT 决定修复或抗炎路径",
        judge=_d53_r03,
        exit_kind="terminal",
        exit_value="炎症性 AAA 专科评估：先鉴别感染；真实瘤径按 ESVS Rec 145 测量，修复或抗炎由 MDT 决定",
    ),
    RuleSlot(
        rule_id="D5.3.R02",
        priority="P1",
        source_row="D5.3!13",
        statement="假性动脉瘤明确或疑似 → 查明创伤/医源性/术后/感染/瘘相关病因，不套用退变性 AAA 监测阈值",
        judge=_d53_r02,
        exit_kind="terminal",
        exit_value="假性动脉瘤专科修复评估：先查明病因与壁连续性（感染/瘘疑似并行 D4.1）",
        next_nodes=("D4.1", "D4.3", "G03", "G05"),
    ),
    RuleSlot(
        rule_id="D5.3.R01",
        priority="P1",
        source_row="D5.3!12",
        statement="囊状真性瘤或 PAU → 单独记录形态/部位/大小/周围血肿/症状与感染线索，不套用梭形 AAA 的单一阈值",
        judge=_d53_r01,
        exit_kind="terminal",
        exit_value="特殊形态（囊状真性瘤/PAU）专科修复评估：按病因、进展、解剖与患者风险由团队决定",
    ),
    RuleSlot(
        rule_id="D5.3.R90",
        priority="P2",
        source_row="D5.3!11",
        statement="特殊形态/炎症影像/真实瘤径/感染鉴别任一关键项未评价或冲突 → 不套用普通 AAA 阈值，转影像/专科复核",
        judge=_d53_r90,
        exit_kind="terminal",
        exit_value="人工复核（特殊形态、炎症影像、真实瘤径或感染鉴别待核查）",
    ),
    RuleSlot(
        rule_id="D5.3.R91",
        priority="P3",
        source_row="D5.3!10",
        statement="无特殊病变证据且普通 AAA 确认 → 回 D1.1 走常规路径；感染疑似并行 D4.1",
        judge=_d53_r91,
        exit_kind="next",
        exit_value="D1.1",
        next_nodes=("D4.1", "D5.3", "D1.1"),
    ),
)


def d5_3(state: GuidelineState) -> NodeOutcome:
    """D5.3：按病因口径给出专科评估出口，并列出未分离的真实瘤径等缺口。"""
    outcome = evaluate_node("D5.3", state, _D53_SLOTS)
    gaps = _d53_gaps(state)
    if gaps:
        outcome = replace(outcome, gaps=gaps)
    return outcome


#: 本模块提供的节点（键与规格 sheet 名一致）。
NODES: dict[str, NodeFn] = {
    "D5.1": d5_1,
    "D5.2": d5_2,
    "D5.3": d5_3,
}

#: 已实现的规则 → 规格源行。
RULE_ROWS: dict[str, str] = {
    **{slot.rule_id: slot.source_row for slot in _D51_SLOTS},
    **{rule.rule_id: rule.source_row for rule in _D51_DISPLAY_RULES},
    **{slot.rule_id: slot.source_row for slot in _D52_SLOTS},
    **{rule.rule_id: rule.source_row for rule in _D52_DISPLAY_RULES},
    **{slot.rule_id: slot.source_row for slot in _D53_SLOTS},
}
