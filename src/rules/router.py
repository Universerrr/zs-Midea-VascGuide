"""术前路由判定流水线 —— V3.1(2) 的 **54 条判定规则**。

对照表见 ``src/rules/RULESET.md``（rule_id ↔ 函数 ↔ 源行）。本模块是规格的**第二真源**：
``RULES`` 字典里的 ``source`` 指向 V3.1(2) 的具体单元格，改规则必须同步改表。

三条语义（不可妥协）
--------------------
1. **三值求值**：``True`` / ``False`` / ``None``(=UNKNOWN)。Kleene 合取：任一 ``False`` → ``False``；
   否则任一 ``None`` → ``None``；全 ``True`` → ``True``。**UNKNOWN 不短路、不当 False**。
2. **不确定值**（可疑/疑似/未评价/不可评价/未知…）先折成 ``UNKNOWN``，永不参与「成立/不成立」。
3. **未提及 ≠ 阴性**：缺字段只能是 ``UNKNOWN``，不得当 ``False`` 放行。

出口（五值，与 ``RouteKind`` 对应）
----------------------------------
``G05`` 急诊 / ``G06`` 复杂 / ``G07`` 指南 → ``MODE``；
``人工复核`` → ``REVIEW``；``资料核实`` → ``VERIFY``。
优先级：**G03 回流 > G06 > VERIFY**；``G07`` 是排他分支，**兜底是 VERIFY**。

范围
----
只实现 G03(21) / G04(18) / G08(11) / G09(4) = **54 条**。
G00–G02 的 9 条属「读取资料 / 证据质量 / 事实抽取」，归 LLM 抽取层，不进本模块。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from ..models.preop.case import UNCERTAIN_TOKENS, VascCase
from ..models.preop.measurement import VascMeasurement
from ..models.preop.recommendation import RouteMode
from .version import RULESET_VERSION

# ===========================================================================
# 出口与结果
# ===========================================================================
Verdict = bool | None
"""``None`` 表示 UNKNOWN —— 不是「没命中」，也不是「阴性」。"""

NextNode = Literal["G05", "G06", "G07", "人工复核", "资料核实"]
EmergencyLevel = Literal["明确危险", "高度疑似", "不能排除", "未触发", "冲突"]
ConfidenceLevel = Literal["高", "中", "低"]
TaaaGrade = Literal["符合", "疑似", "不符合", "不可评价"]
"""``CL.TAAA_IV``（Ⅳ型 TAAA 解剖扩展）的四值 —— V3.1(2) 允许值原样。

它是**判定产物**（映射表 ``01!163``），不进 ``VascCase``、不进 160 字段清单；
由 :func:`g08_r08_taaa_iv_label` 算出后随 :class:`RouteResult` 带给下游工具。
"""


class RouteKind(StrEnum):
    """路由结论的大类。与 ``NextNode`` 五值一一对应。"""

    MODE = "进入模式"
    REVIEW = "人工复核"
    VERIFY = "资料核实"


FALLBACK_VERIFY = "FALLBACK.VERIFY"
"""兜底标识：**没有任何规则能判定**时的出口。

为什么不借某条规则的 id：兜底不是规则判的结论，而是「判不了」这件事本身。
借用 ``G03.R15`` / ``G04.R14`` 会让 ``RULESET.md`` 的出口列说谎
（它们的出口是 G06 / G07，不是资料核实）。
"""

MANUAL_URGENCY = "MANUAL.URGENCY"
"""人工越级标识：医生置位 ``urgency_flag``，跳过自动判定。"""

OUTCOME_PRIORITY: dict[RouteKind, int] = {
    RouteKind.REVIEW: 3,  # 人工复核压过一切模式
    RouteKind.MODE: 2,
    RouteKind.VERIFY: 1,
}
"""冲突时取优先级高者。**VERIFY 最低**：能进模式就不因资料瑕疵退回核查。"""


@dataclass(frozen=True)
class RuleTrace:
    """单条规则的求值留痕（审计用；不要塞进 case）。"""

    rule_id: str
    verdict: Verdict
    exit: NextNode


@dataclass
class RouteResult:
    """路由结论 + 判定轨迹 + 证据缺口。"""

    kind: RouteKind
    rule_id: str
    reason: str
    next_node: NextNode
    emergency_level: EmergencyLevel
    mode: RouteMode | None = None
    evidence_gaps: list[str] = field(default_factory=list)
    confidence_level: ConfidenceLevel = "中"
    class_output: list[str] = field(default_factory=list)
    cl_taaa_iv: TaaaGrade | None = None
    """Ⅳ型 TAAA 解剖扩展（判定产物）。``None`` = 没跑到 G08（急诊出口）。

    为什么不放 ``VascCase``：它是规则算出来的，不是抽取出来的病例事实。
    放进去会让「病例输入」与「判定输出」混在一处，且 160 字段的契约数字会失守。
    下游（指南引擎）按「条目子集」需要它，从 ``route_result`` 取即可。
    """
    k_identity_conflict: str | None = None
    k_diag_conflict: str | None = None
    k_classifiable: str | None = None
    trace: list[RuleTrace] = field(default_factory=list)
    ruleset_version: str = RULESET_VERSION


# ===========================================================================
# 三值求值原语
# ===========================================================================
def _token(v: object) -> object:
    """枚举取 ``value``；其余原样。"""
    return getattr(v, "value", v)


def _is_uncertain_token(v: object) -> bool:
    return isinstance(v, str) and v in UNCERTAIN_TOKENS


def _and(*vs: Verdict) -> Verdict:
    if any(v is False for v in vs):
        return False
    if any(v is None for v in vs):
        return None
    return True


def _or(*vs: Verdict) -> Verdict:
    if any(v is True for v in vs):
        return True
    if any(v is None for v in vs):
        return None
    return False


def _not(v: Verdict) -> Verdict:
    return None if v is None else (not v)


def _eq(value: object, *allowed: object) -> Verdict:
    """取值相等。**显式列出即命中；未列出且属不确定值 → UNKNOWN**。

    顺序必须 ``t in allowed`` 在前：规格里「医生明确写疑似 rAAA」把「疑似」当
    **阳性触发**（G03.R04 / R07 / G08.R03）。若先折成 UNKNOWN，这些规则永不命中。
    反过来，规则没显式要「疑似」时它仍是 UNKNOWN，不能拿去支撑任何结论。
    """
    t = _token(value)
    if t is None:
        return None
    if t in allowed:
        return True
    if _is_uncertain_token(t):
        return None
    return False


def _has(values: list[object] | None, *targets: str) -> Verdict:
    """多选字段包含判断。有目标 → True；无目标但含不确定词 → UNKNOWN。"""
    if values is None:
        return None
    tokens = [_token(v) for v in values]
    if any(t in targets for t in tokens):
        return True
    if any(_is_uncertain_token(t) for t in tokens):
        return None
    return False


def _absent(values: list[object] | None, dangerous: tuple[str, ...]) -> Verdict:
    """多选字段**明确不含**危险项。空列表 → False（没有记录 ≠ 阴性）。"""
    if not values:
        return False
    tokens = [_token(v) for v in values]
    if any(t in dangerous for t in tokens):
        return False
    if any(_is_uncertain_token(t) for t in tokens):
        return None
    return True


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _lt(v: object, t: float) -> Verdict:
    x = _num(v)
    return None if x is None else x < t


def _gt(v: object, t: object) -> Verdict:
    x, y = _num(v), _num(t)
    return None if x is None or y is None else x > y


def _ge(v: object, t: float) -> Verdict:
    x = _num(v)
    return None if x is None else x >= t


def _between(v: object, lo: float, hi: float) -> Verdict:
    x = _num(v)
    return None if x is None else lo <= x <= hi


def _present(*vs: object) -> Verdict:
    """全部**有明确取值**（非 None、非不确定、列表非空）。"""
    out: list[Verdict] = []
    for v in vs:
        if isinstance(v, list):
            out.append(bool(v))
            continue
        t = _token(v)
        out.append(None if (t is None or _is_uncertain_token(t)) else True)
    return _and(*out)


def _missing(v: object) -> Verdict:
    """字段是否缺失（含不确定值）。"""
    t = _token(v)
    return t is None or _is_uncertain_token(t)


# ===========================================================================
# 求值上下文：物化派生 + 便捷访问
# ===========================================================================
ABD_BACK_SITES = ("上腹", "下腹", "脐周", "侧腹", "腰", "背")
"""「腹 / 背 / 侧腹 / 腰」—— 主动脉相关疼痛的部位集合。"""

HB_DROP_THRESHOLD_G_L = 30.0
"""Hb 快速下降阈值（拟定安全复核值）。48h 窗口由抽取层在 ``L.HB_COMPARABLE`` 内判断：
该布尔的语义就是「同患者 + 采样间隔 ≤48h + 单位一致」，规则层只读事实，不重复判定。"""

NRS_SEVERE = 7
NEWS2_REVIEW_LOW, NEWS2_REVIEW_HIGH, NEWS2_CRITICAL = 5, 6, 7
EGFR_LOW, SAC_GROWTH_MM, SBP_SHOCK, DIAMETER_AAA_MM = 30.0, 10.0, 80.0, 30.0


@dataclass
class _Ctx:
    """一次判定的上下文：case + 量化值 + 物化派生量（不进模型、不进哈希）。"""

    case: VascCase
    measurement: VascMeasurement | None = None

    def __post_init__(self) -> None:
        c = self.case
        self.p, self.t, self.d, self.q = c.p, c.t, c.d, c.q
        self.s, self.v, self.lab, self.c = c.s, c.v, c.lab, c.c
        self.n, self.gen, self.inf = c.n, c.gen, c.inf
        self.cl, self.k, self.r = c.cl, c.k, c.r

        # ---- 物化派生（F6 裁定：不落模型，只参与本次判定）----
        hb, hb_prev = _num(self.lab.hb), _num(self.lab.hb_prev)
        # 下降量 = 既往 − 当前（正值 = 下降）。写成当前 − 既往会得到负数，规则永不命中。
        self.hb_delta: float | None = None if hb is None or hb_prev is None else hb_prev - hb
        self.news2, self.news2_single3 = _news2(self.v)
        self.genetic_ctx = self.n.genetic_ctx or _derive_genetic_ctx(self.gen)
        self.inflammatory_ctx = self.n.inflammatory_ctx or _derive_inflammatory_ctx(self.inf)

        # ---- 运行时状态（编排层写入，非字段）----
        self.labels: list[str] = []
        self.diag_conflict: bool | None = None
        self.g04_blocking: list[Verdict] = []
        self.gaps_extra: list[str] = []
        self.taaa_iv: TaaaGrade | None = None


def _news2(v: object) -> tuple[float | None, bool | None]:
    """NEWS2 总分与「是否有单项 3 分」。

    7 项**全部可算**才给总分；单项 3 分只要该项可算即可判。
    ``Scale2经确认`` 未实现 → 直接 UNKNOWN（用错量表比没分数更危险）。
    """
    if _token(v.spo2_scale) == "Scale2经确认":  # type: ignore[attr-defined]
        return None, None

    def s_rr(x: float) -> int:
        return 3 if x <= 8 else 1 if x <= 11 else 0 if x <= 20 else 2 if x <= 24 else 3

    def s_spo2(x: float) -> int:
        return 3 if x <= 91 else 2 if x <= 93 else 1 if x <= 95 else 0

    def s_sbp(x: float) -> int:
        return 3 if x <= 90 else 2 if x <= 100 else 1 if x <= 110 else 0 if x <= 219 else 3

    def s_hr(x: float) -> int:
        return (
            3
            if x <= 40
            else 1
            if x <= 50
            else 0
            if x <= 90
            else 1
            if x <= 110
            else 2
            if x <= 130
            else 3
        )

    def s_temp(x: float) -> int:
        return 3 if x <= 35.0 else 1 if x <= 36.0 else 0 if x <= 38.0 else 1 if x <= 39.0 else 2

    comps: dict[str, int | None] = {
        "rr": None if _num(v.rr) is None else s_rr(_num(v.rr)),  # type: ignore[attr-defined]
        "spo2": None if _num(v.spo2) is None else s_spo2(_num(v.spo2)),  # type: ignore[attr-defined]
        "sbp": None if _num(v.sbp) is None else s_sbp(_num(v.sbp)),  # type: ignore[attr-defined]
        "hr": None if _num(v.hr) is None else s_hr(_num(v.hr)),  # type: ignore[attr-defined]
        "temp": None if _num(v.temp) is None else s_temp(_num(v.temp)),  # type: ignore[attr-defined]
    }
    acvpu = _token(v.acvpu)  # type: ignore[attr-defined]
    comps["acvpu"] = (
        None if acvpu is None or _is_uncertain_token(acvpu) else (0 if acvpu == "A" else 3)
    )
    oxygen = _token(v.oxygen)  # type: ignore[attr-defined]
    comps["oxygen"] = (
        None if oxygen is None or _is_uncertain_token(oxygen) else (2 if oxygen == "是" else 0)
    )

    values = list(comps.values())
    single3: bool | None = (
        True if any(s == 3 for s in values) else (None if any(s is None for s in values) else False)
    )
    total: float | None = (
        None if any(s is None for s in values) else float(sum(s for s in values if s))
    )
    return total, single3


def _derive_genetic_ctx(gen: object) -> str | None:
    """遗传证据明细 → 主状态（三层：``GEN.*`` → ``N.GENETIC_CTX`` → 规则）。

    只在抽取层没给主状态时兜底派生；证据不足以支撑结论时返回 ``None``。
    """
    st = _token(gen.syndrome_type)  # type: ignore[attr-defined]
    vc = _token(gen.variant_class)  # type: ignore[attr-defined]
    if st in ("Marfan", "Loeys-Dietz", "血管型EDS", "其他确诊综合征") or vc in ("致病", "可能致病"):
        return "确诊"
    fam = _token(gen.family_aneurysm)  # type: ignore[attr-defined]
    feats = gen.syndromic_features  # type: ignore[attr-defined]
    age = gen.aaa_age_onset  # type: ignore[attr-defined]
    if st == "疑似" or fam == "阳性" or feats or (isinstance(age, int) and age < 60):
        return "疑似遗传背景"
    if fam == "明确无相关史" and isinstance(age, int) and age >= 60 and not feats:
        return "无已知阳性线索"
    return None


def _derive_inflammatory_ctx(inf: object) -> str | None:
    """炎症证据明细 → 主状态。**单纯 CRP/ESR 升高不触发**（G04.R16 明文）。"""
    mantle = _token(inf.mantle_sign)  # type: ignore[attr-defined]
    wall = _token(inf.wall_fibrosis)  # type: ignore[attr-defined]
    if mantle == "明确存在":
        return "影像高度提示"
    if mantle == "可疑" or wall == "可疑":
        return "可疑"
    if mantle == "未见" and wall == "未见":
        return "无当前支持证据"
    return None


# ===========================================================================
# 通用判据片段
# ===========================================================================
def _aaa_confirmed(x: _Ctx) -> Verdict:
    """AAA 已确认：报告确认 / 既往已修复 / 量化直径 ≥30mm，三条通路任一。"""
    return _or(
        _eq(x.cl.aaa_reported, "确认"),
        _eq(x.d.dx_context, "确认", "既往已修复"),
        _ge(x.cl.aaa_diam, DIAMETER_AAA_MM),
    )


def _aaa_suspected(x: _Ctx) -> Verdict:
    """AAA 已确认或疑似（需要近端解剖分级的前置条件）。"""
    return _or(_aaa_confirmed(x), _eq(x.cl.aaa_reported, "疑似"), _eq(x.d.dx_context, "疑似"))


def _acute_abd_back_pain(x: _Ctx) -> Verdict:
    """本次新发/加重的腹背侧腰痛。"""
    return _and(_eq(x.s.pain_current, "当前新发", "本次进展"), _has(x.s.pain_site, *ABD_BACK_SITES))


def _branch_occluded(x: _Ctx) -> Verdict:
    if not x.c.branch:
        return None
    vals = [_token(v) for v in x.c.branch.values()]
    if any(v == "闭塞" for v in vals):
        return True
    if any(_is_uncertain_token(v) for v in vals):
        return None
    return False


# ===========================================================================
# G03 · 急诊判定（21 条）
# ===========================================================================
def g03_r01_direct_extravasation(x: _Ctx) -> Verdict:
    return _and(
        _eq(x.q.cta_enhance, "适当"),
        _eq(x.q.cta_phase, "动脉期"),
        _eq(x.c.extrav, "明确"),
        _eq(x.c.extrav_contig, "明确连续"),
        _eq(x.c.source_kind, "正式报告", "专家确认"),
    )


def g03_r02_aneurysm_with_hematoma(x: _Ctx) -> Verdict:
    return _and(
        _aaa_confirmed(x),
        _eq(x.c.hematoma, "明确"),
        _eq(x.c.hematoma_adj, "明确相邻"),
        _eq(x.c.source_kind, "正式报告", "专家确认"),
    )


def g03_r03_rupture_reported(x: _Ctx) -> Verdict:
    return _and(_eq(x.c.rupture_report, "明确破裂"), _eq(x.q.idmatch, "一致"))


def g03_r04_suspected_rupture_unstable(x: _Ctx) -> Verdict:
    trigger = _or(
        _eq(x.d.dx_context, "疑似"),
        _and(_aaa_confirmed(x), _or(_acute_abd_back_pain(x), _eq(x.s.syncope, "肯定", "可疑"))),
    )
    unstable = _or(_lt(x.v.sbp, SBP_SHOCK), _eq(x.v.conscious_change, "新发下降"))
    return _and(trigger, unstable)


def g03_r05_aaa_acute_pain_attributed(x: _Ctx) -> Verdict:
    return _and(
        _aaa_confirmed(x), _acute_abd_back_pain(x), _eq(x.s.pain_attrib, "明确相关", "怀疑相关")
    )


def g03_r06_aaa_sudden_pain_unattributed(x: _Ctx) -> Verdict:
    return _and(
        _aaa_confirmed(x),
        _acute_abd_back_pain(x),
        _eq(x.s.pain_onset, "突然"),
        _eq(x.s.pain_attrib, "未判断"),
    )


def g03_r07_rupture_not_excluded(x: _Ctx) -> Verdict:
    return _or(
        _eq(x.c.rupture_report, "疑似破裂"),
        _eq(x.c.extrav, "可疑"),
        _eq(x.c.hematoma, "可疑"),
        _and(_eq(x.c.source_kind, "算法候选"), _eq(x.q.report_vs_algo, "仅算法", "冲突")),
    )


def g03_r08_graft_with_gi_bleed(x: _Ctx) -> Verdict:
    graft = _or(_has(x.d.surg, "EVAR", "OSR"), _eq(x.c.graft, "有"))
    return _and(graft, _has(x.s.gi_bleed, "呕血", "黑便", "便血"))


def g03_r09_acute_limb_ischemia(x: _Ctx) -> Verdict:
    severe = _or(
        _has(x.s.limb, "运动障碍", "麻木"),
        _eq(x.s.pulse_deficit, "有", "缺失"),
        _branch_occluded(x),
    )
    return _and(_has(x.s.limb, "痛", "苍白", "发凉"), severe)


def g03_r10_news2_critical(x: _Ctx) -> Verdict:
    return _ge(x.news2, NEWS2_CRITICAL)


def g03_r11_news2_urgent_review(x: _Ctx) -> Verdict:
    band = _between(x.news2, NEWS2_REVIEW_LOW, NEWS2_REVIEW_HIGH)
    single = True if x.news2_single3 else (None if x.news2_single3 is None else False)
    return _or(band, single)


def g03_r12_no_enhanced_cta_with_clues(x: _Ctx) -> Verdict:
    clues = _or(_acute_abd_back_pain(x), _eq(x.s.syncope, "肯定", "可疑"))
    no_cta = _or(_eq(x.q.cta_enhance, "不足", "无动脉期"), _eq(x.q.cta_phase, "平扫", "未知"))
    return _and(_aaa_confirmed(x), clues, no_cta)


def g03_r13_key_fact_conflict(x: _Ctx) -> Verdict:
    return _or(
        _eq(x.q.idmatch, "不一致"),
        _has(x.q.conflict, "数值", "身份", "语义", "时间"),
        _eq(x.q.report_vs_algo, "冲突"),
    )


def g03_r14_screen_incomplete(x: _Ctx) -> Verdict:
    return _or(
        _not(_eq(x.q.current_assess, "本次已评估")),
        _not(_present(x.v.sbp, x.v.hr, x.v.acvpu)),
        _not(_present(x.s.pain_present, x.s.syncope, x.s.gi_bleed, x.s.limb)),
    )


def g03_r15_no_emergency_release(x: _Ctx) -> Verdict:
    return _and(
        _eq(x.q.idmatch, "一致"),
        _eq(x.q.current_assess, "本次已评估"),
        _eq(x.s.pain_present, "否"),
        _eq(x.s.syncope, "否定"),
        _absent(x.s.gi_bleed, ("呕血", "黑便", "便血")),
        _absent(x.s.limb, ("痛", "苍白", "发凉", "麻木", "无力", "运动障碍")),
        _present(x.v.sbp, x.v.hr, x.v.acvpu),
    )


def g03_r16_suspected_rupture_reported(x: _Ctx) -> Verdict:
    return _and(
        _eq(x.q.idmatch, "一致"),
        _or(_eq(x.d.dx_context, "疑似"), _eq(x.c.rupture_report, "疑似破裂")),
    )


def g03_r17_hb_drop_comparable(x: _Ctx) -> Verdict:
    return _and(
        _eq(x.lab.hb_comparable, True),
        _eq(x.lab.hb_confound, False),
        _ge(x.hb_delta, HB_DROP_THRESHOLD_G_L),
    )


def g03_r18_severe_sudden_pain_no_aaa(x: _Ctx) -> Verdict:
    severe = _or(_ge(x.s.pain_severity, NRS_SEVERE), _eq(x.s.pain_severe_screen, True))
    return _and(
        _eq(x.q.idmatch, "一致"),
        _eq(x.d.dx_context, "无相关记录"),
        _acute_abd_back_pain(x),
        _eq(x.s.pain_onset, "突然"),
        severe,
    )


def g03_r19_hb_drop_uncertain(x: _Ctx) -> Verdict:
    """数值差够大，但**可比性/干扰没有被确认为干净** → 只登记「差异待查」。

    这里把 UNKNOWN 按「未确认」处理而不是留成 UNKNOWN：R17 已经吃掉了
    「可比且无干扰」的情形，剩下的（False 或 None）都应进人工复核。
    这是**安全侧**的确定性判断——不主张快速失血，但绝不漏掉复核。
    """
    not_clean = _token(x.lab.hb_comparable) is not True or _token(x.lab.hb_confound) is not False
    return _and(_ge(x.hb_delta, HB_DROP_THRESHOLD_G_L), bool(not_clean))


def g03_r20_severe_hypotension(x: _Ctx) -> Verdict:
    return _lt(x.v.sbp, SBP_SHOCK)


def g03_r21_lactate_above_uln(x: _Ctx) -> Verdict:
    return _and(_gt(x.lab.lact, x.lab.lact_uln), _eq(x.q.unit, "有效", "可转换"))


# ===========================================================================
# G04 · 复杂度判定（18 条）
# ===========================================================================
def g04_r01_short_neck(x: _Ctx) -> Verdict:
    return _and(
        _between(x.n.neck_len, 4, 10), _eq(x.n.renal_l, "未受累"), _eq(x.n.renal_r, "未受累")
    )


def g04_r02_juxtarenal(x: _Ctx) -> Verdict:
    return _and(
        _ge(x.n.neck_len, 0),
        _lt(x.n.neck_len, 4),
        _eq(x.n.renal_l, "未受累"),
        _eq(x.n.renal_r, "未受累"),
    )


def g04_r03_pararenal(x: _Ctx) -> Verdict:
    renal = _or(_eq(x.n.renal_l, "明确受累"), _eq(x.n.renal_r, "明确受累"))
    return _and(renal, _eq(x.n.sma, "未受累"))


def g04_r04_visceral_pararenal(x: _Ctx) -> Verdict:
    renal = _or(_eq(x.n.renal_l, "明确受累"), _eq(x.n.renal_r, "明确受累"))
    return _and(renal, _eq(x.n.sma, "明确受累"), _eq(x.n.ca, "未受累"))


def g04_r05_taaa_iv_extension(x: _Ctx) -> Verdict:
    renal = _or(_eq(x.n.renal_l, "明确受累"), _eq(x.n.renal_r, "明确受累"))
    return _and(renal, _eq(x.n.sma, "明确受累"), _eq(x.n.ca, "明确受累"), _eq(x.n.thoracic, "否"))


def g04_r06_beyond_project_boundary(x: _Ctx) -> Verdict:
    return _eq(x.n.thoracic, "是")


def g04_r07_infection_or_fistula(x: _Ctx) -> Verdict:
    return _or(_eq(x.n.infection_ctx, "医师明确", "高度怀疑"), _eq(x.n.fistula_ctx, "明确", "疑似"))


def g04_r08_ifu_mismatch(x: _Ctx) -> Verdict:
    return _eq(x.n.seal_ifu, "不符")


def g04_r09_iliac_limitation(x: _Ctx) -> Verdict:
    iia = _or(_eq(x.n.iia_left, "闭塞", "严重狭窄"), _eq(x.n.iia_right, "闭塞", "严重狭窄"))
    return _and(_eq(x.n.iliac_bilat, "双侧"), iia)


def g04_r10_access_occlusion(x: _Ctx) -> Verdict:
    return _eq(x.n.access_occlusion, "双侧闭塞")


def g04_r11_prior_repair_reintervention(x: _Ctx) -> Verdict:
    evidence = _or(
        _eq(x.n.endoleak_type, "I", "III"),
        _eq(x.n.graft_major, "组件分离", "移位", "明显闭塞"),
        _ge(x.n.sac_growth, SAC_GROWTH_MM),
    )
    return _and(_not(_eq(x.n.prev_repair, "无")), evidence)


def g04_r12_physiologic_risk(x: _Ctx) -> Verdict:
    return _or(
        _lt(x.n.egfr, EGFR_LOW),
        _eq(x.n.dialysis, "是"),
        _eq(x.n.heart_class, "III", "IV"),
        _eq(x.n.resp_support, "长期吸氧", "无创通气", "有创通气"),
    )


def g04_r13_insufficient_for_low_complexity(x: _Ctx) -> Verdict:
    anatomy = _or(
        _eq(x.n.cta_status, "缺失", "部分", "冲突"),
        _missing(x.n.neck_len),
        _missing(x.n.renal_l),
        _missing(x.n.renal_r),
        _missing(x.n.sma),
        _missing(x.n.ca),
        _eq(x.n.renal_l, "不可评价"),
        _eq(x.n.renal_r, "不可评价"),
        _eq(x.n.sma, "不可评价"),
        _eq(x.n.ca, "不可评价"),
    )
    return _and(_aaa_suspected(x), anatomy)


def g04_r14_low_complexity_to_guideline(x: _Ctx) -> Verdict:
    """低复杂放行：**没有任何阻断型复杂规则命中**，且核心影像可评价。

    ⚠️ 只有「阻断型」（R01–R12）参与放行判断。R15–R18 是**加分型**（遗传/炎症线索），
    其 `UNKNOWN` 不得阻断放行——规格原文「仅因未做遗传检测/CRP 不强制所有常规病例升复杂」。
    真正的资料缺口由 R13 管（核心解剖字段缺失 → 资料核实）。
    """
    if not x.g04_blocking:
        return None
    # 必须**全部明确 False**：任一条 UNKNOWN 都意味着「判不了」，不能当作没命中。
    # 例：``n.seal_ifu`` 为「未核验」时 R08 返回 UNKNOWN —— 没核验 ≠ 符合 IFU。
    all_clear = all(v is False for v in x.g04_blocking)
    return _and(all_clear, _eq(x.n.cta_status, "完整", "部分"))


def g04_r15_genetic_confirmed(x: _Ctx) -> Verdict:
    return _eq(x.genetic_ctx, "确诊")


def g04_r16_inflammatory(x: _Ctx) -> Verdict:
    return _eq(x.inflammatory_ctx, "临床确认", "影像高度提示")


def g04_r17_genetic_suspected(x: _Ctx) -> Verdict:
    feats = x.gen.syndromic_features
    feature_hit = True if feats else (None if feats is None else False)
    return _and(
        _or(_aaa_confirmed(x), _eq(x.d.dx_context, "疑似")),
        _or(
            _lt(x.gen.aaa_age_onset, 60),
            _eq(x.gen.family_aneurysm, "阳性"),
            feature_hit,
        ),
    )


def g04_r18_inflammation_vs_infection(x: _Ctx) -> Verdict:
    return _and(
        _eq(x.inflammatory_ctx, "可疑", "影像高度提示", "临床确认"),
        _eq(x.n.infection_ctx, "医师明确", "高度怀疑"),
    )


# ===========================================================================
# G08 · 病种分类（11 条）
# ===========================================================================
def g08_r01_d1_asymptomatic(x: _Ctx) -> Verdict:
    aaa = _or(_eq(x.cl.aaa_reported, "确认"), _ge(x.cl.aaa_diam, DIAMETER_AAA_MM))
    return _and(
        aaa,
        _eq(x.cl.symptom_aaa, "无当前症状", "明确归因其他"),
        _eq(x.cl.aaa_rupture, "未见相关证据"),
    )


def g08_r02_d2_symptomatic_unruptured(x: _Ctx) -> Verdict:
    aaa = _or(_eq(x.cl.aaa_reported, "确认"), _ge(x.cl.aaa_diam, DIAMETER_AAA_MM))
    return _and(
        aaa,
        _eq(x.cl.symptom_aaa, "医师确认相关", "医师怀疑相关"),
        _not(_eq(x.cl.aaa_rupture, "明确")),
    )


def g08_r03_d3_ruptured(x: _Ctx) -> Verdict:
    return _eq(x.cl.aaa_rupture, "明确", "疑似")


def g08_r04_d4_infected(x: _Ctx) -> Verdict:
    return _or(
        _eq(x.cl.infection, "原生感染明确", "原生感染疑似", "移植物感染明确", "移植物感染疑似"),
        _eq(x.cl.fistula, "主动脉肠瘘", "主动脉腔静脉瘘", "其他瘘", "疑似"),
    )


def g08_r05_d5_special(x: _Ctx) -> Verdict:
    return _or(
        _eq(x.cl.pau, "明确", "可疑"),
        _eq(x.cl.imh, "明确", "可疑"),
        _eq(x.cl.iaad, "明确", "可疑"),
        _eq(x.cl.saccular, "明确", "疑似"),
        _eq(x.cl.pseudo, "明确", "疑似"),
    )


def g08_r06_d6_postop(x: _Ctx) -> Verdict:
    return _and(_not(_eq(x.cl.prev_evor, "无")), _present(x.cl.post_reason))


def g08_r07_d7_iliac(x: _Ctx) -> Verdict:
    return _has(x.cl.iliac_site, "左侧", "右侧", "CIA", "IIA", "EIA")


def g08_r08_taaa_iv_label(x: _Ctx) -> Verdict:
    renal = _or(_eq(x.n.renal_l, "明确受累"), _eq(x.n.renal_r, "明确受累"))
    return _and(_eq(x.n.thoracic, "否"), _eq(x.n.sma, "明确受累"), _eq(x.n.ca, "明确受累"), renal)


def _taaa_grade(v: Verdict) -> TaaaGrade:
    """``G08.R08`` 的三值 → ``CL.TAAA_IV`` 的四值。

    「疑似」当前**不可达**，这是有意的：三值求值只能给 成立 / 不成立 / 未判定，
    而「疑似」要求「部分关键字段明确受累、部分可疑」这种细分判据 —— V3.1(2)
    没给。宁可不产出这个值，也不去猜一个听起来合理的中间态。

    于是映射是确定的：命中 → 符合；明确不命中 → 不符合；有未判定项 → 不可评价。
    「不可评价」与「不符合」必须分开：前者是资料不足，后者是已排除，混用会
    让指南引擎把「判不了」当成「不是 Ⅳ 型」而选错条目子集。
    """
    if v is True:
        return "符合"
    if v is False:
        return "不符合"
    return "不可评价"


def g08_r09_multi_label_conflict(x: _Ctx) -> Verdict:
    """D1/D2/D3 互斥；同时出现 ≥2 个即冲突（D3 危险优先）。

    两个来源都要看：① 本轮规则算出的 ``x.labels``；② 抽取层预填的
    ``cl.diag_multi``（外部给的标签同样可能自相矛盾，不能只信自己算的）。
    """
    seen = set(x.labels) | {str(v) for v in (x.cl.diag_multi or [])}
    return sum(1 for lab in ("D1", "D2", "D3") if lab in seen) >= 2


def g08_r10_inflammatory_as_d5_subtype(x: _Ctx) -> Verdict:
    return _eq(x.inflammatory_ctx, "临床确认", "影像高度提示", "可疑")


def g08_r11_genetic_as_modifier(x: _Ctx) -> Verdict:
    return _eq(x.genetic_ctx, "确诊", "疑似遗传背景")


# ===========================================================================
# G09 · 分类收尾（4 条）
# ===========================================================================
def g09_r01_new_emergency_reflow(x: _Ctx) -> Verdict:
    return _eq(x.k.new_urgent, "是")


def g09_r02_identity_conflict(x: _Ctx) -> Verdict:
    return _or(_eq(x.q.idmatch, "不一致"), _has(x.q.conflict, "身份"))


def g09_r03_diagnosis_conflict(x: _Ctx) -> Verdict:
    if x.diag_conflict is None:
        return None
    return x.diag_conflict


def g09_r04_classify_and_stop(x: _Ctx) -> Verdict:
    """正常出口：无新急症 + 冲突已排除 + 至少一个可信标签。

    ``diag_conflict`` 为 ``None``（冲突还没判定）时必须 UNKNOWN ——
    「不知道有没有冲突」不等于「没有冲突」。
    """
    if x.diag_conflict is None:
        return None
    if not x.labels:
        return False
    return not x.diag_conflict


# ===========================================================================
# 规则登记表（54 条，唯一真源）
# ===========================================================================
@dataclass(frozen=True)
class RuleSpec:
    func: Callable[[_Ctx], Verdict]
    source: str
    exit: NextNode
    name: str
    fields: tuple[str, ...] = ()
    level: EmergencyLevel = "明确危险"


LABEL_BY_RULE: dict[str, str] = {
    "G08.R01": "D1",
    "G08.R02": "D2",
    "G08.R03": "D3",
    "G08.R04": "D4",
    "G08.R05": "D5",
    "G08.R06": "D6",
    "G08.R07": "D7",
    "G08.R08": "Ⅳ型TAAA扩展",
    "G08.R10": "D5",
}

RULES: dict[str, RuleSpec] = {
    # ---- G03（21）----
    "G03.R01": RuleSpec(
        g03_r01_direct_extravasation,
        "G03!9",
        "G05",
        "明确主动脉壁外造影剂外溢",
        ("c.extrav", "c.extrav_contig", "q.cta_enhance", "c.source_kind"),
    ),
    "G03.R02": RuleSpec(
        g03_r02_aneurysm_with_hematoma,
        "G03!10",
        "G05",
        "主动脉瘤伴相邻血肿",
        ("c.hematoma", "c.hematoma_adj", "c.aorta"),
    ),
    "G03.R03": RuleSpec(
        g03_r03_rupture_reported,
        "G03!11",
        "G05",
        "医师报告明确主动脉破裂",
        ("c.rupture_report", "q.idmatch"),
    ),
    "G03.R04": RuleSpec(
        g03_r04_suspected_rupture_unstable,
        "G03!12",
        "G05",
        "疑似破裂伴血流动力学不稳定",
        ("d.dx_context", "v.sbp", "v.conscious_change", "s.pain_current"),
        "高度疑似",
    ),
    "G03.R05": RuleSpec(
        g03_r05_aaa_acute_pain_attributed,
        "G03!13",
        "G05",
        "AAA 合并急性相关疼痛",
        ("s.pain_current", "s.pain_site", "s.pain_attrib"),
    ),
    "G03.R06": RuleSpec(
        g03_r06_aaa_sudden_pain_unattributed,
        "G03!14",
        "G05",
        "已知 AAA + 突发疼痛（归因未定）",
        ("s.pain_current", "s.pain_onset", "s.pain_attrib"),
        "高度疑似",
    ),
    "G03.R07": RuleSpec(
        g03_r07_rupture_not_excluded,
        "G03!15",
        "G05",
        "CTA 可疑破裂征象",
        ("c.rupture_report", "c.extrav", "c.hematoma", "q.report_vs_algo"),
        "不能排除",
    ),
    "G03.R08": RuleSpec(
        g03_r08_graft_with_gi_bleed,
        "G03!16",
        "G05",
        "主动脉移植物史合并消化道出血",
        ("d.surg", "s.gi_bleed"),
    ),
    "G03.R09": RuleSpec(
        g03_r09_acute_limb_ischemia,
        "G03!17",
        "G05",
        "急性肢体缺血危险征象",
        ("s.limb", "s.pulse_deficit", "c.branch"),
    ),
    "G03.R10": RuleSpec(
        g03_r10_news2_critical,
        "G03!18",
        "G05",
        "NEWS2 严重恶化（≥7）",
        ("v.rr", "v.sbp", "v.hr", "v.spo2", "v.acvpu", "v.temp", "v.oxygen"),
    ),
    "G03.R11": RuleSpec(
        g03_r11_news2_urgent_review,
        "G03!19",
        "人工复核",
        "NEWS2 需紧急复核（5–6 或单项 3）",
        ("v.rr", "v.sbp", "v.hr", "v.spo2", "v.temp"),
    ),
    "G03.R12": RuleSpec(
        g03_r12_no_enhanced_cta_with_clues,
        "G03!20",
        "G05",
        "无可靠增强 CTA 但有急症线索",
        ("q.cta_enhance", "q.cta_phase", "d.dx_context"),
        "不能排除",
    ),
    "G03.R13": RuleSpec(
        g03_r13_key_fact_conflict,
        "G03!21",
        "资料核实",
        "关键事实冲突",
        ("q.idmatch", "q.conflict", "q.report_vs_algo"),
    ),
    "G03.R14": RuleSpec(
        g03_r14_screen_incomplete,
        "G03!22",
        "资料核实",
        "最低筛查项未满足",
        ("q.current_assess", "s.pain_present", "v.sbp", "v.hr", "v.acvpu"),
    ),
    "G03.R15": RuleSpec(
        g03_r15_no_emergency_release,
        "G03!23",
        "G06",
        "明确无急症触发后放行",
        ("q.idmatch", "q.current_assess", "s.pain_present", "s.syncope", "v.sbp"),
    ),
    "G03.R16": RuleSpec(
        g03_r16_suspected_rupture_reported,
        "G03!24",
        "G05",
        "医生明确怀疑破裂（不要求 SBP<80）",
        ("q.idmatch", "d.dx_context", "c.rupture_report"),
        "高度疑似",
    ),
    "G03.R17": RuleSpec(
        g03_r17_hb_drop_comparable,
        "G03!25",
        "人工复核",
        "可比 Hb 在窗口内明显下降",
        ("lab.hb", "lab.hb_prev", "lab.hb_comparable", "lab.hb_confound"),
    ),
    "G03.R18": RuleSpec(
        g03_r18_severe_sudden_pain_no_aaa,
        "G03!26",
        "人工复核",
        "无已知 AAA 但突发剧烈腹背痛",
        ("s.pain_current", "s.pain_site", "s.pain_onset", "s.pain_severity"),
    ),
    "G03.R19": RuleSpec(
        g03_r19_hb_drop_uncertain,
        "G03!27",
        "人工复核",
        "Hb 差异明显但可比性/干扰未明",
        ("lab.hb", "lab.hb_prev", "lab.hb_comparable", "lab.hb_confound"),
    ),
    "G03.R20": RuleSpec(
        g03_r20_severe_hypotension,
        "G03!28",
        "G05",
        "严重低血压（不依赖已知 AAA）",
        ("v.sbp", "q.idmatch"),
    ),
    "G03.R21": RuleSpec(
        g03_r21_lactate_above_uln,
        "G03!29",
        "人工复核",
        "乳酸高于同次检验上限",
        ("lab.lact", "lab.lact_uln", "q.unit"),
    ),
    # ---- G04（18）----
    "G04.R01": RuleSpec(
        g04_r01_short_neck,
        "G04!40",
        "G06",
        "短颈型肾下 AAA",
        ("n.neck_len", "n.renal_l", "n.renal_r"),
    ),
    "G04.R02": RuleSpec(
        g04_r02_juxtarenal,
        "G04!41",
        "G06",
        "近肾型 AAA",
        ("n.neck_len", "n.renal_l", "n.renal_r"),
    ),
    "G04.R03": RuleSpec(
        g04_r03_pararenal, "G04!42", "G06", "肾旁型 AAA", ("n.renal_l", "n.renal_r", "n.sma")
    ),
    "G04.R04": RuleSpec(
        g04_r04_visceral_pararenal,
        "G04!43",
        "G06",
        "内脏旁型 AAA",
        ("n.renal_l", "n.renal_r", "n.sma", "n.ca"),
    ),
    "G04.R05": RuleSpec(
        g04_r05_taaa_iv_extension,
        "G04!44",
        "G06",
        "Ⅳ型 TAAA 扩展",
        ("n.renal_l", "n.sma", "n.ca", "n.thoracic"),
    ),
    "G04.R06": RuleSpec(
        g04_r06_beyond_project_boundary, "G04!45", "G06", "超过Ⅳ型 TAAA 项目边界", ("n.thoracic",)
    ),
    "G04.R07": RuleSpec(
        g04_r07_infection_or_fistula,
        "G04!46",
        "G06",
        "感染或主动脉相关瘘",
        ("n.infection_ctx", "n.fistula_ctx"),
    ),
    "G04.R08": RuleSpec(
        g04_r08_ifu_mismatch, "G04!47", "G06", "已核验的器械近端 IFU 不适配", ("n.seal_ifu",)
    ),
    "G04.R09": RuleSpec(
        g04_r09_iliac_limitation,
        "G04!48",
        "G06",
        "内髂灌注及双侧髂动脉限制",
        ("n.iliac_bilat", "n.iia_left", "n.iia_right"),
    ),
    "G04.R10": RuleSpec(
        g04_r10_access_occlusion, "G04!49", "G06", "常规股动脉通路受限", ("n.access_occlusion",)
    ),
    "G04.R11": RuleSpec(
        g04_r11_prior_repair_reintervention,
        "G04!50",
        "G06",
        "重要 EVAR/OSR 术后再干预线索",
        ("n.prev_repair", "n.endoleak_type", "n.sac_growth"),
    ),
    "G04.R12": RuleSpec(
        g04_r12_physiologic_risk,
        "G04!51",
        "G06",
        "重要生理风险需综合评估",
        ("n.egfr", "n.dialysis", "n.heart_class", "n.resp_support"),
    ),
    "G04.R13": RuleSpec(
        g04_r13_insufficient_for_low_complexity,
        "G04!52",
        "资料核实",
        "资料不足不允许判为低复杂",
        ("n.cta_status", "n.neck_len", "n.renal_l", "n.sma", "n.ca"),
    ),
    "G04.R14": RuleSpec(
        g04_r14_low_complexity_to_guideline,
        "G04!53",
        "G07",
        "低复杂进入指南模式",
        ("n.cta_status",),
    ),
    "G04.R15": RuleSpec(
        g04_r15_genetic_confirmed,
        "G04!54",
        "G06",
        "已明确遗传综合征或遗传性主动脉病",
        ("n.genetic_ctx",),
    ),
    "G04.R16": RuleSpec(
        g04_r16_inflammatory,
        "G04!55",
        "G06",
        "炎症性 AAA 明确或影像高度提示",
        ("n.inflammatory_ctx", "inf.mantle_sign", "inf.wall_fibrosis"),
    ),
    "G04.R17": RuleSpec(
        g04_r17_genetic_suspected,
        "G04!56",
        "G06",
        "存在遗传评估线索但尚未确诊",
        ("gen.aaa_age_onset", "gen.family_aneurysm", "gen.syndromic_features"),
    ),
    "G04.R18": RuleSpec(
        g04_r18_inflammation_vs_infection,
        "G04!57",
        "G06",
        "炎症与感染的鉴别",
        ("n.inflammatory_ctx", "n.infection_ctx"),
    ),
    # ---- G08（11）----
    "G08.R01": RuleSpec(
        g08_r01_d1_asymptomatic,
        "G08!21",
        "G06",
        "D1 无症状 AAA 入口",
        ("cl.aaa_reported", "cl.symptom_aaa", "cl.aaa_rupture"),
    ),
    "G08.R02": RuleSpec(
        g08_r02_d2_symptomatic_unruptured,
        "G08!22",
        "G06",
        "D2 症状性未破裂 AAA 入口",
        ("cl.aaa_reported", "cl.symptom_aaa", "cl.aaa_rupture"),
    ),
    "G08.R03": RuleSpec(
        g08_r03_d3_ruptured,
        "G08!23",
        "G06",
        "D3 破裂/疑似破裂 AAA 入口",
        ("cl.aaa_rupture", "cl.aaa_reported"),
    ),
    "G08.R04": RuleSpec(
        g08_r04_d4_infected,
        "G08!24",
        "G06",
        "D4 感染性主动脉病变入口",
        ("cl.infection", "cl.fistula"),
    ),
    "G08.R05": RuleSpec(
        g08_r05_d5_special,
        "G08!25",
        "G06",
        "D5 特殊主动脉病变入口",
        ("cl.pau", "cl.imh", "cl.iaad", "cl.saccular", "cl.pseudo"),
    ),
    "G08.R06": RuleSpec(
        g08_r06_d6_postop,
        "G08!26",
        "G06",
        "D6 EVAR/OSR 术后入口",
        ("cl.prev_evor", "cl.post_reason"),
    ),
    "G08.R07": RuleSpec(
        g08_r07_d7_iliac, "G08!27", "G06", "D7 髂动脉瘤入口", ("cl.iliac_site", "cl.iliac_diam")
    ),
    "G08.R08": RuleSpec(
        g08_r08_taaa_iv_label,
        "G08!28",
        "G06",
        "Ⅳ型 TAAA 扩展标签",
        ("n.thoracic", "n.sma", "n.ca"),
    ),
    "G08.R09": RuleSpec(
        g08_r09_multi_label_conflict, "G08!29", "人工复核", "多标签与冲突处理", ("cl.diag_multi",)
    ),
    "G08.R10": RuleSpec(
        g08_r10_inflammatory_as_d5_subtype,
        "G08!30",
        "G06",
        "炎症性 AAA 归入 D5 特殊病种",
        ("n.inflammatory_ctx", "inf.mantle_sign"),
    ),
    "G08.R11": RuleSpec(
        g08_r11_genetic_as_modifier,
        "G08!31",
        "G06",
        "遗传综合征保留为病因修饰标签",
        ("n.genetic_ctx", "gen.syndrome_type"),
    ),
    # ---- G09（4）----
    "G09.R01": RuleSpec(
        g09_r01_new_emergency_reflow, "G09!7", "G05", "发现新急症立即回流", ("k.new_urgent",)
    ),
    "G09.R02": RuleSpec(
        g09_r02_identity_conflict,
        "G09!8",
        "资料核实",
        "身份或关键数据冲突",
        ("q.idmatch", "q.conflict"),
    ),
    "G09.R03": RuleSpec(
        g09_r03_diagnosis_conflict,
        "G09!9",
        "人工复核",
        "病种判断相互矛盾",
        ("cl.diag_multi", "k.diag_conflict"),
    ),
    "G09.R04": RuleSpec(
        g09_r04_classify_and_stop,
        "G09!10",
        "G06",
        "输出疾病分类并停止",
        ("k.classifiable", "k.class_output"),
    ),
}

assert len(RULES) == 54, f"V3.1(2) 有 54 条判定规则，实际 {len(RULES)}"

# G03 优先级桶（P01–P04 是分组，不是规则，不进 RULES）
G03_P01 = (
    "G03.R01",
    "G03.R02",
    "G03.R03",
    "G03.R04",
    "G03.R05",
    "G03.R06",
    "G03.R07",
    "G03.R08",
    "G03.R09",
    "G03.R10",
    "G03.R12",
    "G03.R16",
    "G03.R20",
)
G03_P02 = ("G03.R11", "G03.R17", "G03.R18", "G03.R19", "G03.R21")
G03_P03 = ("G03.R13", "G03.R14")
G03_P04 = ("G03.R15",)
G04_BLOCKING = (
    "G04.R01",
    "G04.R02",
    "G04.R03",
    "G04.R04",
    "G04.R05",
    "G04.R06",
    "G04.R07",
    "G04.R08",
    "G04.R09",
    "G04.R10",
    "G04.R11",
    "G04.R12",
)
"""阻断型：命中即判复杂；其 UNKNOWN 也不是放行理由（交给 R13 兜）。"""
G04_EXTRA = ("G04.R15", "G04.R16", "G04.R17", "G04.R18")
"""加分型：遗传/炎症线索。命中→复杂；其 UNKNOWN **不阻断** G07 放行。"""
G08_RULES = (
    "G08.R01",
    "G08.R02",
    "G08.R03",
    "G08.R04",
    "G08.R05",
    "G08.R06",
    "G08.R07",
    "G08.R08",
)


# ===========================================================================
# 编排
# ===========================================================================
def _read_path(case: VascCase, path: str) -> object:
    obj: object = case
    for part in path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def _collect_gaps(x: _Ctx, rule_ids: tuple[str, ...], trace: list[RuleTrace]) -> list[str]:
    """把**导致 UNKNOWN 的**空字段列成缺口清单（去重、保序）。

    只统计求值结果为 ``UNKNOWN`` 的规则。原因：``True``（命中）与 ``False``
    （确定不成立）都意味着该规则已闭环，此时它声明的其余字段即使为空也**不是缺口**
    —— 例如无既往修复时 ``CL.POST_REASON`` 本就该空，若照收会把 ``confidence_level``
    从「高」拉低到「中」，让与本次判定无关的字段污染置信度分级。
    没跑过的规则（前面已命中而提前返回）同理不收。
    """
    verdicts = {t.rule_id: t.verdict for t in trace}
    gaps: list[str] = []
    for rid in rule_ids:
        if verdicts.get(rid, False) is not None:
            continue
        for path in RULES[rid].fields:
            if path in gaps:
                continue
            val = _read_path(x.case, path)
            if val is None or _is_uncertain_token(_token(val)) or val == []:
                gaps.append(path)
    return gaps + [g for g in x.gaps_extra if g not in gaps]


def _confidence(gaps: list[str], unknown_decisive: bool) -> ConfidenceLevel:
    if unknown_decisive:
        return "低"
    return "中" if gaps else "高"


def _run(x: _Ctx, rule_ids: tuple[str, ...], trace: list[RuleTrace]) -> tuple[str | None, bool]:
    """按顺序求值；返回 ``(首个命中的 rule_id, 期间是否出现 UNKNOWN)``。"""
    has_unknown = False
    for rid in rule_ids:
        v = RULES[rid].func(x)
        trace.append(RuleTrace(rid, v, RULES[rid].exit))
        if v is True:
            return rid, has_unknown
        if v is None:
            has_unknown = True
    return None, has_unknown


def _classifiable(x: _Ctx) -> str:
    if not x.labels:
        return "不能分类"
    return "疑似" if x.diag_conflict else "确定"


def _make(
    x: _Ctx,
    *,
    kind: RouteKind,
    rule_id: str,
    reason: str,
    next_node: NextNode,
    level: EmergencyLevel,
    mode: RouteMode | None = None,
    gaps: list[str] | None = None,
    unknown_decisive: bool = False,
    trace: list[RuleTrace] | None = None,
) -> RouteResult:
    gap_list = gaps or []
    return RouteResult(
        kind=kind,
        rule_id=rule_id,
        reason=reason,
        next_node=next_node,
        emergency_level=level,
        mode=mode,
        evidence_gaps=gap_list,
        confidence_level=_confidence(gap_list, unknown_decisive),
        class_output=list(x.labels),
        cl_taaa_iv=x.taaa_iv,
        k_identity_conflict="是" if _token(x.q.idmatch) == "不一致" else None,
        k_diag_conflict=None if x.diag_conflict is None else ("是" if x.diag_conflict else "否"),
        k_classifiable=_classifiable(x),
        trace=trace or [],
    )


def route(case: VascCase, measurement: VascMeasurement | None = None) -> RouteResult:
    """术前路由主入口：G03 → G04 →（G05 | G06 | G07）→ G08 → G09。

    参数：
        case: V3.1(2) 病例（160 字段）。
        measurement: 工具 B 的影像量化值；为 ``None`` 时按 B6 回落读 ``CL.AAA_DIAM``，
            并在 ``evidence_gaps`` 里登记 ``数据源=报告值``（不与量化值混淆）。

    返回：
        :class:`RouteResult`：出口 / 命中规则 / 缺口清单 / 置信度分级 / 判定轨迹。
    """
    x = _Ctx(case, measurement)
    trace: list[RuleTrace] = []

    if measurement is None and case.cl.aaa_diam is None and case.c.maxd is not None:
        x.gaps_extra.append("cl.aaa_diam（缺量化值，已回落报告值 c.maxd，修复阈值不可用）")

    # ---- 人工越级：urgency_flag 置位直接进 G05 ----
    if case.urgency_flag:
        return _make(
            x,
            kind=RouteKind.MODE,
            rule_id=MANUAL_URGENCY,
            reason=f"{MANUAL_URGENCY} 医生人工置位 urgency_flag：直接按急症处理，跳过自动判定。",
            next_node="G05",
            level="明确危险",
            mode=RouteMode.EMERGENCY,
            gaps=_collect_gaps(x, G03_P01, trace),
            trace=trace,
        )

    # ---- G03：急诊 ----
    hit, _ = _run(x, G03_P01, trace)
    if hit:
        return _make(
            x,
            kind=RouteKind.MODE,
            rule_id=hit,
            reason=f"{hit} {RULES[hit].name}：命中急诊条件，进入急诊模式（G05）。",
            next_node="G05",
            level=RULES[hit].level,
            mode=RouteMode.EMERGENCY,
            gaps=_collect_gaps(x, G03_P01, trace),
            trace=trace,
        )

    hit, unknown = _run(x, G03_P02, trace)
    if hit:
        return _make(
            x,
            kind=RouteKind.REVIEW,
            rule_id=hit,
            reason=f"{hit} {RULES[hit].name}：需人工临床复核，不能当作无急症。",
            next_node="人工复核",
            level="未触发",
            gaps=_collect_gaps(x, G03_P02, trace),
            unknown_decisive=unknown,
            trace=trace,
        )

    hit, _ = _run(x, G03_P03, trace)
    if hit:
        return _make(
            x,
            kind=RouteKind.VERIFY,
            rule_id=hit,
            reason=f"{hit} {RULES[hit].name}：资料冲突或筛查不完整，先核实再判定。",
            next_node="资料核实",
            level="未触发",
            gaps=_collect_gaps(x, G03_P03, trace),
            trace=trace,
        )

    release = RULES["G03.R15"].func(x)
    trace.append(RuleTrace("G03.R15", release, "G06"))
    if release is not True:
        return _make(
            x,
            kind=RouteKind.VERIFY,
            rule_id=FALLBACK_VERIFY,
            reason=(
                f"{FALLBACK_VERIFY} 急诊 P01–P03 均未命中，但存在未判定项（UNKNOWN）："
                "资料不足不下结论，转资料核实。"
            ),
            next_node="资料核实",
            level="未触发",
            gaps=_collect_gaps(x, G03_P01 + G03_P02 + G03_P03, trace),
            unknown_decisive=True,
            trace=trace,
        )

    # ---- G04：复杂度 ----
    for rid in G04_BLOCKING:
        v = RULES[rid].func(x)
        x.g04_blocking.append(v)
        trace.append(RuleTrace(rid, v, "G06"))
        if v is True:
            return _finish_mode(x, "G06", RouteMode.COMPLEX, rid, trace)

    # 遗传/炎症线索：命中即复杂；UNKNOWN 不算命中、也不阻断放行（规格明文）
    for rid in G04_EXTRA:
        v = RULES[rid].func(x)
        trace.append(RuleTrace(rid, v, "G06"))
        if v is True:
            return _finish_mode(x, "G06", RouteMode.COMPLEX, rid, trace)

    v13 = RULES["G04.R13"].func(x)
    trace.append(RuleTrace("G04.R13", v13, "资料核实"))
    if v13 is True:
        return _make(
            x,
            kind=RouteKind.VERIFY,
            rule_id="G04.R13",
            reason=f"G04.R13 {RULES['G04.R13'].name}：转资料核实。",
            next_node="资料核实",
            level="未触发",
            gaps=_collect_gaps(x, (*G04_BLOCKING, *G04_EXTRA, "G04.R13"), trace),
            trace=trace,
        )

    v14 = RULES["G04.R14"].func(x)
    trace.append(RuleTrace("G04.R14", v14, "G07"))
    if v14 is True:
        return _finish_mode(x, "G07", RouteMode.GUIDELINE, "G04.R14", trace)

    return _make(
        x,
        kind=RouteKind.VERIFY,
        rule_id=FALLBACK_VERIFY,
        reason=(
            f"{FALLBACK_VERIFY} 复杂（G04.R01–R12）与低复杂（G04.R14）均无法确认，"
            "资料不足不下结论，转资料核实。"
        ),
        next_node="资料核实",
        level="未触发",
        gaps=_collect_gaps(x, (*G04_BLOCKING, *G04_EXTRA, "G04.R13", "G04.R14"), trace),
        unknown_decisive=True,
        trace=trace,
    )


def _finish_mode(
    x: _Ctx, next_node: NextNode, mode: RouteMode, rule_id: str, trace: list[RuleTrace]
) -> RouteResult:
    """进入模式后继续跑 G08/G09：产出病种标签、处理冲突与回流。"""
    for rid in G08_RULES:
        v = RULES[rid].func(x)
        trace.append(RuleTrace(rid, v, "G06"))
        if rid == "G08.R08":
            # CL.TAAA_IV 是判定产物：算法在这里一次性固化，随 RouteResult 下去，
            # 不让下游工具自己重算（那会变成第二处判据）。
            x.taaa_iv = _taaa_grade(v)
        if v is True and LABEL_BY_RULE[rid] not in x.labels:
            x.labels.append(LABEL_BY_RULE[rid])

    if RULES["G08.R10"].func(x) is True and "D5" not in x.labels:
        x.labels.append("D5")
        trace.append(RuleTrace("G08.R10", True, "G06"))
    if RULES["G08.R11"].func(x) is True:
        trace.append(RuleTrace("G08.R11", True, "G06"))

    conflict = RULES["G08.R09"].func(x)
    x.diag_conflict = bool(conflict)
    trace.append(RuleTrace("G08.R09", conflict, "人工复核"))

    # G09.R01 回流优先于模式（优先级 G03 回流 > G06）
    if RULES["G09.R01"].func(x) is True:
        trace.append(RuleTrace("G09.R01", True, "G05"))
        return _make(
            x,
            kind=RouteKind.MODE,
            rule_id="G09.R01",
            reason="G09.R01 分类期间出现新的危险证据：立即回流 G03/G05，不得因分类进行中而延迟。",
            next_node="G05",
            level="明确危险",
            mode=RouteMode.EMERGENCY,
            gaps=_collect_gaps(x, ("G09.R01",), trace),
            trace=trace,
        )

    # G09.R02：身份/来源冲突 → 隔离不可信记录，转资料核实（排在 R03 之前）
    if RULES["G09.R02"].func(x) is True:
        trace.append(RuleTrace("G09.R02", True, "资料核实"))
        return _make(
            x,
            kind=RouteKind.VERIFY,
            rule_id="G09.R02",
            reason="G09.R02 身份或来源冲突：隔离不可信记录，先核实再分类（保留其他已确认警报）。",
            next_node="资料核实",
            level="未触发",
            gaps=_collect_gaps(x, ("G09.R02",), trace),
            unknown_decisive=True,
            trace=trace,
        )

    # G09.R04 是正常出口：无新急症 + 无冲突 + 至少一个可信标签
    trace.append(RuleTrace("G09.R04", RULES["G09.R04"].func(x), "G06"))

    if x.diag_conflict:
        trace.append(RuleTrace("G09.R03", True, "人工复核"))
        return _make(
            x,
            kind=RouteKind.REVIEW,
            rule_id="G09.R03",
            reason=f"G09.R03 病种标签冲突（{'/'.join(x.labels)}）：不得自动择一，需人工复核。",
            next_node="人工复核",
            level="未触发",
            gaps=_collect_gaps(x, ("G08.R09", "G09.R03"), trace),
            trace=trace,
        )

    return _make(
        x,
        kind=RouteKind.MODE,
        rule_id=rule_id,
        reason=f"{rule_id} {RULES[rule_id].name}：进入{mode.value}；病种标签 {x.labels or '未形成'}。",
        next_node=next_node,
        level="未触发",
        mode=mode,
        gaps=_collect_gaps(x, (*G08_RULES, "G04.R14"), trace),
        trace=trace,
    )


__all__ = [
    "FALLBACK_VERIFY",
    "G03_P01",
    "G03_P02",
    "G03_P03",
    "G03_P04",
    "G04_BLOCKING",
    "G04_EXTRA",
    "G08_RULES",
    "LABEL_BY_RULE",
    "MANUAL_URGENCY",
    "OUTCOME_PRIORITY",
    "RULES",
    "RouteKind",
    "RouteResult",
    "RuleSpec",
    "RuleTrace",
    "TaaaGrade",
    "route",
]
