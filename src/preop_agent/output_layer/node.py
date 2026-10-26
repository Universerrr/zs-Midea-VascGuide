"""输出层节点 —— 把判定结果聚合为 :class:`FinalRecommendation`（决策产物）。

本层做五件事（全部**机械可验证**，无一处自由裁量）：
    ① ``trace → decision_path``：``RuleTrace`` → ``DecisionStep``，**含被否决项**
       （审计要求：不得只留命中项）。三值结果原样保留 —— ``None`` 是「未判定」，
       转成 ``False`` 会把「资料不足」伪装成「已排除」；
    ② ``ruleset_version``：直接取自 :class:`RouteResult` 的复合版本串；
    ③ ``input_snapshot_hash``：取自输入层算好的输入快照哈希；
    ④ ``evidence_chain``：路由结论的依据 + ``VascCase.field_evidence`` 里每个
       带出处的字段（原告原文），使每条建议可回指到具体规则与原文；
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

## 明确的两个留白（等外部输入，非本层能自行决定）

**留白一：诊断与治疗方式建议。**
    ``primary_recommendation`` / ``treatment_options`` 目前是显式占位
    （见 :data:`PRIMARY_RECOMMENDATION_PLACEHOLDER`），**不含任何临床主张**。
    原因：设计表只管「路由」，管不到「治疗建议措辞」；三模式各自的标准处置路径
    属临床口径，必须由临床侧确认。**在口径确认前，宁可为空，不可编造。**

**留白二：LLM 输出转译。**
    ``translated_text`` 保持 ``None``（见 :data:`TRANSLATION_BLOCKED_REASON`）。
    该字段属于「LLM 只出现在两处」红线中的第二处（输出转译），但它的价值是
    **措辞润色**，前提是先有确定的结论文本可润色；留白一的文案未定，转译也就无处落脚。

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

#: 主建议占位（**不含任何临床主张**，等临床口径确认后替换）。
PRIMARY_RECOMMENDATION_PLACEHOLDER: str = (
    "〔待补临床口径〕诊断与治疗方式的主建议尚未定义：需先确认三种路由模式各自的"
    "标准处置路径，确认后替换本占位文本。在此之前本字段不作为临床依据。"
)

#: LLM 输出转译是否已实现（当前为 ``False``）。
TRANSLATION_IMPLEMENTED: bool = False

#: 输出转译未实现的原因。
TRANSLATION_BLOCKED_REASON: str = (
    "LLM 输出转译未实现：转译只做措辞润色、不改结论，其输入应是**已确定的结论文本**。"
    "当前 primary_recommendation / treatment_options 尚未定稿（见留白一），"
    "无内容可转译，贸然接入只会产生无依据的表述。"
)


def _render(value: Any) -> str:
    """把字段值渲染为人类可读字符串（枚举取中文值，列表串接）。"""
    if value is None:
        return "（未提及）"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (list, tuple)):
        return "、".join(_render(v) for v in value)
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


def _summarize_product(product: Any) -> str:
    """把一个工具产物机械渲染成「字段 = 值；…」摘要（不改数、不推断）。"""
    if product is None:
        return "（无产出）"
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
    """构造证据链：路由结论的依据 + 各带出处字段的原文。

    证据链的用途是「可追溯」——每条建议都要能回指到规则编号与原文片段。
    """
    items: list[EvidenceItem] = []

    mode_text = result.mode.value if result.mode else result.next_node
    items.append(
        EvidenceItem(
            statement=f"路由结论：{mode_text}；依据：{result.reason}",
            rule_ref=result.rule_id,
        )
    )

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
        # 留白一：等临床口径（见模块文档）。
        primary_recommendation=PRIMARY_RECOMMENDATION_PLACEHOLDER,
        treatment_options=[],
        confidence_level=result.confidence_level,
        manual_review_required=result.kind is RouteKind.REVIEW,
        evidence_chain=_build_evidence(case, result),
        decision_path=_to_decision_steps(result.trace),
        # 工具产物 → 呈现条目；未跑的工具不出现在这里（不编造「跑了但没结果」）。
        tool_findings=build_tool_findings(state.get("tool_outputs") or {}),
        ruleset_version=result.ruleset_version,
        input_snapshot_hash=state.get("input_snapshot_hash"),
        # 留白二：等留白一定稿后再接 LLM 转译。
        translated_text=None,
    )

    return {"final_recommendation": recommendation}
