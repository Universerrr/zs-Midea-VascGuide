"""FinalRecommendation —— 术前决策的最终产物（聚合输出层）。

定位：
    依赖 VascCase + VascMeasurement + AnatomyScore + RiskAssessment，
    由决策树仲裁产出治疗方案 + 证据链。

审计要求（核心）：
    1. 必须记录**完整决策路径**（P1~P8 逐条求值结果，含被否决项）；
    2. 必须记录**规则版本号**（复合串：表1 版本 + 表2 版本）；
    3. 必须记录**输入快照哈希**，保证可复现。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..enums import RouteMode


class EvidenceItem(BaseModel):
    """一条证据：结论 + 出处 + 依据规则。"""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(..., description="证据结论（如「颈长 12mm < 15mm 阈值」）。")
    source: str | None = Field(default=None, description="证据出处（原文片段 / 影像 / 量表）。")
    rule_ref: str | None = Field(default=None, description="依据规则编号（如 O15 / R001）。")


class DecisionStep(BaseModel):
    """决策流水线上的一个步骤记录（含被否决项）。"""

    model_config = ConfigDict(extra="forbid")

    order: str = Field(..., description="判定顺序（P0a~P8）。")
    condition: str = Field(..., description="判定条件描述。")
    result: bool = Field(..., description="该步求值结果。")
    rule_ref: str | None = Field(default=None, description="依据覆盖规则编号。")
    note: str | None = Field(default=None, description="补充说明。")


class ToolFinding(BaseModel):
    """一个工具产物在最终结论中的**呈现条目**。

    为什么需要这一层，而不是把 ``tool_outputs`` 原样塞进结论：
        ``tool_outputs`` 的值是各工具的产物模型（``VascMeasurement`` /
        ``AnatomyScore`` …），属于**内部数据结构**。直接暴露会带来三个问题：
        ① 结论产物与工具实现耦合，换一个工具版本就改一次结论结构；
        ② 原始数值被抄进结论 —— 与 ``tool_outputs`` 形成**两处真相**，日后对不上；
        ③ 序列化白名单与哈希边界被外部类型的形状拖动。

    因此本条目只承载**呈现所需的最小信息**：谁产出的、叫什么、要点是什么。
    要点由输出层**机械渲染**（不改数、不推断、不下临床结论），原始数值仍以
    ``tool_outputs`` 为唯一真源，可随时回指。
    """

    model_config = ConfigDict(extra="forbid")

    tool_key: str = Field(..., description="工具键（如 imaging_quantifier）。")
    label: str = Field(..., description="工具中文名（如「工具B · 影像量化器」）。")
    summary: str = Field(
        ...,
        description="产物要点摘要（机械渲染的「字段 = 值」串，不含临床主张）。",
    )


class FinalRecommendation(BaseModel):
    """术前决策最终输出。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., description="关联病例标识。")

    # ---- 决策结论 ----
    route_mode: RouteMode = Field(..., description="最终路由模式。")
    primary_recommendation: str = Field(
        ..., description="主建议（如「进入急诊应急预案」/「择期修复评估」）。"
    )
    treatment_options: list[str] = Field(default_factory=list, description="可选治疗方案清单。")
    confidence: float = Field(..., ge=0.0, le=1.0, description="结论置信度。")
    manual_review_required: bool = Field(..., description="是否需人工复核。")

    # ---- 证据与审计 ----
    evidence_chain: list[EvidenceItem] = Field(default_factory=list, description="证据链。")
    decision_path: list[DecisionStep] = Field(
        default_factory=list, description="完整决策路径（含被否决分支）。"
    )

    # ---- 工具产物（呈现用；与 evidence_chain 刻意分开） ----
    tool_findings: list[ToolFinding] = Field(
        default_factory=list,
        description=(
            "各工具产物的要点摘要，按工具键升序；未运行/未实现的工具不出现。"
            "与 evidence_chain 分工：evidence_chain 是**判定依据**（为什么这样路由），"
            "本字段是**测量产物**（影像/评分/检索各算出了什么）。"
        ),
    )

    ruleset_version: str = Field(
        ...,
        description="规则版本号（复合串，如 'table1@v1.0+table2@v1.3'）。审计必需。",
    )
    input_snapshot_hash: str | None = Field(default=None, description="输入快照哈希（用于复现）。")
    translated_text: str | None = Field(
        default=None,
        description="输出转译文本（LLM 生成，供医生阅读；仅措辞润色，不改结论）。",
    )


__all__ = ["DecisionStep", "EvidenceItem", "FinalRecommendation", "ToolFinding"]
