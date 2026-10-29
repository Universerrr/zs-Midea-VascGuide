"""输出层节点 —— 把判定结果聚合为 :class:`FinalRecommendation`（决策产物）。

本层做五件事（全部**机械可验证**，无一处自由裁量）：
    ① ``trace → decision_path``：``RuleTrace`` → ``DecisionStep``，**含被否决项**
       （审计要求：不得只留命中项）。三值结果原样保留 —— ``None`` 是「未判定」，
       转成 ``False`` 会把「资料不足」伪装成「已排除」；
    ② ``ruleset_version``：直接取自 :class:`RouteResult` 的复合版本串；
    ③ ``input_snapshot_hash``：取自输入层算好的输入快照哈希；
    ④ ``evidence_chain``：三层可逐环核对 —— **结论 → 每条规则的实际判定与它读到的
       字段值 → ``VascCase.field_evidence`` 里的原文出处**。被否决的规则同样入链
       （不得只留命中项），否则「为什么排除其他分支」无从复核；
    ⑤ ``tool_findings``：各工具产物的要点摘要（见下节）。

## 为什么必须读 ``tool_outputs``

执行层把工具产物写进 ``state.tool_outputs``，但**工具产物不会自己走进结论**。
本层若不读它，工具算出的东西就只存在于状态与 checkpoint 里 —— 医生看不到、
证据链里也没有它，等于白算。故本层显式消费 ``tool_outputs``，转成
:class:`ToolFinding`（呈现条目）。

刻意**不把原始数值抄进结论**：``tool_outputs`` 是唯一真源，摘要只做渲染
（枚举取中文值、数值原样、缺失不补），抄一份就成两处真相。

与 ``evidence_chain`` 的分工：
    ``evidence_chain`` = **判定依据**（为什么这样路由）；
    ``tool_findings``  = **测量产物**（影像/评分/检索各算出了什么）。
    两者不互相复制 —— 同一条内容出现在两处，日后改一处就会不一致。

## 两个留白（等外部输入，非本层能自行决定）

**留白一（已部分落地）：主建议改为机械装配。**
    ``primary_recommendation`` 不再是占位符，而是把既有结论排成一句话
    （见 :func:`_build_primary_recommendation`）：:

        急诊模式 · 破裂／疑似破裂 AAA：EVAR 候选（IFU 待核验，术者确认）；待补 6 项关键资料

    **它仍不含任何临床主张**：只重排 ``route_result`` 与 ``tool_outputs`` 里
    已有的内容，本层不引入任何「如果 A 则建议 B」的分支。

    ``treatment_options`` 仍为空——方案清单的粒度是「具体术式（入路/器械/型号）」，
    工具A 目前只给到「EVAR 候选 / 评估 OSR」这一层，留空是如实反映粒度而非遗漏。

**留白二：LLM 输出转译。**
    ``translated_text`` 保持 ``None``（见 :data:`TRANSLATION_BLOCKED_REASON`）。
    该字段属于「LLM 只出现在两处」红线中的第二处（输出转译），但它的价值是
    **措辞润色**，前提是主建议的措辞已由临床确认；当前文案是机械拼接，
    还没有到"需要润色"的状态。

治理红线：
    本层是**聚合与转写**，不是判定。不得在此引入任何新的判定分支或阈值——
    任何「如果 A 则 B」的临床逻辑一旦出现在这里，就等于绕过 ``src/rules`` 另立了一处规则。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any, Final

from pydantic import BaseModel

from ...models.preop.case import VascCase
from ...models.preop.recommendation import (
    DecisionStep,
    EvidenceItem,
    FinalRecommendation,
    ToolFinding,
)
from ...rules.router import RULES, RouteKind, RouteResult, RuleTrace, _read_path
from ..execution_layer.registry import SPEC_BY_KEY
from ..state import PreopState

__all__ = [
    "PRIMARY_RECOMMENDATION_PLACEHOLDER",
    "TRANSLATION_BLOCKED_REASON",
    "TRANSLATION_IMPLEMENTED",
    "build_tool_findings",
    "output_node",
]

#: 单条摘要最多呈现多少个字段（防长模型把结论撑爆；超出部分只报总数）。
MAX_SUMMARY_FIELDS: Final[int] = 10

#: 摘要中跳过的字段：外键对结论读者无信息量（``case_id`` 已在结论顶层）。
SUMMARY_SKIPPED_FIELDS: Final[frozenset[str]] = frozenset({"case_id"})

#: 非模型产物（不该发生，但兜底）的摘要截断长度。
MAX_FALLBACK_SUMMARY_CHARS: Final[int] = 120

#: 三值判定 → 中文。``None`` 必须单独措辞：它是「未判定」，不是「不成立」。
VERDICT_TEXT: Final[Mapping[bool | None, str]] = {True: "成立", False: "不成立", None: "无法判定"}

#: 主建议的兜底文本 —— 仅在**既无模式也无工具结论**时出现。
#: 正常情况下主建议由 :func:`_build_primary_recommendation` 装配。
PRIMARY_RECOMMENDATION_PLACEHOLDER: str = (
    "〔无法给出结论〕本次运行既未进入任何路由模式，也没有病种流程产物。"
    "请检查路由层输出与工具执行记录，不要据本字段做任何临床判断。"
)

#: 病种标签 → 中文名。**逐字取自规格 G08**「D1无症状AAA；D2症状性未破裂AAA；
#: D3破裂/疑似破裂AAA；D4感染/相关瘘；D5 PAU/IMH/IAAD/囊状/假性/炎症性病变；
#: D6术后并发症；D7髂动脉瘤」，不另行命名。
DISEASE_LABELS: Final[dict[str, str]] = {
    "D1": "无症状 AAA",
    "D2": "症状性未破裂 AAA",
    "D3": "破裂／疑似破裂 AAA",
    "D4": "感染／相关瘘",
    "D5": "PAU／IMH／IAAD／囊状／假性／炎症性病变",
    "D6": "术后并发症",
    "D7": "髂动脉瘤",
}

#: LLM 输出转译是否已实现（当前为 ``False``）。
TRANSLATION_IMPLEMENTED: bool = False

#: 输出转译未实现的原因。
TRANSLATION_BLOCKED_REASON: str = (
    "LLM 输出转译未实现：转译只做措辞润色、不改结论，其输入应是**已确定的结论文本**。"
    "当前 primary_recommendation 是机械装配的（模式 · 病种 · 终点 · 待补数），"
    "措辞尚未经临床确认；在此之上做润色会掩盖「哪些话是系统拼的、哪些是模型写的」。"
)


def _render(value: Any) -> str:
    """把字段值渲染为人类可读字符串（枚举取中文值，列表/字典展开）。

    空容器要显式说「（空）」：``d.surg = `` 这种右值留白的写法在证据链里
    会被误读成「漏了」，而它其实是一个已求值但为空的字段。
    """
    if value is None:
        return "（未提及）"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (list, tuple)):
        return "、".join(_render(v) for v in value) or "（空）"
    if isinstance(value, dict):
        return "、".join(f"{key}={_render(item)}" for key, item in value.items()) or "（空）"
    return str(value)


def _render_scalar(value: Any) -> str:
    """标量渲染：枚举取中文值、布尔转「是/否」、其余原样输出。"""
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _field_label(name: str, description: str | None) -> str:
    """字段展示名：优先用 ``Field(description=...)``，缺失时退回字段名。

    复用 pydantic 的 description 而非另建一份中文标签表 —— 标签只存在一处，
    改字段说明时摘要自动跟上，不会两处漂移。
    """
    return (description or "").strip().rstrip("。；;") or name


def _summarize_guideline_report(product: Any) -> str | None:
    """工具A 的产物按「终点 → 路径 → 命中规则 → 缺口数」渲染。

    为什么用鸭子类型而不是 ``isinstance``：``output_layer`` 不该 import ``src.tools``
    ——工具层是可替换的，编排层只认产物形状。``path`` / ``terminal`` / ``entries``
    这三个属性同时出现的类型，目前只有 ``GuidelineReport``。

    不这么做的后果是本轮实测到的：整份报告被 ``repr()`` 截成一行，
    术式方向（``terminal``）和缺口全部看不见。
    """
    path = getattr(product, "path", None)
    entries = getattr(product, "entries", None)
    gaps = getattr(product, "gaps", None)
    if not isinstance(path, tuple) or not isinstance(entries, tuple) or not isinstance(gaps, tuple):
        return None

    parts = [
        f"终点 = {getattr(product, 'terminal', None) or '未给出'}",
        f"路径 = {' → '.join(path) or '未进入节点'}",
    ]
    hits = [
        entry.rule_id
        for entry in entries
        if getattr(entry, "verdict", None) is True and hasattr(entry, "rule_id")
    ]
    if hits:
        parts.append(f"命中 = {'、'.join(hits)}")
    if gaps:
        # 列前两条明细：只说「缺口 6 项」等于把待补资料藏起来，
        # 而 G05 明确要求输出「待补关键资料」。
        head = "；".join(gaps[:2])
        more = f" 等 {len(gaps)} 项" if len(gaps) > 2 else ""
        parts.append(f"缺口 = {head}{more}")
    else:
        parts.append("缺口 = 无")
    return "；".join(parts)


def _summarize_product(product: Any) -> str:
    """把一个工具产物机械渲染成「字段 = 值；…」摘要（不改数、不推断）。"""
    if product is None:
        return "（无产出）"
    guideline = _summarize_guideline_report(product)
    if guideline is not None:
        return guideline
    if not isinstance(product, BaseModel):
        text = repr(product)
        if len(text) <= MAX_FALLBACK_SUMMARY_CHARS:
            return text
        return text[:MAX_FALLBACK_SUMMARY_CHARS] + "…"

    parts: list[str] = []
    for name, info in type(product).model_fields.items():
        if name in SUMMARY_SKIPPED_FIELDS:
            continue
        value = getattr(product, name, None)
        # 只呈现标量：嵌套模型与列表（如中心线剖面）对结论读者是噪声。
        if value is None or not isinstance(value, (str, bool, int, float, Enum)):
            continue
        parts.append(f"{_field_label(name, info.description)} = {_render_scalar(value)}")

    if not parts:
        return "（无标量产出）"
    shown = parts[:MAX_SUMMARY_FIELDS]
    if len(parts) > MAX_SUMMARY_FIELDS:
        shown.append(f"等共 {len(parts)} 项")
    return "；".join(shown)


def build_tool_findings(tool_outputs: Mapping[str, Any]) -> list[ToolFinding]:
    """``tool_outputs`` → 呈现条目列表。

    按工具键**升序**呈现（而非执行顺序）：``final_recommendation`` 参与审计哈希，
    顺序一旦依赖字典插入顺序，调度顺序变化就会改变哈希。排序把这一层不确定性挡在外面。
    """
    findings: list[ToolFinding] = []
    for key in sorted(tool_outputs):
        spec = SPEC_BY_KEY.get(key)
        findings.append(
            ToolFinding(
                tool_key=key,
                label=spec.label if spec is not None else key,
                summary=_summarize_product(tool_outputs[key]),
            )
        )
    return findings


def _to_decision_steps(trace: Sequence[RuleTrace]) -> list[DecisionStep]:
    """``RuleTrace`` → ``DecisionStep``：**保留全部被否决步骤**，三值结果原样保留。

    映射是机械的（但不再 1:1，因为 ``RuleTrace`` 只有三格）：

    ============  =====================================
    DecisionStep   来源
    ============  =====================================
    order          轨迹中的位置（从 1 起）
    condition      ``RULES[rule_id].name``（规则名）
    result         ``verdict``（True / False / None 三值）
    rule_ref       ``rule_id``
    note           ``RULES[rule_id].source``（V3.1(2) 源单元格）
    ============  =====================================
    """
    steps: list[DecisionStep] = []
    for index, entry in enumerate(trace, start=1):
        spec = RULES.get(entry.rule_id)
        steps.append(
            DecisionStep(
                order=index,
                condition=spec.name if spec else entry.rule_id,
                result=entry.verdict,
                rule_ref=entry.rule_id,
                note=spec.source if spec else None,
            )
        )
    return steps


def _build_evidence(case: VascCase, result: RouteResult) -> list[EvidenceItem]:
    """构造证据链：**结论 → 规则 → 字段值 → 原文出处**，四环各自可独立核对。

    证据链回答的是「这个结论怎么推出来的」，不是「记录有没有被改过」（后者是
    ``audit_trail`` 的哈希链）。因此它必须做到 **每一环都能被第三个人复算**：

    ==========  ===========================================================
    环节        内容 / 来源
    ==========  ===========================================================
    ① 结论      ``result.reason`` + 命中规则 ``result.rule_id``
    ② 规则      ``result.trace`` 逐条（**含被否决项**），每条写明判定结果
                与它读到的字段实际值
    ③ 字段      字段清单取自 ``RULES[rule_id].fields`` —— 规则自己声明的输入，
                不在此处另立一份「这条规则读什么」的映射（那是第二处真源）
    ④ 原文      ``case.field_evidence[path].source_quote``，缺则留空
    ==========  ===========================================================

    ② 与 ④ 刻意分成两段而不是逐字段嵌套：同一字段可能被多条规则读
    （如 ``cl.aaa_rupture`` 同时被 ``G08.R01`` / ``G08.R03`` 读），
    若把原文挂在每条规则下会重复 N 次；单独成段则只出现一次。

    字段值一律经 :func:`_read_path` 现读 ``case``，不缓存在 ``RouteResult`` 里 ——
    ``case`` 本身就是判定时的输入快照，两处存同一事实只会漂移。
    """
    items: list[EvidenceItem] = []

    mode_text = result.mode.value if result.mode else result.next_node
    items.append(
        EvidenceItem(
            statement=f"路由结论：{mode_text}；依据：{result.reason}",
            rule_ref=result.rule_id,
        )
    )

    # ② 每条已求值规则一行：判定结果 + 它声明读取的字段实际值
    for entry in result.trace:
        spec = RULES.get(entry.rule_id)
        verdict = VERDICT_TEXT[entry.verdict]
        if spec is None:
            # 规则表里查不到（不应发生）——如实标注，不编造字段清单。
            items.append(
                EvidenceItem(
                    statement=f"{entry.rule_id} → {verdict}｜（规则未登记，无法列出读取字段）",
                    rule_ref=entry.rule_id,
                )
            )
            continue
        # 不设截断：证据链的价值就是完整可核对，「等共 N 项」会让复算断在半路。
        # 当前 54 条规则最多读 7 个字段（G03.R10 的 NEWS2 各项），一条一行仍可读。
        pairs = [f"{path} = {_render(_read_path(case, path))}" for path in spec.fields]
        detail = "；".join(pairs) if pairs else "（本规则不读模型字段）"
        items.append(
            EvidenceItem(
                statement=f"{entry.rule_id} {spec.name} → {verdict}｜{detail}",
                rule_ref=entry.rule_id,
            )
        )

    # ④ 原文出处：每个带出处的字段单独一条，附写入者（归责必需）
    for field_name in sorted(case.field_evidence):
        provenance = case.field_evidence[field_name]
        items.append(
            EvidenceItem(
                statement=(
                    f"字段 {field_name} = {_render(_read_path(case, field_name))}"
                    f"（写入者：{provenance.writer.value}）"
                ),
                source=provenance.source_quote,
            )
        )

    return items


def _build_primary_recommendation(result: RouteResult, tool_outputs: Mapping[str, Any]) -> str:
    """把已判出的结论排成**一句话** —— 只重排，不新增判断。

    构成：``<模式> · <病种>：<工具A 终点>；待补 N 项关键资料``

    三段的来源都是既有结构：``route_result.mode`` / ``class_output`` /
    ``tool_outputs['guideline_engine'].terminal``。本函数**不含任何**
    「如果 A 则建议 B」的分支——那属于判定层，写在这里就是第二处真源。

    为什么必须做这一步：``route_result`` 和 ``tool_outputs`` 都是结构化对象，
    医生打开 CLI 看到的是字段名和枚举值。把结论排成一句话是**呈现**，不是判定
    ——这与 ``_summarize_product`` 对数值摘要做的事性质相同。

    为什么 ``translated_text`` 仍是 ``None``：本函数产出的是**事实排列**，
    不是润色。LLM 转译要落在"已定稿的结论文本"上，而这里的措辞仍需临床确认。
    """
    if result.kind is RouteKind.REVIEW:
        return f"人工复核：{result.reason}"
    if result.kind is RouteKind.VERIFY:
        return f"资料核实：{result.reason}"

    head = result.mode.value if result.mode is not None else "未定模式"
    diseases = (
        "、".join(DISEASE_LABELS.get(tag, tag) for tag in result.class_output) or "病种未分类"
    )

    report = tool_outputs.get("guideline_engine")
    terminal = getattr(report, "terminal", None)
    gaps = getattr(report, "gaps", ()) or ()

    if not terminal:
        return f"{head} · {diseases}：病种流程未给出结论"
    tail = f"{terminal}；待补 {len(gaps)} 项关键资料" if gaps else str(terminal)
    return f"{head} · {diseases}：{tail}"


def _build_treatment_options(tool_outputs: Mapping[str, Any]) -> list[str]:
    """把工具A 的术式候选渲染成治疗方案清单。

    澄清一个容易搞错的粒度问题：本字段**不该**到「EVAR，经右股动脉，用 XX 型号」
    这一层。规格有 5 处明文禁止工具A 走到器械型号（``D1.3.R03``「不在本节点选
    具体型号」、``D1.3.R06``「不确定具体 SKU」、``D1.3.R90``「不批准具体术式或
    器械适配」、``D7.3.R01``「不选品牌/SKU」、``D7.3.R04``「不批准最终器械/术式
    方案」）——那属于手术规划，不在本系统范围内。

    规格要求的形态由 ``D1.3.R91`` 给出：「分别列出 EVAR/OSR 的**可行候选**、
    **条件性候选**和**排除原因**」。所以清单每项是「术式：状态（原因）」而不是
    一串术式名——后者会把「候选方向」伪装成「已定方案」。

    渲染交给候选对象自己的 ``render()``：本层不 import ``src.tools``（工具层可
    替换），也不自己拼串（措辞两处维护必然漂移）。
    """
    report = tool_outputs.get("guideline_engine")
    candidates = getattr(report, "candidates", ()) or ()
    return [
        candidate.render()
        for candidate in candidates
        if callable(getattr(candidate, "render", None))
    ]


def output_node(state: PreopState) -> dict[str, Any]:
    """输出层节点：聚合出 :class:`FinalRecommendation`。

    参数：
        state: 至少含 ``case``（已回填）、``route_result``、``input_snapshot_hash``；
            ``tool_outputs`` 可缺省（缺省即「本次没有任何工具产出」，``tool_findings`` 为空）。

    返回：
        含 ``final_recommendation`` 的状态增量。
    """
    case = state["case"]
    result = state["route_result"]

    recommendation = FinalRecommendation(
        case_id=case.case_id,
        route_mode=result.mode,
        # 呈现：把既有结论排成一句话（不新增判断，见 _build_primary_recommendation）。
        primary_recommendation=_build_primary_recommendation(
            result, state.get("tool_outputs") or {}
        ),
        # 治疗方案清单 = 工具A 的术式候选（EVAR/OSR 两列 × 候选状态）。
        # 不是「具体术式」：规格禁止工具A 走到器械型号，见 _build_treatment_options。
        treatment_options=_build_treatment_options(state.get("tool_outputs") or {}),
        confidence_level=result.confidence_level,
        manual_review_required=result.kind is RouteKind.REVIEW,
        evidence_chain=_build_evidence(case, result),
        decision_path=_to_decision_steps(result.trace),
        # 工具产物 → 呈现条目；未跑的工具不出现在这里（不编造「跑了但没结果」）。
        tool_findings=build_tool_findings(state.get("tool_outputs") or {}),
        ruleset_version=result.ruleset_version,
        input_snapshot_hash=state.get("input_snapshot_hash"),
        # 留白二：等主建议措辞经临床确认后再接 LLM 转译。
        translated_text=None,
    )

    return {"final_recommendation": recommendation}
