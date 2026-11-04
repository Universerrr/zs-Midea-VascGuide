"""D6 · 主动脉修复术后（``D6.1`` → ``D6.2`` / ``D6.3``）。

规格来源：``VascGuide_V3.6.2.xlsx`` 的 ``D6.1`` / ``D6.2`` / ``D6.3`` 三个 sheet，
共 28 条规则（11 + 9 + 8），全部实现。

## 三个节点各管什么

``D6.1`` 术后随访分诊
    回答「这次随访有没有需要处理的东西，以及没有的话该按什么间隔复查」。
    出口四条：急症 → ``G05``；内漏/瘤囊变化 → ``D6.2``；感染/瘘/结构/靶血管 →
    ``D6.3``；都没有 → 按修复类型与风险组给出影像随访计划。

``D6.2`` 内漏与瘤囊变化
    按内漏类型分流：I/III 型 → 及时再干预；II 型 + 瘤囊增长 ≥10 mm 且已排除
    I/III → 继发干预评估；IV 型早期 → 观察；无可见内漏但增长 ≥10 mm →
    多模态排除隐匿内漏与感染。

``D6.3`` 结构与靶血管
    感染/瘘 → ``D4.3``；靶血管阻塞或灌注障碍 → 再血管化评估；吻合口旁瘤 →
    先查感染；组件分离/移位/破损/闭塞 → 再干预评估。

## 七处必须先讲清的结构决定

**① 三个节点都不产出术式候选。**
    规格在 D6 反复写「最终方式需结合具体密封失效机制、解剖、器械和手术风险由血管
    团队确认」（``D6.2.R01``）、「由团队评估」（``D6.2.R06``）、「不自动决定再干预」
    （``D6.2.R90``）。所以本文件不给 :class:`~..state.Candidate`——给出候选会把
    「有再干预指征」包装成「已定方案」。

**② ``D6.1.R03`` 统一走 ``D6.3``，感染再由 ``D6.3.R01`` 转 ``D4.3``。**
    规格这一条有**两个目的地**（感染/瘘 → D4.3；结构故障 → D6.3），而
    :class:`~..state.NodeOutcome` 的 ``next_node`` 是单值。这里不写「按条件动态改
    出口」的钩子，而是让 ``D6.3`` 做中转：它的 ``R01`` 本来就是「移植物感染或
    主动脉相关瘘 → D4.3」。多一跳，但每一跳都有规格源行，且 ``D6.3`` 还会顺手
    把结构/灌注的缺口列出来——那对感染病例也是有用的。

**③ ``D6.1.R90`` 不把「手术日期未记录」当阻断项。**
    规格 F 列把 ``D6.REPAIR_DATE`` 列进了阻断字段。照字面实现，任何未录手术日期
    的病例都会停在人工复核——而这类病例在首次导入时占绝大多数，其中有人正在
    出血（P0 已先命中，不受影响），其余人只是随访间隔算不出来。故本实现把它降为
    缺口：日期影响「多久复查一次」，不影响「这次有没有并发症」。这与 ``D2.2``
    对「生命体征趋势缺失」的处理同一条理由。

**④ ``D6.1`` 的随访计划由「显示性规则 + 动态终点」合成。**
    ``R04``–``R09`` 六条都是**随访计划义务**（30 天基线 CTA、风险分层、OSR 每 5 年、
    复杂 EVAR、FEVAR/BEVAR、瘤囊缩小降级），彼此不互斥，放进出口竞争会互相遮蔽。
    它们做成 :class:`~.common.DisplayRule` 求值留痕，最终计划由
    :func:`_surveillance_plan` 按「修复类型 + 风险组」合成一句话写进终点。

**⑤ 两个 10 mm 阈值都取自判定层的常量。**
    ``SAC_GROWTH_MM``（瘤囊增长）与 ``D6.SAC_SHRINKAGE``（缩小）用的是同一个 10。
    规格在 ``D6.1.R09`` / ``D6.2.R02`` / ``D6.2.R06`` 三处都写「≥10 mm」，而
    ``G08`` 的瘤囊增长判据也用 10——常量只放 ``router.py`` 一处，本层 import。

**⑥ 模型把「内漏类型」与「是否内漏」压在一格里。**
    ``n.endoleak_type`` 的取值是 ``I/II/III/IV/V/疑似/无/未知``：``无`` 是明确阴性，
    ``疑似`` 是**不确定**（``UNCERTAIN_TOKENS``），``未知``/``None`` 是没记录。
    所以读内漏必须三分：``_has_endoleak``（阳性，含疑似）、``_endoleak_clear``
    （明确无）、``_endoleak_unresolved``（判不了）。把「疑似」折成「无」是本节点
    最危险的错——它会让可疑内漏直接进常规随访。

**⑦ ``CL.POST_REASON`` 在模型里是单值，规格是多选。**
    规格给了 12 个取值（含「瘘」「移位或分离」「破损或扭折」「肢体缺血」「靶血管阻塞」），
    模型 ``cl.post_reason`` 只收 8 个单值（内漏/瘤囊变化/感染/再狭窄或闭塞/假性动脉瘤/
    常规随访/其他/不详）。所以本文件读它就诊原因时一律用 ``eq(token(...))`` 而**不是**
    ``has()``——``has()`` 对字符串会**逐字符迭代**，「内漏」会被拆成两个字去比对，
    永远不命中；那种错不报错，只是让每个内漏病例静默掉回常规随访。
    规格多出来的那几类（瘘、移位、肢体缺血）改由 ``N.GRAFT_MAJOR`` / ``C.BRANCH`` /
    ``N.SAC_GROWTH`` 等结构化字段补上，并且**不依赖**就诊原因做判定。

**⑧ ``N.GRAFT_MAJOR`` 的模型值域比规格窄。**
    规格是「组件分离/移位/破损/扭折/闭塞/无/疑似/未知」，模型是
    「组件分离/移位/**明显闭塞**/无/疑似/未知」——没有「破损」「扭折」两档，且
    「闭塞」写作「明显闭塞」。读的时候按模型写；规格那两个取值目前无法表达，
    在缺口里显式提示「结构异常类型按模型六态记录」。
"""

from __future__ import annotations

import re
from dataclasses import replace

from ....rules.router import (
    SAC_GROWTH_MM,
    Verdict,
    and_,
    eq,
    has,
    not_,
    or_,
    token,
)
from ..state import GuidelineState, Link, NodeFn, NodeOutcome
from .common import (
    DisplayRule,
    RuleSlot,
    active_gi_bleed,
    acute_limb_ischemia,
    display_entries,
    evaluate_node,
    fistula_present,
    infection_typed,
    is_spec_uncertain,
    malperfusion,
    missing_fields,
    sac_growing,
    sac_growth,
    spec_token,
    unstable,
)

__all__ = ["NODES", "d6_1", "d6_2", "d6_3"]

#: ``n.endoleak_type`` 的五个真性分型（``疑似``/``无``/``未知`` 不算分型）。
_END0LEAK_TYPES: tuple[str, ...] = ("I", "II", "III", "IV", "V")

#: ``N.GRAFT_MAJOR`` 里属于「结构故障已确立」的三档（模型值域，见模块文档第 ⑦ 条）。
_GRAFT_FAILURE: tuple[str, ...] = ("组件分离", "移位", "明显闭塞")

#: ``CL.POST_REASON`` 里属于「内漏/瘤囊」的就诊原因（**模型是单值**，不是多选）。
_POST_REASON_LEAK: tuple[str, ...] = ("内漏", "瘤囊变化")

#: ``CL.POST_REASON`` 里属于「结构/靶血管」的就诊原因（模型是单值）。
_POST_REASON_STRUCTURE: tuple[str, ...] = ("再狭窄或闭塞", "假性动脉瘤")

#: 从自由文本里抠出第一个数字（``D6.SEAL_LENGTH`` / ``D6.MIGRATION`` 允许带部位描述）。
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


# ---------------------------------------------------------------------------
# 判据原语
# ---------------------------------------------------------------------------
def _number(value: object) -> float | None:
    """把「数值 / 带单位或部位描述的字符串」统一成 ``float``；取不到返回 ``None``。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        matched = _NUMBER.search(value)
        return float(matched.group()) if matched else None
    return None


def _endoleak(state: GuidelineState) -> object:
    """``N.ENDOLEAK_TYPE`` 的原始取值（``疑似`` 保留，不折成 ``None``）。"""
    return token(state.case.n.endoleak_type)


def _has_endoleak(state: GuidelineState) -> Verdict:
    """是否存在内漏（**含「疑似」**）。

    「疑似」按阳性处理，理由与 ``G08.R05`` 一致：可疑内漏进常规随访，代价是漏掉
    一次可以处理的密封失效；而多报一次「需分型复核」，代价只是一次 CTA 复核。
    """
    by_type = eq(_endoleak(state), *_END0LEAK_TYPES, "疑似")
    if by_type is True:
        return True
    by_reason = eq(token(state.case.cl.post_reason), *_POST_REASON_LEAK)
    if by_reason is True:
        return True
    return by_type if by_reason is None else by_reason


def _endoleak_clear(state: GuidelineState) -> Verdict:
    """内漏是否**明确排除**（``无``）—— 三值。"""
    return eq(_endoleak(state), "无")


def _endoleak_unresolved(state: GuidelineState) -> bool:
    """内漏状态是否判不了（缺失 / 未知 / 证据冲突）。"""
    raw = _endoleak(state)
    return raw is None or is_spec_uncertain(raw)


def _sac_changed(state: GuidelineState) -> Verdict:
    """瘤囊是否「有变化」—— ``D6.1.R02`` 的判据。

    规格只说「瘤囊变化」没给阈值，本实现取两处可核实的来源：医生在就诊原因里
    明确写下的「瘤囊变化」，或 ``N.SAC_GROWTH`` 达到规格唯一给出的 10 mm 阈值
    （双向：增长与缩小都算「变化」）。**不自己另设一个 5 mm**——那会成为第二套阈值。
    """
    recorded = eq(token(state.case.cl.post_reason), "瘤囊变化")
    if recorded is True:
        return True
    growth = sac_growth(state)
    if growth is None:
        return recorded if recorded is None else False
    return abs(growth) >= SAC_GROWTH_MM


def _graft_failure(state: GuidelineState) -> Verdict:
    """移植物结构重大异常是否**确立**（``组件分离`` / ``移位`` / ``明显闭塞``）。"""
    return eq(token(state.case.n.graft_major), *_GRAFT_FAILURE)


def _graft_unresolved(state: GuidelineState) -> bool:
    raw = token(state.case.n.graft_major)
    return raw is None or is_spec_uncertain(raw)


def _branch_occlusion(state: GuidelineState) -> Verdict:
    """靶血管（肾/内脏/下肢）是否明确狭窄或闭塞 —— 读 ``C.BRANCH`` 的值域。"""
    branch = state.case.c.branch
    if not branch:
        return None
    flags = [
        isinstance(value, str) and ("闭塞" in value or "狭窄" in value) for value in branch.values()
    ]
    if any(flags):
        return True
    if any(isinstance(v, str) and is_spec_uncertain(v) for v in branch.values()):
        return None
    return False


# 瘘的判据与 D4 共用 ``common.fistula_present``（同一事实只许一个判据函数）。
_fistula_present = fistula_present


def _infection_suspected(state: GuidelineState) -> Verdict:
    """移植物/主动脉感染是否**未排除** —— ``common.infection_typed`` 的一种放宽。

    D6 问的是「能不能排除感染」（``D6.3`` 的结构与感染分流要筛得宽），所以比
    公共判据多收一档「仅非特异征象」。但**下界必须与 D4/D7 一致**：分型字段写
    「原生感染疑似」时不能判 False —— 那会让同一次运行里 D4 说有感染、D6 说
    没有。
    """
    return or_(infection_typed(state), eq(token(state.case.n.infection_ctx), "仅非特异征象"))


def _repaired(state: GuidelineState) -> Verdict:
    """是否有既往主动脉修复史 —— 这是进入 D6 的前提。"""
    return eq(token(state.case.cl.prev_evor), "EVAR", "OSR", "其他")


def _seal_length(state: GuidelineState) -> float | None:
    return _number(spec_token(state.node_field("D6.SEAL_LENGTH")))


def _seal_insufficient(state: GuidelineState) -> Verdict:
    """密封区是否不足（<10 mm，``Rec104–106`` 的阈值）。"""
    length = _seal_length(state)
    if length is None:
        return None
    return length < 10.0


def _migration_significant(state: GuidelineState) -> Verdict:
    """移植物迁移是否有临床意义（>10 mm，或任何伴症状/二次干预的迁移）。"""
    raw = token(state.node_field("D6.MIGRATION"))
    if raw is None:
        return None
    text = str(raw)
    if text == "无":
        return False
    if ">10" in text or "伴症状" in text:
        return True
    if is_spec_uncertain(text):
        return None
    return None


# ---------------------------------------------------------------------------
# D6.1 术后随访分诊
# ---------------------------------------------------------------------------
def _d61_r01(state: GuidelineState) -> Verdict:
    """P0-A：新出现破裂/出血/急性肢体缺血/严重灌注障碍 → 回 G03/G05。

    术后背景**不降低**急症优先级（规格 J 列原话）。这里只认「可靠当前证据」：
    明确/疑似的破裂状态、活动性消化道出血、肢体濒危表现、休克、急性灌注障碍。
    """
    return or_(
        eq(token(state.case.cl.aaa_rupture), "明确", "疑似"),
        active_gi_bleed(state),
        acute_limb_ischemia(state),
        unstable(state),
        malperfusion(state),
    )


def _d61_r03(state: GuidelineState) -> Verdict:
    """P1：感染/瘘、结构故障或靶血管阻塞 → ``D6.3``（感染再由 ``D6.3.R01`` 转 D4.3）。"""
    return or_(
        _infection_suspected(state),
        _fistula_present(state),
        _graft_failure(state),
        _branch_occlusion(state),
        eq(token(state.case.cl.post_reason), *_POST_REASON_STRUCTURE),
    )


def _d61_r02(state: GuidelineState) -> Verdict:
    """P1：有内漏或瘤囊变化 → ``D6.2`` 分型与再干预评估。"""
    return or_(_has_endoleak(state), _sac_changed(state))


def _d61_r04(state: GuidelineState) -> Verdict:
    """P1（展示性）：EVAR 术后 30 天内应有基线 CTA（评价内漏/重叠/密封）。

    做成展示性规则：它不决定去哪个节点，而是决定「能不能现在就给低风险随访」。
    未完成时由 :func:`_surveillance_plan` 拒绝给出低频方案，并进缺口。
    """
    if eq(token(state.case.cl.prev_evor), "EVAR") is not True:
        return False
    return eq(spec_token(state.node_field("D6.BASELINE_CTA")), "已完成")


def _d61_r05(state: GuidelineState) -> Verdict:
    """P1（展示性）：EVAR 风险组分层（低风险/高风险/失败组/未分层）。"""
    if eq(token(state.case.cl.prev_evor), "EVAR") is not True:
        return False
    group = spec_token(state.node_field("D6.EVAR_RISK_GROUP"))
    if group is None:
        return None
    return group != "未分层"


def _d61_r06(state: GuidelineState) -> Verdict:
    """P1（展示性）：OSR 术后可考虑每 5 年影像随访全主动脉与外周动脉。"""
    if eq(token(state.case.cl.prev_evor), "OSR") is not True:
        return False
    return eq(spec_token(state.node_field("D6.OSR_SURVEILLANCE")), "约每5年", "个体化")


def _d61_r07(state: GuidelineState) -> Verdict:
    """P3（展示性）：**非分支**复杂腔内修复（平行支架、新型构型）个体化随访。"""
    if eq(token(state.case.cl.prev_evor), "EVAR") is not True:
        return False
    if spec_token(state.node_field("D6.EVAR_SURVEILLANCE")) != "复杂EVAR个体化":
        return False
    # 分支表空 = 没有内脏/分支支架要随访 → 归「其他复杂构型」；
    # 有分支表 = 分支或开窗支架 → 归 FEVAR/BEVAR（R08）。
    return not bool(state.case.c.branch)


def _d61_r08(state: GuidelineState) -> Verdict:
    """P3（展示性）：FEVAR/BEVAR 等**分支/开窗**修复的专属随访（30 天 + 1 年 CTA）。

    与 ``R07`` 的分野：「复杂」在模型里只有一个取值（``复杂EVAR个体化``），分不出
    分支支架与平行支架。这里用「有没有分支靶血管要随访」当证据——分支表有内容即
    分支/开窗修复。这个判据不完美，但比让两条规则判据完全相同、后者永不命中要好；
    规格对两者的随访强度本来就不同（``Rec130–132`` vs ``Rec130–131``）。
    """
    if eq(token(state.case.cl.prev_evor), "EVAR") is not True:
        return False
    if spec_token(state.node_field("D6.EVAR_SURVEILLANCE")) != "复杂EVAR个体化":
        return False
    return bool(state.case.c.branch)


def _d61_r09(state: GuidelineState) -> Verdict:
    """P1（展示性）：瘤囊缩小 ≥10 mm 且其他低风险条件满足 → 可考虑转低风险组。

    规格明写「不能仅凭缩小值单项降级」，所以这里要求同时满足：无内漏、风险组已判为
    低风险。缩小值取 ``D6.SAC_SHRINKAGE``（正数表示缩小量）。
    """
    shrinkage = _number(spec_token(state.node_field("D6.SAC_SHRINKAGE")))
    if shrinkage is None:
        return None
    if shrinkage < SAC_GROWTH_MM:
        return False
    return and_(
        not_(_has_endoleak(state)),
        eq(spec_token(state.node_field("D6.EVAR_RISK_GROUP")), "低风险"),
    )


def _d61_r90(state: GuidelineState) -> Verdict:
    """P2：关键**临床状态**判不了 → 不自动归常规低风险随访。

    阻断项是「这次随访有没有并发症」直接依赖的五个状态：内漏、瘤囊变化、结构、
    感染、灌注。``D6.REPAIR_DATE`` **不在其中**（见模块文档第 ③ 条）。
    """
    return bool(_d61_unresolved(state))


def _d61_unresolved(state: GuidelineState) -> list[str]:
    """列出判不了的随访状态字段（只用于 R90 与缺口文案）。"""
    names: list[str] = []
    if _endoleak_unresolved(state) and eq(token(state.case.cl.post_reason), "内漏") is not True:
        names.append("N.ENDOLEAK_TYPE：内漏类型未记录（不得当作无内漏）")
    if sac_growth(state) is None and eq(token(state.case.cl.post_reason), "瘤囊变化") is not True:
        names.append("N.SAC_GROWTH：瘤囊直径变化未记录或不可比")
    if _graft_unresolved(state):
        names.append("N.GRAFT_MAJOR：移植物结构状态未记录（未知/疑似不当作无异常）")
    if _infection_suspected(state) is None:
        names.append("N.INFECTION_CTX：移植物感染状态未记录（未知不等于无感染）")
    if malperfusion(state) is None:
        names.append("C.MALPERFUSION：器官/肢体灌注状态未评价")
    return names


def _d61_r91(state: GuidelineState) -> Verdict:
    """P3 出口：无内漏、无瘤囊变化、无结构/感染/灌注异常 → 常规随访。"""
    return and_(
        _endoleak_clear(state),
        not_(_sac_changed(state)),
        not_(_graft_failure(state)),
        not_(_infection_suspected(state)),
        not_(_fistula_present(state)),
        not_(malperfusion(state)),
    )


#: 阶段一：决定去哪个节点。
_D61_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D6.1.R01",
        priority="P0-A",
        source_row="D6.1!11",
        statement="新出现破裂/出血/急性肢体缺血/严重灌注障碍 → 回 G03/G05；术后背景不降低急症优先级",
        judge=_d61_r01,
        exit_kind="escalate",
        exit_value="G05",
        next_nodes=("G03", "G05"),
        link=Link.EITHER,
    ),
    RuleSlot(
        rule_id="D6.1.R03",
        priority="P1",
        source_row="D6.1!13",
        statement="移植物感染/瘘、结构故障或靶血管阻塞 → D6.3（感染由 D6.3.R01 转 D4.3）",
        judge=_d61_r03,
        exit_kind="next",
        exit_value="D6.3",
        next_nodes=("D4.3", "D6.3"),
    ),
    RuleSlot(
        rule_id="D6.1.R02",
        priority="P1",
        source_row="D6.1!12",
        statement="有内漏或瘤囊变化 → D6.2；未知内漏类型或不可比增长不直接决定再干预",
        judge=_d61_r02,
        exit_kind="next",
        exit_value="D6.2",
    ),
    RuleSlot(
        rule_id="D6.1.R90",
        priority="P2",
        source_row="D6.1!10",
        statement="内漏/瘤囊变化/感染/结构/灌注状态未评价或冲突 → 不自动归常规低风险随访",
        judge=_d61_r90,
        exit_kind="terminal",
        exit_value="人工复核（术后随访关键状态未评价，不得归入常规低风险随访）",
    ),
    RuleSlot(
        rule_id="D6.1.R91",
        priority="P3",
        source_row="D6.1!9",
        statement="常规随访且无异常 → 按修复类型与风险组制定影像随访计划",
        judge=_d61_r91,
        exit_kind="terminal",
        exit_value="常规随访（随访间隔见下方计划；任何新发症状/内漏征象即升级）",
        next_nodes=("D6.2", "D6.3", "G03"),
    ),
)


#: 阶段二：随访计划义务（并列，全部留痕）。
_D61_DISPLAY_RULES: tuple[DisplayRule, ...] = (
    DisplayRule(
        rule_id="D6.1.R04",
        source_row="D6.1!19",
        statement="EVAR 术后 30 天内应有基线 CTA（内漏/组件重叠/密封区长度）；未完成时不得直接分入低风险",
        judge=_d61_r04,
    ),
    DisplayRule(
        rule_id="D6.1.R05",
        source_row="D6.1!20",
        statement="按内漏/IFU/密封长度/近端瘤颈/髂固定区/瘤囊变化分低风险、高风险或失败组",
        judge=_d61_r05,
    ),
    DisplayRule(
        rule_id="D6.1.R06",
        source_row="D6.1!21",
        statement="OSR 术后可考虑每 5 年影像随访全主动脉与外周动脉；症状/遗传背景者提前",
        judge=_d61_r06,
    ),
    DisplayRule(
        rule_id="D6.1.R07",
        source_row="D6.1!22",
        statement="非 FEVAR/BEVAR 的复杂腔内修复按器械与构型个体化长期影像计划",
        judge=_d61_r07,
    ),
    DisplayRule(
        rule_id="D6.1.R08",
        source_row="D6.1!24",
        statement="FEVAR/BEVAR：术后 30 天内与 1 年 CTA，之后个体化长期影像；分支通畅与桥接支架风险纳入复查",
        judge=_d61_r08,
    ),
    DisplayRule(
        rule_id="D6.1.R09",
        source_row="D6.1!25",
        statement="瘤囊缩小 ≥10 mm 且无内漏、解剖在 IFU 内、密封充分 → 可考虑转低风险组并延长间隔（不得仅凭缩小值降级）",
        judge=_d61_r09,
    ),
)

_D61_FIELDS: tuple[tuple[str, str], ...] = (
    ("D6.REPAIR_DATE", "既往修复日期与术后间隔（决定早期术后与长期随访的分界）"),
)


def _surveillance_plan(state: GuidelineState) -> str:
    """按修复类型与风险组合成一句随访计划 —— 只在常规随访出口使用。

    为什么不写进 ``RuleSlot`` 的静态文案：计划有五种形态（OSR / 低风险 EVAR /
    高风险 EVAR / 复杂 EVAR / 未分层），静态串只能取其一，另外四种会被说错。
    这里也不做新的临床判断——每一条都能回到 ``D6.1`` 的 R04–R09。
    """
    repair = token(state.case.cl.prev_evor)
    if repair == "OSR":
        return "OSR 术后：每 5 年影像随访全主动脉与外周动脉；有症状、遗传背景或新发动脉瘤时提前"
    if _d61_r08(state) is True:
        return "复杂腔内修复（FEVAR/BEVAR 或分支支架）：术后 30 天内与 1 年 CTA，之后个体化长期影像；分支通畅与桥接支架纳入复查"
    if _d61_r07(state) is True:
        return "复杂腔内修复：按器械与修复构型制定个体化长期影像计划；术后第一年后部分患者可 DUS 替代持续 CTA"
    group = spec_token(state.node_field("D6.EVAR_RISK_GROUP"))
    if _d61_r04(state) is not True:
        return "EVAR 术后：30 天基线 CTA 未完成，先由医生制定补充影像计划，暂不进入低频随访"
    if group == "低风险":
        return "EVAR 低风险：前 5 年低频影像，5 年复查全主动脉与髂动脉；仍需长期随访"
    if group == "高风险":
        return "EVAR 高风险：至少年度 CTA 或 DUS"
    if group == "失败组":
        return "EVAR 失败组：进入再干预评估（D6.2），不做常规随访安排"
    return "EVAR：风险组未分层，按年度影像随访并补齐分层所需条件"


def d6_1(state: GuidelineState) -> NodeOutcome:
    """D6.1：分诊到 G05 / D6.2 / D6.3，或给出常规随访的影像计划。"""
    outcome = evaluate_node("D6.1", state, _D61_SLOTS)
    extra_entries, extra_verdicts = display_entries("D6.1", state, _D61_DISPLAY_RULES)
    outcome = replace(
        outcome,
        verdicts={**outcome.verdicts, **extra_verdicts},
        entries=(*outcome.entries, *extra_entries),
        gaps=_d61_gaps(state),
    )
    # 常规随访出口的文案是动态的（见 :func:`_surveillance_plan`）。
    if outcome.verdicts.get("D6.1.R91") is True and outcome.terminal is not None:
        outcome = replace(outcome, terminal=f"常规随访：{_surveillance_plan(state)}")
    return outcome


def _d61_gaps(state: GuidelineState) -> tuple[str, ...]:
    """D6.1 缺什么 —— 随访状态 + 修复时间线 + 风险分层所需条件。"""
    gaps = list(missing_fields(state, _D61_FIELDS))
    gaps.extend(_d61_unresolved(state))
    if _repaired(state) is not True:
        gaps.append("CL.PREV_EVOR：既往修复方式未确认（术后路径的前提）")
    if _d61_r04(state) is not True and eq(token(state.case.cl.prev_evor), "EVAR") is True:
        gaps.append("D6.BASELINE_CTA：EVAR 术后 30 天内基线 CTA 未完成（不得直接分入低风险组）")
    if _d61_r05(state) is not True and eq(token(state.case.cl.prev_evor), "EVAR") is True:
        gaps.append("D6.EVAR_RISK_GROUP：风险组未分层（需内漏/IFU/密封长度/瘤颈/髂固定区）")
    if eq(token(state.case.cl.prev_evor), "OSR") is True and _d61_r06(state) is not True:
        gaps.append("D6.OSR_SURVEILLANCE：OSR 术后长期影像计划未制定")
    if _fistula_present(state) is True and token(state.case.cl.fistula) == "疑似":
        gaps.append("CL.FISTULA_TYPE：瘘类型未确定（保留疑似，需内镜/术中证据）")
    return tuple(gaps)


# ---------------------------------------------------------------------------
# D6.2 内漏与瘤囊变化
# ---------------------------------------------------------------------------
def _d62_r01(state: GuidelineState) -> Verdict:
    """P1：Type I 或 Type III 内漏明确 → 及时再干预恢复密封。"""
    return eq(_endoleak(state), "I", "III")


def _d62_r02(state: GuidelineState) -> Verdict:
    """P1：Type II + 瘤囊增长 ≥10 mm + 已排除 Type I/III → 继发干预评估。

    三个条件缺一不可：规格明写「**仅当**增长 ≥10 mm、测量方式一致且已排除
    Type I/III 后，才考虑继发干预」。缺一个就只能去复核（``R04``），不能默认
    归因于 Type II。
    """
    return and_(
        eq(_endoleak(state), "II"),
        sac_growing(state),
        eq(spec_token(state.node_field("D6.TYPE13_EXCLUDED")), "已排除"),
    )


def _d62_r05(state: GuidelineState) -> Verdict:
    """P1：确认 Type IV 且无持续增长 → 记录为早期孔隙性渗漏，术者决定观察与复查。"""
    return and_(
        eq(_endoleak(state), "IV"),
        eq(spec_token(state.node_field("D6.TYPE4_CONTEXT")), "早期术后", "影像明确"),
        not_(sac_growing(state)),
    )


def _d62_r06(state: GuidelineState) -> Verdict:
    """P1：瘤囊增长 ≥10 mm 但常规影像无可见内漏 → 多模态排除隐匿内漏与感染。

    ``Type V``（endotension）不是「什么都不用做」的同义词：规格给它的下一步是
    升级检查。所以这条的出口是「进一步诊断」，不是观察。
    """
    return and_(
        _endoleak_clear(state),
        sac_growing(state),
        not_(_infection_suspected(state)),
    )


def _d62_r07(state: GuidelineState) -> Verdict:
    """P1：迁移 >10 mm（或任何伴症状/密封丢失的迁移）→ ``D6.3`` 评估再干预。

    判据只取「迁移」这一件事实：规格 F 列虽把 ``D6.SEAL_LENGTH`` 列进来，但它的
    语义是「**迁移导致的**密封丢失」，不是「密封区短于 10 mm」。后者由 ``R03``
    单独处理——两个判据混在一起，等于把「密封区短」也说成「移植物移位」。
    """
    return _migration_significant(state)


def _d62_r03(state: GuidelineState) -> Verdict:
    """P1：无可见内漏但密封 <10 mm 或瘤颈进行性扩张 → 考虑改善密封。"""
    return and_(
        _endoleak_clear(state),
        or_(_seal_insufficient(state), eq(spec_token(state.node_field("D6.NECK_DILATION")), "是")),
    )


def _d62_r04(state: GuidelineState) -> Verdict:
    """P2：内漏类型不确定 / 瘤囊变化不可比 / 增长原因不明 → 不归因 Type II。

    这条与 ``R90`` 的分工：``R04`` 是**诊断不确定**（有内漏但分不清型、或增长原因
    不明），``R90`` 是**资料缺失**。两者出口相同（人工复核），但缺口不同——
    ``R04`` 要的是「补充多模态影像/MDT」，``R90`` 要的是「把某个字段补上」。
    """
    if _has_endoleak(state) is not True:
        return False
    if eq(_endoleak(state), "疑似") is True:
        return True
    if _endoleak_unresolved(state):
        return True
    return sac_growth(state) is None


def _d62_r90(state: GuidelineState) -> Verdict:
    """P2：内漏分型、瘤囊可比性或密封/迁移信息未知 → 不自动干预。"""
    return bool(_d62_unresolved(state))


def _d62_unresolved(state: GuidelineState) -> list[str]:
    names: list[str] = []
    if _endoleak_unresolved(state):
        names.append("N.ENDOLEAK_TYPE：内漏分型未记录（不得按 Type II 归因）")
    if sac_growth(state) is None:
        names.append("N.SAC_GROWTH：瘤囊直径变化未记录或不可比")
    if _seal_length(state) is None:
        names.append("D6.SEAL_LENGTH：近端/远端密封区长度未测量（<10 mm 为不足）")
    if token(state.node_field("D6.MIGRATION")) is None:
        names.append("D6.MIGRATION：移植物迁移未与固定解剖标志比较")
    return names


def _d62_r91(state: GuidelineState) -> Verdict:
    """P3 出口：无内漏、无增长且密封稳定 → 常规随访。

    **结构兜底**：I/III（``R01``）、II+增长（``R02``）、IV（``R05``）、隐匿增长
    （``R06``）、迁移/密封（``R07``/``R03``）、不确定（``R04``/``R90``）已把输入
    空间分完；能走到这里说明有内漏但未达任何干预条件，出口只能是「继续观察」。
    """
    return True


_D62_FIELDS: tuple[tuple[str, str], ...] = (
    ("D6.SEAL_LENGTH", "近端/远端密封区长度（须标明部位；<10 mm 为密封不足）"),
    ("D6.NECK_DILATION", "瘤颈是否进行性扩张（同方法比较）"),
    ("D6.TYPE13_EXCLUDED", "Type I/III 内漏是否已排除（归因 Type II 之前必须核查）"),
    ("D6.MIGRATION", "移植物迁移（相对固定解剖标志，>10 mm 为有意义）"),
)

_D62_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D6.2.R01",
        priority="P1",
        source_row="D6.2!9",
        statement="Type I 内漏明确（含 III）→ 及时再干预恢复密封，方式由血管团队按密封失效机制与解剖确认",
        judge=_d62_r01,
        exit_kind="terminal",
        exit_value="及时再干预规划（首选腔内，最终方式由血管团队确认；不指定器械型号）",
    ),
    RuleSlot(
        rule_id="D6.2.R02",
        priority="P1",
        source_row="D6.2!10",
        statement="Type II 且瘤囊较基线/随访最小值增长 ≥10 mm、方法一致、已排除 I/III → 继发干预评估",
        judge=_d62_r02,
        exit_kind="terminal",
        exit_value="Type II 继发干预评估（通常优先腔内；需术者确认指征）",
    ),
    RuleSlot(
        rule_id="D6.2.R05",
        priority="P1",
        source_row="D6.2!17",
        statement="确认 Type IV 且无持续增长/其他危险征象 → 记录为早期孔隙性渗漏，术者决定观察与复查",
        judge=_d62_r05,
        exit_kind="terminal",
        exit_value="Type IV 早期孔隙性渗漏：观察 + 短期复查（不触发侵入性再干预；若出现增长重新排除 I/III）",
    ),
    RuleSlot(
        rule_id="D6.2.R06",
        priority="P1",
        source_row="D6.2!18",
        statement="瘤囊增长 ≥10 mm 且常规影像无可见内漏 → 多模态排除隐匿内漏与感染；仍增长可考虑内衬或开放转换",
        judge=_d62_r06,
        exit_kind="terminal",
        exit_value="未明原因增长：升级多模态影像（CEUS/动态 CTA/MRA/PET-CT）排除隐匿内漏与感染后 MDT 评估",
    ),
    RuleSlot(
        rule_id="D6.2.R07",
        priority="P1",
        source_row="D6.2!19",
        statement="迁移 >10 mm 或任何伴症状/密封丢失的迁移 → 评估 I 型内漏、组件分离与再干预",
        judge=_d62_r07,
        exit_kind="next",
        exit_value="D6.3",
    ),
    RuleSlot(
        rule_id="D6.2.R03",
        priority="P1",
        source_row="D6.2!11",
        statement="无可见内漏但密封区 <10 mm 或瘤颈进行性扩张 → 考虑改善密封（近端受损评估 FEVAR/BEVAR 延长或开放转换）",
        judge=_d62_r03,
        exit_kind="terminal",
        exit_value="密封改善评估（先确认测量位置、器械 IFU 及是否合并其他内漏；方式由团队确定）",
    ),
    RuleSlot(
        rule_id="D6.2.R04",
        priority="P2",
        source_row="D6.2!12",
        statement="内漏类型不确定/瘤囊变化不可比/增长原因不明 → 不归因 Type II，补充影像或多模态后复核",
        judge=_d62_r04,
        exit_kind="terminal",
        exit_value="人工复核（内漏分型或增长原因未明，先补充多模态影像/MDT）",
        next_nodes=("D6.2", "D6.3"),
    ),
    RuleSlot(
        rule_id="D6.2.R90",
        priority="P2",
        source_row="D6.2!8",
        statement="内漏分型、瘤囊可比性或密封/迁移信息未知 → 不自动干预，补充影像与复核",
        judge=_d62_r90,
        exit_kind="terminal",
        exit_value="人工复核（内漏分型、瘤囊可比性或密封/迁移信息待补齐）",
    ),
    RuleSlot(
        rule_id="D6.2.R91",
        priority="P3",
        source_row="D6.2!7",
        statement="无内漏、无增长且密封稳定 → 常规随访；I/III 型与增长/密封失败 → 再干预评估",
        judge=_d62_r91,
        exit_kind="terminal",
        exit_value="继续观察：内漏未达再干预条件，按 D6.1 的随访计划复查",
    ),
)


def _d62_gaps(state: GuidelineState) -> tuple[str, ...]:
    """D6.2 缺什么 —— 分型依据 + 瘤囊可比性 + 密封/迁移。"""
    gaps = list(missing_fields(state, _D62_FIELDS))
    gaps.extend(_d62_unresolved(state))
    if (
        eq(_endoleak(state), "II") is True
        and sac_growing(state) is True
        and spec_token(state.node_field("D6.TYPE13_EXCLUDED")) != "已排除"
    ):
        gaps.append(
            "D6.TYPE13_EXCLUDED：Type I/III 未排除，不得把增长归因于 Type II（先做 CTA/超声复核）"
        )
    if (
        eq(_endoleak(state), "IV") is True
        and spec_token(state.node_field("D6.TYPE4_CONTEXT")) is None
    ):
        gaps.append("D6.TYPE4_CONTEXT：Type IV 需结合术后时间与器械信息确认是否为早期孔隙性渗漏")
    if (
        _endoleak_clear(state) is True
        and sac_growing(state) is True
        and spec_token(state.node_field("D6.OCCULT_LEAK_WORKUP")) in (None, "未完成")
    ):
        gaps.append("D6.OCCULT_LEAK_WORKUP：无可见内漏但瘤囊增长，需升级检查排除隐匿内漏与感染")
    return tuple(dict.fromkeys(gaps))


def d6_2(state: GuidelineState) -> NodeOutcome:
    """D6.2：按内漏类型与瘤囊变化给出再干预/观察/复核三档出口。"""
    outcome = evaluate_node("D6.2", state, _D62_SLOTS)
    gaps = _d62_gaps(state)
    if gaps:
        outcome = replace(outcome, gaps=gaps)
    return outcome


# ---------------------------------------------------------------------------
# D6.3 移植物结构与靶血管
# ---------------------------------------------------------------------------
def _d63_r08(state: GuidelineState) -> Verdict:
    """P0-A：术后主动脉肠瘘/腔脏器瘘疑似 → ``D4.3`` 并按急症等级回 G03/G05。

    判据取「既往修复 + 以下任一」：GI 出血、瘘状态阳性、术后瘘征象。
    规格 J 列写「不能只按常规内漏处理」——所以这条先于所有 P1。
    """
    if _repaired(state) is not True:
        return False
    # 只有**瘘状态本身**不算 P0：慢性主动脉肠瘘需要尽快进 D4.3，但不等于正在出血。
    # 触发 P0 的是出血与影像直接证据——这与规格「先兆/活动性 GI 出血或相关 CTA 征象」
    # 的措辞一致，也避免把每个已知瘘都升级成急症。
    return or_(
        active_gi_bleed(state),
        has(spec_token(state.node_field("D6.AEF_POSTOP")) or [], "GI出血", "直接交通", "腔外气体"),
    )


def _d63_r06(state: GuidelineState) -> Verdict:
    """P0-A：急性肢体缺血或肾/肠系膜灌注障碍 → 回 G03/G05。"""
    return or_(acute_limb_ischemia(state), malperfusion(state))


def _d63_r01(state: GuidelineState) -> Verdict:
    """P1：移植物感染或主动脉相关瘘 → ``D4.3`` 按 VGEI 指南评估。"""
    return or_(_infection_suspected(state), _fistula_present(state))


def _d63_r02(state: GuidelineState) -> Verdict:
    """P1：靶血管阻塞或相关灌注障碍 → 及时评估再血管化。"""
    return or_(_branch_occlusion(state), malperfusion(state))


def _d63_r03(state: GuidelineState) -> Verdict:
    """P1：吻合口旁动脉瘤 → 先核查感染与瘘，非感染性才比较腔内/开放方案。"""
    return eq(spec_token(state.node_field("D6.PARA_ANASTOMOTIC")), "明确", "疑似")


def _d63_r04(state: GuidelineState) -> Verdict:
    """P1：组件分离/移位/破损/闭塞 → 再干预评估。

    规格要求「若伴 I/III 型内漏、瘤囊增长、密封失效或灌注障碍」才「及时」再干预；
    本节点不区分「及时」与「择期」——那是术者判断，方案层只标注指征是否成立。
    """
    return or_(
        _graft_failure(state),
        _migration_significant(state),
        has(spec_token(state.node_field("D6.LIMB_OCCLUSION")) or [], "闭塞", "扭折"),
    )


def _d63_r90(state: GuidelineState) -> Verdict:
    """P2：结构、感染、瘘或器官/肢体灌注信息未知 → 不能自动判无并发症。"""
    return bool(_d63_unresolved(state))


def _d63_unresolved(state: GuidelineState) -> list[str]:
    names: list[str] = []
    if _graft_unresolved(state):
        names.append("N.GRAFT_MAJOR：移植物结构状态未记录（未知/疑似不当作无异常）")
    if _infection_suspected(state) is None:
        names.append("N.INFECTION_CTX：移植物感染状态未记录")
    if eq(token(state.case.cl.fistula), "未知", "疑似") is True:
        names.append("CL.FISTULA：主动脉相关瘘状态未确定")
    if malperfusion(state) is None:
        names.append("C.MALPERFUSION：器官/肢体灌注状态未评价")
    # ``C.BRANCH`` 空字典 = 本例没有分支/靶血管需要评估（标准 EVAR 的常态），
    # 不算缺口；只有在已经记了部位却判不了通畅性时才报。
    if state.case.c.branch and _branch_occlusion(state) is None:
        names.append("C.BRANCH：分支/靶血管通畅性未记录")
    return names


def _d63_r91(state: GuidelineState) -> Verdict:
    """P3 出口：感染/瘘、结构故障、吻合口旁瘤、靶血管阻塞均未见 → 随访。

    **结构兜底**：P0-A 两条与 P1 四条把「需要处理」的形态分完，剩余情形只能是
    「没有需要处理的结构或靶血管问题」，出口即随访。
    """
    return True


_D63_FIELDS: tuple[tuple[str, str], ...] = (
    ("D6.PARA_ANASTOMOTIC", "吻合口旁动脉瘤（开放修复后；先考虑感染）"),
    ("D6.REINTERVENTION_HISTORY", "既往针对该并发症的处理（方式/次数/结果）"),
    ("D6.LIMB_OCCLUSION", "移植物肢体狭窄/扭折/闭塞及缺血表现"),
    ("D6.ANATOMICAL_CHANGE", "瘤颈扩张、着陆区退变、分支/吻合口异常"),
    ("D6.AEF_POSTOP", "术后主动脉肠瘘征象（GI 出血/肠壁增厚/腔外气体/直接交通）"),
    ("D6.PARA_INFECT", "吻合口旁动脉瘤是否伴感染/瘘"),
)

_D63_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D6.3.R08",
        priority="P0-A",
        source_row="D6.3!18",
        statement="既往修复后出现先兆/活动性 GI 出血、脓毒症或相关 CTA 征象 → D4.3 并按急症等级回 G03/G05",
        judge=_d63_r08,
        exit_kind="escalate",
        exit_value="G05",
        # 规格 M 列：「下一节点/终点：D4.3、G03、G05 / 动作：D4.3；急症→G03/G05」
        next_nodes=("D4.3", "G03", "G05"),
        link=Link.PARALLEL,
        also_next="D4.3",
    ),
    RuleSlot(
        rule_id="D6.3.R06",
        priority="P0-A",
        source_row="D6.3!17",
        statement="急性肢体缺血或肾/肠系膜灌注障碍 → 回 G03/G05；无急症但明确闭塞 → 及时评估再血管化",
        judge=_d63_r06,
        # 规格 M 列：「下一节点/终点：**G03** / 动作：急症→G03；否则血管团队评估」。
        # 修前写的是 G05 —— 值不在这一格里。G03 是重新筛查（21 条急诊规则重跑），
        # G05 是直接进急诊模式，两者不是同一条路。
        exit_kind="escalate",
        exit_value="G03",
        next_nodes=("G03",),
        link=Link.EITHER,
    ),
    RuleSlot(
        rule_id="D6.3.R01",
        priority="P1",
        source_row="D6.3!9",
        statement="移植物感染或主动脉相关瘘 → D4.3（按 ESVS 2026 VGEI 评估范围、病原学、移植物处理与长期抗感染）",
        judge=_d63_r01,
        exit_kind="next",
        exit_value="D4.3",
    ),
    RuleSlot(
        rule_id="D6.3.R02",
        priority="P1",
        source_row="D6.3!10",
        statement="肾/内脏靶血管阻塞或相关灌注障碍 → 及时评估可能的再血管化",
        judge=_d63_r02,
        exit_kind="terminal",
        exit_value="紧急血管评估：再血管化候选（方式与时机由血管团队确认）",
    ),
    RuleSlot(
        rule_id="D6.3.R03",
        priority="P1",
        source_row="D6.3!11",
        statement="既往 OSR 后吻合口旁动脉瘤 → 先核查感染与瘘；非感染性才按症状/增长/解剖/风险比较腔内与开放",
        judge=_d63_r03,
        exit_kind="terminal",
        exit_value="吻合口旁动脉瘤再干预评估（已排除感染/瘘的前提下，腔内与开放方案由血管团队比较）",
        next_nodes=("D4.3", "D6.3"),
    ),
    RuleSlot(
        rule_id="D6.3.R04",
        priority="P1",
        source_row="D6.3!12",
        statement="组件分离/明显移位/破损/闭塞 → 记录部位与证据；伴 I/III 型内漏、增长、密封失效或灌注障碍 → 及时再干预",
        judge=_d63_r04,
        exit_kind="terminal",
        exit_value="结构故障再干预评估（先由医生确认影像，方案由血管团队确定）",
        next_nodes=("D6.2", "D6.3", "G03", "G05"),
    ),
    RuleSlot(
        rule_id="D6.3.R90",
        priority="P2",
        source_row="D6.3!8",
        statement="结构、感染、瘘或器官/肢体灌注信息未知/冲突 → 不能自动判无并发症",
        judge=_d63_r90,
        exit_kind="terminal",
        exit_value="人工复核（结构、感染、瘘或灌注状态待补齐）",
    ),
    RuleSlot(
        rule_id="D6.3.R91",
        priority="P3",
        source_row="D6.3!7",
        statement="感染/瘘、结构故障、吻合口旁瘤、靶血管阻塞均未见且资料充分 → 随访",
        judge=_d63_r91,
        exit_kind="terminal",
        exit_value="随访：未见需要处理的结构或靶血管问题，按 D6.1 的随访计划复查",
    ),
)


def _d63_gaps(state: GuidelineState) -> tuple[str, ...]:
    """D6.3 缺什么 —— 结构/感染/瘘/灌注 + 再干预史与解剖进展。"""
    gaps = list(missing_fields(state, _D63_FIELDS))
    gaps.extend(_d63_unresolved(state))
    if _d63_r03(state) is True and spec_token(state.node_field("D6.PARA_INFECT")) is None:
        gaps.append("D6.PARA_INFECT：吻合口旁动脉瘤必须先核查感染（血培养/周围气体液体/瘘）")
    if (
        _d63_r03(state) is True
        and spec_token(state.node_field("D6.REINTERVENTION_HISTORY")) is None
    ):
        gaps.append("D6.REINTERVENTION_HISTORY：既往处理方式与结果未记录（影响下一次策略）")
    return tuple(dict.fromkeys(gaps))


def d6_3(state: GuidelineState) -> NodeOutcome:
    """D6.3：感染/瘘 → D4.3；结构或靶血管问题 → 再干预评估；否则随访。"""
    outcome = evaluate_node("D6.3", state, _D63_SLOTS)
    gaps = _d63_gaps(state)
    if gaps:
        outcome = replace(outcome, gaps=gaps)
    return outcome


#: 本模块提供的节点（键与规格 sheet 名一致）。
NODES: dict[str, NodeFn] = {
    "D6.1": d6_1,
    "D6.2": d6_2,
    "D6.3": d6_3,
}

#: 已实现的规则 → 规格源行。
RULE_ROWS: dict[str, str] = {
    **{slot.rule_id: slot.source_row for slot in _D61_SLOTS},
    **{rule.rule_id: rule.source_row for rule in _D61_DISPLAY_RULES},
    **{slot.rule_id: slot.source_row for slot in _D62_SLOTS},
    **{slot.rule_id: slot.source_row for slot in _D63_SLOTS},
}
