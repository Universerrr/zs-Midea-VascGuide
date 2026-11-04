"""D4 · 感染性主动脉病变（``D4.1`` → ``D4.2`` / ``D4.3``）。

规格来源：``VascGuide_V3.6.2.xlsx`` 的 ``D4.1`` / ``D4.2`` / ``D4.3`` 三个 sheet，
共 25 条规则（7 + 7 + 11），全部实现。

## 三个节点各管什么

``D4.1`` 感染分型与瘘（分诊台）
    回答「这是原生主动脉感染、既往移植物感染，还是主动脉相关瘘；还是都不是」。
    三条 P1 各指向一个后续节点：原生 → ``D4.2``、移植物 → ``D4.3``、瘘 → ``D4.3``。
    ``R90`` 守的是一条硬线：**分不清原生还是移植物时不许猜**——两侧的治疗逻辑
    完全不同（一侧按感染源控制，一侧先按 MAGIC 定性），猜错等于换了整条路径。

``D4.2`` 原生感染：感染源控制与重建
``D4.3`` 移植物感染：MAGIC 定性、移植物处理与长期管理

    这两个节点都**不是「术式二选一」节点**，而是「MDT 方案完整性」节点。规格在
    这里反复写「具体重建材料需 MDT 决定」「Agent 不得开立医嘱」「不能由 Agent 指定
    重建材料或疗程」，所以候选档位封顶在「需 MDT 评估」——**没有「可行候选」这一档**。
    ``D1.3`` / ``D2.3`` / ``D3.2`` 能给「可行」，是因为那里的判断对象是解剖与 IFU；
    这里判断对象是「感染控制得干不干净」，而那是术中所见与病原学才能回答的。

## 五处必须先讲清的结构决定

**① D4.2 的 5 条 P1 做成展示性规则，不进出口竞争。**
    ``R01``（先采培养再经验性抗菌）、``R02``（不等直径阈值）、``R03``（方式比较）、
    ``R04``（能力不足转诊）、``R09``（方案完整性）是**并列义务**：一次会诊里要同时
    满足，不存在「命中一条就跳过其余」。放进 :func:`evaluate_node` 的槽里，先命中的
    那条会遮蔽其余四条——报告上就只剩「需抗菌」，而「要转诊」「方案不完整」全都消失。

**② MAGIC 由逐项标准算，与 ``D4.MAGIC_STATUS`` 交叉校验。**
    规格给了两处：``D4.MAGIC_CLINICAL/IMAGING/LAB`` 是逐项证据清单，
    ``D4.MAGIC_STATUS`` 是团队按 MAGIC 框架给出的总状态。两者不一致时取**更严**的
    一侧（确诊 > 疑似 > 不支持）并留一条缺口——团队手上有清单之外的证据是正常的，
    但"清单算出来确诊、总状态写不支持"这种降级不能静默通过。
    标准未收全（三类清单有空项）时**不得标「不支持」**（``D4.MAGIC_STATUS`` 的 J 列）。

**③ 血培养「未采集／待结果」不阻断路径。**
    ``D4.2.R01`` 明写「危重、脓毒症或活动性出血患者不得为等待培养而延误治疗」。
    把它写进 ``R90`` 的阻断条件，等于让所有还没出培养结果的病例都停在人工复核——
    而那些人恰恰是最不能等的。故只进缺口，不阻断（与 ``D2.2`` 对「趋势缺失」的处理同构）。

**④ 两列候选直接叫 ``EVAR`` / ``OSR``，不自造名称。**
    感染一旦成立，``D1.3`` 给出的「EVAR 可行候选」就失效了——标准腔内修复在
    感染性病变里只能作桥接。``GuidelineState.candidates`` 的合并规则是「同术式
    后者覆盖前者」，病种顺序又是 D1 → D4，所以把这两列命名为 ``EVAR`` / ``OSR``
    正好让 D4 的结论盖住 D1 的；自造名称（如「腔内桥接」）会让两条并列出现在
    同一份报告里，医生读到的是「EVAR 可行」和「EVAR 只能桥接」两个相反结论。

**⑤ 术语映射：模型把「状态」与「类型」压在一格里。**
    规格分 ``CL.INFECTION`` / ``CL.FISTULA`` / ``CL.FISTULA_TYPE`` 三格；模型
    ``cl.infection`` 把「原生/移植物」与「明确/疑似」压成六态，``cl.fistula`` 把
    「有无」与「瘘类型」压成一格。所以本文件读瘘时用 ``token`` 而**不是**
    ``spec_token``：``"疑似"`` 在这里是**阳性触发值**（规格 J 列：「疑似或不可评价
    按风险升级复核」），折成 ``None`` 会让每个疑似瘘静默掉回「无」。
"""

from __future__ import annotations

from dataclasses import replace

from ....rules.router import (
    Verdict,
    and_,
    eq,
    not_,
    or_,
    token,
)
from ..state import Candidate, CandidateStatus, GuidelineState, Link, NodeFn, NodeOutcome
from .common import (
    SPEC_UNCERTAIN_TOKENS,
    DisplayRule,
    RuleSlot,
    active_gi_bleed,
    display_entries,
    evaluate_node,
    fistula_present,
    infection_typed,
    missing_fields,
    multi_field,
    rupture_evidence,
    spec_token,
    unstable,
)

__all__ = ["NODES", "d4_1", "d4_2", "d4_3"]

#: 模型 ``cl.infection`` 里「原生主动脉感染」的两个取值。
_NATIVE_INFECTION: tuple[str, ...] = ("原生感染明确", "原生感染疑似")

#: 模型 ``cl.infection`` 里「既往移植物感染」的两个取值。
_GRAFT_INFECTION: tuple[str, ...] = ("移植物感染明确", "移植物感染疑似")

#: ``s.gi_bleed_type`` 里属于「活动性出血」的表现（``无`` / ``未知`` 不在其中）。
_GI_BLEED_SIGNS: tuple[str, ...] = ("呕血", "黑便", "便血")

#: ``D4.AEF_WARNING`` 里属于「已发生出血」的取值。
_AEF_BLEEDING: tuple[str, ...] = ("先兆性少量出血", "大量出血")

#: ``D4.BLOOD_CULTURE`` 里「采样了但还没结果 / 根本没采」的写法。
_CULTURE_UNRESOLVED: tuple[str, ...] = ("未采集", "未做", "待结果", "已采集待结果")

#: 各类证据清单里表示「明确阴性」的写法 —— 除此之外的条目都算「支持」。
_NEGATIVE_ITEMS: tuple[str, ...] = ("无", "未见", "否认", "阴性")

#: MAGIC 三个类别的固定顺序（计算与呈现都按它，避免字典序抖动）。
_MAGIC_CATEGORIES: tuple[str, ...] = ("clinical", "imaging", "lab")

#: MAGIC 主要标准（``D4.3`` 的 F 列逐字）。
_MAGIC_MAJOR: dict[str, frozenset[str]] = {
    "clinical": frozenset(
        {"移植物周围脓液", "移植物暴露或窦道", "瘘", "移植物植入于感染部位"},
    ),
    "imaging": frozenset(
        {"植入后≥3个月周围液体", "植入后≥7周周围气体", "序列影像气体增加"},
    ),
    "lab": frozenset(
        {"取出移植物或术中标本检出病原体", "影像引导周围液体标本检出病原体"},
    ),
}

#: MAGIC 次要标准（``D4.3`` 的 F 列逐字）。
_MAGIC_MINOR: dict[str, frozenset[str]] = {
    "clinical": frozenset({"局部红肿热痛或脓性分泌物", "无其他原因发热≥38°C"}),
    "imaging": frozenset(
        {
            "可疑气液或软组织炎症",
            "动脉瘤扩大",
            "假性瘤",
            "局灶肠壁增厚",
            "椎间盘炎或骨髓炎",
            "PET或WBC显像异常",
        },
    ),
    "lab": frozenset({"无其他感染源的血培养阳性", "炎症标志物升高归因于VGEI"}),
}

#: MAGIC 结论的严格程度排序：不一致时取更大者（不得静默降级）。
_MAGIC_ORDER: dict[str, int] = {"不支持": 1, "疑似": 2, "确诊": 3}


# ---------------------------------------------------------------------------
# 跨节点共用的读取器
# ---------------------------------------------------------------------------
def _infection(state: GuidelineState) -> object:
    """``CL.INFECTION`` —— 模型 ``cl.infection``（六态，原生/移植物 × 明确/疑似）。

    用 :func:`spec_token` 而不是 ``token``：这里的「未评价」是「还没有结论」，
    折成 ``None`` 才能让 ``eq`` 给出 UNKNOWN。注意 ``原生感染疑似`` 是**一个完整
    字面量**，不是不确定词 ``疑似``，所以不会被折掉——这正是规格要的阳性触发。
    """
    return spec_token(state.case.cl.infection)


def _fistula(state: GuidelineState) -> object:
    """``CL.FISTULA`` —— 模型 ``cl.fistula``（有无 + 类型压在一格）。

    用 ``token`` 而**不**折不确定词：``"疑似"`` 在本字段是阳性触发值，折成 ``None``
    会把疑似瘘读成「无」——方向反了（见模块文档④）。
    """
    return token(state.case.cl.fistula)


# 瘘的判据与 D6 共用 ``common.fistula_present``（同一事实只许一个判据函数）。
_fistula_present = fistula_present


def _aef_sign(state: GuidelineState) -> Verdict:
    """``C.AEF_SIGN`` —— 影像上的主动脉肠瘘线索。

    只把「明确」当阳性：「可疑」进 UNKNOWN，因为规格 J 列写「未见直接交通不能
    单独排除」，反过来说也成立——可疑线索不能单独确认。
    """
    sign = token(state.case.c.aef_sign)
    if sign == "明确":
        return True
    if sign in ("未见",):
        return False
    return None


def _graft_history(state: GuidelineState) -> Verdict:
    """既往主动脉修复史 —— ``N.PREV_REPAIR`` / ``CL.PREV_EVOR`` / ``C.GRAFT`` 三处。

    为什么读三个字段：模型里「既往修复」有两处独立落点——``cl.prev_evor`` 是
    ``G08.R06``（D6 术后病种）的判据，``n.prev_repair`` 是解剖/病生理层的历史；
    同一个事实两处记录是既有模型的结构，本层不做合并（合并会改动一个进哈希的字段）。
    只看其中一个，遇到另一个填了、这个空着的病例就会把移植物感染错分到原生感染
    分支——整条路径都换掉，代价远高于多读一个字段。
    """
    repairs = (token(state.case.n.prev_repair), token(state.case.cl.prev_evor))
    graft = token(state.case.c.graft)
    signals = [s for s in (*repairs, graft) if s is not None]
    if not signals:
        return None
    # 阳性证据优先：任一处写「有修复/移植物」即成立，哪怕另一处写「无」——
    # 漏判一条既往修复会把移植物感染错分到原生感染分支。
    if any(s in ("EVAR", "OSR", "其他", "有") for s in signals):
        return True
    # 只有**全部**证据都是明确阴性才算没有修复史。影像写「可疑」而病史写「无」时
    # 两者冲突，此处不给结论（交 R90），不拿病史单方面否定影像。
    if all(s == "无" for s in signals):
        return False
    return None


def _native(state: GuidelineState) -> Verdict:
    """``CL.INFECTION`` 明确指向原生感染。"""
    return eq(_infection(state), *_NATIVE_INFECTION)


def _graft_infected(state: GuidelineState) -> Verdict:
    """``CL.INFECTION`` 明确指向移植物感染。"""
    return eq(_infection(state), *_GRAFT_INFECTION)


def _active_gi_bleed(state: GuidelineState) -> Verdict:
    """活动性消化道出血 —— 判据本体在 ``common.active_gi_bleed``。"""
    return active_gi_bleed(state)


def _hemodynamically_unstable(state: GuidelineState) -> Verdict:
    """血流动力学不稳定 —— 判据本体在 ``common.unstable``（与 ``G03`` 同一定义）。"""
    return unstable(state)


def _base_support(state: GuidelineState, spec_id: str) -> Verdict:
    """一类证据清单是否「支持感染」——支持 / 明确阴性 / 未评估。

    判据只有一条：清单里出现任一**非阴性**条目即算支持。规格给这些清单的允许值
    几乎全是阳性发现（腹背痛、菌血症、周围气体、快速扩张…），阴性只有「无/未见」。
    """
    values = multi_field(state, spec_id)
    if not values:
        return None
    tokens = [str(v) for v in values]
    # 顺序：阳性 → 不确定 → 明确阴性。必须先排除不确定词再判阳性，
    # 否则「未知」「不可评价」会被当成一条支持证据（「不在阴性集合里」≠ 阳性）。
    if any(t not in SPEC_UNCERTAIN_TOKENS and t not in _NEGATIVE_ITEMS for t in tokens):
        return True
    if any(t in SPEC_UNCERTAIN_TOKENS for t in tokens):
        return None
    return False


def _composite_support(state: GuidelineState) -> Verdict:
    """临床 + 实验室/微生物 + 影像**组合**是否支持感染性 AAA（``D4.1.R04``）。

    规格要求组合而非单项（「任何单项不自动确诊」），故：≥2 类支持 → 成立；
    只有 1 类支持且其余已明确阴性 → 不成立；仍有类别未评估 → 判不了。
    """
    cats = [
        _base_support(state, "D4.NATIVE_CLINICAL"),
        _base_support(state, "D4.NATIVE_LAB"),
        _base_support(state, "D4.NATIVE_CTA"),
    ]
    if sum(1 for c in cats if c is True) >= 2:
        return True
    if any(c is None for c in cats):
        return None
    return False


def _infection_signal(state: GuidelineState) -> Verdict:
    """任一来源的感染线索 —— 分型字段 / 医生判断（``common.infection_typed``）
    + 本节点自己的影像组合证据。

    前两项交给公共层：D6/D7 问的是同一件事，分流出去写过两份，口径必然分叉。
    ``_composite_support`` 留在本节点，它是 D4 的诊断判据（三类证据任一类支持），
    不是「有没有感染」这个问题的通用答案。
    """
    return or_(infection_typed(state), _composite_support(state))


# ---------------------------------------------------------------------------
# D4.1 感染分型与瘘
# ---------------------------------------------------------------------------
_D41_CORE_FIELDS: tuple[tuple[str, str], ...] = (
    ("D4.BLOOD_CULTURE", "血培养采集与结果（采样时间/抗菌药前后/菌种/药敏）"),
    (
        "D4.INFECTION_IMAGING",
        "主动脉周围感染影像（气体/液体/软组织/快速扩张/假性动脉瘤/骨或肠受累）",
    ),
    ("D4.INFECTION_SOURCE", "感染源（心内膜炎/尿路/肺部/皮肤软组织/腹腔/不明/无已知来源）"),
)

_D41_NATIVE_FIELDS: tuple[tuple[str, str], ...] = (
    ("D4.NATIVE_CLINICAL", "原生感染临床表现（腹背痛/发热≥38℃/菌血症/脓毒症/邻近感染）"),
    ("D4.NATIVE_LAB", "原生感染实验室证据（WBC/CRP/PCT/血培养或组织培养）"),
    ("D4.NATIVE_CTA", "原生感染影像形态（囊状/偏心/多房/周围软组织/气体/快速扩大/血肿）"),
)


def _d41_r03(state: GuidelineState) -> Verdict:
    """P0-A：明确/疑似主动脉相关瘘（或影像直接线索）→ D4.3。

    规格 M 列写的是两段：「下一节点/终点：D4.3、G03、G05 / 动作：**D4.3；急症→
    G03/G05**」。也就是说**主出口恒为 D4.3**，回 G03/G05 是另一个条件分支，
    由 :func:`_d41_r03_emergency` 单独判 —— 不写进本函数，否则「有没有急症」
    会变成「命不命中这条规则」，两件事混成一个。
    """
    return or_(_fistula_present(state), _aef_sign(state))


def _d41_r03_emergency(state: GuidelineState) -> Verdict:
    """``D4.1.R03`` 的附加条件：「如伴活动性出血、休克或新破裂证据，先回 G03/G05」。

    三项合起来就是 :func:`common.active_gi_bleed` + :func:`common.rupture_evidence`
    （后者已含休克与新破裂证据）。**瘘本身不构成急症**：规格把「瘘→D4.3」写成
    常规出口，把急症写成「如伴」的附加条件。修前无条件 ``escalate G03``，
    于是没有出血的瘘病人，报告里也会出现「回 G03」。
    """
    return or_(_active_gi_bleed(state), rupture_evidence(state))


def _d41_r01(state: GuidelineState) -> Verdict:
    """P1：无移植物史 + 分型指向原生感染 → D4.2。

    要求「无移植物史」而不只是「分型写着原生」：分型字段可能由下级医院填写，
    而 ``N.PREV_REPAIR`` 是硬事实。两者冲突时以事实为准（见 :func:`_graft_history`），
    冲突本身由 ``R90`` 兜到人工复核。
    """
    return and_(_native(state), not_(_graft_history(state)))


def _d41_r02(state: GuidelineState) -> Verdict:
    """P1：既往 EVAR/OSR 且符合疑似/确诊 VGEI 标准 → D4.3。

    不要求 MAGIC 已经做完：VGEI 一旦成立，后续本来就要在 ``D4.3`` 里补 MAGIC
    评估，把「MAGIC 未完成」写成本规则的阻断条件会让这类病例停在分诊台，
    而 ``D4.3.R90`` 才是该报「未完成评估」的地方。
    """
    return and_(
        _graft_history(state),
        or_(
            _graft_infected(state),
            eq(token(state.node_field("D4.MAGIC_STATUS")), "确诊", "疑似"),
        ),
    )


def _d41_r04(state: GuidelineState) -> Verdict:
    """P1：临床 + 实验室/微生物 + 影像组合支持感染性 AAA → D4.2。

    这是证据侧入口，与 ``R01`` 的分工：``R01`` 读**分型结论**（``CL.INFECTION``），
    本规则读**证据组合**。分型字段还没填、但三类证据已经凑齐两类的病例由本规则接住
    ——否则它会一路掉到 ``R91``「不支持感染」，方向完全反了。
    """
    return and_(_composite_support(state), not_(_graft_history(state)))


def _d41_r05(state: GuidelineState) -> Verdict:
    """P1：炎症性与感染性证据重叠 → 保留双标签，D4.2 与 D5.3 并行评估。

    规格点名的四种「不能排除感染」的炎症表现（mantle sign / 纤维化 + 发热、菌血症、
    周围气体、快速扩大）里，前两项属炎症影像、后两项属感染证据，所以判据取
    「炎症性证据存在」∧「任一感染线索」。**不得以 CRP 升高或典型炎症影像单独排除
    感染**——这正是把它放在 ``R91`` 之前的原因：它拦住的是「有炎症影像就写无感染」。
    """
    inflammation = eq(token(state.case.n.inflammatory_ctx), "临床确认", "影像高度提示")
    return and_(inflammation, _infection_signal(state))


def _d41_r90(state: GuidelineState) -> Verdict:
    """P2：分型、瘘状态或修复史判不了 → 不许输出「无感染」，交人工复核。

    三项阻断条件都指向同一件事：**分不清该走哪条支路**。
    · 感染状态未评价 → 连「有没有感染」都不知道；
    · 瘘状态未知 → 疑似瘘按风险升级复核（规格 J 列），不能当无瘘；
    · 修复史未知且存在感染线索 → 原生与移植物两条路径的治疗逻辑不同，猜不得。
    · 既往有修复史但 MAGIC 未评估 → 规格明写「标准未完整收集时不得标不支持」。
    """
    return any(
        (
            _infection(state) is None,
            _fistula(state) is None,
            _graft_history(state) is None and _infection_signal(state) is not False,
            _graft_history(state) is True
            and _graft_infected(state) is False
            and _magic_stated(state) is None,
        )
    )


def _d41_r91(state: GuidelineState) -> Verdict:
    """P3 出口：既无感染证据也无瘘 → 回 G08 重新鉴别病因。

    走到这里说明 ``R90`` 没拦（关键项都有结论）且四条分支规则都没命中，
    即「高质量评估不支持感染/瘘」。此时不能留在 D4 里编一条阴性结论——
    ``G08`` 才是重新分配病种标签的地方。
    """
    return True


_D41_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D4.1.R03",
        priority="P0-A",
        source_row="D4.1!13",
        statement="明确/疑似主动脉相关瘘（或影像直接交通线索）→ D4.3；伴活动性出血/休克先回 G03/G05",
        judge=_d41_r03,
        # 规格 M 列：「下一节点/终点：D4.3、G03、G05 / 动作：D4.3；急症→G03/G05」
        # 主出口是 D4.3；G03/G05 是**条件性并行义务**，由 escalate_if 判。
        exit_kind="next",
        exit_value="D4.3",
        next_nodes=("D4.3", "G03", "G05"),
        link=Link.PARALLEL,
        also_escalate="G03",
        escalate_if=_d41_r03_emergency,
    ),
    RuleSlot(
        rule_id="D4.1.R01",
        priority="P1",
        source_row="D4.1!11",
        statement="无相关移植物史且医生/影像提示原生感染性AAA → D4.2（保留疑似/确诊状态）",
        judge=_d41_r01,
        exit_kind="next",
        exit_value="D4.2",
    ),
    RuleSlot(
        rule_id="D4.1.R02",
        priority="P1",
        source_row="D4.1!12",
        statement="既往 EVAR/OSR 且符合疑似或确诊移植物感染标准 → D4.3（按 ESVS 2026 VGEI 进一步规划）",
        judge=_d41_r02,
        exit_kind="next",
        exit_value="D4.3",
    ),
    RuleSlot(
        rule_id="D4.1.R04",
        priority="P1",
        source_row="D4.1!18",
        statement="临床 + 实验室/微生物 + 影像组合支持感染性AAA（由医生确认为疑似/确诊）→ D4.2",
        judge=_d41_r04,
        exit_kind="next",
        exit_value="D4.2",
    ),
    RuleSlot(
        rule_id="D4.1.R05",
        priority="P1",
        source_row="D4.1!19",
        statement="炎症性与感染性证据重叠 → 保留双标签，D4.2 与 D5.3 并行评估（MDT）",
        judge=_d41_r05,
        exit_kind="next",
        # 规格 M 列：「D4.2+D5.3 并行/MDT」——两个节点都要执行。
        # 修前写成 terminal + 一句描述：「并行」两个字进了报告，另一条分支却没走。
        exit_value="D4.2",
        next_nodes=("D4.2", "D5.3"),
        link=Link.PARALLEL,
    ),
    RuleSlot(
        rule_id="D4.1.R90",
        priority="P2",
        source_row="D4.1!10",
        statement="感染/瘘状态、修复史或 MAGIC 关键项未评价/冲突 → 保留疑似，不得输出无感染",
        judge=_d41_r90,
        exit_kind="terminal",
        exit_value="人工复核（感染分型或瘘状态待核查）",
    ),
    RuleSlot(
        rule_id="D4.1.R91",
        priority="P3",
        source_row="D4.1!9",
        statement="高质量评估不支持感染/瘘 → 返回 G08 重新鉴别其他病因",
        judge=_d41_r91,
        exit_kind="escalate",
        exit_value="G08",
        # 规格 M 列：「下一节点/终点：D4.2、D4.3、G08」——三个值都在这一列里。
        next_nodes=("D4.2", "D4.3", "G08"),
    ),
)


def _d41_gaps(state: GuidelineState) -> tuple[str, ...]:
    """D4.1 缺什么 —— 核心三项 + 按分支列的专项证据。

    分支内的字段按 ``_graft_history`` 分列而不是全列：把原生感染的三类证据挂到
    移植物感染病例上，读者会以为「补上它们就能定方案」，而那边要的是 MAGIC。
    """
    gaps = list(missing_fields(state, _D41_CORE_FIELDS))
    if _infection(state) is None:
        gaps.append("CL.INFECTION：感染状态未评价（不得当作无感染）")
    if _fistula(state) is None:
        gaps.append("CL.FISTULA：主动脉相关瘘状态未知（未见直接交通不能单独排除）")
    graft = _graft_history(state)
    if graft is None:
        gaps.append("N.PREV_REPAIR / C.GRAFT：既往主动脉修复史未确认（原生与移植物分支由此分野）")
    elif graft:
        if _magic_stated(state) is None:
            gaps.append("D4.MAGIC_STATUS：MAGIC 评估未完成（标准未收全时不得标「不支持」）")
    else:
        gaps.extend(missing_fields(state, _D41_NATIVE_FIELDS))
    if _fistula_present(state) is True and _fistula(state) == "疑似":
        gaps.append("CL.FISTULA_TYPE：瘘类型未确定（保留疑似状态，需内镜/术中证据）")
    return tuple(gaps)


def d4_1(state: GuidelineState) -> NodeOutcome:
    """D4.1：定分型（原生/移植物/瘘/无）并分流到 D4.2、D4.3 或回 G08。"""
    outcome = evaluate_node("D4.1", state, _D41_SLOTS)
    gaps = _d41_gaps(state)
    if gaps:
        outcome = replace(outcome, gaps=gaps)
    return outcome


# ---------------------------------------------------------------------------
# D4.2 原生感染：感染源控制与重建
# ---------------------------------------------------------------------------
_D42_FIELDS: tuple[tuple[str, str], ...] = (
    ("D4.BLOOD_CULTURE", "血培养采集与结果（先采后用药，但不为采样延误急救）"),
    ("D4.INFECTION_EXTENT", "感染范围（局限/广泛/瘘/脓肿/骨受累/破裂）"),
    ("D4.NATIVE_FITNESS", "开放手术耐受性与总体风险（可接受/高风险/极高风险）"),
    ("D4.CENTRE_CAPABILITY", "中心能力（血管外科/感染科/微生物/重建材料/ICU）"),
    ("D4.SOURCE_CONTROL", "感染源控制计划（切除清创/原位重建/解剖外重建/EVAR桥接/长期抑制）"),
    ("D4.RECONSTRUCTION_OPTIONS", "重建方式与材料（自体静脉/冷冻血管/经处理人工血管/大网膜覆盖）"),
    ("D4.ANTIMICROBIAL_PLAN", "抗菌治疗计划（经验性/靶向/疗程/责任医生）"),
    ("D4.POSTOP_SURVEILLANCE", "术后随访计划（疗程/复查指标/影像）"),
)


def _extent(state: GuidelineState) -> list[object] | None:
    """``D4.INFECTION_EXTENT``（多选）—— 决定切除清创范围。"""
    return multi_field(state, "D4.INFECTION_EXTENT")


def _fitness(state: GuidelineState) -> object:
    """``D4.NATIVE_FITNESS`` —— 开放手术耐受性（医生/麻醉评估结论）。"""
    return spec_token(state.node_field("D4.NATIVE_FITNESS"))


def _capability(state: GuidelineState) -> object:
    """``D4.CENTRE_CAPABILITY`` —— 本中心感染性主动脉病变的 MDT 与重建能力。"""
    return spec_token(state.node_field("D4.CENTRE_CAPABILITY"))


def _source_control(state: GuidelineState) -> object:
    """``D4.SOURCE_CONTROL`` —— 感染源控制计划；``未决定`` 表示还没定。"""
    return token(state.node_field("D4.SOURCE_CONTROL"))


def _antimicrobial(state: GuidelineState) -> object:
    """``D4.ANTIMICROBIAL_PLAN`` —— 抗菌治疗计划状态。"""
    return token(state.node_field("D4.ANTIMICROBIAL_PLAN"))


def _culture(state: GuidelineState) -> object:
    """``D4.BLOOD_CULTURE`` —— 血培养的结构化记录。"""
    return token(state.node_field("D4.BLOOD_CULTURE"))


def _plan_items_missing(state: GuidelineState) -> tuple[str, ...]:
    """确定性方案的要素里还缺哪几项（``D4.2.R09`` 的判据本体）。

    五项来自规格 G 列：「病灶切除/清创、瘘处理、重建方式、可用材料、术后抗菌与监测」。
    瘘处理只在有瘘时才计——无瘘病例不该被要求填「瘘怎么修」。
    """
    missing: list[str] = []
    if _source_control(state) in (None, "未决定"):
        missing.append("感染源控制计划（切除/清创范围）")
    if _reconstruction(state) is None:
        missing.append("重建方式与材料")
    if _antimicrobial(state) in (None, "未开始", "待感染科"):
        missing.append("抗菌治疗计划")
    if not state.node_field("D4.POSTOP_SURVEILLANCE"):
        missing.append("术后抗菌与影像随访计划")
    if _fistula_present(state) is True:
        missing.append("瘘的处理方式（本例存在主动脉相关瘘）")
    return tuple(missing)


def _reconstruction(state: GuidelineState) -> list[object] | None:
    """``D4.RECONSTRUCTION_OPTIONS``（多选）—— 可用重建方式。"""
    return multi_field(state, "D4.RECONSTRUCTION_OPTIONS")


def _d42_r01(state: GuidelineState) -> Verdict:
    """P1：疑似/确诊感染 → 生成「先采培养、后经验性抗菌」的**待确认准备项**。

    只生成待确认项，不下医嘱（规格原文：「Agent只生成待确认准备项，不下医嘱」）。
    判据用 :func:`_infection_signal` 而非 ``_native``：分型字段空着但医生已按
    临床判断为感染的病例，同样需要这条准备项。
    """
    return _infection_signal(state)


def _d42_r02(state: GuidelineState) -> Verdict:
    """P1：感染性 AAA **不按退变性 AAA 的直径阈值等待**，尽快评估感染源控制。

    这条与 ``D1.1`` 的 55/50 mm 阈值是一对反向约束：那边等得到 55 mm，这边等不起。
    判据只看有没有感染线索——直径在本节点不参与任何判断。
    """
    return _infection_signal(state)


def _d42_r03(state: GuidelineState) -> Verdict:
    """P1：感染源控制方式未定 → 必须完成开放与腔内桥接的并列比较。

    ``EVAR`` 在此只作**桥接**：规格明写「EVAR 不自动代表感染源已控制」。
    """
    return _infection_signal(state) if _source_control(state) in (None, "未决定") else False


def _d42_r04(state: GuidelineState) -> Verdict:
    """P1：中心能力明确不具备 → 提示高容量中心协作/转诊（资源缺口不等于急症诊断）。"""
    return eq(_capability(state), "不具备")


def _d42_r09(state: GuidelineState) -> Verdict:
    """P1：方案要素不全 → **不得**标记为「完整确定性方案」。"""
    return bool(_plan_items_missing(state))


def _d42_r90(state: GuidelineState) -> Verdict:
    """P2：感染范围或患者耐受性未评价 → 不自动定根治方式。

    只阻断这两项（都是「判不出重建方向」），不阻断血培养与中心能力：
    · 血培养未回不阻断——规格明写不得为采样延误急救；
    · 中心能力未知不阻断——它决定「去哪做」而不是「做什么」，进缺口即可。
    """
    return any((not _extent(state), _fitness(state) is None))


def _d42_r91(state: GuidelineState) -> Verdict:
    """P3 出口：感染源控制与重建方案交感染 MDT 确认（不指定材料与疗程）。"""
    return True


_D42_DISPLAY_RULES: tuple[DisplayRule, ...] = (
    DisplayRule(
        rule_id="D4.2.R01",
        source_row="D4.2!8",
        statement="疑似感染性AAA → 生成待确认准备项：尽量治疗前采血培养，随后尽快经验性静脉抗菌",
        judge=_d42_r01,
    ),
    DisplayRule(
        rule_id="D4.2.R02",
        source_row="D4.2!9",
        statement="不按普通退变性 AAA 直径阈值等待；尽快评估感染源控制与修复时机（时机由团队定）",
        judge=_d42_r02,
    ),
    DisplayRule(
        rule_id="D4.2.R03",
        source_row="D4.2!10",
        statement="并列比较开放切除/清创+重建与 EVAR 桥接；EVAR 不自动代表感染源已控制",
        judge=_d42_r03,
    ),
    DisplayRule(
        rule_id="D4.2.R04",
        source_row="D4.2!11",
        statement="中心能力不足 → 高容量中心协作/转诊；活动性出血/休克时急症路径优先",
        judge=_d42_r04,
    ),
    DisplayRule(
        rule_id="D4.2.R09",
        source_row="D4.2!16",
        statement="缺少感染源控制/重建/抗菌/随访要素 → 不得标记为完整确定性方案",
        judge=_d42_r09,
    ),
)

_D42_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D4.2.R90",
        priority="P2",
        source_row="D4.2!7",
        statement="感染范围或患者耐受性未评价 → 不自动定根治方式；MDT 复核，急症优先",
        judge=_d42_r90,
        exit_kind="terminal",
        exit_value="人工复核（感染范围或耐受性待评估）",
    ),
    RuleSlot(
        rule_id="D4.2.R91",
        priority="P3",
        source_row="D4.2!6",
        statement="感染性AAA明确/疑似 → 感染源控制与修复评估（不等直径阈值）；MDT 确认方案",
        judge=_d42_r91,
        exit_kind="terminal",
        exit_value="感染MDT：感染源控制 + 重建方案确认（具体材料与疗程由 MDT 决定）",
    ),
)


def _open_control_candidate(state: GuidelineState) -> Candidate:
    """开放切除清创 + 重建这一列（候选名 ``OSR``）—— 至多到「需 MDT 评估」。

    为什么没有「可行候选」：感染性主动脉病变的可行与否取决于清创能不能干净、
    重建材料选什么，两者都是术中所见与 MDT 的结论（规格 4 处明令不指定材料）。
    """
    fitness = _fitness(state)
    if fitness is None and not _extent(state):
        return Candidate(
            "OSR",
            CandidateStatus.UNASSESSED,
            "感染范围与开放手术耐受性均未评价",
            ("D4.2.R90",),
        )
    if fitness == "极高风险":
        return Candidate(
            "OSR",
            CandidateStatus.MDT,
            "极高风险：个体化或姑息方案由 MDT 决定；须记录未根治的再感染与出血风险",
            ("D4.2.R03",),
        )
    if _capability(state) == "不具备":
        return Candidate(
            "OSR",
            CandidateStatus.MDT,
            "本中心不具备感染性主动脉 MDT/重建能力 → 转诊高容量中心协作",
            ("D4.2.R04",),
        )
    if not _extent(state):
        return Candidate(
            "OSR",
            CandidateStatus.CONDITIONAL,
            "感染范围未评估：切除清创范围与重建路径无法确定",
            ("D4.2.R90",),
        )
    return Candidate(
        "OSR",
        CandidateStatus.MDT,
        "感染源控制（切除清创 + 重建）的范围、材料与疗程须 MDT 确认",
        ("D4.2.R03", "D4.2.R09"),
    )


def _evar_bridge_candidate(state: GuidelineState) -> Candidate:
    """腔内桥接这一列（候选名 ``EVAR``）—— 恒为条件性候选，**永不升为可行**。

    规格 ``D4.2.R03`` 的原话是「EVAR 不自动代表感染源已控制」。把它标成可行，
    等于系统替团队宣布感染源已经处理好了——这是本节点最危险的一类错。
    """
    if _capability(state) == "不具备":
        return Candidate(
            "EVAR",
            CandidateStatus.MDT,
            "本中心能力不足 → 转诊；桥接亦须在具备感染控制条件的监护条件下完成",
            ("D4.2.R04",),
        )
    return Candidate(
        "EVAR",
        CandidateStatus.CONDITIONAL,
        "仅作桥接或过渡（控制出血、争取时间），不视为已完成感染源控制；须 MDT 确认",
        ("D4.2.R03",),
    )


def _d42_gaps(state: GuidelineState) -> tuple[str, ...]:
    """D4.2 缺什么 —— 八项方案要素 + 培养/能力的状态说明。"""
    gaps = list(missing_fields(state, _D42_FIELDS))
    if _culture(state) in _CULTURE_UNRESOLVED:
        gaps.append(f"D4.BLOOD_CULTURE：{_culture(state)}（未采集/待结果不等于阴性）")
    if _capability(state) is None:
        gaps.append("D4.CENTRE_CAPABILITY：中心能力未确认（决定转诊/协作，不阻断方案方向）")
    missing = _plan_items_missing(state)
    if missing:
        gaps.append("确定性方案要素不全：" + "、".join(missing))
    return tuple(gaps)


def d4_2(state: GuidelineState) -> NodeOutcome:
    """D4.2：给出血培养/抗菌准备项、方式比较与方案完整性清单 + 两列候选。"""
    outcome = evaluate_node("D4.2", state, _D42_SLOTS)
    extra_entries, extra_verdicts = display_entries("D4.2", state, _D42_DISPLAY_RULES)
    return replace(
        outcome,
        verdicts={**outcome.verdicts, **extra_verdicts},
        entries=(*outcome.entries, *extra_entries),
        gaps=_d42_gaps(state),
        candidates=(_open_control_candidate(state), _evar_bridge_candidate(state)),
    )


# ---------------------------------------------------------------------------
# D4.3 移植物感染：MAGIC 定性、移植物处理与长期管理
# ---------------------------------------------------------------------------
_D43_FIELDS: tuple[tuple[str, str], ...] = (
    (
        "D4.MAGIC_CLINICAL",
        "MAGIC 临床/手术标准（主要：周围脓液/移植物暴露或窦道/瘘/植入于感染部位）",
    ),
    ("D4.MAGIC_IMAGING", "MAGIC 影像标准（须结合植入后时间窗与序列影像）"),
    ("D4.MAGIC_LAB", "MAGIC 实验室标准（标本来源不可混用）"),
    ("D4.GRAFT_EXTENT", "移植物受累范围（局灶/广泛/吻合口/腔内/瘘相关）"),
    ("D4.GRAFT_FITNESS", "根治性手术耐受性（可接受/高风险/极高风险）"),
    ("D4.SOURCE_CONTROL", "感染源控制与移植物处理计划"),
    ("D4.VGEI_LONGTERM_PLAN", "长期抗菌与复查责任计划"),
)


def _verdict_gap(state: GuidelineState, spec_id: str, label: str) -> str | None:
    """「结论型」字段缺口的专用判据 —— 不把「疑似」当不确定。

    ``D4.MAGIC_STATUS`` 与 ``D4.CANDIDA`` 的允许值里都有「疑似」，而
    ``SPEC_UNCERTAIN_TOKENS`` 把「疑似」列为不确定词（对绝大多数枚举是对的）。
    这两个字段的「疑似」是**团队给出的确定结论**，用通用缺口函数会把它报成
    「取值不确定」——缺口清单里出现「D4.MAGIC_STATUS：疑似」会让读者以为
    评估没做完，而实际上做完了、结论就是疑似。
    """
    value = token(state.node_field(spec_id))
    if value is None or value == "未评价":
        return f"{spec_id}（{label}）：未评价"
    return None


def _magic_stated(state: GuidelineState) -> str | None:
    """``D4.MAGIC_STATUS`` 的团队结论；「未完成评估」「证据冲突」折成 ``None``。

    用 ``token`` 而不是 ``spec_token``：``疑似`` 在本字段是**一个确定的结论**，
    折成 ``None`` 会让「疑似 VGEI」变成「判不了」，整条 MAGIC 计算都失效。
    """
    value = token(state.node_field("D4.MAGIC_STATUS"))
    if value is None or value in ("未完成评估", "证据冲突"):
        return None
    return value


def _magic_items(state: GuidelineState, spec_id: str) -> list[str]:
    """读一类 MAGIC 证据清单，统一成字符串列表。"""
    values = multi_field(state, spec_id) or []
    return [str(v) for v in values]


def _magic_counts(
    state: GuidelineState,
) -> tuple[dict[str, list[str]], dict[str, int], dict[str, int]] | None:
    """统计三类 MAGIC 标准的主要/次要命中数；三类全空返回 ``None``。"""
    raw = {cat: _magic_items(state, f"D4.MAGIC_{cat.upper()}") for cat in _MAGIC_CATEGORIES}
    if not any(raw.values()):
        return None
    major = {c: sum(1 for t in raw[c] if t in _MAGIC_MAJOR[c]) for c in _MAGIC_CATEGORIES}
    minor = {c: sum(1 for t in raw[c] if t in _MAGIC_MINOR[c]) for c in _MAGIC_CATEGORIES}
    return raw, major, minor


def _category_assessed(items: list[str], category: str) -> bool:
    """这一类标准是否**真的被评估过** —— 至少有一条能归类的条目。

    只写了「移植物周围气体」这种缺时间窗的写法时，条目归不了类，这一格等于没评；
    若把它算成「已评估且为阴性」，就会得出「不支持 VGEI」——正好是规格 J 列
    明令禁止的那个降级（「标准未完整收集时=未完成评估，不得标不支持」）。
    """
    known = _MAGIC_MAJOR[category] | _MAGIC_MINOR[category] | set(_NEGATIVE_ITEMS)
    return any(item in known for item in items)


def _magic_computed(state: GuidelineState) -> str | None:
    """按 MAGIC 定义算出的结论：确诊 / 疑似 / 不支持 / 判不了（``None``）。

    判定顺序：确诊 → 疑似 → 不支持，不能颠倒：
    · 确诊 = 某类别 ≥1 项主要标准 **+ 另一类别**任一标准（同类别不算）；
    · 疑似 = 任一类别 1 项主要标准，或**不同类别** ≥2 项次要标准；
    · 不支持 = 三类清单都收了、一条标准都不满足。**任一类为空就不给这个结论**
      （规格 J 列：「标准未完整收集时=未完成评估，不得标不支持」）。
    """
    counts = _magic_counts(state)
    if counts is None:
        return None
    raw, major, minor = counts
    for cat in _MAGIC_CATEGORIES:
        others = [o for o in _MAGIC_CATEGORIES if o != cat]
        if major[cat] and any(major[o] + minor[o] for o in others):
            return "确诊"
    if any(major.values()):
        return "疑似"
    if sum(1 for c in _MAGIC_CATEGORIES if minor[c]) >= 2:
        return "疑似"
    if all(_category_assessed(raw[c], c) for c in _MAGIC_CATEGORIES):
        return "不支持"
    return None


def _magic_effective(state: GuidelineState) -> str | None:
    """逐项标准与团队结论中**更严**的那个 —— 不允许静默降级。"""
    computed = _magic_computed(state)
    stated = _magic_stated(state)
    candidates = [v for v in (computed, stated) if v in _MAGIC_ORDER]
    if not candidates:
        return None
    return max(candidates, key=lambda v: _MAGIC_ORDER[v])


def _magic_is(state: GuidelineState, *values: str) -> Verdict:
    """MAGIC 结论是否落在给定档位 —— 用布尔而不是 :func:`eq`。

    这里不能走 ``eq``：``疑似`` 在 ``UNCERTAIN_TOKENS`` 里，``eq(x, "确诊")`` 对
    「疑似」会返回 UNKNOWN 而不是「不成立」。而 ``_magic_effective`` 已经把原始
    取值归一成三个**结论**（确诊/疑似/不支持），到这一步它不再是「不确定的词」。
    """
    value = _magic_effective(state)
    if value is None:
        return None
    return value in values


def _magic_conflict(state: GuidelineState) -> bool:
    """逐项标准与团队结论是否互相矛盾（用于留缺口，不改变结论）。"""
    computed = _magic_computed(state)
    stated = _magic_stated(state)
    return computed is not None and stated is not None and computed != stated


def _unclassified_magic(state: GuidelineState) -> tuple[str, ...]:
    """三类清单里**未被任何标准命中**的条目 —— 需要人工归类。

    这一项存在的理由：条目名是团队手写的，写成「移植物周围气体」时缺了「植入后
    ≥7 周」这个时间窗，按 MAGIC 就不能计为主要标准。静默忽略会让「写了但不算」
    看起来像「没写」，故显式列出来。
    """
    counts = _magic_counts(state)
    if counts is None:
        return ()
    raw = counts[0]
    out: list[str] = []
    for cat in _MAGIC_CATEGORIES:
        known = _MAGIC_MAJOR[cat] | _MAGIC_MINOR[cat]
        for item in raw[cat]:
            if item not in known and item not in _NEGATIVE_ITEMS:
                out.append(item)
    return tuple(out)


def _graft_fitness(state: GuidelineState) -> object:
    """``D4.GRAFT_FITNESS`` —— 根治性手术耐受性。"""
    return spec_token(state.node_field("D4.GRAFT_FITNESS"))


def _graft_extent(state: GuidelineState) -> object:
    """``D4.GRAFT_EXTENT`` —— 移植物受累范围。"""
    return spec_token(state.node_field("D4.GRAFT_EXTENT"))


def _aef_warning(state: GuidelineState) -> object:
    """``D4.AEF_WARNING`` —— 主动脉肠瘘的预警出血信号。"""
    return token(state.node_field("D4.AEF_WARNING"))


def _d43_r01(state: GuidelineState) -> Verdict:
    """P0-A：活动性出血或血流动力学不稳定 → G05 优先。

    在 VGEI/瘘的语境下，出血与休克都指向「感染已侵蚀到相邻结构」，这是本节点
    唯一必须先于方案讨论处理的事。腔内止血可以按团队决定作桥接，但**不能默认
    它完成了感染源控制**（规格 G 列原话）。
    """
    return or_(_active_gi_bleed(state), _hemodynamically_unstable(state))


def _d43_r07(state: GuidelineState) -> Verdict:
    """P0-A：疑似主动脉肠瘘或先兆出血 → G05 + 血管外科/消化道外科。

    与 ``R01`` 的分野：``R01`` 判「已经不稳」，本规则判「有 AEF 警示信号但还没崩」。
    先兆性少量出血（herald bleed）是肠瘘破裂前的最后一段窗口，按规格必须提前拦截，
    不能等它变成大量出血才进急症路径。
    """
    return or_(
        eq(_aef_warning(state), *_AEF_BLEEDING),
        eq(_fistula(state), "主动脉肠瘘"),
        and_(_active_gi_bleed(state), _graft_history(state)),
    )


def _d43_r02(state: GuidelineState) -> Verdict:
    """P1：确诊/高度疑似 VGEI 且可耐受根治性手术 → 评估移植物去除 + 清创 + 重建。"""
    return and_(
        _magic_is(state, "确诊", "疑似"),
        eq(_graft_fitness(state), "可接受", "高风险"),
    )


def _d43_r03(state: GuidelineState) -> Verdict:
    """P1：根治性手术风险极高 → 个体化抗感染 / 保留 / 姑息，须记录未根治风险。"""
    return and_(
        _magic_is(state, "确诊", "疑似"),
        eq(_graft_fitness(state), "极高风险"),
    )


def _d43_r04(state: GuidelineState) -> Verdict:
    """P1：存在主动脉肠瘘 → 纳入消化道外科共同评估（不得只输出腔内方案）。"""
    return or_(
        eq(_fistula(state), "主动脉肠瘘"),
        eq(_aef_warning(state), *_AEF_BLEEDING),
    )


def _d43_r05(state: GuidelineState) -> Verdict:
    """P3：按 MAGIC 计算「疑似 VGEI」（保存逐项命中，不自动等同确诊）。"""
    return _magic_is(state, "疑似")


def _d43_r06(state: GuidelineState) -> Verdict:
    """P1：1 项主要标准 + 另一类别任一标准 → 符合 MAGIC 确诊标准，待医生确认。"""
    return _magic_is(state, "确诊")


def _d43_r08(state: GuidelineState) -> Verdict:
    """P1：MDT 必须明确治疗目标与移植物处理方案（治愈性/桥接/长期抑制/姑息）。

    「目标」这个词是规格的关键：不写目标的方案无法评价成败，也无法在以后再感染时
    判断该升级还是转姑息。故本规则恒要求目标显式化。
    """
    if _magic_is(state, "确诊", "疑似") is not True:
        return False
    return _source_control(state) in (None, "未决定") or not state.node_field(
        "D4.VGEI_LONGTERM_PLAN"
    )


def _d43_r09(state: GuidelineState) -> Verdict:
    """P1：确诊/持续疑似 VGEI 需长期抗菌与复查计划 + 复发/出血警示与责任人。"""
    return and_(
        _magic_is(state, "确诊", "疑似"),
        not_(bool(state.node_field("D4.VGEI_LONGTERM_PLAN"))),
    )


def _d43_r90(state: GuidelineState) -> Verdict:
    """P2：MAGIC 未完成、移植物范围不清或瘘状态未知 → 未完成评估，不能排除 VGEI。"""
    return any(
        (
            _magic_effective(state) is None,
            _graft_extent(state) is None,
            _fistula(state) is None,
        )
    )


def _d43_r91(state: GuidelineState) -> Verdict:
    """P3 出口：证据不支持 VGEI/瘘 → 转 D6（术后随访）或其他病因。"""
    return True


_D43_DISPLAY_RULES: tuple[DisplayRule, ...] = (
    DisplayRule(
        rule_id="D4.3.R04",
        source_row="D4.3!14",
        statement="存在主动脉肠瘘 → 消化道外科共同评估肠道缺损与感染源控制（不可只给腔内方案）",
        judge=_d43_r04,
    ),
    DisplayRule(
        rule_id="D4.3.R05",
        source_row="D4.3!20",
        statement="MAGIC 满足 1 项主要标准或不同类别 ≥2 项次要标准 → 标记「疑似 VGEI」，不等同确诊",
        judge=_d43_r05,
    ),
    DisplayRule(
        rule_id="D4.3.R06",
        source_row="D4.3!21",
        statement="1 项主要标准 + 另一类别任一标准 → 符合 MAGIC 确诊标准（待授权医生确认）",
        judge=_d43_r06,
    ),
    DisplayRule(
        rule_id="D4.3.R08",
        source_row="D4.3!23",
        statement="MDT 明确治愈性/桥接/长期抑制/姑息目标，并评估去除范围、清创、重建与瘘修补",
        judge=_d43_r08,
    ),
    DisplayRule(
        rule_id="D4.3.R09",
        source_row="D4.3!25",
        statement="确诊或持续疑似 VGEI → 长期抗菌、临床/实验室/影像复查与再感染/出血警示",
        judge=_d43_r09,
    ),
)

_D43_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D4.3.R01",
        priority="P0-A",
        source_row="D4.3!11",
        statement="活动性出血或血流动力学不稳定 → G03/G05 优先；腔内止血只作桥接，不视为完成感染源控制",
        judge=_d43_r01,
        exit_kind="escalate",
        exit_value="G05",
        # 规格 M 列：「下一节点/终点：G03、G05、D4.3 / 动作：G03/G05+MDT；**后续**D4.3」
        # 三个值都记下；``also_next`` 刻意留空 —— D4.3 指回当前节点，本轮不再走一遍，
        # 「后续」是急诊处置与 MDT 之后的事，不是本次运行能自动执行的跳转。
        next_nodes=("G03", "G05", "D4.3"),
        link=Link.FOLLOW_UP,
    ),
    RuleSlot(
        rule_id="D4.3.R07",
        priority="P0-A",
        source_row="D4.3!22",
        statement="疑似主动脉肠瘘或先兆/活动性出血 → 立即 G03/G05 并交血管外科与消化道外科",
        judge=_d43_r07,
        exit_kind="escalate",
        exit_value="G05",
        # 规格 M 列：「G03、G05、D4.3 / 动作：G03/G05**+**D4.3 MDT」——加号表示并行。
        next_nodes=("G03", "G05", "D4.3"),
        link=Link.PARALLEL,
    ),
    RuleSlot(
        rule_id="D4.3.R02",
        priority="P1",
        source_row="D4.3!12",
        statement="确诊/高度疑似 VGEI 且可耐受 → 评估移植物部分/全部去除、清创与重建（目标由 MDT 定）",
        judge=_d43_r02,
        exit_kind="terminal",
        exit_value="感染MDT：移植物去除范围 + 清创 + 重建（材料与疗程由 MDT 决定）",
    ),
    RuleSlot(
        rule_id="D4.3.R03",
        priority="P1",
        source_row="D4.3!13",
        statement="根治性手术风险极高 → 个体化抗感染/保留/姑息；须记录再感染与出血风险及患者目标",
        judge=_d43_r03,
        exit_kind="terminal",
        exit_value="感染MDT：个体化或姑息路径（记录未根治的再感染与出血风险）",
    ),
    RuleSlot(
        rule_id="D4.3.R90",
        priority="P2",
        source_row="D4.3!10",
        statement="MAGIC 关键证据/时间条件未收集、移植物范围不清或瘘状态未知 → 未完成评估，不能排除 VGEI",
        judge=_d43_r90,
        exit_kind="terminal",
        exit_value="人工复核（MAGIC 评估未完成或范围/瘘状态待明确）",
    ),
    RuleSlot(
        rule_id="D4.3.R91",
        priority="P3",
        source_row="D4.3!9",
        statement="证据不支持 VGEI/瘘 → 转 D6 或其他病因路径；活动性出血/休克 → G03/G05",
        judge=_d43_r91,
        exit_kind="next",
        exit_value="D6.1",
    ),
)


def _removal_candidate(state: GuidelineState) -> Candidate:
    """「移植物去除 + 重建（根治）」这一列。"""
    fitness = _graft_fitness(state)
    if fitness is None:
        return Candidate(
            "移植物去除 + 重建（根治）",
            CandidateStatus.UNASSESSED,
            "根治性手术耐受性未评价",
            ("D4.3.R90",),
        )
    if fitness == "极高风险":
        return Candidate(
            "移植物去除 + 重建（根治）",
            CandidateStatus.MDT,
            "极高风险：由 MDT 权衡根治与保留策略；须记录未根治的再感染与出血风险",
            ("D4.3.R03",),
        )
    if _capability(state) == "不具备":
        return Candidate(
            "移植物去除 + 重建（根治）",
            CandidateStatus.MDT,
            "本中心不具备感染性主动脉 MDT/重建能力 → 转诊高容量中心协作",
            ("D4.2.R04",),
        )
    if _graft_extent(state) is None:
        return Candidate(
            "移植物去除 + 重建（根治）",
            CandidateStatus.CONDITIONAL,
            "移植物受累范围未明确：去除范围与重建路径无法确定",
            ("D4.3.R90",),
        )
    return Candidate(
        "移植物去除 + 重建（根治）",
        CandidateStatus.MDT,
        "去除范围（部分/全部）、重建方式与材料须 MDT 确认",
        ("D4.3.R02",),
    )


def _retention_candidate(state: GuidelineState) -> Candidate:
    """「保留移植物 + 长期抑制（姑息）」这一列。

    与 ``D4.2`` 的桥接列同理：保留策略**永不是「可行候选」**——它把感染留在体内，
    只能作为 MDT 权衡后的选择，且必须写明复发风险与随访责任人。
    """
    if _magic_effective(state) is None:
        return Candidate(
            "保留移植物 + 长期抑制（姑息）",
            CandidateStatus.UNASSESSED,
            "MAGIC 评估未完成：保留策略的复发风险无从评估",
            ("D4.3.R90",),
        )
    return Candidate(
        "保留移植物 + 长期抑制（姑息）",
        CandidateStatus.MDT,
        "须 MDT 确认，并记录再感染/出血风险、随访责任人与升级条件",
        ("D4.3.R03", "D4.3.R09"),
    )


def _d43_gaps(state: GuidelineState) -> tuple[str, ...]:
    """D4.3 缺什么 —— MAGIC 三类清单 + 范围/耐受性/外科参与 + 计划类字段。"""
    gaps = list(missing_fields(state, _D43_FIELDS))
    for spec_id, label in (
        ("D4.MAGIC_STATUS", "MAGIC 诊断状态"),
        ("D4.CANDIDA", "念珠菌感染证据（明确/疑似/未发现）"),
    ):
        gap = _verdict_gap(state, spec_id, label)
        if gap:
            gaps.append(gap)
    if _magic_effective(state) is None:
        gaps.append("MAGIC：三类标准未收全，结论记「未完成评估」（不得标「不支持」）")
    if _magic_conflict(state):
        computed, stated = _magic_computed(state), _magic_stated(state)
        gaps.append(
            f"MAGIC：逐项标准算出「{computed}」而团队结论为「{stated}」，已按更严一档处理，需复核"
        )
    unclassified = _unclassified_magic(state)
    if unclassified:
        gaps.append("MAGIC 标准项未归类（缺时间窗或非标准写法）：" + "、".join(unclassified))
    if _d43_r04(state) is True and token(state.node_field("D4.ENTERIC_SURGEON")) not in (
        "已参与",
        "已联系",
    ):
        gaps.append("D4.ENTERIC_SURGEON：主动脉肠瘘需消化道外科参与（尚未联系）")
    return tuple(gaps)


def d4_3(state: GuidelineState) -> NodeOutcome:
    """D4.3：算出 MAGIC 结论、给出去除与保留两列候选 + MDT 待办清单。"""
    outcome = evaluate_node("D4.3", state, _D43_SLOTS)
    extra_entries, extra_verdicts = display_entries("D4.3", state, _D43_DISPLAY_RULES)
    return replace(
        outcome,
        verdicts={**outcome.verdicts, **extra_verdicts},
        entries=(*outcome.entries, *extra_entries),
        gaps=_d43_gaps(state),
        candidates=(_removal_candidate(state), _retention_candidate(state)),
    )


#: 本模块提供的节点（键与规格 sheet 名一致）。
NODES: dict[str, NodeFn] = {
    "D4.1": d4_1,
    "D4.2": d4_2,
    "D4.3": d4_3,
}

#: 已实现的规则 → 规格源行。``D4.2``/``D4.3`` 的并列义务以展示性条目留痕。
RULE_ROWS: dict[str, str] = {
    **{slot.rule_id: slot.source_row for slot in _D41_SLOTS},
    **{slot.rule_id: slot.source_row for slot in _D42_SLOTS},
    **{rule.rule_id: rule.source_row for rule in _D42_DISPLAY_RULES},
    **{slot.rule_id: slot.source_row for slot in _D43_SLOTS},
    **{rule.rule_id: rule.source_row for rule in _D43_DISPLAY_RULES},
}
