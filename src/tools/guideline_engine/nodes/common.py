"""病种层公共设施 —— 优先级求值、缺口汇总、出户口径。

为什么需要这一层：
    21 个病种 sheet 的规则都按同一套优先级执行（``NODE.ORDER`` 明文：
    「按 P0→P1→P2→P3 执行，P0 危险先输出且不得被低优先级覆盖」），并且每个
    sheet 都自带 ``R90``（P2 数据质量阻断）与 ``R91``（P3 出口）两条结构性规则。
    把这套执行语义写一次，7 个病种文件只声明「哪个规则、什么优先级、命中后去哪」。

优先级与出口的对应（规格 ``NODE.ORDER`` 各 sheet 一致）::

    P0-A  急症危险    → escalate 到 G05（急诊），可同时 next 到下一节点
    P1    重要分支    → next 或 terminal（急诊术式备选 / 专科评估）
    P2    数据阻断    → terminal「人工复核」+ gaps（对应 R90）
    P3    常规出口    → next / terminal（对应 R91 与各病种出口规则）

三条不变量（都有测试守着）：
    1. **P0 先于一切**：按序执行，P0 命中即返回，低优先级不得覆盖它；
    2. **UNKNOWN 不是 False**：``verdict is None`` 的规则继续往下跑，不被当否定；
    3. **全部落空不等于放行**：无任何出口时落到「人工复核」，不默认阴性。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from ....models.preop.case import UNCERTAIN_TOKENS
from ....rules.router import (
    FISTULA_LABELS,
    GI_BLEED_SIGNS,
    INFECTION_LABELS,
    LIMB_ISCHEMIA_SIGNS,
    SAC_GROWTH_MM,
    SBP_SHOCK,
    Verdict,
    eq,
    has,
    or_,
    token,
)
from ..state import GuidelineEntry, GuidelineState, Link, NodeOutcome

__all__ = [
    "ILIAC_REPAIR_MM",
    "PRIORITY_ORDER",
    "SPEC_UNCERTAIN_TOKENS",
    "THRESHOLD_FEMALE_MM",
    "THRESHOLD_MALE_MM",
    "DisplayRule",
    "ExitKind",
    "Link",
    "Priority",
    "RuleSlot",
    "aaa_diameter",
    "access_blocked",
    "active_gi_bleed",
    "acute_limb_ischemia",
    "anatomy_available",
    "at_repair_threshold",
    "branch_involved",
    "cta_evaluable",
    "display_entries",
    "evaluate_node",
    "fistula_present",
    "gap_if_missing",
    "infection_typed",
    "is_spec_uncertain",
    "limb_ischemia_present",
    "malperfusion",
    "missing_fields",
    "multi_field",
    "recorded",
    "rupture_evidence",
    "sac_growing",
    "sbp_below_shock",
    "sex_threshold",
    "spec_token",
    "threshold_usable",
    "unstable",
]

#: 规格 ``D*.`` 字段自己的「有值但不确定」写法 —— 与模型层词表**分开维护**。
#:
#: 为什么不并进 :data:`~src.models.preop.case.UNCERTAIN_TOKENS`：那是
#: ``VascCase`` 值域的集合，增删会改变**判定层**行为。``q.current_assess`` 的
#: 「未评估」是**确定的时间口径结论**（``G03`` 用它判「本次是否已筛查」），
#: 一旦折成 UNKNOWN，那条规则就从「命中」变成「未知」——性质变了，且是安全侧
#: 反向变化。规格 ``D*.`` 字段是另一套值域：``D1.LIFE_EXPECTANCY`` 写「未评价」、
#: ``D1.PROGNOSIS`` 写「未评估」、``D1.PREFERENCE`` 写「尚未讨论」、
#: ``D1.FAMILY_HISTORY`` 写「未询问」——四个都表示「还没有结论」，
#: 但模型层词表只收了第一个。
SPEC_UNCERTAIN_TOKENS: frozenset[str] = UNCERTAIN_TOKENS | {
    "未评估",
    "尚未讨论",
    "未讨论",
    "未询问",
}

Priority = Literal["P0-A", "P1", "P2", "P3"]

#: 数字越小越先执行；规格的 ``P0-A/P0-G > P1 > P2 > P3``。
PRIORITY_ORDER: dict[str, int] = {"P0-A": 0, "P1": 1, "P2": 2, "P3": 3}

ExitKind = Literal["next", "escalate", "terminal"]


@dataclass(frozen=True, slots=True)
class RuleSlot:
    """一条病种规则的声明。

    ``exit_kind`` 与 ``exit_value`` 决定命中后去哪：

    · ``escalate``：回流判定层（``"G05"`` 急诊 / ``"G03"`` 重新筛查）——
      与 ``next`` **可以共存**：规格多条规则写的是「G05 急诊模式并进入 D3.2」，
      即「同时通知急诊路径」和「继续查 D3.2」，两者不矛盾；
    · ``next``：继续下一个病种节点；
    · ``terminal``：落到人工决策（``"人工复核"`` / ``"术者确认"`` / ``"监测"``）。
      与 ``next`` 互斥——既终止又继续会让「本次到底去哪」失去主语。
    """

    rule_id: str
    priority: Priority
    source_row: str
    statement: str
    judge: Callable[[GuidelineState], bool | None]
    exit_kind: ExitKind
    exit_value: str
    next_nodes: tuple[str, ...] = ()
    """规格 M 列「下一节点/终点」的**完整值域**（含 ``exit_value``）。

    留空表示该列只有 ``exit_value`` 一个值。填了就必须与 M 列逐字一致 ——
    ``tests/test_guideline_engine.py::TestSpecNextNodes`` 拿它和规格原文对账。
    """
    link: Link = Link.EITHER
    """``next_nodes`` 之间的关系，按规格**动作列**的措辞定档（见 :class:`Link`）。"""
    also_next: str | None = None
    """``escalate`` 时附带继续走的病种节点（规格「→G05 并进入 D3.2」）。"""
    also_escalate: str | None = None
    """``next`` 型规则在 ``escalate_if`` 成立时**并行**通知的上游节点。

    规格 ``D4.1.R03``：「明确/疑似主动脉相关瘘→D4.3；**如伴**活动性出血、休克
    或新破裂证据，先回 G03/G05」。主出口恒为 D4.3，急症是**附加义务**而不是
    替代出口 —— 用 ``exit_kind="escalate"`` 表达会把「无急症的瘘」也标成急症。
    """
    escalate_if: Callable[[GuidelineState], bool | None] | None = None
    """``also_escalate`` 的触发条件；``None`` 表示只要命中这条规则就并行通知。"""
    gaps: tuple[str, ...] = ()


def _continuing_node(slot: RuleSlot) -> str | None:
    """本次**继续往下走**的病种节点。

    ``escalate`` 型：看 ``also_next``（规格「G05 并进入 D3.2」里的 D3.2）；
    ``next`` 型：就是 ``exit_value``；
    ``terminal`` 型：不走，返回 ``None``。
    """
    return slot.also_next or (slot.exit_value if slot.exit_kind == "next" else None)


def _escalation_of(slot: RuleSlot, state: GuidelineState) -> str | None:
    """本次需要通知的上游节点（急诊/重筛查/重分类）。

    两种来源，语义不同：

    · ``exit_kind="escalate"``：这条规则的**主出口**就是上游节点；
    · ``also_escalate``：主出口是病种节点，``escalate_if`` 成立时才**并行**通知。
      规格 ``D4.1.R03`` 的「瘘→D4.3；**如伴**活动性出血…先回 G03/G05」属于这种——
      没有急症的瘘不该被标成急症。
    """
    if slot.exit_kind == "escalate":
        return slot.exit_value
    if slot.also_escalate is None:
        return None
    if slot.escalate_if is None:
        return slot.also_escalate
    return slot.also_escalate if slot.escalate_if(state) is True else None


def evaluate_node(
    node_id: str,
    state: GuidelineState,
    slots: Sequence[RuleSlot],
) -> NodeOutcome:
    """按优先级执行规则，取**第一条命中**的出口。

    参数：
        node_id: 当前节点（``"D3.1"``）。
        state: 运行状态（只读，不改）。
        slots: 本节点的规则声明，顺序无关（内部按 ``priority`` 排序）。

    返回：
        该节点的 :class:`NodeOutcome`，含全部已执行规则的判定留痕。

    为什么未命中的规则也进 ``entries``：审计要回答的是「为什么没走那条路」，
    而不只是「走了哪条路」。只记命中项会让复核者看不到被否决的链条。
    """
    ordered = sorted(slots, key=lambda s: PRIORITY_ORDER[s.priority])
    verdicts: dict[str, bool | None] = {}
    entries: list[GuidelineEntry] = []

    for slot in ordered:
        verdict = slot.judge(state)
        verdicts[slot.rule_id] = verdict
        entries.append(
            GuidelineEntry(
                node_id=node_id,
                rule_id=slot.rule_id,
                source_row=slot.source_row,
                statement=slot.statement,
                verdict=verdict,
                next_node=_continuing_node(slot),
                priority=slot.priority,
            )
        )
        if verdict is True:
            return NodeOutcome(
                node_id=node_id,
                verdicts=verdicts,
                entries=tuple(entries),
                next_node=_continuing_node(slot),
                escalation=_escalation_of(slot, state),
                terminal=slot.exit_value if slot.exit_kind == "terminal" else None,
                next_nodes=slot.next_nodes or (slot.exit_value,),
                link=slot.link,
                gaps=slot.gaps,
            )

    # 全部规则都未命中（None 或 False）→ 规格要求「未知不等于阴性」，
    # 不得默认放行，落到人工复核。
    return NodeOutcome(
        node_id=node_id,
        verdicts=verdicts,
        entries=tuple(entries),
        terminal="人工复核",
        gaps=(f"{node_id}：全部规则均未命中，无可放行的确定性依据",),
    )


def gap_if_missing(label: str, value: object) -> str | None:
    """字段缺失或取值本身不确定时，返回一条缺口描述；否则 ``None``。

    只用于**生成待补清单**，不参与判定——判定一律走 ``eq`` / ``has`` 等三值原语。
    分开的理由：缺口要给人看（要写清字段名和实际取值），判定要给机器算。
    「算不算不确定」本身只走 :func:`is_spec_uncertain`，不在本函数里再判一次。
    """
    if value is None:
        return f"{label}：未记录"
    raw = getattr(value, "value", value)
    return f"{label}：{raw}" if is_spec_uncertain(raw) else None


def is_spec_uncertain(value: object) -> bool:
    """取值本身不确定（「未核验」「未评估」「尚未讨论」等）。

    与 :func:`gap_if_missing` 的分工：本函数回答「这个值算不算不确定」，
    后者回答「要不要记一条缺口」。判定与展示分开，避免同一份白名单两处维护。

    词表取 :data:`SPEC_UNCERTAIN_TOKENS`（**不是**模型层的 ``UNCERTAIN_TOKENS``）：
    本函数只服务于病种域字段，那里的「未评估」「尚未讨论」「未询问」同样是
    「还没有结论」，但不在模型层词表里。两者**不同名**是刻意的——同名同参却
    答案不同（``「未评估」`` 在这里为真、在模型层为假），是最难查的那类错。
    """
    raw = getattr(value, "value", value)
    return isinstance(raw, str) and raw in SPEC_UNCERTAIN_TOKENS


#: ``CL.INFECTION`` 里表示「有感染」的四个取值（明确 + 疑似，原生 + 移植物）。
#: **直接复用判定层的同一份表** —— 两处各写一份，改规格时必有一处漏改。
INFECTION_SUBTYPES: tuple[str, ...] = INFECTION_LABELS

#: ``N.INFECTION_CTX`` 里表示「医生认为有感染」的两个取值。
#: 「仅非特异征象」不在此列：规格 ``D4.NATIVE_CLINICAL`` 明写「发热或疼痛单项
#: 不能确诊感染」，把它算成感染会让每个发热病例都进感染路径。
INFECTION_DOCTOR_TOKENS: tuple[str, ...] = ("医师明确", "高度怀疑")


def fistula_present(state: GuidelineState) -> Verdict:
    """主动脉相关瘘（含「疑似」）—— **全引擎唯一判据**，D4 与 D6 共用。

    取值表直接复用判定层的 :data:`~src.rules.router.FISTULA_LABELS`：D4 与 D6
    原先各写一份四值元组，改规格时漏改一处，同一份报告就会一边说「有瘘」、
    另一边说「没有」。
    """
    return eq(token(state.case.cl.fistula), *FISTULA_LABELS)


def infection_typed(state: GuidelineState) -> Verdict:
    """「这个病人的主动脉/髂动脉有没有感染」—— **全引擎唯一判据**。

    读两处，任一阳性即成立：

    · ``CL.INFECTION``  —— 分型结论（原生/移植物 × 明确/疑似）；
    · ``N.INFECTION_CTX`` —— 医生的整体判断（医师明确 / 高度怀疑）。

    为什么必须共用：这三个病种都问同一件事，原先各写一份，口径还都不一样 ——

    ==================== ========================================== ============
    消费者                原判据                                       漏掉的情形
    ==================== ========================================== ============
    ``D4._infection_signal`` ``cl.infection`` ∪ 医生判断 ∪ 影像组合证据   —
    ``D6._infection_suspected`` 只读 ``n.infection_ctx``（含「仅非特异」） ``cl.infection`` 写「原生感染疑似」时判 False
    ``D7._infection_present`` 只读 ``cl.infection``                  医生「高度怀疑」时判 False
    ==================== ========================================== ============

    后果不是崩溃，是**同一份报告里出现相反结论**：D4 说「感染性，EVAR 仅作
    桥接」，D7 说「髂动脉腔内修复：可行候选」。

    各消费者只在本函数之上加**自己那一层**：D4 追加影像组合证据（诊断用），
    D6 追加「仅非特异征象」（筛「未排除」用）。**不要**再各写一份 ``cl.infection``
    的取值表。
    """
    return or_(
        eq(token(state.case.cl.infection), *INFECTION_SUBTYPES),
        eq(token(state.case.n.infection_ctx), *INFECTION_DOCTOR_TOKENS),
    )


def spec_token(value: object) -> object:
    """读一个规格病种域字段：不确定写法折成 ``None``，其余取 ``value``。

    折成 ``None`` 而不是保留原串，是为让三值原语的语义自然生效：
    ``eq(None, "有限")`` → UNKNOWN（判不了），而 ``eq("未评估", "有限")`` 会得到
    ``False``——被当成「**确定**不是有限」，这正是规格反复禁止的那类误判
    （``D1.2`` 的 J 列：「未评估不等于无衰弱」「未讨论→安排共同决策」）。
    """
    raw = getattr(value, "value", value)
    if raw is None:
        return None
    if isinstance(raw, str) and raw in SPEC_UNCERTAIN_TOKENS:
        return None
    return raw


def recorded(state: GuidelineState, spec_id: str) -> Verdict:
    """病种域字段是否**已记录明确内容**（非空、非不确定）—— 单值与多选都适用。

    与 :func:`spec_token` 的分工：后者给单值字段用（返回折过 None 的值），
    本函数给「多选 + 状态」类字段用（``D1.RISK_OPTIMIZATION`` /
    ``D1.DISTAL_SEAL``）——那类字段的值是列表或结构化串，「有内容」本身就是结论。
    """
    value = state.node_field(spec_id)
    if value is None:
        return None
    if isinstance(value, str):
        return None if value in SPEC_UNCERTAIN_TOKENS else True
    if isinstance(value, list | tuple):
        return True if value else None
    return True


def multi_field(state: GuidelineState, spec_id: str) -> list[object] | None:
    """读一个多选型病种域字段，统一成 ``list``；缺失返回 ``None``。

    为什么需要它：``node_inputs`` 是裸 ``dict``，同一个字段可能被写成 ``"无"``
    （单值）或 ``["无"]``（列表）。而 ``has()`` 对字符串会**逐字符迭代**——
    把「输尿管梗阻」拆成五个字去比对，永远不命中。进 ``has()`` 之前先归一。
    """
    value = state.node_field(spec_id)
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return list(value)
    return [value]


def missing_fields(state: GuidelineState, fields: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """从 ``node_inputs`` 通道逐项取字段，缺失或取值不确定的记一条缺口。

    ``fields`` 是 ``(规格ID, 人话标签)`` 序列。标签要写清「缺了它会挡住什么」，
    因为缺口清单直接给医生看：``D3.2.R06`` 要求「立即显示缺失的术者、手术室、
    影像、器械、血库/ICU 能力」，只报字段名等于把翻译工作推回给读者。
    """
    out: list[str] = []
    for spec_id, label in fields:
        gap = gap_if_missing(f"{spec_id}（{label}）", state.node_field(spec_id))
        if gap:
            out.append(gap)
    return tuple(out)


def anatomy_available(state: GuidelineState) -> Verdict:
    """EVAR 解剖三件套（瘤颈长度 / 直径 / 角度）是否足以比较术式。

    三值：齐全 → ``True``；**一个都没有** → ``False``（数据阻断，``D*.R90`` 口径）；
    部分缺 → ``None``（有方向但把握不足，只记缺口，不阻断）。

    为什么不建一个「解剖信息状态」字段：它问的是「资料够不够」，而够不够直接看
    三个参数有没有值就知道。再造一个「关于信息的字段」等于把同一事实存两遍。
    """
    vals = (state.case.n.neck_len, state.case.n.neck_d, state.case.n.neck_angle)
    present = sum(v is not None for v in vals)
    if present == len(vals):
        return True
    if present == 0:
        return False
    return None


def access_blocked(state: GuidelineState) -> Verdict:
    """入路是否**明确阻断**腔内修复。

    只有「双侧」才阻断：单侧闭塞或迂曲时对侧仍可建立入路，EVAR 依然可行——
    这正是规格把「入路可行」与「器械可用」分开写的原因。
    """
    occlusion = token(state.case.n.access_occlusion)
    tortuosity = token(state.case.n.access_tortuosity)
    if occlusion == "双侧闭塞" or tortuosity == "双侧严重迂曲":
        return True
    if occlusion is None or tortuosity is None:
        return None
    if is_spec_uncertain(occlusion) or is_spec_uncertain(tortuosity):
        return None
    return False


def aaa_diameter(state: GuidelineState) -> float | None:
    """病例的主动脉最大径（``cl.aaa_diam``，工具B 影像量化值 / B6 回落报告值）。

    **不是** ``c.maxd``：后者是 CTA 报告文字里读到的数字，只用于交叉校验
    （不一致 → ``Q.CONFLICT`` → ``G09``）。修复阈值只认本函数读的这个值。
    """
    value = state.case.cl.aaa_diam
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


#: 男性/女性的修复阈值（``D1.1.R05`` 原文：男 ≥55 mm、女 ≥50 mm）。
#:
#: 放在公共层而不是 ``D1.1`` 里，是因为 ``D5.3.R09`` 也要用同一个阈值判
#: 「低于常规修复阈值但有明显炎症症状」。两处各写一遍 55/50，改一处就会分叉，
#: 而这类分叉不会报错——只会让同一病例在两个节点得到相反的阈值结论。
THRESHOLD_MALE_MM = 55.0
THRESHOLD_FEMALE_MM = 50.0

#: 髂动脉瘤择期修复阈值（ESVS 2024 **Rec135** 原文：≥40 mm 可考虑择期修复）。
#:
#: 与上面两个 55/50 并列但**不是同一套口径**：55/50 判的是肾下腹主动脉，
#: 本常量判的是髂动脉，且不受性别影响。规格 ``D7.1`` 另有一句硬约束：
#: 「修复阈值**不能用于定义有无**髂动脉瘤」——所以本常量只出现在 ``D7.2`` 的
#: 分流判据里，``_iliac_confirmed`` 一个字都不读它。
ILIAC_REPAIR_MM = 40.0


def sex_threshold(state: GuidelineState) -> tuple[str, float] | None:
    """当前病例的性别与对应修复阈值；性别不可用时返回 ``None``。"""
    sex = token(state.case.p.sex)
    if sex == "男":
        return "男", THRESHOLD_MALE_MM
    if sex == "女":
        return "女", THRESHOLD_FEMALE_MM
    return None


def threshold_usable(state: GuidelineState) -> Verdict:
    """直径能否用于性别阈值判断 —— 三值，不是布尔。

    返回：

        ``True``   方法已知且为超声标准测量（规格定义的阈值基准）
        ``False``  方法已知为 CTA 外径（**明确不可**直接套用超声阈值）
        ``None``   方法缺失或取值不确定 —— 不判，但不等于「不能判」

    为什么 ``False`` 与 ``None`` 要分开：前者是「查过了，方法不匹配」，后者是
    「还没查」。临床上前者要改走专科确认，后者只要补一个字段——处理方式不同。
    """
    source = state.node_field("D1.SEX_THRESHOLD_SOURCE")
    if source is not None and not is_spec_uncertain(source):
        if source == "超声测量匹配":
            return True
        if source == "CTA需转换或专科确认":
            return False
        return None

    method = token(state.node_field("D1.DIAM_METHOD"))
    if method is None or is_spec_uncertain(method):
        return None
    # 规格的阈值基准是超声 AP 内-内/外-外；其余方法一律按「需专科确认」处理。
    return "超声" in str(method)


def at_repair_threshold(state: GuidelineState) -> Verdict:
    """直径是否达到性别修复阈值 —— 三值。

    与 ``D1.1`` 的阈值分层前置（``_threshold_ready``）分开：那个还带「特殊形态
    已排除」这一前置（``D1.1`` 的分流需要它），本函数不带——能走到术式比较说明
    形态已处理过，再判一次会把「形态字段未填」的病例挡在修复评估之外。

    在 ``D5.3.R09`` 里同样只用它判「低于常规修复阈值」这个限定词，**不用它产出
    修复指征**：修复指征归 ``D1.1``，两处只共用口径，不共用结论。
    """
    sex = sex_threshold(state)
    diam = aaa_diameter(state)
    if sex is None or diam is None or threshold_usable(state) is not True:
        return None
    return diam >= sex[1]


#: ``S.GI_BLEED`` 里属于「活动性出血」的表现 —— 直接用路由层那一份，
#: 两处各写一张表迟早会漏掉「其他出血表现」。
_GI_BLEED_SIGNS: tuple[str, ...] = GI_BLEED_SIGNS

#: ``S.LIMB`` 里属于「急性肢体缺血」的两档（``疼痛``/``苍白``/``发凉``是早期表现，
#: 单独出现不构成「肢体濒危」；运动障碍与无力才是必须立刻处理的信号）。
_LIMB_THREATENED: tuple[str, ...] = ("运动障碍", "无力")


def active_gi_bleed(state: GuidelineState) -> Verdict:
    """活动性消化道出血 —— 状态优先、明细兜底，与判定层同一口径。

    这几个病种规则的「允许值」列写的是 ``S.GI_BLEED``（状态五态）；
    ``S.GI_BLEED_TYPE`` 是抽取层的补充记录，只在状态未填时兜底。
    两者矛盾（状态=否、明细却有呕血）时不替任何一方作主，返回 UNKNOWN。
    """
    st = token(state.case.s.gi_bleed)
    detail = has(state.case.s.gi_bleed_type, *_GI_BLEED_SIGNS)
    if st == "是":
        return True
    if st == "否":
        return None if detail is True else False
    if st is not None and is_spec_uncertain(st):
        return None
    return detail


def limb_ischemia_present(state: GuidelineState) -> Verdict:
    """本次是否有急性肢体缺血表现（``S.LIMB`` 状态优先、体征明细兜底）。"""
    st = token(state.case.s.limb)
    detail = has(state.case.s.limb_signs, *LIMB_ISCHEMIA_SIGNS)
    if st == "是":
        return True
    if st == "否":
        return None if detail is True else False
    if st is not None and is_spec_uncertain(st):
        return None
    return detail


def acute_limb_ischemia(state: GuidelineState) -> Verdict:
    """肢体**濒危**（运动障碍 / 无力）—— 术后病例的 P0 触发项之一。

    比 :func:`limb_ischemia_present` 窄：只有「已威胁肢体存活」的体征才升级到 P0，
    早期表现（疼痛/苍白/发凉）走 ``S.LIMB`` 状态那条路。
    """
    return has(state.case.s.limb_signs, *_LIMB_THREATENED)


def sbp_below_shock(state: GuidelineState) -> Verdict:
    """收缩压低于休克阈值。

    复用判定层的 :data:`~src.rules.router.SBP_SHOCK`，**不在这里写 80**：同一阈值
    两处维护，改一处就分叉（``G03`` 的休克判据用的是同一个常量）。
    """
    sbp = state.case.v.sbp
    if sbp is None or isinstance(sbp, bool):
        return None
    return float(sbp) < SBP_SHOCK


def unstable(state: GuidelineState) -> Verdict:
    """血流动力学不稳定 —— 与 ``G03`` 同一定义（低血压或新发意识下降）。"""
    return or_(sbp_below_shock(state), eq(token(state.case.v.conscious_change), "新发下降"))


def rupture_evidence(state: GuidelineState) -> Verdict:
    """破裂/疑似破裂的可靠当前证据，或重大循环危险。

    三处规格问的是同一件事，措辞不同：
        ``D4.1.R03``「如伴活动性出血、休克或**新破裂证据**」
        ``D6.1.R01``「**新出现急症危险**」
        ``D7.1.R01``「**破裂/疑似破裂或重大循环危险**」

    合成一个函数是因为它们读的字段完全一致（``CL.AAA_RUPTURE`` /
    ``C.EXTRAV`` / ``C.HEMATOMA`` / ``V.SBP`` / ``V.CONSCIOUS_CHANGE``）。
    各病种各写一份的话，改一处字段口径就会在另一个病种里留下旧的判断。

    只认「明确」级影像征象：``C.EXTRAV`` / ``C.HEMATOMA`` 的可疑值走 UNKNOWN，
    由调用方按自己的缺失策略处理（``D4.1`` 有出血时仍会转急症，
    ``D7.1`` 则交给 ``R90`` 报缺口）。
    """
    return or_(
        eq(token(state.case.cl.aaa_rupture), "明确", "疑似"),
        eq(token(state.case.c.extrav), "明确"),
        eq(token(state.case.c.hematoma), "明确"),
        unstable(state),
    )


def malperfusion(state: GuidelineState) -> Verdict:
    """器官/肢体灌注障碍（``C.MALPERFUSION`` 多选）—— 模型字段优先，通道补位。

    空列表返回 ``None`` 而不是 ``False``：``has()`` 对空列表返回「明确不含」，
    但「一条都没记录」不等于「查过、没有」。
    """
    values = state.case.c.malperfusion
    if values is None:
        raw = state.node_field("C.MALPERFUSION")
        if raw is None:
            return None
        values = [raw] if isinstance(raw, str) else list(raw)
    values = list(values)
    if not values:
        return None
    return has(values, "肾", "肠系膜", "下肢", "其他器官")


def sac_growth(state: GuidelineState) -> float | None:
    """瘤囊直径变化 mm（``N.SAC_GROWTH``；模型约定「不可比 → ``None``」）。"""
    value = state.case.n.sac_growth
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def sac_growing(state: GuidelineState) -> Verdict:
    """瘤囊是否较基线/随访最小值增长 ≥10 mm（``Rec107`` 的阈值）。

    阈值取判定层的 :data:`~src.rules.router.SAC_GROWTH_MM`（``G08`` 的瘤囊增长
    判据用同一个常量），不在本层另写 10。
    """
    value = sac_growth(state)
    if value is None:
        return None
    return value >= SAC_GROWTH_MM


def cta_evaluable(state: GuidelineState) -> Verdict:
    """本次 CTA 对当前节点任务的可评价性（``N.CTA_STATUS``）。

    模型里 ``n.cta_status`` 只有四态（完整/部分/缺失/冲突），规格还有第五态
    「不可评价」——它落在 ``None`` 上：没有记录就是判不了，与「部分可评价」一样
    都是 UNKNOWN。**不新增字段**：``cta_status`` 已存在，缺的只是那一档。
    """
    status = token(state.case.n.cta_status)
    if status == "完整":
        return True
    if status == "缺失":
        return False
    return None


def branch_involved(state: GuidelineState) -> Verdict:
    """近端瘤颈是否累及重要分支（肾 / SMA / 腹腔干）或已累及胸腹主动脉。

    「可疑」也算：三个术式节点（``D1.3`` / ``D2.3`` / ``D3.2``）都靠它决定
    「标准腔内修复还算不算标准」。把可疑当未受累，会把病人推进标准 EVAR 路径，
    而实际可能需要 FEVAR——方向错了要靠术中发现，代价比多做一个 CTA 高得多。
    """
    return or_(
        eq(token(state.case.n.renal_r), "明确受累", "可疑"),
        eq(token(state.case.n.renal_l), "明确受累", "可疑"),
        eq(token(state.case.n.sma), "明确受累", "可疑"),
        eq(token(state.case.n.ca), "明确受累", "可疑"),
        eq(token(state.case.n.thoracic), "是"),
    )


@dataclass(frozen=True, slots=True)
class DisplayRule:
    """一条**展示性**规则 —— 求值并留痕，但不参与出口竞争。

    为什么需要这一类：:func:`evaluate_node` 取第一条命中即返回，适合「互斥分流」
    （``D1.1`` 的达阈值 / 未达阈值）。但有些规则**本就不互斥**——``D1.3`` 的
    ``R01``（EVAR 候选）与 ``R02``（OSR 候选）规格要求两列并列输出，若放进槽里，
    先命中的那一列会遮蔽另一列：主建议变成「OSR 候选」，而 EVAR 其实也可行。
    这类规则只留痕、不抢出口，由节点在 :func:`evaluate_node` 之后补进 ``entries``。
    """

    rule_id: str
    source_row: str
    statement: str
    judge: Callable[[GuidelineState], Verdict]


def display_entries(
    node_id: str, state: GuidelineState, rules: Sequence[DisplayRule]
) -> tuple[tuple[GuidelineEntry, ...], dict[str, Verdict]]:
    """求值展示性规则，返回（条目, 判定表）。

    返回判定表而不只返回条目：调用方要用这些判定去推导术式候选状态，
    再从同一次求值里取 ``rule_id`` 回溯——重算一遍会让「留痕的判定」与
    「推导用的判定」有机会分叉。
    """
    entries: list[GuidelineEntry] = []
    verdicts: dict[str, Verdict] = {}
    for rule in rules:
        verdict = rule.judge(state)
        verdicts[rule.rule_id] = verdict
        entries.append(
            GuidelineEntry(
                node_id=node_id,
                rule_id=rule.rule_id,
                source_row=rule.source_row,
                statement=rule.statement,
                verdict=verdict,
            )
        )
    return tuple(entries), verdicts
