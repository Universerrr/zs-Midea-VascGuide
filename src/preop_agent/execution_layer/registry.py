"""工具登记表 —— 5 个确定性算子的规格与执行入口。

定位：
    编排层与工具实现之间的**唯一接缝**。本模块只做四件事：汇总 ``SPEC``、
    按模式取子集、按依赖排序、分派执行。**不含任何临床逻辑**。

为什么 ``ToolSpec`` 定义在 ``src/tools/base.py``：
    元信息属于工具契约，不属于调度器。放在工具包里，依赖方向才是
    ``models ← tools ← preop_agent``（单向），工具模块无需反向 import 编排层。

实现一个工具需要改两处，缺一不可：
    1. ``src/tools/<tool>.py``：``SPEC.blocked_reason = None`` + 补齐 ``run()``；
    2. 什么都不用改——:data:`IMPLEMENTED_TOOLS` 从 ``SPEC.implemented`` 派生，
       计划与执行顺序也会自动跟上。

新增一个工具需要改三处：
    1. ``src/tools/<tool>.py``（新文件）；
    2. 本模块的 :data:`TOOL_REGISTRY` 与 :data:`RUNNER_BY_KEY`；
    3. ``plan.py`` 的 ``MODE_PLANS``（``TestModePlans`` 会强制你改）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Final

from ...tools.anatomy_scorer import SPEC as ANATOMY_SCORER_SPEC
from ...tools.anatomy_scorer import run as run_anatomy_scorer
from ...tools.base import ToolRequest, ToolSpec
from ...tools.guideline_engine import SPEC as GUIDELINE_ENGINE_SPEC
from ...tools.guideline_engine import run as run_guideline_engine
from ...tools.imaging_quantifier import SPEC as IMAGING_QUANTIFIER_SPEC
from ...tools.imaging_quantifier import run as run_imaging_quantifier
from ...tools.memory_retriever import SPEC as MEMORY_RETRIEVER_SPEC
from ...tools.memory_retriever import run as run_memory_retriever
from ...tools.risk_calculator import SPEC as RISK_CALCULATOR_SPEC
from ...tools.risk_calculator import run as run_risk_calculator
from ..state import PreopState

__all__ = [
    "IMPLEMENTED_TOOLS",
    "RUNNER_BY_KEY",
    "SPEC_BY_KEY",
    "TOOL_REGISTRY",
    "run_tool",
    "unimplemented_tools",
]

#: 顺序即展示顺序（A→E），不承担调度语义——调度看 ``plan.MODE_PLANS``。
TOOL_REGISTRY: tuple[ToolSpec, ...] = (
    GUIDELINE_ENGINE_SPEC,
    IMAGING_QUANTIFIER_SPEC,
    ANATOMY_SCORER_SPEC,
    MEMORY_RETRIEVER_SPEC,
    RISK_CALCULATOR_SPEC,
)

SPEC_BY_KEY: Final[Mapping[str, ToolSpec]] = MappingProxyType(
    {spec.key: spec for spec in TOOL_REGISTRY}
)

RUNNER_BY_KEY: Final[Mapping[str, Callable[[ToolRequest], Any]]] = MappingProxyType(
    {
        "guideline_engine": run_guideline_engine,
        "imaging_quantifier": run_imaging_quantifier,
        "anatomy_scorer": run_anatomy_scorer,
        "memory_retriever": run_memory_retriever,
        "risk_calculator": run_risk_calculator,
    }
)

#: 已实现的工具键集合（由 ``SPEC.implemented`` 派生，不手工维护）。
IMPLEMENTED_TOOLS: frozenset[str] = frozenset(s.key for s in TOOL_REGISTRY if s.implemented)


def unimplemented_tools() -> tuple[ToolSpec, ...]:
    """返回尚未实现的工具（含各自阻塞原因），供测试与报告展示。"""
    return tuple(s for s in TOOL_REGISTRY if not s.implemented)


def run_tool(
    key: str,
    state: PreopState,
    *,
    upstream: Mapping[str, Any] | None = None,
) -> Any:
    """按工具键执行单个工具。

    参数：
        key: 工具键，必须在 :data:`TOOL_REGISTRY` 中。
        state: 至少含 ``case``（``route_mode`` 需已回填）。
        upstream: 本次运行中已完成的**上游**工具产物（工具键 → 产物）。

    抛出：
        KeyError: ``key`` 未登记。
        NotImplementedError: 该工具尚未实现（说明文字即其阻塞原因）。
        RuntimeError: ``case.route_mode`` 未回填。
    """
    spec = SPEC_BY_KEY[key]
    if not spec.implemented:
        raise NotImplementedError(f"{spec.label} 未实现：{spec.blocked_reason}")

    case = state["case"]
    if case.route_mode is None:
        raise RuntimeError(
            f"{spec.label} 需要已回填 route_mode 的 case；请在 router_layer 之后调用。"
        )

    request = ToolRequest(case=case, route_mode=case.route_mode, upstream=upstream or {})
    return RUNNER_BY_KEY[key](request)
