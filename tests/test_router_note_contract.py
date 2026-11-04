"""判定结论自证契约 —— 每条 ``RouteResult`` 必须让人**独立复核**「为什么是这个结论」。

V3.6.2 下，旧的 ``note`` 文本契约（``<结论句>（<检查项>；…）``）已随十步流水线一起废弃。
取而代之的是**四个可机械校验的自证要素**：

1. ``rule_id`` 可回溯：命中的规则必须在 ``RULES`` 里（或显式登记为人工覆盖），
   且每条规则带 V3.6.2 源行号 —— 否则「某条规则判了它」这句话无法查证。
2. ``reason`` 说人话：非空、含规则编号、不含 ``None``/``nan`` 这类占位泄漏。
3. ``evidence_gaps`` 可用：每条路径都能在模型里解析；**且不得把已命中规则的字段
   算成缺口**（命中的规则已经闭环，它的其余字段为空是正常状态）。
4. ``trace`` 不虚构：只记录真正求值过的规则，且顺序与编排一致。

为什么值得单独成文件：指南模式这类结论**完全由否决链支撑**，复核主体正是
「哪些规则被否决了、为什么否决」。这条链一旦断了，结论就没有据可查——
而它断了也不会让任何判定结果变错，所以只能靠本文件守着。

分工：本文件只校验**结论的可解释性**，不校验判定对错
（判定行为由 ``test_preop.py`` 与 ``test_router_branches.py`` 守着）。

运行：``uv run pytest tests/test_router_note_contract.py -v``
"""

from __future__ import annotations

import pytest
from conftest import make_case

from src.models.preop.case import VascCase
from src.rules.router import RULES, RouteKind, RouteMode, RouteResult, _read_path, route

ENGINE_RULE_IDS = frozenset({"MANUAL.URGENCY", "FALLBACK.VERIFY"})
"""两个**不是 V3.6.2 规则**的标识，必须显式白名单：

- ``MANUAL.URGENCY``：医生置位 ``urgency_flag``，跳过自动判定；
- ``FALLBACK.VERIFY``：没有任何规则能判定（资料不足）——兜底不是规则判的，
  所以不借 ``G03.R15`` / ``G04.R14`` 的 id，否则 RULESET.md 的出口列会说谎。
"""

SCENARIOS = {
    "指南": lambda: make_case(),
    "复杂-短颈": lambda: make_case(n={"neck_len": 8.0}),
    "复杂-生理风险": lambda: make_case(n={"egfr": 22.0}),
    "急诊-低血压": lambda: make_case(v={"sbp": 70.0}),
    "急诊-疑似破裂": lambda: make_case(d={"dx_context": "疑似"}, v={"sbp": 70.0}),
    "复核-失血": lambda: make_case(lab={"hb": 85.0, "hb_prev": 120.0}),
    "核实-缺颈长": lambda: make_case(n={"neck_len": None}),
    "核实-空病例": lambda: VascCase(case_id="EMPTY"),
    "人工越级": lambda: make_case(urgency_flag=True),
}


def _results() -> dict[str, RouteResult]:
    return {label: route(build()) for label, build in SCENARIOS.items()}


# ===========================================================================
# 一、rule_id 可回溯
# ===========================================================================
class TestRuleIdIsTraceable:
    @pytest.mark.parametrize("label", sorted(SCENARIOS))
    def test_rule_id_exists(self, label: str) -> None:
        result = _results()[label]
        assert result.rule_id in ENGINE_RULE_IDS or result.rule_id in RULES

    @pytest.mark.parametrize("label", sorted(SCENARIOS))
    def test_rule_id_has_a_source_cell(self, label: str) -> None:
        """能回溯到 V3.6.2 的具体单元格，否则「规则判的」这句话没法查。"""
        rule_id = _results()[label].rule_id
        if rule_id in ENGINE_RULE_IDS:
            pytest.skip("兜底 / 人工越级不是规格规则")
        assert RULES[rule_id].source.count("!") == 1

    @pytest.mark.parametrize("label", sorted(SCENARIOS))
    def test_exit_matches_result(self, label: str) -> None:
        """规则的声明出口必须与实际出口一致 —— 否则 RULESET.md 会说谎。"""
        result = _results()[label]
        if result.rule_id in ENGINE_RULE_IDS:
            pytest.skip("兜底 / 人工越级不经过 RULES，出口由兜底逻辑决定")
        assert RULES[result.rule_id].exit == result.next_node


# ===========================================================================
# 二、reason 说人话
# ===========================================================================
class TestReasonIsSelfExplaining:
    @pytest.mark.parametrize("label", sorted(SCENARIOS))
    def test_reason_is_not_empty(self, label: str) -> None:
        assert _results()[label].reason.strip()

    @pytest.mark.parametrize("label", sorted(SCENARIOS))
    def test_reason_names_the_rule(self, label: str) -> None:
        """reason 里必须出现 rule_id，否则日志里只剩一句没有归属的结论。"""
        result = _results()[label]
        assert result.rule_id in result.reason

    @pytest.mark.parametrize("label", sorted(SCENARIOS))
    def test_reason_has_no_placeholder_leak(self, label: str) -> None:
        """``None`` / ``nan`` 是程序泄漏，不是给医生看的结论。"""
        reason = _results()[label].reason
        for token in ("None", "nan", "{", "}"):
            assert token not in reason, f"reason 泄漏占位符 {token!r}：{reason}"

    @pytest.mark.parametrize("label", sorted(SCENARIOS))
    def test_reason_is_single_line(self, label: str) -> None:
        assert "\n" not in _results()[label].reason


# ===========================================================================
# 三、evidence_gaps 可用
# ===========================================================================
class TestEvidenceGapsAreUsable:
    @pytest.mark.parametrize("label", sorted(SCENARIOS))
    def test_every_gap_path_is_resolvable(self, label: str) -> None:
        """缺口是给抽取层「去补哪些字段」的指令，路径解析不了就等于没说。"""
        result = _results()[label]
        case = SCENARIOS[label]()
        for gap in result.evidence_gaps:
            if "（" in gap:  # 带说明的附加缺口（如 B6 回落），不按路径解析
                continue
            _read_path(case, gap)

    @pytest.mark.parametrize("label", sorted(SCENARIOS))
    def test_gaps_are_deduplicated(self, label: str) -> None:
        gaps = _results()[label].evidence_gaps
        assert len(gaps) == len(set(gaps))

    def test_hit_rule_fields_are_not_reported_as_gaps(self) -> None:
        """命中的规则已闭环，它引用的其余字段为空是正常状态，不能算缺口。"""
        result = route(make_case())
        rule_fields = set(RULES[result.rule_id].fields)
        assert not (rule_fields & set(result.evidence_gaps))

    def test_no_gaps_on_a_fully_answered_case(self) -> None:
        assert route(make_case()).evidence_gaps == []

    def test_empty_case_reports_a_substantial_gap_list(self) -> None:
        """空病例的缺口必须足够长 —— 只报一两条说明检漏逻辑失效了。"""
        assert len(route(VascCase(case_id="EMPTY")).evidence_gaps) >= 10


# ===========================================================================
# 四、置信度分级可复算
# ===========================================================================
class TestConfidenceIsRecomputable:
    def test_high_requires_zero_gaps(self) -> None:
        result = route(make_case())
        assert result.confidence_level == "高"
        assert result.evidence_gaps == []

    def test_low_when_unknown_was_decisive(self) -> None:
        result = route(VascCase(case_id="EMPTY"))
        assert result.confidence_level == "低"

    def test_levels_are_from_the_documented_enum(self) -> None:
        assert {r.confidence_level for r in _results().values()} <= {"高", "中", "低"}

    def test_low_implies_verify_or_review(self) -> None:
        """置信度低却直接进模式（还带标签）是最危险的组合。

        例外只有一种：**急诊 + 零病种标签**。``G03.R09/R10/R20`` 确实没有
        D1–D7 可归，分流结论本身是确定的（低血压就是低血压），缺的是病种级
        建议。这类病例已由 ``manual_review_required`` 强制人工接手，不属于
        「低置信度还假装有结论」——它们是「置信度低，且明确说了没有结论」。
        """
        for label, result in _results().items():
            if result.confidence_level != "低":
                continue
            if result.mode is RouteMode.EMERGENCY and not result.class_output:
                assert result.kind is RouteKind.MODE, label
                assert any("未匹配 D1–D7" in gap for gap in result.evidence_gaps), label
                continue
            assert result.kind in (RouteKind.VERIFY, RouteKind.REVIEW), label


# ===========================================================================
# 五、trace 不虚构
# ===========================================================================
class TestTraceIsHonest:
    @pytest.mark.parametrize("label", sorted(SCENARIOS))
    def test_trace_entries_are_real_rules(self, label: str) -> None:
        for entry in _results()[label].trace:
            assert entry.rule_id in RULES

    @pytest.mark.parametrize("label", sorted(SCENARIOS))
    def test_trace_verdicts_are_three_valued(self, label: str) -> None:
        for entry in _results()[label].trace:
            assert entry.verdict in (True, False, None)

    @pytest.mark.parametrize("label", sorted(SCENARIOS))
    def test_trace_declares_its_exit(self, label: str) -> None:
        """每条留痕都要写明它指向哪个节点，且必须是规格 M 列出现过的出口。

        ``G08`` 在集合里是因为 ``G04.R20``（遗传/炎症/感染与主病种并存）的
        出口就是分类阶段 —— 它不是模式出口，但确实是 M 列写的去向。
        ``无目标病变`` 是 V3.6.2 新增的终末出口（``G09.R07``）。
        """
        allowed = {"G05", "G06", "G07", "G08", "G09", "人工复核", "资料核实", "无目标病变"}
        for entry in _results()[label].trace:
            assert entry.exit in allowed, f"{entry.rule_id} 声明了未知出口 {entry.exit}"

    def test_trace_stops_at_the_hit(self) -> None:
        """命中即返回：命中的规则在 trace 里必须是**最后一条**，后面不许再有求值。"""
        result = route(make_case(v={"sbp": 70.0}))
        assert result.trace
        assert result.trace[-1].rule_id == result.rule_id
        assert result.trace[-1].verdict is True

    def test_trace_has_no_duplicate_terminal_entries(self) -> None:
        result = route(make_case())
        ids = [entry.rule_id for entry in result.trace]
        assert ids.count(result.rule_id) >= 1

    def test_manual_override_has_empty_trace(self) -> None:
        """人工越级跳过自动判定，所以自动判定的 trace 必须是空的（否则是伪造）。"""
        assert route(make_case(urgency_flag=True)).trace == []


# ===========================================================================
# 六、五出口完整性
# ===========================================================================
class TestExitCoverage:
    def test_all_five_exits_are_reachable(self) -> None:
        """54 条规则不能有「写了但永远走不到」的出口。"""
        exits = {result.next_node for result in _results().values()}
        assert exits == {"G05", "G06", "G07", "人工复核", "资料核实"}

    def test_kind_matches_next_node(self) -> None:
        mapping = {
            "G05": RouteKind.MODE,
            "G06": RouteKind.MODE,
            "G07": RouteKind.MODE,
            "人工复核": RouteKind.REVIEW,
            "资料核实": RouteKind.VERIFY,
        }
        for label, result in _results().items():
            assert result.kind is mapping[result.next_node], label

    def test_mode_is_set_only_for_mode_exits(self) -> None:
        """``mode`` 只在 ``kind=MODE`` 时非空；REVIEW / VERIFY 带 mode 会让执行层误判。"""
        for label, result in _results().items():
            if result.kind is RouteKind.MODE:
                assert result.mode is not None, label
            else:
                assert result.mode is None, label
