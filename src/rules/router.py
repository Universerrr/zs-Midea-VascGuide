"""术前路由判定流水线 —— V3.6.2 的 **66 条判定规则**。

对照表见 ``src/rules/RULESET.md``（rule_id ↔ 函数 ↔ 源行）。本模块是规格的**第二真源**：
``RULES`` 字典里的 ``source`` 指向 V3.6.2 的具体单元格，改规则必须同步改表。

66 = 规格 65 条（G03 21 / G04 20 / G08 15 / G09 9）+ 工程补齐 1 条（``G04.R21``）。

三条语义（不可妥协）
--------------------
1. **三值求值**：``True`` / ``False`` / ``None``(=UNKNOWN)。Kleene 合取：任一 ``False`` → ``False``；
   否则任一 ``None`` → ``None``；全 ``True`` → ``True``。**UNKNOWN 不短路、不当 False**。
2. **不确定值**（可疑/疑似/未评价/不可评价/未知…）先折成 ``UNKNOWN``，永不参与「成立/不成立」。
3. **未提及 ≠ 阴性**：缺字段只能是 ``UNKNOWN``，不得当 ``False`` 放行。

出口（六值，与 ``RouteKind`` 对应）
----------------------------------
``G05`` 急诊 / ``G06`` 复杂 / ``G07`` 指南 → ``MODE``；
``人工复核`` → ``REVIEW``；``资料核实`` → ``VERIFY``；
``无目标病变`` → ``NO_TARGET``（V3.6.2 新增的终末出口：**查清了没有病** ≠ **查不清**）。
优先级：**G03 回流 > G06 > VERIFY > NO_TARGET**；``G07`` 是排他分支，**兜底是 VERIFY**。

范围
----
只实现 G03(21) / G04(21) / G08(15) / G09(9) = **66 条**（规格 65 + 工程补齐 1）。
G00–G02 的 9 条属「读取资料 / 证据质量 / 事实抽取」，归 LLM 抽取层，不进本模块。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from ..models.preop.case import (
    UNCERTAIN_TOKENS,
    GeneticDomain,
    InflammatoryDomain,
    VascCase,
    VitalDomain,
)
from ..models.preop.measurement import VascMeasurement
from ..models.preop.recommendation import RouteMode
from .version import RULESET_VERSION

# ===========================================================================
# 出口与结果
# ===========================================================================
Verdict = bool | None
"""``None`` 表示 UNKNOWN —— 不是「没命中」，也不是「阴性」。"""

NextNode = Literal["G05", "G06", "G07", "人工复核", "资料核实", "无目标病变"]
EmergencyLevel = Literal["明确危险", "高度疑似", "不能排除", "未触发", "冲突"]
ConfidenceLevel = Literal["高", "中", "低"]
TaaaGrade = Literal["符合", "疑似", "不符合", "不可评价"]
"""``CL.TAAA_IV``（Ⅳ型 TAAA 解剖扩展）的四值 —— V3.6.2 允许值原样。

它是**判定产物**，不进 ``VascCase``、不进 162 字段清单；
由 :func:`g08_r08_taaa_iv_label` 算出后随 :class:`RouteResult` 带给下游工具。
"""

SourceKind = Literal["ESVS原文", "工程安全规则", "其他指南"]
"""规则来源性质（V3.6 起）。

为什么要有这一层：66 条判定规则里有一部分并非 ESVS 2024 原文，而是工程为
安全自行拟定的分流规则（另有少数取自 ACC/AHA、NEWS2、KDIGO 等）。不标注
来源，输出层就无法告诉医生「这条依据是不是指南说的」—— 而这正是临床采纳
与论文溯源时最需要的一句话。
"""


class RouteKind(StrEnum):
    """路由结论的大类。与 ``NextNode`` 六值一一对应。"""

    MODE = "进入模式"
    REVIEW = "人工复核"
    VERIFY = "资料核实"
    NO_TARGET = "无目标病变"
    """V3.6.2 新增的终末出口：目标区域已充分评价且确实没有目标病变。

    为什么不能并进「人工复核」：复核的含义是「系统判不了，请人看」；这个出口
    的含义是「系统查清了，确实没有」——后者不需要人再复核一遍分类，只需要
    临床继续找其他病因。两者的责任归属与后续动作完全不同。
    """


FALLBACK_VERIFY = "FALLBACK.VERIFY"
"""兜底标识：**没有任何规则能判定**时的出口。

为什么不借某条规则的 id：兜底不是规则判的结论，而是「判不了」这件事本身。
借用 ``G03.R15`` / ``G04.R14`` 会让 ``RULESET.md`` 的出口列说谎
（它们的出口是 G06 / G07，不是资料核实）。
"""

MANUAL_URGENCY = "MANUAL.URGENCY"
"""人工越级标识：医生置位 ``urgency_flag``，跳过自动判定。"""

GUARD_D3_LABEL = "GUARD.D3_LABEL"
"""出口自洽性标识：**病种标签含 D3，但急诊路由没触发**。

它不是规格里的规则，也不是「判不了」的兜底，而是对**两个判定产物**的一致性检查：
``G08.R03`` 说「破裂/疑似破裂 → D3」，``G03`` 说「影像证据/报告/临床疑似 → G05」。
规格 ``G08!32`` 要求「D3 危险优先」、``G08.R03`` 要求「疑似时保持 G05 优先」——
两句话都以「出 D3 时急诊已经触发」为前提，但**没有任何一条规则把两者绑在一起**。

不借 ``G03`` / ``G08.R03`` 的 id：这不是它们判的结论，而是「它们两个不可能同时为真」
这件事本身。借了会让 ``RULESET.md`` 的出口列说谎。
"""

OUTCOME_PRIORITY: dict[RouteKind, int] = {
    RouteKind.REVIEW: 3,  # 人工复核压过一切模式
    RouteKind.MODE: 2,
    RouteKind.VERIFY: 1,
    RouteKind.NO_TARGET: 0,  # 最低：任何一条危险证据都不该被「没病」盖掉
}
"""冲突时取优先级高者。**VERIFY 最低**：能进模式就不因资料瑕疵退回核查。

``NO_TARGET`` 比 VERIFY 还低 —— 它是「查清了没有病」的结论，优先级必须垫底，
否则一个已被其它规则标出急症线索的病例会被「无目标病变」抢先终止。
"""


TraceExit = Literal["G05", "G06", "G07", "G08", "G09", "人工复核", "资料核实", "无目标病变"]
"""单条规则留痕能声明的去向 —— 比 :data:`NextNode` 多 ``G08``/``G09``。

为什么留痕比路由出口宽：``NextNode`` 是**路由结论**的取值域（这一例去哪），
而留痕记录的是**这条规则**在规格 M 列写的去向。``G04.R20`` 写的就是 G08
（分类阶段）、``G08.R14/R15`` 写的就是 G09（交分类收尾）—— 它们都不是路由
出口，但审计时必须如实记下「这条规则把人送到了哪一步」。
"""


@dataclass
class RuleTrace:
    """单条规则的求值留痕（审计用；不要塞进 case）。"""

    rule_id: str
    verdict: Verdict
    exit: TraceExit


@dataclass
class RouteResult:
    """路由结论 + 判定轨迹 + 证据缺口。"""

    kind: RouteKind
    rule_id: str
    reason: str
    next_node: NextNode
    emergency_level: EmergencyLevel
    next_nodes: list[str] = field(default_factory=list)
    """本次结论要走的**全部节点**（有序，恒以 ``next_node`` 打头）。V3.6 起启用。

    规格的「输出／下一节点」列天然是序列 —— ``D3.1.R01`` 写的是
    「G05 急诊模式**并**进入 D3.2」，两个动作都要做，不是二选一；
    ``D7.2.R91`` 甚至可以从病种流程反向跳回 ``G03``。单值 ``next_node``
    表达不了这种「同时/依次走多处」。

    为什么保留 ``next_node``：现有 66 条规则各走一个节点，它是**主出口**；
    全部改写成序列会让「本次到底去哪」这个主语丢失，输出层无从判断。
    约定：``len(next_nodes) == 1`` 时与旧语义完全等价。
    """
    mode: RouteMode | None = None
    evidence_gaps: list[str] = field(default_factory=list)
    confidence_level: ConfidenceLevel = "中"
    class_output: list[str] = field(default_factory=list)
    cl_taaa_iv: TaaaGrade | None = None
    """Ⅳ型 TAAA 解剖扩展（判定产物）。``None`` = 没跑到 G08（急诊出口）。

    为什么不放 ``VascCase``：它是规则算出来的，不是抽取出来的病例事实。
    放进去会让「病例输入」与「判定输出」混在一处，且 162 字段的契约数字会失守。
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


def _gi_bleed_status(x: _Ctx) -> Verdict:
    """本次是否有消化道出血（三值）—— ``S.GI_BLEED`` 是主判据，明细兜底。

    规格的「允许值」列写的就是状态字段：``G03.R08`` 是 ``D.SURG;S.GI_BLEED``，
    ``G03.R15`` 是 ``…;S.GI_BLEED;S.LIMB;…``；``S.GI_BLEED_TYPE`` 只有抽取层
    ``G02.R04`` 读。所以状态有值时以它为准，未填时才看明细。

    **两者矛盾时不替任何一方作主**：状态写「否」而明细记了呕血，返回 UNKNOWN
    ——「一份记录说没有、另一份记着呕血」不是能自动裁的事。
    """
    state = _token(x.s.gi_bleed)
    detail = _has(x.s.gi_bleed_type, *GI_BLEED_SIGNS)
    if state == "是":
        return True
    if state == "否":
        return None if detail is True else False
    if state is not None and _is_uncertain_token(state):
        return None
    return detail


def _limb_status(x: _Ctx) -> Verdict:
    """本次是否有急性肢体缺血表现（三值）—— 同 :func:`_gi_bleed_status` 的口径。

    ``S.LIMB`` 的五态里把 ``未评估`` 单列出来，正是为了防「没查 = 没有」。
    """
    state = _token(x.s.limb)
    detail = _has(x.s.limb_signs, *LIMB_ISCHEMIA_SIGNS)
    if state == "是":
        return True
    if state == "否":
        return None if detail is True else False
    if state is not None and _is_uncertain_token(state):
        return None
    return detail


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
        self.malperfusion, self.malperfusion_organs = _materialize_malperfusion(self)

        # ---- 运行时状态（编排层写入，非字段）----
        self.labels: list[str] = []
        self.diag_conflict: bool | None = None
        self.g04_blocking: list[Verdict] = []
        self.gaps_extra: list[str] = []
        self.taaa_iv: TaaaGrade | None = None
        # G08.R14 / R15 的候选标记：G09.R07 / R08 据此终审。
        # 两个都是「暂定」，所以只进气种层终审，不直接改出口。
        self.no_target_candidate: Verdict = False
        self.unclassifiable_candidate: Verdict = False
        # G09.R05 / R06 的命中记录（任一为真 → 不得「先把分类做完」）
        self.reflow_evidence: str | None = None


def _news2(v: VitalDomain) -> tuple[float | None, bool | None]:
    """NEWS2 总分与「是否有单项 3 分」。

    7 项**全部可算**才给总分；单项 3 分只要该项可算即可判。
    ``Scale2经确认`` 未实现 → 直接 UNKNOWN（用错量表比没分数更危险）。
    """
    if _token(v.spo2_scale) == "Scale2经确认":
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
        "rr": None if _num(v.rr) is None else s_rr(_num(v.rr)),
        "spo2": None if _num(v.spo2) is None else s_spo2(_num(v.spo2)),
        "sbp": None if _num(v.sbp) is None else s_sbp(_num(v.sbp)),
        "hr": None if _num(v.hr) is None else s_hr(_num(v.hr)),
        "temp": None if _num(v.temp) is None else s_temp(_num(v.temp)),
    }
    acvpu = _token(v.acvpu)
    comps["acvpu"] = (
        None if acvpu is None or _is_uncertain_token(acvpu) else (0 if acvpu == "A" else 3)
    )
    oxygen = _token(v.oxygen)
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


GENETIC_CONFIRMED_SYNDROMES: tuple[str, ...] = (
    "Marfan",
    "Loeys-Dietz",
    "血管型EDS",
    "其他确诊综合征",
)
"""``GEN.SYNDROME_TYPE`` 里表示「已确诊某种遗传综合征」的取值。"""


def _genetic_confirmed(gen: GeneticDomain) -> bool:
    """遗传证据是否已足以确诊（综合征名称或致病/可能致病变异）。"""
    return _token(gen.syndrome_type) in GENETIC_CONFIRMED_SYNDROMES or _token(
        gen.variant_class
    ) in ("致病", "可能致病")


def _genetic_suspected(gen: GeneticDomain) -> bool:
    """是否只有**遗传评估线索**、尚未确诊。

    四条线索任一成立即可：未确诊的综合征记录、家族史阳性、典型体征、早发
    （首次确诊 < 60 岁）。规格 ``G04.R17`` 的「符合以上任一项」就是这四条 ——
    它与 :func:`_derive_genetic_ctx` 共用本函数，避免同一事实两处判据。
    """
    age = gen.aaa_age_onset
    return (
        _token(gen.syndrome_type) == "疑似"
        or _token(gen.family_aneurysm) == "阳性"
        or bool(gen.syndromic_features)
        or (age is not None and age < 60)
    )


def _derive_genetic_ctx(gen: GeneticDomain) -> str | None:
    """遗传证据明细 → 主状态（三层：``GEN.*`` → ``N.GENETIC_CTX`` → 规则）。

    只在抽取层没给主状态时兜底派生；证据不足以支撑结论时返回 ``None``。
    """
    if _genetic_confirmed(gen):
        return "确诊"
    if _genetic_suspected(gen):
        return "疑似遗传背景"
    if (
        _token(gen.family_aneurysm) == "明确无相关史"
        and gen.aaa_age_onset is not None
        and gen.aaa_age_onset >= 60
        and not gen.syndromic_features
    ):
        return "无已知阳性线索"
    return None


def _derive_inflammatory_ctx(inf: InflammatoryDomain) -> str | None:
    """炎症证据明细 → 主状态。**单纯 CRP/ESR 升高不触发**（G04.R16 明文）。"""
    mantle = _token(inf.mantle_sign)
    wall = _token(inf.wall_fibrosis)
    if mantle == "明确存在":
        return "影像高度提示"
    if mantle == "可疑" or wall == "可疑":
        return "可疑"
    if mantle == "未见" and wall == "未见":
        return "无当前支持证据"
    return None


MPF_ORGANS = ("肾", "肠系膜", "下肢", "其他器官")
"""``C.MALPERFUSION`` 里代表受累器官的取值；其余（无/疑似/未知/证据冲突）是状态词。"""

MPF_UNCERTAIN = ("疑似", "未知", "证据冲突")
"""``C.MALPERFUSION`` 里「有值但值本身不确定」的取值 —— 只能产出 UNKNOWN。"""


def _materialize_malperfusion(x: _Ctx) -> tuple[Verdict, frozenset[str]]:
    """器官/肢体灌注障碍 → ``(三值, 受累器官集合)``。

    ``C.MALPERFUSION`` 在 V3.6.2 只有定义行（``G04!40``）、没有规则行，也没有
    独立的证据明细层（对照 ``GEN.*`` / ``INF.*`` 那种三层结构）。本函数补上
    这一层：抽取值优先，缺失时保守派生。

    **本函数永不因分支解剖异常推出 ``True``。** 规格 ``G04!J40`` 明令
    「不能仅凭解剖狭窄自动推断器官缺血」——``C.BRANCH`` 异常只能导致 ``None``
    （无法排除），不能导致确认。确认通路只有一条：医生在病历里明确记录了
    器官/肢体灌注障碍，由抽取层写入 ``c.malperfusion``。

    之所以不在派生里判 ``True``：确认器官缺血需要临床阈值（肌酐升高多少、
    乳酸超上限多少），V3.6.2 一个都没给；``G03.R21`` 还专门写明「单独乳酸升高
    不诊断」。在此硬编码阈值，等于把未经审核的判据塞进判定层。
    """
    raw = x.c.malperfusion
    if raw is not None:
        tokens = {_token(t) for t in raw}
        organs = frozenset(t for t in tokens if isinstance(t, str) and t in MPF_ORGANS)
        if tokens & set(MPF_UNCERTAIN):
            return None, organs
        return (True, organs) if organs else (False, frozenset())

    branches = x.c.branch
    if not branches:
        return None, frozenset()
    vals = {_token(v) for v in branches.values()}
    if vals & {"可疑", "不可评价"}:
        return None, frozenset()
    limb_clear = _limb_status(x) is False
    if all(v == "通畅" for v in vals) and limb_clear:
        return False, frozenset()
    return None, frozenset()


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


REPAIR_KINDS: tuple[str, ...] = ("EVAR", "OSR", "其他")
"""``CL.PREV_EVOR`` / ``N.PREV_REPAIR`` 里表示「做过修复」的取值。"""


def _has_prior_repair(x: _Ctx) -> Verdict:
    """有既往主动脉修复史。「无」是明确阴性；「未知」/未填是 UNKNOWN。

    两个来源任一为阳性即可：``CL.PREV_EVOR``（分类域）与 ``N.PREV_REPAIR``
    （解剖域）问的是同一件事，抽取层可能只填了其中一个。
    """
    return _or(
        _eq(x.cl.prev_evor, *REPAIR_KINDS),
        _eq(x.n.prev_repair, *REPAIR_KINDS),
    )


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
    """疑似破裂背景 + 血流动力学不稳定 → 最高紧急响应。

    **V3.6.2 收窄了本条**：它不再独立触发，只在「本次医生/影像报告明确疑似
    破裂」（即 ``G03.R16`` 的前提）成立时，用 ``SBP<80`` 或新发意识下降标记
    「严重不稳定」。规格原句：「本条不独立诊断破裂；若没有主动脉疑虑，严重
    低血压由 ``G03.R20`` 触发一般危重响应」。

    修前它还接受「AAA + 急性腹痛/晕厥」作为触发，于是「症状性未破裂 AAA +
    一过性低血压」也会被记成「疑似破裂伴不稳定」——把两种完全不同的临床情形
    （可能破了 vs 没破但疼）压成同一个 ``rule_id``，急诊团队读报告时无法区分。

    与 ``R16`` 的关系：两者前提相同，区别只在是否不稳定。谁先命中取决于
    ``G03_P01`` 的顺序（R04 在 R16 之前），所以「疑似破裂 + 低血压」报 R04、
    「疑似破裂 + 血压正常」报 R16 —— 两条互补，不会同时出现。
    """
    trigger = _and(
        _eq(x.q.idmatch, "一致"),
        _or(_eq(x.d.dx_context, "疑似"), _eq(x.c.rupture_report, "疑似破裂")),
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
    return _and(graft, _gi_bleed_status(x))


def g03_r09_acute_limb_ischemia(x: _Ctx) -> Verdict:
    severe = _or(
        _has(x.s.limb_signs, "运动障碍", "麻木"),
        _eq(x.s.pulse_deficit, "有", "缺失"),
        _branch_occluded(x),
    )
    return _and(_limb_status(x), severe)


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
        _not(_gi_bleed_status(x)),
        _not(_limb_status(x)),
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
# G04 · 复杂度判定（21 条）
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
    return _eq(x.n.seal_ifu, "不符合")


def g04_r09_iliac_limitation(x: _Ctx) -> Verdict:
    iia = _or(_eq(x.n.iia_left, "闭塞", "严重狭窄"), _eq(x.n.iia_right, "闭塞", "严重狭窄"))
    return _and(_eq(x.n.iliac_bilat, "双侧"), iia)


def g04_r10_access_occlusion(x: _Ctx) -> Verdict:
    return _eq(x.n.access_occlusion, "双侧闭塞")


def g04_r11_prior_repair_reintervention(x: _Ctx) -> Verdict:
    """既往修复 + 确认 I/III 型内漏、结构重大异常或瘤囊增长 ≥10mm → 复杂。

    「有修复史」统一走 :func:`_has_prior_repair`（``CL.PREV_EVOR`` 与
    ``N.PREV_REPAIR`` 任一为阳性即可）。只看后者会让「分类域写了 EVAR、
    解剖域没填」的病例漏判——而 ``R19``（术后无异常）用的正是前者，
    两条规则对同一事实必须同一判据。
    """
    evidence = _or(
        _eq(x.n.endoleak_type, "I", "III"),
        _eq(x.n.graft_major, "组件分离", "移位", "明显闭塞"),
        _ge(x.n.sac_growth, SAC_GROWTH_MM),
    )
    return _and(_has_prior_repair(x), evidence)


def g04_r12_physiologic_risk(x: _Ctx) -> Verdict:
    return _or(
        _lt(x.n.egfr, EGFR_LOW),
        _eq(x.n.dialysis, "是"),
        _eq(x.n.heart_class, "III", "IV"),
        _eq(x.n.resp_support, "长期吸氧", "无创通气", "有创通气"),
    )


def g04_r13_insufficient_for_low_complexity(x: _Ctx) -> Verdict:
    """关键解剖字段缺失/不可评价 → 不得判低复杂。

    ``_missing`` 已把「不可评价」这类不确定值算作缺失（``UNCERTAIN_TOKENS``），
    故四个分支字段各判一次即可，不再逐值重复列举。
    """
    anatomy = _or(
        _eq(x.n.cta_status, "缺失", "部分", "冲突"),
        _missing(x.n.neck_len),
        _missing(x.n.renal_l),
        _missing(x.n.renal_r),
        _missing(x.n.sma),
        _missing(x.n.ca),
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
    """有主动脉病变 + 只有遗传评估线索（未确诊）→ 复杂，做专科遗传评估。

    两条通路取并集：``GEN.*`` 明细支持「疑似」（见 :func:`_genetic_suspected`），
    或抽取层直接给了 ``N.GENETIC_CTX="疑似遗传背景"``。修复前只读后者中的一半
    ——明细里 ``GEN.SYNDROME_TYPE="疑似"`` 这一路永远不触发。
    """
    aortic = _or(_aaa_confirmed(x), _eq(x.d.dx_context, "疑似"))
    suspected = _or(_genetic_suspected(x.gen), _eq(x.genetic_ctx, "疑似遗传背景"))
    return _and(aortic, suspected)


def g04_r18_inflammation_vs_infection(x: _Ctx) -> Verdict:
    return _and(
        _eq(x.inflammatory_ctx, "可疑", "影像高度提示", "临床确认"),
        _eq(x.n.infection_ctx, "医师明确", "高度怀疑"),
    )


def g04_r21_malperfusion(x: _Ctx) -> Verdict:
    """器官/肢体灌注障碍 → 复杂。

    V3.6.2 只在 ``G04!40`` 定义了 ``C.MALPERFUSION``，没有对应的规则行。
    本规则是代码侧补齐的消费端，**来源性质登记为「工程安全规则」**——
    不得声称它是 ESVS 原文。

    取值来自 :func:`_materialize_malperfusion`：只有医生明确记录才可能为
    ``True``；分支解剖异常一律 ``None``，不构成复杂因素。
    """
    return x.malperfusion


# ---- G04.R19 / R20（V3.6.2 新增）----
def g04_r19_prior_repair_unremarkable(x: _Ctx) -> Verdict:
    """P3：仅有既往修复史、本次无危险征象或重要异常 —— 不因手术史判复杂。

    规格 ``G04.R19``：「仅有既往修复史且本次为常规随访、无危险征象或重要异常时，
    不单凭既往手术史判为复杂模式；按 G08 进入 D6 随访路由」。

    出口与 ``R14`` 相同（``G07``），本规则只是给出**更具体的依据**：``R14``
    回答「所有阻断项都清了」，``R19`` 回答「清了，而且这人是术后随访」。
    审计时前者看不出病人做过手术。

    「无异常」是**合取且要求明确阴性**：任一异常项缺失即返回 ``None``，
    不按「没记录＝没有」放行（``G04.R19`` 缺失列：「任一关键条件缺失/冲突→
    不得按阴性处理」）。
    """
    repaired = _has_prior_repair(x)
    if repaired is not True:
        return repaired
    abnormal = _or(
        _not(_eq(x.n.endoleak_type, "无")),
        _not(_eq(x.n.graft_major, "无")),
        _ge(x.n.sac_growth, SAC_GROWTH_MM),
        _eq(x.cl.infection, "原生感染明确", "原生感染疑似", "移植物感染明确", "移植物感染疑似"),
        _eq(x.cl.fistula, *FISTULA_LABELS),
    )
    return _not(abnormal)


def g04_r20_comorbidity_with_primary(x: _Ctx) -> Verdict:
    """P1：遗传/炎症/感染线索与主病种并存 —— 病因标签不取代主病种。

    出口是 ``G08``（多标签分类），**不进复杂模式**：它说的是「别把病因标签当成
    主病种的替代」，不是「这条比别的更危险」。所以求值后只记 trace 与标签，
    不改变路由出口。

    与 ``R15``/``R16`` 的分工：那两条回答「有遗传/炎症**所以要判复杂**」，
    本条回答「即使已经判了复杂/指南，病因与主病种**仍要各记一个标签**」。
    两者可以同时成立，不冲突。
    """
    primary = _aaa_confirmed(x)
    if primary is not True:
        return primary
    return _or(
        _eq(x.genetic_ctx, "确诊", "疑似遗传背景"),
        _eq(x.inflammatory_ctx, "临床确认", "影像高度提示", "可疑"),
        _eq(x.n.infection_ctx, "医师明确", "高度怀疑"),
    )


# ===========================================================================
# G08 · 病种分类（15 条）
# ===========================================================================
def _no_prior_repair(x: _Ctx) -> Verdict:
    """未做过主动脉修复 —— ``D1``/``D2`` 两个「原发瘤」入口的前置条件。

    已修复的动脉瘤不是「无症状 AAA」，是**术后状态**：它的随访内容是内漏与
    瘤囊变化（``D6.1``），不是按直径的复查间隔。不加这个前置，术后病人会同时
    拿到 ``D1`` 与 ``D6`` 两个标签，病种引擎从两个入口各走一遍，报告里出现
    两套互相矛盾的随访建议。

    ``G08.R13`` 说的「术后状态标签**不等于**并发症确诊」也支持这个方向：
    术后就是术后，有没有并发症由 D6 内部再分。
    """
    repaired = _has_prior_repair(x)
    if repaired is None:
        return None
    return not repaired


def g08_r01_d1_asymptomatic(x: _Ctx) -> Verdict:
    aaa = _or(_eq(x.cl.aaa_reported, "确认"), _ge(x.cl.aaa_diam, DIAMETER_AAA_MM))
    return _and(
        aaa,
        _eq(x.cl.symptom_aaa, "无当前症状", "明确归因其他"),
        _eq(x.cl.aaa_rupture, "未见相关证据"),
        _no_prior_repair(x),
    )


def g08_r02_d2_symptomatic_unruptured(x: _Ctx) -> Verdict:
    aaa = _or(_eq(x.cl.aaa_reported, "确认"), _ge(x.cl.aaa_diam, DIAMETER_AAA_MM))
    return _and(
        aaa,
        _eq(x.cl.symptom_aaa, "医师确认相关", "医师怀疑相关"),
        _not(_eq(x.cl.aaa_rupture, "明确")),
        _no_prior_repair(x),
    )


def g08_r03_d3_ruptured(x: _Ctx) -> Verdict:
    return _eq(x.cl.aaa_rupture, "明确", "疑似")


#: ``CL.INFECTION`` 里表示「有感染」的四个取值（原生/移植物 × 明确/疑似）。
#: 病种层 ``common.INFECTION_SUBTYPES`` 直接复用它，不再抄一份取值表。
INFECTION_LABELS: tuple[str, ...] = (
    "原生感染明确",
    "原生感染疑似",
    "移植物感染明确",
    "移植物感染疑似",
)

#: ``CL.FISTULA`` 里表示「有主动脉相关瘘」的取值。
#: ``未确定`` 是规格 ``CL.FISTULA_TYPE`` 的一态——类型待定，但瘘已成立。
FISTULA_LABELS: tuple[str, ...] = (
    "主动脉肠瘘",
    "主动脉腔静脉瘘",
    "其他腔脏器瘘",
    "未确定",
    "疑似",
)

#: ``S.GI_BLEED`` 里表示「有活动性出血」的取值（``无`` / ``未知`` 不在其中）。
#: 路由层与病种层共用这一份，避免两处各写一张表。
GI_BLEED_SIGNS: tuple[str, ...] = ("呕血", "黑便", "便血", "其他出血表现")

#: ``S.LIMB`` 里表示「有缺血表现」的取值（``无`` / ``未知`` 不在其中）。
#: ``脉搏减弱或消失`` 归 ``S.PULSE_DEFICIT``，不重复放在这里。
LIMB_ISCHEMIA_SIGNS: tuple[str, ...] = (
    "疼痛",
    "苍白",
    "发凉",
    "麻木",
    "无力",
    "运动障碍",
    "其他",
)


def _infection_or_fistula_label(x: _Ctx) -> Verdict:
    """分类域里的感染/瘘标签（``CL.INFECTION`` / ``CL.FISTULA``）—— D4 的唯一判据。

    ``G08.R04`` 用它打 D4 标签，``G09.R05`` 在它之上再加「主状态」那一层；
    两处若各抄一份取值域，「什么算感染」就会有两个版本。
    """
    return _or(_eq(x.cl.infection, *INFECTION_LABELS), _eq(x.cl.fistula, *FISTULA_LABELS))


def _cross_domain_gaps(x: _Ctx) -> list[str]:
    """同一事实在 CL 域与 N 域结论相反时，记一条**证据缺口**。

    规格有意把两域分开：``G04.R07`` 的「允许值」列只有 ``N.INFECTION_CTX`` /
    ``N.FISTULA_CTX``，``G08.R04`` 只有 ``CL.INFECTION`` / ``CL.FISTULA`` ——
    各自只看自己那一域。所以这里**不改任何判定**（改了就越出规格声明的输入），
    只把「抽取层把同一件事填成了两种结论」报出来，让置信度从「高」降「中」。

    典型来源：医生在病程里写「高度怀疑移植物感染」（N 域有），而分类结论那一栏
    留空或写了「无证据」（CL 域无）——判据各按自己的域走，报告里就会出现
    「复杂模式、但病种标签只有 D1」。缺口写出来，复核者才知道该补哪一栏。
    """
    gaps: list[str] = []
    pairs = (
        (
            _eq(x.cl.infection, *INFECTION_LABELS),
            _eq(x.n.infection_ctx, "医师明确", "高度怀疑"),
            "CL.INFECTION",
            "N.INFECTION_CTX",
            "感染",
        ),
        (
            _eq(x.cl.fistula, *FISTULA_LABELS),
            _eq(x.n.fistula_ctx, "明确", "疑似"),
            "CL.FISTULA",
            "N.FISTULA_CTX",
            "主动脉相关瘘",
        ),
    )
    for cl_v, n_v, cl_name, n_name, what in pairs:
        if cl_v is True and n_v is False:
            gaps.append(f"{cl_name} 有{what}证据，但 {n_name} 记为无：同一事实两域结论相反，需核对")
        elif cl_v is False and n_v is True:
            gaps.append(f"{n_name} 提示{what}，但 {cl_name} 未给证据：同一事实两域结论相反，需核对")
    return gaps


def g08_r04_d4_infected(x: _Ctx) -> Verdict:
    return _infection_or_fistula_label(x)


def g08_r05_d5_special(x: _Ctx) -> Verdict:
    return _or(
        _eq(x.cl.pau, "明确", "可疑"),
        _eq(x.cl.imh, "明确", "可疑"),
        _eq(x.cl.iaad, "明确", "可疑"),
        _eq(x.cl.saccular, "明确", "疑似"),
        _eq(x.cl.pseudo, "明确", "疑似"),
    )


def g08_r06_d6_postop(x: _Ctx) -> Verdict:
    """既往 EVAR/OSR → ``D6``（术后随访入口）。

    **修前还要求 ``CL.POST_REASON`` 有值**，于是「有 EVAR 史但没写本次就诊原因」
    的病例落到 ``D1``（原发无症状 AAA），拿到的是按直径的随访间隔 —— 而术后
    病人需要的是内漏与瘤囊监测，两者在随访内容与频次上完全不同。

    规格 ``G08.R06`` 的原句是「既往EVAR或OSR**且本次为术后随访或术后并发症
    相关评估**→D6；『既往支架』与『已确诊严重并发症』分开」。后半句是在提醒
    「有支架 ≠ 有并发症」，即 D6 是**随访入口**而不是并发症结论；前半句的
    「本次为术后随访」描述的是适用场景，不是「必须有个字段记录就诊原因」。
    本次具体是什么原因，由 ``D6.1`` 分诊时再问（它的 R01–R03 就是干这个的）。

    所以判据只留「有修复史」这一半，宁可把术后病人按术后管，也不要把他们
    当成原发 AAA 随访。
    """
    return _has_prior_repair(x)


def g08_r07_d7_iliac(x: _Ctx) -> Verdict:
    return _has(x.cl.iliac_site, "左侧", "右侧", "CIA", "IIA", "EIA")


def g08_r08_taaa_iv_label(x: _Ctx) -> Verdict:
    renal = _or(_eq(x.n.renal_l, "明确受累"), _eq(x.n.renal_r, "明确受累"))
    return _and(_eq(x.n.thoracic, "否"), _eq(x.n.sma, "明确受累"), _eq(x.n.ca, "明确受累"), renal)


def _taaa_grade(v: Verdict) -> TaaaGrade:
    """``G08.R08`` 的三值 → ``CL.TAAA_IV`` 的四值。

    「疑似」当前**不可达**，这是有意的：三值求值只能给 成立 / 不成立 / 未判定，
    而「疑似」要求「部分关键字段明确受累、部分可疑」这种细分判据 —— V3.6.2
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


def _exclusive_states(x: _Ctx) -> frozenset[str]:
    """同属「同一病灶同一时点」的三个状态标签 —— D1 无症状 / D2 症状性 / D3 破裂。

    两个来源合并：本轮规则算出的 ``x.labels``，以及抽取层预填的
    ``cl.diag_multi``（外部给的标签同样可能自相矛盾，不能只信自己算的）。
    其余标签（D4–D7、Ⅳ型TAAA 扩展）按 ``G09.R09`` 都是**允许共存**的。
    """
    return frozenset(set(x.labels) | set(x.cl.diag_multi or ())) & {"D1", "D2", "D3"}


def g08_r09_multi_label_conflict(x: _Ctx) -> Verdict:
    """D1/D2/D3 是同一动脉瘤的三种状态，同时出现 ≥2 个即冲突（D3 危险优先）。"""
    return len(_exclusive_states(x)) >= 2


def g08_r10_inflammatory_as_d5_subtype(x: _Ctx) -> Verdict:
    return _eq(x.inflammatory_ctx, "临床确认", "影像高度提示", "可疑")


def g08_r11_genetic_as_modifier(x: _Ctx) -> Verdict:
    return _eq(x.genetic_ctx, "确诊", "疑似遗传背景")


# ---- G08.R12 / R13（V3.6.2 新增）----
def _infection_site(x: _Ctx) -> str | None:
    """感染部位：``native`` / ``graft`` / ``None``（判不了）。

    单独成函数是因为「部位」不是三值 —— 规格 ``G08.R12`` 要在 D4.2 与 D4.3
    之间分流，二值表达不了。求值仍走 ``_token``，不确定词折成 ``None``。
    """
    v = _token(x.cl.infection)
    if not isinstance(v, str) or v not in INFECTION_LABELS:
        return None
    return "native" if v.startswith("原生") else "graft"


def g08_r12_infection_origin(x: _Ctx) -> Verdict:
    """P1：能否确定感染发生在原生主动脉还是既往植入物。

    路由层只做**证据定档**，不决定入口节点：``D4.1`` 的分诊本来就要判同一件事
    （感染/瘘 → D4.2 或 D4.3），在这里再决定一次就是第二处判据。本规则的产出
    （原生 / 移植物 / 判不了）记进 trace，供审计与下游核对。

    「明确无感染证据 + 有移植物」返回 ``None`` 而不是 ``False``：VGEI 可以没有
    典型征象，单凭「这次没看到感染」不足以排除 —— 规格的缺失列写的是
    「任一关键条件缺失/冲突→不得按阴性处理」。
    """
    if _infection_site(x) is not None:
        return True
    v = _token(x.cl.infection)
    if v is None or _is_uncertain_token(v):  # 「未评价」等不确定值已在白名单里
        return None
    return None if _has_prior_repair(x) is True else False


def g08_r13_postop_label_not_complication(x: _Ctx) -> Verdict:
    """P1：既往修复 + 本次无**确诊**并发症 → 进 ``D6.1`` 常规随访。

    规格原句：「既往EVAR/OSR且本次常规随访→D6.1；具体内漏/增长/结构异常/感染/瘘
    **经证据支持**→D6.2/D6.3；未核验异常保留疑似状态」。

    所以本条回答的是「有没有**确诊**的并发症」，不是「是不是术后」——
    后者由 ``G08.R06`` 回答（它负责给 ``D6`` 标签）。两条同时成立时，
    标签是 ``D6``、入口是 ``D6.1``，分诊交给 ``D6.1`` 自己按 R01–R03 做。
    """
    repaired = _has_prior_repair(x)
    if repaired is not True:
        return repaired
    confirmed = _or(
        _eq(x.n.endoleak_type, "I", "III", "IV", "V"),
        _ge(x.n.sac_growth, SAC_GROWTH_MM),
        _eq(x.n.graft_major, "组件分离", "移位", "明显闭塞"),
        _eq(x.cl.infection, "原生感染明确", "移植物感染明确"),
        _eq(x.cl.fistula, "主动脉肠瘘", "主动脉腔静脉瘘", "其他腔脏器瘘"),
    )
    return _not(confirmed)


def g08_r14_no_target_candidate(x: _Ctx) -> Verdict:
    """P3：可靠检查未发现任何目标病变 —— **暂定**候选，交 ``G09.R07`` 终审。

    四个条件缺一不可，任一为 UNKNOWN 都不能当阴性：
      1. 身份与时间可靠；
      2. 影像覆盖充分且 CTA 可评价；
      3. 所有目标病种都没有可靠阳性/疑似证据；
      4. 无既往修复需要本次随访。

    「未检查 / 覆盖不足 / 报告未述及」**不满足**本条 —— 规格把它们指向
    ``G08.R15`` / ``G09.R08``（未分类）。所以这里只认 ``aaa_reported == "否定"``，
    不把「未述及」当阴性。
    """
    reliable = _and(_eq(x.q.idmatch, "一致"), _eq(x.q.time, "有效"))
    if reliable is not True:
        return None if reliable is None else False
    covered = _and(_eq(x.q.cta_cover, "完整"), _eq(x.n.cta_status, "完整"))
    if covered is not True:
        return None if covered is None else False
    return _and(
        _eq(x.cl.aaa_reported, "否定"),
        _eq(x.cl.aaa_rupture, "未见相关证据"),
        _eq(x.cl.symptom_aaa, "无当前症状", "明确归因其他"),
        _eq(x.cl.infection, "无证据"),
        _eq(x.cl.fistula, "否"),
        _absent(x.cl.iliac_site, ("左侧", "右侧", "CIA", "IIA", "EIA", "疑似")),
        _not(_has_prior_repair(x)),
    )


def g08_r15_unclassifiable_candidate(x: _Ctx) -> Verdict:
    """P2：没能形成任何标签 —— 暂定「未分类」，交 ``G09.R08``。

    与 ``R14`` 的区别：``R14`` 是「查了，确实没有」；``R15`` 是「查不清」。
    前者可以终止病种路由，后者**不能** —— 「不得默认无病变」是规格原话。
    """
    if x.labels:
        return False
    return _or(
        _not(_eq(x.n.cta_status, "完整")),
        _not(_eq(x.q.cta_cover, "完整")),
        _eq(x.cl.aaa_reported, "未述及"),
        _has(x.cl.aorta_location, "未知"),
    )


# ===========================================================================
# G09 · 分类收尾（9 条）
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


# ---- G09.R05–R09（V3.6.2 新增）----
def _active_infection_or_fistula(x: _Ctx) -> Verdict:
    """当前有感染或主动脉相关瘘证据（含疑似）。

    在 ``G08.R04`` 的分类标签之上，多读一层 ``N.INFECTION_CTX``（抽取层给的
    主状态）：分类域没打标签、但主状态说「医师明确/高度怀疑」时，同样是
    「分类期间新发现的感染」，不能因为前者没填就当成没有。
    """
    return _or(_infection_or_fistula_label(x), _eq(x.n.infection_ctx, "医师明确", "高度怀疑"))


def g09_r05_infection_or_fistula_reflow(x: _Ctx) -> Verdict:
    """P0-A：分类过程中发现感染/主动脉瘘 —— 保留状态并回流 D4，急症时回 G03。

    与 ``G09.R01`` 的分工：``R01`` 只读 ``K.NEW_URGENT``（抽取层给的「正在恶化」
    标记），本条读**分类阶段新发现的**感染/瘘证据。两条都可能为真，任一为真都
    不许「先把分类做完再说」。

    ``True`` 表示「存在需要回流的证据」，具体回 G03 还是只标 D4 由
    :func:`_finish_mode` 结合生命体征判 —— 本规则不重复判急症等级。
    """
    return _active_infection_or_fistula(x)


def g09_r06_postop_abnormality(x: _Ctx) -> Verdict:
    """P1：术后出现内漏/瘤囊增长/移植物故障 —— 进 D6 对应节点，急症回 G03。"""
    repaired = _has_prior_repair(x)
    if repaired is not True:
        return repaired
    return _or(
        _eq(x.n.endoleak_type, "I", "II", "III", "IV", "V", "疑似"),
        _ge(x.n.sac_growth, SAC_GROWTH_MM),
        _eq(x.n.graft_major, "组件分离", "移位", "明显闭塞", "疑似"),
        _eq(x.n.infection_ctx, "医师明确", "高度怀疑"),
    )


def g09_r07_no_target_disease_terminal(x: _Ctx) -> Verdict:
    """P3：确认「无目标病变」是终末结论，不是「暂时没看见」。

    只有 ``G08.R14`` 的候选成立，且以下全部为真时才成立：覆盖充分、影像可评价、
    无新急症、身份/时间可信、无既往修复需随访、无未解决冲突。

    任何一项证据不足都落到 ``G09.R08``（未分类），**不可终止目标病种路由**。
    """
    if x.no_target_candidate is not True:
        return x.no_target_candidate
    confirmed = _and(
        _eq(x.q.cta_cover, "完整"),
        _eq(x.n.cta_status, "完整"),
        _eq(x.q.idmatch, "一致"),
        _eq(x.q.time, "有效"),
        _not(_eq(x.k.new_urgent, "是")),
        _not(_has_prior_repair(x)),
    )
    return confirmed


def g09_r08_unclassifiable(x: _Ctx) -> Verdict:
    """P2：无可信标签或证据不足 —— 输出「未分类」并列出待核字段。

    **不能强行选 D1–D7**：没有标签不等于 D1。这一点和 ``G09.R04`` 是一体两面 ——
    R04 要求「至少一个可信标签」才放行，R08 是它为假时的出口。
    """
    if x.labels:
        # 有标签但冲突未解决 → 也走这里（冲突本身的复核由 R03 承担）
        return None if x.diag_conflict is None else bool(x.diag_conflict)
    return True


def g09_r09_multilabel_coexistence(x: _Ctx) -> Verdict:
    """P3：**允许共存**的多标签（True）与真正互斥的冲突（False）之分。

    规格列举的共存组合：AAA+髂动脉瘤、感染+动脉瘤、炎症性AAA+感染疑似、
    术后状态+新病变 —— 都可以同时成立，不自动判冲突。只有**同一病灶/同一
    时点的互斥状态**（D1/D2/D3 中 ≥2 个）才是真冲突。

    ``G08.R09`` 判「有没有互斥标签」，本条判「这个多标签集合能不能共存」——
    两者是同一判据的正反面，共用 :func:`_exclusive_states`。返回值是
    **「允许共存」**，不是「存在冲突」：调用方据此决定要不要升级成冲突。
    """
    all_labels = set(x.labels) | set(x.cl.diag_multi or ())
    if len(all_labels) < 2:
        return False
    return len(_exclusive_states(x)) < 2


# ===========================================================================
# 规则登记表（66 条，唯一真源）
# ===========================================================================
@dataclass(frozen=True)
class RuleSpec:
    func: Callable[[_Ctx], Verdict]
    source: str
    exit: NextNode
    name: str
    fields: tuple[str, ...] = ()
    level: EmergencyLevel = "未触发"
    """命中后 ``R.AORTIC_EMERGENCY`` 的取值。

    只在 G03 的 P0-A/P0-G 规则上显式声明（那才是「主动脉急症危险」）；
    其余规则即便命中也不是急症结论，故默认「未触发」。给它们一律填
    「明确危险」会让 ``RULES`` 里 40 多条规则声称自己是急症判据。
    """


RULE_SOURCE: dict[str, tuple[SourceKind, str | None]]
"""规则 → ``(来源性质, ESVS 章节/推荐号)``。定义与校验见 ``RULES`` 之后。

为什么单独一张表、而不是写进 :class:`RuleSpec`：
    「来源性质」是横切属性 —— 单看某一条规则的参数时，「它是不是指南原文」
    并不显眼；集中成一张表才能一眼审阅全貌，也才能一眼看出工程规则占多少。
    这与既有的 ``LABEL_BY_RULE`` 是同一种做法。

为什么必须登记到条：不标来源，「本结论基于 ESVS 2024」就是失实的 ——
而临床采纳与论文写作恰好都要用这句话。标成 ``ESVS原文`` 却给不出章节，
等同于空口引用，故一并断言。
"""


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
        "明确危险",
    ),
    "G03.R02": RuleSpec(
        g03_r02_aneurysm_with_hematoma,
        "G03!10",
        "G05",
        "主动脉瘤伴相邻血肿",
        ("c.hematoma", "c.hematoma_adj", "c.source_kind"),
        "明确危险",
    ),
    "G03.R03": RuleSpec(
        g03_r03_rupture_reported,
        "G03!11",
        "G05",
        "医师报告明确主动脉破裂",
        ("c.rupture_report", "q.idmatch"),
        "明确危险",
    ),
    "G03.R04": RuleSpec(
        g03_r04_suspected_rupture_unstable,
        "G03!12",
        "G05",
        "疑似破裂伴血流动力学不稳定",
        ("q.idmatch", "d.dx_context", "c.rupture_report", "v.sbp", "v.conscious_change"),
        "高度疑似",
    ),
    "G03.R05": RuleSpec(
        g03_r05_aaa_acute_pain_attributed,
        "G03!13",
        "G05",
        "AAA 合并急性相关疼痛",
        ("s.pain_current", "s.pain_site", "s.pain_attrib"),
        "明确危险",
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
        ("d.surg", "s.gi_bleed", "s.gi_bleed_type"),
        "明确危险",
    ),
    "G03.R09": RuleSpec(
        g03_r09_acute_limb_ischemia,
        "G03!17",
        "G05",
        "急性肢体缺血危险征象",
        ("s.limb", "s.limb_signs", "s.pulse_deficit", "c.branch"),
        "明确危险",
    ),
    "G03.R10": RuleSpec(
        g03_r10_news2_critical,
        "G03!18",
        "G05",
        "NEWS2 严重恶化（≥7）",
        ("v.rr", "v.sbp", "v.hr", "v.spo2", "v.acvpu", "v.temp", "v.oxygen"),
        "明确危险",
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
        "人工复核",
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
        "明确危险",
    ),
    "G03.R21": RuleSpec(
        g03_r21_lactate_above_uln,
        "G03!29",
        "人工复核",
        "乳酸高于同次检验上限",
        ("lab.lact", "lab.lact_uln", "q.unit"),
    ),
    # ---- G04（21）----
    "G04.R01": RuleSpec(
        g04_r01_short_neck,
        "G04!42",
        "G06",
        "短颈型肾下 AAA",
        ("n.neck_len", "n.renal_l", "n.renal_r"),
    ),
    "G04.R02": RuleSpec(
        g04_r02_juxtarenal,
        "G04!43",
        "G06",
        "近肾型 AAA",
        ("n.neck_len", "n.renal_l", "n.renal_r"),
    ),
    "G04.R03": RuleSpec(
        g04_r03_pararenal, "G04!44", "G06", "肾旁型 AAA", ("n.renal_l", "n.renal_r", "n.sma")
    ),
    "G04.R04": RuleSpec(
        g04_r04_visceral_pararenal,
        "G04!45",
        "G06",
        "内脏旁型 AAA",
        ("n.renal_l", "n.renal_r", "n.sma", "n.ca"),
    ),
    "G04.R05": RuleSpec(
        g04_r05_taaa_iv_extension,
        "G04!46",
        "G06",
        "Ⅳ型 TAAA 扩展",
        ("n.renal_l", "n.sma", "n.ca", "n.thoracic"),
    ),
    "G04.R06": RuleSpec(
        g04_r06_beyond_project_boundary, "G04!47", "G06", "超过Ⅳ型 TAAA 项目边界", ("n.thoracic",)
    ),
    "G04.R07": RuleSpec(
        g04_r07_infection_or_fistula,
        "G04!48",
        "G06",
        "感染或主动脉相关瘘",
        ("n.infection_ctx", "n.fistula_ctx"),
    ),
    "G04.R08": RuleSpec(
        g04_r08_ifu_mismatch, "G04!49", "G06", "已核验的器械近端 IFU 不适配", ("n.seal_ifu",)
    ),
    "G04.R09": RuleSpec(
        g04_r09_iliac_limitation,
        "G04!50",
        "G06",
        "内髂灌注及双侧髂动脉限制",
        ("n.iliac_bilat", "n.iia_left", "n.iia_right"),
    ),
    "G04.R10": RuleSpec(
        g04_r10_access_occlusion, "G04!51", "G06", "常规股动脉通路受限", ("n.access_occlusion",)
    ),
    "G04.R11": RuleSpec(
        g04_r11_prior_repair_reintervention,
        "G04!52",
        "G06",
        "重要 EVAR/OSR 术后再干预线索",
        ("n.prev_repair", "n.endoleak_type", "n.sac_growth"),
    ),
    "G04.R12": RuleSpec(
        g04_r12_physiologic_risk,
        "G04!53",
        "G06",
        "重要生理风险需综合评估",
        ("n.egfr", "n.dialysis", "n.heart_class", "n.resp_support"),
    ),
    "G04.R13": RuleSpec(
        g04_r13_insufficient_for_low_complexity,
        "G04!54",
        "资料核实",
        "资料不足不允许判为低复杂",
        ("n.cta_status", "n.neck_len", "n.renal_l", "n.sma", "n.ca"),
    ),
    "G04.R14": RuleSpec(
        g04_r14_low_complexity_to_guideline,
        "G04!55",
        "G07",
        "低复杂进入指南模式",
        ("n.cta_status",),
    ),
    "G04.R15": RuleSpec(
        g04_r15_genetic_confirmed,
        "G04!56",
        "G06",
        "已明确遗传综合征或遗传性主动脉病",
        ("n.genetic_ctx",),
    ),
    "G04.R16": RuleSpec(
        g04_r16_inflammatory,
        "G04!57",
        "G06",
        "炎症性 AAA 明确或影像高度提示",
        ("n.inflammatory_ctx", "inf.mantle_sign", "inf.wall_fibrosis"),
    ),
    "G04.R17": RuleSpec(
        g04_r17_genetic_suspected,
        "G04!58",
        "G06",
        "存在遗传评估线索但尚未确诊",
        ("n.genetic_ctx", "gen.aaa_age_onset", "gen.family_aneurysm", "gen.syndromic_features"),
    ),
    "G04.R18": RuleSpec(
        g04_r18_inflammation_vs_infection,
        "G04!59",
        "G06",
        "炎症与感染的鉴别",
        ("n.inflammatory_ctx", "n.infection_ctx"),
    ),
    "G04.R21": RuleSpec(
        g04_r21_malperfusion,
        "G04!40",
        "G06",
        "器官或肢体灌注障碍",
        ("c.malperfusion", "c.branch", "s.limb", "s.limb_signs"),
    ),
    "G04.R19": RuleSpec(
        g04_r19_prior_repair_unremarkable,
        "G04!61",
        "G07",
        "既往 EVAR/OSR 但无异常",
        ("cl.prev_evor", "n.prev_repair", "n.endoleak_type", "n.sac_growth", "n.graft_major"),
    ),
    "G04.R20": RuleSpec(
        g04_r20_comorbidity_with_primary,
        "G04!62",
        "G08",
        "遗传/炎症/感染与主病种并存",
        ("genetic_ctx", "inflammatory_ctx", "n.infection_ctx", "cl.aaa_reported"),
    ),
    # ---- G08（15）----
    "G08.R01": RuleSpec(
        g08_r01_d1_asymptomatic,
        "G08!24",
        "G06",
        "D1 无症状 AAA 入口",
        ("cl.aaa_reported", "cl.symptom_aaa", "cl.aaa_rupture"),
    ),
    "G08.R02": RuleSpec(
        g08_r02_d2_symptomatic_unruptured,
        "G08!25",
        "G06",
        "D2 症状性未破裂 AAA 入口",
        ("cl.aaa_reported", "cl.symptom_aaa", "cl.aaa_rupture"),
    ),
    "G08.R03": RuleSpec(
        g08_r03_d3_ruptured,
        "G08!26",
        "G06",
        "D3 破裂/疑似破裂 AAA 入口",
        ("cl.aaa_rupture",),
    ),
    "G08.R04": RuleSpec(
        g08_r04_d4_infected,
        "G08!27",
        "G06",
        "D4 感染性主动脉病变入口",
        ("cl.infection", "cl.fistula"),
    ),
    "G08.R05": RuleSpec(
        g08_r05_d5_special,
        "G08!28",
        "G06",
        "D5 特殊主动脉病变入口",
        ("cl.pau", "cl.imh", "cl.iaad", "cl.saccular", "cl.pseudo"),
    ),
    "G08.R06": RuleSpec(
        g08_r06_d6_postop,
        "G08!29",
        "G06",
        "D6 EVAR/OSR 术后入口",
        ("cl.prev_evor", "n.prev_repair"),
    ),
    "G08.R07": RuleSpec(g08_r07_d7_iliac, "G08!30", "G06", "D7 髂动脉瘤入口", ("cl.iliac_site",)),
    "G08.R08": RuleSpec(
        g08_r08_taaa_iv_label,
        "G08!31",
        "G06",
        "Ⅳ型 TAAA 扩展标签",
        ("n.thoracic", "n.sma", "n.ca"),
    ),
    "G08.R09": RuleSpec(
        g08_r09_multi_label_conflict, "G08!32", "人工复核", "多标签与冲突处理", ("cl.diag_multi",)
    ),
    "G08.R10": RuleSpec(
        g08_r10_inflammatory_as_d5_subtype,
        "G08!33",
        "G06",
        "炎症性 AAA 归入 D5 特殊病种",
        ("n.inflammatory_ctx",),
    ),
    "G08.R11": RuleSpec(
        g08_r11_genetic_as_modifier,
        "G08!34",
        "G06",
        "遗传综合征保留为病因修饰标签",
        ("n.genetic_ctx",),
    ),
    "G08.R12": RuleSpec(
        g08_r12_infection_origin,
        "G08!36",
        "G06",
        "区分感染发生在原生主动脉还是既往植入物",
        ("cl.infection", "cl.prev_evor", "n.prev_repair"),
    ),
    "G08.R13": RuleSpec(
        g08_r13_postop_label_not_complication,
        "G08!37",
        "G06",
        "术后状态标签不等于并发症确诊",
        ("cl.prev_evor", "n.endoleak_type", "n.sac_growth", "n.graft_major"),
    ),
    "G08.R14": RuleSpec(
        g08_r14_no_target_candidate,
        "G08!23",
        "G06",
        "可靠检查未发现本项目目标病变",
        ("cl.aorta_location", "cl.aaa_reported", "cl.iliac_site", "q.cta_cover"),
    ),
    "G08.R15": RuleSpec(
        g08_r15_unclassifiable_candidate,
        "G08!22",
        "G06",
        "目标病变无法分类或证据不足",
        ("cl.aorta_location", "cl.aaa_reported", "n.cta_status"),
    ),
    # ---- G09（9）----
    "G09.R01": RuleSpec(
        g09_r01_new_emergency_reflow, "G09!10", "G05", "发现新急症立即回流", ("k.new_urgent",)
    ),
    "G09.R02": RuleSpec(
        g09_r02_identity_conflict,
        "G09!11",
        "资料核实",
        "身份或关键数据冲突",
        ("q.idmatch", "q.conflict"),
    ),
    "G09.R03": RuleSpec(
        g09_r03_diagnosis_conflict,
        "G09!12",
        "人工复核",
        "病种判断相互矛盾",
        ("cl.diag_multi", "k.diag_conflict"),
    ),
    "G09.R04": RuleSpec(
        g09_r04_classify_and_stop,
        "G09!13",
        "G06",
        "输出疾病分类并停止",
        ("k.diag_conflict",),
    ),
    "G09.R05": RuleSpec(
        g09_r05_infection_or_fistula_reflow,
        "G09!14",
        "G05",
        "分类中发现感染或主动脉相关瘘",
        ("cl.infection", "cl.fistula", "n.infection_ctx"),
        "明确危险",
    ),
    "G09.R06": RuleSpec(
        g09_r06_postop_abnormality,
        "G09!15",
        "G06",
        "新增内漏/瘤囊增长/移植物故障",
        ("n.endoleak_type", "n.sac_growth", "n.graft_major", "n.infection_ctx"),
        "高度疑似",
    ),
    "G09.R07": RuleSpec(
        g09_r07_no_target_disease_terminal,
        "G09!9",
        "无目标病变",
        "无目标病变且目标区域充分评价",
        ("q.cta_cover", "n.cta_status", "q.idmatch", "q.time", "k.new_urgent"),
    ),
    "G09.R08": RuleSpec(
        g09_r08_unclassifiable,
        "G09!8",
        "资料核实",
        "诊断未定、资料不足或无法分类",
        ("k.classifiable", "k.diag_conflict"),
    ),
    "G09.R09": RuleSpec(
        g09_r09_multilabel_coexistence,
        "G09!7",
        "人工复核",
        "允许共存的多标签与真正冲突的区分",
        ("cl.diag_multi",),
    ),
}

assert len(RULES) == 66, (
    "V3.6.2 有 65 条判定规则（G03 21 / G04 20 / G08 15 / G09 9）"
    f"外加 1 条工程补齐（G04.R21），应为 66 条，实际 {len(RULES)}"
)

PENDING_RULES: frozenset[str] = frozenset()
"""V3.6.2 规格里**尚未实现**的判定层规则 —— **当前为空**（65 条全部实现）。

为什么保留这个空常量而不是删掉：``test_ids_are_contiguous`` 用「已实现 + 待实现」
拼出完整编号，缺号会立刻失败。留一个显式空集，等于把「缺口已清零」写进代码，
而不是靠「忘了写」来证明。下一次规格升级新增规则时，往这里加一条即可，
总数断言会立刻提醒还差几条。

历史：V3.6.2 相对 V3.1(2) 新增的 11 条（``G04.R19/R20``、``G08.R12–R15``、
``G09.R05–R09``）已全部实现，本集合因此清空。

G00–G02 的 9 条不在此列——它们归 LLM 抽取层，不进本模块。
当前总数 66 = 规格判定层 65 + 工程补充 1（``G04.R21``）。
"""

assert len(RULES) + len(PENDING_RULES) == 66, (
    f"规格判定层 65 条 + 工程补充 1 条 = 66，实际 {len(RULES)} + {len(PENDING_RULES)}"
)

# ===========================================================================
# 规则来源性质登记（V3.6 起）—— 全 66 条，一条不少
# ===========================================================================
RULE_SOURCE = {
    # ---- G03 急诊分诊（21）：ESVS 6 / 工程 13 / 其他 2 ----
    "G03.R01": ("ESVS原文", "§6.1"),
    "G03.R02": ("ESVS原文", "§6.1"),
    "G03.R03": ("ESVS原文", "§6.1"),
    "G03.R04": ("ESVS原文", "§6"),
    "G03.R05": ("ESVS原文", "§6"),
    "G03.R06": ("工程安全规则", None),
    "G03.R07": ("工程安全规则", None),
    "G03.R08": ("工程安全规则", None),
    "G03.R09": ("工程安全规则", None),
    "G03.R10": ("其他指南", None),  # NEWS2 评分标准，非 AAA 诊断指南
    "G03.R11": ("工程安全规则", None),
    "G03.R12": ("工程安全规则", None),
    "G03.R13": ("工程安全规则", None),
    "G03.R14": ("工程安全规则", None),
    "G03.R15": ("工程安全规则", None),
    "G03.R16": ("工程安全规则", None),
    "G03.R17": ("工程安全规则", None),
    "G03.R18": ("工程安全规则", None),
    "G03.R19": ("工程安全规则", None),
    "G03.R20": ("ESVS原文", "§6"),
    "G03.R21": ("其他指南", None),  # 检验参考区间，非破裂诊断阈值
    # ---- G04 复杂度（21）：ESVS 15 / 工程 6 ----
    "G04.R01": ("ESVS原文", "§8.1"),
    "G04.R02": ("ESVS原文", "§8.1"),
    "G04.R03": ("ESVS原文", "§8.1"),
    "G04.R04": ("ESVS原文", "§8.1"),
    "G04.R05": ("ESVS原文", "§8.1"),
    "G04.R06": ("ESVS原文", "§8.1"),
    "G04.R07": ("ESVS原文", "§10"),
    "G04.R08": ("ESVS原文", "§8.1"),
    "G04.R09": ("ESVS原文", "§9"),
    "G04.R10": ("工程安全规则", None),
    "G04.R11": ("ESVS原文", "§7"),
    "G04.R12": ("工程安全规则", None),  # KDIGO 为其中一部分，主体是工程安全触发
    "G04.R13": ("工程安全规则", None),
    "G04.R14": ("工程安全规则", None),
    "G04.R15": ("ESVS原文", "§10.5 Rec154–155"),
    "G04.R16": ("ESVS原文", "§10.2 Rec145–146"),
    "G04.R17": ("ESVS原文", "Rec154"),
    "G04.R18": ("ESVS原文", "§10.1–10.2"),
    "G04.R21": ("工程安全规则", None),
    "G04.R19": ("ESVS原文", "§7"),  # K 列：ESVS 2024 章节来源已定位
    "G04.R20": ("工程安全规则", None),  # K 列：工程安全规则（非指南原文）
    # ---- G08 病种分类（15）：ESVS 10 / 工程 3 / 其他 2 ----
    "G08.R01": ("ESVS原文", "§2"),
    "G08.R02": ("其他指南", None),  # ACC/AHA 2022
    "G08.R03": ("ESVS原文", "§6"),
    "G08.R04": ("ESVS原文", "§10"),
    "G08.R05": ("ESVS原文", "§10"),
    "G08.R06": ("ESVS原文", "§7"),
    "G08.R07": ("ESVS原文", "§9"),
    "G08.R08": ("ESVS原文", "§8.1"),
    "G08.R09": ("工程安全规则", None),  # VascGuide 临床标签设计规则
    "G08.R10": ("ESVS原文", "§10.2 Rec145–146"),
    "G08.R11": ("ESVS原文", "§10.5 Rec154–155"),
    "G08.R12": ("其他指南", "Rec139–144"),  # K 列：专题指南/共识；L 列另有 ESVS 2026 VGEI
    "G08.R13": ("ESVS原文", "§7"),  # K 列：ESVS 2024 章节来源已定位
    "G08.R14": ("工程安全规则", None),  # K 列：工程安全规则（非指南原文）
    "G08.R15": ("工程安全规则", None),
    # ---- G09 分类收尾（9）：工程 7 / 其他指南 2 ----
    "G09.R01": ("工程安全规则", None),
    "G09.R02": ("工程安全规则", None),
    "G09.R03": ("工程安全规则", None),  # 临床标签一致性规则
    "G09.R04": ("工程安全规则", None),  # V1.1 既有分类体系
    "G09.R05": ("其他指南", "§10"),  # ESVS 2024 §10；ESVS 2026 VGEI
    "G09.R06": ("其他指南", "§7"),  # ESVS 2024 §7；ESVS 2026 VGEI
    "G09.R07": ("工程安全规则", None),
    "G09.R08": ("工程安全规则", None),
    "G09.R09": ("工程安全规则", None),
}
"""66 条规则的来源性质与 ESVS 章节（V3.6.2「备注／依据」列的口径）。

当前分布：ESVS 原文 31 / 工程安全规则 29 / 其他指南 6。

值得知道的是：**G03 急诊 21 条里只有 6 条是 ESVS 原文**，其余 13 条是
工程为安全自行拟定的分流规则 —— 写论文或做临床采纳时不能笼统声称
「全部基于 ESVS 2024」。

V3.6.2 新增的 11 条按各自 K 列登记：``G04.R19``/``G08.R13`` 标「ESVS 2024
章节来源已定位」；``G04.R20``、``G08.R14/R15``、``G09.R07/R08/R09`` 标
「工程安全规则（非指南原文）」；``G08.R12``、``G09.R05/R06`` 标
「专题指南/共识来源」→ 归 ``其他指南``，章节取 L 列（ESVS 2024 §7/§10、
ESVS 2026 VGEI）。
"""

_unregistered = set(RULES) - set(RULE_SOURCE)
if _unregistered:
    raise RuntimeError(f"以下规则未登记来源性质：{sorted(_unregistered)}")

_stray = set(RULE_SOURCE) - set(RULES)
if _stray:
    raise RuntimeError(f"RULE_SOURCE 里有不存在的规则：{sorted(_stray)}")

_empty_section = [rid for rid, (kind, sec) in RULE_SOURCE.items() if kind == "ESVS原文" and not sec]
if _empty_section:
    raise RuntimeError(
        f"以下规则标为 ESVS原文 却未给章节（声称取自指南就必须指得出章节）：{_empty_section}"
    )

# G03 优先级桶（P01–P04 是分组，不是规则，不进 RULES）
#
# 分组口径以 **V3.6.2 每条规则行自己的 P 标记 + M 列出口**为准，不以分组行 F 列
# 的清单为准 —— 两者在 ``G03.R12`` 上已经不同步：分组行仍写 ``R11;R17–R19;R21``，
# 但 R12 单行已标 P1、出口 REVIEW。分组行的清单是旧版残留，单条声明才是判据。
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
    "G03.R16",
    "G03.R20",
)
G03_P02 = ("G03.R11", "G03.R12", "G03.R17", "G03.R18", "G03.R19", "G03.R21")
G03_P03 = ("G03.R13", "G03.R14")
G03_P04 = ("G03.R15",)

EMERGENCY_LABELS: dict[str, str] = {
    # ---- 破裂／疑似破裂 → D3 ----
    "G03.R01": "D3",
    "G03.R02": "D3",
    "G03.R03": "D3",
    "G03.R04": "D3",
    "G03.R05": "D3",
    "G03.R06": "D3",
    "G03.R07": "D3",
    "G03.R16": "D3",
    # ---- 主动脉移植物 + 消化道出血 → D6（主动脉肠瘘是术后并发症） ----
    "G03.R08": "D6",
}
"""命中即**预置病种标签**的急诊规则 → 该规则的病种入口。

为什么在 G03 就预置标签：急诊模式**不跑 G08**（分类需要完整资料），若不预置，
``class_output`` 为空，下游工具A 拿不到病种入口 —— 病种流程永远启动不了，
输出退化成「病种未分类：病种流程未给出结论」。预置只回答「该走哪条病种路径」，
**不改变路由出口本身**（出口仍是 G05）。

为什么是 ``dict`` 而不是一个集合：**标签不都是 D3**。早先只登记了八条破裂规则、
默认所有预置都是 ``D3``，于是 G03.P01 里另外几条命中的病例全部空转。其中两条是
实打实的遗漏，不是「无病种可归」：

``G03.R16`` 医生明确怀疑破裂
    规格 ``D3`` sheet 的对象本就是「破裂／**疑似破裂** AAA」，判据
    （``d.dx_context == "疑似"`` 或 ``c.rupture_report == "疑似破裂"``）描述的
    正是该 sheet 的病人。归 ``D3``。

``G03.R08`` 主动脉移植物史合并消化道出血
    这是**主动脉肠瘘**，属术后并发症。``D6.3.R01`` 的判据里本来就有
    ``active_gi_bleed``，入口 ``D6.1`` 也把「感染/瘘」路由到 ``D6.3``。
    归 ``D6`` 恰是规格已经写好的那条路。

**``G03.R12`` 反过来是「不该有标签」**：它曾在此表里归 ``D3``，因为早先把它
放在 ``G03.P01``（命中即进 G05）。V3.6.2 把它的出口改成 ``REVIEW``（``G03!20``：
「紧急复核，不能降为常规」）——影像不可靠时**不得**据此给出术式方向。它已移入
``G03.P02``，故从本表移除。

剩下三条**确实无病种可归**，登记在 :data:`EMERGENCY_NO_LABEL` —— 不预置是
正确边界而非遗漏，它们靠五段式的「危险提示」交代，不硬塞进病种流程。
"""

#: 命中急诊但**确实无病种可归**的规则 —— 与 :data:`EMERGENCY_LABELS` 互补。
#:
#: 为什么要把「无病种」也显式登记，而不是留空：留空无法与「忘了登记」区分。
#: 下面这条导入期自检要求 ``G03_P01`` 的每一条都落在本集合或
#: :data:`EMERGENCY_LABELS` 里——新增一条急诊规则却忘了决定标签时，import 就
#: 会炸，而不是等到某个病例输出「病种未分类」才发现。
EMERGENCY_NO_LABEL: frozenset[str] = frozenset(
    {
        # 急性肢体缺血：D1–D7 里没有这个病种，工具A 无 sheet 可查。
        "G03.R09",
        # NEWS2 严重恶化：全身性危险征象，与主动脉病种无关。
        "G03.R10",
        # 严重低血压（不依赖已知 AAA）：同上，未确认是主动脉源。
        "G03.R20",
    }
)

#: 命中即预置 ``D3`` 标签的规则 —— 从 :data:`EMERGENCY_LABELS` 派生，不另抄一份。
#: 病种层（``nodes/d3_ruptured.py``）用它判「确认型 vs 临床怀疑型」，故保留这个名字。
D3_EMERGENCY_RULES: frozenset[str] = frozenset(
    rid for rid, label in EMERGENCY_LABELS.items() if label == "D3"
)

# 导入期自检：G03.P01 每条规则必须「有病种标签」或「明确登记为无病种」。
# 二者皆无 = 命中后工具A 静默空转（输出退化成「病种未分类」），正是本表要防的。
_undecided_emergency = set(G03_P01) - EMERGENCY_LABELS.keys() - EMERGENCY_NO_LABEL
if _undecided_emergency:
    raise RuntimeError(
        "以下 G03 急诊规则既没有病种标签、也没有登记为「无病种可归」，"
        "命中后工具A 会静默空转："
        f"{sorted(_undecided_emergency)}"
    )
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
    "G04.R18",
)
"""阻断型：命中即判复杂；其 UNKNOWN 也不是放行理由（由 R14 的 ``all_clear`` 兜）。

为什么 ``G04.R18``（炎症与感染的鉴别）在这组而不是「加分型」：V3.6.2 给它的
类别是 **P0-A**，缺失列写「关键输入缺失/未知/冲突时转 VERIFY/REVIEW，不按阴性
处理」。放进加分型会让「炎症与感染分不清」的病例在 UNKNOWN 时照常放行到 G07，
于是一个未排除感染的病人被当成低复杂择期处理。这正是 ``all_clear`` 要挡的事。
"""
G04_EXTRA = ("G04.R15", "G04.R16", "G04.R17", "G04.R21")
"""加分型：遗传 / 炎症 / 灌注线索。命中 → 复杂；其 UNKNOWN **不阻断** G07 放行。

规格对遗传与炎症明写「**仅因**未做遗传检测 / CRP 不强制所有常规病例升复杂」
（``G04!55``）——这两类描述的是**病因修饰**，不是「必须排除的危险」。
``G04.R18`` 不在其中，见上。

``G04.R21`` 是本条唯一一条**代码侧补齐**的规则（V3.6.2 只有 ``G04!40``
``C.MALPERFUSION`` 字段行、没有规则行），放在加分型是同一逻辑：灌注障碍缺证据时
不该把整例挡在 G07 之外，但一旦确认就必须进复杂。来源性质登记为「工程安全规则」，
不得计入 ESVS 原文。
"""
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


def _confidence(
    gaps: list[str],
    unknown_decisive: bool,
    *,
    emergency_without_label: bool = False,
) -> ConfidenceLevel:
    """置信度的三档口径。

    ``emergency_without_label`` 是「急诊模式但一个 D1–D7 标签都没打出来」——
    它不是「资料待补」，而是**本次结论只到分流层**：``G03.R09`` 急性肢体缺血、
    ``G03.R10`` NEWS2 恶化、``G03.R20`` 严重低血压这三条本就无病种可归，
    工具A 一个节点都启动不了。此时若仍报「高」，等于说「系统看明白了」，
    而实际上后面什么都没有。降「低」是让它进人工复核，不是降低危险等级
    （危险提示照给，那由 ``emergency_level`` 与五段式承担）。
    """
    if unknown_decisive or emergency_without_label:
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
    next_nodes: list[str] | None = None,
    class_output: list[str] | None = None,
) -> RouteResult:
    labels_out = list(class_output) if class_output is not None else list(x.labels)
    gap_list = [*(gaps or []), *_cross_domain_gaps(x)]

    # 急诊模式 + 零标签 = 病种流程无法启动。补一条能被输出层直接展示的缺口，
    # 并降置信度（见 ``_confidence``）。三条无病种规则（R09/R10/R20）走的就是这里：
    # 危险提示是对的，但「病种级建议」确实不存在，报告不能假装它存在。
    emergency_without_label = mode is RouteMode.EMERGENCY and not labels_out
    if emergency_without_label:
        marker = f"未匹配 D1–D7 病种流程（{rule_id}）：病种级建议不可用，需人工判断"
        if marker not in gap_list:
            gap_list.append(marker)

    return RouteResult(
        kind=kind,
        rule_id=rule_id,
        reason=reason,
        next_node=next_node,
        # 只走一处时自动退化为一元序列，调用方无需判空。
        next_nodes=next_nodes or [next_node],
        emergency_level=level,
        mode=mode,
        evidence_gaps=gap_list,
        confidence_level=_confidence(
            gap_list,
            unknown_decisive,
            emergency_without_label=emergency_without_label,
        ),
        class_output=labels_out,
        cl_taaa_iv=x.taaa_iv,
        k_identity_conflict="是" if _token(x.q.idmatch) == "不一致" else None,
        k_diag_conflict=None if x.diag_conflict is None else ("是" if x.diag_conflict else "否"),
        k_classifiable=_classifiable(x),
        trace=trace or [],
    )


def route(case: VascCase, measurement: VascMeasurement | None = None) -> RouteResult:
    """术前路由主入口：G03 → G04 →（G05 | G06 | G07）→ G08 → G09。

    参数：
        case: V3.6.2 病例（162 字段）。
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
            class_output=[EMERGENCY_LABELS[hit]] if hit in EMERGENCY_LABELS else None,
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
        # G04.R19 与 R14 同出口（G07），但依据更具体：R14 只回答「所有阻断项
        # 都清了」，R19 回答「清了，而且这人是术后随访」。审计时后者能一眼
        # 看出病人做过手术 —— 同样的出口，证据粒度不同。
        if RULES["G04.R19"].func(x) is True:
            trace.append(RuleTrace("G04.R19", True, "G07"))
            return _finish_mode(x, "G07", RouteMode.GUIDELINE, "G04.R19", trace)
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
    """进入模式后继续跑 G08/G09：产出病种标签、处理冲突与回流。

    V3.6.2 的完整顺序（顺序本身是判据，不要重排）::

        G08.R01–R08   打主标签（D1–D7 + Ⅳ型TAAA 扩展）
        G08.R10/R11   病因修饰标签（炎症 → D5、遗传 → 保留为病因）
        G04.R20       病因与主病种并存的确认（不改出口，只留痕）
        G08.R12/R13   感染来源与术后分诊的**证据定档**（不决定入口节点）
        G08.R14/R15   无目标病变 / 未分类的**候选**（暂定，交 G09 终审）
        G08.R09+R09   多标签冲突（R09 把允许共存的组合排除在冲突之外）
        G09.R05/R06   分类中新发现的感染/瘘、术后异常（补标签，可回流）
        G09.R01       抽取层标记的新急症 → 回流 G05（最优先）
        G09.R02       身份/来源冲突 → 隔离核实
        G09.R04       正常出口
        G09.R03       冲突 → 人工复核
        G09.R07/R08   无标签时的终审：无目标病变 / 未分类
    """
    # ---- G08 主标签 ----
    for rid in G08_RULES:
        v = RULES[rid].func(x)
        trace.append(RuleTrace(rid, v, "G06"))
        if rid == "G08.R08":
            # CL.TAAA_IV 是判定产物：算法在这里一次性固化，随 RouteResult 下去，
            # 不让下游工具自己重算（那会变成第二处判据）。
            x.taaa_iv = _taaa_grade(v)
        if v is True and LABEL_BY_RULE[rid] not in x.labels:
            x.labels.append(LABEL_BY_RULE[rid])

    # ---- G08.R10/R11 病因修饰标签 ----
    if RULES["G08.R10"].func(x) is True and "D5" not in x.labels:
        x.labels.append("D5")
        trace.append(RuleTrace("G08.R10", True, "G06"))
    if RULES["G08.R11"].func(x) is True:
        trace.append(RuleTrace("G08.R11", True, "G06"))

    # ---- G04.R20 病因与主病种并存 ----
    # 出口写的是 G08，说明它属于分类阶段。此处复记一次：G04 阶段判它时
    # 主标签还没出来，只能判「有病因、有 AAA」；到这里才谈得上「并存」。
    trace.append(RuleTrace("G04.R20", RULES["G04.R20"].func(x), "G08"))

    # ---- G08.R12/R13 证据定档，不决定入口 ----
    # 入口细化（D4.2/D4.3、D6.1/D6.2/D6.3）由 D4.1 / D6.1 分诊时判 —— 在这里
    # 再判一次就是第二处判据。这两条只留痕，供审计与下游核对。
    for rid in ("G08.R12", "G08.R13"):
        trace.append(RuleTrace(rid, RULES[rid].func(x), "G06"))

    # ---- G08.R14/R15 候选（暂定）----
    x.no_target_candidate = RULES["G08.R14"].func(x)
    trace.append(RuleTrace("G08.R14", x.no_target_candidate, "G09"))
    x.unclassifiable_candidate = RULES["G08.R15"].func(x)
    trace.append(RuleTrace("G08.R15", x.unclassifiable_candidate, "G09"))

    # ---- G08.R09 × G09.R09 冲突判定 ----
    raw_conflict = RULES["G08.R09"].func(x)
    coexist = RULES["G09.R09"].func(x)
    trace.append(RuleTrace("G08.R09", raw_conflict, "人工复核"))
    trace.append(RuleTrace("G09.R09", coexist, "人工复核"))
    # R09 的白名单意义在这里落地：被判为「允许共存」的多标签不升级成冲突。
    x.diag_conflict = bool(raw_conflict) and coexist is not True

    # ---- G09.R05 分类中新发现的感染/主动脉瘘 ----
    # 规格：「新增感染/瘘证据时保留疑似/确诊状态并路由 D4；如活动性出血、休克
    # 或破裂危险，**同时**回 G03/G05」。
    #
    # 这里**只留痕、不补 D4 标签**：D4 标签由 ``G08.R04`` 负责（两者同源读
    # ``CL.INFECTION`` / ``CL.FISTULA``）。补一次会让病种引擎从 ``D4.1`` 再走
    # 一遍 —— 而 ``D6.3`` 已经转到 ``D4.3``，多出来的 ``D4.1`` 是重复路径。
    # 急症回流由 ``G09.R01`` 承担，本规则不重复判急症等级（那是 G03 的职责）。
    trace.append(RuleTrace("G09.R05", RULES["G09.R05"].func(x), "G06"))

    # ---- G09.R06 术后异常 ----
    if RULES["G09.R06"].func(x) is True:
        trace.append(RuleTrace("G09.R06", True, "G06"))
        if "D6" not in x.labels:
            x.labels.append("D6")

    # ---- G09.R01 回流优先于模式（优先级 G03 回流 > G06）----
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

    # ---- G09.R02 身份/来源冲突 → 隔离不可信记录 ----
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

    # ---- G09.R04 正常出口 ----
    trace.append(RuleTrace("G09.R04", RULES["G09.R04"].func(x), "G06"))

    # ---- G09.R03 冲突 → 人工复核 ----
    # 求值而不是硬写 True：留痕里的 ``verdict`` 必须是这条规则自己算出来的，
    # 否则「R03 成立」这句话在审计里没有依据（``diag_conflict`` 恒为 bool）。
    if RULES["G09.R03"].func(x) is True:
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

    # ---- G09.R07 / R08：没有标签时的终审 ----
    # 这一段只在「一个标签都没打出来」时生效。有标签就走正常出口 ——
    # 「无目标病变」和「已分到 D1」是互斥的，不能同时成立。
    if not x.labels:
        if RULES["G09.R07"].func(x) is True:
            trace.append(RuleTrace("G09.R07", True, "无目标病变"))
            return _make(
                x,
                kind=RouteKind.NO_TARGET,
                rule_id="G09.R07",
                reason=(
                    "G09.R07 目标区域已充分评价且未见任何目标病变：停止 D1–D7 分流，"
                    "由临床团队继续其他病因诊断。"
                ),
                next_node="无目标病变",
                level="未触发",
                gaps=_collect_gaps(x, ("G08.R14", "G09.R07"), trace),
                trace=trace,
            )
        if RULES["G09.R08"].func(x) is True:
            trace.append(RuleTrace("G09.R08", True, "资料核实"))
            return _make(
                x,
                kind=RouteKind.VERIFY,
                rule_id="G09.R08",
                reason=(
                    "G09.R08 未能形成可信病种标签：输出未分类并列出待核字段，不强行选择 D1–D7。"
                ),
                next_node="资料核实",
                level="未触发",
                gaps=_collect_gaps(x, ("G08.R15", "G09.R08"), trace),
                unknown_decisive=True,
                trace=trace,
            )

    # ---- 出口自洽性：D3 标签与「非急诊模式」不可能同时为真 ----
    # 为什么需要这一层：G03 的 21 条规则里**没有一条读 `CL.AAA_RUPTURE`**，它只认
    # 影像证据（`C.EXTRAV`/`C.HEMATOMA`/`C.RUPTURE_REPORT`）与「临床疑似」。
    # 抽取层若给了「破裂：明确」而影像明细缺项，P0 一条都不命中 → `G03.R15` 放行
    # → `G04` → G07 指南模式；随后 `G08.R03` 才补上 D3 标签。结果是一份
    # 「指南模式 · 破裂 AAA：EVAR 一线」的报告：模式是择期、标签是破裂。
    #
    # 这里**不替 G03 判急症**——那会造出规格里没有的第二处判据；只拒绝「按常规继续」：
    # 降为人工复核，并把矛盾写进缺口。降级的不是危险等级，是「能不能自动往下走」。
    if "D3" in x.labels and mode is not RouteMode.EMERGENCY:
        trace.append(RuleTrace(GUARD_D3_LABEL, True, "人工复核"))
        return _make(
            x,
            kind=RouteKind.REVIEW,
            rule_id=GUARD_D3_LABEL,
            reason=(
                f"{GUARD_D3_LABEL} 病种标签含 D3（破裂／疑似破裂），"
                f"但急诊路由未触发（当前判定为{mode.value}）："
                "两者不可能同时为真，不得按常规流程继续，需人工确认破裂证据。"
            ),
            next_node="人工复核",
            level="未触发",
            gaps=[
                *_collect_gaps(x, ("G08.R03",), trace),
                "CL.AAA_RUPTURE 与影像证据不一致：已给出 D3 标签，但 G03 无急症触发",
            ],
            unknown_decisive=True,
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


# ===========================================================================
# 三值原语的公开别名 —— 供 ``src/tools/`` 复用同一套判定语言
# ===========================================================================
# 「规则主导」不等于「规则实现只能有一份」：三值语义是全系统共用的**判定语言**，
# 工具A 的病种规则必须与 G03–G09 用同一套 Kleene 求值 —— 否则同一个「疑似」
# 在两个层会被解释成两种结果，而审计时无从判断哪个对。
#
# 下面的赋值**不是第二份实现**：只是给同一批函数对象再起一个公开名字，
# 让 ``src/tools/`` 不必 import 下划线私名。改语义仍然只改上面那一处。
and_ = _and
or_ = _or
not_ = _not
eq = _eq
has = _has
absent = _absent
ge = _ge
between = _between
token = _token
is_uncertain_token = _is_uncertain_token


__all__ = [
    "D3_EMERGENCY_RULES",
    "EMERGENCY_LABELS",
    "EMERGENCY_NO_LABEL",
    "FALLBACK_VERIFY",
    "FISTULA_LABELS",
    "G03_P01",
    "G03_P02",
    "G03_P03",
    "G03_P04",
    "G04_BLOCKING",
    "G04_EXTRA",
    "G08_RULES",
    "GI_BLEED_SIGNS",
    "GUARD_D3_LABEL",
    "INFECTION_LABELS",
    "LABEL_BY_RULE",
    "LIMB_ISCHEMIA_SIGNS",
    "MANUAL_URGENCY",
    "OUTCOME_PRIORITY",
    "PENDING_RULES",
    "RULES",
    "RULE_SOURCE",
    "RouteKind",
    "RouteResult",
    "RuleSpec",
    "RuleTrace",
    "SourceKind",
    "TaaaGrade",
    "Verdict",
    "absent",
    "and_",
    "between",
    "eq",
    "ge",
    "has",
    "is_uncertain_token",
    "not_",
    "or_",
    "route",
    "token",
]
