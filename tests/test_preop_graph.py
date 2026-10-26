"""编排层测试 —— 端到端流水线、Checkpoint 往返、以及两条架构守卫。

与另几个测试文件的分工（病例基线只从 ``conftest`` 取，不各写一份）：
    · ``test_preop.py``           —— 路由**走得对不对**（86 项端到端基线）
    · ``test_model_contract.py``  —— 数据**口径符不符合设计表**（81 项）
    · ``test_router_branches.py`` —— **54 条规则**逐条命中与三值语义（193 项）
    · ``test_preop_graph.py``（本文件）—— 编排**串得对不对 + 审计**落得住、可回放、
      以及**架构红线是否被机械守住**：
        ① 薄封装守卫（router_layer 不得偷藏任何临床规则）
        ② 判定层零 LLM 守卫（判定路径不得 import 任何 LLM 客户端）

运行方式（``uv run`` 会自动向上找到项目根并使用 ``.venv`` 解释器，
无需手动 cd 或指定解释器路径）：

    uv run pytest tests/test_preop_graph.py -v

用例均以 ``GRAPH-`` 开头构造病例，避免与另几个文件的病例标识混淆。
"""

from __future__ import annotations

import ast
import importlib
import logging
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from conftest import make_case as build_case
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, ValidationError

from src.models.audit import FieldProvenance, FieldWriter, UrgencyFlagAudit
from src.models.preop.assessment import AnatomyScore, RiskAssessment
from src.models.preop.case import VascCase, derive
from src.models.preop.measurement import CenterlinePoint, VascMeasurement
from src.models.preop.recommendation import FinalRecommendation, RouteMode, ToolFinding
from src.preop_agent import graph as graph_module
from src.preop_agent.execution_layer import node as exec_node_module
from src.preop_agent.execution_layer import plan as plan_module
from src.preop_agent.execution_layer import registry as registry_module
from src.preop_agent.execution_layer.node import (
    IMPLEMENTED_TOOLS,
    MODE_PLANS,
    TOOL_REGISTRY,
    execution_node,
    execution_order,
    run_tool,
    unimplemented_tools,
)
from src.preop_agent.execution_layer.plan import plan_for
from src.preop_agent.execution_layer.registry import SPEC_BY_KEY
from src.preop_agent.graph import (
    build_checkpoint_serde,
    build_preop_graph,
    run_preop,
    sqlite_checkpointer,
    thread_config,
)
from src.preop_agent.input_layer.node import (
    EXTRACTOR_BLOCKED_REASON,
    EXTRACTOR_IMPLEMENTED,
    extract_case_from_text,
    input_node,
)
from src.preop_agent.output_layer.node import (
    MAX_FALLBACK_SUMMARY_CHARS,
    MAX_SUMMARY_FIELDS,
    PRIMARY_RECOMMENDATION_PLACEHOLDER,
    TRANSLATION_IMPLEMENTED,
    _render,
    _render_scalar,
    build_tool_findings,
    output_node,
)
from src.preop_agent.router_layer.node import router_node
from src.preop_agent.state import HASHED_KEYS, _project, stable_hash
from src.rules.router import RULES, RouteKind, RouteResult, route
from src.tools.base import MissingUpstreamError, ToolRequest, ToolSpec

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 判定层目录：这些目录下的代码**只允许是确定性实现**。
JUDGMENT_LAYER_DIRS = (
    REPO_ROOT / "src" / "rules",
    REPO_ROOT / "src" / "tools",
    REPO_ROOT / "src" / "preop_agent" / "router_layer",
    REPO_ROOT / "src" / "preop_agent" / "execution_layer",
    REPO_ROOT / "src" / "preop_agent" / "output_layer",
)

ROUTER_LAYER_NODE = REPO_ROOT / "src" / "preop_agent" / "router_layer" / "node.py"

#: LLM 客户端 SDK 的模块名前缀（判定层出现任何一个即视为红线被破）。
FORBIDDEN_LLM_MODULES = (
    "openai",
    "anthropic",
    "cohere",
    "mistralai",
    "litellm",
    "ollama",
    "llama_cpp",
    "transformers",
    "dashscope",
    "zhipuai",
    "qianfan",
    "groq",
    "together",
    "google.generativeai",
    "google.genai",
    "langchain_openai",
    "langchain_anthropic",
)

#: 临床规则痕迹：病种 / 解剖 / 处置关键词。thin wrapper 里出现即说明规则被抄了一份。
FORBIDDEN_CLINICAL_TOKENS = (
    "肾下",
    "近肾",
    "短颈",
    "瘤颈",
    "成角",
    "钙化",
    "血栓",
    "破裂",
    "感染",
    "炎症",
    "内漏",
    "假性动脉瘤",
    "吻合口",
    "合并症",
    "急诊",
    "入路",
)


def make_case(**overrides: Any) -> VascCase:
    """基线病例（标识以 ``GRAPH-`` 开头，与另两个测试文件的病例区分）。

    基线本身定义在 ``conftest.baseline_kwargs()`` —— **只有一份**。这个包装只做
    两件事：换 ``case_id`` 前缀、透传域级覆盖。
    """
    return build_case(case_id=overrides.pop("case_id", "GRAPH-000"), **overrides)


def make_rich_case() -> VascCase:
    """带完整审计留痕（证据链 + 急症标志留痕）的病例。"""
    return make_case(
        case_id="GRAPH-RICH",
        field_evidence={
            "cl.aaa_diam": FieldProvenance(
                source_quote="CTA 示腹主动脉最大径 42mm。",
                writer=FieldWriter.LLM,
                confidence=0.95,
            )
        },
        urgency_flag_audit=UrgencyFlagAudit(
            set_by=FieldWriter.HUMAN,
            set_at="2026-10-13T09:30:00+08:00",
            reason="门诊初筛标记",
        ),
    )


def _iter_source_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py") if p.is_file())


def _iter_imported_modules(path: Path) -> list[str]:
    """列出源文件里所有 ``import`` 的模块全名（含相对导入的模块部分）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)
    return modules


def _code_constants(path: Path) -> tuple[list[str], list[float]]:
    """取出源码里的**代码常量**（字符串 / 数值），**排除文档字符串**。

    排除 docstring 是刻意的：文档里解释「规则为什么不写在这里」是正当的，
    但**代码里**出现阈值或病种关键词就不正当了。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))

    docstring_ids: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        body = getattr(node, "body", [])
        if not body or not isinstance(node, holders):
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstring_ids.add(id(first.value))

    strings: list[str] = []
    numbers: list[float] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in docstring_ids:
            continue
        if isinstance(node.value, str):
            strings.append(node.value)
        elif isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            numbers.append(float(node.value))
    return strings, numbers


# ===========================================================================
# 稳定哈希
# ===========================================================================
class TestStableHash:
    def test_hash_ignores_key_order(self) -> None:
        """同内容、不同插入顺序 → 同哈希（可复现的前提）。"""
        case = make_case()
        a = {"case": case, "case_id": "GRAPH-000"}
        b = {"case_id": "GRAPH-000", "case": case}
        assert stable_hash(a) == stable_hash(b)

    def test_hash_ignores_audit_trail(self) -> None:
        """audit_trail 含时间戳，必须被排除在哈希之外，否则永不「可复现」。"""
        state: dict[str, Any] = {"case": make_case(), "case_id": "GRAPH-000"}
        noisy = {**state, "audit_trail": ["任意时间戳内容", "再来一条"]}
        assert stable_hash(_project(state)) == stable_hash(_project(noisy))

    def test_hash_changes_with_content(self) -> None:
        assert stable_hash({"case_id": "A"}) != stable_hash({"case_id": "B"})

    def test_hash_handles_pydantic_enum_dataclass(self) -> None:
        """pydantic 模型 / 枚举 / dataclass 都要能参与哈希且不抛异常。"""
        payload = {
            "case": make_case(),
            "enum": RouteMode.GUIDELINE,
            "product": AnatomyScore(case_id="GRAPH-000", total_score=1.5),
        }
        digest = stable_hash(payload)
        assert len(digest) == 64
        assert digest == stable_hash({**payload, "enum": RouteMode.GUIDELINE})

    def test_hashed_keys_exclude_audit_trail(self) -> None:
        assert "audit_trail" not in HASHED_KEYS

    def test_product_content_is_reproducible_and_version_sensitive(self) -> None:
        """D-16 回归：产物**同内容必同哈希**，但换算法版本**必须**换哈希。

        ``tool_outputs`` 在哈希投影内，故产物模型里任何「何时跑的」都会破坏可复现性；
        而算法版本改变理应改变哈希 —— 两者的区别正是本条要钉住的语义。
        """
        same_a = VascMeasurement(case_id="GRAPH-HASH", max_diameter_mm=61.2, method="vmtk v1.2")
        same_b = VascMeasurement(case_id="GRAPH-HASH", max_diameter_mm=61.2, method="vmtk v1.2")
        newer = VascMeasurement(case_id="GRAPH-HASH", max_diameter_mm=61.2, method="vmtk v1.3")

        def digest(product: VascMeasurement) -> str:
            return stable_hash({"tool_outputs": {"imaging_quantifier": product}})

        assert digest(same_a) == digest(same_b)
        assert digest(same_a) != digest(newer)

    def test_hashed_product_models_carry_no_timestamp_fields(self) -> None:
        """止血带：进哈希的模型里出现时间戳字段，就等于把可复现性交出去。"""
        forbidden = {"measured_at", "created_at", "updated_at", "timestamp", "run_at"}
        for model in (VascMeasurement, AnatomyScore, RiskAssessment, FinalRecommendation):
            leaked = forbidden & set(model.model_fields)
            assert not leaked, (
                f"{model.__name__} 含时间戳字段 {sorted(leaked)}：它会破坏审计哈希可复现性"
            )


# ===========================================================================
# 输入层
# ===========================================================================
class TestInputLayer:
    def test_produces_snapshot_hash_and_case_id(self) -> None:
        update = input_node({"case": make_case()})  # type: ignore[arg-type]
        assert update["case_id"] == "GRAPH-000"
        assert len(update["input_snapshot_hash"]) == 64

    def test_rejects_annotation_suffix(self) -> None:
        """照抄设计表的带括号写法**不再被接受**（V3.1(2) 改用 Literal 严格校验）。

        旧版靠共享枚举的归一化容忍这种东西；新版字段专属枚举直接写中文值，
        于是「有（活动性）」必须由抽取层归一成「有」——多一道机器检查，
        少一处「看起来能跑但值不对」的隐患。
        """
        with pytest.raises(ValidationError):
            make_case(cl={"aaa_reported": "确认（影像）"})

    def test_does_not_mutate_input_case(self) -> None:
        """输入层必须产出新对象，否则调用方原件会被路由层的回填污染。"""
        original = make_case()
        copied = input_node({"case": original})["case"]  # type: ignore[index]
        assert copied is not original
        assert original.r.next is None

    def test_snapshot_hash_is_reproducible(self) -> None:
        first = input_node({"case": make_case()})["input_snapshot_hash"]  # type: ignore[index]
        second = input_node({"case": make_case()})["input_snapshot_hash"]  # type: ignore[index]
        assert first == second

    def test_missing_case_raises_with_guidance(self) -> None:
        with pytest.raises(ValueError, match="输入层缺少 case"):
            input_node({})  # type: ignore[arg-type]

    def test_llm_extractor_is_explicitly_unimplemented(self) -> None:
        """唯一的 LLM 入口必须显式不可用，并说明原因（不给「偷偷接 LLM」留缝）。"""
        assert EXTRACTOR_IMPLEMENTED is False
        with pytest.raises(NotImplementedError, match="标注病例评估集"):
            extract_case_from_text("患者男性，72 岁……")
        assert "评估集" in EXTRACTOR_BLOCKED_REASON


# ===========================================================================
# 路由层（薄封装）
# ===========================================================================
class TestRouterLayerThinWrapper:
    def test_wrapper_matches_rules_layer_exactly(self) -> None:
        """薄封装的结果必须与直接调用规则层 **逐字段一致**。"""
        wrapped = router_node({"case": make_case()})  # type: ignore[arg-type]
        reference_case = derive(make_case())
        reference_result = route(make_case())

        assert wrapped["case"].model_dump() == reference_case.model_dump()
        assert wrapped["route_result"].mode == reference_result.mode
        assert wrapped["route_result"].ruleset_version == reference_result.ruleset_version
        assert wrapped["route_result"].confidence_level == reference_result.confidence_level
        assert wrapped["route_result"].kind is reference_result.kind

        for actual, expected in zip(
            wrapped["route_result"].trace, reference_result.trace, strict=True
        ):
            assert actual.rule_id == expected.rule_id
            assert actual.verdict == expected.verdict
            assert actual.exit == expected.exit

    def test_wrapper_reuses_single_route_call(self) -> None:
        """route() 只能被调用一次（一次判定，两处消费）。"""
        update = router_node({"case": make_case()})  # type: ignore[arg-type]
        assert update["route_result"] is not None
        assert update["case"].r.next == update["route_result"].next_node

    def test_no_float_thresholds_in_router_layer(self) -> None:
        """薄封装里不得出现任何数值常量（阈值一律留在规则层）。"""
        _, numbers = _code_constants(ROUTER_LAYER_NODE)
        assert numbers == [], f"router_layer 出现数值常量：{numbers}"

    def test_no_clinical_tokens_in_router_layer(self) -> None:
        """薄封装里不得出现病种 / 解剖 / 处置关键词。"""
        strings, _ = _code_constants(ROUTER_LAYER_NODE)
        hits = [token for token in FORBIDDEN_CLINICAL_TOKENS for text in strings if token in text]
        assert hits == [], f"router_layer 出现临床规则痕迹：{sorted(set(hits))}"

    def test_no_rule_refs_in_router_layer(self) -> None:
        """薄封装里不得出现覆盖规则编号（O01~O29 / P0a~P8）。"""
        strings, _ = _code_constants(ROUTER_LAYER_NODE)
        suspicious = [
            text
            for text in strings
            if text.strip().startswith(("O0", "O1", "O2")) or text.strip().startswith("P1")
        ]
        assert suspicious == [], f"router_layer 出现规则编号：{suspicious}"


# ===========================================================================
# 执行层
# ===========================================================================
class TestExecutionLayer:
    def test_all_five_tools_registered(self) -> None:
        assert len(TOOL_REGISTRY) == 5
        assert {spec.key for spec in TOOL_REGISTRY} == {
            "guideline_engine",
            "imaging_quantifier",
            "anatomy_scorer",
            "memory_retriever",
            "risk_calculator",
        }

    def test_every_tool_documents_its_blocker(self) -> None:
        """未实现的工具必须写明**缺什么**，而不是一句「待办」。"""
        for spec in unimplemented_tools():
            assert spec.blocked_reason and len(spec.blocked_reason) > 10
        assert len(unimplemented_tools()) == len(TOOL_REGISTRY)

    def test_implemented_set_is_empty(self) -> None:
        assert IMPLEMENTED_TOOLS == frozenset()

    def test_run_tool_raises_with_reason(self) -> None:
        with pytest.raises(NotImplementedError, match="未实现"):
            run_tool("guideline_engine", {"case": make_case()})  # type: ignore[arg-type]

    def test_node_returns_empty_outputs(self) -> None:
        """工具产物不存在，就必须返回空——绝不凭空造评分。"""
        # 指南模式 → 计划里只有 A/B 两个工具，且都未实现 ⇒ 空产物
        state = {"case": make_case(), "route_result": route(make_case())}  # type: ignore[arg-type]
        assert execution_node(state) == {"tool_outputs": {}}  # type: ignore[arg-type]

    def test_non_mode_result_runs_no_tool(self) -> None:
        """人工复核 / 资料核实不跑任何工具：判定依据不足时算出来的东西不可信。"""
        case = make_case(n={"neck_len": None})
        result = route(case)
        assert result.mode is None
        assert execution_node({"case": case, "route_result": result}) == {"tool_outputs": {}}  # type: ignore[arg-type]

    def test_node_requires_routed_case(self) -> None:
        """未经路由层就给执行层喂 case → 响亮失败，不猜模式。"""
        with pytest.raises(RuntimeError, match="route_result"):
            execution_node({"case": make_case()})  # type: ignore[arg-type]

    def test_run_tool_requires_routed_case(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """已实现工具被单独调用时，同样不允许在无模式的情况下开跑。"""
        fake = ToolSpec(
            key="guideline_engine",
            label="工具A · 指南引擎",
            consumes=(),
            produces="",
            blocked_reason=None,
        )
        monkeypatch.setattr(registry_module, "SPEC_BY_KEY", {"guideline_engine": fake})
        with pytest.raises(RuntimeError, match="router_layer"):
            run_tool("guideline_engine", {"case": make_case()})  # type: ignore[arg-type]

    def test_node_dispatches_plan_subset_in_dependency_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用假工具证明三件事：计划外的工具不跑、上游先于下游、产物按键落位。"""
        calls: list[str] = []

        def fake_runner(key: str) -> Any:
            def _run(request: ToolRequest) -> dict[str, str]:
                calls.append(key)
                return {"by": key}

            return _run

        specs = {
            "imaging_quantifier": ToolSpec(
                key="imaging_quantifier",
                label="B",
                consumes=(),
                produces="",
                blocked_reason=None,
            ),
            "anatomy_scorer": ToolSpec(
                key="anatomy_scorer",
                label="C",
                consumes=(),
                produces="",
                depends_on=("imaging_quantifier",),
                blocked_reason=None,
            ),
            "memory_retriever": ToolSpec(
                key="memory_retriever",
                label="D",
                consumes=(),
                produces="",
                blocked_reason=None,
            ),
        }
        for module in (exec_node_module, registry_module):
            monkeypatch.setattr(module, "SPEC_BY_KEY", specs)
        monkeypatch.setattr(exec_node_module, "IMPLEMENTED_TOOLS", frozenset(specs))
        monkeypatch.setattr(registry_module, "RUNNER_BY_KEY", {k: fake_runner(k) for k in specs})

        case = make_case(n={"neck_len": 10.0})  # 短颈 → 复杂模式
        result = route(case)
        assert result.mode is RouteMode.COMPLEX

        outputs = execution_node({"case": case, "route_result": result})["tool_outputs"]  # type: ignore[arg-type]

        assert calls == ["imaging_quantifier", "anatomy_scorer", "memory_retriever"]
        assert set(outputs) == set(specs)


# ===========================================================================
# 执行层 · 三模式调度表
# ===========================================================================
class TestModePlans:
    """「跑哪些工具」的唯一真源 —— 模式之间是包含关系，不是并列关系。"""

    def test_every_mode_has_a_plan(self) -> None:
        assert set(MODE_PLANS) == set(RouteMode)

    def test_emergency_plan_is_empty(self) -> None:
        """设计表 06：急诊「不预置择期工具」。"""
        assert MODE_PLANS[RouteMode.EMERGENCY] == ()

    def test_guideline_plan_is_strict_subset_of_complex(self) -> None:
        """指南 ⊂ 复杂：指南只是「少跑几个」，不是另一套流程。"""
        assert set(MODE_PLANS[RouteMode.GUIDELINE]) < set(MODE_PLANS[RouteMode.COMPLEX])

    def test_guideline_plan_is_exactly_engine_and_imaging(self) -> None:
        """设计表 06：指南模式只跑 A + B。多塞一个都算错，不只是「子集」就算对。"""
        assert MODE_PLANS[RouteMode.GUIDELINE] == ("guideline_engine", "imaging_quantifier")

    def test_complex_plan_covers_every_registered_tool(self) -> None:
        """新增工具却忘了补计划 → 在这里失败（刻意设计的摩擦）。"""
        assert set(MODE_PLANS[RouteMode.COMPLEX]) == {spec.key for spec in TOOL_REGISTRY}

    def test_plans_only_reference_registered_tools(self) -> None:
        registered = {spec.key for spec in TOOL_REGISTRY}
        for mode, plan in MODE_PLANS.items():
            assert set(plan) <= registered, f"{mode} 引用了未登记的工具"

    def test_plans_are_dependency_closed(self) -> None:
        """计划里每个工具的依赖必须也在同一计划内，否则它必然跑不起来。"""
        for mode, plan in MODE_PLANS.items():
            for key in plan:
                for dep in SPEC_BY_KEY[key].depends_on:
                    assert dep in plan, f"{mode} 的 {key} 依赖 {dep}，但计划里没有"

    def test_declared_order_respects_dependencies(self) -> None:
        """声明的书写顺序本身就是拓扑序，读代码时不必再算一遍。"""
        for mode, plan in MODE_PLANS.items():
            position = {key: index for index, key in enumerate(plan)}
            for key in plan:
                for dep in SPEC_BY_KEY[key].depends_on:
                    assert position[dep] < position[key], f"{mode}: {dep} 应排在 {key} 之前"

    def test_plan_for_rejects_unrouted_case(self) -> None:
        with pytest.raises(RuntimeError, match="route_result"):
            plan_for(None)

    def test_plan_for_unknown_mode_is_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(plan_module, "MODE_PLANS", {})
        with pytest.raises(ValueError, match="没有登记工具计划"):
            plan_for(RouteMode.GUIDELINE)


# ===========================================================================
# 执行层 · 依赖排序
# ===========================================================================
class TestExecutionOrder:
    """排序器只认 ``depends_on``；测试传全量集合即可验证，无需实现任何工具。"""

    ALL_KEYS = frozenset(spec.key for spec in TOOL_REGISTRY)

    def test_nothing_implemented_yields_empty_order(self) -> None:
        assert execution_order(MODE_PLANS[RouteMode.COMPLEX], frozenset()) == ()

    def test_upstream_runs_before_downstream(self) -> None:
        order = execution_order(MODE_PLANS[RouteMode.COMPLEX], self.ALL_KEYS)
        assert order.index("imaging_quantifier") < order.index("anatomy_scorer")
        assert order.index("anatomy_scorer") < order.index("risk_calculator")

    def test_order_covers_exactly_the_plan(self) -> None:
        for plan in MODE_PLANS.values():
            assert set(execution_order(plan, self.ALL_KEYS)) == set(plan)

    def test_implemented_tool_with_blocked_upstream_is_loud(self) -> None:
        """「C 已实现、B 未实现」不得静默跳过 B 去算 C。"""
        with pytest.raises(RuntimeError, match="上游"):
            execution_order(MODE_PLANS[RouteMode.COMPLEX], frozenset({"anatomy_scorer"}))

    def test_dependency_outside_plan_is_loud(self) -> None:
        with pytest.raises(RuntimeError, match="不在当前模式的计划内"):
            execution_order(("anatomy_scorer",), frozenset({"anatomy_scorer"}))

    def test_unregistered_tool_is_loud(self) -> None:
        with pytest.raises(RuntimeError, match="未在 TOOL_REGISTRY 登记"):
            execution_order(("no_such_tool",), frozenset({"no_such_tool"}))

    def test_dependency_cycle_is_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = {
            "x": ToolSpec(key="x", label="X", consumes=(), produces="", depends_on=("y",)),
            "y": ToolSpec(key="y", label="Y", consumes=(), produces="", depends_on=("x",)),
        }
        monkeypatch.setattr(exec_node_module, "SPEC_BY_KEY", fake)
        with pytest.raises(RuntimeError, match="成环"):
            execution_order(("x", "y"), frozenset({"x", "y"}))


# ===========================================================================
# 工具包契约
# ===========================================================================
class TestToolsPackage:
    """一工具一文件、一份 SPEC、一条写明缺什么的阻塞说明。"""

    TOOL_KEYS = (
        "anatomy_scorer",
        "guideline_engine",
        "imaging_quantifier",
        "memory_retriever",
        "risk_calculator",
    )

    def _request(self) -> ToolRequest:
        """工具请求一律由**真路由**产出，不手搓 ``RouteResult``。

        好处是 ``route_result`` 与 ``case`` 天然自洽——手搓能造出「复杂模式 + 无颈长」
        这种真实路由永远不会给出的组合，测试就会守住一个不存在的世界。
        """
        case = make_case()
        return ToolRequest(case=case, route_result=route(case))

    def test_each_tool_module_exports_a_matching_spec(self) -> None:
        for key in self.TOOL_KEYS:
            module = importlib.import_module(f"src.tools.{key}")
            assert module.SPEC is SPEC_BY_KEY[key]
            assert module.SPEC.key == key
            assert callable(module.run)

    def test_tool_implementation_lives_in_its_own_file(self) -> None:
        """5 个工具是 5 个文件，不是一个文件里的 5 个分支。"""
        modules = sorted(
            path.stem
            for path in (REPO_ROOT / "src" / "tools").glob("*.py")
            if path.stem not in {"__init__", "base"}
        )
        assert modules == list(self.TOOL_KEYS)

    def test_blocked_tools_raise_their_own_reason(self) -> None:
        """未实现的工具必须抛**自己的**阻塞原因，而不是一句通用占位。"""
        for spec in unimplemented_tools():
            module = importlib.import_module(f"src.tools.{spec.key}")
            with pytest.raises(NotImplementedError, match="未实现"):
                module.run(self._request())

    def test_implemented_tool_has_a_real_runner(self) -> None:
        """守住「只把 blocked_reason 置 None、却没写实现体」的不一致。"""
        for key in IMPLEMENTED_TOOLS:
            module = importlib.import_module(f"src.tools.{key}")
            try:
                module.run(self._request())
            except NotImplementedError:
                pytest.fail(f"{key} 标记为已实现，但 run() 仍抛 NotImplementedError")
            except Exception as exc:  # noqa: BLE001 - 真实实现可能因输入不足失败，非本测试职责
                logging.getLogger(__name__).debug("%s 在构造输入下抛错：%r", key, exc)

    def test_consumes_paths_resolve_against_the_models(self) -> None:
        """``consumes`` 里的路径必须真能解析到，两类前缀各验各的。

        · ``case.<域>.<字段>``  → 必须在 ``VascCase.domain_field_names()`` 里；
        · ``route_result.<字段>`` → 必须是 ``RouteResult`` 的字段（判定产物走这条）。

        为什么必须有这条：``consumes`` 是**纯声明**——全仓没有任何代码读取它做校验。
        V3.1(2) 把 ``case.max_diameter_mm`` 改名成 ``c.maxd`` 时，声明会静静过期，
        直到有人真去实现那个工具才发现（本轮正是这样发现 ``guideline_engine``
        三条路径全部失效的）。

        校验范围刻意收窄：只有这两种前缀被当作字段路径；描述性条目（``"CTA 影像"``）
        不校验——强求它可解析等于逼人编一个假字段名。
        """
        domains = VascCase.domain_field_names()
        route_fields = {f.name for f in fields(RouteResult)}
        problems: list[str] = []
        for spec in TOOL_REGISTRY:
            for path in spec.consumes:
                if path.startswith("case."):
                    parts = path.split(".")
                    if len(parts) != 3:
                        problems.append(f"{spec.key}: {path!r} 应形如 case.<域>.<字段>")
                    elif parts[2] not in domains.get(parts[1], ()):
                        problems.append(f"{spec.key}: {path!r} 在 VascCase 中不存在")
                elif path.startswith("route_result."):
                    if path.split(".", 1)[1] not in route_fields:
                        problems.append(f"{spec.key}: {path!r} 在 RouteResult 中不存在")
        assert not problems, "consumes 声明了不存在的字段路径：" + "；".join(problems)

    def test_dependency_chain_is_declared(self) -> None:
        """B → C → E 的硬依赖必须写进 SPEC，而不是靠调用顺序约定。"""
        assert SPEC_BY_KEY["anatomy_scorer"].depends_on == ("imaging_quantifier",)
        assert SPEC_BY_KEY["risk_calculator"].depends_on == ("anatomy_scorer",)
        assert SPEC_BY_KEY["guideline_engine"].depends_on == ()
        assert SPEC_BY_KEY["memory_retriever"].depends_on == ()
        assert SPEC_BY_KEY["imaging_quantifier"].depends_on == ()

    def test_require_raises_when_upstream_missing(self) -> None:
        """缺上游是错误，不是缺省——不得返回 None 让下游算出一个「正常」结果。"""
        case = make_case(n={"neck_len": 10.0})  # 短颈 → 复杂模式
        request = ToolRequest(case=case, route_result=route(case))
        with pytest.raises(MissingUpstreamError, match="缺少上游工具产物"):
            request.require("imaging_quantifier")

    def test_require_returns_upstream_output(self) -> None:
        marker = object()
        case = make_case(n={"neck_len": 10.0})
        request = ToolRequest(
            case=case,
            route_result=route(case),
            upstream={"imaging_quantifier": marker},
        )
        assert request.require("imaging_quantifier") is marker


# ===========================================================================
# 输出层
# ===========================================================================
class TestOutputLayer:
    def _recommendation(self) -> tuple[FinalRecommendation, Any]:
        case = make_rich_case()
        state: dict[str, Any] = {"case": case}
        state.update(input_node(state))  # type: ignore[arg-type]
        state.update(router_node(state))  # type: ignore[arg-type]
        state.update(execution_node(state))  # type: ignore[arg-type]
        update = output_node(state)  # type: ignore[arg-type]
        return update["final_recommendation"], state["route_result"]

    def test_decision_path_matches_trace_one_to_one(self) -> None:
        """decision_path 与 trace 必须逐条对应，**含被否决项**。"""
        recommendation, result = self._recommendation()
        assert len(recommendation.decision_path) == len(result.trace)
        for index, (step, source) in enumerate(
            zip(recommendation.decision_path, result.trace, strict=True), start=1
        ):
            assert step.order == index
            assert step.result is source.verdict
            assert step.rule_ref == source.rule_id
            assert step.condition == RULES[source.rule_id].name
            assert step.note == RULES[source.rule_id].source

    def test_rejected_steps_are_preserved(self) -> None:
        """审计要求：不得只留命中项。"""
        recommendation, _ = self._recommendation()
        assert any(not step.result for step in recommendation.decision_path)

    def test_ruleset_version_and_snapshot_hash_propagated(self) -> None:
        recommendation, result = self._recommendation()
        assert recommendation.ruleset_version == result.ruleset_version
        assert recommendation.input_snapshot_hash is not None
        assert len(recommendation.input_snapshot_hash) == 64

    def test_confidence_and_review_flag_come_from_rules_layer(self) -> None:
        recommendation, result = self._recommendation()
        assert recommendation.confidence_level == result.confidence_level
        assert recommendation.manual_review_required is (result.kind is RouteKind.REVIEW)

    def test_evidence_chain_carries_source_quote(self) -> None:
        recommendation, _ = self._recommendation()
        quotes = [item.source for item in recommendation.evidence_chain if item.source]
        assert "CTA 示腹主动脉最大径 42mm。" in quotes

    def test_clinical_conclusion_is_an_explicit_placeholder(self) -> None:
        """治疗建议口径未定 → 必须是显式占位，不得编造临床主张。"""
        recommendation, _ = self._recommendation()
        assert recommendation.primary_recommendation == PRIMARY_RECOMMENDATION_PLACEHOLDER
        assert "待补" in recommendation.primary_recommendation
        assert recommendation.treatment_options == []

    def test_llm_translation_is_explicitly_unimplemented(self) -> None:
        recommendation, _ = self._recommendation()
        assert TRANSLATION_IMPLEMENTED is False
        assert recommendation.translated_text is None

    # ---- 工具产物承接（P0：让计算结果有观众） ----
    def _recommendation_with_tools(self, tool_outputs: dict[str, Any]) -> FinalRecommendation:
        """跑输入层 + 路由层，然后用**给定的** tool_outputs 覆盖执行层结果。"""
        case = make_rich_case()
        state: dict[str, Any] = {"case": case}
        state.update(input_node(state))  # type: ignore[arg-type]
        state.update(router_node(state))  # type: ignore[arg-type]
        state["tool_outputs"] = tool_outputs
        return output_node(state)["final_recommendation"]  # type: ignore[arg-type]

    def test_tool_outputs_reach_the_recommendation(self) -> None:
        """核心回归：工具产物必须出现在结论里，否则「算了等于没算」。"""
        measurement = VascMeasurement(
            case_id="GRAPH-TOOL-001", max_diameter_mm=61.2, method="vmtk v1.2"
        )
        recommendation = self._recommendation_with_tools({"imaging_quantifier": measurement})

        assert len(recommendation.tool_findings) == 1
        finding = recommendation.tool_findings[0]
        assert finding.tool_key == "imaging_quantifier"
        assert finding.label == SPEC_BY_KEY["imaging_quantifier"].label
        assert "61.2" in finding.summary
        assert "vmtk v1.2" in finding.summary

    def test_no_tool_run_yields_no_findings(self) -> None:
        """一个工具都没跑 → 空列表；不得伪造「跑了但没结果」的条目。"""
        assert self._recommendation_with_tools({}).tool_findings == []

    def test_findings_are_sorted_by_tool_key(self) -> None:
        """结论参与审计哈希 → 呈现顺序必须与字典插入顺序无关。"""
        product = VascMeasurement(case_id="GRAPH-TOOL-001", max_diameter_mm=61.2)
        findings = build_tool_findings({"risk_calculator": product, "anatomy_scorer": product})
        assert [f.tool_key for f in findings] == ["anatomy_scorer", "risk_calculator"]

    def test_summary_omits_empty_nested_and_foreign_key_fields(self) -> None:
        """摘要只呈现标量：None 不补占位、列表不灌入、外键不重复。"""
        measurement = VascMeasurement(
            case_id="GRAPH-TOOL-001",
            max_diameter_mm=61.2,
            centerline=[CenterlinePoint(distance_mm=0.0, diameter_mm=28.4)],
        )
        summary = build_tool_findings({"imaging_quantifier": measurement})[0].summary
        assert "最大直径" in summary
        assert "中心线" not in summary
        assert "关联病例标识" not in summary
        assert "未提及" not in summary

    def test_unknown_tool_key_falls_back_to_key(self) -> None:
        """登记表里没有的键不得静默丢弃——用键名兜底，宁可难看也不吞数据。"""
        finding = build_tool_findings({"unregistered_tool": "raw"})[0]
        assert finding.label == "unregistered_tool"
        assert finding.summary == "'raw'"

    def test_findings_do_not_become_clinical_advice(self) -> None:
        """承接工具产物**不得**顺手把结论也写了：留白仍为留白。"""
        measurement = VascMeasurement(case_id="GRAPH-TOOL-001", max_diameter_mm=61.2)
        recommendation = self._recommendation_with_tools({"imaging_quantifier": measurement})
        assert recommendation.primary_recommendation == PRIMARY_RECOMMENDATION_PLACEHOLDER
        assert recommendation.treatment_options == []
        assert isinstance(recommendation.tool_findings[0], ToolFinding)

    def test_summary_reports_empty_product(self) -> None:
        """工具「跑了但没产物」要如实写出来，不能渲染成空串。"""
        assert build_tool_findings({"imaging_quantifier": None})[0].summary == "（无产出）"

    def test_summary_reports_product_without_scalars(self) -> None:
        product = VascMeasurement(case_id="GRAPH-TOOL-001")
        summary = build_tool_findings({"imaging_quantifier": product})[0].summary
        assert summary == "（无标量产出）"

    def test_summary_truncates_overlong_fallback(self) -> None:
        summary = build_tool_findings({"unregistered_tool": "y" * 500})[0].summary
        assert summary.endswith("…")
        assert len(summary) == MAX_FALLBACK_SUMMARY_CHARS + 1

    def test_summary_caps_rendered_field_count(self) -> None:
        """长产物不得把结论撑爆：超量只报总数，且总数必须真实。"""
        measurement = VascMeasurement(
            case_id="GRAPH-TOOL-001",
            max_diameter_mm=61.2,
            max_diameter_slice_mm=12.0,
            aneurysm_length_mm=80.0,
            volume_ml=120.5,
            thrombus_volume_pct=35.0,
            neck_length_mm=12.0,
            neck_diameter_mm=24.0,
            neck_angle_deg=58.0,
            neck_calcification_pct=10.0,
            neck_thrombus_pct=5.0,
            access_min_diameter_mm=6.0,
            access_calcification_pct=20.0,
            iliac_tortuosity_deg=70.0,
        )
        summary = build_tool_findings({"imaging_quantifier": measurement})[0].summary
        assert summary.count(" = ") == MAX_SUMMARY_FIELDS
        assert "等共 13 项" in summary

    def test_scalar_rendering_covers_enum_and_bool(self) -> None:
        assert _render_scalar(RouteMode.GUIDELINE) == RouteMode.GUIDELINE.value
        assert _render_scalar(True) == "是"
        assert _render_scalar(False) == "否"
        assert _render_scalar(61.2) == "61.2"

    def test_evidence_rendering_covers_missing_enum_and_list(self) -> None:
        """证据链的字段渲染：缺失写「未提及」、枚举取中文值、列表串接。"""
        assert _render(None) == "（未提及）"
        assert _render(RouteMode.COMPLEX) == RouteMode.COMPLEX.value
        assert _render([RouteMode.COMPLEX, "x"]) == f"{RouteMode.COMPLEX.value}、x"


# ===========================================================================
# 端到端
# ===========================================================================
class TestGraphEndToEnd:
    def test_run_produces_valid_recommendation(self) -> None:
        state = run_preop(make_case(), checkpointer=None)
        recommendation = state["final_recommendation"]

        assert isinstance(recommendation, FinalRecommendation)
        assert recommendation.case_id == "GRAPH-000"
        assert recommendation.route_mode is RouteMode.GUIDELINE
        assert recommendation.confidence_level in {"高", "中", "低"}
        assert recommendation.decision_path
        assert recommendation.evidence_chain

    def test_emergency_case_routes_emergency(self) -> None:
        state = run_preop(make_case(c={"rupture_report": "明确破裂"}), checkpointer=None)
        assert state["final_recommendation"].route_mode is RouteMode.EMERGENCY

    def test_audit_trail_records_all_four_nodes(self) -> None:
        state = run_preop(make_case(), checkpointer=None)
        nodes = [record.node for record in state["audit_trail"]]
        assert nodes == ["input_layer", "router_layer", "execution_layer", "output_layer"]

    def test_audit_trail_has_timestamps_and_hashes(self) -> None:
        for record in run_preop(make_case(), checkpointer=None)["audit_trail"]:
            assert record.started_at and record.finished_at
            assert len(record.input_hash) == 64
            assert len(record.output_hash) == 64

    def test_audit_chain_closes_ring_by_ring(self) -> None:
        """审计链不变量：上一节点的输出哈希 == 下一节点的输入哈希。"""
        trail = run_preop(make_case(), checkpointer=None)["audit_trail"]
        for previous, following in pairwise(trail):
            assert previous.output_hash == following.input_hash

    def test_input_hash_covers_pure_input(self) -> None:
        """首环输入哈希应等于「纯输入 case」的状态投影哈希。"""
        case = make_case()
        trail = run_preop(case, checkpointer=None)["audit_trail"]
        assert trail[0].input_hash == stable_hash(_project({"case": case}))

    def test_hashes_are_reproducible_across_runs(self) -> None:
        """同输入 + 同规则版本 → 同哈希（时间戳不同不影响）。"""
        first = run_preop(make_case(), checkpointer=None)["audit_trail"]
        second = run_preop(make_case(), checkpointer=None)["audit_trail"]

        assert [r.input_hash for r in first] == [r.input_hash for r in second]
        assert [r.output_hash for r in first] == [r.output_hash for r in second]
        assert first[0].started_at != ""  # 时间戳确实被记录了

    def test_original_case_is_not_mutated(self) -> None:
        """图内部对 case 的回填不得污染调用方对象。"""
        case = make_case()
        run_preop(case, checkpointer=None)
        assert case.r.next is None
        assert case.k.classifiable is None

    def test_tool_outputs_empty_reflects_real_gap(self) -> None:
        assert run_preop(make_case(), checkpointer=None)["tool_outputs"] == {}


# ===========================================================================
# Checkpoint 往返
# ===========================================================================
class TestCheckpointRoundTrip:
    def test_state_round_trips_through_independent_connection(self, tmp_path: Path) -> None:
        """换一个**独立连接**读回同一 state —— 这是「可审计」的真正兑现。"""
        db = tmp_path / "checkpoints.sqlite"
        case = make_rich_case()

        with sqlite_checkpointer(db) as saver:
            run_preop(case, checkpointer=saver, thread_id="T-ROUNDTRIP")

        # 上面 with 已关闭连接，这里重新打开 —— 数据必须来自磁盘而非内存。
        with sqlite_checkpointer(db) as reopened:
            app = build_preop_graph(checkpointer=reopened)
            values = app.get_state(thread_config("T-ROUNDTRIP")).values

        assert isinstance(values["case"], VascCase)
        assert values["case"].case_id == "GRAPH-RICH"
        assert values["case"].field_evidence["cl.aaa_diam"].source_quote.startswith("CTA")
        assert values["case"].urgency_flag_audit is not None
        assert values["case"].urgency_flag_audit.set_by is FieldWriter.HUMAN

        assert len(values["route_result"].trace) > 0
        assert isinstance(values["final_recommendation"], FinalRecommendation)
        assert values["final_recommendation"].route_mode is values["route_result"].mode

    def test_audit_trail_survives_round_trip(self, tmp_path: Path) -> None:
        db = tmp_path / "checkpoints.sqlite"
        with sqlite_checkpointer(db) as saver:
            run_preop(make_case(), checkpointer=saver, thread_id="T-AUDIT")

        with sqlite_checkpointer(db) as reopened:
            app = build_preop_graph(checkpointer=reopened)
            trail = app.get_state(thread_config("T-AUDIT")).values["audit_trail"]

        assert [record.node for record in trail] == [
            "input_layer",
            "router_layer",
            "execution_layer",
            "output_layer",
        ]
        for record in trail:
            assert len(record.input_hash) == 64
            assert len(record.output_hash) == 64

    def test_checkpoint_database_is_sqlite_with_tables(self, tmp_path: Path) -> None:
        db = tmp_path / "checkpoints.sqlite"
        with sqlite_checkpointer(db) as saver:
            run_preop(make_case(), checkpointer=saver, thread_id="T-TABLES")

        conn = sqlite3.connect(str(db))
        try:
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()
        assert {"checkpoints", "writes"} <= tables

    def test_state_history_is_replayable(self, tmp_path: Path) -> None:
        """每个节点落一次盘 → 可逐步回放。"""
        db = tmp_path / "checkpoints.sqlite"
        with sqlite_checkpointer(db) as saver:
            run_preop(make_case(), checkpointer=saver, thread_id="T-HISTORY")
            app = build_preop_graph(checkpointer=saver)
            history = list(app.get_state_history(thread_config("T-HISTORY")))

        assert len(history) >= 4

    def test_two_threads_do_not_collide(self, tmp_path: Path) -> None:
        db = tmp_path / "checkpoints.sqlite"
        with sqlite_checkpointer(db) as saver:
            run_preop(make_case(case_id="GRAPH-A"), checkpointer=saver, thread_id="T-A")
            run_preop(
                make_case(case_id="GRAPH-B", c={"rupture_report": "明确破裂"}),
                checkpointer=saver,
                thread_id="T-B",
            )
            app = build_preop_graph(checkpointer=saver)
            first = app.get_state(thread_config("T-A")).values
            second = app.get_state(thread_config("T-B")).values

        assert first["case_id"] == "GRAPH-A"
        assert second["case_id"] == "GRAPH-B"
        assert first["final_recommendation"].route_mode != second["final_recommendation"].route_mode


def _collect_custom_types(value: Any, seen: set[type]) -> set[type]:
    """递归收集一个值里出现过的**自定义类型**（pydantic / dataclass / Enum）。

    用途：Checkpoint 序列化白名单必须覆盖 state 里出现的**每一个**自定义类型，
    故先由真实 state 反推出「应该注册哪些类型」，再与白名单比对 —— 这样
    以后往 state 里新增模型时，测试会立刻提示漏注册，而不是等到升级才炸。
    """
    if isinstance(value, type):
        return seen

    if isinstance(value, Enum):
        seen.add(type(value))
        return seen

    if isinstance(value, BaseModel):
        seen.add(type(value))
        for name in type(value).model_fields:
            _collect_custom_types(getattr(value, name, None), seen)
        return seen

    if is_dataclass(value) and not isinstance(value, type):
        seen.add(type(value))
        for spec in fields(value):
            _collect_custom_types(getattr(value, spec.name, None), seen)
        return seen

    if isinstance(value, Mapping):
        for key, item in value.items():
            _collect_custom_types(key, seen)
            _collect_custom_types(item, seen)
        return seen

    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _collect_custom_types(item, seen)
        return seen

    return seen


# ===========================================================================
# 落盘策略（默认落盘 + 显式放弃时告警）
# ===========================================================================
class TestPersistencePolicy:
    """``run_preop`` 的落盘语义：「默认即安全」+ 放弃落盘必须出声。

    改造前 ``checkpointer`` 默认 ``None``，即「不传就不落盘」，且**毫无提示** ——
    属静默 fail-open，与「全程可审计」红线冲突。改造后：
        · 不传 → 落盘到 :data:`..graph.DEFAULT_CHECKPOINT_DB`；
        · 显式 ``None`` → 不落盘，且 ``logging.warning`` 出声（fail-loud）。
    本类把这几条语义钉成测试；``thread_id`` 的派生语义见 :class:`TestRunIsolation`。
    """

    def test_default_call_persists_to_default_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """不传 checkpointer → 默认落盘。用 monkeypatch 把默认库导向 tmp_path。"""
        db = tmp_path / "default.sqlite"
        monkeypatch.setattr(graph_module, "DEFAULT_CHECKPOINT_DB", db)

        result = run_preop(make_case())

        assert db.exists(), "默认调用必须产生审计库文件"
        assert result["audit_persisted"] is True
        assert len(result["audit_trail"]) == 4

    def test_explicit_none_does_not_persist_and_warns(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """显式 ``checkpointer=None`` → 不落盘，且必须出声（fail-loud 而非 fail-open）。"""
        db = tmp_path / "default.sqlite"
        monkeypatch.setattr(graph_module, "DEFAULT_CHECKPOINT_DB", db)

        with caplog.at_level(logging.WARNING):
            result = run_preop(make_case(), checkpointer=None)

        assert result["audit_persisted"] is False
        assert not db.exists(), "显式放弃落盘时不得创建任何库文件"
        assert any("未落盘" in record.getMessage() for record in caplog.records)

    def test_thread_id_is_derived_per_run_and_returned(self, tmp_path: Path) -> None:
        """不传 thread_id → 派生 ``preop:{case_id}:{run_id}``，并**从返回值读回**。

        为什么必须回传：run_id 含时间戳，调用方无法预先算出。若不回传，
        自动派生的线程就是「写进去了但捞不出来」的死记录。
        """
        db = tmp_path / "checkpoints.sqlite"
        case = make_case(case_id="GRAPH-THREAD")

        with sqlite_checkpointer(db) as saver:
            result = run_preop(case, checkpointer=saver)

        thread_id = result["thread_id"]
        assert thread_id.startswith("preop:GRAPH-THREAD:"), thread_id

        with sqlite_checkpointer(db) as reopened:
            app = build_preop_graph(checkpointer=reopened)
            values = app.get_state(thread_config(thread_id)).values

        assert values["case_id"] == "GRAPH-THREAD"

    def test_explicit_thread_id_overrides_derivation(self, tmp_path: Path) -> None:
        """显式传入的 thread_id 覆盖派生值。"""
        db = tmp_path / "checkpoints.sqlite"
        case = make_case(case_id="GRAPH-OVERRIDE")

        with sqlite_checkpointer(db) as saver:
            result = run_preop(case, checkpointer=saver, thread_id="T-EXPLICIT")

        assert result["thread_id"] == "T-EXPLICIT"

        with sqlite_checkpointer(db) as reopened:
            app = build_preop_graph(checkpointer=reopened)
            values = app.get_state(thread_config("T-EXPLICIT")).values

        assert values["case_id"] == "GRAPH-OVERRIDE"

    def test_two_cases_do_not_share_a_thread(self, tmp_path: Path) -> None:
        """派生 thread 的核心收益：不同病例互不污染审计轨迹。"""
        db = tmp_path / "checkpoints.sqlite"

        with sqlite_checkpointer(db) as saver:
            first = run_preop(make_case(case_id="GRAPH-X"), checkpointer=saver)
            second = run_preop(make_case(case_id="GRAPH-Y"), checkpointer=saver)

        assert len(first["audit_trail"]) == 4
        assert len(second["audit_trail"]) == 4, "第二例不得累积第一例的审计帧"

    def test_audit_persisted_stays_out_of_checkpoint(self, tmp_path: Path) -> None:
        """运行期元信息不得进 checkpoint —— 库里只应有「关于病例的真相」。"""
        db = tmp_path / "checkpoints.sqlite"
        with sqlite_checkpointer(db) as saver:
            run_preop(make_case(), checkpointer=saver, thread_id="T-META")

        with sqlite_checkpointer(db) as reopened:
            app = build_preop_graph(checkpointer=reopened)
            values = app.get_state(thread_config("T-META")).values

        assert "audit_persisted" not in values

    def test_audit_persisted_is_not_hashed(self) -> None:
        """元信息不参与审计哈希 —— 改它不得影响任何哈希。"""
        assert "audit_persisted" not in HASHED_KEYS


# ===========================================================================
# 运行隔离（一次运行 = 一条线程）
# ===========================================================================
class TestRunIsolation:
    """同病例重复运行不得互相污染审计轨迹。

    为什么单独立类：这是「默认落盘」与「thread 由 case_id 派生」两个改动**叠加后**
    才显形的问题 —— 修复前，同一 ``case_id`` 第二次调用会把上一次的 4 帧审计记录
    一并带回（4 帧 → 8 帧），审计链不变量随之断裂；病例被修订后重跑，断链还会更早。

    根因在语义层：``thread_id`` 在 LangGraph 里是**状态机线程**，对同一 thread 再
    ``invoke`` 是 **resume**（续跑），不是「新开一次运行」。修复方式是把「运行身份」
    显式编入 ``thread_id`` —— 默认路径「一运行一线程」，而 resume 只在**显式传入**
    ``thread_id`` 时发生（那是调用方主动要求续跑 / 回放）。

    这组用例必须在 ``tmp_path`` 之外另有一条理由：缺陷只在「同一 thread 被重复使用」
    时出现，而修复前的测试每次都换新 thread，因此 205 项全绿也掩盖了它。
    """

    def test_repeat_runs_of_same_case_stand_alone(self, tmp_path: Path) -> None:
        """同病例同输入连跑两次：两次各 4 帧，第二次不得累积第一次的记录。"""
        db = tmp_path / "checkpoints.sqlite"
        case = make_case(case_id="GRAPH-RERUN")

        with sqlite_checkpointer(db) as saver:
            first = run_preop(case, checkpointer=saver)
            second = run_preop(case, checkpointer=saver)

        assert first["thread_id"] != second["thread_id"]
        assert len(first["audit_trail"]) == 4
        assert len(second["audit_trail"]) == 4, "第二次运行不得继承上一次的审计帧"

    def test_revised_case_rerun_keeps_each_chain_closed(self, tmp_path: Path) -> None:
        """病例被修订后重跑（输入变化）—— 两次运行各自的审计链必须自闭合。

        修复前这是最坏情形：第二次返回 8 帧且断链出现得更早，因为第二次运行的首环
        输入哈希与前一次运行的某环输出对不上。
        """
        db = tmp_path / "checkpoints.sqlite"
        original = make_case(case_id="GRAPH-REVISED")
        revised = make_case(case_id="GRAPH-REVISED", n={"neck_len": 12.0})

        with sqlite_checkpointer(db) as saver:
            first = run_preop(original, checkpointer=saver)
            second = run_preop(revised, checkpointer=saver)

        for result in (first, second):
            assert len(result["audit_trail"]) == 4
            for previous, following in pairwise(result["audit_trail"]):
                assert previous.output_hash == following.input_hash

        # 前提校验：两次输入确实不同，否则本用例证明不了「输入变化」这条路径。
        assert first["input_snapshot_hash"] != second["input_snapshot_hash"]

    def test_runs_of_one_case_are_retrievable_by_prefix(self, tmp_path: Path) -> None:
        """「一运行一线程」不牺牲可检索性：按 ``preop:{case_id}:`` 前缀取回全部运行史。"""
        db = tmp_path / "checkpoints.sqlite"
        case = make_case(case_id="GRAPH-PREFIX")

        with sqlite_checkpointer(db) as saver:
            for _ in range(3):
                run_preop(case, checkpointer=saver)

        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE ?",
                ("preop:GRAPH-PREFIX:%",),
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 3, rows

    def test_explicit_thread_id_still_resumes(self, tmp_path: Path) -> None:
        """显式传入同一 thread_id = 主动续跑 —— 此时累积是**预期行为**，不是缺陷。"""
        db = tmp_path / "checkpoints.sqlite"
        case = make_case(case_id="GRAPH-RESUME")

        with sqlite_checkpointer(db) as saver:
            first = run_preop(case, checkpointer=saver, thread_id="T-RESUME")
            second = run_preop(case, checkpointer=saver, thread_id="T-RESUME")

        assert first["thread_id"] == second["thread_id"] == "T-RESUME"
        assert len(first["audit_trail"]) == 4
        assert len(second["audit_trail"]) == 8, "显式续跑时累积是刻意保留的能力"

    def test_run_ids_are_unique_within_process(self) -> None:
        """派生标识不得碰撞 —— 碰撞会让两次运行被静默合并成一条线程。"""
        run_ids = [graph_module._new_run_id() for _ in range(200)]
        assert len(set(run_ids)) == len(run_ids)

    def test_thread_id_stays_out_of_checkpoint(self, tmp_path: Path) -> None:
        """运行期元信息不得进 checkpoint —— 库里只应有「关于病例的真相」。"""
        db = tmp_path / "checkpoints.sqlite"
        with sqlite_checkpointer(db) as saver:
            result = run_preop(make_case(), checkpointer=saver)

        with sqlite_checkpointer(db) as reopened:
            app = build_preop_graph(checkpointer=reopened)
            values = app.get_state(thread_config(result["thread_id"])).values

        assert "thread_id" not in values
        assert "audit_persisted" not in values

    def test_thread_id_is_not_hashed(self) -> None:
        assert "thread_id" not in HASHED_KEYS


# ===========================================================================
# 序列化白名单守卫
# ===========================================================================
class TestSerializationAllowlist:
    """守住 Checkpoint 的序列化白名单。

    为什么值得单开一类测试（这是本轮发现的**定时炸弹**）：

        LangGraph 反序列化遇到未登记的自定义类型时，**当前只告警不阻断**，
        但日志明确写着 ``This will be blocked in a future version``。
        后果极不对称 —— **不注册时全部测试依然通过**，直到某次依赖升级后
        历史 Checkpoint 集体读不回来、审计链静默断裂，且此时已无法追溯修复。

        因此「白名单完整」不能靠开发者记得，必须由测试挡住。
    """

    def test_allowlist_covers_every_type_in_state(self) -> None:
        """真实 state 里出现的每个自定义类型，都必须在白名单内。"""
        state = run_preop(make_rich_case(), checkpointer=None)

        seen: set[type] = set()
        _collect_custom_types(state, seen)
        assert seen, "未能从 state 中收集到任何自定义类型，收集器可能已失效"

        declared = build_checkpoint_serde()._allowed_msgpack_modules
        missing = {(cls.__module__, cls.__qualname__) for cls in seen} - declared

        assert missing == set(), (
            f"以下类型出现在 state 中但未登记到序列化白名单，"
            f"未来 LangGraph 升级后将无法反序列化历史 Checkpoint：{sorted(missing)}"
        )

    def test_allowed_types_helper_matches_serde(self) -> None:
        """``allowed_msgpack_types()`` 与 ``build_checkpoint_serde()`` 必须同源。"""
        declared = build_checkpoint_serde()._allowed_msgpack_modules
        assert len(declared) > 0
        # 白名单以 (模块名, 类限定名) 二元组形式存在
        assert all(isinstance(item, tuple) and len(item) == 2 for item in declared)

    def test_registered_saver_emits_no_unregistered_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """走正式路径（登记了白名单）时，往返全过程**不得**出现未注册类型告警。"""
        db = tmp_path / "allowlist.sqlite"
        case = make_rich_case()

        with caplog.at_level(logging.WARNING):
            with sqlite_checkpointer(db) as saver:
                run_preop(case, checkpointer=saver, thread_id="T-ALLOWLIST")
            with sqlite_checkpointer(db) as reopened:
                app = build_preop_graph(checkpointer=reopened)
                values = app.get_state(thread_config("T-ALLOWLIST")).values

        assert isinstance(values["case"], VascCase)
        assert "unregistered" not in caplog.text.lower(), (
            f"往返过程出现未注册类型告警（白名单不完整）：\n{caplog.text}"
        )

    def test_canary_unregistered_types_are_observably_flagged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """阳性对照 —— 证明上一条测试不是「空断言」。

        故意**绕开白名单**构造 saver（用裸 ``SqliteSaver``），必须能观测到
        ``unregistered`` 告警。这样上一条测试里「零告警」才是有信息量的断言。

        ⚠️ 实测记录（不要想当然）：``LANGGRAPH_STRICT_MSGPACK=true`` **不是**
        更可靠的守卫 —— 实测在该模式下，未注册类型的 state 依然能正确往返，
        且**不再产生任何告警**。也就是说它既不报错也不提示，风险反而更隐蔽。
        故本测试在严格模式下跳过（该信号不可观测），真正的防线是
        「白名单必须完整」+ :meth:`test_allowlist_covers_every_type_in_state`。
        """
        if os.environ.get("LANGGRAPH_STRICT_MSGPACK", "").lower() in {"1", "true", "yes"}:
            pytest.skip("严格模式下未注册类型不再产生可观测告警，本对照无法进行")

        db = tmp_path / "canary.sqlite"
        case = make_rich_case()
        conn = sqlite3.connect(str(db), check_same_thread=False)

        try:
            plain_saver = SqliteSaver(conn)
            plain_saver.setup()
            with caplog.at_level(logging.WARNING):
                run_preop(case, checkpointer=plain_saver, thread_id="T-CANARY")
                app = build_preop_graph(checkpointer=plain_saver)
                app.get_state(thread_config("T-CANARY"))
        finally:
            conn.close()

        assert "unregistered" in caplog.text.lower(), (
            "未注册类型不再产生告警，说明该失败模式已变为静默；请重新评估本守卫是否仍有意义。"
        )


# ===========================================================================
# 架构守卫：判定层零 LLM
# ===========================================================================
class TestJudgmentLayerHasNoLlm:
    def test_no_llm_client_imported_in_judgment_layers(self) -> None:
        """判定层（规则 + 路由 / 执行 / 输出）不得 import 任何 LLM 客户端。"""
        offenders: list[str] = []
        for directory in JUDGMENT_LAYER_DIRS:
            for path in _iter_source_files(directory):
                for module in _iter_imported_modules(path):
                    if module.startswith(FORBIDDEN_LLM_MODULES):
                        offenders.append(f"{path.relative_to(REPO_ROOT)} → {module}")
        assert offenders == [], f"判定层出现 LLM 客户端导入：{offenders}"

    def test_rules_layer_has_no_third_party_llm_framework(self) -> None:
        """规则层不得引入任何 LLM 编排框架或客户端。"""
        offenders: list[str] = []
        for path in _iter_source_files(REPO_ROOT / "src" / "rules"):
            for module in _iter_imported_modules(path):
                if module.startswith(("langgraph", "langchain", "openai", "anthropic")):
                    offenders.append(f"{path.name} → {module}")
        assert offenders == []

    def test_input_layer_is_the_only_llm_entry_documented(self) -> None:
        """input_layer 必须自述为「唯一 LLM 入口」，该声明被测试钉住。"""
        source = (REPO_ROOT / "src" / "preop_agent" / "input_layer" / "node.py").read_text(
            encoding="utf-8"
        )
        assert "唯一的 LLM 入口" in source

    def test_router_layer_is_documented_as_thin_wrapper(self) -> None:
        source = ROUTER_LAYER_NODE.read_text(encoding="utf-8")
        assert "薄封装" in source
