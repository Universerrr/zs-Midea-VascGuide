"""工具C · 解剖评分器 —— case 解剖字段 + VascMeasurement → AnatomyScore。

定位：
    把「一堆分散的解剖读数」压成一个可比较的复杂度分值。它是工具E 的上游，
    也是「复杂模式」这个路由结论在数值上的体现。

输入契约（``SPEC.consumes``）：
    ``case 解剖字段``   颈长 / 成角 / 入路直径 / 分型（浅层门控读数）
    ``VascMeasurement`` 工具B 的产物（各节段直径 / 钙化 / 中心线）

    **必须走** :meth:`ToolRequest.require` 取上游，缺上游时抛
    :class:`MissingUpstreamError`。严禁在本工具里写「取不到就用 case 里的
    浅层读数兜底」——那会让「B 没跑」和「B 跑了但结果正常」在产物上无法区分。

输出契约：
    ``AnatomyScore``：``total`` / ``grade``（如 0–3 级）/ ``items``（逐项分值
    与阈值）/ ``scale_version``（量表版本）。

实现要点（验收标准）：
    1. 评分量表是**外部临床输入**。设计表 6 个 Sheet 中没有这张表，
       ``05_说明与待确认`` 已明确「解剖评分的完整参数不由 VascCase 承载」。
       在量表到位前不要自行设计分值权重——那是编造临床依据。
    2. ``items`` 必须逐项留痕（哪一项、实际值、阈值、得分），否则审计时
       只能看到一个总分，无法验算。这与 ``router.py`` 的 note 契约同源：
       **结论必须能由原始值独立复算**。
    3. ``scale_version`` 进审计哈希投影：量表改版会改变分值，必须可分辨。
    4. 分值输出后**不回写** ``case.complex_anatomy``——路由判定已经完成，
       回写会让「判定层结论」与「评分器结论」变成两处真相。
"""

from __future__ import annotations

from typing import Any

from .base import ToolRequest, ToolSpec

__all__ = ["SPEC", "run"]

SPEC = ToolSpec(
    key="anatomy_scorer",
    label="工具C · 解剖评分器",
    consumes=("case 解剖字段", "VascMeasurement"),
    produces="AnatomyScore",
    depends_on=("imaging_quantifier",),
    blocked_reason=(
        "需临床评分量表（各项分值口径与权重）。设计表 6 个 Sheet 中**没有**"
        "评分量表这张表，且 Sheet「05_说明与待确认」已明确：解剖评分的完整参数"
        "不由 VascCase 承载。故量表是外部输入，当前缺失。"
    ),
)


def run(request: ToolRequest) -> Any:
    """按量表逐项打分，返回 ``AnatomyScore``。

    参数：
        request: 需 ``request.require("imaging_quantifier")`` 取 B 的产物。

    返回：
        ``AnatomyScore``，含逐项明细与量表版本。

    抛出：
        NotImplementedError: 量表缺失（说明文字即阻塞原因）。
    """
    raise NotImplementedError(f"{SPEC.label} 未实现：{SPEC.blocked_reason}")
