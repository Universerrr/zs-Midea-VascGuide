"""工具B · 影像量化器 —— CTA 影像 → VascMeasurement（容积 / 各节段直径 / 钙化）。

定位：
    解剖测量的**真值来源**。``VascCase`` 里的 ``neck_length_mm`` / ``neck_angle_deg`` /
    ``access_diameter_mm`` 只是「门控用的浅层读数」（设计表 ``05_说明与待确认`` 第 8 条），
    精密量化属于本工具，产物写回旁路模型 ``VascMeasurement``，**不回写 VascCase**。

输入契约：
    ``CTA 影像`` —— **注意：这不是 VascCase 的字段**。当前 :class:`ToolRequest`
    只携带 ``case``，没有影像句柄通道。实现本工具前必须先补上这一通道
    （预期形态：``case`` 之外的影像定位符，或由调用方在 ``ToolRequest`` 上扩展
    一个可选 ``study`` 字段）。**不要在 case 里加一个 ``cta_path`` 字符串字段**——
    那会把「病例事实」与「外部资源定位」混为一谈，且让审计库无法复现
    （同一路径在不同机器上指向不同文件）。

输出契约：
    ``VascMeasurement``（继承 ``VascCase`` 的只有 ``case_id``）：

    ``volume_mm3``            瘤体容积
    ``segment_diameters``     各节段最大径（肾上 / 肾旁 / 肾下 / 髂总 / 髂外）
    ``calcification_score``   钙化量化
    ``centerline_length_mm``  中心线长度

实现要点（验收标准）：
    1. 血管分割 + 中心线提取是算法本体，不是「调个库」。需要明确算法选型与
       版本，否则同一份影像在不同版本下产出不同数值，审计链失效。
    2. 输出必须带算法版本号，并进入审计哈希投影——「谁算的、用哪个版本算的」
       是审计的前提。
    3. 测量精度口径（取 1 位小数？还是保留原始浮点？）需与 ``VascCase``
       的 ``_quantize`` 一致，否则「同一测量」在两处显示不同值。
    4. 本工具是工具C 与工具E 的**唯一**上游（见 ``SPEC.depends_on`` 的反向边）。
       它缺失时 C 与 E 必须整体跳过，不得退化成「用 case 浅层读数凑一个评分」。
"""

from __future__ import annotations

from typing import Any

from .base import ToolRequest, ToolSpec

__all__ = ["SPEC", "run"]

SPEC = ToolSpec(
    key="imaging_quantifier",
    label="工具B · 影像量化器",
    consumes=("CTA 影像",),
    produces="VascMeasurement（容积 / 各节段直径 / 钙化量化）",
    depends_on=(),
    blocked_reason=(
        "需 CTA 影像数据 + 血管分割与中心线提取算法；当前既无影像数据，"
        "也无算法实现，故无法产出可验证的量化结果。"
    ),
)


def run(request: ToolRequest) -> Any:
    """对 CTA 影像做分割与量化，返回 ``VascMeasurement``。

    参数：
        request: 需携带影像来源（见模块文档「输入契约」——该通道尚未建立）。

    返回：
        ``VascMeasurement``，含算法版本号。

    抛出：
        NotImplementedError: 影像通道与算法均缺失（说明文字即阻塞原因）。
    """
    raise NotImplementedError(f"{SPEC.label} 未实现：{SPEC.blocked_reason}")
