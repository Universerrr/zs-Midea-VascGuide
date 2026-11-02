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

from ....rules.router import (
    D3_EMERGENCY_RULES,
    Verdict,
    and_,
    eq,
    not_,
    or_,
    token,
)
from ..state import Candidate, CandidateStatus, GuidelineState, NodeFn, NodeOutcome
from .common import (
    DisplayRule,
    RuleSlot,
    access_blocked,
    anatomy_available,
    display_entries,
    evaluate_node,
    missing_fields,
    multi_field,
    spec_token,
)

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
    """P3：患者是否**足够稳定**等到 CTA（``D3.1!12``）。

    规格 G 列是两档：稳定 → 尽快 CTA 胸腹主动脉与入路；过度不稳定 → 直接
    手术室/术中影像。两档的出口相同（都进 ``D3.2``），所以本规则只回答
    「能不能等」这一个问题；分档的措辞由 ``exit_value`` 承担。

    判据主体是 ``D3.CTA_FEASIBLE``（规格 H 列：医生/麻醉评估）：
    ``可以`` → 能等；``不可以`` → 不能等；``不确定``/未记录 → 判不了
    （规格 J 列：「不确定→紧急团队判断」）。

    ⚠️ 读**原始值** ``token`` 而不是 ``spec_token``：``spec_token`` 会把
    ``不确定`` 折成 ``None``（它在 ``UNCERTAIN_TOKENS`` 里）。这里的
    「不确定」是**字段的合法结论**，不是「值缺失」——同 ``D3.CTA_COVERAGE``
    的「不可评价」。两层「不确定」必须分开：``Q.VALUE_STATUS`` 的
    「不可评价」是「这个值的状态不明」，``D3.*`` 的「不可评价」是「影像
    确实无法评价」这个确定结论。

    ⚠️ **端到端不可达，但不是「判据没写」**：``class_output`` 含 ``D3`` 只在
    ``D3_EMERGENCY_RULES``（G03 九条）命中时发生，而那九条全部落在
    ``_D3_CONFIRMED`` 或 ``_D3_SUSPECTED`` 里 —— ``R01``/``R02`` 必有一条先命中
    （同为 P0-A 且声明在前）。所以本规则只在手动调用、或将来 R01/R02
    的覆盖范围收缩时才会执行。保留真判据的理由：占位 ``return None`` 与
    「实现了但被遮蔽」在工程上是两件事——后者不是待办，前者是。
    """
    return eq(token(state.node_field("D3.CTA_FEASIBLE")), "可以")


#: ``D3.CTA_COVERAGE`` 里表示「影像不足以判」的三档（规格 F 列值域）。
#: ``腹主动脉至髂股入路`` / ``胸腹扩展`` 两档属覆盖充分，不在其中。
_CTA_INCOMPLETE: tuple[str, ...] = ("未做", "不可评价", "部分")


def _d31_r04(state: GuidelineState) -> Verdict:
    """P3：临床仍在怀疑、但影像不足以判 → 维持急诊疑似，不降级（``D3.1!16``）。

    规格 G 列的两个前提：临床医生明确疑似（或危险证据持续）**且** CTA 未做／
    不可评价／结果冲突。所以判据分两段，缺一段就判不了：

    1. 影像是否不足以判 —— 读 ``D3.CTA_COVERAGE``（值域稳定，四档明确）；
    2. 临床是否仍在怀疑 —— 读 ``CL.AAA_RUPTURE`` 或 ``C.RUPTURE_REPORT``。

    为什么第二段不读 ``D3.RUPTURE_CONTEXT``：它是「已知AAA+急性腹痛+晕厥…」
    这种**组合串**（规格 D 列写的「结构化组合」），按子串匹配等于关键词匹配，
    本仓库明令禁止。``CL.AAA_RUPTURE`` 是同一事实的结构化版本，判它更可靠。

    ⚠️ ``D3.CTA_COVERAGE`` 同样读原始值：它的 ``不可评价`` 是确定结论
    （「这次影像确实没法评价」），而 ``spec_token`` 会把它折成 ``None``——
    折掉之后「影像不可评价」与「没填这个字段」就分不开了，而 R04 要的正是前者。

    与 ``R03`` 同一条限制：端到端被 ``R01``/``R02`` 遮蔽，见 R03 的说明。
    """
    coverage = eq(token(state.node_field("D3.CTA_COVERAGE")), *_CTA_INCOMPLETE)
    if coverage is not True:
        return coverage
    return or_(
        eq(_rupture(state), "疑似", "无法评价"),
        eq(_report(state), "疑似破裂"),
    )


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
        statement="决定 CTA 是否可等待：足够稳定则尽快 CTA 胸腹主动脉与入路；"
        "过度不稳定则直接手术室/术中影像（两档出口相同，均进 D3.2）",
        judge=_d31_r03,
        exit_kind="next",
        exit_value="D3.2",
    ),
    RuleSlot(
        rule_id="D3.1.R04",
        priority="P3",
        source_row="D3.1!16",
        statement="临床高度怀疑但影像未做/不可评价/结果冲突 → 维持急诊疑似，"
        "不因未见影像证据或 SBP≥80 自动降级",
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
#
# ``anatomy_available`` / ``access_blocked`` 已提到 ``common.py``：``D1.3`` 的择期
# 术式候选用的是完全相同的两条判据。留在本文件会让同一事实出现两套实现。
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

    「团队/设备可用」怎么处理：只**否定明确不可用**这一种情况。

    · ``D3.TEAM_RESOURCE = 均不可立即用`` → 本规则不成立。本院此刻做不了这台
      手术，再说「EVAR 一线」就是让团队去做做不到的事（``R03`` 接住去转诊）。
    · 未记录 / 未知 / 证据冲突 → 不进判据，仍按原样成立。规格 R90 明写
      「未确认不等于不可用」「不延误控制」，拿「没填资源清单」否定一线方案，
      等于让资料缺失替医生做决定。

    两种情形分开处理的理由：**明确不可用是事实，未记录是资料缺口**——后者该进
    ``gaps``，前者必须改变结论。
    """
    if eq(token(state.node_field("D3.TEAM_RESOURCE")), "均不可立即用") is True:
        return False
    return and_(
        anatomy_available(state),
        not_(access_blocked(state)),
        eq(token(state.case.n.seal_ifu), "符合"),
        _patient_evaluable(state),
    )


def _d32_r02(state: GuidelineState) -> Verdict:
    """P1：EVAR 不可行 → 评估 OSR。

    「不可行」的两种硬证据：入路双侧阻断、IFU 明确不符。IFU **未核验**
    不等于不符（三值语义），此时本规则保持 UNKNOWN，不把病人推去开放手术。
    """
    return or_(
        access_blocked(state),
        eq(token(state.case.n.seal_ifu), "不符"),
    )


#: ``D3.TEAM_RESOURCE`` 里表示「本中心能做」的三个取值。
#: 其余三档（``均不可立即用`` / ``未知`` / ``证据冲突``）都不构成「能实施」。
_RESOURCE_AVAILABLE: tuple[str, ...] = ("EVAR团队可用", "OSR团队可用", "两者可用")


def _resource_unavailable(state: GuidelineState) -> bool:
    """院方是否**明确**此刻做不了这台手术（不是「没填」，是「填了做不了」）。"""
    return eq(token(state.node_field("D3.TEAM_RESOURCE")), "均不可立即用") is True


def _d32_r03(state: GuidelineState) -> Verdict:
    """P3：本中心不能及时提供必要修复 → 现场救援/转诊。

    比较对象是 ``D3.TEAM_RESOURCE`` 的规范值 **``均不可立即用``**。
    修前这里写的是「本中心无法实施」——**那个字符串不在该字段的值域里**
    （值域：EVAR团队可用 / OSR团队可用 / 两者可用 / 均不可立即用 / 未知 /
    证据冲突），所以本条恒为 ``False``，从来没命中过。这不是缺测试，是词表写错：
    照规格的**标题**（「本中心不能及时提供必要修复」）抄，而不是照字段的**值域**写。
    """
    return eq(token(state.node_field("D3.TEAM_RESOURCE")), "均不可立即用")


def _d32_r04(state: GuidelineState) -> Verdict:
    """P3：解剖可行但 IFU 未核验 → **EVAR 候选**，并记录核验程度。

    这是 R01 的降级版本：同样给方向，只是把握程度低一档。规格原文要求
    「记录具体依据与 IFU 核验程度，最终由术者确认」——记录程度就是本规则的产出。
    """
    return and_(anatomy_available(state), not_(access_blocked(state)))


def _d32_r06(state: GuidelineState) -> Verdict:
    """P3（展示性）：资源未确认或有缺项 → 把缺失清单摆出来。

    为什么从出口槽位改成展示性规则：「立即显示缺失的术者、手术室、影像、
    器械、血库/ICU 能力」是**展示义务**，不是「去哪」的判断。留在槽位里它只能
    恒返 ``None`` 才不抢 ``R91`` 的出口——那等于登记了一条永不生效的规则
    （实测：全量测试跑完，它一次都没判过 ``True``）。

    判据取「是否已确认可用」而不是「有哪些缺项」：缺项清单在 ``gaps`` 里，
    这里只回答「要不要把清单摆到读者面前」。
    """
    resource = token(state.node_field("D3.TEAM_RESOURCE"))
    # 未记录（None）/ 均不可立即用 / 未知 / 证据冲突 —— 四种都该把清单摆出来。
    return resource not in _RESOURCE_AVAILABLE


def _d32_r07(state: GuidelineState) -> Verdict:
    """P3（展示性）：复苏/血液/手术室并行准备 —— 进了本节点就恒适用。

    规格原文「疑似/明确 rAAA 需同时组织复苏、血液准备、手术室和修复策略；
    Agent 提示缺项并记录状态，不生成具体复苏医嘱或固定目标」。只要病例走到
    ``D3.2``，它就是疑似/明确的破裂病例，这条义务必然成立；具体缺项在
    ``gaps`` 里，本规则只留痕「提醒过」。
    """
    return True


def _d32_r90(state: GuidelineState) -> Verdict:
    """P2：解剖参数一个都没有 → 不得给出任何术式方向。"""
    return anatomy_available(state) is False


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
        rule_id="D3.2.R91",
        priority="P3",
        source_row="D3.2!5",
        statement="可及时安全实施且解剖候选符合 → EVAR 候选；否则评估 OSR／现场救援／转诊",
        judge=_d32_r91,
        exit_kind="terminal",
        exit_value="术者确认（急诊血管团队）",
    ),
)


_D32_DISPLAY_RULES: tuple[DisplayRule, ...] = (
    DisplayRule(
        rule_id="D3.2.R06",
        source_row="D3.2!18",
        statement="显示缺失的术者、手术室、影像、器械、血库/ICU 能力；现场救援或转诊由临床团队决定",
        judge=_d32_r06,
    ),
    DisplayRule(
        rule_id="D3.2.R07",
        source_row="D3.2!19",
        statement="疑似/明确 rAAA 并行组织复苏、血液准备、手术室与修复策略；不生成具体复苏医嘱或固定目标",
        judge=_d32_r07,
    ),
)


def _d32_gaps(state: GuidelineState) -> tuple[str, ...]:
    """「还差什么才敢比较 EVAR 与 OSR」——只列**真正没有**的项。

    区分「缺数据」与「缺能力」：前 4 项能从 ``VascCase`` 补录，后 4 项是机构
    准备状态，本系统不采集，只提醒术者当场确认。
    """
    gaps: list[str] = []

    if anatomy_available(state) is not True:
        gaps.append("解剖参数不足以比较 EVAR 与 OSR（瘤颈长度/直径/角度）")
    if eq(token(state.case.n.seal_ifu), "符合", "不符") is None:
        gaps.append("EVAR 器械 IFU 未核验（seal_ifu = 未核验/未选型号）")
    if access_blocked(state) is None:
        gaps.append("入路可行性未评估（股髂直径/钙化/迂曲）")
    if _patient_evaluable(state) is None:
        gaps.append("患者是否可完成评估未明确（血压/意识状态）")

    gaps.extend(missing_fields(state, _D32_RESOURCE_FIELDS))
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
    anatomy = anatomy_available(state)
    blocked = access_blocked(state)
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
    if _resource_unavailable(state):
        # 术式本身没被否定——否定的是「本院此刻能不能做」。所以不给 EXCLUDED，
        # 给 MDT：决定转诊还是现场救援是团队的事，不是解剖的事。
        # 不加这一档的后果：终点写「转诊」、候选栏写「EVAR：可行候选」，两句相反。
        return Candidate(
            "EVAR",
            CandidateStatus.MDT,
            "术式本身未被否定，但本院此刻无法实施（团队/手术室/血库均不可立即用）："
            "转诊或现场救援由临床团队决定",
            ("D3.2.R03",),
        )
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
    if _resource_unavailable(state):
        return Candidate(
            "OSR",
            CandidateStatus.MDT,
            "本院此刻无法实施（团队/手术室/血库均不可立即用）：开放解剖与麻醉评估无输入，"
            "转诊或现场救援由临床团队决定",
            ("D3.2.R03",),
        )
    evar_excluded = (
        access_blocked(state) is True or eq(token(state.case.n.seal_ifu), "符合") is False
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
    extra_entries, extra_verdicts = display_entries("D3.2", state, _D32_DISPLAY_RULES)
    outcome = replace(
        outcome,
        verdicts={**outcome.verdicts, **extra_verdicts},
        entries=(*outcome.entries, *extra_entries),
        gaps=_d32_gaps(state),
        candidates=(_evar_candidate(state), _osr_candidate(state)),
    )

    # 本节点的出口恒为终端态（术式方向 + 术者确认），**不向 D3.3 改道**。
    #
    # 规格 D3.3 全表 12 行的 M 列（输出／下一节点）都是「人工决策 / 医生确认 /
    # 终点动作」——没有一条 ``next_node``。它是急诊流程的**终止面**，不是中间站。
    #
    # 曾经在这里加过一条「``D3.GOALS`` 已记录 → ``next_node="D3.3"``」的改写，
    # 那是错的：治疗目标是医生在系统之外做出的决定，不在术前评估数据里
    # （``D3.GOALS`` 的优先来源是「血管团队/上级医师」），系统只能等它进来，
    # 不能主动去取。加那条改写的实际后果是给 D3.3 造了一条规格没有的入边，
    # 并让「已确认不修复」与候选栏的「EVAR：可行候选」并列出现在一份报告里。
    return outcome


# ---------------------------------------------------------------------------
# D3.3 EVAR/OSR/救援及不修复（裁决记录面 · 当前无自动入边）
# ---------------------------------------------------------------------------
#
# ⚠️ **本节点在当前输入范围内不可达**，这是忠实于规格的结果，不是缺陷。
#
# 规格 D3.3 全表 12 行的 M 列（输出／下一节点）都是「人工决策 / 医生确认 /
# 记录理由 / 终点动作/人工处理」，**没有一条 ``next_node``**。它不是流程中的
# 一跳，而是急诊流程的**终止面**：万一哪天判据指向「不可修复」，这里必须由人
# 接手，而不是模型自己下结论。
#
# 五条规则的 G 列在重复同一句话——「不得由模型仅凭年龄或合并症自动判定无修复
# 价值」「未评价不得自动输出不修复」「Agent 不得自动作出不可手术结论」「系统
# 不得自行下达不修复结论」「不得自动判定不修复」。所以本节点是**刹车**，不是
# **方向盘**：它不产生方向，只拦停。
#
# 为什么它拿不到输入：``D3.REPAIR_FEASIBILITY`` 的优先来源是「急诊血管团队」、
# ``D3.GOALS`` 是「血管团队/上级医师」、``D3.NONREPAIR_REASON`` 是
# 「血管团队/MDT/医患沟通」。三者都是**决策产物**，不在术前评估数据里——对比
# G00 的真正病例字段（``D.VITAL`` 监护、``D.LAB`` LIS、``D.CTA`` PACS、
# ``D.SURG`` 手术记录），全是检查与记录，没有一条是「医生的决定」。
#
# 所以本节点：判据写全、单测覆盖，但不接自动入边。等「医生在系统内登记处置
# 决定」的入口建好，再决定用哪种方式接上。在那之前它不进主流程——也正因如此，
# 报告里不会出现「终点说已确认不修复、候选栏说 EVAR 可行」这种自相矛盾。
#
# ⚠️ 一处曾经犯过的错，留在这里防重犯：曾把 R02 正文「必须记录…裁决医师与
# 时间」读成两个输入字段（``D3.ATTEST_ACTOR`` / ``D3.ATTEST_AT``），并让它们
# 驱动 R03/R02 分档。那是越界——R02 的 M 列给系统的动作只有「记录并停止自动
# 术式推荐」，签名是医院流程的事，规格字段表里没有这两个 id。已删除。
_D33_FIELDS: tuple[tuple[str, str], ...] = (
    ("D3.REPAIR_FEASIBILITY", "修复可行性：存在 EVAR/OSR 方案 / 需转诊 / 无可行方案"),
    ("D3.PATIENT_WISH", "患者意愿：有决策能力时的明确表达 / 预先医疗决定"),
    ("D3.CAPACITY", "决策能力评估与合法代理人"),
    ("D3.PROGNOSIS", "获益与预后综合评估"),
    ("D3.GOALS", "治疗目标：积极修复 / 转诊 / 有限治疗 / 舒适照护"),
)

#: ``D3.GOALS`` 里属于「不修复／限定治疗」的两档 —— 只有这两档才需要非修复原因。
_D33_NONREPAIR_GOALS: tuple[str, ...] = ("有限治疗", "舒适照护")

#: ``D3.PATIENT_WISH`` 里表示「患者/代理已明确拒绝」的两档。
_D33_REFUSAL: tuple[str, ...] = ("拒绝特定治疗", "预先医疗决定")

#: ``D3.REPAIR_FEASIBILITY`` 里表示「本中心没有可实施方案」的取值。
_D33_NO_OPTION: str = "无可行方案"

#: ``D3.NONREPAIR_REASON`` 里表示「还没定」的一项 —— 有它就等于没记录原因。
_D33_REASON_UNSET: str = "未确定"


def _d33_value(state: GuidelineState, spec_id: str) -> object:
    """读一个 ``D3.*`` 字段；「未知」这类不确定写法折成 ``None``。"""
    return spec_token(state.node_field(spec_id))


def _d33_reason_recorded(state: GuidelineState) -> Verdict:
    """非修复原因是否写清（多选，且不含「未确定」）。"""
    reasons = multi_field(state, "D3.NONREPAIR_REASON")
    if reasons is None:
        return None
    tokens = [token(value) for value in reasons]
    if not tokens:
        return None
    return _D33_REASON_UNSET not in tokens


def _d33_r03(state: GuidelineState) -> Verdict:
    """P3：非修复/舒适照护计划经完整评估后才输出（``D3.3!12``）。

    判据 = 「这是一个不修复决定」（``R02``）+ 「非修复原因已写清」。

    「五项字段齐全」不在这里重复判：``R90``（P2）先执行，任一缺失就已阻断。
    两处都判会让「缺哪一项」的说明分散在两个地方，而且 R03 的 F 列与 R90 的
    F 列本来就不同（R03 少 ``D3.PROGNOSIS``、多 ``D3.NONREPAIR_REASON``）——
    把「齐不齐」交给 R90、「原因写没写」留给本节点，两个清单各管各的。

    与 ``R02`` 的分界正是**原因是否写清**：写清走 R03（计划完整），没写清走
    R02（决定已记录、原因待补）。声明顺序 R03 在前，构成两档。
    """
    decided = _d33_r02(state)
    if decided is not True:
        return decided
    return _d33_reason_recorded(state)


def _d33_r02(state: GuidelineState) -> Verdict:
    """P3：决定不修复/不转诊 → 记录，并**停止自动术式推荐**（``D3.3!9``）。

    ``D3.GOALS`` 落在「有限治疗／舒适照护」即视为已决定不修复。规格 M 列给
    系统的动作原文就是「记录并**停止自动术式推荐**」——这是本节点存在的实际
    作用，也是它必须与候选栏对齐的原因。

    不把「原因是否写清」放进来：那是 R03 与 R02 的分界，本条只回答「这是不是
    一个不修复决定」。放进来会让两条规则判据重合，R02 永远抢不到。
    """
    goals = _d33_value(state, "D3.GOALS")
    if goals is None:
        return None
    return goals in _D33_NONREPAIR_GOALS


def _d33_r01(state: GuidelineState) -> Verdict:
    """P3：无可行方案或患者/代理拒绝 → 评估转诊、替代处置或舒适照护（``D3.3!8``）。

    它接的是**还没有决定**的那一档：有理由（做不了 / 不愿做），但目标栏还没填。
    此时系统的职责是把可选路径摊开（转诊 / 替代 / 舒适照护），
    而不是替医生选一个——所以出口仍是「授权医生确认」。
    """
    return or_(
        eq(_d33_value(state, "D3.REPAIR_FEASIBILITY"), _D33_NO_OPTION),
        eq(_d33_value(state, "D3.PATIENT_WISH"), *_D33_REFUSAL),
    )


def _d33_r90(state: GuidelineState) -> Verdict:
    """P2：五项规定字段任一未完成 → 不得自动判定不修复（``D3.3!6``）。

    清单取规格 R90 的 F 列（可行性／意愿／能力／预后／目标）。它与 R03 的
    F 列不是同一组：R03 少 ``D3.PROGNOSIS``、多 ``D3.NONREPAIR_REASON``。
    阻断口径用本清单（更严），因为「获益评估没做就不许下不修复结论」是
    ``D3.PROGNOSIS`` J 列的原话。
    """
    return bool(missing_fields(state, _D33_FIELDS))


def _d33_r91(state: GuidelineState) -> Verdict:
    """P3 出口：五项齐备但目标不属不修复类（多为「积极修复」）→ 交术者确认。"""
    return True


def _d33_gaps(state: GuidelineState) -> tuple[str, ...]:
    """``D3.3`` 缺什么 —— 五项规定字段 + **决定不修复时**才追加的留痕提示。

    「裁决医师与时间」在这里只作**提示**，不是字段、不是判据。规格 R02 的
    正文要求「必须记录…裁决医师与时间」，但 M 列给系统的动作只有「记录并停止
    自动术式推荐」——签名是医院流程的事，本系统不采集，只提示补记。
    """
    gaps: list[str] = list(missing_fields(state, _D33_FIELDS))
    if _d33_value(state, "D3.GOALS") in _D33_NONREPAIR_GOALS:
        if _d33_reason_recorded(state) is not True:
            gaps.append(
                "D3.NONREPAIR_REASON（非修复原因：无可行解剖／患者极高风险／"
                "无资源且转诊不可行／患者拒绝／治疗目标／其他）：未记录或仍为「未确定」"
            )
        gaps.append(
            "须在病历中补记裁决医师与时间（规格 D3.3.R02 正文要求；本系统不采集签名，只提示）"
        )
    return tuple(dict.fromkeys(gaps))


def _d32_direction(state: GuidelineState) -> str | None:
    """上一节点（``D3.2``）给出的术式方向 —— 拼进本节点终点，免得丢了。"""
    return next(
        (o.terminal for o in reversed(state.visited) if o.node_id == "D3.2" and o.terminal),
        None,
    )


_D33_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D3.3.R03",
        priority="P3",
        source_row="D3.3!12",
        statement="非修复/舒适照护须经授权医生确认：可行性、获益风险、能力/意愿、转诊可能性"
        "均已评估且不可行原因已记录 → 输出非修复计划",
        judge=_d33_r03,
        exit_kind="terminal",
        exit_value="非修复／舒适照护计划（五项已评估、原因已记录，待授权医生确认）",
    ),
    RuleSlot(
        rule_id="D3.3.R02",
        priority="P3",
        source_row="D3.3!9",
        statement="决定不修复/不转诊：医生已记录治疗目标 → 记录该决定并停止自动术式推荐，"
        "保留急症危险提示；不可行原因仍待补",
        judge=_d33_r02,
        exit_kind="terminal",
        exit_value="不修复决定已记录：停止自动术式推荐，须补非修复原因，并由授权医生确认",
    ),
    RuleSlot(
        rule_id="D3.3.R01",
        priority="P3",
        source_row="D3.3!8",
        statement="本中心无可行方案或患者/代理拒绝 → 评估转诊/救援、替代处置或舒适照护，"
        "由授权医生确认；系统不自行下达不修复结论",
        judge=_d33_r01,
        exit_kind="terminal",
        exit_value="授权医生确认的转诊／替代处置／舒适照护",
    ),
    RuleSlot(
        rule_id="D3.3.R90",
        priority="P2",
        source_row="D3.3!6",
        statement="患者意愿、决策能力、修复可行性或获益评估未完成 → 不得自动判定不修复",
        judge=_d33_r90,
        exit_kind="terminal",
        exit_value="术者确认（须补修复可行性／患者意愿／决策能力／获益评估／治疗目标）",
    ),
    RuleSlot(
        rule_id="D3.3.R91",
        priority="P3",
        source_row="D3.3!5",
        statement="授权医生记录可行性、获益风险、患者/代理意愿与转诊可能性后确认处置",
        judge=_d33_r91,
        exit_kind="terminal",
        exit_value="术者确认（修复/转诊/非修复/舒适照护）",
    ),
)


def d3_3(state: GuidelineState) -> NodeOutcome:
    """D3.3：记录医生已做出的处置决定，并据此停止术式推荐。

    本节点**不产出任何「建议不修复」的结论**——它读的全是医生已经写下的记录，
    唯一加工是把上游（``D3.2``）的术式方向拼进终点，让报告最后一句同时包含
    「定下来的处置」和「此前算出的方向」。

    ⚠️ 当前**没有自动入边**（见模块注释：规格 M 列对 D3.3 每行都是「人工」）。
    这里的实现完整、可测，但只有手动调用或将来接上「登记处置决定」的入口才会
    执行。``_d32_direction`` 也只在那种场景下才有内容可拼。
    """
    outcome = evaluate_node("D3.3", state, _D33_SLOTS)

    terminal = outcome.terminal
    direction = _d32_direction(state)
    if terminal is not None and direction is not None:
        terminal = f"{terminal}；上游术式方向：{direction}"

    return replace(outcome, terminal=terminal, gaps=_d33_gaps(state))


#: 本模块提供的节点（键与规格 sheet 名一致）。
NODES: dict[str, NodeFn] = {
    "D3.1": d3_1,
    "D3.2": d3_2,
    "D3.3": d3_3,
}

#: 已登记的规则 → 规格源行。``D3.1`` 7 条 + ``D3.2`` 8 条 + ``D3.3`` 5 条（共 20）。
#:
#: 可达性分三层，不要笼统说「已实现」：
#:   * ``D3.1.R03/R04`` —— 图可达、**求值不可达**：``class_output`` 含 ``D3``
#:     只在 ``D3_EMERGENCY_RULES``（G03 九条）命中时发生，而那九条**全部**
#:     落在 ``_D3_CONFIRMED`` 或 ``_D3_SUSPECTED`` 里 —— 即 ``R01``/``R02``
#:     必有一条先命中（同为 P0-A、声明在前）。判据是真判据，不是占位；
#:     只在手动调用或 R01/R02 覆盖范围收缩时执行。
#:   * ``D3.3.R01–R03`` —— 判据完整、**图不可达**：规格 M 列对 D3.3 每行都是
#:     「人工」，没有入边。见该节模块注释。
#:   * 其余 14 条 —— 三层全通，由端到端用例覆盖。
RULE_ROWS: dict[str, str] = {
    **{slot.rule_id: slot.source_row for slot in (*_D31_SLOTS, *_D32_SLOTS, *_D33_SLOTS)},
    **{rule.rule_id: rule.source_row for rule in _D32_DISPLAY_RULES},
}
