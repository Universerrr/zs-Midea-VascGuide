"""D3 · 破裂／疑似破裂 AAA（``D3.1`` → ``D3.2`` → ``D3.3``）。

规格来源：``VascGuide_V3.6.2.xlsx`` 的 ``D3.1`` / ``D3.2`` / ``D3.3`` 三个 sheet，
共 20 条规则（7 + 8 + 5，含每 sheet 自带的 R90/R91）。当前实现 17 条。

## 三个节点的性质完全不同，实现方式也不同

``D3.1`` 破裂确定性与证据
    真正有判定逻辑的节点：用 ``CL.AAA_RUPTURE`` / ``C.EXTRAV`` / ``C.HEMATOMA``
    / ``C.RUPTURE_REPORT`` / ``V.SBP`` / ``V.CONSCIOUS_CHANGE`` 做三值判定。
    这 6 个字段都在 ``VascCase`` 里，7 条规则全部可运行。

``D3.2`` 循环状态、快速准备与资源
    **唯一做术式方向判断的节点**。规格里 R01 / R02 / R04 都有明确结论——
    「EVAR 为指南推荐的一线治疗选项」「EVAR 不适合则评估 OSR」——只是每条后面
    都跟一句「仍需术者确认」。所以本节点给方向 + 把握程度 + 待补清单，
    而不是「不判」。

    判据全部走 ``VascCase`` 现成字段：``n.neck_len`` / ``n.neck_d`` /
    ``n.neck_angle``（解剖）、``n.seal_ifu``（IFU 结论）、
    ``n.access_occlusion`` / ``n.access_tortuosity``（入路）、
    ``v.sbp`` / ``v.conscious_change``（患者状态）。规格那 5 个 ``D3.*`` 字段
    有 4 个就是这样派生的；只有「团队资源 / 血液准备 / 麻醉 / 复苏」这 4 项
    真的没有来源——它们是**机构能力而非病例事实**，只记缺口、不阻断方向。

``D3.3`` EVAR/OSR/救援及不修复
    规格五条规则里三条反复写「Agent 不得自动作出不可手术结论」「不得仅凭年龄
    或合并症判定无修复价值」。故本节点只整理依据 + 报告缺项，出口恒为
    「术者确认」，不产出任何"建议不修复"的结论。

## 为什么病种域字段不进 ``VascCase``

``VascCase`` 是路由专用（161 字段，进哈希）。``D3.TEAM_RESOURCE`` 这类字段
既不影响路由，也不需要进路由哈希——塞进去会让每一次路由哈希都随病种字段抖动。
本文件因此把「能派生的派生，不能派生的留空 + 记缺口」，两点都不新增模型字段。
"""

from __future__ import annotations

from dataclasses import replace

from ....models.preop.case import UNCERTAIN_TOKENS
from ....rules.router import (
    D3_EMERGENCY_RULES,
    Verdict,
    and_,
    eq,
    not_,
    or_,
    token,
)
from ..state import Candidate, CandidateStatus, GuidelineEntry, GuidelineState, NodeFn, NodeOutcome
from .common import RuleSlot, evaluate_node, gap_if_missing

__all__ = ["NODES", "d3_1", "d3_2", "d3_3"]

#: 「影像/报告已经明确」的触发规则（外溢 / 瘤伴血肿 / 报告明确破裂）。
_D3_CONFIRMED: frozenset[str] = frozenset({"G03.R01", "G03.R02", "G03.R03"})

#: 「临床高度怀疑」的触发规则——由总数派生，不另抄一份清单。
_D3_SUSPECTED: frozenset[str] = D3_EMERGENCY_RULES - _D3_CONFIRMED

#: ``D3.2`` 里**确实没有对应物**的字段（规格 ID → 人话）。只有这 4 个。
#:
#: 规格该节点共引用 5 个字段，其中 4 个能由 ``VascCase`` 现成字段派生，见下表::
#:
#:     D3.ANATOMY_INFO_STATUS  ← n.neck_len / n.neck_d / n.neck_angle 是否有值
#:     N.EVAR_IFU_STATUS       ← n.seal_ifu（符合 / 不符 / 未核验 / 未选型号）
#:     D3.ACCESS_PLAN          ← n.access_occlusion + n.access_tortuosity + cl.iliac_diam
#:     D3.PATIENT_STATUS       ← v.sbp + v.conscious_change
#:
#: 剩下这 4 个是「机构能力与准备状态」，不属于病例事实（换个病人也不变），
#: 故不建模型字段；缺它们只记缺口、**不阻断**术式方向——规格 D3.2.R90 明写
#: 「由急诊血管团队基于可用信息决策，不延误控制」。
_D32_RESOURCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("D3.TEAM_RESOURCE", "当次团队与资源：术者/手术室/DSA/血库/麻醉/ICU"),
    ("D3.BLOOD_PREP", "血液准备：血型/交叉配血/大量输血流程"),
    ("D3.ANAESTHESIA_PLAN", "麻醉与生理策略评估"),
    ("D3.RESUSCITATION_STRATEGY", "复苏策略与目标"),
)

_D33_FIELDS: tuple[tuple[str, str], ...] = (
    ("D3.REPAIR_FEASIBILITY", "修复可行性：存在 EVAR/OSR 方案 / 需转诊 / 无可行方案"),
    ("D3.PATIENT_WISH", "患者意愿：有决策能力时的明确表达 / 预先医疗决定"),
    ("D3.CAPACITY", "决策能力评估与合法代理人"),
    ("D3.PROGNOSIS", "获益与预后综合评估"),
    ("D3.GOALS", "治疗目标：积极修复 / 转诊 / 有限治疗 / 舒适照护"),
)


def _missing(state: GuidelineState, fields: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    """从 ``state.d3`` 通道读字段，缺失或取值不确定的记一条缺口。"""
    out: list[str] = []
    for spec_id, label in fields:
        gap = gap_if_missing(f"{spec_id}（{label}）", state.d3.get(spec_id))
        if gap:
            out.append(gap)
    return tuple(out)


# ---------------------------------------------------------------------------
# D3.1 破裂确定性与证据
# ---------------------------------------------------------------------------
def _rupture(state: GuidelineState) -> object:
    return token(state.case.cl.aaa_rupture)


def _extrav(state: GuidelineState) -> object:
    return token(state.case.c.extrav)


def _hematoma(state: GuidelineState) -> object:
    return token(state.case.c.hematoma)


def _report(state: GuidelineState) -> object:
    return token(state.case.c.rupture_report)


def _d31_r01(state: GuidelineState) -> Verdict:
    """影像/医生有明确破裂证据 → 急诊路径（不等待直径阈值）。

    **判据不在这里重写**：``G03.R01/R02/R03`` 已经在判定层判过一遍，结果就是
    ``route_result.rule_id``。在本节点再拼一遍 ``c.extrav`` / ``c.hematoma``
    等于同一事实两份判据——今天一致，改一处阈值就会分叉。
    """
    return state.route.rule_id in _D3_CONFIRMED


def _d31_r02(state: GuidelineState) -> Verdict:
    """临床怀疑型触发（疑似破裂 / 急性疼痛 / 可疑征象 / 无可靠 CTA）。

    同样复用判定层结论：``G03.R04/R05/R06/R07/R12`` 都属「临床高度怀疑」，
    与 R01 的「影像明确」分开记录，审计时才看得出是「拍到了」还是「怀疑」。
    """
    return state.route.rule_id in _D3_SUSPECTED


def _d31_r03(state: GuidelineState) -> Verdict:
    """CTA 是否可等待 —— 依赖 ``D3.CTA_FEASIBLE``，当前无输入通道。"""
    return None  # type: ignore[return-value]  # 字段未提供 ≠ 不可化验，保持 UNKNOWN


def _d31_r04(state: GuidelineState) -> Verdict:
    """临床高度怀疑但影像缺失/不确定 —— 依赖 ``D3.*`` 三个组合字段，当前无输入通道。"""
    return None  # type: ignore[return-value]


def _d31_r05(state: GuidelineState) -> Verdict:
    """确定性分级：不可评价或与报告冲突时，**不得**降级为「排除破裂」。"""
    grade = _rupture(state)
    if grade is None:
        return None
    if grade == "无法评价":
        return True
    return eq(_report(state), "明确破裂", "疑似破裂") is True and grade == "未见相关证据"


def _d31_r90(state: GuidelineState) -> Verdict:
    """P2 数据质量阻断：破裂状态或关键影像不可评价。"""
    return or_(
        eq(_rupture(state), "无法评价"),
        eq(_extrav(state), "不可评价"),
        eq(_hematoma(state), "不可评价"),
    )


def _d31_r91(state: GuidelineState) -> Verdict:
    """P3 出口：可靠评估支持**未见**破裂，且无任何阳性证据。"""
    return and_(
        eq(_rupture(state), "未见相关证据"),
        eq(_extrav(state), "未见"),
        eq(_hematoma(state), "未见"),
        not_(eq(_report(state), "明确破裂", "疑似破裂")),
    )


_D31_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D3.1.R01",
        priority="P0-A",
        source_row="D3.1!10",
        statement="影像或医生明确/疑似破裂，或主动脉外出血征象 → 急诊路径，不等待直径阈值",
        judge=_d31_r01,
        exit_kind="escalate",
        exit_value="G05",
        also_next="D3.2",
    ),
    RuleSlot(
        rule_id="D3.1.R02",
        priority="P0-A",
        source_row="D3.1!11",
        statement="疑似 rAAA 背景下 SBP<80 mmHg 或新发意识变化 → 血流动力学不稳定",
        judge=_d31_r02,
        exit_kind="escalate",
        exit_value="G05",
        also_next="D3.2",
    ),
    RuleSlot(
        rule_id="D3.1.R05",
        priority="P0-A",
        source_row="D3.1!17",
        statement="破裂分级不可评价或与报告冲突 → 不得降级为「排除破裂」，保持急诊疑似",
        judge=_d31_r05,
        exit_kind="escalate",
        exit_value="G05",
        also_next="D3.2",
    ),
    RuleSlot(
        rule_id="D3.1.R03",
        priority="P3",
        source_row="D3.1!12",
        statement="决定 CTA 是否可等待：稳定则尽快 CTA，过度不稳定则直接手术室",
        judge=_d31_r03,
        exit_kind="next",
        exit_value="D3.2",
    ),
    RuleSlot(
        rule_id="D3.1.R04",
        priority="P3",
        source_row="D3.1!16",
        statement="临床高度怀疑但影像缺失/不可评价 → 维持急诊疑似，不因未见影像证据降级",
        judge=_d31_r04,
        exit_kind="next",
        exit_value="D3.2",
    ),
    RuleSlot(
        rule_id="D3.1.R90",
        priority="P2",
        source_row="D3.1!9",
        statement="破裂状态或关键影像不可评价 → 保留疑似/不能排除，紧急团队复核",
        judge=_d31_r90,
        exit_kind="terminal",
        exit_value="人工复核",
    ),
    RuleSlot(
        rule_id="D3.1.R91",
        priority="P3",
        source_row="D3.1!8",
        statement="可靠评估支持未见破裂且授权医生确认 → 按症状状态回 D1.1 或 D2.1",
        judge=_d31_r91,
        exit_kind="terminal",
        exit_value="回 D1.1/D2.1（按症状归因）",
    ),
)


def d3_1(state: GuidelineState) -> NodeOutcome:
    """D3.1：破裂确定性与证据。"""
    return evaluate_node("D3.1", state, _D31_SLOTS)


# ---------------------------------------------------------------------------
# D3.2 循环状态、快速准备与资源（EVAR / OSR 候选）
# ---------------------------------------------------------------------------
#
# 这一节是本文件唯一有「术式方向」判断的地方，也是规格里最容易误读的一段。
# 规格在 R90 里反复强调「由急诊血管团队决定」，容易被读成「Agent 什么都不判」；
# 但 R01 写的是「解剖适合 EVAR 且团队/设备可用时，EVAR 为**指南推荐的一线治疗
# 选项**」——那是一条有明确结论的推荐，只是后面跟了一句「仍需术者即时确认」。
#
# 所以正确形态是：**给出方向 + 标注把握程度 + 列出还差什么**，而不是「不判」。
def _anatomy_available(state: GuidelineState) -> Verdict:
    """解剖参数是否足以比较 EVAR 与 OSR（替代规格的 ``D3.ANATOMY_INFO_STATUS``）。

    为什么不建这个字段：它问的是「资料够不够」，而够不够直接看三个瘤颈参数
    有没有值就知道。再造一个「关于信息的字段」等于把同一事实存两遍。

    三值：齐全 → ``True``；一个都没有 → ``False``（R90 阻断）；部分缺 → ``None``
    （不阻断，但不给方向，让团队补）。
    """
    vals = (state.case.n.neck_len, state.case.n.neck_d, state.case.n.neck_angle)
    present = sum(v is not None for v in vals)
    if present == len(vals):
        return True
    if present == 0:
        return False
    return None


def _access_blocked(state: GuidelineState) -> Verdict:
    """入路是否**明确阻断** EVAR（替代规格的 ``D3.ACCESS_PLAN``）。

    只有「双侧」才阻断：单侧闭塞/迂曲时对侧仍可建立入路，EVAR 依然可行。
    这正是规格把「入路可行」与「器械可用」分开写的原因。
    """
    occlusion = token(state.case.n.access_occlusion)
    tortuosity = token(state.case.n.access_tortuosity)
    if occlusion == "双侧闭塞" or tortuosity == "双侧严重迂曲":
        return True
    if occlusion is None or tortuosity is None:
        return None
    if _is_uncertain(occlusion) or _is_uncertain(tortuosity):
        return None
    return False


def _is_uncertain(value: object) -> bool:
    """取值本身不确定（「未知」等）—— 只用于本地判断，不参与规则求值。"""
    return isinstance(value, str) and value in UNCERTAIN_TOKENS


def _patient_evaluable(state: GuidelineState) -> Verdict:
    """患者是否处于可完成术前评估的状态（替代规格的 ``D3.PATIENT_STATUS``）。

    分档而非二值：``SBP<80`` 明确不稳定（与 ``D3.1.R02`` 同一阈值）；
    80–89 属临界，不判；``≥90`` 视为可评估。

    取值口径以 ``VitalsDomain.conscious_change`` 的字面量为准：
    ``新发下降 / 无新变化 / 基线未知``。写成「新发意识下降」会让比较恒为假
    ——``Literal`` 不在枚举里的字符串永远不会相等，那条分支就成了死代码。
    """
    vitals = state.case.v
    if eq(token(vitals.conscious_change), "新发下降") is True:
        return False
    if vitals.sbp is None:
        return None
    if vitals.sbp < 80:
        return False
    if vitals.sbp >= 90:
        return True
    return None


def _d32_r01(state: GuidelineState) -> Verdict:
    """P0-A：解剖初步可行 + 入路无阻断 + IFU 符合 + 患者可评估 → **EVAR 一线**。

    入路为什么也进本判据：规格 R01 的依赖字段只列了「解剖信息状态」，但双侧
    髂动脉闭塞时 EVAR 根本无法建立入路，把它算作「解剖适合」是错的——入路
    本就属于「解剖是否适合 EVAR」的一部分。

    「团队/设备可用」不参与判据：它没有数据来源，且规格 R90 明确要求
    「不延误控制」，不能因为没有资源清单就否定一线方案。该条件以缺口形式
    单独列出，由术者当场确认。
    """
    return and_(
        _anatomy_available(state),
        not_(_access_blocked(state)),
        eq(token(state.case.n.seal_ifu), "符合"),
        _patient_evaluable(state),
    )


def _d32_r02(state: GuidelineState) -> Verdict:
    """P1：EVAR 不可行 → 评估 OSR。

    「不可行」的两种硬证据：入路双侧阻断、IFU 明确不符。IFU **未核验**
    不等于不符（三值语义），此时本规则保持 UNKNOWN，不把病人推去开放手术。
    """
    return or_(
        _access_blocked(state),
        eq(token(state.case.n.seal_ifu), "不符"),
    )


def _d32_r03(state: GuidelineState) -> Verdict:
    """P3：本中心无法安全实施 → 现场救援/转诊。依赖团队资源，当前无来源。"""
    return eq(token(state.d3.get("D3.TEAM_RESOURCE")), "本中心无法实施")


def _d32_r04(state: GuidelineState) -> Verdict:
    """P3：解剖可行但 IFU 未核验 → **EVAR 候选**，并记录核验程度。

    这是 R01 的降级版本：同样给方向，只是把握程度低一档。规格原文要求
    「记录具体依据与 IFU 核验程度，最终由术者确认」——记录程度就是本规则的产出。
    """
    return and_(_anatomy_available(state), not_(_access_blocked(state)))


def _d32_r06(state: GuidelineState) -> Verdict:
    """P3：资源缺项清单（提示类，不参与判定）。

    规格要求「立即显示缺失的术者、手术室、影像、器械、血库/ICU 能力」——
    这是**展示义务**而非判定条件，故恒 UNKNOWN，缺项由节点的 ``gaps`` 承担。
    """
    return None


def _d32_r07(state: GuidelineState) -> Verdict:
    """P3：复苏/血液/手术室并行准备（提示类）。

    规格原文「Agent 提示缺项并记录状态，不生成具体复苏医嘱或固定目标」——
    所以这里没有判定，只有留痕。
    """
    return None


def _d32_r90(state: GuidelineState) -> Verdict:
    """P2：解剖参数一个都没有 → 不得给出任何术式方向。"""
    return _anatomy_available(state) is False


def _d32_r91(state: GuidelineState) -> Verdict:
    """P3 出口：把候选方向或现场团队决策交出去。"""
    return True


_D32_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D3.2.R01",
        priority="P0-A",
        source_row="D3.2!10",
        statement="破裂 AAA 解剖适合 EVAR 且团队/设备可用 → EVAR 为指南推荐的一线治疗选项（仍需术者即时确认）",
        judge=_d32_r01,
        exit_kind="terminal",
        exit_value="EVAR 一线（术者即时确认）",
    ),
    RuleSlot(
        rule_id="D3.2.R02",
        priority="P1",
        source_row="D3.2!9",
        statement="EVAR 解剖不适合或无法及时安全实施 → 评估 OSR；不能因 CTA 缺失自动排除任何术式",
        judge=_d32_r02,
        exit_kind="terminal",
        exit_value="评估 OSR（术者决定，必要时术中评估）",
    ),
    RuleSlot(
        rule_id="D3.2.R90",
        priority="P2",
        source_row="D3.2!6",
        statement="急诊解剖资料不完整 → 不宣称 IFU 已符合，由急诊血管团队基于可用信息决策",
        judge=_d32_r90,
        exit_kind="terminal",
        exit_value="人工复核（解剖参数不足）",
    ),
    RuleSlot(
        rule_id="D3.2.R03",
        priority="P3",
        source_row="D3.2!16",
        statement="立即提示可实施方案与资源缺口；现场救援或转诊由临床团队决定",
        judge=_d32_r03,
        exit_kind="terminal",
        exit_value="急诊交接／转诊／现场救援",
    ),
    RuleSlot(
        rule_id="D3.2.R04",
        priority="P3",
        source_row="D3.2!15",
        statement="解剖可行且入路无阻断 → EVAR 候选；记录具体依据与 IFU 核验程度",
        judge=_d32_r04,
        exit_kind="terminal",
        exit_value="EVAR 候选（IFU 待核验，术者确认）",
    ),
    RuleSlot(
        rule_id="D3.2.R06",
        priority="P3",
        source_row="D3.2!18",
        statement="显示缺失的术者、手术室、影像、器械、血库/ICU 能力（提示类，不产生判定）",
        judge=_d32_r06,
        exit_kind="terminal",
        exit_value="急诊交接／救援／转诊",
    ),
    RuleSlot(
        rule_id="D3.2.R07",
        priority="P3",
        source_row="D3.2!19",
        statement="疑似/明确 rAAA 并行组织复苏、血液准备、手术室与修复策略（提示类，不生成医嘱）",
        judge=_d32_r07,
        exit_kind="terminal",
        exit_value="急诊团队即时执行",
    ),
    RuleSlot(
        rule_id="D3.2.R91",
        priority="P3",
        source_row="D3.2!5",
        statement="可及时安全实施且解剖候选符合 → EVAR 候选；否则评估 OSR／现场救援／转诊",
        judge=_d32_r91,
        exit_kind="terminal",
        exit_value="术者确认（急诊血管团队）",
    ),
)


def _d32_gaps(state: GuidelineState) -> tuple[str, ...]:
    """「还差什么才敢比较 EVAR 与 OSR」——只列**真正没有**的项。

    区分「缺数据」与「缺能力」：前 4 项能从 ``VascCase`` 补录，后 4 项是机构
    准备状态，本系统不采集，只提醒术者当场确认。
    """
    gaps: list[str] = []

    if _anatomy_available(state) is not True:
        gaps.append("解剖参数不足以比较 EVAR 与 OSR（瘤颈长度/直径/角度）")
    if eq(token(state.case.n.seal_ifu), "符合", "不符") is None:
        gaps.append("EVAR 器械 IFU 未核验（seal_ifu = 未核验/未选型号）")
    if _access_blocked(state) is None:
        gaps.append("入路可行性未评估（股髂直径/钙化/迂曲）")
    if _patient_evaluable(state) is None:
        gaps.append("患者是否可完成评估未明确（血压/意识状态）")

    gaps.extend(_missing(state, _D32_RESOURCE_FIELDS))
    return tuple(gaps)


# ---------------------------------------------------------------------------
# D3.2 的术式候选（EVAR / OSR 两列）
# ---------------------------------------------------------------------------
#
# 为什么要单开这一层：``entries`` 按 rule_id 排，回答「这条规则凭什么成立」；
# 但规格 D1.3.R91 要的是「**分别**列出 EVAR/OSR 的可行候选、条件性候选和
# 排除原因」——那是按术式排的。用规则序列表达不了「OSR 现在是什么状态」，
# 因为可能一条规则都没提 OSR。两个切面必须分开。
def _evar_candidate(state: GuidelineState) -> Candidate:
    """EVAR 这一列 —— 按「硬排除 → 未评估 → 可行 → 条件性」的优先级落档。

    顺序不是随手排的：硬排除先于数据阻断。入路双侧闭塞时，资料再全也是排除；
    反之瘤颈参数全缺时不叫排除，叫「未评估」（``D3.2.R90`` 的阻断口径）。
    """
    anatomy = _anatomy_available(state)
    blocked = _access_blocked(state)
    ifu_ok = eq(token(state.case.n.seal_ifu), "符合")
    evaluable = _patient_evaluable(state)

    if blocked is True:
        return Candidate(
            "EVAR",
            CandidateStatus.EXCLUDED,
            "双侧股髂闭塞或严重迂曲，无法建立入路",
            ("D3.2.R02",),
        )
    if ifu_ok is False:
        return Candidate("EVAR", CandidateStatus.EXCLUDED, "器械 IFU 明确不符", ("D3.2.R02",))
    if anatomy is False:
        return Candidate(
            "EVAR",
            CandidateStatus.UNASSESSED,
            "瘤颈长度/直径/角度均缺，无法判断解剖适配",
        )
    if anatomy is True and blocked is False and ifu_ok is True and evaluable is True:
        return Candidate(
            "EVAR",
            CandidateStatus.FEASIBLE,
            "解剖符合、入路无阻断、IFU 符合、患者可评估；术者即时确认",
            ("D3.2.R01",),
        )

    # 到这里说明「有方向但缺条件」——逐项点名差什么，不合并成一句「资料不全」。
    reasons: list[str] = []
    if anatomy is None:
        reasons.append("瘤颈参数不全")
    if blocked is None:
        reasons.append("入路可行性未评估")
    if ifu_ok is None:
        reasons.append("IFU 未核验")
    if evaluable is None:
        reasons.append("患者状态待明确")
    elif evaluable is False:
        reasons.append("患者不稳定，需并行复苏")
    return Candidate(
        "EVAR",
        CandidateStatus.CONDITIONAL,
        "；".join(reasons) or "待术者确认",
        ("D3.2.R04",),
    )


def _osr_candidate(state: GuidelineState) -> Candidate:
    """OSR 这一列 —— 恒为「需 MDT 评估」，只换理由。

    为什么不给「可行候选」：``D1.3.R02`` 要求「解剖上可安全开放夹闭/重建」，
    而 ``VascCase`` 的 27 个解剖字段**全是腔内解剖**（瘤颈/入路/髂动脉），
    没有一个描述开放手术关心的夹闭位置、肾内脏灌注、腹膜后条件。没有判据就
    不下结论——这正是该规则「须由术者/MDT 确认」的落点，不是实现缺口。

    为什么仍要列出这一行：规格要求「分别列出」两列。只给 EVAR 会让读者以为
    OSR 已被排除，而实际上它只是待专家评估——两者临床含义完全不同。
    """
    evar_excluded = (
        _access_blocked(state) is True or eq(token(state.case.n.seal_ifu), "符合") is False
    )
    reason = (
        "EVAR 不可行，本方向成为唯一候选；开放解剖与麻醉评估无输入"
        if evar_excluded
        # 措辞对齐 D1.3.R09「不得因某一术式不适配而自动把另一术式设为首选」：
        # 这里只声明「要并列比较」，不给排序。
        else "开放解剖与麻醉评估无输入；须与 EVAR 并列比较"
    )
    return Candidate("OSR", CandidateStatus.MDT, reason, ("D3.2.R02",))


def d3_2(state: GuidelineState) -> NodeOutcome:
    """D3.2：给出 EVAR/OSR 的候选方向 + 把握程度 + 待补清单。

    判定走 :func:`evaluate_node`（P0→P1→P2→P3 逐条求值），缺口与候选在节点层
    统一补齐——因为两者都是「本次运行的输入状态」的映射，不属于任何单条规则的
    声明：缺口是全局输入缺口，候选是跨规则的术式归并。
    """
    outcome = evaluate_node("D3.2", state, _D32_SLOTS)
    return replace(
        outcome,
        gaps=_d32_gaps(state),
        candidates=(_evar_candidate(state), _osr_candidate(state)),
    )


# ---------------------------------------------------------------------------
# D3.3 EVAR/OSR/救援及不修复（缺项提示器）
# ---------------------------------------------------------------------------
def d3_3(state: GuidelineState) -> NodeOutcome:
    """D3.3：整理非修复/转诊决策的依据与缺项，出口恒为医生确认。

    规格三条规则都写了同一句话：「系统不得自行下达不修复结论」。故本节点
    **不产出任何"建议不修复"的结论**，只把缺项和依据摊开给授权医生。
    """
    gaps = _missing(state, _D33_FIELDS)
    if gaps:
        return NodeOutcome(
            node_id="D3.3",
            verdicts={"D3.3.R90": True},
            entries=(
                GuidelineEntry(
                    node_id="D3.3",
                    rule_id="D3.3.R90",
                    source_row="D3.3!6",
                    statement="患者意愿、决策能力、修复可行性或获益评估未完成 → 不得自动判定不修复",
                    verdict=True,
                ),
            ),
            terminal="术者确认（须记录可行性/意愿/裁决医师与时间）",
            gaps=gaps,
        )
    return NodeOutcome(
        node_id="D3.3",
        verdicts={"D3.3.R91": True},
        entries=(
            GuidelineEntry(
                node_id="D3.3",
                rule_id="D3.3.R91",
                source_row="D3.3!5",
                statement="授权医生记录可行性、获益风险、患者/代理意愿与转诊可能性后确认处置",
                verdict=True,
            ),
        ),
        terminal="术者确认（修复/转诊/非修复/舒适照护）",
    )


#: 本模块提供的节点（键与规格 sheet 名一致）。
NODES: dict[str, NodeFn] = {
    "D3.1": d3_1,
    "D3.2": d3_2,
    "D3.3": d3_3,
}

#: 已实现的规则 → 规格源行。``D3.1`` 7 条 + ``D3.2`` 8 条全实现；
#: ``D3.3`` 只实现 R90/R91 两条结构性规则——它的 R01–R03 判据全部是
#: 「授权医生记录可行性/意愿/预后后确认」，属裁决留痕而非判定，等录入通道
#: 建好再补（否则三条规则恒 UNKNOWN，只是多三行噪声）。
RULE_ROWS: dict[str, str] = {
    **{slot.rule_id: slot.source_row for slot in (*_D31_SLOTS, *_D32_SLOTS)},
    "D3.3.R90": "D3.3!6",
    "D3.3.R91": "D3.3!5",
}
