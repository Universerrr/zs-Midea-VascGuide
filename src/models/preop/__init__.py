"""术前阶段（Preoperative）数据模型包。

按架构分层组织，目录结构即架构图：

    VascCase             输入层 · LLM 抽取的术前输入事实全集（case.py）
    VascMeasurement      影像量化器 B 的产物      （measurement.py）
    AnatomyScore         解剖评分器 C 的产物      （assessment.py）
    RiskAssessment       风险评估器 E 的产物      （assessment.py）
    FinalRecommendation  决策产物（聚合输出层）     （recommendation.py）

数据流（关键）：
    VascCase 被多方读取 → 各路工具产物各自独立成模型，**不回写 VascCase**。
    即：VascCase 管「输入」，不管「产物」。

    ┌──────────────┐
    │   VascCase   │  LLM 一次抽取 · 术前输入事实全集
    └──────┬───────┘
           │ 被读取（只读）
   ┌───────┼────────┬──────────────┬───────────────┐
   ▼       ▼        ▼              ▼               ▼
 Router   B影像量化  C解剖评分       E风险评估      A指南引擎
 （本包）  → VascMeasurement → AnatomyScore → RiskAssessment
   └───────┴────────┴──────────────┴───────────────┘
                          ▼
              FinalRecommendation（决策产物）

共享枚举见 src/models/enums.py。
"""

from .assessment import AnatomyScore, RiskAssessment
from .case import (
    AUDIT_FIELDS,
    CRITICAL_FACT_FIELDS,
    ENUM_FACT_FIELDS,
    ONE_DECIMAL_FIELDS,
    VascCase,
    apply_route_result,
    derive,
)
from .measurement import CenterlinePoint, VascMeasurement
from .recommendation import DecisionStep, EvidenceItem, FinalRecommendation, ToolFinding

__all__ = [
    "AUDIT_FIELDS",
    "CRITICAL_FACT_FIELDS",
    "ENUM_FACT_FIELDS",
    "ONE_DECIMAL_FIELDS",
    "AnatomyScore",
    "CenterlinePoint",
    "DecisionStep",
    "EvidenceItem",
    "FinalRecommendation",
    "RiskAssessment",
    "ToolFinding",
    "VascCase",
    "VascMeasurement",
    "apply_route_result",
    "derive",
]
