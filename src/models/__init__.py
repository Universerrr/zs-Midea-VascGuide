"""VascGuide 数据模型包。

按 Agent 阶段组织：
    preop/   术前阶段（VascCase / VascMeasurement / AnatomyScore /
             RiskAssessment / FinalRecommendation）
    intraop  术中阶段（待实现）
    postop   术后阶段（待实现）

共享枚举见 enums.py；跨 Agent 审计留痕结构见 audit.py。
"""

from .audit import FieldProvenance, UrgencyFlagAudit
from .enums import (
    AnatomyLevel,
    CKDStage,
    DiagnosisCertainty,
    DiseaseCategory,
    EndoleakType,
    FieldWriter,
    HeartFailureNYHA,
    InfectionEvidence,
    LesionMorphology,
    MalignancyStatus,
    PainAttribution,
    PriorRepair,
    RouteMode,
    RuptureStatus,
    Sex,
    SymptomAAA,
    TemporalStatus,
    Tortuosity,
    YesNoUnknown,
)
from .preop import (
    AUDIT_FIELDS,
    ENUM_FACT_FIELDS,
    ONE_DECIMAL_FIELDS,
    AnatomyScore,
    CenterlinePoint,
    DecisionStep,
    EvidenceItem,
    FinalRecommendation,
    RiskAssessment,
    ToolFinding,
    VascCase,
    VascMeasurement,
    derive,
)

__all__ = [
    "AUDIT_FIELDS",
    "ENUM_FACT_FIELDS",
    "ONE_DECIMAL_FIELDS",
    "AnatomyLevel",
    "AnatomyScore",
    "CKDStage",
    "CenterlinePoint",
    "DecisionStep",
    "DiagnosisCertainty",
    "DiseaseCategory",
    "EndoleakType",
    "EvidenceItem",
    "FieldProvenance",
    "FieldWriter",
    "FinalRecommendation",
    "HeartFailureNYHA",
    "InfectionEvidence",
    "LesionMorphology",
    "MalignancyStatus",
    "PainAttribution",
    "PriorRepair",
    "RiskAssessment",
    "RouteMode",
    "RuptureStatus",
    "Sex",
    "SymptomAAA",
    "TemporalStatus",
    "ToolFinding",
    "Tortuosity",
    "UrgencyFlagAudit",
    "VascCase",
    "VascMeasurement",
    "YesNoUnknown",
    "derive",
]
