"""工具E · 风险计算器 —— case 合并症字段 + AnatomyScore → RiskAssessment。

定位：
    围术期风险量化，是工具链的**末端**（B → C → E）。它同时消费「病例侧的
    合并症」与「解剖侧的评分」，因此天然跨越两层输入。

输入契约（``SPEC.consumes``）：
    ``case 合并症字段``  COPD / CKD / CAD / NYHA / 糖尿病 / ASA 分级等
    ``AnatomyScore``    工具C 的产物

    与工具C 同规矩：上游必须经 :meth:`ToolRequest.require` 取，缺则抛
    :class:`MissingUpstreamError`。不得在缺 ``AnatomyScore`` 时按「解剖风险取 0」
    算出一个偏乐观的数值——那是最危险的一种静默降级。

输出契约：
    ``RiskAssessment``：``score`` / ``risk_grade`` / ``model_name`` /
    ``model_version`` / ``contributions``（各因素贡献度）。

实现要点（验收标准）：
    1. 需要**具体**风险模型及其系数（围术期死亡率的计算口径、模型版本）。
       该口径不在设计表内，属外部临床输入。在没有模型前不要拿「合并症条数」
       凑一个分——那会被当成有循证依据的风险值。
    2. ``contributions`` 必须可加总回 ``score``（误差在量化精度内），
       否则又是「只能看总分、无法验算」。
    3. ``model_version`` 进审计哈希投影。同一病例在两个模型版本下得出不同
       风险值，是必须能被区分的两个事实。
    4. 风险值**不参与路由判定**（路由已在执行层之前完成）。它只影响输出层的
       措辞与是否建议 MDT，不得反向修改 ``case.route_mode``。
"""

from __future__ import annotations

from typing import Any

from .base import ToolRequest, ToolSpec

__all__ = ["SPEC", "run"]

SPEC = ToolSpec(
    key="risk_calculator",
    label="工具E · 风险计算器",
    consumes=("case 合并症字段", "AnatomyScore"),
    produces="RiskAssessment",
    depends_on=("anatomy_scorer",),
    blocked_reason=(
        "需具体风险模型及其系数（围术期死亡率的计算口径、模型版本）。"
        "该口径同样不在设计表内，属外部临床输入，当前缺失。"
    ),
)


def run(request: ToolRequest) -> Any:
    """按风险模型计算围术期风险，返回 ``RiskAssessment``。

    参数：
        request: 需 ``request.require("anatomy_scorer")`` 取 C 的产物。

    返回：
        ``RiskAssessment``，含各因素贡献度与模型版本。

    抛出：
        NotImplementedError: 风险模型缺失（说明文字即阻塞原因）。
    """
    raise NotImplementedError(f"{SPEC.label} 未实现：{SPEC.blocked_reason}")
