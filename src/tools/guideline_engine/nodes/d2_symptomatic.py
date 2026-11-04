"""D2 · 有症状 AAA（``D2.1`` → ``D2.2`` → ``D2.3``）。

规格来源：``VascGuide_V3.6.2.xlsx`` 的 ``D2.1`` / ``D2.2`` / ``D2.3`` 三个 sheet，
共 24 条规则（8 + 8 + 8），全部实现。

## 三个节点各管什么

``D2.1`` 症状确认与破裂排除
    回答「这个症状是不是 AAA 的、瘤子破了没有」。三条 P0-A 各守一件事：破裂证据
    （→G03 并进 D3.1）、腹痛但归因不明（→G03 并进 D2.2）、压迫/栓塞疑似（→D2.2）。

``D2.2`` 紧急程度与快速评估
    回答「能不能等」。P0-A 判不稳定/持续恶化/判不了，P1 判「破裂已排除 + 归因相关
    + 临床稳定」→ D2.3 做术式比较。规格反复强调**不因等待优化而不安全延误**。

``D2.3`` 术式候选
    回答「EVAR 还是 OSR」。与 ``D1.3`` / ``D3.2`` 同构，差别在时效：这是紧急场景，
    所以**不要求**择期才核验的远端密封区；IFU 未核验只降为「待核验候选」，
    不阻断方向（``D2.3.R06``）。

## 三处词表陷阱（照规格字面写会得到死分支）

``S.PAIN_ATTRIB``
    规格 F 列：医师确认相关 / 医师怀疑相关 / 明确归因其他原因 / 未评价 / 证据冲突
    模型 ``s.pain_attrib``：明确相关 / 怀疑相关 / 明确其他原因 / 未判断
    两套字面量不同。照规格写 ``eq(x, "医师确认相关")`` 恒为 ``False``——不是
    「判不出」，是「永远判成不相关」，方向最坏的那种错。

``S.AAA_TENDER``
    规格写「明确阴性」，模型写「阴性」。同样按模型。

``S.LIMB``
    规格是单值枚举（是/否/疑似/未评估/证据冲突），模型是**多选列表**
    （疼痛/苍白/发凉/麻木/无力/运动障碍/其他/无/未知）。故用 ``has()`` 看列表里有没有
    症状项，不能用 ``eq``。

## 为什么 D2 的破裂证据回 G03 而不是直接进 G05

``G05`` 是「急诊模式」出口，``G03`` 是「急诊危险规则」入口。D2 的病例已被 ``G08``
判为「有症状**未破裂**」，此时新发现破裂证据的处置是**回判定层重判**——重判会带上
完整的急诊证据链（外溢/血肿/血红蛋白下降/NEWS2…），直接跳 ``G05`` 会绕过它。
``D3`` 才用 ``G05``，因为那里破裂已经是**确定结论**而非**新证据**。

例外是 ``D2.2.R04``（持续恶化）：那时病人已经在快速评估通道里，再回判定层重判
等于把最急的一类往后排，故规格写「回 G03/**G05**」——本实现取 ``G05``。

## 为什么 D2.3 不要求远端密封区

``D1.3.R05`` 要求「近**远端**密封区/入路全部参数符合 IFU」才能标「标准 EVAR 候选」，
那是择期场景：等得起，可以等远端测量。D2.3 是紧急场景，规格 ``R90`` 明写
「急诊不等待非关键资料」——用择期的完整度要求去卡急诊，等于把病人卡死在评估里。
"""

from __future__ import annotations

from dataclasses import replace

from ....rules.router import (
    Verdict,
    and_,
    eq,
    has,
    not_,
    or_,
    token,
)
from ..state import (
    Candidate,
    CandidateStatus,
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
    branch_involved,
    cta_evaluable,
    display_entries,
    evaluate_node,
    limb_ischemia_present,
    missing_fields,
    multi_field,
    spec_token,
)

__all__ = ["NODES", "d2_1", "d2_2", "d2_3"]

#: 模型 ``s.pain_attrib`` 里「归因于 AAA」的两个取值（规格写法见模块文档）。
_ATTRIB_RELATED: tuple[str, ...] = ("明确相关", "怀疑相关")

#: 模型 ``s.pain_attrib`` 里「明确归因其他原因」的取值。
_ATTRIB_OTHER: str = "明确其他原因"

#: 模型 ``s.pain_attrib`` 里「还没判」的取值（**不是** None，也算缺失）。
_ATTRIB_UNJUDGED: str = "未判断"

#: ``D2.COMPRESSION`` 里属于「有压迫表现」的取值（``无`` / ``未知`` 不在其中）。
_COMPRESSION_SIGNS: tuple[str, ...] = (
    "输尿管梗阻",
    "肾积水",
    "肠道压迫",
    "静脉回流障碍",
    "神经压迫",
)


# ---------------------------------------------------------------------------
# D2.1 症状确认与破裂排除
# ---------------------------------------------------------------------------
_D21_FIELDS: tuple[tuple[str, str], ...] = (
    ("D2.SYMPTOM_TIMELINE", "症状时间线（起病方式/部位/持续时间/NRS/放射/镇痛后变化）"),
    ("D2.ALT_DIAGNOSIS", "替代病因及评估状态（已确认其他原因/正在评估/未评估/未发现）"),
    ("D2.COMPRESSION", "局部压迫表现（输尿管梗阻/肾积水/肠道压迫/静脉回流障碍/神经压迫）"),
    ("D2.EMBOLIC_SOURCE", "远端栓塞是否来自 AAA（医师认为相关/怀疑相关/其他来源明确/未评价）"),
)


def _pain_present(state: GuidelineState) -> object:
    return token(state.case.s.pain_present)


def _attrib(state: GuidelineState) -> object:
    return token(state.case.s.pain_attrib)


def _tender(state: GuidelineState) -> object:
    return token(state.case.s.aaa_tender)


def _limb_ischemia(state: GuidelineState) -> Verdict:
    """急性肢体缺血/外周栓塞表现（规格 ``S.LIMB``）。

    状态优先、体征明细兜底，判据本体在 ``common.limb_ischemia_present``；
    ``S.LIMB`` 五态里把「未评估」单列出来，正是为了防「没查 = 没有」。
    """
    return limb_ischemia_present(state)


def _wall_breach(state: GuidelineState) -> Verdict:
    """主动脉壁破裂/壁外出血证据（替代规格的 ``D2.WALL_BREACH``）。

    规格要求「综合 ``C.EXTRAV`` / ``C.HEMATOMA`` / ``C.WALL`` / ``C.CONTAIN`` 及
    报告；必须区分主动脉周围血肿与单纯壁内血肿」。四个模型字段都是四态
    （明确/可疑/未见/不可评价），故取最强者：任一「明确」→ True；否则任一
    「可疑」或「不可评价」→ None；全部「未见」→ False。

    为什么不把「可疑」判成 True：那会让每个影像有轻微脂肪间隙模糊的病例都进急诊
    破裂路径；而判成 False 更糟——「可疑」不成立破裂排除（``R91`` 会因此放行）。
    """
    grades = [
        token(state.case.c.extrav),
        token(state.case.c.hematoma),
        token(state.case.c.wall),
        token(state.case.c.contain),
    ]
    present = [g for g in grades if g is not None]
    if not present:
        return None
    if "明确" in present:
        return True
    if "可疑" in present:
        return None
    if all(g == "未见" for g in present):
        return False
    return None


def _rupture_excluded(state: GuidelineState) -> Verdict:
    """破裂是否已被**充分**排除。

    只有「未见相关证据」**且**没有壁外出血证据才算排除。规格 J 列明写
    「不能因无 CTA 或正常血压直接排除」，所以「无法评价」与缺失一律不算。
    """
    rupture = token(state.case.cl.aaa_rupture)
    if rupture == "未见相关证据":
        # 「未见相关证据」本身已含「影像与临床可评价」这个前提（规格 G 列），
        # 所以只有**明确**的壁外出血证据才能推翻它；壁外出血未评估不推翻。
        return _wall_breach(state) is not True
    if rupture in ("明确", "疑似", "无法评价"):
        return False
    return None


def _aaa_symptom_present(state: GuidelineState) -> Verdict:
    """当前是否有 AAA 相关症状 —— 规格 R01 列的三项：疼痛 / 触痛 / 肢体缺血。

    「触痛不确定」记 UNKNOWN 而非 True：规格明写「触痛本身也不能单独确诊
    症状性 AAA」，把它算成确认症状会让 ``R01`` 在只有一次含糊查体时放行。
    """
    return or_(
        eq(_pain_present(state), "是", "可疑"),
        eq(_tender(state), "明确阳性"),
        _limb_ischemia(state),
    )


def _suspected_symptom(state: GuidelineState) -> Verdict:
    """症状**疑似**（而非确认）—— 规格 ``R02`` 的「但有疑似症状」。"""
    return or_(
        eq(_pain_present(state), "可疑"),
        eq(_tender(state), "不确定"),
    )


def _attributed(state: GuidelineState) -> Verdict:
    """医生是否把症状归因于 AAA（确认或怀疑相关）。"""
    return eq(_attrib(state), *_ATTRIB_RELATED)


def _attrib_unjudged(state: GuidelineState) -> bool:
    """归因是否「还没有结论」—— 缺失或模型侧 ``未判断``。

    两者的区别只在于写法：``None`` 是没录，``未判断`` 是录了「医生没判」。
    对下游是同一件事：**不得当作无关**。
    """
    value = _attrib(state)
    return value is None or value == _ATTRIB_UNJUDGED


def _below_55(state: GuidelineState) -> Verdict:
    """直径是否 <55 mm（规格 ``R02`` 的阈值，原文不分性别）。

    规格只给了 55 这一个数，故不套 ``D1.1`` 的性别分档——那会把女性 52 mm 的
    症状性 AAA 判成「已达阈值」，与规格这条规则的原意（「小瘤子也不能排除症状」）相反。
    """
    diam = aaa_diameter(state)
    return None if diam is None else diam < 55.0


def _d21_r03(state: GuidelineState) -> Verdict:
    """P0-A：破裂明确/疑似/无法评价，或存在壁外出血证据 → 回 G03 并进 D3.1。"""
    return or_(
        eq(token(state.case.cl.aaa_rupture), "明确", "疑似", "无法评价"),
        _wall_breach(state),
    )


def _d21_r05(state: GuidelineState) -> Verdict:
    """P0-A：当前腹痛但医生未确认归因 → 保持紧急鉴别，不进入无症状路径。

    窄口径（只判「疼痛=是」）：更宽的症状集合归 ``R04`` 管。分两条的理由是出口
    不同——本规则继续去 ``D2.2`` 做快速评估，``R04`` 要停在人工复核。
    """
    return and_(
        eq(_pain_present(state), "是"),
        True if _attrib_unjudged(state) else None,
    )


def _d21_r06(state: GuidelineState) -> Verdict:
    """P0-A：压迫症状或远端栓塞与 AAA 可能相关 → 由医生评估是否属症状性/复杂 AAA。

    「急性肢体缺血优先回 G03」这一步不在这里判：急性缺血是判定层 ``G03.R09`` 的
    职责，能走进 ``D2`` 说明它没触发。在节点里再判一次等于把那条规则抄一遍。
    """
    return or_(
        has(multi_field(state, "D2.COMPRESSION"), *_COMPRESSION_SIGNS),
        eq(
            spec_token(state.node_field("D2.EMBOLIC_SOURCE")),
            "医师认为相关",
            "怀疑相关",
        ),
    )


def _d21_r02(state: GuidelineState) -> Verdict:
    """P1：直径 <55 mm 但有疑似症状 → 紧急检查证实症状诊断，记录替代诊断。

    规格原文：「直径<55 mm 不排除症状性 AAA；ESVS 建议紧急检查以证实症状诊断，
    必须记录替代诊断及归因证据」。所以出口是 ``D2.2``（继续查），不是「排除」。
    """
    return and_(_suspected_symptom(state), _below_55(state))


def _d21_r04(state: GuidelineState) -> Verdict:
    """P2：症状存在但 AAA 归因未评价/冲突 → 不得自动判为无症状 AAA。

    只判「有症状 + 归因未判」。「无疼痛」的病例归因本来就是「未判断」（没症状
    何来归因），若一并阻断会让所有非疼痛病例都停在人工复核——故以
    :func:`_aaa_symptom_present` 为前提。
    """
    if _attrib_unjudged(state) and _aaa_symptom_present(state) is True:
        return True
    return False if _aaa_symptom_present(state) is False else None


def _d21_r90(state: GuidelineState) -> Verdict:
    """P2：当前症状、归因或破裂状态未知/冲突 → 不得进入确定无症状 D1。"""
    return any(
        (
            _pain_present(state) is None,
            _attrib_unjudged(state) and _aaa_symptom_present(state) is True,
            token(state.case.cl.aaa_rupture) is None,
        )
    )


def _d21_r01(state: GuidelineState) -> Verdict:
    """P3：AAA 已确认 + 有症状 + 归因相关 + 无壁破裂证据 → D2.2。"""
    return and_(
        eq(token(state.case.cl.aaa_reported), "确认"),
        _aaa_symptom_present(state),
        _attributed(state),
        not_(_wall_breach(state)),
    )


def _d21_r91(state: GuidelineState) -> Verdict:
    """P3 出口：症状不成立或已归因他因，且破裂状态已充分评估 → 回无症状路径。

    用 ``next=D1.1`` 而不是 ``terminal``：规格 M 列写的是「D1.1、D2.2 或 G03/D3.1」，
    说明这条出口是**真跳转**。另两支已被 ``R03``（→G03/D3.1）与 ``R01``（→D2.2）
    接走，能走到这里只剩「症状消失」这一支。
    """
    return and_(
        _rupture_excluded(state),
        or_(
            eq(_pain_present(state), "否"),
            eq(_attrib(state), _ATTRIB_OTHER),
        ),
    )


_D21_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D2.1.R03",
        priority="P0-A",
        source_row="D2.1!12",
        statement="破裂明确/疑似、CTA 可疑或无法评价且临床不能排除 → 不走症状性未破裂路径，回 G03 并进 D3.1",
        judge=_d21_r03,
        exit_kind="escalate",
        exit_value="G03",
        also_next="D3.1",
        next_nodes=("G03", "D3.1"),
        link=Link.FOLLOW_UP,
    ),
    RuleSlot(
        rule_id="D2.1.R05",
        priority="P0-A",
        source_row="D2.1!18",
        statement="已知/疑似 AAA 且当前腹痛但医生未确认归因 → 保持紧急鉴别评估，不进入确定无症状路径",
        judge=_d21_r05,
        exit_kind="escalate",
        exit_value="G03",
        also_next="D2.2",
        next_nodes=("D2.2", "G03", "D3.1"),
        link=Link.EITHER,
    ),
    RuleSlot(
        rule_id="D2.1.R06",
        priority="P0-A",
        source_row="D2.1!19",
        statement="压迫症状或远端栓塞与 AAA 可能相关 → 由医生评估是否属症状性/复杂 AAA",
        judge=_d21_r06,
        exit_kind="next",
        exit_value="D2.2",
        next_nodes=("D2.2", "D5.3", "G03"),
    ),
    RuleSlot(
        rule_id="D2.1.R02",
        priority="P1",
        source_row="D2.1!11",
        statement="直径 <55 mm 但有疑似症状 → 紧急检查证实症状诊断，必须记录替代诊断与归因证据",
        judge=_d21_r02,
        exit_kind="next",
        exit_value="D2.2",
    ),
    RuleSlot(
        rule_id="D2.1.R04",
        priority="P2",
        source_row="D2.1!13",
        statement="症状存在但 AAA 归因未评价/冲突 → 不得自动判为无症状 AAA，保持紧急评估并由医生确认归因",
        judge=_d21_r04,
        exit_kind="terminal",
        exit_value="人工复核（症状归因未确认，不得进入无症状路径）",
    ),
    RuleSlot(
        rule_id="D2.1.R90",
        priority="P2",
        source_row="D2.1!9",
        statement="当前症状、症状归因或破裂状态未知/冲突 → 不得进入确定无症状 D1；紧急评估并补充信息",
        judge=_d21_r90,
        exit_kind="terminal",
        exit_value="人工复核（关键症状或破裂状态待核查）",
    ),
    RuleSlot(
        rule_id="D2.1.R01",
        priority="P3",
        source_row="D2.1!10",
        statement="AAA 已确认 + 当前疼痛/触痛/外周栓塞 + 归因相关 + 无壁破裂证据 → D2.2",
        judge=_d21_r01,
        exit_kind="next",
        exit_value="D2.2",
    ),
    RuleSlot(
        rule_id="D2.1.R91",
        priority="P3",
        source_row="D2.1!8",
        statement="无当前症状或已明确归因其他原因且破裂已充分评估 → 回 G08/D1.1 无症状路径",
        judge=_d21_r91,
        exit_kind="next",
        exit_value="D1.1",
        next_nodes=("D1.1", "D2.2", "G03", "D3.1"),
    ),
)


def _d21_gaps(state: GuidelineState) -> tuple[str, ...]:
    """本节点缺什么 —— 症状侧四项 + 归因状态。

    归因单列：它缺的时候 ``R04``/``R05`` 都判不了，而这两个字段的取值在模型里
    有个专门表示「录了但没判」的 ``未判断``——缺口里要说清是哪一种。
    """
    gaps = list(missing_fields(state, _D21_FIELDS))
    if _attrib_unjudged(state):
        gaps.append("S.PAIN_ATTRIB（症状归因）：医生尚未判断症状是否与 AAA 相关")
    if _wall_breach(state) is None:
        gaps.append("壁外出血证据未评估（C.EXTRAV / C.HEMATOMA / C.WALL / C.CONTAIN）")
    return tuple(gaps)


def d2_1(state: GuidelineState) -> NodeOutcome:
    """D2.1：确认症状归因、排除破裂，决定去 D2.2 还是回无症状路径。"""
    outcome = evaluate_node("D2.1", state, _D21_SLOTS)
    gaps = _d21_gaps(state)
    if gaps:
        outcome = replace(outcome, gaps=gaps)
    return outcome


# ---------------------------------------------------------------------------
# D2.2 紧急程度与快速评估
# ---------------------------------------------------------------------------
_D22_FIELDS: tuple[tuple[str, str], ...] = (
    ("D2.VITAL_TREND", "连续生命体征与灌注趋势（逐次 SBP/HR/意识/灌注/氧合 + 时间）"),
    ("D2.OPT_STATUS", "快速评估/优化状态（未开始/进行中/完成/因紧急程度不能等待）"),
    ("D2.PREOP_READINESS", "快速准备清单（血管团队/麻醉/血库/CTA/手术室/ICU 逐项状态）"),
    ("D2.URGENCY_DECISION", "授权医生确认的紧急程度（急诊/紧急/择期/继续诊断/不确定）"),
    ("D2.CLINICAL_STABILITY", "临床稳定性（稳定/不稳定/不确定）"),
)


def _stability(state: GuidelineState) -> object:
    return spec_token(state.node_field("D2.CLINICAL_STABILITY"))


def _clinically_unstable(state: GuidelineState) -> Verdict:
    """当前不稳定 —— 只读医生的稳定性结论，不自己拿单次血压判。

    规格 ``V.SBP`` 的 G 列明写「不得将单一阈值用于确诊 AAA 破裂」，
    ``D2.CLINICAL_STABILITY`` 的 G 列又要求「不能只用单次生命体征」。
    所以稳定性只能来自授权临床人员的综合判断；缺了就是判不了，不是稳定。
    """
    return eq(_stability(state), "不稳定")


def _stable(state: GuidelineState) -> Verdict:
    return eq(_stability(state), "稳定")


def _deteriorating(state: GuidelineState) -> Verdict:
    """生命体征趋势是否持续恶化（``D2.VITAL_TREND`` 的趋势结论）。"""
    return eq(spec_token(state.node_field("D2.VITAL_TREND")), "恶化")


def _urgency_decided(state: GuidelineState) -> Verdict:
    """授权医生是否已给出时效级别（急诊/紧急/择期/继续诊断）。

    「不确定」不算已定：规格 ``D2.URGENCY_DECISION`` 的 J 列写「未确认→保留待确认」。
    """
    return eq(
        spec_token(state.node_field("D2.URGENCY_DECISION")),
        "急诊",
        "紧急",
        "择期",
        "继续诊断",
    )


def _d22_r04(state: GuidelineState) -> Verdict:
    """P0-A：持续恶化或不稳定 → 回 G05（急诊），不等完整常规术前清单。

    为什么这里用 ``G05`` 而不是 ``G03``：病人**已经在快速评估通道里**，再回判定层
    重判等于把最急的一类往后排。规格 M 列写「G03/G05、D3.1、D3.2」，取最急的一支。
    """
    return or_(_deteriorating(state), _clinically_unstable(state))


def _d22_r01(state: GuidelineState) -> Verdict:
    """P0-A：不稳定、破裂新证据或不能排除破裂 → 回 G03 并进 D3.1。"""
    return or_(
        _clinically_unstable(state),
        eq(token(state.case.cl.aaa_rupture), "明确", "疑似", "无法评价"),
        _wall_breach(state),
    )


def _d22_r06(state: GuidelineState) -> Verdict:
    """P0-A：CTA 不可评价或临床状态**冲突** → 不能自动认定稳定或未破裂。

    规格原文三项：CTA 不可评价、生命体征趋势缺失、临床状态冲突。本实现只取
    首末两项，「趋势缺失」交给 ``R90``（P2 数据阻断）：

    · 趋势是连续监护数据，本系统拿到的通常是单次快照，据它阻断会让所有无监护
      记录的病例都停在人工复核——而这恰恰是院前/门诊转来的常见状态；
    · 「稳定性未评价」同理归 ``R90``：那是**资料缺失**，不是**状态冲突**。
      把它算进 P0-A 会把「还没评」和「评了说冲突」混成同一档，而后者才需要
      立刻按急症处理。
    """
    return any(
        (
            cta_evaluable(state) is False,
            eq(_stability(state), "不确定") is True,
        )
    )


def _d22_r02(state: GuidelineState) -> Verdict:
    """P1：破裂已排除 + 归因相关 + 临床稳定 → 短时间快速评估优化后紧急修复。

    出口 ``D2.3`` 而不是「直接手术」：规格 G 列要求「理想情况下在最佳团队/工作
    时段完成，但不因等待而不安全延误」——术式与时机都还要在 D2.3 比较。
    """
    return and_(
        _rupture_excluded(state),
        _attributed(state) if _attrib_known(state) else None,
        _stable(state),
    )


def _trend_not_worse(state: GuidelineState) -> Verdict:
    """生命体征趋势已记录且未恶化 —— ``R05`` 里「可快速评估」的客观锚点。"""
    trend = spec_token(state.node_field("D2.VITAL_TREND"))
    if trend is None:
        return None
    return trend != "恶化"


def _d22_r05(state: GuidelineState) -> Verdict:
    """P1：破裂已排除 + 归因相关 + 趋势未恶化 + 已定紧急度 → D2.3。

    与 ``R02`` 的分野在**判据来源**而非严格程度：``R02`` 读医生的综合结论
    （``D2.CLINICAL_STABILITY``），本规则读客观趋势（``D2.VITAL_TREND``）与
    已定的时效级别。所以「稳定性还没评、但趋势已记录且没恶化、团队已定紧急」
    这组状态由本规则接住——``R02`` 在那里判不了，而病例其实可以往下走。
    """
    return and_(
        _rupture_excluded(state),
        _attributed(state) if _attrib_known(state) else None,
        _trend_not_worse(state),
        _urgency_decided(state),
    )


def _attrib_known(state: GuidelineState) -> bool:
    """归因是否已由医生判过（``未判断`` 与缺失都算没判）。"""
    value = _attrib(state)
    return value is not None and value != _ATTRIB_UNJUDGED


def _d22_r90(state: GuidelineState) -> Verdict:
    """P2：稳定性或破裂排除状态未知 → 不自动标记稳定/未破裂，保留紧急复核。"""
    return any(
        (
            _stability(state) is None,
            _rupture_excluded(state) is None,
        )
    )


def _d22_r03(state: GuidelineState) -> Verdict:
    """P3：症状来源未证实 → 继续紧急诊断与替代病因评估，不得降为 D1 监测。"""
    return and_(
        _rupture_excluded(state),
        eq(_attrib(state), _ATTRIB_OTHER),
    )


def _d22_r91(state: GuidelineState) -> Verdict:
    """P3 出口：破裂已排除、患者稳定且症状性 AAA 确认 → D2.3。

    恒 ``True``，但**实际不可达**：``R90`` 在 P2 已经拦住「稳定性或破裂排除未知」，
    而稳定性一旦有值（稳定/不稳定/不确定）就必然被 ``R02`` / ``R04`` / ``R06``
    接走。保留它是为了「新增判据时不会漏掉兜底」——与 ``evaluate_node`` 内部的
    人工复核兜底同性质。规格 G 列的这句出口本身是完整的，缺的是可达路径。
    """
    return True


_D22_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D2.2.R04",
        priority="P0-A",
        source_row="D2.2!15",
        statement="循环恶化、新意识变化、破裂证据或无法安全等待 → 回 G05 并快速评估急诊修复，不等完整清单",
        judge=_d22_r04,
        exit_kind="escalate",
        exit_value="G05",
        also_next="D3.2",
        next_nodes=("G03", "G05", "D3.1", "D3.2"),
        link=Link.FOLLOW_UP,
    ),
    RuleSlot(
        rule_id="D2.2.R01",
        priority="P0-A",
        source_row="D2.2!9",
        statement="不稳定、破裂新证据或不能排除破裂 → 返回 G03 并进 D3.1，不等待常规术前优化",
        judge=_d22_r01,
        exit_kind="escalate",
        exit_value="G03",
        also_next="D3.1",
        next_nodes=("G03", "D3.1"),
        link=Link.FOLLOW_UP,
    ),
    RuleSlot(
        rule_id="D2.2.R06",
        priority="P0-A",
        source_row="D2.2!17",
        statement="CTA 不可评价或临床稳定性判不了 → 不能自动认定稳定或未破裂，保留紧急复核",
        judge=_d22_r06,
        exit_kind="terminal",
        exit_value="人工复核（稳定性或破裂排除状态不可判定）",
    ),
    RuleSlot(
        rule_id="D2.2.R02",
        priority="P1",
        source_row="D2.2!10",
        statement="破裂已排除 + 症状性 AAA 确认/高度怀疑 + 可监护评估 → 短时间快速评估优化后紧急修复",
        judge=_d22_r02,
        exit_kind="next",
        exit_value="D2.3",
    ),
    RuleSlot(
        rule_id="D2.2.R05",
        priority="P1",
        source_row="D2.2!16",
        statement="破裂已排除 + 症状与 AAA 相关 + 已定紧急程度 → 快速评估/必要优化后紧急修复",
        judge=_d22_r05,
        exit_kind="next",
        exit_value="D2.3",
    ),
    RuleSlot(
        rule_id="D2.2.R90",
        priority="P2",
        source_row="D2.2!8",
        statement="稳定性趋势或破裂排除状态未知/冲突 → 不自动标记稳定或未破裂，保留紧急复核",
        judge=_d22_r90,
        exit_kind="terminal",
        exit_value="人工复核（稳定性趋势或破裂排除待核查）",
    ),
    RuleSlot(
        rule_id="D2.2.R03",
        priority="P3",
        source_row="D2.2!11",
        statement="未证实症状来源 → 继续紧急诊断与替代病因评估，不得自动降为 D1 无症状监测",
        judge=_d22_r03,
        exit_kind="escalate",
        exit_value="G03",
        next_nodes=("G03",),
        link=Link.EITHER,
    ),
    RuleSlot(
        rule_id="D2.2.R91",
        priority="P3",
        source_row="D2.2!7",
        statement="破裂已排除、患者稳定且症状性 AAA 确认/高度怀疑 → 快速评估后紧急修复候选，进入 D2.3",
        judge=_d22_r91,
        exit_kind="next",
        exit_value="D2.3",
    ),
)


def _d22_gaps(state: GuidelineState) -> tuple[str, ...]:
    """本节点缺什么 —— 稳定性与时效是两条主线，其余是快速准备。"""
    gaps = list(missing_fields(state, _D22_FIELDS))
    if _rupture_excluded(state) is not True:
        gaps.append("破裂未充分排除（CL.AAA_RUPTURE 或壁外出血证据不可评价）")
    return tuple(gaps)


def d2_2(state: GuidelineState) -> NodeOutcome:
    """D2.2：判断能不能等，能等的去 D2.3 做术式比较。"""
    outcome = evaluate_node("D2.2", state, _D22_SLOTS)
    gaps = _d22_gaps(state)
    if gaps:
        outcome = replace(outcome, gaps=gaps)
    return outcome


# ---------------------------------------------------------------------------
# D2.3 术式候选（EVAR / OSR 两列）
# ---------------------------------------------------------------------------
_D23_FIELDS: tuple[tuple[str, str], ...] = (
    ("D2.RISK", "当前手术风险（可接受/高风险/不确定）"),
    ("D2.PREF", "患者偏好与知情沟通（EVAR倾向/OSR倾向/未表达/无法确认）"),
    ("D3.TEAM_RESOURCE", "当次团队与资源（EVAR/OSR 团队、器械、DSA、血库、麻醉、ICU）"),
)


def _risk(state: GuidelineState) -> object:
    return spec_token(state.node_field("D2.RISK"))


def _pref(state: GuidelineState) -> object:
    return spec_token(state.node_field("D2.PREF"))


def _team_absent(state: GuidelineState) -> Verdict:
    """本中心当次是否无法实施（``D3.TEAM_RESOURCE`` 的明确结论）。"""
    return eq(spec_token(state.node_field("D3.TEAM_RESOURCE")), "均不可立即用")


def _ifu(state: GuidelineState) -> object:
    """``N.EVAR_IFU_STATUS`` —— **整体** EVAR 器械适配（不是近端密封区）。

    ``D2.3`` 全部 7 条规则的「允许值」列都点名这个字段。用它而不是
    ``n.seal_ifu``：后者规格明写「只代表近端密封项，不代表整体 EVAR 适配」，
    拿近端合格冒充整体合格，会让候选栏写出「IFU 符合」而实际只核了密封区
    （``D2.3.R06`` 明令禁止）。
    """
    return token(state.case.n.evar_ifu_status)


def _d23_r01(state: GuidelineState) -> Verdict:
    """P3：解剖可评价且指定 EVAR 方案符合 IFU → 列为 EVAR 候选。"""
    return and_(
        cta_evaluable(state),
        anatomy_available(state),
        not_(access_blocked(state)),
        not_(branch_involved(state)),
        eq(_ifu(state), "符合"),
    )


def _d23_r02(state: GuidelineState) -> Verdict:
    """P3：EVAR 不适配但开放修复风险可接受 → 列为 OSR 候选。"""
    return and_(
        or_(
            access_blocked(state),
            eq(_ifu(state), "不符合"),
            branch_involved(state),
        ),
        eq(_risk(state), "可接受"),
    )


def _d23_r03(state: GuidelineState) -> Verdict:
    """P3：短颈/近肾/重要分支受累 → 复杂解剖，转 G06。"""
    return branch_involved(state)


def _d23_r04(state: GuidelineState) -> Verdict:
    """P3：EVAR 与 OSR 必须分别记录可行性/IFU/风险/及时性/耐久性/意愿（展示义务）。"""
    return True


def _d23_r05(state: GuidelineState) -> Verdict:
    """P3：标准 EVAR 不适配或本中心资源不具备 → 复杂团队比较或转诊。

    与 ``R03`` 的分野：``R03`` 判**解剖**复杂（分支受累），本规则判**适配/资源**
    不具备（IFU 不符、入路阻断、团队不可用）。
    """
    return or_(
        eq(_ifu(state), "不符合"),
        access_blocked(state),
        _team_absent(state),
    )


def _d23_r06(state: GuidelineState) -> Verdict:
    """P3：IFU 未核验/未选择型号 → 只标「待核验候选」，不得输出「符合 IFU」。"""
    return eq(_ifu(state), "未核验", "未选择型号")


_D23_DISPLAY_RULES: tuple[DisplayRule, ...] = (
    DisplayRule(
        rule_id="D2.3.R01",
        source_row="D2.3!9",
        statement="解剖可评价且指定 EVAR 方案符合 IFU → 列为 EVAR 候选（结合风险/耐久性/意愿比较）",
        judge=_d23_r01,
    ),
    DisplayRule(
        rule_id="D2.3.R02",
        source_row="D2.3!10",
        statement="EVAR 不适配且开放修复风险可接受 → 列为 OSR 候选；两种方式均有重大限制 → 专家/MDT",
        judge=_d23_r02,
    ),
    DisplayRule(
        rule_id="D2.3.R04",
        source_row="D2.3!13",
        statement="分别记录 EVAR 与 OSR 的可行性、IFU 状态、手术风险、及时性、耐久性与患者意愿",
        judge=_d23_r04,
    ),
    DisplayRule(
        rule_id="D2.3.R06",
        source_row="D2.3!15",
        statement="IFU 缺失/地区不适用/型号未指定 → 只标记「待核验候选」，不得输出「符合 IFU」",
        judge=_d23_r06,
    ),
)


def _d23_r90(state: GuidelineState) -> Verdict:
    """P2：关键解剖、IFU 或患者风险缺失 → 不批准具体术式；**急诊不等待非关键资料**。

    与 ``D1.3.R90`` 同一结构、不同尺度：这里只判「连方向都给不出」的三项
    （解剖全缺 / CTA 缺失 / IFU 完全没录）。团队资源与服务能力**不**参与阻断——
    规格 ``D3.TEAM_RESOURCE`` 的 J 列明写「未确认不等于不可用；资源缺口不得自动
    延误急症安全响应」。
    """
    return any(
        (
            anatomy_available(state) is False,
            cta_evaluable(state) is False,
            _ifu(state) is None,
        )
    )


def _d23_r91(state: GuidelineState) -> Verdict:
    """P3 出口：两列候选已列出；均不适配或本中心无法及时实施 → MDT/转诊/救援。"""
    return True


_D23_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D2.3.R90",
        priority="P2",
        source_row="D2.3!8",
        statement="关键解剖、IFU 或患者状态缺失/冲突 → 不批准具体术式；急诊不等待非关键资料",
        judge=_d23_r90,
        exit_kind="terminal",
        exit_value="人工复核（草案：关键解剖或 IFU 资料缺失）",
    ),
    RuleSlot(
        rule_id="D2.3.R03",
        priority="P3",
        source_row="D2.3!11",
        statement="短颈/近肾/重要分支受累 → 复杂解剖，转 G06 比较复杂开放修复与适用腔内技术",
        judge=_d23_r03,
        exit_kind="escalate",
        exit_value="G06",
        next_nodes=("G06",),
        link=Link.EITHER,
    ),
    RuleSlot(
        rule_id="D2.3.R05",
        priority="P3",
        source_row="D2.3!14",
        statement="标准器械不符合 IFU 或本中心不能实施 → 复杂团队比较或提示转诊/协作，急诊处置优先",
        judge=_d23_r05,
        exit_kind="escalate",
        exit_value="G06",
        next_nodes=("G06",),
        link=Link.EITHER,
    ),
    RuleSlot(
        rule_id="D2.3.R91",
        priority="P3",
        source_row="D2.3!7",
        statement="分别比较 EVAR/OSR 的解剖适配、可及时实施性、患者风险与资源；均不适配 → MDT/转诊/救援",
        judge=_d23_r91,
        exit_kind="terminal",
        exit_value="EVAR/OSR 候选已列出（复杂解剖→G06，均不适配→MDT/转诊）",
    ),
)


def _evar_candidate(state: GuidelineState) -> Candidate:
    """EVAR 这一列 —— 按「硬排除 → 未评估 → 可行 → 条件性」落档。

    与 ``D1.3`` 的差别只有一处：**不要求远端密封区**。紧急场景等不起远端测量，
    规格 ``D2.3.R90`` 明写「急诊不等待非关键资料」。
    """
    anatomy = anatomy_available(state)
    blocked = access_blocked(state)
    ifu = _ifu(state)

    if blocked is True:
        return Candidate(
            "EVAR",
            CandidateStatus.EXCLUDED,
            "双侧股髂闭塞或严重迂曲，无法建立入路",
            ("D2.3.R05",),
        )
    if ifu == "不符合":
        return Candidate(
            "EVAR",
            CandidateStatus.EXCLUDED,
            "指定器械不符合 IFU；比较备选或转复杂规划",
            ("D2.3.R05",),
        )
    if branch_involved(state) is True:
        return Candidate(
            "EVAR",
            CandidateStatus.EXCLUDED,
            "近端瘤颈累及重要分支；标准 EVAR 不适配，需 FEVAR/BEVAR 或复杂开放修复",
            ("D2.3.R03",),
        )
    if anatomy is False:
        return Candidate(
            "EVAR",
            CandidateStatus.UNASSESSED,
            "瘤颈长度/直径/角度均缺，无法判断解剖适配",
        )
    if anatomy is True and blocked is False and ifu == "符合":
        return Candidate(
            "EVAR",
            CandidateStatus.FEASIBLE,
            "解剖符合、入路无阻断、指定器械 IFU 符合；术者确认",
            ("D2.3.R01",),
        )

    reasons: list[str] = []
    if anatomy is None:
        reasons.append("瘤颈参数不全")
    if blocked is None:
        reasons.append("入路可行性未评估")
    if ifu in ("未核验", "未选择型号"):
        reasons.append("IFU 未核验（待核验候选）")
    elif ifu is None:
        reasons.append("IFU 未记录")
    return Candidate(
        "EVAR",
        CandidateStatus.CONDITIONAL,
        "；".join(reasons) or "待术者确认",
        ("D2.3.R06",),
    )


def _osr_candidate(state: GuidelineState) -> Candidate:
    """OSR 这一列 —— 至多到「需 MDT 评估」。

    理由与 ``D1.3`` 相同：``VascCase`` 的解剖字段全是腔内解剖，没有夹闭位置、
    肾内脏灌注这类开放手术判据。差别是本节点额外读 ``D2.RISK``（紧急场景的手术
    风险是独立申报值，不像择期那样由 ``D1.OP_FITNESS`` 承载）。
    """
    risk = _risk(state)
    evar_out = (
        access_blocked(state) is True or _ifu(state) == "不符合" or branch_involved(state) is True
    )
    if risk is None:
        return Candidate("OSR", CandidateStatus.UNASSESSED, "当前手术风险未评价")
    if risk == "高风险":
        return Candidate(
            "OSR",
            CandidateStatus.MDT,
            "高危；开放解剖与麻醉评估无输入，须与 EVAR 并列比较",
            ("D2.3.R02",),
        )
    if evar_out:
        reason = "EVAR 不可行，本方向成为唯一候选；开放解剖与麻醉评估无输入"
    elif _pref(state) == "OSR倾向":
        reason = "患者倾向 OSR；开放解剖与麻醉评估无输入，须术者/MDT 确认"
    else:
        reason = "风险可接受；开放解剖与长期耐久性比较无输入，须与 EVAR 并列比较"
    return Candidate("OSR", CandidateStatus.MDT, reason, ("D2.3.R02",))


def _d23_gaps(state: GuidelineState) -> tuple[str, ...]:
    """本节点缺什么 —— 只列会改变候选状态的项。"""
    gaps: list[str] = []
    if anatomy_available(state) is not True:
        gaps.append("EVAR 解剖参数不全（N.NECK_LEN / N.NECK_D / N.NECK_ANGLE）")
    if access_blocked(state) is None:
        gaps.append("入路可行性未评估（N.ACCESS_OCCLUSION / N.ACCESS_TORTUOSITY）")
    if cta_evaluable(state) is None:
        gaps.append("N.CTA_STATUS：本次 CTA 对本节点任务的可评价性未记录")
    if _ifu(state) is None:
        gaps.append("N.EVAR_IFU_STATUS：器械整体 IFU 未记录（待核验候选）")
    gaps.extend(missing_fields(state, _D23_FIELDS))
    return tuple(gaps)


def d2_3(state: GuidelineState) -> NodeOutcome:
    """D2.3：给出 EVAR / OSR 两列候选 + 把握程度 + 待补清单。

    与 ``D1.3`` 同构（出口由 ``evaluate_node`` 定，术式规则只留痕），差别是这里的
    出口可以 ``escalate G06``：紧急场景转复杂中心比择期更需要「立刻说清去哪」。
    """
    outcome = evaluate_node("D2.3", state, _D23_SLOTS)
    extra_entries, extra_verdicts = display_entries("D2.3", state, _D23_DISPLAY_RULES)
    return replace(
        outcome,
        verdicts={**outcome.verdicts, **extra_verdicts},
        entries=(*outcome.entries, *extra_entries),
        gaps=_d23_gaps(state),
        candidates=(_evar_candidate(state), _osr_candidate(state)),
    )


#: 本模块提供的节点（键与规格 sheet 名一致）。
NODES: dict[str, NodeFn] = {
    "D2.1": d2_1,
    "D2.2": d2_2,
    "D2.3": d2_3,
}

#: 已实现的规则 → 规格源行。``D2.3`` 四条术式规则以展示性条目留痕。
RULE_ROWS: dict[str, str] = {
    **{slot.rule_id: slot.source_row for slot in _D21_SLOTS},
    **{slot.rule_id: slot.source_row for slot in _D22_SLOTS},
    **{slot.rule_id: slot.source_row for slot in _D23_SLOTS},
    **{rule.rule_id: rule.source_row for rule in _D23_DISPLAY_RULES},
}
