"""FinalRecommendation —— 术前决策的最终产物（聚合输出层）。

定位：
    依赖 VascCase + VascMeasurement + AnatomyScore + RiskAssessment，
    由决策树仲裁产出治疗方案 + 证据链。

审计要求（核心）：
    1. 必须记录**完整决策路径**（逐条规则求值结果，含被否决项）；
    2. 必须记录**规则版本号**（``RULESET_VERSION``，如 ``v36@v3.6``）；
    3. 必须记录**输入快照哈希**，保证可复现。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RouteMode(StrEnum):
    """最终路由模式。优先级：急诊 > 复杂 > 指南。

    与 :class:`src.rules.router.RouteKind` 是**正交的两轴**，不可合并：

    ==================  ============================================
    ``RouteKind``       结论属哪一类（进入模式 / 人工复核 / 资料核实）
    ``RouteMode``       进入模式时走哪条路径（本枚举，仅 kind=MODE 时非空）
    ==================  ============================================

    放在本模块而非共享枚举文件：它是 ``FinalRecommendation.route_mode`` 的取值域，
    而 :class:`FinalRecommendation` 是它最上游的消费者。执行层与工具层从本模块
    取用（``rules → models`` 单向依赖），**不能**反向定义在 ``router.py`` 里 ——
    那会让模型层依赖判定层并形成循环导入。

    ⚠️ 成员值是审计哈希的一部分（``RouteResult`` 在 ``HASHED_KEYS`` 里），只增不改。
    """

    EMERGENCY = "急诊模式"
    COMPLEX = "复杂模式"
    GUIDELINE = "指南模式"


class EvidenceItem(BaseModel):
    """一条证据：结论 + 出处 + 依据规则。"""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(..., description="证据结论（如「颈长 12mm < 15mm 阈值」）。")
    source: str | None = Field(default=None, description="证据出处（原文片段 / 影像 / 量表）。")
    rule_ref: str | None = Field(default=None, description="依据规则编号（如 O15 / R001）。")


class DecisionStep(BaseModel):
    """决策流水线上的一个步骤记录（含被否决项）。"""

    model_config = ConfigDict(extra="forbid")

    order: int = Field(..., ge=1, description="求值顺序（从 1 起，按实际执行次序）。")
    condition: str = Field(..., description="判定条件描述（取规则名）。")
    result: bool | None = Field(
        ...,
        description=(
            "该步求值结果。**三值**：True 成立 / False 不成立 / None 未判定（UNKNOWN）。"
            "None 不等于 False —— 它表示资料不足，不能当作阴性。"
        ),
    )
    rule_ref: str | None = Field(default=None, description="依据规则编号（如 G03.R04）。")
    note: str | None = Field(default=None, description="规则来源（V3.6.2 单元格，如 G03!12）。")


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


class EmergencyBrief(BaseModel):
    """急诊五段式出口 —— 只有 ``route_mode`` 是急诊时才有值。

    为什么单独成型而不是把五段拼进 ``primary_recommendation``：
        G05 要求「安全规则通知先行、不等待非必要分析」。拼成一句话后，
        「危险提示」会与「术式方向」平铺在一起，读者要读到第二句才知道
        病人正在出血。分段之后，顺序本身携带优先级。

    五段与数据源的对应（**全部是既有结论的重排，本模型不含新判断**）：

    ================  ====================================================
    字段              来源
    ================  ====================================================
    ``danger``        命中的 P0 规则（路由层标签 + 工具A 条目），规格原文
    ``known_facts``   ``evidence_chain`` 的前若干条
    ``pending``       工具A 的 ``gaps``（待补关键资料）
    ``rapid_assessment`` 工具A 的术式候选 + 终点
    ``handoff``       资源类字段**是否已确认**，按接收方分行
    ================  ====================================================

    ⚠️ 不含任何时间戳：本对象随 ``FinalRecommendation`` 进 ``HASHED_KEYS``，
    加时间会让「同一输入两次运行」产出不同哈希。
    """

    model_config = ConfigDict(extra="forbid")

    danger: list[str] = Field(
        default_factory=list, description="危险提示：命中的 P0 规则（规格原文，不重写措辞）。"
    )
    known_facts: list[str] = Field(default_factory=list, description="已知事实（判定依据摘录）。")
    pending: list[str] = Field(default_factory=list, description="待补关键资料（工具A 缺口）。")
    rapid_assessment: list[str] = Field(
        default_factory=list, description="快速评估：术式方向与终点（不等完整分析）。"
    )
    handoff: list[str] = Field(
        default_factory=list, description="交接：接收方 + 该方资源是否已确认（不写具体人）。"
    )


class FinalRecommendation(BaseModel):
    """术前决策最终输出。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., description="关联病例标识。")

    # ---- 决策结论 ----
    route_mode: RouteMode | None = Field(
        default=None,
        description=(
            "最终路由模式。**仅当结论是「进入模式」时非空**；"
            "转人工复核或资料核实时为 None（此时没有可执行的工具计划）。"
        ),
    )
    primary_recommendation: str = Field(
        ..., description="主建议（如「进入急诊应急预案」/「择期修复评估」）。"
    )
    treatment_options: list[str] = Field(default_factory=list, description="可选治疗方案清单。")
    confidence_level: Literal["高", "中", "低"] = Field(
        ...,
        description=(
            "证据充分性分级（B4/B5 裁定：不输出无依据的点估计）。"
            "判据可枚举：低＝有决定性未判定项；中＝有缺口但不影响结论；高＝无缺口。"
        ),
    )
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

    emergency_brief: EmergencyBrief | None = Field(
        default=None,
        description=(
            "急诊五段式出口。**仅急诊模式非空**；其余模式为 None —— "
            "择期病例没有「不等待分析」的时限要求，用同一套结构会把"
            "「危险提示」写成空段，读起来像「没有危险」。"
        ),
    )

    ruleset_version: str = Field(
        ...,
        description="规则版本号（如 'v36@v3.6'）。审计必需。",
    )
    input_snapshot_hash: str | None = Field(default=None, description="输入快照哈希（用于复现）。")
    translated_text: str | None = Field(
        default=None,
        description="输出转译文本（LLM 生成，供医生阅读；仅措辞润色，不改结论）。",
    )


__all__ = [
    "DecisionStep",
    "EmergencyBrief",
    "EvidenceItem",
    "FinalRecommendation",
    "RouteMode",
    "ToolFinding",
]
