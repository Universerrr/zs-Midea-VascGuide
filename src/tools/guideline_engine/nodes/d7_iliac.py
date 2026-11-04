"""D7 · 髂动脉瘤（``D7.1`` → ``D7.2`` → ``D7.3``）。

规格来源：``VascGuide_V3.6.2.xlsx`` 的 ``D7.1`` / ``D7.2`` / ``D7.3`` 三个 sheet，
共 24 条规则（7 + 9 + 8），全部实现。

## 三个节点各管什么

``D7.1`` 髂动脉瘤分诊
    回答「这到底是不是髂动脉瘤、是不是急症、要不要同时走别的病种」。四条出口：
    破裂/重大循环危险 → ``G05``；假性/感染性/术后吻合口旁 → ``D4``/``D5``/``D6``；
    已确认 → ``D7.2``；判不了 → 人工复核。

``D7.2`` 直径与监测/修复指征
    回答「这个直径是继续监测、还是进修复评估」。40 mm 是唯一的择期修复入口
    （``Rec135``），但**症状与复杂病因不受它约束**（``R06``）。监测间隔按
    ``Rec134`` 的三档给出（20–24 / 25–29 / ≥30 mm）。

``D7.3`` 术式候选
    回答「腔内、髂分支器械、还是开放/杂交」。三条硬约束：着陆区与入路要可核验；
    内髂血流要尽可能保留至少一侧；**不选品牌/SKU**（``R01`` 原话「本节点不选品牌」）。

## 七处必须先讲清的结构决定

**① ``CL.ILIAC_SITE`` 在模型里是「侧别 + 血管段」两个独立取值，不是一个复合值。**
    规格写 ``左CIA/右CIA/左IIA/…``（一个格子一个复合值），模型是
    ``list[Literal["左侧","右侧","CIA","IIA","EIA","疑似","无"]]``——侧别与段各占
    一个元素，靠列表共存表达「左侧 CIA」。所以判断「有没有髂动脉瘤」必须
    **同时**看到侧别与血管段：只有「左侧」不知道哪一段，只有「CIA」不知道哪一侧。
    照规格写 ``eq(x, "左CIA")`` 恒为 ``False``——不是「判不出」，是「永远判成没有」。

**② 修复阈值不参与「有没有」的判断（规格 ``D7.1`` F 列原话）。**
    ``_iliac_confirmed`` 只看 ``CL.ILIAC_SITE``，一个字都不读直径。反过来
    ``D7.2`` 的 40 mm 分流只看 ``CL.ILIAC_DIAM``，不看 ``CL.ILIAC_SITE``。
    两者混用会让「直径 38 mm 的 CIA 扩张」被判成「不是髂动脉瘤→无需随访」。

**③ ``CL.ILIAC_DIAM`` 是「段 → mm」的字典，取**最大段**做阈值判断。**
    规格要求左右 CIA/IIA/EIA 逐段测量，模型压成一个 ``dict[str, float]``，键是
    自由字符串（``"左侧CIA"`` / ``"右IIA"`` / ``"左侧"``）。判阈值取全段最大值
    （``Rec135`` 按最粗处定指征），键只用于回报「哪一段决定阈值」，**不参与判定**
    ——按键名做正则匹配会把「左髂总」这类写法静默漏掉。

**④ ``D7.1.R02`` 与 ``R04`` 做成展示性规则，不进出口竞争。**
    ``R02``（合并 AAA）的规格动作是「保留 D7 标签，并同时进入相应 AAA 病种；
    D7 继续评估」——它**不改变本节点的去向**。``R04``（症状/压迫）同理：它说的是
    「不直接套用无症状 40 mm 阈值」，而「套不套阈值」这件事由 ``D7.2.R06`` 判。
    放进 :func:`evaluate_node` 的槽里，``R02`` 会先命中并遮蔽 ``R05``（假性/感染性/
    术后），``R04`` 会遮蔽 ``R03``——两条真正的分流规则从此永不执行。

**⑤ ``R05`` 要去三个节点，而 ``next_node`` 是单值。**
    规格写「并行 D4/D5/D6」。本实现按病因取**最匹配的一个**作为本轮跳转目标
    （感染 → ``D4``、术后吻合口旁 → ``D6``、其余假性/病因不明 → ``D5``），
    其余两条写进缺口。为什么不用 ``D6.1.R03`` 那种「中转节点」办法：D7 没有
    可当中转的节点，硬加一个等于往 ``path`` 里塞规格没有的节点。

**⑥ 监测间隔是**合成为一句话**，不是六条规则各自出结论。**
    ``R01``（20–39 mm 三档间隔）与 ``R07``（<20 mm 不套既定间隔）都做成展示性
    规则，最终计划由 :func:`_surveillance_plan` 按「直径 + 生命预期 + 监测获益 +
    患者偏好」合成，写进 ``terminal``。理由与 ``D6.1`` 的随访计划一致：那些是
    并列的**计划要素**，不是互斥出口；放进出口竞争只会让报告只剩「每 3 年」而
    丢掉「寿命预期有限」这个限定。

**⑦ ``D7.3`` 的三列候选自造名称，不与 ``EVAR``/``OSR`` 合并。**
    ``GuidelineState.candidates`` 按术式名去重、后者覆盖前者，而 D7 排在 D1 之后。
    若这里也叫 ``EVAR``，髂动脉的结论会**覆盖掉** D1.3 的腹主动脉结论——两者是
    不同解剖靶区，不构成替代关系。故用 ``髂动脉腔内修复`` / ``髂分支器械（IBE/IBD）``
    / ``开放/杂交髂动脉重建`` 三个独立名。这与 D4 的取舍方向相反，原因也相反：
    D4 的感染让 D1 的标准 EVAR 失效，D7 的髂动脉病变不让腹主动脉结论失效。
"""

from __future__ import annotations

from dataclasses import replace

from ....rules.router import Verdict, and_, eq, has, not_, or_, token
from ..state import Candidate, CandidateStatus, GuidelineState, Link, NodeFn, NodeOutcome
from .common import (
    ILIAC_REPAIR_MM,
    DisplayRule,
    RuleSlot,
    acute_limb_ischemia,
    cta_evaluable,
    display_entries,
    evaluate_node,
    infection_typed,
    is_spec_uncertain,
    missing_fields,
    multi_field,
    rupture_evidence,
    spec_token,
)

__all__ = ["NODES", "d7_1", "d7_2", "d7_3"]

#: ``CL.ILIAC_SITE`` 里的侧别取值。
_ILIAC_SIDES: tuple[str, ...] = ("左侧", "右侧")

#: ``CL.ILIAC_SITE`` 里的血管段取值（对应规格的 CIA / IIA / EIA）。
_ILIAC_SEGMENTS: tuple[str, ...] = ("CIA", "IIA", "EIA")

#: ``D7.SYMPTOM_COMPRESSION`` 里属于「有症状」的取值。
#:
#: 规格把「盆腔/腹股沟疼痛」写成一个格子，模型层没有这个字段（走通道），
#: 所以不按字面逐项列举，而是「非『无』且非不确定词」即阳性——这样规格将来
#: 把「盆腔疼痛」拆成两项也不会漏判。
_SYMPTOM_NEGATIVE = "无"

#: ``D7.SYMPTOM_COMPRESSION`` 里必须回流急诊的两档（``D7.1.R04`` / ``D7.2.R06``）。
_SYMPTOM_EMERGENT: tuple[str, ...] = ("急性肢体缺血", "栓塞")

#: ``D7.ANEURYSM_TYPE`` 里属于「非退变性」的三档 —— 必须并行 D4/D5/D6。
_TYPE_PATHOLOGIC: tuple[str, ...] = ("假性", "感染性", "术后吻合口旁")

#: ``D7.PELVIC_PLAN`` 里表示「已有明确内髂保留/重建方案」的三档。
_PELVIC_DEFINITE: tuple[str, ...] = ("分支器械", "旁路重建", "其他保留方案")

#: ``D7.IIA_PRESERVATION`` 里表示「尚未形成方案」的一档。
_PRESERVATION_PENDING = "未定"


# ---------------------------------------------------------------------------
# 判据原语
# ---------------------------------------------------------------------------
def _site(state: GuidelineState) -> list[object] | None:
    """``CL.ILIAC_SITE`` 的侧别/血管段列表 —— 模型优先，通道补位。

    为什么不只用 ``node_field``：该字段在 ``VascCase`` 里**已经存在**
    （``cl.iliac_site``），而 ``node_inputs`` 通道是给模型没有的病种域字段用的。
    两处都读，是为了让「抽取层写进模型」与「评估时人工补录」都能生效；
    模型优先符合「病例事实 > 现场补录」的优先级。
    """
    values = state.case.cl.iliac_site
    if values is not None:
        return list(values)
    return multi_field(state, "CL.ILIAC_SITE")


def _iliac_confirmed(state: GuidelineState) -> Verdict:
    """髂动脉瘤是否**已确认存在**（侧别 + 血管段都能读到）。

    返回：
        ``True``   侧别与段同时出现（``["左侧","CIA"]``）
        ``False``  明确写了「无」
        ``None``   只有一半（只有侧别 / 只有段）、或含不确定词

    **不读直径**：规格 ``D7.1`` F 列明文「修复阈值不能用于定义有无髂动脉瘤」。
    也用 ``not_`` 反转「明确无」与「判不了」——两者在 ``R90`` 里的处置不同。
    """
    sites = _site(state)
    if sites is None:
        return None
    tokens = [token(value) for value in sites]
    if "无" in tokens:
        return False
    if not tokens:
        return None
    has_side = any(value in _ILIAC_SIDES for value in tokens)
    has_segment = any(value in _ILIAC_SEGMENTS for value in tokens)
    if has_side and has_segment:
        return True
    # 只有一半（只有侧别 / 只有段）也是判不了：``["左侧"]`` 说不出是哪一段，
    # ``["CIA"]`` 说不出是哪一侧。这里返回 ``False`` 会把「信息不全」当成
    # 「明确没有髂动脉瘤」，而两者在 R90 里的处置正相反。
    return None


def _diameters(state: GuidelineState) -> dict[str, float]:
    """``CL.ILIAC_DIAM`` 归一成 ``{段标签: mm}``；取不到返回空字典。"""
    values = state.case.cl.iliac_diam
    if values:
        return {
            str(key): float(value)
            for key, value in values.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
    raw = state.node_field("CL.ILIAC_DIAM")
    if isinstance(raw, dict):
        return {
            str(key): float(value)
            for key, value in raw.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return {"未标注段": float(raw)}
    return {}


def _iliac_diameter(state: GuidelineState) -> tuple[str, float] | None:
    """各段最大外径 —— 返回（决定阈值的段, mm）；没有可用数值返回 ``None``。

    取最大值而不是按段名挑：``Rec135`` 的指征按**最粗处**定，而字典键是自由
    字符串，按名匹配会把「左髂总」这类写法静默漏掉。键只用于回报「哪一段决定
    了阈值」，让医生能回头核对该段测量是否可信。
    """
    values = _diameters(state)
    if not values:
        return None
    label, value = max(values.items(), key=lambda item: item[1])
    return label, value


def _at_repair_threshold(state: GuidelineState) -> Verdict:
    """最大髂动脉径是否达到择期修复阈值（``Rec135`` 的 40 mm）—— 三值。"""
    diameter = _iliac_diameter(state)
    if diameter is None:
        return None
    return diameter[1] >= ILIAC_REPAIR_MM


def _symptom_items(state: GuidelineState) -> list[object] | None:
    return multi_field(state, "D7.SYMPTOM_COMPRESSION")


def _compression_symptom_present(state: GuidelineState) -> Verdict:
    """髂动脉瘤相关症状/压迫是否存在 —— 三值。"""
    items = _symptom_items(state)
    if not items:
        return None
    tokens = [token(value) for value in items]
    if any(value != _SYMPTOM_NEGATIVE and not is_spec_uncertain(value) for value in tokens):
        return True
    if any(is_spec_uncertain(value) for value in tokens):
        return None
    return False


def _symptom_recorded(state: GuidelineState) -> bool:
    """症状**是否已评价**（有没有明确结论都算）—— 只用于 ``R90`` 的阻断判断。"""
    items = _symptom_items(state)
    if not items:
        return False
    return not any(is_spec_uncertain(token(value)) for value in items)


def _emergent_symptom(state: GuidelineState) -> Verdict:
    """症状里是否含「急性肢体缺血 / 栓塞」—— 触发回流 ``G03`` 的那两档。

    两处都读：``D7.SYMPTOM_COMPRESSION`` 是髂动脉病种自己的症状记录，
    ``S.LIMB`` 是查体层的肢体缺血表现。前者可能漏记（病种字段没有抽取来源），
    后者可能只记到「发凉」这种早期表现——任一处阳性就够（急症宁可多报一次）。
    """
    return or_(has(_symptom_items(state), *_SYMPTOM_EMERGENT), acute_limb_ischemia(state))


def _types(state: GuidelineState) -> list[object] | None:
    return multi_field(state, "D7.ANEURYSM_TYPE")


def _pathologic_type(state: GuidelineState) -> Verdict:
    """病变性质是否属「假性 / 感染性 / 术后吻合口旁」—— 三值。

    规格 ``D7.1.R05`` 的字段清单里还有 ``CL.PSEUDO`` / ``CL.INFECTION`` /
    ``N.PREV_REPAIR`` 三个模型字段：``ANEURYSM_TYPE`` 是本次评估的分类结论，
    后三者是它的证据来源。任一处阳性即成立（``or_`` 的三值语义：有 ``True``
    就是 ``True``，全 ``None`` 才 ``None``）。
    """
    return or_(
        has(_types(state), *_TYPE_PATHOLOGIC),
        eq(token(state.case.cl.pseudo), "明确", "疑似"),
        _infection_present(state),
        _postop_anastomotic(state),
    )


def _infection_present(state: GuidelineState) -> Verdict:
    """髂动脉病变是否伴感染 —— 判据本体在 ``common.infection_typed``。

    本节点只是调用点。原先在这里另写一份 ``cl.infection`` 的取值表，口径比
    D4 窄一格：医生在 ``N.INFECTION_CTX`` 写「高度怀疑」、分型字段留空或写
    「无证据」的病例，D4 与 D6 都按感染处理，D7 却当退变性病变给出解剖候选。
    """
    return infection_typed(state)


def _postop_anastomotic(state: GuidelineState) -> Verdict:
    """术后吻合口旁髂动脉病变。

    只看 ``D7.ANEURYSM_TYPE`` 的「术后吻合口旁」：有修复史**不等于**吻合口旁瘤
    （``N.PREV_REPAIR`` 阳性也可能是原位复发或对侧新发）。有修复史但类型没记录时
    返回 ``None`` 而不是 ``True``——那是「判不了」，不是「就是」。
    """
    by_type = has(_types(state), "术后吻合口旁")
    if by_type is True:
        return True
    repair = eq(token(state.case.n.prev_repair), "EVAR", "OSR", "其他")
    if repair is True and by_type is None:
        return None
    return by_type


def _aaa_present(state: GuidelineState) -> Verdict:
    """**当前**是否并存未修复的腹主动脉瘤（``D7.1.R02`` 的判据）。

    ``D.DX_CONTEXT`` 的「既往已修复」不算：规格 ``CL.AAA_REPORTED`` 的 F 列明写
    「既往已修复 AAA **不等于**当前存在未修复 AAA」。把已修复病例也判成并存，
    会让 D1（无症状 AAA）再跑一遍并给出「按直径监测」——而那个瘤已经不在了。
    """
    reported = token(state.case.cl.aaa_reported)
    if reported in ("确认", "疑似"):
        return True
    if reported == "否定":
        return False
    return eq(token(state.case.d.dx_context), "确认", "疑似")


def _rupture_or_unstable(state: GuidelineState) -> Verdict:
    """破裂/疑似破裂或重大循环危险（``D7.1.R01`` 的判据）。

    判据本体在 :func:`common.rupture_evidence` —— ``D4.1.R03`` / ``D6.1.R01``
    问的是同一件事，三处共用一份，避免字段口径分叉。
    """
    return rupture_evidence(state)


def _rupture_unresolved(state: GuidelineState) -> bool:
    """破裂状态是否判不了（缺失 / 无法评价 / 证据冲突）。"""
    raw = token(state.case.cl.aaa_rupture)
    return raw is None or is_spec_uncertain(raw)


def _growth_comparable(state: GuidelineState) -> Verdict:
    """增长率是否**可比** —— ``D7.2.R05`` 的判据。

    规格要求「侧别/血管段/测量方法/影像质量不一致 → 不计算确定增长率」。
    模型没有「既往测量方法」字段，所以只认两处可核实的信号：评估者已在
    ``D7.GROWTH_RATE`` 标了「不可比」，或本次测量方式是「其他」（非常规方法，
    无法保证与历史可比）。返回 ``True`` 表示**可以算**，``False`` 表示不可比。
    """
    raw = spec_token(state.node_field("D7.GROWTH_RATE"))
    if raw == "不可比":
        return False
    method = spec_token(state.node_field("D7.DIAM_METHOD"))
    if method == "其他":
        return False
    if raw is None or method is None:
        return None
    return True


def _method_recorded(state: GuidelineState) -> bool:
    """``D7.DIAM_METHOD`` 是否已记录 —— ``D7.2.R04`` 的复测触发器。"""
    return spec_token(state.node_field("D7.DIAM_METHOD")) is not None


def _seal_ok(state: GuidelineState) -> Verdict:
    """近远端着陆区是否可用（``D7.TARGET_SEAL``）。"""
    value = spec_token(state.node_field("D7.TARGET_SEAL"))
    if value is None:
        return None
    return value == "可用"


def _seal_blocked(state: GuidelineState) -> Verdict:
    """着陆区是否**明确不足或已被病变累及**（腔内不适配的证据）。"""
    value = spec_token(state.node_field("D7.TARGET_SEAL"))
    if value is None:
        return None
    return value in ("不足", "病变累及")


def _access_ok(state: GuidelineState) -> Verdict:
    """髂股入路是否可用于腔内 —— 只有**双侧闭塞**才算阻断（与 ``common.access_blocked`` 同口径）。"""
    value = token(state.case.n.access_occlusion)
    if value is None or is_spec_uncertain(value):
        return None
    return value != "双侧闭塞"


def _pelvic_protected(state: GuidelineState) -> Verdict:
    """盆腔灌注是否有可靠保留/重建方案 —— 三值。

    「未确定 / 未定」返回 ``None`` 而不是 ``False``：那是**还没决定**，不是
    「决定不保」。两者在候选档位上差一档——前者是「条件性候选 + 暂停最终批准」
    （``R03`` 的职责），后者会让腔内修复直接进「排除」，把还能做的病例说死。
    """
    plan = spec_token(state.node_field("D7.PELVIC_PLAN"))
    keep = spec_token(state.node_field("D7.IIA_PRESERVATION"))
    if plan in _PELVIC_DEFINITE:
        return True
    if plan == "不适用":
        return True
    if keep is not None and keep != _PRESERVATION_PENDING:
        return True
    return None


def _ifu_ok(state: GuidelineState) -> Verdict:
    """髂分支器械 IFU 是否**已核验符合** —— 三值。

    ``D7.IBE_IFU`` 是「地区 + 型号 + IFU 版本 + 结论」的复合串，所以按包含关系读，
    且必须**先判「不符合」再判「符合」**：``"不符合"`` 里含 ``"符合"``，顺序颠倒会
    把每一个明确不符合的器械判成符合。
    """
    value = spec_token(state.node_field("D7.IBE_IFU"))
    if value is None:
        return None
    text = str(value)
    if "未核验" in text or "不可评价" in text:
        return None
    if "不符合" in text:
        return False
    if "符合" in text:
        return True
    return None


def _iia_status(state: GuidelineState, side: str) -> object:
    """一侧内髂动脉的通畅状态（``side`` 取 ``"left"`` / ``"right"``）。"""
    return token(getattr(state.case.n, f"iia_{side}"))


def _iia_at_risk(state: GuidelineState) -> Verdict:
    """双侧内髂血流是否同时处于「将被牺牲」的境地 —— ``D7.3.R03`` 的判据。

    只认两个可核实的信号：影像明确写了「双侧」髂动脉瘤，或两侧内髂都**不是通畅**。
    「单侧通畅 + 双侧病变」不在此列：那时保一侧就够，具体保哪侧由
    ``D7.PELVIC_PLAN`` 与术者规划回答——规格把这件事写进了 ``R03`` 的
    「唯一通畅侧可能被覆盖」，而那是规划问题，不是影像结论。
    """
    if token(state.case.n.iliac_bilat) == "双侧":
        return True
    left = _iia_status(state, "left")
    right = _iia_status(state, "right")
    if left is None or right is None or is_spec_uncertain(left) or is_spec_uncertain(right):
        return None
    return bool(left != "通畅" and right != "通畅")


def _iia_unresolved(state: GuidelineState) -> bool:
    """双侧内髂状态是否都判不了（``D7.3.R90`` 的第二个阻断项）。"""
    both = [_iia_status(state, side) for side in ("left", "right")]
    return all(value is None or is_spec_uncertain(value) for value in both)


# ---------------------------------------------------------------------------
# D7.1 髂动脉瘤分诊
# ---------------------------------------------------------------------------
def _d71_r01(state: GuidelineState) -> Verdict:
    """P0-A：破裂/疑似破裂或重大循环危险 → 回 ``G03``/``G05``。

    术后背景**不降低**急症优先级（与 ``D6.1.R01`` 同一条理由）。判据只看
    「可靠当前证据」：破裂状态、外溢/血肿的明确影像征象、休克或新发意识下降。
    """
    return _rupture_or_unstable(state)


def _d71_r02(state: GuidelineState) -> Verdict:
    """P1（展示性）：合并 AAA → 保留 D7 标签并同时进入 AAA 病种，D7 继续评估。

    故意不进出口竞争：规格给它的动作是「**并行**」，不是「改道」。多标签本身
    由 ``G08.R01``–``R07`` 产生，``engine._walk`` 会依次跑完 D1/D2/D3 与 D7，
    所以本节点不需要做任何跳转。做成展示性规则的价值在于留痕：报告里能读出
    「这是髂动脉瘤合并 AAA 的病例」，而不是只看到两条互不相关的病种结论。
    """
    return _aaa_present(state)


def _d71_r03(state: GuidelineState) -> Verdict:
    """P3：部位/侧别/直径三件套齐备且无急症 → ``D7.2``。

    与 ``R91`` 的分野是**直径**：本条是「资料完整」的路径，``R91`` 接住
    「已确认但还没测直径」的病例。为什么不把后者也塞进 ``R90``：``R90`` 是
    数据阻断（转人工复核），而没测直径只需要补一次超声——那是 ``D7.2`` 自己的
    ``R90`` 与 ``R04`` 要回答的问题，不该在分诊台就把人挡回去。
    """
    confirmed = _iliac_confirmed(state)
    if confirmed is not True:
        return False if confirmed is False else None
    if _rupture_or_unstable(state) is True:
        return False
    return _iliac_diameter(state) is not None


def _d71_r04(state: GuidelineState) -> Verdict:
    """P1（展示性）：有症状/压迫 → 不直接套用无症状 40 mm 阈值。

    与 ``R02`` 同一条理由：它回答的是「阈值还成不成立」，不是「去哪」。真正
    的处置由 ``D7.2.R06``（P0-A）执行——判据里带「未达 40 mm」的前提，所以
    达阈值的病例仍由 ``R02`` 走正常的修复评估路径。
    """
    return _compression_symptom_present(state)


def _d71_r05(state: GuidelineState) -> Verdict:
    """P1：假性/感染性/术后吻合口旁 → 并行 ``D4``/``D5``/``D6``，不按普通阈值独立决策。"""
    return _pathologic_type(state)


#: 并发病种 → 它的入口节点。**必须写节点键**（``D4.1``）而不是病种名（``D4``）：
#: ``engine._walk`` 拿 ``next_node`` 去查 ``NODES``，查不到就当成「节点未实现」，
#: 报告里会出现「待进入 D4（该节点尚未实现）」这种假缺口。
_COMORBID_ENTRY: dict[str, str] = {"D4": "D4.1", "D5": "D5.1", "D6": "D6.1"}


def _d71_target(state: GuidelineState) -> str:
    """``R05`` 本轮先跳哪个**节点** —— 按病因取最匹配者，其余写进缺口。

    单值 ``next_node`` 的已知限制（见模块文档第 ⑤ 条）。排序依据是「哪个病种
    决定治疗主线」：感染先控感染源（``D4``），吻合口旁先查结构与感染（``D6``），
    其余假性按特殊病变评估（``D5``）。
    """
    if _infection_present(state) is True:
        return _COMORBID_ENTRY["D4"]
    if _postop_anastomotic(state) is True:
        return _COMORBID_ENTRY["D6"]
    return _COMORBID_ENTRY["D5"]


def _d71_r90(state: GuidelineState) -> Verdict:
    """P2：髂动脉瘤存在与否、或破裂状态判不了 → 不按阴性放行。

    与规格字面的差别：F 列把 ``CL.ILIAC_DIAM`` / ``D7.SYMPTOM_COMPRESSION`` /
    ``D7.ANEURYSM_TYPE`` 全列进阻断字段。照字面实现，任何还没做完整评估的病例
    都会停在人工复核——而 ``D7.1`` 的职责恰恰是「先分诊，再补评估」。故本节点
    只阻断两件真正让分诊失去依据的事：**不知道是不是髂动脉瘤**、
    **不知道有没有破裂**。其余缺失留给下游节点按各自的 ``R90`` 处理。
    """
    confirmed = _iliac_confirmed(state)
    if confirmed is False:
        return False
    if confirmed is None:
        return True
    return _rupture_unresolved(state)


def _d71_r91(state: GuidelineState) -> Verdict:
    """P3：已确认且无急症 → ``D7.2``（直径缺失也放行，由 ``D7.2.R90`` 拦）。

    这是 ``D7.1`` 的结构性出口：``R90`` 已把「判不了」的病例挡掉，能走到这里的
    只剩「确认存在 + 无急症」。它比 ``R03`` 宽一档，正是规格写给 ``R91`` 的
    「未命中前置高优先级规则后的明确出口」。
    """
    return and_(_iliac_confirmed(state), not_(_rupture_or_unstable(state)))


_D71_FIELDS: tuple[tuple[str, str], ...] = (
    ("D7.SYMPTOM_COMPRESSION", "本次症状/压迫表现（盆腔或腹股沟疼痛、输尿管梗阻、栓塞等）"),
    ("D7.ANEURYSM_TYPE", "病变性质（退变性/假性/感染性/PAU 或夹层相关/术后吻合口旁）"),
    ("D7.DIAM_METHOD", "髂动脉直径测量方法（超声/CTA 正交/其他）"),
)


def _d71_gaps(state: GuidelineState) -> tuple[str, ...]:
    """``D7.1`` 缺什么 —— 分诊三件套 + 并行病种的提示。"""
    gaps = list(missing_fields(state, _D71_FIELDS))
    if _iliac_diameter(state) is None:
        gaps.append("CL.ILIAC_DIAM（各侧各段最大外径）：未记录，D7.2 无法判断修复指征与监测间隔")
    if _aaa_present(state) is True:
        gaps.append(
            "合并腹主动脉瘤：D1/D2/D3 病种并行评估（本节点只判髂动脉段，腹主动脉的修复指征归 D1.1）"
        )
    if _pathologic_type(state) is True:
        target = _d71_target(state)
        others = [node for node in _COMORBID_ENTRY.values() if node != target]
        gaps.append(
            f"假性/感染性/术后吻合口旁病变：本轮先进入 {target}，"
            f"{'、'.join(others)} 亦需并行评估（不按普通退变性髂动脉瘤阈值决定）"
        )
    if (
        _postop_anastomotic(state) is None
        and eq(token(state.case.n.prev_repair), "EVAR", "OSR", "其他") is True
    ):
        gaps.append("N.PREV_REPAIR 阳性但 D7.ANEURYSM_TYPE 未记录：需确认是否为吻合口旁病变")
    return tuple(dict.fromkeys(gaps))


_D71_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D7.1.R01",
        priority="P0-A",
        source_row="D7.1!10",
        statement="破裂/疑似破裂或重大循环危险 → 回 G03/G05，不等待常规直径阈值",
        judge=_d71_r01,
        exit_kind="escalate",
        exit_value="G05",
        next_nodes=("G03", "G05"),
        link=Link.EITHER,
    ),
    RuleSlot(
        rule_id="D7.1.R05",
        priority="P1",
        source_row="D7.1!16",
        statement="假性/感染性/术后吻合口旁髂动脉病变 → 并行 D4/D5/D6，不按普通孤立髂动脉瘤阈值独立决策",
        judge=_d71_r05,
        exit_kind="next",
        # 声明里写的是**默认**目的地（感染优先），真值由 ``_d71_exit`` 按病因
        # 改写。必须写节点键而不是病种名：``exit_value`` 会直接进
        # ``GuidelineEntry.next_node``，写 ``"D4"`` 时若 ``_d71_exit`` 没兜住，
        # 报告里就会出现「待进入 D4（该节点尚未实现）」这种假缺口。
        exit_value="D4.1",
    ),
    RuleSlot(
        rule_id="D7.1.R90",
        priority="P2",
        source_row="D7.1!9",
        statement="髂动脉瘤存在与否、或破裂状态不可评价 → 不判为无髂动脉瘤或低风险，转人工复核",
        judge=_d71_r90,
        exit_kind="terminal",
        exit_value="人工复核（髂动脉瘤部位/侧别或破裂状态待核查）",
    ),
    RuleSlot(
        rule_id="D7.1.R03",
        priority="P3",
        source_row="D7.1!12",
        statement="部位、侧别与直径均已确认且无急症 → D7.2 判监测或修复指征",
        judge=_d71_r03,
        exit_kind="next",
        exit_value="D7.2",
    ),
    RuleSlot(
        rule_id="D7.1.R91",
        priority="P3",
        source_row="D7.1!8",
        statement="髂动脉瘤确认且无急症 → D7.2（合并 AAA 可多标签并行）；破裂/急性缺血 → G03",
        judge=_d71_r91,
        exit_kind="next",
        exit_value="D7.2",
    ),
)

_D71_DISPLAY_RULES: tuple[DisplayRule, ...] = (
    DisplayRule(
        rule_id="D7.1.R02",
        source_row="D7.1!11",
        statement="合并腹主动脉瘤 → 保留 D7 标签并同时进入相应 AAA 病种；破裂/症状性 AAA 时急症路径优先",
        judge=_d71_r02,
    ),
    DisplayRule(
        rule_id="D7.1.R04",
        source_row="D7.1!15",
        statement="有相关疼痛、尿路梗阻、静脉/神经压迫、栓塞或缺血 → 不直接套用无症状 40 mm 阈值，"
        "由医生评估症状归因与修复时机（急性缺血 → G03）",
        judge=_d71_r04,
    ),
)


def d7_1(state: GuidelineState) -> NodeOutcome:
    """``D7.1``：急症回 G05；假性/感染/术后并行 D4/D5/D6；其余进 D7.2。"""
    outcome = evaluate_node("D7.1", state, _D71_SLOTS)
    extra_entries, extra_verdicts = display_entries("D7.1", state, _D71_DISPLAY_RULES)
    outcome = replace(
        outcome,
        verdicts={**outcome.verdicts, **extra_verdicts},
        entries=(*outcome.entries, *extra_entries),
        gaps=_d71_gaps(state),
    )

    # ``R05`` 的三个目的地收成一个：``next_node`` 只放一个真节点，
    # 留痕里的 ``next_node`` 同步改掉——否则报告说「去 D4.1」而实际去了 D5.1。
    if outcome.verdicts.get("D7.1.R05") is True:
        outcome = _d71_exit(state, outcome)
    return outcome


def _d71_exit(state: GuidelineState, outcome: NodeOutcome) -> NodeOutcome:
    """``R05`` 命中后的出口 —— 分「该病种本轮已评估」与「尚未评估」两种。

    为什么要分：``G08`` 允许多标签，感染性/术后病例通常**已经**从 ``D4``/``D6``
    标签先跑过一遍。这时再跳进去只会被 ``_walk`` 的 ``seen`` 拦掉，``path`` 停在
    ``D7.1``，读者看到的是一条断掉的流程。已评估过就继续 D7 主干，把「并行」
    这件事交给缺口清单交代——那条链的结论已经在报告里了。
    """
    target = _d71_target(state)
    visited = {o.node_id for o in state.visited}

    if target in visited:
        return replace(
            outcome,
            next_node="D7.2",
            entries=tuple(
                replace(entry, next_node="D7.2") if entry.rule_id == "D7.1.R05" else entry
                for entry in outcome.entries
            ),
        )

    other = "、".join(node for node in _COMORBID_ENTRY.values() if node != target)
    skipped = (
        f"D7.2 与 D7.3 本轮未执行：按 D7.1.R05 先进入 {target}"
        f"（假性/感染性/术后吻合口旁不按普通退变性阈值评估）；{other} 亦需并行评估"
    )
    gaps = (*outcome.gaps, skipped)
    return replace(
        outcome,
        next_node=target,
        gaps=gaps,
        entries=tuple(
            replace(entry, next_node=target) if entry.rule_id == "D7.1.R05" else entry
            for entry in outcome.entries
        ),
    )


# ---------------------------------------------------------------------------
# D7.2 直径与监测 / 修复指征
# ---------------------------------------------------------------------------
def _below_iliac_repair_threshold(state: GuidelineState) -> Verdict:
    """最大髂动脉径是否**低于**择期修复阈值 —— 三值。"""
    diameter = _iliac_diameter(state)
    if diameter is None:
        return None
    return diameter[1] < ILIAC_REPAIR_MM


def _d72_r01(state: GuidelineState) -> Verdict:
    """P3（展示性）：20–39 mm 按 ESVS 三档间隔随访.

    ``Rec134`` 原文：20–24 mm 每 3 年、25–29 mm 每 2 年、≥30 mm 每年超声监测。
    做成展示性规则而不出口，是因为「间隔」是最终计划的一个**参数**，不是目的地；
    真正的计划由 :func:`_surveillance_plan` 合成（见模块文档第 ⑥ 条）。
    """
    diameter = _iliac_diameter(state)
    if diameter is None:
        return None
    return 20.0 <= diameter[1] < ILIAC_REPAIR_MM


def _d72_r02(state: GuidelineState) -> Verdict:
    """P1：直径 ≥40 mm → ``D7.3`` 术式候选（``Rec135``）。

    「达到阈值是修复评估入口，**不自动等于手术批准**」（规格 F 列原话）——
    所以出口是 ``D7.3`` 而不是「建议手术」。
    """
    return _at_repair_threshold(state)


def _d72_r03(state: GuidelineState) -> Verdict:
    """P1：低于阈值且无症状、无复杂病因 → 进入影像监测。"""
    return and_(
        _below_iliac_repair_threshold(state),
        not_(_compression_symptom_present(state)),
        not_(_pathologic_type(state)),
        # 不知道「怎么量的」就不能给间隔：那是 R04 的「复测」要解决的问题。
        # 不加这一项，R03（P1）会先命中并把方法缺失的病例直接送进监测计划。
        _method_recorded(state),
    )


def _d72_r04(state: GuidelineState) -> Verdict:
    """P3：测量方式未记录 → 先复测，不自动归入低风险监测。

    与 ``R90`` 的分工：``R90`` 拦的是「连直径都没有」，本条拦的是「有数值但
    不知道是怎么量的」。后者能算出一个数，却无法回答「同一段、同一方法比较」
    这个前提，所以只能给「复测」而不是给间隔。
    """
    if _method_recorded(state):
        return False
    # 增长已标「不可比」时交给 R05（P1，优先级更高）——这里只需要不命中。
    return _growth_comparable(state) is not False


def _d72_r05(state: GuidelineState) -> Verdict:
    """P1：增长不可比（侧别/段/方法/影像质量不一致）→ 不算确定增长率，先复测。

    :func:`_growth_comparable` 回答的是「能不能算」，本条要的是「不能算才命中」，
    所以必须**取反**——直接返回它会让每一个可比的病例都被打回复测。
    """
    comparable = _growth_comparable(state)
    if comparable is None:
        return None
    return not comparable


def _d72_r06(state: GuidelineState) -> Verdict:
    """P0-A：**未达 40 mm** 但有症状/压迫或复杂病因 → 不自动进入普通监测。

    判据带「低于阈值」这个前提（规格 F 列原文「未达 40 mm 但有症状…」）。
    不带的后果：所有达阈值的病例都会先命中本条（P0-A 优先级最高），
    ``R02`` 从此永不执行，报告上写的是「未达 40 mm 但有症状」——与事实相反。
    """
    return and_(
        _below_iliac_repair_threshold(state),
        or_(_compression_symptom_present(state), _pathologic_type(state)),
    )


def _d72_r07(state: GuidelineState) -> Verdict:
    """P3（展示性）：<20 mm → 不套用既定间隔（``Rec134`` 未定义该区间）。"""
    diameter = _iliac_diameter(state)
    if diameter is None:
        return None
    return diameter[1] < 20.0


def _d72_r90(state: GuidelineState) -> Verdict:
    """P2：侧别/段、直径、破裂或症状状态判不了 → 不自动归入监测。

    ``D7.DIAM_METHOD`` **不在**此列：方法缺失有 ``R04`` 的「复测」接住，那是
    一次补测就能解决的事，不该升级成人工复核。``D7.GROWTH_RATE`` 同理——
    增长不可比由 ``R05`` 处理。
    """
    if _iliac_confirmed(state) is not True:
        return True
    if _iliac_diameter(state) is None:
        return True
    if _rupture_unresolved(state):
        return True
    return not _symptom_recorded(state)


def _d72_r91(state: GuidelineState) -> Verdict:
    """P3：结构兜底 —— 常规出口恒真，接住 ``R03`` 判不出方向的一切组合。

    与 ``D5.2.R91`` 同一理由：新增字段后仍要有人接住。恒真不等于「放行无症状
    病例」——``R90``（P2）已经先把资料不全的挡掉了，能走到这里说明资料齐备，
    只是「无症状」「无复杂病因」这两个否定判据里有一项判不了（``None``）。
    此时给监测建议是安全的：监测的代价是一次超声，而漏掉随访的代价是破裂。
    """
    return True


def _surveillance_plan(state: GuidelineState) -> str:
    """按「直径 + 生命预期 + 监测获益 + 患者偏好」合成一句随访计划。

    这是 ``D7.2`` 真正的交付物（与 ``D6.1`` 的随访计划同构）。措辞必须带三个
    限定：**哪一段**决定了阈值、**同一方法比较**的前提、以及**为什么**可能改变间隔。
    """
    diameter = _iliac_diameter(state)
    if diameter is None:
        return (
            "监测：髂动脉直径未测 —— 先完成各侧各段（CIA/IIA/EIA）中心线正交平面测量，"
            "再按 ESVS Rec134 的区间确定随访间隔"
        )

    label, value = diameter
    if value < 20.0:
        band = "低于 Rec134 监测区间下界（20 mm）"
        interval = "不套用既定间隔，由医生结合是否已达髂动脉瘤定义与测量可信度决定"
    elif value < 25.0:
        band, interval = "20–24 mm", "每 3 年超声随访"
    elif value < 30.0:
        band, interval = "25–29 mm", "每 2 年超声随访"
    else:
        band, interval = "30–39 mm", "每年超声随访"

    parts = [
        f"监测：{label} {value:.0f} mm（{band}）→ {interval}",
        "须同一血管段、同一测量方式比较",
    ]

    life = spec_token(state.node_field("D7.LIFE_EXPECTANCY"))
    if life == "有限":
        parts.append("寿命预期有限，避免为无症状小瘤安排过多影像")
    elif life is None:
        parts.append("生命预期未评估（影响间隔是否值得）")

    eligibility = spec_token(state.node_field("D7.SURVEILLANCE_ELIGIBILITY"))
    if eligibility == "有限":
        parts.append("继续监测的实际获益有限，应与患者讨论是否转入修复评估")
    elif eligibility is None:
        parts.append("监测适宜性未评价")

    # 「未讨论」在 SPEC_UNCERTAIN_TOKENS 里，走 spec_token 会被折成 None——
    # 那样这一档就永远不命中，而它恰恰是必须写进计划的一句话（规格：
    # 「未讨论 → 共同决策」）。故此处读原始值。
    preference = token(state.node_field("D7.PREFERENCE"))
    if preference == "倾向修复":
        parts.append("患者倾向修复，进入共同决策")
    elif preference == "未讨论":
        parts.append("患者偏好尚未讨论，需共同决策")

    return "；".join(parts)


_D72_FIELDS: tuple[tuple[str, str], ...] = (
    ("D7.DIAM_METHOD", "髂动脉直径测量方法（超声/CTA 正交/其他，历史比较须同法同段）"),
    ("D7.LIFE_EXPECTANCY", "预期寿命与未来修复适宜性（较长/有限）"),
    ("D7.PREFERENCE", "患者偏好（倾向监测/倾向修复/未讨论）"),
    ("D7.GROWTH_RATE", "同方法测量的年化增长（数值/不可比）"),
    ("D7.SURVEILLANCE_ELIGIBILITY", "继续监测是否有实际获益（有潜在修复获益/有限）"),
)


def _d72_gaps(state: GuidelineState) -> tuple[str, ...]:
    """``D7.2`` 缺什么 —— 测量五件套 + 阈值/症状的说明性缺口。"""
    gaps = list(missing_fields(state, _D72_FIELDS))
    diameter = _iliac_diameter(state)
    if diameter is not None and len(_diameters(state)) == 1:
        gaps.append(
            "CL.ILIAC_DIAM：只记录了 1 个血管段 —— 左右 CIA/IIA/EIA 应逐段测量，"
            "否则「最大径」可能取自非目标段"
        )
    if _at_repair_threshold(state) is True and _compression_symptom_present(state) is True:
        gaps.append(
            "有症状/压迫且已达 40 mm：修复指征同时来自症状与直径，症状归因须由医生确认（D7.1.R04）"
        )
    if _growth_comparable(state) is False:
        gaps.append("D7.GROWTH_RATE：本次与历史不可比，不得据此计算增长率（先复测）")
    return tuple(dict.fromkeys(gaps))


_D72_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D7.2.R06",
        priority="P0-A",
        source_row="D7.2!14",
        statement="未达 40 mm 但有症状/压迫、破裂/出血、感染或假性瘤 → 不自动进入普通监测，"
        "转紧急/专科评估（急性缺血 → G03，感染/假性 → D4/D5）",
        judge=_d72_r06,
        exit_kind="next",
        exit_value="D7.3",
    ),
    RuleSlot(
        rule_id="D7.2.R05",
        priority="P1",
        source_row="D7.2!13",
        statement="侧别/血管段/测量方法/影像质量不一致 → 不计算确定增长率，先复测或人工核对",
        judge=_d72_r05,
        exit_kind="terminal",
        exit_value="补充影像/复测：本次与历史测量不可比，不计算增长率",
    ),
    RuleSlot(
        rule_id="D7.2.R02",
        priority="P1",
        source_row="D7.2!8",
        statement="髂动脉最大径 ≥40 mm → D7.3 术式候选（达到阈值是修复评估入口，不自动等于手术批准）",
        judge=_d72_r02,
        exit_kind="next",
        exit_value="D7.3",
    ),
    RuleSlot(
        rule_id="D7.2.R03",
        priority="P1",
        source_row="D7.2!9",
        statement="低于 40 mm 且无症状/破裂、无复杂病因 → 影像监测（增长、症状或合并 AAA 需个体化复评）",
        judge=_d72_r03,
        exit_kind="terminal",
        exit_value="监测",
    ),
    RuleSlot(
        rule_id="D7.2.R90",
        priority="P2",
        source_row="D7.2!6",
        statement="侧别/血管段、直径、破裂或症状状态未知 → 不自动归入监测，先补测/复核",
        judge=_d72_r90,
        exit_kind="terminal",
        exit_value="人工复核（髂动脉侧别/段、直径或症状/破裂状态待补齐）",
    ),
    RuleSlot(
        rule_id="D7.2.R04",
        priority="P3",
        source_row="D7.2!10",
        statement="测量方法/侧别/血管段不明，或急症状态未评价 → 先补充信息，不自动归入低风险监测",
        judge=_d72_r04,
        exit_kind="terminal",
        exit_value="复测/临床复核：测量方式未记录，先按同法同段补测再定间隔",
    ),
    RuleSlot(
        rule_id="D7.2.R91",
        priority="P3",
        source_row="D7.2!5",
        statement="常规/阴性出口：无症状、无复杂病因且低于 40 mm → 按 ESVS 适用区间个体化监测",
        judge=_d72_r91,
        exit_kind="terminal",
        exit_value="监测",
    ),
)

_D72_DISPLAY_RULES: tuple[DisplayRule, ...] = (
    DisplayRule(
        rule_id="D7.2.R01",
        source_row="D7.2!7",
        statement="ESVS Rec134 监测区间：20–24 mm 每 3 年、25–29 mm 每 2 年、≥30 mm 每年超声，"
        "并结寿命预期、未来修复适宜性、合并主动脉扩张与患者偏好",
        judge=_d72_r01,
    ),
    DisplayRule(
        rule_id="D7.2.R07",
        source_row="D7.2!15",
        statement="<20 mm：Rec134 未给该区间间隔，不自动套用，结合是否达到动脉瘤定义与测量可信度个体化随访",
        judge=_d72_r07,
    ),
)


def d7_2(state: GuidelineState) -> NodeOutcome:
    """``D7.2``：≥40 mm 或症状/复杂病因 → ``D7.3``；否则给个体化监测计划。"""
    outcome = evaluate_node("D7.2", state, _D72_SLOTS)
    extra_entries, extra_verdicts = display_entries("D7.2", state, _D72_DISPLAY_RULES)
    outcome = replace(
        outcome,
        verdicts={**outcome.verdicts, **extra_verdicts},
        entries=(*outcome.entries, *extra_entries),
        gaps=_d72_gaps(state),
    )

    # 两个监测出口（``R03`` 完整路径 / ``R91`` 兜底路径）共用同一句计划：
    # 计划是「直径 + 生命预期 + 监测获益 + 偏好」的函数，与走哪条规则无关。
    if outcome.terminal == "监测":
        outcome = replace(outcome, terminal=_surveillance_plan(state))

    # 急性肢体缺血/栓塞要回流 ``G03``，但**不放弃** ``D7.3`` 的术式评估：
    # ``escalation`` 是并列信号（``engine._walk`` 只看 ``terminal``/``next_node``），
    # 所以这里给的是「同时」而不是「改道」。
    if outcome.verdicts.get("D7.2.R06") is True and _emergent_symptom(state) is True:
        outcome = replace(outcome, escalation="G03")
    return outcome


# ---------------------------------------------------------------------------
# D7.3 术式候选
# ---------------------------------------------------------------------------
#: 三列候选的术式名 —— **故意不叫 EVAR/OSR**（见模块文档第 ⑦ 条）。
_PROC_ENDOVASCULAR = "髂动脉腔内修复"
_PROC_BRANCH = "髂分支器械（IBE/IBD）"
_PROC_OPEN = "开放/杂交髂动脉重建"


def _d73_r01(state: GuidelineState) -> Verdict:
    """P3：符合 IFU 且着陆可靠、盆腔灌注可保护 → 列为腔内候选（**不选品牌**）。"""
    return and_(
        cta_evaluable(state),
        _seal_ok(state),
        _access_ok(state),
        _pelvic_protected(state),
    )


def _d73_r02(state: GuidelineState) -> Verdict:
    """P3：腔内解剖不适配 → 比较开放重建（**不自动证明开放可行**）。"""
    return and_(cta_evaluable(state), _seal_blocked(state))


def _d73_r03(state: GuidelineState) -> Verdict:
    """P1：盆腔灌注保护 —— 双侧内髂将同时被牺牲且无可靠方案时暂停最终批准。"""
    at_risk = _iia_at_risk(state)
    if at_risk is not True:
        return at_risk
    planned = _pelvic_protected(state)
    return planned is not True


def _d73_r04(state: GuidelineState) -> Verdict:
    """P2：CTA 不完整、关键解剖/分支/IFU 未核验 → 只输出候选草案，不批准最终术式。

    「CTA 部分可评价 / 冲突」也归这里。能看一部分不等于能定术式（该给草案），
    但也不等于什么都看不了（那是 ``R90`` 的「不输出候选」）。不接这一档的后果：
    ``R91`` 拿 ``cta_evaluable`` 的 ``None`` 当判据也返回 ``None``，本节点一条
    规则都不命中，落到全局兜底——报告上出现**通用「人工复核」文案 + 三条完整
    候选**，读者既看不出为什么被复核，也看不出候选是基于残缺影像给的。
    """
    if cta_evaluable(state) is not True:
        return True
    if _seal_ok(state) is None and _seal_blocked(state) is None:
        return True
    if _pelvic_protected(state) is None:
        return True
    return _ifu_ok(state) is None


def _d73_r06(state: GuidelineState) -> Verdict:
    """P3：具体型号有效地区 IFU 与解剖、着陆区、入路均核验符合 → IBE/IBD 候选。"""
    return and_(
        cta_evaluable(state),
        _ifu_ok(state),
        _seal_ok(state),
        _pelvic_protected(state),
        _access_ok(state),
    )


def _d73_r07(state: GuidelineState) -> Verdict:
    """P3：腔内不适配或需复杂盆腔灌注重建 → 比较开放/杂交方案。"""
    return and_(cta_evaluable(state), _iia_at_risk(state))


def _d73_r90(state: GuidelineState) -> Verdict:
    """P2：CTA 不可评价或双侧内髂状态都判不了 → 阻断候选输出。"""
    if cta_evaluable(state) is False:
        return True
    return _iia_unresolved(state)


def _d73_r91(state: GuidelineState) -> Verdict:
    """P3：结构兜底 —— 常规出口恒真，接住前面判不出方向的三值组合。

    与 ``D5.2.R91`` / ``D7.2.R91`` 同一写法：新增字段或新增三值分支后仍要有人
    接住。恒真不等于「什么都放行」——``R03``（P1）、``R90``/``R04``（P2）已经把
    盆腔风险、影像缺失、关键解剖未核验的挡在前面，能走到这里说明这三类都不是
    「是」，只是「不是」或「判不了」，此时给术式方向是安全的。

    改用 ``cta_evaluable(state)`` 当判据是错的：它在「部分/冲突」时返回 ``None``，
    兜底规则返 ``None`` 就不再是兜底。
    """
    return True


def _status_of(verdict: Verdict) -> CandidateStatus:
    """三值判定 → 候选档位：``True`` 可行 / ``None`` 条件性 / ``False`` 排除。

    「未评估」这一档不在这里出现：它留给「整块输入缺失」的调用方（``R90`` 命中
    或 `CTA 不可评价`），因为那种情况下连「条件性」都不该说——条件性意味着
    「只差最后一步核验」。
    """
    if verdict is True:
        return CandidateStatus.FEASIBLE
    if verdict is None:
        return CandidateStatus.CONDITIONAL
    return CandidateStatus.EXCLUDED


def _d73_candidates(state: GuidelineState, verdicts: dict[str, Verdict]) -> tuple[Candidate, ...]:
    """按四条展示性规则的三值判定推导三列候选。

    两处**整体降级**，都不是保守，而是事实：

    · 病变性质属假性/感染性/术后吻合口旁 → 可行降为「需 MDT 评估」。规格
      ``D7.1.R05`` 明写这类病变「**不按普通孤立髂动脉瘤阈值独立决定**」。
      不降档会让报告同时出现「髂动脉腔内修复：可行候选」与 D4 的「EVAR 仅作
      桥接」——两条相反结论并列，医生无法判断该信哪条。这与 D4 用
      ``EVAR``/``OSR`` 覆盖 D1 的取舍同源：病因变了，解剖适配就不再是主判据。
    """
    at_risk = _d73_r03(state) is True
    pathologic = _pathologic_type(state) is True

    notes: list[str] = []
    if at_risk:
        notes.append("内髂保留/重建方案未定")
    if pathologic:
        notes.append("病变性质为假性/感染性/术后吻合口旁，术式归 D4/D5/D6")
    prefix = "；".join(notes) + "，" if notes else ""

    endovascular = verdicts.get("D7.3.R01")
    branch = verdicts.get("D7.3.R06")
    open_recon = or_(verdicts.get("D7.3.R02"), verdicts.get("D7.3.R07"))

    endo_status = _status_of(endovascular)
    branch_status = _status_of(branch)
    if pathologic:
        # 非退变性病变：腔内与分支器械都只能交 MDT，不给「可行」也不给「排除」
        endo_status = CandidateStatus.MDT
        branch_status = CandidateStatus.MDT
    # ``at_risk`` 不再另设降级：``R01``/``R06`` 的判据本身就含 ``_pelvic_protected``，
    # 盆腔方案没定时它们不可能返回 ``True``——候选自然落在条件性或排除。
    # 再加一层「可行→条件性」是死代码，且会让「有方案但双侧受累」的病例被误降。

    candidates = [
        Candidate(
            procedure=_PROC_ENDOVASCULAR,
            status=endo_status,
            reason=prefix
            + (
                "着陆区可用、入路非双侧闭塞、盆腔灌注有保护方案"
                if endovascular is True
                else "着陆区/入路/盆腔灌注方案待核验"
                if endovascular is None
                else "着陆区不足或累及、入路双侧闭塞、或无盆腔灌注保护方案"
            ),
            rule_ids=("D7.3.R01",),
        ),
        Candidate(
            procedure=_PROC_BRANCH,
            status=branch_status,
            reason=prefix
            + (
                "具体型号地区 IFU 与解剖、着陆区、入路均已核验符合"
                if branch is True
                else "器械 IFU 未核验或解剖待确认"
                if branch is None
                else "IFU 不符合或解剖/入路不满足"
            ),
            rule_ids=("D7.3.R06",),
        ),
    ]

    if open_recon is True:
        candidates.append(
            Candidate(
                procedure=_PROC_OPEN,
                status=CandidateStatus.MDT,
                reason="腔内不适配或需复杂盆腔灌注重建：开放/杂交的解剖与风险须由血管团队比较"
                "（腔内不适配不自动证明开放可行）",
                rule_ids=("D7.3.R02", "D7.3.R07"),
            )
        )
    elif open_recon is None:
        candidates.append(
            Candidate(
                procedure=_PROC_OPEN,
                status=CandidateStatus.UNASSESSED,
                reason="开放/杂交的解剖需求与患者风险未评估",
                rule_ids=("D7.3.R02", "D7.3.R07"),
            )
        )
    else:
        candidates.append(
            Candidate(
                procedure=_PROC_OPEN,
                status=CandidateStatus.EXCLUDED,
                reason="未见开放/杂交指征（着陆区与盆腔灌注仍在腔内可处理范围）",
                rule_ids=("D7.3.R02", "D7.3.R07"),
            )
        )
    return tuple(candidates)


_D73_FIELDS: tuple[tuple[str, str], ...] = (
    ("D7.TARGET_SEAL", "近远端着陆区是否可用（长度/直径/钙化/分支位置按 IFU 核验）"),
    ("D7.PELVIC_PLAN", "盆腔灌注计划（分支器械/旁路重建/其他保留方案/不适用）"),
    ("D7.IIA_PRESERVATION", "至少保留一侧内髂血流的计划（左侧/右侧/双侧保留）"),
    ("D7.IBE_IFU", "髂分支器械的具体型号地区 IFU 核验结论"),
    ("D7.OPEN_RECON", "开放/杂交重建的解剖需求（吻合区/旁路/内髂重建）"),
)


def _d73_gaps(state: GuidelineState) -> tuple[str, ...]:
    """``D7.3`` 缺什么 —— 解剖五件套 + 盆腔灌注保护的硬约束提示。"""
    gaps = list(missing_fields(state, _D73_FIELDS))
    if _iia_at_risk(state) is True:
        gaps.append(
            "双侧内髂血流同时处于风险：必须先形成明确的内髂保留/重建方案（IBE/IBD、旁路或开放重建），"
            "否则不批准最终术式（D7.3.R03）"
        )
    if _iia_unresolved(state):
        gaps.append("N.IIA_LEFT / N.IIA_RIGHT：双侧内髂通畅状态均未记录，无法判断盆腔灌注风险")
    if cta_evaluable(state) is None:
        gaps.append(
            "N.CTA_STATUS：CTA 标注为部分可评价或结果冲突 —— 候选仅为草案，"
            "着陆区、入路与双侧内髂须以完整影像复核（D7.3.R04）"
        )
    if spec_token(state.node_field("D7.IBE_IFU")) is None:
        gaps.append("D7.IBE_IFU：分支器械 IFU 未核验 → IBE/IBD 只能列为条件性候选，不得称「适配」")
    if _pathologic_type(state) is True:
        gaps.append(
            "病变性质为假性/感染性/术后吻合口旁：术式候选已封顶在「需 MDT 评估」，"
            "不按退变性髂动脉瘤的解剖阈值决定（D7.1.R05）"
        )
    if _compression_symptom_present(state) is True:
        gaps.append(
            "有症状/压迫：修复指征不受 40 mm 阈值限制，症状归因与时机由医生确认（D7.1.R04）"
        )
    return tuple(dict.fromkeys(gaps))


_D73_SLOTS: tuple[RuleSlot, ...] = (
    RuleSlot(
        rule_id="D7.3.R03",
        priority="P1",
        source_row="D7.3!12",
        statement="规划应尽可能保留至少一侧内髂血流；双侧内髂受累/计划覆盖双侧/唯一通畅侧将被覆盖时，"
        "必须先明确内髂保留/重建方案，否则暂停最终术式批准并转复杂规划/MDT",
        judge=_d73_r03,
        exit_kind="terminal",
        exit_value="MDT：内髂保留/重建方案未定，暂停最终术式与器械批准",
    ),
    RuleSlot(
        rule_id="D7.3.R90",
        priority="P2",
        source_row="D7.3!9",
        statement="CTA 不可评价或双侧内髂状态判不了 → 不输出术式候选，转人工复核",
        judge=_d73_r90,
        exit_kind="terminal",
        exit_value="人工复核（CTA 可评价性或双侧内髂通畅状态待补齐）",
    ),
    RuleSlot(
        rule_id="D7.3.R04",
        priority="P2",
        source_row="D7.3!13",
        statement="着陆区、内髂血流计划或具体 IFU 未核验 → 只输出候选草案，不批准最终器械/术式方案",
        judge=_d73_r04,
        exit_kind="terminal",
        exit_value="候选草案：关键解剖或器械 IFU 未核验，需补充 CTA/IFU 与医生确认",
    ),
    RuleSlot(
        rule_id="D7.3.R91",
        priority="P3",
        source_row="D7.3!8",
        statement="常规出口：比较腔内分支器械、常规腔内、开放/杂交方案，尽可能保留至少一侧内髂血流",
        judge=_d73_r91,
        exit_kind="terminal",
        exit_value="术式方向已列出：腔内 / 髂分支器械 / 开放或杂交，由血管团队结合 IFU 与患者风险确认",
    ),
)

_D73_DISPLAY_RULES: tuple[DisplayRule, ...] = (
    DisplayRule(
        rule_id="D7.3.R01",
        source_row="D7.3!10",
        statement="符合具体器械 IFU 且能实现可靠着陆与盆腔灌注保护 → 列为腔内候选（本节点不选品牌/SKU）",
        judge=_d73_r01,
    ),
    DisplayRule(
        rule_id="D7.3.R02",
        source_row="D7.3!11",
        statement="腔内解剖不适配或开放更合适 → 按解剖、病变范围、患者风险与中心经验比较开放重建"
        "（腔内不适配不自动证明开放可行）",
        judge=_d73_r02,
    ),
    DisplayRule(
        rule_id="D7.3.R06",
        source_row="D7.3!17",
        statement="具体型号有效地区 IFU 与解剖、着陆区、入路均核验符合且内髂保留可实现 → 列为 IBE/IBD 候选",
        judge=_d73_r06,
    ),
    DisplayRule(
        rule_id="D7.3.R07",
        source_row="D7.3!18",
        statement="腔内不适配或需复杂盆腔灌注重建 → 比较开放/杂交方案、手术风险、中心能力与患者偏好",
        judge=_d73_r07,
    ),
)


def d7_3(state: GuidelineState) -> NodeOutcome:
    """``D7.3``：盆腔灌注保护优先；未核验只给草案；否则列出三类候选。"""
    outcome = evaluate_node("D7.3", state, _D73_SLOTS)
    extra_entries, extra_verdicts = display_entries("D7.3", state, _D73_DISPLAY_RULES)
    verdicts = {**outcome.verdicts, **extra_verdicts}

    # ``R90`` 命中时连草案都不给：CTA 不可评价意味着「着陆区/入路/内髂」三件事
    # 一件都没核过，此时三列候选全是猜的。候选空元组本身就是结论。
    candidates = () if verdicts.get("D7.3.R90") is True else _d73_candidates(state, verdicts)

    terminal = outcome.terminal
    # 18 mm 但有输尿管梗阻的病例，终点若只写「术式方向已列出」会被读成
    # 「按直径该修」。规格 D7.1.R04 要求把「症状改变指征」写进结论。
    # R90 的「人工复核」不加：那时结论本身还没形成。
    needs_symptom_note = (
        terminal is not None
        and verdicts.get("D7.3.R90") is not True
        and _compression_symptom_present(state) is True
    )
    if needs_symptom_note:
        terminal += "；修复指征来自症状/压迫，不依赖 40 mm 直径阈值（症状归因由医生确认）"

    return replace(
        outcome,
        terminal=terminal,
        verdicts=verdicts,
        entries=(*outcome.entries, *extra_entries),
        gaps=_d73_gaps(state),
        candidates=candidates,
    )


#: 本模块提供的节点（键与规格 sheet 名一致）。
NODES: dict[str, NodeFn] = {
    "D7.1": d7_1,
    "D7.2": d7_2,
    "D7.3": d7_3,
}

#: 已实现的规则 → 规格源行。
RULE_ROWS: dict[str, str] = {
    **{slot.rule_id: slot.source_row for slot in _D71_SLOTS},
    **{rule.rule_id: rule.source_row for rule in _D71_DISPLAY_RULES},
    **{slot.rule_id: slot.source_row for slot in _D72_SLOTS},
    **{rule.rule_id: rule.source_row for rule in _D72_DISPLAY_RULES},
    **{slot.rule_id: slot.source_row for slot in _D73_SLOTS},
    **{rule.rule_id: rule.source_row for rule in _D73_DISPLAY_RULES},
}
