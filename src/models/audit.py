"""审计留痕模型 —— 跨 Agent 共用（术前 / 术中 / 术后）。

依据：
    · `.workbuddy/spec/VascCase字段设计表.xlsx` · Sheet `01_字段总表`「审计留痕」列；
    · `.workbuddy/spec/VascCase开发交接文档.md` 第六节「审计留痕设计」。

留痕规律（按字段层）：
    事实层 —— 原文出处 + 写入者（LLM / 影像量化器 / 人工）+ 置信度 + 覆写痕迹；
    判定层 —— 读入的事实快照 + 命中规则编号 + 规则版本；
    输出层 —— 完整决策路径（含**被否决项**）+ 输入快照哈希 + 规则版本。

三个审计目标：
    可复现（同输入 + 同规则版本 → 必然同结论）
    可追溯（每条建议可回指到具体规则与证据）
    可归责（区分 LLM 判断 / 规则判断 / 人工覆写）

治理红线：本模块只做数据承载，**不得引入任何 LLM 客户端**。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class FieldWriter(StrEnum):
    """字段写入者 —— 审计归责用：区分 LLM 判断 / 规则判断 / 人工覆写。

    放在本模块而非共享枚举文件：它是**审计**的专属概念，唯一的消费者就是
    下面的 :class:`FieldProvenance` 与 :class:`UrgencyFlagAudit`。类型与它的
    使用场景同址，改归责口径时只需看这一个文件。

    ⚠️ **成员值是审计哈希的一部分**（``field_evidence`` 挂在 ``VascCase`` 上，
    而 ``case`` 进 :data:`HASHED_KEYS`）。改名（如「人工」→「医生」）会让已有
    审计记录的哈希链对不上，因此只增不改。

    启用状态（2026-10-26）：
        · ``LLM``     —— 抽取器实现后由 :func:`extract_case_from_text` 写入（当前恒抛
          ``NotImplementedError``，属三处留白之一）；
        · ``IMAGING`` —— 工具 B 影像量化器实现后启用；注意 ``VascMeasurement`` 目前
          **没有** provenance 通道，需要届时补上，否则该值无处安放；
        · ``HUMAN``   —— 医生覆写 / 置位 ``urgency_flag`` 时写入；
        · ``SYSTEM``  —— 程序回填（如 ``derive()``）。
    """

    LLM = "LLM"
    IMAGING = "影像量化器"
    HUMAN = "人工"
    SYSTEM = "系统"


class FieldProvenance(BaseModel):
    """单个事实层字段的证据留痕。

    说明：
        「写入者」是归责的关键 —— 同一个字段可能由 LLM 抽取、由影像量化器写入，
        或被人工覆写；三者可信度与责任主体完全不同，必须可区分。

    ⚠️ **本模型刻意不含 ``confidence`` 字段**（2026-10-26 删除）。
    字段级点估计给不出依据：LLM 报的 0.95 与影像算法的 0.95 不是同一种东西，
    二者乘进同一套公式只会造出「看起来精确的无依据数字」。改由两处表达：

        · **缺什么** —— ``RouteResult.evidence_gaps``（逐项列出未判定的字段）；
        · **结论有多可信** —— ``FinalRecommendation.confidence_level``（高/中/低，
          判据可枚举，见 :func:`src.rules.router._confidence`）。

    若将来确要重新引入，须先回答「这个数字的依据是什么、如何校准」，
    否则不要加回。
    """

    model_config = ConfigDict(extra="forbid")

    source_quote: str | None = Field(
        default=None,
        description="原文出处（病历/CTA 报告片段及其字符偏移区间）。",
    )
    writer: FieldWriter = Field(
        ...,
        description="写入者：LLM / 影像量化器 / 人工 / 系统。归责必需。",
    )
    overwritten_by_human: bool = Field(
        default=False,
        description="是否被人工覆写过（覆写痕迹）。",
    )
    note: str | None = Field(
        default=None,
        description="补充说明（如否定句/时态解析过程、覆写理由）。",
    )


class UrgencyFlagAudit(BaseModel):
    """``VascCase.urgency_flag`` 的置位留痕（O01 审计要求）。

    设计表「审计留痕」列明确要求：记录**置位者**（人工/系统）+ **时间戳** + **置位理由**。
    该字段独立于自动判定（破裂/症状/休克），置位即最高优先。
    """

    model_config = ConfigDict(extra="forbid")

    set_by: FieldWriter = Field(..., description="置位者：人工 / 系统。")
    set_at: str = Field(
        ...,
        description="置位时间（ISO 8601 字符串，如 '2026-10-12T14:20:00+08:00'）。",
    )
    reason: str = Field(..., description="置位理由（临床依据原文或授权说明）。")


__all__ = ["FieldProvenance", "FieldWriter", "UrgencyFlagAudit"]
