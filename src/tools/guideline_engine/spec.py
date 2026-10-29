"""工具A 的元信息 —— :class:`ToolSpec` 实例。

单独成文件而不写在 ``__init__.py``：``engine.run()`` 要引用 ``SPEC.label``，
放在包初始化文件里会形成 ``__init__ → engine → __init__`` 的循环导入。
依赖方向固定为 ``__init__ → engine → spec``。
"""

from __future__ import annotations

from ..base import ToolSpec

__all__ = ["SPEC"]

SPEC = ToolSpec(
    key="guideline_engine",
    label="工具A · 指南引擎",
    consumes=(
        "route_result.class_output",
        "route_result.cl_taaa_iv",
        "case.cl.aaa_rupture",
        "case.c.extrav",
        "case.c.hematoma",
        "case.c.rupture_report",
        "case.v.sbp",
        "case.v.conscious_change",
    ),
    produces="病种层结论 + 缺口 + 决策路径（GuidelineReport）",
    depends_on=(),
    blocked_reason=None,
)
