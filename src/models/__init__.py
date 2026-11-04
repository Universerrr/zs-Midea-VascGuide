"""VascGuide 数据模型包。

按 Agent 阶段组织：
    preop/   术前阶段（VascCase / VascMeasurement / AnatomyScore /
             RiskAssessment / FinalRecommendation）
    intraop  术中阶段（待实现）
    postop   术后阶段（待实现）

类型落点原则：**每个类型只定义一次，且在它真正的消费者那里**。
    · ``FieldWriter`` —— 审计归责，只被 ``FieldProvenance`` / ``UrgencyFlagAudit``
      使用，定义在 ``audit.py``；
    · ``RouteMode`` —— 只被 ``FinalRecommendation.route_mode`` 及执行层消费，
      定义在 ``preop/recommendation.py``；
    · 事实字段的取值域 —— 用字段级 ``Literal``（中文值直写），
      不再有「共享枚举文件」这一层。理由是「一列四义」的取值列无法用单一
      ``StrEnum`` 表达，强行共用会逼出归一化函数与隐式别名。
"""

from .audit import FieldProvenance, FieldWriter, UrgencyFlagAudit
from .preop import (
    DOMAIN_FIELD_COUNTS,
    TOTAL_FIELD_COUNT,
    UNCERTAIN_TOKENS,
    AnatomyScore,
    CenterlinePoint,
    DecisionStep,
    EvidenceItem,
    FinalRecommendation,
    RiskAssessment,
    RouteMode,
    ToolFinding,
    VascCase,
    VascMeasurement,
    derive,
    is_uncertain,
)

__all__ = [
    "DOMAIN_FIELD_COUNTS",
    "TOTAL_FIELD_COUNT",
    "UNCERTAIN_TOKENS",
    "AnatomyScore",
    "CenterlinePoint",
    "DecisionStep",
    "EvidenceItem",
    "FieldProvenance",
    "FieldWriter",
    "FinalRecommendation",
    "RiskAssessment",
    "RouteMode",
    "ToolFinding",
    "UrgencyFlagAudit",
    "VascCase",
    "VascMeasurement",
    "derive",
    "is_uncertain",
]
