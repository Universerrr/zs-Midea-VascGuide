"""工具A（指南引擎）的契约测试 —— D3 分支与登记表。

为什么测试要写得比实现还细：工具A 的规则是**临床判据**，改错的代价不是崩一个
接口，而是给医生一个错的下一步。这里守三类东西：

  1. **判据复用**：``D3.1.R01`` 必须直接看 ``route_result.rule_id``，不得在工具里
     重写 ``c.extrav`` / ``c.hematoma`` 的组合——同一事实两套判据，改一处就分叉；
  2. **不越权**：``D3.2`` / ``D3.3`` 的规格明令「Agent 不得自动作出不可手术结论」，
     故这两个节点的出口恒为「术者确认」，不得出现「建议不做」类结论；
  3. **缺口可见**：``D3.*`` 字段无抽取来源时，必须报「待补」而不是静默为空。
"""

from __future__ import annotations

from conftest import make_case

from src.models.preop.case import VascCase
from src.preop_agent.output_layer.node import build_tool_findings
from src.rules.router import D3_EMERGENCY_RULES, route
from src.tools.base import ToolRequest
from src.tools.guideline_engine import run
from src.tools.guideline_engine.nodes.common import RuleSlot, evaluate_node
from src.tools.guideline_engine.nodes.d3_ruptured import (
    _D33_FIELDS,
    RULE_ROWS,
    d3_1,
    d3_2,
    d3_3,
)
from src.tools.guideline_engine.registry import (
    IMPLEMENTED_RULE_COUNT,
    NODES,
    SPEC_NODE_SHEETS,
    SPEC_RULE_COUNTS,
    SPEC_RULE_TOTAL,
)
from src.tools.guideline_engine.state import (
    Candidate,
    CandidateStatus,
    GuidelineState,
    NodeOutcome,
)


def _report(case: VascCase):
    return run(ToolRequest(case=case, route_result=route(case)))


def _options(case: VascCase) -> list[str]:
    """走完整编排层取 ``treatment_options`` —— 验证渲染，不走工具内部。"""
    from src.preop_agent.graph import run_preop

    return run_preop(case, checkpointer=None)["final_recommendation"].treatment_options


def _rupture_case() -> VascCase:
    """CTA 明确外溢 → G03.R01 → 急诊 + D3 标签。"""
    return make_case(
        c={"extrav": "明确", "extrav_contig": "明确连续"},
        q={"cta_enhance": "适当", "cta_phase": "动脉期"},
    )


# ===========================================================================
# 登记表：规格规模 vs 已实现规模
# ===========================================================================
class TestRegistry:
    def test_spec_sheets_are_the_21_node_sheets(self) -> None:
        assert len(SPEC_NODE_SHEETS) == 21
        assert SPEC_NODE_SHEETS[0] == "D1.1"
        assert SPEC_NODE_SHEETS[-1] == "D7.3"

    def test_spec_rule_counts_sum_to_172(self) -> None:
        """逐 sheet 条数之和必须等于规格总数——少一条就是抄漏了。"""
        assert sum(SPEC_RULE_COUNTS.values()) == SPEC_RULE_TOTAL == 172

    def test_implemented_nodes_belong_to_spec_sheets(self) -> None:
        assert set(NODES) == {"D3.1", "D3.2", "D3.3"}

    def test_implemented_rules_belong_to_their_sheet_and_count(self) -> None:
        for rule_id, row in RULE_ROWS.items():
            sheet = rule_id.rsplit(".R", 1)[0]
            assert sheet in SPEC_NODE_SHEETS
            assert row.startswith(f"{sheet}!"), f"{rule_id} 的源行 {row} 与 sheet 不符"

    def test_implemented_rule_count_matches_declaration(self) -> None:
        assert IMPLEMENTED_RULE_COUNT == len(RULE_ROWS) == 17

    def test_implemented_rules_per_sheet_do_not_exceed_spec(self) -> None:
        for sheet, total in SPEC_RULE_COUNTS.items():
            done = sum(1 for rid in RULE_ROWS if rid.startswith(f"{sheet}."))
            assert done <= total, f"{sheet} 实现了 {done} 条，规格只有 {total} 条"


# ===========================================================================
# D3.1：破裂确定性与证据
# ===========================================================================
class TestD31:
    def test_confirmed_extravasation_hits_r01_and_escalates(self) -> None:
        report = _report(_rupture_case())
        assert report.path[0] == "D3.1"
        assert report.escalation == "G05"
        assert ("D3.1.R01", True) in [(e.rule_id, e.verdict) for e in report.entries]

    def test_r01_reuses_the_router_verdict(self) -> None:
        """R01 必须直接看 ``route_result.rule_id``，不得在工具里重判。"""
        case = _rupture_case()
        result = route(case)
        assert result.rule_id in D3_EMERGENCY_RULES

        state = GuidelineState(case=case, route=result)
        outcome = d3_1(state)
        assert outcome.verdicts["D3.1.R01"] is True

    def test_r01_false_when_router_took_another_path(self) -> None:
        """指南模式（非急诊）进来时，R01 必须为假——不因为「有 CTA」就判破裂。"""
        case = make_case()
        result = route(case)
        assert result.mode is not None and result.rule_id not in D3_EMERGENCY_RULES

        outcome = d3_1(GuidelineState(case=case, route=result))
        assert outcome.verdicts["D3.1.R01"] is False

    def test_unknown_rupture_status_stays_in_emergency(self) -> None:
        """破裂状态不可评价 → 不得降级为「排除破裂」，保持急诊疑似。

        由 ``R05``（P0-A）兜住，而不是 ``R90``（P2）——优先级顺序即规格
        ``NODE.ORDER`` 的「P0 先输出且不得被低优先级覆盖」。
        """
        case = make_case(cl={"aaa_rupture": "无法评价"})
        outcome = d3_1(GuidelineState(case=case, route=route(case)))
        assert outcome.verdicts.get("D3.1.R05") is True
        assert outcome.escalation == "G05"

    def test_unevaluable_imaging_with_clear_rupture_status_falls_to_r90(self) -> None:
        """破裂状态明确为「未见」，但 CTA 不可评价 → P2 数据阻断，人工复核。"""
        case = make_case(
            cl={"aaa_rupture": "未见相关证据"},
            c={"extrav": "不可评价"},
        )
        outcome = d3_1(GuidelineState(case=case, route=route(case)))
        assert outcome.verdicts.get("D3.1.R90") is True
        assert outcome.terminal == "人工复核"

    def test_clear_negative_reaches_r91_exit(self) -> None:
        """破裂/外溢/血肿都明确未见 → R91 出口，回 D1.1/D2.1。"""
        case = make_case(
            cl={"aaa_rupture": "未见相关证据"},
            c={"extrav": "未见", "hematoma": "未见", "rupture_report": "明确未见"},
        )
        outcome = d3_1(GuidelineState(case=case, route=route(case)))
        assert outcome.verdicts.get("D3.1.R91") is True
        assert outcome.terminal is not None and "D1.1" in outcome.terminal

    def test_hit_rule_is_recorded_with_its_source_row(self) -> None:
        """命中项必须带源行——结论要能回到规格的具体单元格。"""
        case = _rupture_case()
        outcome = d3_1(GuidelineState(case=case, route=route(case)))
        assert outcome.entries[0].rule_id == "D3.1.R01"  # P0 排在最前，命中即停
        assert outcome.entries[0].source_row == "D3.1!10"
        assert outcome.entries[0].verdict is True


# ===========================================================================
# D3.2 / D3.3：缺项提示器，不是自动决策器
# ===========================================================================
class TestD32AndD33:
    def test_d32_reports_missing_resources_without_choosing_a_repair(self) -> None:
        report = _report(_rupture_case())
        assert "D3.2" in report.path
        assert report.gaps, "D3.* 无抽取来源时必须报缺口，不能静默为空"
        assert all("：" in g for g in report.gaps), "每条缺口都要写清字段与原因"

    def test_d32_never_claims_first_line_without_ifu_verification(self) -> None:
        """IFU 未核验时不得给「一线推荐」，只能降级为候选。

        规格 R01 的条件是「解剖适合 EVAR **且** IFU 符合」；缺后半个条件还写成
        一线，等于把「没核验」当成「符合」——这是三值语义最典型的踩坑点。
        """
        report = _report(
            make_case(
                c={"extrav": "明确", "extrav_contig": "明确连续"},
                q={"cta_enhance": "适当", "cta_phase": "动脉期"},
                n={"seal_ifu": "未核验"},
            )
        )
        verdicts = {e.rule_id: e.verdict for e in report.entries}
        assert verdicts.get("D3.2.R01") is not True
        assert report.terminal is not None and "候选" in report.terminal
        assert any("IFU 未核验" in gap for gap in report.gaps)

    def test_d33_is_unreachable_until_d32_is_confirmed(self) -> None:
        """D3.2 的出口是「术者确认」，故本轮不会自动进 D3.3。"""
        report = _report(_rupture_case())
        assert "D3.3" not in report.path

    def test_terminal_is_always_a_doctor_action(self) -> None:
        """D3.2/D3.3 的终态只能是人工决策，不能是「不修复」这类自动结论。"""
        report = _report(_rupture_case())
        assert report.terminal is not None
        assert "确认" in report.terminal or "复核" in report.terminal
        assert "不修复" not in report.terminal


# ===========================================================================
# 入口与边界
# ===========================================================================
class TestEntryAndBoundary:
    def test_emergency_rules_carry_the_d3_label(self) -> None:
        """急诊破裂类规则必须预置 D3 标签，否则工具A 拿不到入口。"""
        result = route(_rupture_case())
        assert result.class_output == ["D3"]
        assert result.rule_id in D3_EMERGENCY_RULES

    def test_no_class_output_yields_empty_report(self) -> None:
        """未分到病种 → 空报告，不编默认条目。"""
        case = make_case()
        result = route(case)
        result.class_output = []
        report = run(ToolRequest(case=case, route_result=result))
        assert report.entries == ()
        assert report.path == ()

    def test_unimplemented_node_reports_a_gap(self) -> None:
        """D1 标签指向未实现的 D1.1：报缺口，不假装有结论。"""
        report = _report(make_case())
        assert report.path == ("D1.1",)
        assert report.entries == ()
        assert any("D1.1" in g for g in report.gaps)

    def test_run_does_not_mutate_the_case(self) -> None:
        """病种流程的产物属于本次运行，不得写回病例。"""
        case = _rupture_case()
        before = case.model_dump()
        _report(case)
        assert case.model_dump() == before


# ===========================================================================
# 公共求值器：全部落空不等于放行
# ===========================================================================
class TestEvaluateNodeFallback:
    def test_all_rules_unknown_falls_back_to_manual_review(self) -> None:
        """所有规则都给不出结论时，不得默认放行——落到人工复核并说明原因。"""
        case = make_case()
        state = GuidelineState(case=case, route=route(case))
        slots = (
            RuleSlot(
                rule_id="X.R01",
                priority="P3",
                source_row="X!1",
                statement="永不命中",
                judge=lambda _s: None,
                exit_kind="next",
                exit_value="X.2",
            ),
        )
        outcome = evaluate_node("X", state, slots)
        assert outcome.terminal == "人工复核"
        assert outcome.next_node is None
        assert outcome.gaps and "全部规则均未命中" in outcome.gaps[0]


# ===========================================================================
# D3.3：非修复决策的记录器
# ===========================================================================
class TestD33:
    def test_reports_gaps_when_nothing_recorded(self) -> None:
        case = _rupture_case()
        outcome = d3_3(GuidelineState(case=case, route=route(case)))
        assert outcome.verdicts["D3.3.R90"] is True
        assert len(outcome.gaps) == len(_D33_FIELDS)
        assert outcome.terminal is not None and "确认" in outcome.terminal

    def test_reaches_doctor_confirmation_when_all_recorded(self) -> None:
        """五项都记录后仍需医生确认——不是系统给出「不修复」结论。"""
        case = _rupture_case()
        filled = {spec_id: "已记录" for spec_id, _ in _D33_FIELDS}
        outcome = d3_3(GuidelineState(case=case, route=route(case), d3=filled))
        assert outcome.verdicts["D3.3.R91"] is True
        assert outcome.gaps == ()
        assert outcome.terminal is not None and "确认" in outcome.terminal


# ===========================================================================
# D3.2：术式方向（EVAR 一线 / 候选 / OSR / 阻断）
# ===========================================================================
class TestD32RepairDirection:
    """D3.2 是唯一给术式方向的节点，四条出口必须各有正例与边界。

    这些判据全部读 ``VascCase`` 现成字段（瘤颈/IFU/入路/血压），不依赖任何
    「D3.* 元字段」——实现如果退回读 ```state.d3```，本组测试会全部失败。
    """

    @staticmethod
    def _d32(**overrides):
        case = make_case(
            c={"extrav": "明确", "extrav_contig": "明确连续"},
            q={"cta_enhance": "适当", "cta_phase": "动脉期"},
            **overrides,
        )
        return d3_2(GuidelineState(case=case, route=route(case)))

    def test_full_workup_gives_evar_first_line(self) -> None:
        """解剖齐全 + IFU 符合 + 血压稳定 → EVAR 一线（规格 R01，P0-A）。"""
        outcome = self._d32()
        assert outcome.verdicts["D3.2.R01"] is True
        assert outcome.terminal is not None and "一线" in outcome.terminal

    def test_unverified_ifu_downgrades_to_candidate(self) -> None:
        """IFU 未核验 → 不给一线，降为候选（R04），且缺口里点名 IFU。"""
        outcome = self._d32(n={"seal_ifu": "未核验"})
        assert outcome.verdicts["D3.2.R01"] is None
        assert outcome.verdicts["D3.2.R04"] is True
        assert outcome.terminal is not None and "候选" in outcome.terminal
        assert any("IFU 未核验" in gap for gap in outcome.gaps)

    def test_ifu_mismatch_switches_to_osr(self) -> None:
        """IFU 明确不符 → 评估 OSR（规格 R02，P1）。"""
        outcome = self._d32(n={"seal_ifu": "不符"})
        assert outcome.verdicts["D3.2.R02"] is True
        assert outcome.terminal is not None and "OSR" in outcome.terminal

    def test_bilateral_access_occlusion_switches_to_osr(self) -> None:
        """双侧入路闭塞 → EVAR 不可行 → 评估 OSR。"""
        outcome = self._d32(n={"access_occlusion": "双侧闭塞"})
        assert outcome.verdicts["D3.2.R02"] is True

    def test_unilateral_access_occlusion_keeps_evar(self) -> None:
        """单侧闭塞不阻断：对侧仍可建立入路，不得把病人推去开放手术。"""
        outcome = self._d32(n={"access_occlusion": "单侧闭塞", "seal_ifu": "未核验"})
        assert outcome.verdicts["D3.2.R02"] is not True
        assert outcome.verdicts["D3.2.R04"] is True

    def test_missing_anatomy_blocks_any_direction(self) -> None:
        """三个瘤颈参数全缺 → R90 阻断，不给任何术式方向。"""
        outcome = self._d32(n={"neck_len": None, "neck_d": None, "neck_angle": None})
        assert outcome.verdicts["D3.2.R90"] is True
        assert outcome.terminal is not None and "复核" in outcome.terminal
        # R90 命中即停，后续 P3 规则不再求值——「没执行」本身就是要守住的行为
        assert "D3.2.R04" not in outcome.verdicts

    def test_partial_anatomy_withholds_direction(self) -> None:
        """只有部分瘤颈参数 → 不阻断，但也不给方向（留 UNKNOWN 交团队补）。"""
        outcome = self._d32(n={"neck_angle": None})
        assert outcome.verdicts["D3.2.R90"] is False
        assert outcome.verdicts["D3.2.R01"] is None
        assert outcome.verdicts["D3.2.R04"] is None

    def test_missing_team_resource_never_blocks_direction(self) -> None:
        """团队资源无来源是常态，不能因此否掉术式方向——规格要求「不延误控制」。"""
        outcome = self._d32(n={"seal_ifu": "未核验"})
        assert outcome.verdicts["D3.2.R04"] is True
        assert any("TEAM_RESOURCE" in gap for gap in outcome.gaps)

    def test_borderline_sbp_withholds_first_line(self) -> None:
        """SBP 82 属临界：不判「可评估」，故不给一线，但仍可给候选。"""
        outcome = self._d32(v={"sbp": 82.0}, n={"seal_ifu": "未核验"})
        assert outcome.verdicts["D3.2.R01"] is None
        assert outcome.verdicts["D3.2.R04"] is True

    def test_frank_shock_blocks_evaluation(self) -> None:
        """SBP<80 明确不稳定 → 患者状态判 False，R01 不成立（不是 UNKNOWN）。"""
        outcome = self._d32(v={"sbp": 70.0})
        assert outcome.verdicts["D3.2.R01"] is False


# ===========================================================================
# 产物渲染：结论必须可读，不能被 repr 吞掉
# ===========================================================================
class TestReportRendering:
    """工具A 的产物是 dataclass，不是 BaseModel——通用摘要分支会走 repr 截断。

    截断的后果是「终点 = EVAR 候选」这一行在 CLI 里看不见，等于白算。
    这组测试守住 output_layer 的专用渲染分支。
    """

    @staticmethod
    def _text(case: VascCase) -> str:
        rep = run(ToolRequest(case=case, route_result=route(case)))
        return " ".join(
            finding.summary for finding in build_tool_findings({"guideline_engine": rep})
        )

    def test_renders_terminal_and_path_instead_of_repr(self) -> None:
        text = self._text(_rupture_case())
        assert "终点 = " in text
        assert "路径 = D3.1 → D3.2" in text
        assert "GuidelineReport(" not in text, "不得退回 repr 截断"

    def test_renders_hit_rules_and_gap_count(self) -> None:
        text = self._text(_rupture_case())
        assert "命中 = " in text and "D3.2.R01" in text
        assert "缺口 = " in text


# ===========================================================================
# 术式候选：规格 D1.3.R91 的「两列 × 三档」
# ===========================================================================
class TestCandidateStatusVocabulary:
    """候选状态的措辞不得自创 —— 前三档逐字来自规格 D1.3.R91。

    为什么守这个：「可行候选」被改成「可行」或「推荐」后，读者会以为系统已经
    批准了该术式；而规格的原意是「列为候选，待术者确认」。一词之差改变的是
    法律含义，不是风格。
    """

    def test_first_three_levels_come_from_the_spec(self) -> None:
        assert CandidateStatus.FEASIBLE == "可行候选"
        assert CandidateStatus.CONDITIONAL == "条件性候选"
        assert CandidateStatus.EXCLUDED == "排除"

    def test_five_levels_total(self) -> None:
        """五档：三档来自规格 + MDT 裁决 + 未评估。多一档意味着语义被稀释。"""
        assert len(CandidateStatus) == 5

    def test_render_has_no_treatment_verb(self) -> None:
        """候选行的措辞只能是「术式：状态（原因）」，不得夹带治疗主张。"""
        cand = Candidate("EVAR", CandidateStatus.CONDITIONAL, "IFU 未核验")
        text = cand.render()
        assert text == "EVAR：条件性候选（IFU 未核验）"
        for word in ("建议", "推荐使用", "应行", "首选", "宜"):
            assert word not in text


class TestD32Candidates:
    """D3.2 是唯一给术式方向的节点，两列 × 四情形必须各有正例。

    这组测试同时是「粒度边界」的守门人：任何候选都不得出现品牌 / SKU / 型号，
    因为规格有 5 处明文禁止工具A 走到器械选型（D1.3.R03/R06/R90、D7.3.R01/R04）。
    """

    @staticmethod
    def _candidates(**overrides) -> dict[str, Candidate]:
        case = make_case(
            c={"extrav": "明确", "extrav_contig": "明确连续"},
            q={"cta_enhance": "适当", "cta_phase": "动脉期"},
            **overrides,
        )
        outcome = d3_2(GuidelineState(case=case, route=route(case)))
        return {c.procedure: c for c in outcome.candidates}

    def test_always_has_both_columns(self) -> None:
        """规格要求「分别列出 EVAR/OSR」——缺一列会让读者以为那列已被排除。"""
        got = self._candidates()
        assert set(got) == {"EVAR", "OSR"}

    def test_full_workup_gives_feasible_evar(self) -> None:
        got = self._candidates(n={"seal_ifu": "符合"}, v={"sbp": 120.0})
        assert got["EVAR"].status is CandidateStatus.FEASIBLE
        assert "D3.2.R01" in got["EVAR"].rule_ids

    def test_unverified_ifu_downgrades_to_conditional(self) -> None:
        got = self._candidates(n={"seal_ifu": "未核验"})
        assert got["EVAR"].status is CandidateStatus.CONDITIONAL
        assert "IFU 未核验" in got["EVAR"].reason

    def test_ifu_mismatch_is_exclusion_not_unknown(self) -> None:
        """IFU 明确不符是**排除**，不是「未评估」——两者临床含义相反。"""
        got = self._candidates(n={"seal_ifu": "不符"})
        assert got["EVAR"].status is CandidateStatus.EXCLUDED

    def test_bilateral_access_occlusion_is_exclusion(self) -> None:
        got = self._candidates(n={"access_occlusion": "双侧闭塞"})
        assert got["EVAR"].status is CandidateStatus.EXCLUDED
        assert "入路" in got["EVAR"].reason

    def test_unilateral_access_occlusion_is_not_exclusion(self) -> None:
        """单侧闭塞不排除：对侧仍可建立入路。"""
        got = self._candidates(n={"access_occlusion": "单侧闭塞", "seal_ifu": "未核验"})
        assert got["EVAR"].status is CandidateStatus.CONDITIONAL

    def test_missing_anatomy_is_unassessed_not_excluded(self) -> None:
        """瘤颈参数全缺 → 未评估。不得写成排除（那等于替医生下了结论）。"""
        got = self._candidates(n={"neck_len": None, "neck_d": None, "neck_angle": None})
        assert got["EVAR"].status is CandidateStatus.UNASSESSED

    def test_osr_is_always_mdt_pending(self) -> None:
        """OSR 恒为「需 MDT 评估」——VascCase 无开放解剖字段，没有判据就不下结论。"""
        for overrides in ({}, {"n": {"seal_ifu": "不符"}}, {"n": {"access_occlusion": "双侧闭塞"}}):
            got = self._candidates(**overrides)
            assert got["OSR"].status is CandidateStatus.MDT

    def test_osr_reason_changes_when_evar_excluded(self) -> None:
        """EVAR 被排除时 OSR 成为唯一方向——理由必须跟着变，否则读者分不清优先级。"""
        normal = self._candidates()["OSR"].reason
        evar_out = self._candidates(n={"seal_ifu": "不符"})["OSR"].reason
        assert normal != evar_out
        assert "唯一候选" in evar_out

    def test_unknown_access_is_conditional_with_named_gap(self) -> None:
        """入路字段缺失 → 不排除也不确认，落条件性并点名缺什么。"""
        got = self._candidates(n={"access_occlusion": None})
        assert got["EVAR"].status is CandidateStatus.CONDITIONAL
        assert "入路可行性未评估" in got["EVAR"].reason

    def test_uncertain_access_value_is_not_exclusion(self) -> None:
        """「未知」是有值但值不确定 → 只能不判，不得当成排除。"""
        got = self._candidates(n={"access_occlusion": "未知", "seal_ifu": "未核验"})
        assert got["EVAR"].status is CandidateStatus.CONDITIONAL

    def test_declining_consciousness_blocks_feasible_status(self) -> None:
        """新发意识下降 → 患者不可评估 → EVAR 不给「可行候选」。

        这条同时守住一个曾存在的死分支：代码曾比较「新发意识下降」，而模型
        的 ``Literal`` 是「新发下降」——两者永不相等，导致该判断从没生效。
        """
        got = self._candidates(n={"seal_ifu": "符合"}, v={"conscious_change": "新发下降"})
        assert got["EVAR"].status is CandidateStatus.CONDITIONAL
        assert "不稳定" in got["EVAR"].reason

    def test_baseline_unknown_consciousness_is_not_declining(self) -> None:
        """「基线未知」≠ 新发下降：不当作恶化，血压稳定时仍判可评估。

        与上一条成对：如果这两个值被写成同一个分支，新发下降就再也拦不住
        「可行候选」——急诊最该降级的情形反而放行。
        """
        got = self._candidates(n={"seal_ifu": "符合"}, v={"conscious_change": "基线未知"})
        assert got["EVAR"].status is CandidateStatus.FEASIBLE

    def test_missing_sbp_is_unknown_not_unstable(self) -> None:
        """血压缺失 ≠ 不稳定，只能判 UNKNOWN。"""
        got = self._candidates(n={"seal_ifu": "符合"}, v={"sbp": None})
        assert got["EVAR"].status is CandidateStatus.CONDITIONAL
        assert "患者状态待明确" in got["EVAR"].reason

    def test_no_candidate_mentions_device_model(self) -> None:
        """粒度红线：候选里不得出现品牌/SKU/尺寸——规格 5 处明文禁止。"""
        got = self._candidates(n={"seal_ifu": "符合"})
        for cand in got.values():
            text = cand.render()
            for banned in ("SKU", "品牌", "型号", "mm 支架", "Endurant", "Excluder"):
                assert banned not in text


class TestCandidateAggregation:
    """候选在报告层的合并规则：同术式后者覆盖前者，顺序稳定。"""

    def test_report_exposes_candidates(self) -> None:
        report = _report(_rupture_case())
        assert {c.procedure for c in report.candidates} == {"EVAR", "OSR"}

    def test_later_node_overrides_earlier_for_same_procedure(self) -> None:
        """病种流程单向前进，越靠后的节点结论越成熟——并存会让下游不知读哪条。"""
        state = GuidelineState(case=make_case(), route=route(make_case()))
        early = NodeOutcome(
            node_id="X.1",
            verdicts={},
            entries=(),
            candidates=(Candidate("EVAR", CandidateStatus.UNASSESSED, "早期草案"),),
        )
        late = NodeOutcome(
            node_id="X.2",
            verdicts={},
            entries=(),
            candidates=(Candidate("EVAR", CandidateStatus.FEASIBLE, "定稿"),),
        )
        state.visited.extend((early, late))
        assert len(state.candidates) == 1
        assert state.candidates[0].status is CandidateStatus.FEASIBLE

    def test_order_follows_first_appearance(self) -> None:
        """顺序取首次出现顺序，不排序——final_recommendation 进哈希，顺序不能抖。"""
        state = GuidelineState(case=make_case(), route=route(make_case()))
        state.visited.append(
            NodeOutcome(
                node_id="X.1",
                verdicts={},
                entries=(),
                candidates=(
                    Candidate("OSR", CandidateStatus.MDT, "先出现"),
                    Candidate("EVAR", CandidateStatus.EXCLUDED, "后出现"),
                ),
            )
        )
        assert tuple(c.procedure for c in state.candidates) == ("OSR", "EVAR")


class TestTreatmentOptionsRendering:
    """treatment_options 是候选项的渲染结果，不是第二处判断。"""

    def test_emergency_case_yields_two_options(self) -> None:
        rec = _report(_rupture_case())
        options = _options(_rupture_case())
        assert len(options) == 2
        assert options[0].startswith("EVAR：")
        assert options[1].startswith("OSR：")
        assert rec is not None

    def test_options_are_empty_when_no_candidate_node_ran(self) -> None:
        """D1 分支未实现 → 没有候选节点 → 空列表，不用默认文案填充。"""
        from src.preop_agent.graph import run_preop

        case = VascCase(case_id="EMPTY-CAND")
        assert run_preop(case, checkpointer=None)["final_recommendation"].treatment_options == []

    def test_options_do_not_become_clinical_advice(self) -> None:
        """清单里不得出现治疗主张——那是判定层的职责，不是渲染层的。"""
        for option in _options(_rupture_case()):
            for word in ("建议行", "推荐使用", "应行", "首选"):
                assert word not in option
