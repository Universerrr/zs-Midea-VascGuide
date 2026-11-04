"""工具A（指南引擎）的契约测试 —— D1 / D3 分支与登记表。

为什么测试要写得比实现还细：工具A 的规则是**临床判据**，改错的代价不是崩一个
接口，而是给医生一个错的下一步。这里守三类东西：

  1. **判据复用**：``D3.1.R01`` 必须直接看 ``route_result.rule_id``，不得在工具里
     重写 ``c.extrav`` / ``c.hematoma`` 的组合——同一事实两套判据，改一处就分叉；
  2. **不越权**：``D3.2`` / ``D3.3`` / ``D1.2`` 的规格明令「Agent 不得自动作出不可
     手术结论」「未评估不等于无衰弱」，故出口恒为「术者/医生确认」，不得出现
     「建议不做」类结论；
  3. **缺口可见**：``D3.*`` / ``D1.*`` 字段无抽取来源时，必须报「待补」而不是静默为空。
"""

from __future__ import annotations

import re

import pytest
from conftest import SPEC_XLSX, make_case
from pydantic import ValidationError

from src.models.preop.case import VascCase
from src.preop_agent.output_layer.node import build_tool_findings
from src.rules.router import D3_EMERGENCY_RULES, route
from src.tools.base import ToolRequest
from src.tools.guideline_engine import run
from src.tools.guideline_engine.nodes import (
    d1_asymptomatic,
    d2_symptomatic,
    d3_ruptured,
    d4_infected,
    d5_special,
    d6_postop,
    d7_iliac,
)
from src.tools.guideline_engine.nodes.common import RuleSlot, evaluate_node
from src.tools.guideline_engine.nodes.d1_asymptomatic import d1_1, d1_2, d1_3
from src.tools.guideline_engine.nodes.d2_symptomatic import d2_1, d2_2, d2_3
from src.tools.guideline_engine.nodes.d3_ruptured import (
    _D33_FIELDS,
    _d31_r91,
    d3_1,
    d3_2,
    d3_3,
)
from src.tools.guideline_engine.nodes.d4_infected import d4_1, d4_2, d4_3
from src.tools.guideline_engine.nodes.d5_special import _mantle, d5_1, d5_2, d5_3
from src.tools.guideline_engine.nodes.d6_postop import d6_1, d6_2, d6_3
from src.tools.guideline_engine.nodes.d7_iliac import d7_1, d7_2, d7_3
from src.tools.guideline_engine.registry import (
    IMPLEMENTED_RULE_COUNT,
    NODES,
    RULE_ROWS,
    SPEC_NODE_SHEETS,
    SPEC_RULE_COUNTS,
    SPEC_RULE_TOTAL,
)
from src.tools.guideline_engine.state import (
    Candidate,
    CandidateStatus,
    GuidelineState,
    Link,
    NodeOutcome,
)

#: D1.1 判定年龄阈值所需的测量方法与阈值基准（规格 D1.SEX_THRESHOLD_SOURCE）。
_D1_METHOD: dict[str, dict[str, str]] = {
    "D1": {
        "D1.DIAM_METHOD": "超声AP内-内",
        "D1.SEX_THRESHOLD_SOURCE": "超声测量匹配",
        "D1.MEASURE_VALID": "可比",
    }
}


def _report(case: VascCase, node_inputs: dict[str, dict[str, str]] | None = None):
    return run(
        ToolRequest(
            case=case,
            route_result=route(case),
            node_inputs=node_inputs or {},
        )
    )


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
        assert set(NODES) == {
            "D1.1",
            "D1.2",
            "D1.3",
            "D2.1",
            "D2.2",
            "D2.3",
            "D3.1",
            "D3.2",
            "D3.3",
            "D4.1",
            "D4.2",
            "D4.3",
            "D5.1",
            "D5.2",
            "D5.3",
            "D6.1",
            "D6.2",
            "D6.3",
            "D7.1",
            "D7.2",
            "D7.3",
        }

    def test_implemented_rules_belong_to_their_sheet_and_count(self) -> None:
        for rule_id, row in RULE_ROWS.items():
            sheet = rule_id.rsplit(".R", 1)[0]
            assert sheet in SPEC_NODE_SHEETS
            assert row.startswith(f"{sheet}!"), f"{rule_id} 的源行 {row} 与 sheet 不符"

    def test_implemented_rule_count_matches_declaration(self) -> None:
        assert IMPLEMENTED_RULE_COUNT == len(RULE_ROWS) == 172

    def test_implemented_rules_per_sheet_do_not_exceed_spec(self) -> None:
        for sheet, total in SPEC_RULE_COUNTS.items():
            done = sum(1 for rid in RULE_ROWS if rid.startswith(f"{sheet}."))
            assert done <= total, f"{sheet} 实现了 {done} 条，规格只有 {total} 条"

    def test_every_registered_rule_has_a_judge(self) -> None:
        """登记了 172 条，就必须有 172 条**真的会被求值**。

        ``RULE_ROWS`` 是「已登记」清单，``RuleSlot`` / ``DisplayRule`` 才是真正
        执行判据的载体。两者不一致时，报告里那条结论永远不出现，而条数统计照样
        是 172 —— 光看数字发现不了。

        唯一豁免 ``D1.1.R06``：它是展示性规则，由 ``d1_1`` 在节点层手写产出
        （进槽位会以 P1 抢先命中，挤掉 P3 的监测出口）。豁免必须写在这里，
        新增规则时忘了接判据才会被发现。
        """
        hand_written = {"D1.1.R06"}
        declared: set[str] = set()
        for mod in (
            d1_asymptomatic,
            d2_symptomatic,
            d3_ruptured,
            d4_infected,
            d5_special,
            d6_postop,
            d7_iliac,
        ):
            for attr, value in vars(mod).items():
                if attr.endswith(("_SLOTS", "_DISPLAY_RULES")) and isinstance(value, tuple):
                    declared |= {item.rule_id for item in value}
        missing = sorted(set(RULE_ROWS) - declared - hand_written)
        assert not missing, f"已登记但没有任何判据载体：{missing}"
        assert not declared - set(RULE_ROWS), (
            f"判据载体指向未登记的规则：{declared - set(RULE_ROWS)}"
        )


# ===========================================================================
# D3.1：破裂确定性与证据
# ===========================================================================
class TestD31:
    def test_confirmed_extravasation_hits_r01_and_escalates(self) -> None:
        report = _report(_rupture_case())
        assert report.path[0] == "D3.1"
        assert report.escalation == "G05"
        assert ("D3.1.R01", True) in [(e.rule_id, e.verdict) for e in report.entries]

    def test_r05_stays_unknown_when_no_grade_was_recorded(self) -> None:
        """破裂分级未记录 → ``R05`` 保持 UNKNOWN，不得当成「排除破裂」。"""
        case = make_case(cl={"aaa_rupture": None})
        outcome = d3_1(GuidelineState(case=case, route=route(case)))
        assert outcome.verdicts["D3.1.R05"] is None
        assert outcome.verdicts["D3.1.R01"] is False

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

    def test_unknown_rupture_status_continues_to_d32(self) -> None:
        """破裂状态不可评价 → 不得降级为「排除破裂」，继续进 D3.2 评估。

        由 ``R05``（P0-A）兜住，而不是 ``R90``（P2）——优先级顺序即规格
        ``NODE.ORDER`` 的「P0 先输出且不得被低优先级覆盖」。

        出口按规格 M 列「下一节点/终点：D3.2 / 动作：D3.2 或持续急诊评估」：
        **值域只有 D3.2**，没有 G05。修前写成 ``escalate G05``，把「持续急诊评估」
        当成了独立出口 —— 那描述的是 D3.2 内部的持续观察，不是另一条路。
        """
        case = make_case(cl={"aaa_rupture": "无法评价"})
        outcome = d3_1(GuidelineState(case=case, route=route(case)))
        assert outcome.verdicts.get("D3.1.R05") is True
        assert outcome.next_node == "D3.2"
        assert outcome.escalation is None

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

    def test_r91_requires_the_doctor_to_confirm(self) -> None:
        """影像全阴但医生仍写「疑似」→ **不得**走排除破裂（规格 G 列两段合取）。

        这是 R91 修严的核心：只看影像会把一份「CTA 报阴性」的病例送回择期，
        等于系统替医生撤销了他自己写的怀疑。未记录同样不算确认。
        """
        for dx_context in ("疑似", None):
            case = make_case(
                d={"dx_context": dx_context},
                cl={"aaa_rupture": "未见相关证据"},
                c={"extrav": "未见", "hematoma": "未见", "rupture_report": "明确未见"},
            )
            state = GuidelineState(case=case, route=route(case))
            assert _d31_r91(state) is False, dx_context
            # 链路不能断在分诊层：稳定患者由 R03 接手进 D3.2
            outcome = d3_1(state)
            assert outcome.verdicts.get("D3.1.R91") is not True
            assert outcome.next_node == "D3.2"

    def test_doctor_confirmed_negative_still_takes_the_exclusion_path(self) -> None:
        """医生确认 + 影像全阴 → 仍然走 R91（修严不能把正常出口也堵死）。"""
        case = make_case(
            d={"dx_context": "确认"},
            cl={"aaa_rupture": "未见相关证据"},
            c={"extrav": "未见", "hematoma": "未见", "rupture_report": "明确未见"},
        )
        state = GuidelineState(case=case, route=route(case))
        assert _d31_r91(state) is True

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
                n={"evar_ifu_status": "未核验"},
            )
        )
        verdicts = {e.rule_id: e.verdict for e in report.entries}
        assert verdicts.get("D3.2.R01") is not True
        assert report.terminal is not None and "候选" in report.terminal
        assert any("IFU 未核验" in gap for gap in report.gaps)

    def test_d33_is_not_entered_before_the_doctor_decides(self) -> None:
        """医生还没写治疗目标时，D3.2 停在「术者确认」，不进 D3.3。

        反向用例见 ``TestD33.test_a_recorded_goal_hands_the_case_to_the_recorder``：
        一旦 ``D3.GOALS`` 写下来，同一个 D3.2 就会把病例交给 D3.3 留痕。
        """
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

    def test_unimplemented_node_reports_a_gap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """病种标签指向未实现节点时，只留缺口、不伪装成有结论。

        21 个节点已全部实现，所以这里用 ``monkeypatch`` 摘掉一个——守的是
        ``engine._walk`` 的分支**本身**，不是当前的开发进度。哪天新增病种或
        临时下线某节点，这条测试仍然成立；反过来，若把它删掉，那个分支就再
        也没有任何一处被执行过（``NODES.get() is None`` 会静默走空）。
        """
        from src.tools.guideline_engine import engine

        monkeypatch.setattr(engine, "NODES", {k: v for k, v in engine.NODES.items() if k != "D7.1"})
        case = make_case()
        result = route(case)
        result.class_output = ["D7"]
        report = run(ToolRequest(case=case, route_result=result))
        # 入口节点本身记进 path（它确实是本轮的目标），但一条条目都没有
        assert report.path == ("D7.1",)
        assert report.entries == ()
        assert any("D7.1" in gap and "尚未实现" in gap for gap in report.gaps)
        assert report.terminal is not None and "D7.1" in report.terminal

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
#: 一份「医生已决定不修复」的完整记录 —— 五项 + 原因 + 裁决人。
_D33_NONREPAIR: dict[str, object] = {
    "D3.REPAIR_FEASIBILITY": "无可行方案",
    "D3.PATIENT_WISH": "拒绝特定治疗",
    "D3.CAPACITY": "长期无能力",
    "D3.PROGNOSIS": "医生综合评估：长期卧床、痴呆，修复获益极低",
    "D3.GOALS": "舒适照护",
    "D3.NONREPAIR_REASON": ["患者极高风险", "患者拒绝"],
}


def _d33_state(d3: dict[str, object] | None = None) -> GuidelineState:
    case = _rupture_case()
    return GuidelineState(
        case=case, route=route(case), node_inputs={"D3": {**_D33_NONREPAIR, **(d3 or {})}}
    )


class TestD33:
    """D3.3 是「裁决记录面」，**当前没有自动入边**。

    规格 D3.3 全表 12 行的 M 列（输出／下一节点）都是「人工决策 / 医生确认 /
    记录理由 / 终点动作」，没有一条 ``next_node``。所以本类全部直接调用节点测
    判据——端到端走不到这里，那是设计结果，不是缺口（见节点模块注释）。
    """

    # ---- 结构：不进主流程 ----

    def test_a_recorded_goal_does_not_reroute_d32(self) -> None:
        """医生写下治疗目标也不改道 —— D3.2 的出口恒为「术式方向 + 术者确认」。

        曾加过一条 `D3.GOALS 已记录 → next_node="D3.3"` 的改写，那是给 D3.3
        造了一条规格没有的入边，还让「已确认不修复」与候选栏的「EVAR：可行候选」
        并列出现在同一份报告里。这条测试守的就是「不要再加回去」。
        """
        outcome = d3_2(_d33_state())
        assert outcome.next_node is None
        assert outcome.terminal is not None

    def test_end_to_end_never_enters_d33(self) -> None:
        """完整跑一遍：即使 ``D3.*`` 填满，路径也停在 ``D3.2``。"""
        case = _rupture_case()
        report = run(
            ToolRequest(case=case, route_result=route(case), node_inputs={"D3": _D33_NONREPAIR})
        )
        assert report.path == ("D3.1", "D3.2")
        assert "D3.3" not in report.path

    def test_without_a_goal_d32_still_stops_at_the_doctor(self) -> None:
        case = _rupture_case()
        outcome = d3_2(GuidelineState(case=case, route=route(case)))
        assert outcome.next_node is None
        assert outcome.terminal is not None

    def test_a_blocked_anatomy_keeps_the_manual_review(self) -> None:
        case = make_case(n={"neck_len": None, "neck_d": None, "neck_angle": None})
        outcome = d3_2(
            GuidelineState(case=case, route=route(case), node_inputs={"D3": _D33_NONREPAIR})
        )
        assert outcome.verdicts["D3.2.R90"] is True
        assert outcome.next_node is None

    # ---- R90：五项不全就阻断 ----

    def test_all_fields_missing_blocks_with_a_field_list(self) -> None:
        case = _rupture_case()
        outcome = d3_3(GuidelineState(case=case, route=route(case)))
        assert outcome.verdicts["D3.3.R90"] is True
        assert len(outcome.gaps) == len(_D33_FIELDS)
        assert outcome.terminal is not None and "术者确认" in outcome.terminal

    def test_a_repair_goal_reaches_the_confirmation_exit(self) -> None:
        """五项齐、目标是「积极修复」→ R91，不刷非修复原因的缺口。"""
        case = _rupture_case()
        filled = {spec_id: "已记录" for spec_id, _ in _D33_FIELDS}
        filled["D3.GOALS"] = "积极修复"
        outcome = d3_3(GuidelineState(case=case, route=route(case), node_inputs={"D3": filled}))
        assert outcome.verdicts["D3.3.R91"] is True
        assert not any("NONREPAIR_REASON" in gap for gap in outcome.gaps)
        assert not any("裁决医师" in gap for gap in outcome.gaps)

    # ---- R01：有理由，但还没决定 ----

    def test_no_option_without_a_decision_lists_the_paths(self) -> None:
        """R01：做不了但目标还没填 → 摊开转诊/替代/舒适照护，不替医生选。"""
        outcome = d3_3(_d33_state({"D3.GOALS": "待决定"}))
        assert outcome.verdicts["D3.3.R01"] is True
        assert outcome.terminal is not None and "授权医生确认" in outcome.terminal

    def test_patient_refusal_alone_also_triggers_r01(self) -> None:
        outcome = d3_3(_d33_state({"D3.GOALS": "待决定", "D3.REPAIR_FEASIBILITY": "存在EVAR方案"}))
        assert outcome.verdicts["D3.3.R01"] is True

    # ---- R02 / R03：已决定，看原因写得全不全 ----

    def test_a_decision_without_a_reason_is_recorded_but_flagged(self) -> None:
        """目标写「舒适照护」但原因是「未确定」→ R02，出口说须补原因。"""
        outcome = d3_3(_d33_state({"D3.NONREPAIR_REASON": ["未确定"]}))
        assert outcome.verdicts["D3.3.R03"] is False
        assert outcome.verdicts["D3.3.R02"] is True
        assert outcome.terminal is not None and "须补非修复原因" in outcome.terminal
        assert any("D3.NONREPAIR_REASON" in gap for gap in outcome.gaps)

    def test_a_complete_record_reaches_r03(self) -> None:
        outcome = d3_3(_d33_state())
        assert outcome.verdicts["D3.3.R03"] is True
        assert outcome.terminal is not None and "待授权医生确认" in outcome.terminal

    def test_a_missing_reason_field_is_unknown_for_r03(self) -> None:
        """原因栏整块没填 → R03 判不了（``None``），R02 仍成立。"""
        outcome = d3_3(_d33_state({"D3.NONREPAIR_REASON": None}))
        assert outcome.verdicts["D3.3.R03"] is None
        assert outcome.verdicts["D3.3.R02"] is True

    def test_an_empty_reason_list_is_unknown_for_r03(self) -> None:
        outcome = d3_3(_d33_state({"D3.NONREPAIR_REASON": []}))
        assert outcome.verdicts["D3.3.R03"] is None

    def test_r02_is_unknown_without_a_recorded_goal(self) -> None:
        """目标栏没值时 R02 只能给 UNKNOWN —— 它连「是不是不修复」都判不了。"""
        from src.tools.guideline_engine.nodes.d3_ruptured import _d33_r02

        assert _d33_r02(_d33_state({"D3.GOALS": None})) is None

    def test_a_limited_treatment_goal_also_counts_as_non_repair(self) -> None:
        """「有限治疗」与「舒适照护」同为不修复档（规格 F 列）。"""
        outcome = d3_3(_d33_state({"D3.GOALS": "有限治疗"}))
        assert outcome.verdicts["D3.3.R03"] is True

    # ---- 签名只是提示，不是字段 ----

    def test_the_signature_is_only_a_gap_hint(self) -> None:
        """「裁决医师与时间」只出现在缺口提示里，不构成任何判据。

        规格 R02 的正文要求「必须记录…裁决医师与时间」，但 M 列给系统的动作
        只有「记录并停止自动术式推荐」。签名是医院流程的事，本系统不采集——
        曾经把它读成两个输入字段（``D3.ATTEST_ACTOR`` / ``D3.ATTEST_AT``）
        并驱动规则分档，那是越界，已删除。
        """
        outcome = d3_3(_d33_state())
        assert any("须在病历中补记裁决医师与时间" in gap for gap in outcome.gaps)
        assert not any("ATTEST" in key for key in outcome.verdicts)

    # ---- 终点语言 ----

    def test_the_terminal_keeps_the_upstream_direction(self) -> None:
        """终点要同时说「定下来的处置」和「此前算出的方向」。"""
        from src.tools.guideline_engine.state import NodeOutcome

        state = _d33_state()
        state.visited.append(
            NodeOutcome(node_id="D3.2", verdicts={}, entries=(), terminal="EVAR 一线")
        )
        outcome = d3_3(state)
        assert outcome.terminal is not None and "上游术式方向：EVAR 一线" in outcome.terminal


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
        outcome = self._d32(n={"evar_ifu_status": "未核验"})
        assert outcome.verdicts["D3.2.R01"] is None
        assert outcome.verdicts["D3.2.R04"] is True
        assert outcome.terminal is not None and "候选" in outcome.terminal
        assert any("IFU 未核验" in gap for gap in outcome.gaps)

    def test_ifu_mismatch_switches_to_osr(self) -> None:
        """IFU 明确不符 → 评估 OSR（规格 R02，P1）。"""
        outcome = self._d32(n={"evar_ifu_status": "不符合"})
        assert outcome.verdicts["D3.2.R02"] is True
        assert outcome.terminal is not None and "OSR" in outcome.terminal

    def test_bilateral_access_occlusion_switches_to_osr(self) -> None:
        """双侧入路闭塞 → EVAR 不可行 → 评估 OSR。"""
        outcome = self._d32(n={"access_occlusion": "双侧闭塞"})
        assert outcome.verdicts["D3.2.R02"] is True

    def test_unilateral_access_occlusion_keeps_evar(self) -> None:
        """单侧闭塞不阻断：对侧仍可建立入路，不得把病人推去开放手术。"""
        outcome = self._d32(n={"access_occlusion": "单侧闭塞", "evar_ifu_status": "未核验"})
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
        outcome = self._d32(n={"evar_ifu_status": "未核验"})
        assert outcome.verdicts["D3.2.R04"] is True
        assert any("TEAM_RESOURCE" in gap for gap in outcome.gaps)

    def test_borderline_sbp_withholds_first_line(self) -> None:
        """SBP 82 属临界：不判「可评估」，故不给一线，但仍可给候选。"""
        outcome = self._d32(v={"sbp": 82.0}, n={"evar_ifu_status": "未核验"})
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
        got = self._candidates(n={"evar_ifu_status": "符合"}, v={"sbp": 120.0})
        assert got["EVAR"].status is CandidateStatus.FEASIBLE
        assert "D3.2.R01" in got["EVAR"].rule_ids

    def test_unverified_ifu_downgrades_to_conditional(self) -> None:
        got = self._candidates(n={"evar_ifu_status": "未核验"})
        assert got["EVAR"].status is CandidateStatus.CONDITIONAL
        assert "IFU 未核验" in got["EVAR"].reason

    def test_ifu_mismatch_is_exclusion_not_unknown(self) -> None:
        """IFU 明确不符是**排除**，不是「未评估」——两者临床含义相反。"""
        got = self._candidates(n={"evar_ifu_status": "不符合"})
        assert got["EVAR"].status is CandidateStatus.EXCLUDED

    def test_proximal_seal_alone_never_counts_as_whole_device_ifu(self) -> None:
        """近端密封区符合 ≠ 整体 IFU 符合（规格 ``N.SEAL_IFU`` 判据列明写）。

        两个字段合并时，这里会给「可行候选（IFU 符合）」；拆开后必须降为条件性
        候选，并在理由里说清「只核了近端」。整例也不再宣称「EVAR 一线」。
        """
        case = make_case(
            c={"extrav": "明确", "extrav_contig": "明确连续"},
            q={"cta_enhance": "适当", "cta_phase": "动脉期"},
            n={"seal_ifu": "符合", "evar_ifu_status": None},
        )
        outcome = d3_2(GuidelineState(case=case, route=route(case)))
        evar = _candidate(outcome, "EVAR")
        assert evar.status is CandidateStatus.CONDITIONAL
        assert "仅近端密封区已确认" in evar.reason
        assert "IFU 符合" not in evar.reason
        assert outcome.verdicts["D3.2.R01"] is not True
        assert outcome.terminal is not None and "候选" in outcome.terminal

    def test_overall_ifu_mismatch_wins_over_a_conforming_proximal_seal(self) -> None:
        """近端符合、整体不符合 → 排除 EVAR。合并字段时这个输入根本表达不出来。"""
        got = self._candidates(n={"seal_ifu": "符合", "evar_ifu_status": "不符合"})
        assert got["EVAR"].status is CandidateStatus.EXCLUDED

    def test_bilateral_access_occlusion_is_exclusion(self) -> None:
        got = self._candidates(n={"access_occlusion": "双侧闭塞"})
        assert got["EVAR"].status is CandidateStatus.EXCLUDED
        assert "入路" in got["EVAR"].reason

    def test_unilateral_access_occlusion_is_not_exclusion(self) -> None:
        """单侧闭塞不排除：对侧仍可建立入路。"""
        got = self._candidates(n={"access_occlusion": "单侧闭塞", "evar_ifu_status": "未核验"})
        assert got["EVAR"].status is CandidateStatus.CONDITIONAL

    def test_missing_anatomy_is_unassessed_not_excluded(self) -> None:
        """瘤颈参数全缺 → 未评估。不得写成排除（那等于替医生下了结论）。"""
        got = self._candidates(n={"neck_len": None, "neck_d": None, "neck_angle": None})
        assert got["EVAR"].status is CandidateStatus.UNASSESSED

    def test_osr_is_always_mdt_pending(self) -> None:
        """OSR 恒为「需 MDT 评估」——VascCase 无开放解剖字段，没有判据就不下结论。"""
        for overrides in (
            {},
            {"n": {"evar_ifu_status": "不符合"}},
            {"n": {"access_occlusion": "双侧闭塞"}},
        ):
            got = self._candidates(**overrides)
            assert got["OSR"].status is CandidateStatus.MDT

    def test_osr_reason_changes_when_evar_excluded(self) -> None:
        """EVAR 被排除时 OSR 成为唯一方向——理由必须跟着变，否则读者分不清优先级。"""
        normal = self._candidates()["OSR"].reason
        evar_out = self._candidates(n={"evar_ifu_status": "不符合"})["OSR"].reason
        assert normal != evar_out
        assert "唯一候选" in evar_out

    def test_unknown_access_is_conditional_with_named_gap(self) -> None:
        """入路字段缺失 → 不排除也不确认，落条件性并点名缺什么。"""
        got = self._candidates(n={"access_occlusion": None})
        assert got["EVAR"].status is CandidateStatus.CONDITIONAL
        assert "入路可行性未评估" in got["EVAR"].reason

    def test_uncertain_access_value_is_not_exclusion(self) -> None:
        """「未知」是有值但值不确定 → 只能不判，不得当成排除。"""
        got = self._candidates(n={"access_occlusion": "未知", "evar_ifu_status": "未核验"})
        assert got["EVAR"].status is CandidateStatus.CONDITIONAL

    def test_declining_consciousness_blocks_feasible_status(self) -> None:
        """新发意识下降 → 患者不可评估 → EVAR 不给「可行候选」。

        这条同时守住一个曾存在的死分支：代码曾比较「新发意识下降」，而模型
        的 ``Literal`` 是「新发下降」——两者永不相等，导致该判断从没生效。
        """
        got = self._candidates(n={"evar_ifu_status": "符合"}, v={"conscious_change": "新发下降"})
        assert got["EVAR"].status is CandidateStatus.CONDITIONAL
        assert "不稳定" in got["EVAR"].reason

    def test_baseline_unknown_consciousness_is_not_declining(self) -> None:
        """「基线未知」≠ 新发下降：不当作恶化，血压稳定时仍判可评估。

        与上一条成对：如果这两个值被写成同一个分支，新发下降就再也拦不住
        「可行候选」——急诊最该降级的情形反而放行。
        """
        got = self._candidates(n={"evar_ifu_status": "符合"}, v={"conscious_change": "基线未知"})
        assert got["EVAR"].status is CandidateStatus.FEASIBLE

    def test_missing_sbp_is_unknown_not_unstable(self) -> None:
        """血压缺失 ≠ 不稳定，只能判 UNKNOWN。"""
        got = self._candidates(n={"evar_ifu_status": "符合"}, v={"sbp": None})
        assert got["EVAR"].status is CandidateStatus.CONDITIONAL
        assert "患者状态待明确" in got["EVAR"].reason

    def test_no_candidate_mentions_device_model(self) -> None:
        """粒度红线：候选里不得出现品牌/SKU/尺寸——规格 5 处明文禁止。"""
        got = self._candidates(n={"evar_ifu_status": "符合"})
        for cand in got.values():
            text = cand.render()
            for banned in ("SKU", "品牌", "型号", "mm 支架", "Endurant", "Excluder"):
                assert banned not in text


class TestD31CtaPath:
    """``D3.1.R03/R04`` —— 判据已实现，但端到端被 ``R01``/``R02`` 遮蔽。

    这两条的输入空间被 P0-A 层吃干净了：``class_output`` 含 ``D3`` 只在
    ``D3_EMERGENCY_RULES``（G03 九条）命中时发生，而那九条全部落在
    ``_D3_CONFIRMED`` / ``_D3_SUSPECTED`` 里（见
    ``test_the_two_rule_sets_cover_every_d3_label``）。所以它们
    只在手动调用、或将来 R01/R02 覆盖范围收缩时执行。

    仍然要写真判据：占位 ``return None`` 与「实现了但被遮蔽」在工程上是两件事
    ——后者不是待办，前者是。
    """

    def _state(self, cl: dict[str, object] | None = None, **inputs: object) -> GuidelineState:
        case = make_case(cl={"aaa_rupture": "疑似", **(cl or {})})
        return GuidelineState(
            case=case, route=route(case), node_inputs={"D3": dict(inputs)} if inputs else {}
        )

    # ---- R03：能不能等 CTA ----

    def test_a_stable_patient_can_wait_for_cta(self) -> None:
        from src.tools.guideline_engine.nodes.d3_ruptured import _d31_r03

        assert _d31_r03(self._state(**{"D3.CTA_FEASIBLE": "可以"})) is True

    def test_an_unstable_patient_cannot_wait(self) -> None:
        """``不可以`` 是确定的「不能等」，不是「判不了」——返 False 才让它落到 R04。"""
        from src.tools.guideline_engine.nodes.d3_ruptured import _d31_r03

        assert _d31_r03(self._state(**{"D3.CTA_FEASIBLE": "不可以"})) is False

    def test_an_uncertain_verdict_is_unknown(self) -> None:
        """规格 J 列：「不确定→紧急团队判断」——系统不替它判。"""
        from src.tools.guideline_engine.nodes.d3_ruptured import _d31_r03

        assert _d31_r03(self._state(**{"D3.CTA_FEASIBLE": "不确定"})) is None

    def test_an_unrecorded_field_falls_back_to_vitals(self) -> None:
        """``D3.CTA_FEASIBLE`` 未记录 ≠ 判不了 —— 该行允许值列本就含 V.SBP。

        与上一条的区别：**「不确定」是医生说的「判不了」，「没填」只是没记**。
        后者可以由生命体征回答同一个问题（稳不稳到能等 CTA）；前者系统不该替
        医生判。两者在 ``eq`` 里都折成 ``None``，所以判据先读原始值区分。
        """
        from src.tools.guideline_engine.nodes.d3_ruptured import _d31_r03

        # 基线 SBP 120 → 稳定 → 能等
        assert _d31_r03(self._state()) is True

        unstable = make_case(cl={"aaa_rupture": "疑似"}, v={"sbp": 70.0})
        assert (
            _d31_r03(GuidelineState(case=unstable, route=route(unstable), node_inputs={})) is False
        )

        # 生命体征也缺 → 判不了
        blank = make_case(cl={"aaa_rupture": "疑似"}, v={"sbp": None})
        assert _d31_r03(GuidelineState(case=blank, route=route(blank), node_inputs={})) is None

    # ---- R04：影像不够，临床还在怀疑 ----

    def test_missing_cta_with_clinical_suspicion_keeps_the_emergency_state(self) -> None:
        from src.tools.guideline_engine.nodes.d3_ruptured import _d31_r04

        assert _d31_r04(self._state(**{"D3.CTA_COVERAGE": "未做"})) is True

    def test_unevaluable_cta_with_an_unresolved_grade_also_counts(self) -> None:
        from src.tools.guideline_engine.nodes.d3_ruptured import _d31_r04

        state = self._state(cl={"aaa_rupture": "无法评价"}, **{"D3.CTA_COVERAGE": "不可评价"})
        assert _d31_r04(state) is True

    def test_adequate_coverage_does_not_enter_this_rule(self) -> None:
        from src.tools.guideline_engine.nodes.d3_ruptured import _d31_r04

        state = self._state(**{"D3.CTA_COVERAGE": "腹主动脉至髂股入路"})
        assert _d31_r04(state) is False

    def test_missing_cta_without_suspicion_is_false(self) -> None:
        """影像没做，但破裂状态与报告都不支持怀疑 —— 不是「影像不足以判」。"""
        from src.tools.guideline_engine.nodes.d3_ruptured import _d31_r04

        case = make_case(cl={"aaa_rupture": "明确"}, c={"rupture_report": "明确未见"})
        state = GuidelineState(
            case=case, route=route(case), node_inputs={"D3": {"D3.CTA_COVERAGE": "未做"}}
        )
        assert _d31_r04(state) is False

    # ---- 结构：为什么端到端到不了这两条 ----

    def test_the_end_to_end_case_is_resolved_at_the_p0_layer(self) -> None:
        """P0-A 命中即 return，``verdicts`` 里根本没有 P3 两条。"""
        case = _rupture_case()
        outcome = d3_1(GuidelineState(case=case, route=route(case)))
        assert outcome.verdicts["D3.1.R01"] is True
        assert "D3.1.R03" not in outcome.verdicts
        assert "D3.1.R04" not in outcome.verdicts

    def test_the_two_rule_sets_cover_every_d3_label(self) -> None:
        """``D3`` 标签只来自 G03 九条，而 R01/R02 的判据集合**恰好**覆盖那九条。

        这条断言是 ``D3.1.R03/R04``「求值不可达」的**结构依据**：只要
        ``_D3_CONFIRMED | _D3_SUSPECTED`` 与预置标签集合相等，P0-A 两条就必有一条
        先命中，P3 两条永远轮不到。将来往 ``EMERGENCY_LABELS`` 里加 ``D3`` 规则却
        没落进这两个集合，这里会先炸——否则那两条 P3 会突然变得可达，
        而「不可达」的注释还留在原地。
        """
        from src.rules.router import D3_EMERGENCY_RULES
        from src.tools.guideline_engine.nodes.d3_ruptured import _D3_CONFIRMED, _D3_SUSPECTED

        assert _D3_CONFIRMED | _D3_SUSPECTED == set(D3_EMERGENCY_RULES)
        assert _D3_CONFIRMED & _D3_SUSPECTED == frozenset()


class TestD3ResourceCoverage:
    """D3.2 的资源三条 —— 修前 ``R03``/``R06``/``R07`` 一条都没生效过。"""

    def _d32(self, resource: str | None = None):
        case = make_case(cl={"aaa_rupture": "明确"})
        inputs = {} if resource is None else {"D3": {"D3.TEAM_RESOURCE": resource}}
        return d3_2(GuidelineState(case=case, route=route(case), node_inputs=inputs))

    def test_clinical_suspicion_hits_the_suspected_rule(self) -> None:
        """``D3.1.R02``：疑似背景 **且** 血流动力学不稳定，两段缺一不可。

        修前只判前一段（``route.rule_id ∈ _D3_SUSPECTED``），于是血压 120 的
        疑似病例也命中，危险提示里出现「疑似 rAAA 背景下 SBP<80 mmHg」而病人
        血压是 120 —— 陈述与事实对不上。规格 G 列是两段合取。
        """
        # 稳定：不命中（修严后的关键行为）
        stable = make_case(cl={"aaa_rupture": "疑似"}, c={"rupture_report": "疑似破裂"})
        stable_route = route(stable)
        assert stable_route.rule_id == "G03.R07"
        stable_out = d3_1(GuidelineState(case=stable, route=stable_route))
        assert stable_out.verdicts["D3.1.R02"] is False
        # 链路不断：稳定 + 未记录 D3.CTA_FEASIBLE → R03 按生命体征接手
        assert stable_out.next_node == "D3.2"

        # 不稳定：命中
        unstable = make_case(
            cl={"aaa_rupture": "疑似"}, c={"rupture_report": "疑似破裂"}, v={"sbp": 70.0}
        )
        unstable_route = route(unstable)
        assert unstable_route.rule_id == "G03.R04"
        outcome = d3_1(GuidelineState(case=unstable, route=unstable_route))
        assert outcome.verdicts["D3.1.R02"] is True
        assert outcome.escalation == "G05"
        # 规格 M 列对 ``R02`` 只写「G05 急诊模式；快速控制/修复评估」——
        # 值域里没有 D3.2。血压 70 的病人先稳定，不该同时讨论择期术式候选。
        assert outcome.next_node is None

    def test_center_without_capacity_transfers_instead_of_operating(self) -> None:
        """院方明确做不了 → 转诊/现场救援，不能说「EVAR 一线」。

        修前 ``R03`` 拿「本中心无法实施」去比 ``D3.TEAM_RESOURCE``——那个字符串
        不在字段值域里（正确值是「均不可立即用」），所以恒为 False；同时 ``R01``
        完全不看资源，于是报告会写「EVAR 一线」，让团队去做本院做不到的手术。
        """
        outcome = self._d32("均不可立即用")
        assert outcome.verdicts["D3.2.R01"] is False
        assert outcome.verdicts["D3.2.R03"] is True
        assert outcome.terminal == "急诊交接／转诊／现场救援"

    def test_an_unrecorded_resource_list_does_not_block_first_line(self) -> None:
        """未记录 ≠ 不可用（规格 R90 原话「未确认不等于不可用」）。"""
        outcome = self._d32()
        assert outcome.verdicts["D3.2.R01"] is True
        assert outcome.terminal is not None and "一线" in outcome.terminal

    @pytest.mark.parametrize("resource", [None, "未知", "证据冲突", "均不可立即用"])
    def test_resource_gap_is_shown_to_the_reader(self, resource: str | None) -> None:
        """``R06``：资源未确认或有缺项 → 把缺失清单摆出来（展示性规则）。"""
        assert self._d32(resource).verdicts["D3.2.R06"] is True

    def test_confirmed_capacity_needs_no_resource_checklist(self) -> None:
        assert self._d32("两者可用").verdicts["D3.2.R06"] is False

    def test_unavailable_capacity_downgrades_both_candidates(self) -> None:
        """终点说「转诊」，候选栏就不能说「EVAR 可行」。

        修前只改了 ``R03`` 的出口，候选仍由 ``_evar_candidate`` 按解剖判，
        同一份报告里出现「急诊交接／转诊」与「EVAR：可行候选」两句相反的话。
        档位取 ``MDT`` 而不是 ``EXCLUDED``：否定的不是术式，是「本院此刻能不能做」。
        """
        statuses = {c.procedure: c.status for c in self._d32("均不可立即用").candidates}
        assert statuses["EVAR"] is CandidateStatus.MDT
        assert statuses["OSR"] is CandidateStatus.MDT

    def test_confirmed_capacity_keeps_evar_feasible(self) -> None:
        statuses = {c.procedure: c.status for c in self._d32("两者可用").candidates}
        assert statuses["EVAR"] is CandidateStatus.FEASIBLE

    def test_parallel_preparation_always_applies_inside_this_node(self) -> None:
        """``R07``：进了 D3.2 就是破裂病例，并行复苏/备血/手术室恒适用。"""
        for resource in (None, "两者可用", "均不可立即用"):
            assert self._d32(resource).verdicts["D3.2.R07"] is True

    def test_display_rules_are_recorded_but_hold_no_slot(self) -> None:
        """``R06``/``R07`` 只留痕、不占出口 —— 结构性断言，防有人把它们挪回槽位。

        它们原来在 ``_D32_SLOTS`` 里，判据恒 ``None`` 才不抢出口。那种写法的问题
        不是「保守」而是「永不生效」：``R07`` 恒真的话它会抢在 ``R91`` 前面，
        恒 ``None`` 的话它连记录都留不下。
        """
        from src.tools.guideline_engine.nodes.d3_ruptured import (
            _D32_DISPLAY_RULES,
            _D32_SLOTS,
        )

        slot_ids = {slot.rule_id for slot in _D32_SLOTS}
        display_ids = {rule.rule_id for rule in _D32_DISPLAY_RULES}
        assert display_ids == {"D3.2.R06", "D3.2.R07"}
        assert not (display_ids & slot_ids)

        outcome = self._d32("两者可用")
        assert "D3.2.R06" in outcome.verdicts
        assert "D3.2.R07" in outcome.verdicts
        assert outcome.terminal == "EVAR 一线（术者即时确认）"


class TestShadowedFallbacks:
    """三条「恒真但被遮蔽」的兜底 —— 执行不到，至少要把函数体跑一遍。

    它们不是死代码：``R91`` 的职责是「以后新增征象字段时不会漏掉兜底」。
    但现在输入空间被 R01/R02/R90 分完了，任何用例都到不了这里，所以只能直调判据。
    """

    def test_d13_r08_only_fires_when_both_directions_are_blocked(self) -> None:
        """腔内走不通 + 当前不能开刀 → MDT/转诊，而不是「不可手术」。"""
        case = make_case(
            cl={"aaa_diam": 56.0, "aaa_rupture": "未见相关证据", "diag_multi": None},
            n={"seal_ifu": "不符合"},
        )
        state = GuidelineState(
            case=case, route=route(case), node_inputs={"D1": {"D1.OP_FITNESS": "当前不适合"}}
        )
        outcome = d1_3(state)
        assert outcome.verdicts["D1.3.R08"] is True
        assert outcome.terminal is not None and "MDT" in outcome.terminal

    def test_d13_r08_stays_false_when_surgery_is_still_possible(self) -> None:
        case = make_case(
            cl={"aaa_diam": 56.0, "aaa_rupture": "未见相关证据", "diag_multi": None},
            n={"seal_ifu": "不符合"},
        )
        state = GuidelineState(
            case=case, route=route(case), node_inputs={"D1": {"D1.OP_FITNESS": "可接受"}}
        )
        assert d1_3(state).verdicts["D1.3.R08"] is False

    def test_d22_r91_is_a_constant_exit(self) -> None:
        from src.tools.guideline_engine.nodes.d2_symptomatic import _d22_r91

        state = GuidelineState(case=make_case(), route=route(make_case()))
        assert _d22_r91(state) is True

    def test_d52_r91_is_a_constant_exit(self) -> None:
        from src.tools.guideline_engine.nodes.d5_special import _d52_r91

        state = GuidelineState(case=make_case(), route=route(make_case()))
        assert _d52_r91(state) is True


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


# ===========================================================================
# D1.1 诊断与直径分层
# ===========================================================================
class TestD11DiameterStratification:
    """D1.1 的核心是「性别 + 直径 + 测量方法」三件套，三者缺一不可。

    规格在这里埋了一条硬约束（D1.SEX_THRESHOLD_SOURCE）：「若仅有 CTA 外径，
    不可未经方法核对直接套用超声阈值」。所以本组测试有一半在守「**不该判**」
    的情形——这比守「该判」更重要，因为误判阈值会直接改变手术与否。
    """

    @staticmethod
    def _outcome(**overrides):
        case = make_case(**overrides)
        return d1_1(GuidelineState(case=case, route=route(case), node_inputs=_D1_METHOD))

    def test_male_below_threshold_goes_to_surveillance(self) -> None:
        outcome = self._outcome(cl={"aaa_diam": 42.0}, p={"sex": "男"})
        assert outcome.verdicts["D1.1.R03"] is True
        assert outcome.terminal is not None and "监测" in outcome.terminal

    def test_female_below_threshold_goes_to_surveillance(self) -> None:
        outcome = self._outcome(cl={"aaa_diam": 48.0}, p={"sex": "女"})
        assert outcome.verdicts["D1.1.R04"] is True

    def test_male_at_threshold_goes_to_d12(self) -> None:
        """男 55 mm 是「达到」不是「低于」——边界必须含等号。"""
        outcome = self._outcome(cl={"aaa_diam": 55.0}, p={"sex": "男"})
        assert outcome.verdicts["D1.1.R05"] is True
        assert outcome.next_node == "D1.2"

    def test_female_threshold_differs_from_male(self) -> None:
        """50 mm 女性已达标、男性未达标 —— 守性别阈值没有被写成同一个数。"""
        female = self._outcome(cl={"aaa_diam": 50.0}, p={"sex": "女"})
        male = self._outcome(cl={"aaa_diam": 50.0}, p={"sex": "男"})
        assert female.verdicts["D1.1.R05"] is True
        assert male.verdicts["D1.1.R05"] is False
        assert male.verdicts["D1.1.R03"] is True

    def test_unknown_method_never_applies_threshold(self) -> None:
        """方法未知 → 不套阈值，且缺口必须点名缺的是什么。

        这是规格「仅有 CTA 外径不可直接套用超声阈值」的机械落点。若无此约束，
        42 mm 会被直接判成「未达阈值」，而实际上测量方法可能让结论翻转。
        """
        case = make_case(cl={"aaa_diam": 56.0}, p={"sex": "男"})
        outcome = d1_1(GuidelineState(case=case, route=route(case)))
        assert outcome.verdicts["D1.1.R05"] is None
        assert outcome.verdicts["D1.1.R03"] is None
        assert any("测量方法" in gap for gap in outcome.gaps)

    def test_cta_method_requires_specialist_confirmation(self) -> None:
        """CTA 外径明确标为「需专科确认」，不是「未知」——两者处理方式不同。"""
        case = make_case(cl={"aaa_diam": 56.0}, p={"sex": "男"})
        outcome = d1_1(
            GuidelineState(
                case=case,
                route=route(case),
                node_inputs={"D1": {"D1.DIAM_METHOD": "CTA中心线正交外径"}},
            )
        )
        assert outcome.verdicts["D1.1.R05"] is None
        assert any("需专科确认" in gap for gap in outcome.gaps)

    def test_unknown_sex_falls_back_to_generic_surveillance(self) -> None:
        """性别未知 → 只给方向不给间隔，不替它猜一个阈值。"""
        outcome = self._outcome(cl={"aaa_diam": 42.0}, p={"sex": "未知"})
        assert outcome.verdicts["D1.1.R03"] is None
        assert outcome.verdicts["D1.1.R91"] is True
        assert outcome.terminal is not None and "监测" in outcome.terminal


class TestD11SurveillanceInterval:
    """随访间隔按性别与直径分档（D1.1.R06）—— 分档错了会漏掉该早复查的病人。"""

    @staticmethod
    def _interval(diam: float, sex: str) -> str | None:
        from src.tools.guideline_engine.nodes.d1_asymptomatic import _surveillance_interval

        case = make_case(cl={"aaa_diam": diam}, p={"sex": sex})
        return _surveillance_interval(GuidelineState(case=case, route=route(case)))

    def test_male_bands(self) -> None:
        assert self._interval(35.0, "男") == "每 3 年 1 次"
        assert self._interval(42.0, "男") == "每年 1 次"
        assert self._interval(52.0, "男") == "每 6 个月 1 次"

    def test_female_bands_start_earlier(self) -> None:
        """女性 45 岁起就要每 6 个月 —— 与男性的 50 不同档。"""
        assert self._interval(42.0, "女") == "每年 1 次"
        assert self._interval(46.0, "女") == "每 6 个月 1 次"

    def test_below_30_mm_has_no_surveillance_plan(self) -> None:
        """<30 mm 不是 AAA，套监测计划没有意义。"""
        assert self._interval(25.0, "男") is None

    def test_interval_is_appended_to_monitoring_terminal(self) -> None:
        case = make_case(cl={"aaa_diam": 42.0}, p={"sex": "男"})
        outcome = d1_1(GuidelineState(case=case, route=route(case), node_inputs=_D1_METHOD))
        assert outcome.terminal is not None and "每年 1 次" in outcome.terminal
        assert any(e.rule_id == "D1.1.R06" for e in outcome.entries)


class TestD11Branches:
    """P0 危险与特殊形态分支：走错会导致病例被归入错误的病种路径。"""

    @staticmethod
    def _outcome(**overrides):
        case = make_case(**overrides)
        return d1_1(GuidelineState(case=case, route=route(case), node_inputs=_D1_METHOD))

    def test_symptomatic_case_is_escalated(self) -> None:
        """无症状岗位收到有症状病例 = 标签与事实冲突，退回判定层重判。"""
        outcome = self._outcome(cl={"symptom_aaa": "医师确认相关"})
        assert outcome.verdicts["D1.1.R01"] is True
        assert outcome.escalation == "G03"

    def test_unevaluable_rupture_is_escalated(self) -> None:
        """破裂「无法评价」不得留在 D1 —— 不能因影像不可评就当作未破裂。"""
        outcome = self._outcome(cl={"aaa_rupture": "无法评价"})
        assert outcome.verdicts["D1.1.R01"] is True
        assert outcome.escalation == "G03"

    def test_saccular_lesion_goes_to_special_assessment(self) -> None:
        """囊状病变 → D5.3（规格 R07 的逐项分派，不是一个笼统终点）。"""
        outcome = self._outcome(cl={"saccular": "明确"})
        assert outcome.verdicts["D1.1.R07"] is True
        assert outcome.next_node == "D5.3"
        assert outcome.next_nodes == ("D5.3",)
        assert outcome.terminal is None, "给了确定去处就不该停在本节点"

    def test_infection_goes_to_d41_not_to_the_morphology_node(self) -> None:
        """感染 → D4.1。规格把「感染或瘘」单列一档，不能笼统走 D5.3。"""
        outcome = self._outcome(n={"infection_ctx": "医师明确"})
        assert outcome.verdicts["D1.1.R07"] is True
        assert outcome.next_node == "D4.1"

    def test_coexisting_etiologies_dispatch_in_parallel(self) -> None:
        """「多标签可以并存」：炎症 + 遗传 → D5.3 与 D1.2 都要去。

        串行只走一个时，另一半在报告里彻底消失——而规格明写两者并存。
        """
        outcome = self._outcome(
            n={"inflammatory_ctx": "影像高度提示", "genetic_ctx": "疑似遗传背景"}
        )
        assert outcome.verdicts["D1.1.R07"] is True
        assert set(outcome.next_nodes) == {"D5.3", "D1.2"}
        assert outcome.link is Link.PARALLEL
        assert outcome.next_node == "D5.3", "主出口取紧迫度最高的那个"

    def test_special_morphology_blocks_threshold_path(self) -> None:
        """特殊形态不套普通梭形阈值：即使直径达阈值也不进 D1.2。

        ``R05`` 记 UNKNOWN 而非 False —— 「是否达阈值」在特殊形态下无法用普通
        阈值回答，记 False 会被读成「确认未达阈值」，那是另一个错误结论。
        """
        outcome = self._outcome(cl={"aaa_diam": 60.0, "pseudo": "明确"})
        assert outcome.verdicts["D1.1.R05"] is None
        assert outcome.verdicts["D1.1.R07"] is True
        assert outcome.next_node == "D5.3", "去特殊形态评估，不是 D1.2 的阈值分层"

    def test_genetic_clue_routes_to_d12(self) -> None:
        outcome = self._outcome(n={"genetic_ctx": "疑似遗传背景"})
        assert outcome.verdicts["D1.1.R10"] is True
        assert outcome.next_node == "D1.2"

    def test_early_onset_counts_as_genetic_clue(self) -> None:
        """首次发病 <60 岁也算线索 —— 不要求已确诊遗传背景。"""
        case = make_case(
            cl={"aaa_diam": 42.0},
            n={"genetic_ctx": "无已知阳性线索"},
            gen={"aaa_age_onset": 55},
        )
        outcome = d1_1(GuidelineState(case=case, route=route(case), node_inputs=_D1_METHOD))
        assert outcome.verdicts["D1.1.R10"] is True

    def test_rapid_growth_is_marked_for_recheck(self) -> None:
        """年化 ≥10 mm/年只标记「待复核」，不得直接作为修复指征。"""
        case = make_case(cl={"aaa_diam": 42.0})
        outcome = d1_1(
            GuidelineState(
                case=case,
                route=route(case),
                node_inputs={"D1": {**_D1_METHOD["D1"], "D1.GROWTH_RATE": 12.0}},
            )
        )
        assert outcome.verdicts["D1.1.R02"] is True
        assert outcome.terminal is not None and "复核" in outcome.terminal


# ===========================================================================
# D1.2 患者获益与手术风险
# ===========================================================================
#: 四条关键评估都给出「可以往下走」的取值 —— 各测试在此基础上单点覆盖。
_D12_GO: dict[str, str] = {
    "D1.OP_FITNESS": "可接受",
    "D1.PREFERENCE": "倾向修复",
    "D1.LIFE_EXPECTANCY": "较长",
    "D1.PROGNOSIS": "合理预期",
    "D1.SURVEILLANCE_BENEFIT": "有潜在修复获益",
}


def _d12_outcome(d1: dict[str, object] | None = None, **overrides) -> NodeOutcome:
    """造一个「已达修复阈值」的病例 —— 42 mm 基线进不了 D1.2 的 P1/P3 分支。"""
    overrides.setdefault("cl", {"aaa_diam": 56.0})
    case = make_case(**overrides)
    inputs = {"D1": {**_D1_METHOD["D1"], **_D12_GO, **(d1 or {})}}
    return d1_2(GuidelineState(case=case, route=route(case), node_inputs=inputs))


class TestD12BenefitRisk:
    """D1.2 是「分诊台」：不判病人能不能手术，只按医生已有的结论分流。

    所以本组测试守的主要是**不越权**：医生没给的结论，规则既不能补也不能猜；
    医生给了「不适合/寿命有限」时，规则也不得改写成「建议不做」。
    """

    def test_full_agreement_reaches_d13(self) -> None:
        outcome = _d12_outcome()
        assert outcome.verdicts["D1.2.R04"] is True
        assert outcome.next_node == "D1.3"

    def test_genetic_background_goes_to_d13_with_mdt_requirement(self) -> None:
        """遗传病例的出口也是 D1.3，但缺一条 MDT 前置就必须在缺口里看得见。"""
        outcome = _d12_outcome(n={"genetic_ctx": "疑似遗传背景"})
        assert outcome.verdicts["D1.2.R03"] is True
        assert outcome.next_node == "D1.3"
        assert any("MDT" in gap and "个体化" in gap for gap in outcome.gaps)

    def test_unassessed_fitness_is_unknown_not_denied(self) -> None:
        """「未评价」不得被折成「不可接受」——R04 必须记 UNKNOWN 而非 False。"""
        outcome = _d12_outcome({"D1.OP_FITNESS": "未评价"})
        assert outcome.verdicts["D1.2.R04"] is None
        assert outcome.verdicts["D1.2.R05"] is True
        assert outcome.terminal is not None and "待完成风险评估" in outcome.terminal

    def test_missing_monitoring_benefit_blocks_after_the_three_are_set(self) -> None:
        """前三项齐、只缺监测获益 → 落到 R90（不是 R05）。

        用「高风险」把 P1 的 ``R04`` 挡掉：``evaluate_node`` 命中即停，``R04``
        若成立，``R90`` 根本不会执行，这条测试就测不到想测的东西。
        """
        outcome = _d12_outcome({"D1.SURVEILLANCE_BENEFIT": None, "D1.OP_FITNESS": "高风险"})
        assert outcome.verdicts["D1.2.R05"] is not True
        assert outcome.verdicts["D1.2.R90"] is True
        assert outcome.terminal is not None and "补齐" in outcome.terminal

    def test_no_assessment_at_all_asks_for_the_three_key_items(self) -> None:
        outcome = d1_2(GuidelineState(case=make_case(), route=route(make_case())))
        assert outcome.verdicts["D1.2.R05"] is True
        assert outcome.next_node is None
        missing = [gap for gap in outcome.gaps if "D1.OP_FITNESS" in gap]
        assert missing and "未记录" in missing[0]

    def test_limited_life_expectancy_goes_to_non_surgical_exit(self) -> None:
        outcome = _d12_outcome({"D1.LIFE_EXPECTANCY": "有限"})
        assert outcome.verdicts["D1.2.R01"] is True
        assert outcome.terminal is not None and "非手术" in outcome.terminal

    def test_prognosis_alone_can_carry_the_limited_life_conclusion(self) -> None:
        """寿命未评价、长期预后明确有限 → 仍应走到 R07，而不是被 R05 拦下。

        这一条守的是「两个字段合成一个寿命维度」的判法：若各自独立必填，
        本例会被判成「关键评估缺失」而停在人工复核，丢掉 R07 的明确结论。
        """
        outcome = _d12_outcome(
            {"D1.LIFE_EXPECTANCY": None, "D1.PROGNOSIS": "有限（约2–3年或更短）"}
        )
        assert outcome.verdicts["D1.2.R05"] is not True
        assert outcome.verdicts["D1.2.R07"] is True
        assert outcome.terminal is not None and "预后有限" in outcome.terminal

    def test_currently_unfit_goes_to_optimisation_not_to_refusal(self) -> None:
        """「当前不适合」= 优化后复评，不得写成「不建议手术」。"""
        outcome = _d12_outcome({"D1.OP_FITNESS": "当前不适合"})
        assert outcome.verdicts["D1.2.R02"] is True
        assert outcome.terminal is not None and "优化" in outcome.terminal
        assert "不可手术" not in (outcome.terminal or "")

    def test_stopping_surveillance_needs_both_conditions(self) -> None:
        """只有「监测已无获益」+「修复不适宜」同时成立才考虑停止监测。"""
        outcome = _d12_outcome(
            {"D1.OP_FITNESS": "当前不适合", "D1.SURVEILLANCE_BENEFIT": "无明显获益"}
        )
        assert outcome.verdicts["D1.2.R08"] is True
        assert outcome.terminal is not None and "授权医生确认" in outcome.terminal

    def test_no_monitoring_benefit_alone_does_not_stop_surveillance(self) -> None:
        """监测无获益但手术可接受 → 不该停止监测，该去修复。"""
        outcome = _d12_outcome({"D1.SURVEILLANCE_BENEFIT": "无明显获益"})
        assert outcome.verdicts["D1.2.R04"] is True
        assert outcome.next_node == "D1.3"

    def test_high_risk_with_long_life_expectancy_still_compares_options(self) -> None:
        """高风险不等于不可修复：R04 不成立，但 R09 仍把病例送进 D1.3 做并列比较。"""
        outcome = _d12_outcome({"D1.OP_FITNESS": "高风险"})
        assert outcome.verdicts["D1.2.R04"] is False
        assert outcome.verdicts["D1.2.R09"] is True
        assert outcome.next_node == "D1.3"

    def test_optimisable_risk_factors_form_a_checklist(self) -> None:
        outcome = _d12_outcome(
            {
                "D1.LIFE_EXPECTANCY": "不确定",
                "D1.RISK_OPTIMIZATION": ["吸烟", "血压"],
            }
        )
        assert outcome.verdicts["D1.2.R06"] is True
        assert outcome.terminal is not None and "优化清单" in outcome.terminal

    def test_patient_preference_for_surveillance_closes_the_repair_branch(self) -> None:
        """患者选了监测 → R91 的两个分支只剩一个，文案必须跟着变。"""
        outcome = _d12_outcome({"D1.PREFERENCE": "倾向监测"})
        assert outcome.verdicts["D1.2.R91"] is True
        assert outcome.terminal is not None and "监测方案" in outcome.terminal
        assert "D1.3" not in (outcome.terminal or "")

    def test_age_alone_never_terminates_repair(self) -> None:
        """95 岁但医生评估寿命较长 → 不因年龄单项落到非手术出口。"""
        outcome = _d12_outcome(p={"age": 95})
        assert outcome.verdicts["D1.2.R04"] is True
        assert outcome.next_node == "D1.3"

    def test_frailty_missing_does_not_block_p3_exits(self) -> None:
        """衰弱/合并症未评估只进缺口清单，不得把「当前不适合」挡在人工复核。"""
        outcome = _d12_outcome({"D1.OP_FITNESS": "当前不适合"})
        assert outcome.verdicts["D1.2.R90"] is False
        assert any("D1.FRAILTY" in gap for gap in outcome.gaps)


# ===========================================================================
# D1.3 术式候选（EVAR / OSR 两列）
# ===========================================================================
def _d13_outcome(d1: dict[str, object] | None = None, **overrides) -> NodeOutcome:
    overrides.setdefault("cl", {"aaa_diam": 56.0})
    case = make_case(**overrides)
    inputs = {"D1": {**_D1_METHOD["D1"], **_D12_GO, **(d1 or {})}}
    return d1_3(GuidelineState(case=case, route=route(case), node_inputs=inputs))


def _candidate(outcome: NodeOutcome, procedure: str) -> Candidate:
    for candidate in outcome.candidates:
        if candidate.procedure == procedure:
            return candidate
    raise AssertionError(f"没有 {procedure} 这一列：{outcome.candidates}")


class TestD13Candidates:
    """D1.3 的核心是「两列并列」——任何一列被遮蔽都是结论错误。

    ``R01``（EVAR 候选）与 ``R02``（OSR 候选）不是互斥选项，所以它们不进
    出口竞争；本组测试守两件事：两列都在，且每列的档位与理由对得上判据。
    """

    def test_both_columns_are_always_present(self) -> None:
        outcome = _d13_outcome()
        procedures = [c.procedure for c in outcome.candidates]
        assert procedures == ["EVAR", "OSR"]

    def test_standard_evar_is_feasible_only_with_distal_seal(self) -> None:
        outcome = _d13_outcome({"D1.DISTAL_SEAL": "右 25mm / 左 22mm，无病变"})
        evar = _candidate(outcome, "EVAR")
        assert evar.status is CandidateStatus.FEASIBLE
        assert "D1.3.R05" in evar.rule_ids
        assert outcome.verdicts["D1.3.R05"] is True

    def test_missing_distal_seal_degrades_to_conditional(self) -> None:
        """缺远端密封区 → 不能标「标准 EVAR 候选」，且缺口要点名。"""
        outcome = _d13_outcome()
        evar = _candidate(outcome, "EVAR")
        assert evar.status is CandidateStatus.CONDITIONAL
        assert "远端密封区" in evar.reason
        assert any("D1.DISTAL_SEAL" in gap for gap in outcome.gaps)

    def test_branch_involvement_excludes_standard_evar_and_escalates(self) -> None:
        """分支受累 → 标准 EVAR 排除 + 转 G06；不得同时给出「EVAR 候选」。"""
        outcome = _d13_outcome(n={"renal_r": "明确受累"})
        assert _candidate(outcome, "EVAR").status is CandidateStatus.EXCLUDED
        assert outcome.escalation == "G06"
        assert outcome.verdicts["D1.3.R01"] is False

    def test_ifu_mismatch_excludes_evar_and_escalates(self) -> None:
        outcome = _d13_outcome(n={"seal_ifu": "不符合"})
        assert _candidate(outcome, "EVAR").status is CandidateStatus.EXCLUDED
        assert outcome.escalation == "G06"
        assert outcome.verdicts["D1.3.R06"] is True

    def test_bilateral_access_occlusion_excludes_evar_only(self) -> None:
        """单侧闭塞不排除 EVAR，双侧才排除——这里守双侧那一支。"""
        outcome = _d13_outcome(n={"access_occlusion": "双侧闭塞"})
        evar = _candidate(outcome, "EVAR")
        assert evar.status is CandidateStatus.EXCLUDED
        assert "入路" in evar.reason
        assert outcome.escalation is None

    def test_unverified_ifu_stays_conditional_not_excluded(self) -> None:
        """「未核验」是结构性常态（本节点不选型号），不得当成排除或阻断。"""
        outcome = _d13_outcome(n={"seal_ifu": "未核验"})
        evar = _candidate(outcome, "EVAR")
        assert evar.status is CandidateStatus.CONDITIONAL
        assert "IFU 未核验" in evar.reason

    def test_missing_anatomy_is_unassessed_not_excluded(self) -> None:
        """三个瘤颈参数全缺 → 「未评估」，不是「排除」（R90 的草案口径）。"""
        outcome = _d13_outcome(n={"neck_len": None, "neck_d": None, "neck_angle": None})
        evar = _candidate(outcome, "EVAR")
        assert evar.status is CandidateStatus.UNASSESSED
        assert outcome.verdicts["D1.3.R90"] is True
        assert outcome.terminal is not None and "草案" in outcome.terminal

    def test_osr_never_reaches_feasible(self) -> None:
        """开放解剖无输入 → OSR 封顶在「需 MDT 评估」，任何输入组合都不给可行。"""
        for overrides in (
            {},
            {"D1.OP_FITNESS": "高风险"},
            {"D1.OP_FITNESS": "当前不适合"},
            {"D1.LIFE_EXPECTANCY": "有限"},
        ):
            osr = _candidate(_d13_outcome(overrides), "OSR")
            assert osr.status is CandidateStatus.MDT, overrides

    def test_osr_reason_tracks_the_blocking_condition(self) -> None:
        assert "不可接受" in _candidate(_d13_outcome({"D1.OP_FITNESS": "当前不适合"}), "OSR").reason
        assert "高危" in _candidate(_d13_outcome({"D1.OP_FITNESS": "高风险"}), "OSR").reason
        assert (
            "预期寿命有限" in _candidate(_d13_outcome({"D1.LIFE_EXPECTANCY": "有限"}), "OSR").reason
        )

    def test_patient_refusal_goes_to_non_surgical_exit(self) -> None:
        outcome = _d13_outcome({"D1.PREFERENCE": "倾向监测"})
        assert outcome.verdicts["D1.3.R04"] is True
        assert outcome.terminal is not None and "非手术" in outcome.terminal

    def test_display_rules_do_not_steal_the_exit(self) -> None:
        """四条展示性规则都要留痕，但出口仍由 R91 给出（不是 EVAR/OSR 之一）。"""
        outcome = _d13_outcome({"D1.DISTAL_SEAL": "已记录"})
        for rule_id in ("D1.3.R01", "D1.3.R02", "D1.3.R05", "D1.3.R09"):
            assert rule_id in outcome.verdicts, rule_id
        assert outcome.verdicts["D1.3.R91"] is True
        assert outcome.terminal is not None and "候选已列出" in outcome.terminal

    def test_escalation_does_not_leave_a_contradictory_terminal(self) -> None:
        """转复杂模式时不得再写「候选已列出」——两个互斥结论不能同现。"""
        outcome = _d13_outcome(n={"seal_ifu": "不符合"})
        assert outcome.escalation == "G06"
        assert outcome.terminal is None


class TestD13DegradedInputs:
    """输入残缺时 D1.3 的落档 —— 每一格「差什么」都要能在理由里读出来。

    这些分支不进 ``evaluate_node`` 的出口竞争（候选是节点层独立算的），
    所以必须单独守着：否则「条件性候选」会退化成一句无信息的「资料不全」。
    """

    def test_cta_missing_blocks_with_a_draft(self) -> None:
        outcome = _d13_outcome(n={"cta_status": "缺失"})
        assert outcome.verdicts["D1.3.R90"] is True
        assert _candidate(outcome, "EVAR").status is not CandidateStatus.FEASIBLE

    def test_cta_partial_is_unknown_not_missing(self) -> None:
        """「部分可评价」与「缺失」在处理上不同：前者不阻断，只是判不了完整适配。"""
        outcome = _d13_outcome(n={"cta_status": "部分"})
        assert outcome.verdicts["D1.3.R90"] is False
        assert outcome.verdicts["D1.3.R01"] is None

    def test_partial_neck_parameters_are_named_one_by_one(self) -> None:
        outcome = _d13_outcome(n={"neck_d": None, "neck_angle": None})
        evar = _candidate(outcome, "EVAR")
        assert evar.status is CandidateStatus.CONDITIONAL
        assert "瘤颈参数不全" in evar.reason

    def test_ifu_never_recorded_is_named_as_such(self) -> None:
        """``seal_ifu=None`` 与「未核验」不是同一件事：前者根本没记录。"""
        outcome = _d13_outcome(n={"seal_ifu": None})
        evar = _candidate(outcome, "EVAR")
        assert "IFU 未记录" in evar.reason
        assert any("N.SEAL_IFU" in gap and "未记录" in gap for gap in outcome.gaps)

    def test_unassessed_fitness_leaves_osr_unassessed(self) -> None:
        outcome = _d13_outcome({"D1.OP_FITNESS": "未评价"})
        osr = _candidate(outcome, "OSR")
        assert osr.status is CandidateStatus.UNASSESSED
        assert "未评价" in osr.reason

    def test_unrecorded_access_and_cta_become_explicit_gaps(self) -> None:
        outcome = _d13_outcome(n={"access_occlusion": None, "cta_status": None})
        joined = " ".join(outcome.gaps)
        assert "入路可行性未评估" in joined
        assert "N.CTA_STATUS" in joined

    def test_incomplete_anatomy_is_reported_as_a_gap(self) -> None:
        outcome = _d13_outcome(n={"neck_len": None})
        assert any("EVAR 解剖参数不全" in gap for gap in outcome.gaps)


class TestD11RemainingBranches:
    """D1.1 的边角分支 —— 补在这里而不是散进主类，避免主类被细节淹没。"""

    def test_female_below_40_gets_three_year_interval(self) -> None:
        from src.tools.guideline_engine.nodes.d1_asymptomatic import _surveillance_interval

        case = make_case(cl={"aaa_diam": 35.0}, p={"sex": "女"})
        assert (
            _surveillance_interval(GuidelineState(case=case, route=route(case))) == "每 3 年 1 次"
        )

    def test_recorded_family_history_counts_as_a_genetic_clue(self) -> None:
        """家族史阳性 = 线索；「未询问」不等于阴性（词表已含该写法）。"""
        case = make_case(cl={"aaa_diam": 42.0}, n={"genetic_ctx": "无已知阳性线索"})
        inputs = {"D1": {**_D1_METHOD["D1"], "D1.FAMILY_HISTORY": "阳性"}}
        outcome = d1_1(GuidelineState(case=case, route=route(case), node_inputs=inputs))
        assert outcome.verdicts["D1.1.R10"] is True

    def test_unasked_family_history_is_unknown_not_positive(self) -> None:
        case = make_case(cl={"aaa_diam": 42.0}, n={"genetic_ctx": "未评价"})
        inputs = {"D1": {**_D1_METHOD["D1"], "D1.FAMILY_HISTORY": "未询问"}}
        outcome = d1_1(GuidelineState(case=case, route=route(case), node_inputs=inputs))
        assert outcome.verdicts["D1.1.R10"] is None

    def test_missing_diameter_blocks_the_diagnosis_row(self) -> None:
        case = make_case(cl={"aaa_diam": None})
        outcome = d1_1(GuidelineState(case=case, route=route(case), node_inputs=_D1_METHOD))
        assert outcome.verdicts["D1.1.R90"] is True

    def test_cta_threshold_source_needs_specialist_conversion(self) -> None:
        """来源明确标为「CTA 需转换」→ 阈值不可用，且缺口要说清不是「没记录」。"""
        case = make_case(cl={"aaa_diam": 56.0}, p={"sex": "男"})
        inputs = {
            "D1": {
                "D1.DIAM_METHOD": "CTA中心线正交外径",
                "D1.SEX_THRESHOLD_SOURCE": "CTA需转换或专科确认",
            }
        }
        outcome = d1_1(GuidelineState(case=case, route=route(case), node_inputs=inputs))
        assert outcome.verdicts["D1.1.R05"] is None
        assert any("需专科确认" in gap for gap in outcome.gaps)


class TestRecordedHelper:
    """``recorded`` 的三种值形态 —— 单值 / 多选 / 结构化数值，缺一都不算记录。"""

    def test_scalar_value_counts_as_recorded(self) -> None:
        outcome = _d13_outcome({"D1.DISTAL_SEAL": 25.0})
        assert _candidate(outcome, "EVAR").status is CandidateStatus.FEASIBLE

    def test_empty_list_is_not_recorded(self) -> None:
        outcome = _d13_outcome({"D1.DISTAL_SEAL": []})
        assert _candidate(outcome, "EVAR").status is CandidateStatus.CONDITIONAL

    def test_uncertain_token_is_not_recorded(self) -> None:
        outcome = _d13_outcome({"D1.DISTAL_SEAL": "未评估"})
        assert _candidate(outcome, "EVAR").status is CandidateStatus.CONDITIONAL


# ===========================================================================
# D2 有症状 AAA
# ===========================================================================
#: 能产出 ``D2`` 标签的病例骨架。
#:
#: 三个坑必须一起避开，否则病例走不到 D2：
#:   · ``diag_multi`` 基线是 ``["D1"]`` —— 与 D2 并存会触发 ``G09.R03`` 病种冲突；
#:   · ``pain_present`` 必须是「否」—— ``G03.R15`` 只在无当前疼痛时放行；
#:   · 症状改由 ``aaa_tender``（触痛）承载 —— 这正是 D2 里「非疼痛症状」的入口。
_D2_CTX: dict[str, object] = {
    "symptom_aaa": "医师确认相关",
    "aaa_diam": 56.0,
    "diag_multi": None,
}
_D2_SYMPTOM: dict[str, object] = {
    "pain_present": "否",
    "aaa_tender": "明确阳性",
    "pain_attrib": "明确相关",
}
_D2_GO: dict[str, object] = {
    "D2.CLINICAL_STABILITY": "稳定",
    "D2.URGENCY_DECISION": "紧急",
    "D2.VITAL_TREND": "稳定",
    "D2.RISK": "可接受",
    "D2.PREF": "EVAR倾向",
    "D2.COMPRESSION": ["无"],
    "D2.EMBOLIC_SOURCE": "其他来源明确",
}


def _d2_state(
    cl: dict[str, object] | None = None,
    s: dict[str, object] | None = None,
    n: dict[str, object] | None = None,
    c: dict[str, object] | None = None,
    d2: dict[str, object] | None = None,
    d3: dict[str, object] | None = None,
) -> GuidelineState:
    overrides: dict[str, object] = {
        "cl": {**_D2_CTX, **(cl or {})},
        "s": {**_D2_SYMPTOM, **(s or {})},
    }
    if n:
        overrides["n"] = n
    if c:
        overrides["c"] = c
    case = make_case(**overrides)
    inputs: dict[str, dict[str, object]] = {"D2": {**_D2_GO, **(d2 or {})}}
    if d3:
        # ``D3.TEAM_RESOURCE`` 按前缀分在 D3 通道：node_field 靠前缀定位，
        # 放进 D2 通道是取不到的。
        inputs["D3"] = dict(d3)
    return GuidelineState(case=case, route=route(case), node_inputs=inputs)


class TestD21SymptomAndRupture:
    """D2.1 先把「是不是 AAA 的症状、瘤子破没破」这两件事分开。

    本组测试有一半在守**词表**：规格的 ``S.PAIN_ATTRIB`` / ``S.AAA_TENDER`` 与模型
    的字面量不同，照规格写会得到恒为 ``False`` 的死分支——那种错误不会报错，
    只会安静地把每个症状性 AAA 判成「与 AAA 无关」。
    """

    def test_tender_with_attribution_reaches_d22(self) -> None:
        outcome = d2_1(_d2_state())
        assert outcome.verdicts["D2.1.R01"] is True
        assert outcome.next_node == "D2.2"

    def test_spec_wording_is_rejected_by_the_model(self) -> None:
        """规格写「医师确认相关」，模型只收「明确相关」——类型系统挡住了词表混用。

        这条守的是「**不要**为对齐规格去改模型 ``Literal``」：改了模型，判定层所有
        按模型字面量写的规则都会静默失效（``eq("明确相关", "医师确认相关")`` 恒假），
        而且单测全绿也发现不了。
        """
        with pytest.raises(ValidationError):
            make_case(s={"pain_attrib": "医师确认相关"})

    def test_both_related_values_are_accepted(self) -> None:
        for value in ("明确相关", "怀疑相关"):
            assert d2_1(_d2_state(s={"pain_attrib": value})).verdicts["D2.1.R01"] is True

    def test_rupture_evidence_escalates_and_enters_d31(self) -> None:
        outcome = d2_1(_d2_state(cl={"aaa_rupture": "疑似"}))
        assert outcome.verdicts["D2.1.R03"] is True
        assert outcome.escalation == "G03"
        assert outcome.next_node == "D3.1"

    def test_suspicious_wall_breach_is_unknown_not_confirmed(self) -> None:
        """影像「可疑」不得直接判成破裂，但也不能当作已排除。"""
        outcome = d2_1(_d2_state(c={"extrav": "可疑"}))
        assert outcome.verdicts["D2.1.R03"] is None
        assert outcome.verdicts["D2.1.R01"] is None

    def test_pain_without_attribution_keeps_emergency_assessment(self) -> None:
        """腹痛 + 医生未判归因 → P0-A，回 G03 并继续 D2.2。"""
        outcome = d2_1(_d2_state(s={"pain_present": "是", "pain_attrib": "未判断"}))
        assert outcome.verdicts["D2.1.R05"] is True
        assert outcome.escalation == "G03"
        assert outcome.next_node == "D2.2"

    def test_pain_attributed_elsewhere_is_not_a_d2_symptom(self) -> None:
        outcome = d2_1(_d2_state(s={"pain_present": "是", "pain_attrib": "明确其他原因"}))
        assert outcome.verdicts["D2.1.R05"] is not True

    def test_small_aneurysm_with_suspected_symptom_still_goes_to_d22(self) -> None:
        """直径 <55 mm 不排除症状性 AAA —— 这一条守的是「不因尺寸小就放过」。"""
        outcome = d2_1(
            _d2_state(cl={"aaa_diam": 48.0}, s={"pain_present": "可疑", "pain_attrib": "怀疑相关"})
        )
        assert outcome.verdicts["D2.1.R02"] is True
        assert outcome.next_node == "D2.2"

    def test_large_aneurysm_does_not_trigger_the_small_one_rule(self) -> None:
        outcome = d2_1(_d2_state(s={"pain_present": "可疑"}))
        assert outcome.verdicts["D2.1.R02"] is False

    def test_compression_symptom_goes_to_d22(self) -> None:
        outcome = d2_1(_d2_state(d2={"D2.COMPRESSION": ["肾积水"]}))
        assert outcome.verdicts["D2.1.R06"] is True
        assert outcome.next_node == "D2.2"

    def test_compression_absent_is_not_a_positive(self) -> None:
        outcome = d2_1(_d2_state(d2={"D2.COMPRESSION": ["无"]}))
        assert outcome.verdicts["D2.1.R06"] is False

    def test_symptom_gone_returns_to_the_asymptomatic_path(self) -> None:
        outcome = d2_1(_d2_state(s={"aaa_tender": "阴性", "pain_attrib": "明确其他原因"}))
        assert outcome.verdicts["D2.1.R91"] is True
        assert outcome.next_node == "D1.1"

    def test_unjudged_attribution_with_symptom_blocks_the_asymptomatic_path(self) -> None:
        outcome = d2_1(_d2_state(s={"pain_attrib": "未判断"}))
        assert outcome.verdicts["D2.1.R04"] is True
        assert outcome.terminal is not None and "人工复核" in outcome.terminal
        assert "不得进入无症状路径" in outcome.terminal

    def test_missing_pain_status_blocks(self) -> None:
        outcome = d2_1(_d2_state(s={"pain_present": None}))
        assert outcome.verdicts["D2.1.R90"] is True

    def test_gaps_name_the_unjudged_attribution(self) -> None:
        outcome = d2_1(_d2_state(s={"pain_attrib": "未判断"}))
        assert any("S.PAIN_ATTRIB" in gap and "尚未判断" in gap for gap in outcome.gaps)


class TestD22Urgency:
    """D2.2 只回答一个问题：能不能等。

    规格在这里反复写「不因等待优化而不安全延误」，所以本组测试守的是**不拖延**：
    恶化/不稳定必须先出急症出口，判不了则停人工复核，绝不默认稳定。
    """

    def test_unstable_escalates_to_g05(self) -> None:
        outcome = d2_2(_d2_state(d2={"D2.CLINICAL_STABILITY": "不稳定"}))
        assert outcome.verdicts["D2.2.R04"] is True
        assert outcome.escalation == "G05"

    def test_deteriorating_trend_escalates_even_when_called_stable(self) -> None:
        """趋势恶化优先于「稳定」这个结论——单次结论不能覆盖持续恶化。"""
        outcome = d2_2(_d2_state(d2={"D2.CLINICAL_STABILITY": "稳定", "D2.VITAL_TREND": "恶化"}))
        assert outcome.verdicts["D2.2.R04"] is True
        assert outcome.escalation == "G05"

    def test_unexcluded_rupture_goes_back_to_g03(self) -> None:
        outcome = d2_2(_d2_state(cl={"aaa_rupture": "无法评价"}))
        assert outcome.verdicts["D2.2.R01"] is True
        assert outcome.escalation == "G03"
        assert outcome.next_node == "D3.1"

    def test_missing_cta_cannot_be_called_stable(self) -> None:
        outcome = d2_2(_d2_state(n={"cta_status": "缺失"}))
        assert outcome.verdicts["D2.2.R06"] is True
        assert outcome.terminal is not None and "人工复核" in outcome.terminal

    def test_unassessed_stability_is_unknown_not_stable(self) -> None:
        """「未评价」不得折成「稳定」——``R02`` 必须记 UNKNOWN。

        放行与否由 P1 的另一条规则（``R05``）决定：规格给 ``R05`` 的字段清单里
        根本没有稳定性，只有趋势与已定的时效级别。所以这条测试守的是「别把未评价
        读成稳定」，不是「未评价必须阻断」。
        """
        outcome = d2_2(_d2_state(d2={"D2.CLINICAL_STABILITY": "未评价"}))
        assert outcome.verdicts["D2.2.R02"] is None
        assert outcome.verdicts["D2.2.R05"] is True

    def test_stable_unruptured_case_goes_to_d23(self) -> None:
        outcome = d2_2(_d2_state())
        assert outcome.verdicts["D2.2.R02"] is True
        assert outcome.next_node == "D2.3"

    def test_urgency_decision_alone_is_enough_when_stability_unknown(self) -> None:
        """稳定性没结论但已定「紧急」→ 仍可进 D2.3（R05）。

        ``R02`` 要求临床稳定，``R05`` 只要求时效级别已定——两者是「稳定」与
        「已经决定按紧急处理」的分野。
        """
        outcome = d2_2(_d2_state(d2={"D2.CLINICAL_STABILITY": None}))
        assert outcome.verdicts["D2.2.R02"] is None
        assert outcome.verdicts["D2.2.R05"] is True
        assert outcome.next_node == "D2.3"

    def test_missing_trend_blocks_r05(self) -> None:
        """稳定性与趋势都没有 → R05 判不了，落 R90 人工复核。"""
        outcome = d2_2(_d2_state(d2={"D2.CLINICAL_STABILITY": None, "D2.VITAL_TREND": None}))
        assert outcome.verdicts["D2.2.R05"] is None
        assert outcome.verdicts["D2.2.R90"] is True

    def test_unproven_symptom_source_goes_back_to_g03(self) -> None:
        outcome = d2_2(_d2_state(s={"pain_attrib": "明确其他原因"}))
        assert outcome.verdicts["D2.2.R03"] is True
        assert outcome.escalation == "G03"

    def test_gaps_name_the_unexcluded_rupture(self) -> None:
        outcome = d2_2(_d2_state(cl={"aaa_rupture": "无法评价"}))
        assert any("破裂未充分排除" in gap for gap in outcome.gaps)


class TestD23Candidates:
    """D2.3 与 D1.3 同构，但要守「紧急场景降低完整度要求」这条线。"""

    def test_both_columns_are_always_present(self) -> None:
        assert [c.procedure for c in d2_3(_d2_state()).candidates] == ["EVAR", "OSR"]

    def test_anatomy_and_ifu_met_makes_evar_feasible(self) -> None:
        outcome = d2_3(_d2_state())
        evar = _candidate(outcome, "EVAR")
        assert evar.status is CandidateStatus.FEASIBLE
        assert "D2.3.R01" in evar.rule_ids

    def test_distal_seal_is_not_required_here(self) -> None:
        """紧急场景不要求远端密封区 —— 这正是 D2.3 与 D1.3 的实质差别。

        ``D1.3`` 在同样输入下只能给「条件性候选（远端密封区未评估）」；
        ``D2.3`` 直接给可行。用择期的完整度卡急诊，等于把病人卡在评估里。
        """
        assert _candidate(d2_3(_d2_state()), "EVAR").status is CandidateStatus.FEASIBLE
        d13_case = make_case(cl={"aaa_diam": 56.0})
        d13_state = GuidelineState(
            case=d13_case,
            route=route(d13_case),
            node_inputs={"D1": {**_D1_METHOD["D1"], **_D12_GO}},
        )
        assert _candidate(d1_3(d13_state), "EVAR").status is CandidateStatus.CONDITIONAL

    def test_unverified_ifu_stays_a_pending_candidate(self) -> None:
        outcome = d2_3(_d2_state(n={"evar_ifu_status": "未核验"}))
        evar = _candidate(outcome, "EVAR")
        assert evar.status is CandidateStatus.CONDITIONAL
        assert "待核验候选" in evar.reason
        assert outcome.verdicts["D2.3.R06"] is True

    def test_ifu_mismatch_excludes_evar_and_escalates(self) -> None:
        outcome = d2_3(_d2_state(n={"evar_ifu_status": "不符合"}))
        assert _candidate(outcome, "EVAR").status is CandidateStatus.EXCLUDED
        assert outcome.escalation == "G06"

    def test_ifu_comes_from_the_whole_device_field_not_the_proximal_one(self) -> None:
        """``D2.3`` 的 7 条规则读 ``N.EVAR_IFU_STATUS``，不是 ``N.SEAL_IFU``。

        两个方向一起证明判据没串：近端符合 + 整体不符合 → 排除（合并字段时会
        误判为可行）；近端未核验 + 整体符合 → 仍是可行候选（说明不看近端）。
        """
        contradictory = d2_3(_d2_state(n={"seal_ifu": "符合", "evar_ifu_status": "不符合"}))
        assert _candidate(contradictory, "EVAR").status is CandidateStatus.EXCLUDED

        whole_ok = d2_3(_d2_state(n={"seal_ifu": "未核验", "evar_ifu_status": "符合"}))
        assert _candidate(whole_ok, "EVAR").status is CandidateStatus.FEASIBLE

    def test_branch_involvement_escalates_to_complex(self) -> None:
        outcome = d2_3(_d2_state(n={"renal_l": "明确受累"}))
        assert outcome.verdicts["D2.3.R03"] is True
        assert outcome.escalation == "G06"
        assert _candidate(outcome, "EVAR").status is CandidateStatus.EXCLUDED

    def test_bilateral_access_occlusion_excludes_evar(self) -> None:
        outcome = d2_3(_d2_state(n={"access_occlusion": "双侧闭塞"}))
        assert _candidate(outcome, "EVAR").status is CandidateStatus.EXCLUDED

    def test_missing_anatomy_yields_a_draft(self) -> None:
        outcome = d2_3(_d2_state(n={"neck_len": None, "neck_d": None, "neck_angle": None}))
        assert _candidate(outcome, "EVAR").status is CandidateStatus.UNASSESSED
        assert outcome.verdicts["D2.3.R90"] is True

    def test_missing_team_resource_does_not_block_the_direction(self) -> None:
        """团队资源未确认不阻断 —— 规格明写「未确认不等于不可用」。"""
        outcome = d2_3(_d2_state(d3={"D3.TEAM_RESOURCE": "未知"}))
        assert outcome.verdicts["D2.3.R90"] is False
        assert _candidate(outcome, "EVAR").status is CandidateStatus.FEASIBLE

    def test_unavailable_team_escalates_to_complex(self) -> None:
        outcome = d2_3(_d2_state(d3={"D3.TEAM_RESOURCE": "均不可立即用"}))
        assert outcome.verdicts["D2.3.R05"] is True
        assert outcome.escalation == "G06"

    def test_osr_unassessed_without_risk(self) -> None:
        outcome = d2_3(_d2_state(d2={"D2.RISK": "未评价"}))
        osr = _candidate(outcome, "OSR")
        assert osr.status is CandidateStatus.UNASSESSED
        assert "未评价" in osr.reason

    def test_osr_reason_marks_the_only_option_when_evar_out(self) -> None:
        outcome = d2_3(_d2_state(n={"evar_ifu_status": "不符合"}))
        assert "唯一候选" in _candidate(outcome, "OSR").reason

    def test_display_rules_leave_traces(self) -> None:
        outcome = d2_3(_d2_state())
        for rule_id in ("D2.3.R01", "D2.3.R02", "D2.3.R04", "D2.3.R06"):
            assert rule_id in outcome.verdicts, rule_id

    def test_end_to_end_from_d21_reaches_candidates(self) -> None:
        case = make_case(cl=_D2_CTX, s=_D2_SYMPTOM)
        report = run(
            ToolRequest(
                case=case,
                route_result=route(case),
                node_inputs={"D2": _D2_GO},
            )
        )
        assert report.path == ("D2.1", "D2.2", "D2.3")
        assert [c.procedure for c in report.candidates] == ["EVAR", "OSR"]
        assert report.escalation is None


class TestD2DegradedInputs:
    """D2 的残缺输入 —— 每一格「差什么」都要能在结论里读出来。"""

    def test_all_image_fields_missing_leaves_wall_breach_unknown(self) -> None:
        """四个影像字段全缺 → 壁外出血「判不了」，既不确认也不排除。"""
        outcome = d2_1(
            _d2_state(c={"extrav": None, "hematoma": None, "wall": None, "contain": None})
        )
        assert outcome.verdicts["D2.1.R03"] is None

    def test_unevaluable_image_grade_stays_unknown(self) -> None:
        outcome = d2_1(_d2_state(c={"extrav": "不可评价", "wall": "不可评价"}))
        assert outcome.verdicts["D2.1.R03"] is None

    def test_missing_rupture_status_leaves_exclusion_unknown(self) -> None:
        outcome = d2_1(_d2_state(cl={"aaa_rupture": None}))
        assert outcome.verdicts["D2.1.R90"] is True

    def test_limb_sign_counts_as_a_symptom(self) -> None:
        """``s.limb`` 是多选列表：有症状项即算有表现，不能用 ``eq`` 判。"""
        outcome = d2_1(
            _d2_state(s={"aaa_tender": "阴性", "pain_attrib": "明确相关", "limb": ["发凉"]})
        )
        assert outcome.verdicts["D2.1.R01"] is True

    def test_limb_unknown_is_not_a_negative(self) -> None:
        outcome = d2_1(
            _d2_state(s={"aaa_tender": "阴性", "pain_attrib": "明确相关", "limb": ["未知"]})
        )
        assert outcome.verdicts["D2.1.R01"] is None

    def test_partial_anatomy_names_each_missing_item(self) -> None:
        outcome = d2_3(
            _d2_state(
                n={
                    "neck_d": None,
                    "neck_angle": None,
                    "access_occlusion": None,
                    "evar_ifu_status": None,
                }
            )
        )
        evar = _candidate(outcome, "EVAR")
        assert evar.status is CandidateStatus.CONDITIONAL
        assert "瘤颈参数不全" in evar.reason
        assert "入路可行性未评估" in evar.reason
        assert "IFU 未记录" in evar.reason
        joined = " ".join(outcome.gaps)
        assert "入路可行性未评估" in joined
        assert "N.EVAR_IFU_STATUS" in joined

    def test_missing_cta_status_is_reported(self) -> None:
        outcome = d2_3(_d2_state(n={"cta_status": None}))
        assert any("N.CTA_STATUS" in gap for gap in outcome.gaps)

    def test_high_risk_osr_explains_itself(self) -> None:
        outcome = d2_3(_d2_state(d2={"D2.RISK": "高风险"}))
        assert "高危" in _candidate(outcome, "OSR").reason

    def test_patient_preference_for_osr_shows_in_the_reason(self) -> None:
        outcome = d2_3(_d2_state(d2={"D2.PREF": "OSR倾向"}))
        assert "患者倾向 OSR" in _candidate(outcome, "OSR").reason


class TestMultiFieldNormalisation:
    """``multi_field`` 把标量/列表统一成列表 —— ``has()`` 对字符串会逐字符迭代。"""

    def test_scalar_string_is_wrapped(self) -> None:
        outcome = d2_1(_d2_state(d2={"D2.COMPRESSION": "肾积水"}))
        assert outcome.verdicts["D2.1.R06"] is True

    def test_confirmed_wall_breach_outranks_the_rupture_label(self) -> None:
        """影像明确外溢，即使破裂状态写「未见」也必须进急诊路径。"""
        outcome = d2_1(_d2_state(cl={"aaa_rupture": "未见相关证据"}, c={"extrav": "明确"}))
        assert outcome.verdicts["D2.1.R03"] is True

    def test_rupture_exclusion_is_unknown_without_a_status(self) -> None:
        from src.tools.guideline_engine.nodes.d2_symptomatic import _rupture_excluded

        assert _rupture_excluded(_d2_state(cl={"aaa_rupture": None})) is None


# ===========================================================================
# D4 感染性主动脉病变
# ===========================================================================
#: 原生感染病例的「方案要素齐全」基线 —— 各测试在此基础上单点覆盖。
_D4_PLAN: dict[str, object] = {
    "D4.BLOOD_CULTURE": "阳性：沙门菌",
    "D4.INFECTION_IMAGING": ["主动脉周围气体"],
    "D4.INFECTION_SOURCE": ["不明"],
    "D4.INFECTION_EXTENT": ["局限"],
    "D4.NATIVE_FITNESS": "高风险",
    "D4.CENTRE_CAPABILITY": "具备",
    "D4.SOURCE_CONTROL": "切除清创",
    "D4.RECONSTRUCTION_OPTIONS": ["自体静脉"],
    "D4.ANTIMICROBIAL_PLAN": "培养后已启动",
    "D4.POSTOP_SURVEILLANCE": "6 周静脉抗菌 + 3/6/12 月 CTA",
}

#: 原生感染的三类证据（``D4.1.R04`` 的组合判据）。
_D4_NATIVE_EVIDENCE: dict[str, object] = {
    "D4.NATIVE_CLINICAL": ["发热≥38°C", "菌血症"],
    "D4.NATIVE_LAB": ["CRP 150（参考<5）"],
    "D4.NATIVE_CTA": ["囊状", "周围软组织包块"],
}


def _d4_state(
    cl: dict[str, object] | None = None,
    c: dict[str, object] | None = None,
    n: dict[str, object] | None = None,
    s: dict[str, object] | None = None,
    v: dict[str, object] | None = None,
    d4: dict[str, object] | None = None,
    d3: dict[str, object] | None = None,
) -> GuidelineState:
    overrides: dict[str, object] = {
        "cl": {"aaa_diam": 56.0, "diag_multi": None, **(cl or {})},
    }
    for key, value in (("c", c), ("n", n), ("s", s), ("v", v)):
        if value:
            overrides[key] = value
    case = make_case(**overrides)
    inputs: dict[str, dict[str, object]] = {
        "D4": {**_D4_PLAN, **(d4 or {})},
    }
    if d3:
        inputs["D3"] = dict(d3)
    return GuidelineState(case=case, route=route(case), node_inputs=inputs)


class TestD41Triage:
    """D4.1 是分诊台：原生 → D4.2、移植物 → D4.3、瘘 → D4.3、都没有 → 回 G08。

    这一组测试守的主线是**不许猜**：分型、修复史、瘘状态任一判不了，都必须停在
    人工复核，而不是往某一条支路上推。三条支路的治疗逻辑完全不同（一侧按感染源
    控制 + 重建，一侧先按 MAGIC 定性），猜错等于换了整条路径。
    """

    def test_native_infection_goes_to_the_native_branch(self) -> None:
        outcome = d4_1(_d4_state(cl={"infection": "原生感染明确"}))
        assert outcome.verdicts["D4.1.R01"] is True
        assert outcome.next_node == "D4.2"

    def test_graft_infection_goes_to_the_graft_branch(self) -> None:
        outcome = d4_1(
            _d4_state(
                cl={"infection": "移植物感染疑似", "prev_evor": "EVAR"},
                d4={"D4.MAGIC_STATUS": "疑似"},
            )
        )
        assert outcome.verdicts["D4.1.R02"] is True
        assert outcome.next_node == "D4.3"

    def test_graft_history_can_come_from_imaging_alone(self) -> None:
        """``N.PREV_REPAIR`` 没记录、但 CTA 见到移植物 → 仍按移植物分支走。

        两条证据任一成立即可：漏掉一条既往修复，整条路径就换错了。
        """
        outcome = d4_1(
            _d4_state(
                cl={"infection": "移植物感染明确"}, n={"prev_repair": None}, c={"graft": "有"}
            )
        )
        assert outcome.verdicts["D4.1.R02"] is True
        assert outcome.next_node == "D4.3"

    def test_suspected_fistula_is_a_positive_trigger(self) -> None:
        """``CL.FISTULA = 疑似`` 必须命中 R03 —— 「疑似」不是「没有」。

        出口按规格 M 列「D4.3；**急症→**G03/G05」：无出血/休克/新破裂证据时
        只走 D4.3，不通知 G03。修前无条件 ``escalate G03``，会让每个瘘病人都被
        标成急症。
        """
        outcome = d4_1(_d4_state(cl={"fistula": "疑似"}))
        assert outcome.verdicts["D4.1.R03"] is True
        assert outcome.next_node == "D4.3"
        assert outcome.escalation is None
        assert outcome.next_nodes == ("D4.3", "G03", "G05")
        assert outcome.link is Link.PARALLEL

    def test_fistula_with_active_bleeding_also_notifies_g03(self) -> None:
        """规格「如伴活动性出血、休克或新破裂证据，**先**回 G03/G05」。"""
        outcome = d4_1(_d4_state(cl={"fistula": "主动脉肠瘘"}, s={"gi_bleed": ["黑便"]}))
        assert outcome.verdicts["D4.1.R03"] is True
        assert outcome.next_node == "D4.3"
        assert outcome.escalation == "G03"

    def test_aef_imaging_sign_alone_triggers_the_fistula_branch(self) -> None:
        outcome = d4_1(_d4_state(cl={"fistula": "否"}, c={"aef_sign": "明确"}))
        assert outcome.verdicts["D4.1.R03"] is True

    def test_suspicious_aef_sign_is_unknown_not_positive(self) -> None:
        outcome = d4_1(_d4_state(cl={"fistula": "否"}, c={"aef_sign": "可疑"}))
        assert outcome.verdicts["D4.1.R03"] is None

    def test_no_infection_and_no_fistula_returns_to_g08(self) -> None:
        outcome = d4_1(_d4_state(cl={"infection": "无证据", "fistula": "否"}))
        assert outcome.verdicts["D4.1.R91"] is True
        assert outcome.escalation == "G08"
        assert outcome.next_node is None

    def test_unassessed_infection_blocks_instead_of_concluding_absence(self) -> None:
        outcome = d4_1(_d4_state(cl={"infection": "未评价", "fistula": "否"}))
        assert outcome.verdicts["D4.1.R90"] is True
        assert outcome.terminal is not None and "人工复核" in outcome.terminal
        assert any("CL.INFECTION" in gap for gap in outcome.gaps)

    def test_unknown_graft_history_with_infection_blocks_the_branch_choice(self) -> None:
        """有感染线索、却不知道有没有移植物 → 两条支路选不了，交人工复核。"""
        outcome = d4_1(
            _d4_state(
                cl={"infection": "原生感染疑似"},
                n={"prev_repair": None},
                c={"graft": "可疑"},
            )
        )
        assert outcome.verdicts["D4.1.R90"] is True
        assert any("N.PREV_REPAIR" in gap for gap in outcome.gaps)

    def test_graft_case_without_magic_assessment_blocks(self) -> None:
        outcome = d4_1(
            _d4_state(cl={"infection": "无证据", "prev_evor": "EVAR"}, c={"graft": "有"})
        )
        assert outcome.verdicts["D4.1.R90"] is True
        assert any("MAGIC" in gap for gap in outcome.gaps)

    def test_composite_evidence_without_classification_still_reaches_d42(self) -> None:
        """分型字段空着、但临床 + 实验室 + 影像已凑齐两类 → 仍走原生分支。

        这条守的是「不能因为分类字段没填就掉到 G08」——那会把感染病例判成无感染。
        """
        outcome = d4_1(
            _d4_state(cl={"infection": "未评价", "fistula": "否"}, d4=_D4_NATIVE_EVIDENCE)
        )
        assert outcome.verdicts["D4.1.R04"] is True
        assert outcome.next_node == "D4.2"

    def test_single_evidence_category_is_not_enough(self) -> None:
        outcome = d4_1(
            _d4_state(
                cl={"infection": "未评价", "fistula": "否"},
                d4={
                    "D4.NATIVE_CLINICAL": ["发热≥38°C"],
                    "D4.NATIVE_LAB": ["无"],
                    "D4.NATIVE_CTA": ["未见"],
                },
            )
        )
        assert outcome.verdicts["D4.1.R04"] is False

    def test_inflammation_with_infection_is_kept_as_dual_label(self) -> None:
        """炎症影像 + 感染线索 → 双标签并行评估，不得以炎症影像排除感染。"""
        outcome = d4_1(
            _d4_state(
                cl={"infection": "未评价", "fistula": "否", "saccular": "明确"},
                n={"inflammatory_ctx": "影像高度提示", "infection_ctx": "高度怀疑"},
            )
        )
        assert outcome.verdicts["D4.1.R05"] is True
        assert outcome.next_nodes == ("D4.2", "D5.3")
        assert outcome.link is Link.PARALLEL
        assert outcome.next_node == "D4.2"

    def test_inflammation_alone_does_not_trigger_the_dual_label(self) -> None:
        outcome = d4_1(
            _d4_state(
                cl={"infection": "无证据", "fistula": "否"},
                n={"inflammatory_ctx": "影像高度提示"},
                d4={
                    "D4.NATIVE_CLINICAL": ["无"],
                    "D4.NATIVE_LAB": ["无"],
                    "D4.NATIVE_CTA": ["未见"],
                },
            )
        )
        assert outcome.verdicts["D4.1.R05"] is False

    def test_typed_native_infection_takes_the_exit_before_the_overlap_rule(self) -> None:
        """分型已明确时走 R01 的出口；R05 不再求值（先命中即返回）。

        ``R05`` 的信息不丢：``_d41_gaps`` 会另记一条「双标签并行」的提示。
        """
        outcome = d4_1(
            _d4_state(
                cl={"infection": "原生感染明确", "fistula": "否"},
                n={"inflammatory_ctx": "临床确认"},
            )
        )
        assert outcome.verdicts["D4.1.R01"] is True
        assert "D4.1.R05" not in outcome.verdicts


class TestD42NativePlan:
    """D4.2 不选术式，只回答「确定性方案还差什么」。

    五条 P1 是并列义务，任何一条被遮蔽都意味着报告少了一个必须做的动作。
    候选档位封顶在「需 MDT 评估」——规格 4 处明令不指定材料与疗程。
    """

    def test_complete_plan_reaches_the_mdt_exit(self) -> None:
        outcome = d4_2(_d4_state(cl={"infection": "原生感染明确"}))
        assert outcome.verdicts["D4.2.R91"] is True
        assert outcome.terminal is not None and "MDT" in outcome.terminal
        assert outcome.verdicts["D4.2.R09"] is False

    def test_undecided_source_control_requires_the_comparison(self) -> None:
        outcome = d4_2(
            _d4_state(cl={"infection": "原生感染明确"}, d4={"D4.SOURCE_CONTROL": "未决定"})
        )
        assert outcome.verdicts["D4.2.R03"] is True
        assert outcome.verdicts["D4.2.R09"] is True
        assert any("感染源控制计划" in gap for gap in outcome.gaps)

    def test_missing_antimicrobial_blocks_the_complete_plan_marker(self) -> None:
        outcome = d4_2(
            _d4_state(cl={"infection": "原生感染明确"}, d4={"D4.ANTIMICROBIAL_PLAN": "未开始"})
        )
        assert outcome.verdicts["D4.2.R09"] is True
        assert any("抗菌" in gap for gap in outcome.gaps)

    def test_missing_extent_and_fitness_blocks_the_direction(self) -> None:
        outcome = d4_2(
            _d4_state(
                cl={"infection": "原生感染明确"},
                d4={"D4.INFECTION_EXTENT": None, "D4.NATIVE_FITNESS": None},
            )
        )
        assert outcome.verdicts["D4.2.R90"] is True
        assert outcome.terminal is not None and "人工复核" in outcome.terminal
        assert _candidate(outcome, "OSR").status is CandidateStatus.UNASSESSED

    def test_pending_culture_does_not_block(self) -> None:
        """未采集/待结果不阻断 —— 规格明写不得为采样延误急救。"""
        outcome = d4_2(
            _d4_state(cl={"infection": "原生感染明确"}, d4={"D4.BLOOD_CULTURE": "已采集待结果"})
        )
        assert outcome.verdicts["D4.2.R90"] is False
        assert outcome.verdicts["D4.2.R91"] is True
        assert any("未采集/待结果不等于阴性" in gap for gap in outcome.gaps)

    def test_insufficient_centre_capability_prompts_referral(self) -> None:
        outcome = d4_2(
            _d4_state(cl={"infection": "原生感染明确"}, d4={"D4.CENTRE_CAPABILITY": "不具备"})
        )
        assert outcome.verdicts["D4.2.R04"] is True
        assert "转诊" in _candidate(outcome, "OSR").reason
        assert "转诊" in _candidate(outcome, "EVAR").reason

    def test_extreme_risk_goes_to_individualised_mdt(self) -> None:
        outcome = d4_2(
            _d4_state(cl={"infection": "原生感染明确"}, d4={"D4.NATIVE_FITNESS": "极高风险"})
        )
        assert "极高风险" in _candidate(outcome, "OSR").reason

    def test_evar_bridge_is_never_a_feasible_candidate(self) -> None:
        """腔内桥接永远不是「可行候选」——它从不代表感染源已控制。"""
        for overrides in (
            {},
            {"D4.INFECTION_EXTENT": None},
            {"D4.CENTRE_CAPABILITY": "不具备"},
            {"D4.NATIVE_FITNESS": "极高风险"},
        ):
            outcome = d4_2(_d4_state(cl={"infection": "原生感染明确"}, d4=overrides))
            assert _candidate(outcome, "EVAR").status is not CandidateStatus.FEASIBLE

    def test_fistula_adds_a_repair_item_to_the_plan(self) -> None:
        outcome = d4_2(_d4_state(cl={"infection": "原生感染明确", "fistula": "主动脉肠瘘"}))
        assert any("瘘的处理方式" in gap for gap in outcome.gaps)

    def test_both_columns_are_always_present(self) -> None:
        outcome = d4_2(_d4_state(cl={"infection": "原生感染明确"}))
        assert [c.procedure for c in outcome.candidates] == ["OSR", "EVAR"]


class TestD43Magic:
    """D4.3 的核心是 MAGIC 定性 —— 算错一档就换了治疗目标。

    MAGIC 的判据是「主要/次要标准 + 类别组合」，三处最容易写错的地方都在本组测试里：
    ① 同类别两条次要标准不算疑似（必须来自不同类别）；
    ② 三类标准没收全时不得标「不支持」；
    ③ 逐项标准与团队结论冲突时取更严的一档，不许静默降级。
    """

    @staticmethod
    def _graft_case(d4: dict[str, object] | None = None) -> GuidelineState:
        return _d4_state(
            cl={"infection": "移植物感染疑似", "prev_evor": "EVAR"},
            c={"graft": "有"},
            d4={"D4.GRAFT_EXTENT": "吻合口", "D4.GRAFT_FITNESS": "可接受", **(d4 or {})},
        )

    def test_one_major_plus_another_category_confirms(self) -> None:
        outcome = d4_3(
            self._graft_case(
                {
                    "D4.MAGIC_CLINICAL": ["移植物周围脓液"],
                    "D4.MAGIC_IMAGING": ["植入后≥7周周围气体"],
                    "D4.MAGIC_LAB": [],
                }
            )
        )
        assert outcome.verdicts["D4.3.R06"] is True
        assert outcome.verdicts["D4.3.R05"] is False

    def test_two_minors_from_different_categories_are_suspected(self) -> None:
        outcome = d4_3(
            self._graft_case(
                {
                    "D4.MAGIC_CLINICAL": ["无其他原因发热≥38°C"],
                    "D4.MAGIC_IMAGING": ["动脉瘤扩大"],
                    "D4.MAGIC_LAB": ["无"],
                }
            )
        )
        assert outcome.verdicts["D4.3.R05"] is True
        assert outcome.verdicts["D4.3.R06"] is False

    def test_two_minors_in_one_category_are_not_enough(self) -> None:
        """``D4.3.R05`` 要求「**不同类别** ≥2 项次要标准」。"""
        outcome = d4_3(
            self._graft_case(
                {
                    "D4.MAGIC_CLINICAL": ["无其他原因发热≥38°C", "局部红肿热痛或脓性分泌物"],
                    "D4.MAGIC_IMAGING": ["未见"],
                    "D4.MAGIC_LAB": ["无"],
                }
            )
        )
        assert outcome.verdicts["D4.3.R05"] is False

    def test_uncollected_categories_never_yield_not_supported(self) -> None:
        """三类清单没收全 → 「未完成评估」，R90 阻断，R91 不放行。"""
        outcome = d4_3(
            self._graft_case(
                {
                    "D4.MAGIC_STATUS": None,
                    "D4.MAGIC_CLINICAL": ["无其他原因发热≥38°C"],
                    "D4.MAGIC_IMAGING": [],
                    "D4.MAGIC_LAB": [],
                }
            )
        )
        assert outcome.verdicts["D4.3.R90"] is True
        assert outcome.verdicts.get("D4.3.R91") is not True
        assert any("未完成评估" in gap for gap in outcome.gaps)

    def test_all_categories_negative_yields_not_supported_and_goes_to_d6(self) -> None:
        outcome = d4_3(
            self._graft_case(
                {
                    "D4.MAGIC_STATUS": "不支持",
                    "D4.MAGIC_CLINICAL": ["无"],
                    "D4.MAGIC_IMAGING": ["未见"],
                    "D4.MAGIC_LAB": ["无"],
                }
            )
        )
        assert outcome.verdicts["D4.3.R91"] is True
        assert outcome.next_node == "D6.1"

    def test_stated_status_is_taken_when_criteria_are_incomplete(self) -> None:
        """团队结论也是权威来源：清单没收全但写了「确诊」→ 按确诊处理。"""
        outcome = d4_3(
            self._graft_case(
                {
                    "D4.MAGIC_STATUS": "确诊",
                    "D4.MAGIC_CLINICAL": [],
                    "D4.MAGIC_IMAGING": [],
                    "D4.MAGIC_LAB": [],
                }
            )
        )
        assert outcome.verdicts["D4.3.R06"] is True
        assert outcome.verdicts.get("D4.3.R90") is not True

    def test_criteria_outrank_a_lower_stated_status(self) -> None:
        """逐项标准算确诊、团队写疑似 → 取确诊并留一条冲突缺口（不许降级）。"""
        outcome = d4_3(
            self._graft_case(
                {
                    "D4.MAGIC_STATUS": "疑似",
                    "D4.MAGIC_CLINICAL": ["移植物周围脓液"],
                    "D4.MAGIC_IMAGING": ["植入后≥7周周围气体"],
                    "D4.MAGIC_LAB": [],
                }
            )
        )
        assert outcome.verdicts["D4.3.R06"] is True
        assert any("不一致" in gap or "更严" in gap for gap in outcome.gaps)

    def test_imaging_item_without_time_window_is_surfaced(self) -> None:
        """「移植物周围气体」缺「植入后 ≥7 周」这个时间窗 → 不计入，且要点名。"""
        outcome = d4_3(
            self._graft_case(
                {
                    "D4.MAGIC_STATUS": None,
                    "D4.MAGIC_CLINICAL": ["无"],
                    "D4.MAGIC_IMAGING": ["移植物周围气体"],
                    "D4.MAGIC_LAB": ["无"],
                }
            )
        )
        assert any("未归类" in gap and "移植物周围气体" in gap for gap in outcome.gaps)

    def test_active_bleeding_goes_to_the_emergency_path(self) -> None:
        state = _d4_state(
            cl={"infection": "移植物感染疑似", "prev_evor": "EVAR"},
            c={"graft": "有"},
            s={"gi_bleed": ["呕血"]},
        )
        outcome = d4_3(state)
        assert outcome.verdicts["D4.3.R01"] is True
        assert outcome.escalation == "G05"

    def test_herald_bleed_in_aef_warning_escalates(self) -> None:
        outcome = d4_3(
            _d4_state(
                cl={"fistula": "主动脉肠瘘", "prev_evor": "EVAR"},
                c={"graft": "有"},
                d4={"D4.AEF_WARNING": "先兆性少量出血"},
            )
        )
        assert outcome.verdicts["D4.3.R07"] is True
        assert outcome.escalation == "G05"

    def test_extreme_risk_switches_the_goal_to_palliation(self) -> None:
        outcome = d4_3(
            _d4_state(
                cl={"infection": "移植物感染疑似", "prev_evor": "EVAR"},
                c={"graft": "有"},
                d4={
                    "D4.MAGIC_STATUS": "确诊",
                    "D4.GRAFT_FITNESS": "极高风险",
                    "D4.GRAFT_EXTENT": "广泛",
                },
            )
        )
        assert outcome.verdicts["D4.3.R03"] is True
        assert outcome.terminal is not None and "姑息" in outcome.terminal

    def test_enteric_fistula_asks_for_the_abdominal_surgeon(self) -> None:
        outcome = d4_3(
            _d4_state(
                cl={"fistula": "主动脉肠瘘", "prev_evor": "EVAR"},
                c={"graft": "有"},
                d4={
                    "D4.MAGIC_STATUS": "疑似",
                    "D4.GRAFT_EXTENT": "瘘相关",
                    "D4.GRAFT_FITNESS": "高风险",
                },
            )
        )
        assert outcome.verdicts["D4.3.R04"] is True
        assert any("消化道外科" in gap for gap in outcome.gaps)

    def test_both_columns_are_always_present(self) -> None:
        outcome = d4_3(self._graft_case())
        assert [c.procedure for c in outcome.candidates] == [
            "移植物去除 + 重建（根治）",
            "保留移植物 + 长期抑制（姑息）",
        ]

    def test_retention_column_is_unassessed_without_magic(self) -> None:
        outcome = d4_3(
            _d4_state(
                cl={"infection": "移植物感染疑似", "prev_evor": "EVAR"},
                c={"graft": "有"},
                d4={"D4.MAGIC_STATUS": None},
            )
        )
        assert (
            _candidate(outcome, "保留移植物 + 长期抑制（姑息）").status
            is CandidateStatus.UNASSESSED
        )


class TestD4DegradedInputs:
    """输入残缺时 D4 的落档 —— 每一格「差什么」都要能在缺口里读出来。"""

    def test_empty_inputs_block_at_triage_with_named_gaps(self) -> None:
        case = make_case(cl={"aaa_diam": 56.0, "diag_multi": None, "infection": "未评价"})
        outcome = d4_1(GuidelineState(case=case, route=route(case)))
        assert outcome.verdicts["D4.1.R90"] is True
        joined = " ".join(outcome.gaps)
        assert "D4.BLOOD_CULTURE" in joined
        assert "CL.INFECTION" in joined

    def test_triage_lists_native_evidence_only_for_native_cases(self) -> None:
        native = d4_1(_d4_state(cl={"infection": "原生感染明确"}, d4={"D4.NATIVE_LAB": None}))
        graft = d4_1(
            _d4_state(cl={"infection": "移植物感染疑似", "prev_evor": "EVAR"}, c={"graft": "有"})
        )
        assert any("D4.NATIVE_LAB" in gap for gap in native.gaps)
        assert not any("D4.NATIVE_LAB" in gap for gap in graft.gaps)

    def test_graft_case_with_magic_still_gets_no_native_checklist(self) -> None:
        """MAGIC 已评的移植物病例同样不该被要求补原生感染的三类证据。"""
        graft = d4_1(
            _d4_state(
                cl={"infection": "移植物感染疑似", "prev_evor": "EVAR"},
                c={"graft": "有"},
                d4={"D4.MAGIC_STATUS": "疑似"},
            )
        )
        assert not any("D4.NATIVE_CLINICAL" in gap for gap in graft.gaps)
        assert not any("MAGIC 评估未完成" in gap for gap in graft.gaps)

    def test_suspected_fistula_type_is_named(self) -> None:
        outcome = d4_1(_d4_state(cl={"fistula": "疑似"}, c={"aef_sign": "未见"}))
        assert any("CL.FISTULA_TYPE" in gap for gap in outcome.gaps)

    def test_plan_node_reports_all_missing_elements(self) -> None:
        outcome = d4_2(_d4_state(cl={"infection": "原生感染明确"}, d4={"D4.SOURCE_CONTROL": None}))
        joined = " ".join(outcome.gaps)
        assert "D4.SOURCE_CONTROL" in joined
        assert "确定性方案要素不全" in joined


class TestD4EdgeBranches:
    """D4 的边角分支 —— 三值判据「判不了」的那一侧必须能走通。"""

    def test_no_repair_evidence_at_all_is_unknown(self) -> None:
        """三处修复史全空 → 不给结论（不是「没有修复史」）。"""
        outcome = d4_1(
            _d4_state(
                cl={"infection": "原生感染明确", "prev_evor": None},
                n={"prev_repair": None},
                c={"graft": None},
            )
        )
        assert outcome.verdicts["D4.1.R90"] is True
        assert any("N.PREV_REPAIR" in gap for gap in outcome.gaps)

    def test_uncertain_evidence_item_is_unknown(self) -> None:
        outcome = d4_1(
            _d4_state(
                cl={"infection": "未评价", "fistula": "否"},
                d4={
                    "D4.NATIVE_CLINICAL": ["未知"],
                    "D4.NATIVE_LAB": ["无"],
                    "D4.NATIVE_CTA": ["未见"],
                },
            )
        )
        assert outcome.verdicts["D4.1.R04"] is None

    def test_unrecorded_fistula_status_is_named(self) -> None:
        outcome = d4_1(_d4_state(cl={"infection": "原生感染明确", "fistula": None}))
        assert any("CL.FISTULA" in gap for gap in outcome.gaps)

    def test_missing_sbp_is_unknown_not_unstable(self) -> None:
        outcome = d4_3(
            _d4_state(
                cl={"infection": "移植物感染疑似", "prev_evor": "EVAR"},
                c={"graft": "有"},
                v={"sbp": None},
            )
        )
        assert outcome.verdicts["D4.3.R01"] is None

    def test_single_major_criterion_is_suspected(self) -> None:
        outcome = d4_3(
            _d4_state(
                cl={"infection": "移植物感染疑似", "prev_evor": "EVAR"},
                c={"graft": "有"},
                d4={
                    "D4.GRAFT_EXTENT": "吻合口",
                    "D4.GRAFT_FITNESS": "可接受",
                    "D4.MAGIC_CLINICAL": ["移植物周围脓液"],
                    "D4.MAGIC_IMAGING": ["未见"],
                    "D4.MAGIC_LAB": ["无"],
                },
            )
        )
        assert outcome.verdicts["D4.3.R05"] is True

    def test_plan_names_reconstruction_and_surveillance(self) -> None:
        outcome = d4_2(
            _d4_state(
                cl={"infection": "原生感染明确"},
                d4={"D4.RECONSTRUCTION_OPTIONS": None, "D4.POSTOP_SURVEILLANCE": None},
            )
        )
        joined = " ".join(outcome.gaps)
        assert "重建方式与材料" in joined
        assert "术后抗菌与影像随访计划" in joined

    def test_unknown_capability_is_reported_without_blocking(self) -> None:
        outcome = d4_2(
            _d4_state(cl={"infection": "原生感染明确"}, d4={"D4.CENTRE_CAPABILITY": None})
        )
        assert outcome.verdicts["D4.2.R90"] is False
        assert any("D4.CENTRE_CAPABILITY" in gap for gap in outcome.gaps)

    def test_removal_column_tracks_risk_and_capability(self) -> None:
        def removal(overrides: dict[str, object]) -> Candidate:
            state = _d4_state(
                cl={"infection": "移植物感染疑似", "prev_evor": "EVAR"},
                c={"graft": "有"},
                d4={"D4.MAGIC_STATUS": "确诊", "D4.GRAFT_EXTENT": "广泛", **overrides},
            )
            return _candidate(d4_3(state), "移植物去除 + 重建（根治）")

        assert "极高风险" in removal({"D4.GRAFT_FITNESS": "极高风险"}).reason
        assert (
            "转诊"
            in removal({"D4.GRAFT_FITNESS": "可接受", "D4.CENTRE_CAPABILITY": "不具备"}).reason
        )
        assert (
            removal({"D4.GRAFT_FITNESS": "可接受", "D4.GRAFT_EXTENT": None}).status
            is CandidateStatus.CONDITIONAL
        )

    def test_multi_label_report_ends_at_the_specialty_conclusion(self) -> None:
        """``D1 + D4`` 双标签：报告终点取**末端**（感染 MDT），不是 D1 的监测结论。

        ``G08`` 允许多标签且顺序固定 D1 → D7，D1 链总是先跑完。若报告取第一个
        终点，一个 56 mm 的感染性 AAA 会被总结成「按性别与直径安排随访」——
        感染需要控制感染源，而不是继续监测。
        """
        case = make_case(cl={"aaa_diam": 56.0, "infection": "原生感染明确", "diag_multi": None})
        report = run(ToolRequest(case=case, route_result=route(case), node_inputs={"D4": _D4_PLAN}))
        assert report.path == ("D1.1", "D4.1", "D4.2")
        assert report.terminal is not None and "MDT" in report.terminal
        assert "监测" not in report.terminal


# ===========================================================================
# D5 特殊主动脉病变
# ===========================================================================
#: 「复杂征象全部明确未见」的方案基线 —— 各测试在此基础上单点覆盖。
_D5_PLAN: dict[str, object] = {
    "D5.SYMPTOMS": ["无"],
    "D5.CAUSE": ["退变"],
    "D5.LESION_LOCATION": "肾下，未累及肾动脉",
    "D5.LESION_GROWTH": "稳定",
    "D5.PERIAORTIC_BLEED": "无",
    "D5.EMBOLISM": "否",
    "D5.PAIN_RECURRENT": "无",
    "D5.INTIMAL_DISRUPTION": "未见",
    "D5.PAU_DEPTH": "深 8mm / 宽 12mm，肾下",
    "D5.IMH_FEATURES": "最大厚度 9mm，主动脉 46mm，肾下至肾旁",
    "D5.IAAD_FEATURES": "入口位于肾下，远端至髂总，假腔血栓化",
    "D5.SACCULAR_DEF": "球形，累及部分周径",
    "D5.PSEUDO_CAUSE": ["术后吻合口"],
    "INF.IGG4": "未检测",
    "INF.PET_MRI": "未做",
}


def _d5_state(
    cl: dict[str, object] | None = None,
    n: dict[str, object] | None = None,
    c: dict[str, object] | None = None,
    s: dict[str, object] | None = None,
    inf: dict[str, object] | None = None,
    d5: dict[str, object] | None = None,
) -> GuidelineState:
    overrides: dict[str, object] = {"cl": {"aaa_diam": 56.0, "diag_multi": None, **(cl or {})}}
    for key, value in (("n", n), ("c", c), ("s", s), ("inf", inf)):
        if value:
            overrides[key] = value
    case = make_case(**overrides)
    # 按前缀分组投递：``D5.*`` 进 D5 通道，``INF.*`` / ``C.*`` 各归各的通道
    # （``node_field`` 就是按前缀定位的，全塞进 D5 会取不到）。
    channels: dict[str, dict[str, object]] = {}
    for spec_id, value in {**_D5_PLAN, **(d5 or {})}.items():
        channels.setdefault(spec_id.split(".", 1)[0], {})[spec_id] = value
    return GuidelineState(case=case, route=route(case), node_inputs=channels)


class TestD51Triage:
    """D5.1 是分诊台：壁结构病变 → D5.2、形态与病因病变 → D5.3、都不是 → G08。

    本组守的最关键一条是 **R90 不能无条件阻断**（模块文档第 ④ 条）：照规格字面
    实现成「任一字段未知即阻断」，一个 ``PAU = 明确`` 而囊状字段没填的病例就会
    停在人工复核——而它的归类依据其实已经充分。
    """

    def test_pau_goes_to_complexity(self) -> None:
        outcome = d5_1(_d5_state(cl={"pau": "明确"}))
        assert outcome.verdicts["D5.1.R01"] is True
        assert outcome.next_node == "D5.2"

    def test_imh_and_iaad_take_the_same_branch(self) -> None:
        for field in ("imh", "iaad"):
            outcome = d5_1(_d5_state(cl={field: "明确"}))
            assert outcome.next_node == "D5.2", field

    def test_saccular_goes_to_the_morphology_branch(self) -> None:
        outcome = d5_1(_d5_state(cl={"saccular": "明确"}))
        assert outcome.verdicts["D5.1.R02"] is True
        assert outcome.next_node == "D5.3"

    def test_the_infection_branch_parallels_only_when_infection_is_suspected(self) -> None:
        """「感染疑似并行 D4.1」是有条件的，不能无条件分流。

        ``R02`` 本身只判囊状/假性/炎症，不判感染。若把并行写死，每个囊状病变都会
        多跑一遍感染分诊，报告里凭空多出一条「无感染证据」——把「没查」写成
        「查过没有」，正是三值语义最忌的那种错。
        """
        without = d5_1(_d5_state(cl={"saccular": "明确"}))
        assert without.next_nodes == ("D5.3",)
        assert without.link is Link.EITHER

        with_infection = d5_1(_d5_state(cl={"saccular": "明确"}, n={"infection_ctx": "医师明确"}))
        assert with_infection.next_nodes == ("D5.3", "D4.1")
        assert with_infection.link is Link.PARALLEL

    def test_inflammatory_context_alone_reaches_d53(self) -> None:
        """``G08.R10`` 的入口：没有 PAU/囊状，靠炎症状态进 D5。"""
        outcome = d5_1(_d5_state(n={"inflammatory_ctx": "影像高度提示"}))
        assert outcome.verdicts["D5.1.R02"] is True
        assert outcome.next_node == "D5.3"

    def test_confirmed_rupture_escalates_to_the_emergency_path(self) -> None:
        outcome = d5_1(_d5_state(cl={"pau": "明确", "aaa_rupture": "明确"}))
        assert outcome.verdicts["D5.1.R03"] is True
        assert outcome.escalation == "G05"
        assert outcome.next_node is None

    def test_suspicious_imaging_sign_does_not_trigger_p0(self) -> None:
        """可疑外溢/血肿不算「可靠本次事实」——P0 只由明确事实触发。"""
        outcome = d5_1(_d5_state(cl={"pau": "明确"}, c={"extrav": "可疑", "hematoma": "可疑"}))
        assert outcome.verdicts["D5.1.R03"] is not True
        assert outcome.next_node == "D5.2"

    def test_acute_malperfusion_escalates(self) -> None:
        outcome = d5_1(_d5_state(cl={"pau": "明确"}, c={"malperfusion": ["肠系膜"]}))
        assert outcome.verdicts["D5.1.R03"] is True
        assert outcome.escalation == "G05"

    def test_branch_involvement_goes_to_complex_without_continuing_d52(self) -> None:
        """累及肾动脉 → 转复杂路径。

        出口按规格 M 列「下一节点/终点：G04、G06 / 动作：G04/G06/专科路径」——
        **值域里没有 D5.2**。修前的 ``also_next="D5.2"`` 是代码自加的：
        规格既然把「复杂或转相关主动脉路径」写成唯一动作，就不该同时继续
        在本病种节点里判复杂性（那会让同一个病人同时得到两套结论）。
        """
        outcome = d5_1(_d5_state(cl={"pau": "明确"}, n={"renal_r": "明确受累"}))
        assert outcome.verdicts["D5.1.R05"] is True
        assert outcome.escalation == "G06"
        assert outcome.next_node is None
        assert outcome.next_nodes == ("G04", "G06")

    def test_branch_involvement_outranks_the_plain_d52_route(self) -> None:
        """``R05`` 声明在 ``R01`` 之前：同优先级靠声明顺序，先声明的先命中。"""
        outcome = d5_1(_d5_state(cl={"pau": "明确"}, n={"sma": "明确受累"}))
        assert "D5.1.R01" not in outcome.verdicts

    def test_all_negative_returns_to_g08(self) -> None:
        outcome = d5_1(_d5_state())
        assert outcome.verdicts["D5.1.R91"] is True
        assert outcome.escalation == "G08"

    def test_unassessed_fields_block_when_no_subtype_is_established(self) -> None:
        outcome = d5_1(
            _d5_state(
                cl={"pau": None, "imh": None, "iaad": None, "saccular": None, "pseudo": None},
                n={"inflammatory_ctx": None},
            )
        )
        assert outcome.verdicts["D5.1.R90"] is True
        assert outcome.terminal is not None and "人工复核" in outcome.terminal

    def test_established_subtype_is_not_blocked_by_an_unfilled_neighbour(self) -> None:
        """PAU 明确 + 囊状字段没填 → 不阻断（归类依据已充分）。"""
        outcome = d5_1(_d5_state(cl={"pau": "明确", "saccular": None}))
        assert outcome.verdicts["D5.1.R90"] is False
        assert outcome.next_node == "D5.2"

    def test_cause_check_is_traced_even_if_another_rule_owns_the_exit(self) -> None:
        """``R04`` 是展示性规则：出口被 ``R02`` 拿走时它仍要留痕。"""
        outcome = d5_1(_d5_state(cl={"saccular": "明确"}))
        assert outcome.verdicts["D5.1.R04"] is True
        assert outcome.next_node == "D5.3"

    def test_missing_cause_is_named_for_morphology_cases(self) -> None:
        outcome = d5_1(_d5_state(cl={"saccular": "明确"}, d5={"D5.CAUSE": None}))
        assert any("D5.CAUSE" in gap for gap in outcome.gaps)

    def test_both_branches_present_is_disclosed(self) -> None:
        """囊状 + PAU 并存：本轮只走 D5.3，缺口里要说清 D5.2 没跑。"""
        outcome = d5_1(_d5_state(cl={"saccular": "明确", "pau": "明确"}))
        assert any("D5.2 复杂性评估未执行" in gap for gap in outcome.gaps)

    def test_spec_wording_is_rejected_by_the_model(self) -> None:
        """规格写「疑似」，模型只收「可疑」——类型系统挡住了词表混用。"""
        with pytest.raises(ValidationError):
            make_case(cl={"pau": "疑似"})


class TestD52Complexity:
    """D5.2 只回答「这个壁结构病变有没有并发征象」，不决定术式。

    三档出口的边界必须清楚：高危组合 → 加急；任一复杂征象 → 修复评估；全阴性
    才允许「无并发症 + 监测」。判不了的一律不许落到第三档。
    """

    def test_pau_with_imh_is_a_high_risk_combination(self) -> None:
        outcome = d5_2(_d5_state(cl={"pau": "明确", "imh": "明确"}, c={"malperfusion": ["无"]}))
        assert outcome.verdicts["D5.2.R03"] is True
        assert outcome.terminal is not None and "加急" in outcome.terminal

    def test_growth_alone_is_a_high_risk_combination(self) -> None:
        outcome = d5_2(
            _d5_state(
                cl={"imh": "明确"}, d5={"D5.LESION_GROWTH": "扩大"}, c={"malperfusion": ["无"]}
            )
        )
        assert outcome.verdicts["D5.2.R03"] is True

    def test_complex_sign_without_high_risk_goes_to_repair_assessment(self) -> None:
        outcome = d5_2(
            _d5_state(
                cl={"iaad": "明确"},
                d5={"D5.INTIMAL_DISRUPTION": "明确"},
                c={"malperfusion": ["无"]},
            )
        )
        assert outcome.verdicts["D5.2.R02"] is True
        assert outcome.verdicts.get("D5.2.R03") is not True
        assert outcome.terminal is not None and "专科评估是否修复" in outcome.terminal

    def test_malperfusion_is_a_complex_sign(self) -> None:
        outcome = d5_2(_d5_state(cl={"imh": "明确"}, c={"malperfusion": ["肾"]}))
        assert outcome.verdicts["D5.2.R02"] is True

    def test_all_negative_reaches_the_monitoring_branch(self) -> None:
        outcome = d5_2(_d5_state(cl={"pau": "明确"}, c={"malperfusion": ["无"]}))
        assert outcome.verdicts["D5.2.R01"] is True
        assert outcome.terminal is not None and "监测" in outcome.terminal

    def test_unrecorded_malperfusion_blocks_the_monitoring_conclusion(self) -> None:
        """灌注状态没记录 → 不许判「无并发症」（规格 R90 的 J 列）。"""
        outcome = d5_2(_d5_state(cl={"pau": "明确"}))
        assert outcome.verdicts["D5.2.R90"] is True
        assert outcome.terminal is not None and "不得判为无并发症" in outcome.terminal

    def test_incomparable_growth_blocks_instead_of_counting_as_stable(self) -> None:
        """「不可比」有值但不能比较 —— 它既不支持「扩大」也不支持「稳定」。"""
        outcome = d5_2(
            _d5_state(
                cl={"pau": "明确"}, d5={"D5.LESION_GROWTH": "不可比"}, c={"malperfusion": ["无"]}
            )
        )
        assert outcome.verdicts["D5.2.R90"] is True
        assert any("不可比" in gap for gap in outcome.gaps)

    def test_missing_embolism_status_blocks(self) -> None:
        outcome = d5_2(
            _d5_state(cl={"pau": "明确"}, d5={"D5.EMBOLISM": None}, c={"malperfusion": ["无"]})
        )
        assert outcome.verdicts["D5.2.R90"] is True

    def test_size_threshold_policy_leaves_a_trace(self) -> None:
        """``R06`` 是取值口径而非分流：无论走哪条出口都要留痕。"""
        outcome = d5_2(_d5_state(cl={"pau": "明确"}, c={"malperfusion": ["无"]}))
        assert outcome.verdicts["D5.2.R06"] is True

    def test_morphology_gaps_follow_the_present_subtype(self) -> None:
        pau = d5_2(
            _d5_state(cl={"pau": "明确"}, d5={"D5.PAU_DEPTH": None}, c={"malperfusion": ["无"]})
        )
        iaad = d5_2(
            _d5_state(
                cl={"iaad": "明确"}, d5={"D5.IAAD_FEATURES": None}, c={"malperfusion": ["无"]}
            )
        )
        assert any("D5.PAU_DEPTH" in gap for gap in pau.gaps)
        assert not any("D5.IAAD_FEATURES" in gap for gap in pau.gaps)
        assert any("D5.IAAD_FEATURES" in gap for gap in iaad.gaps)


class TestD53MorphologyAndInflammation:
    """D5.3 的核心是「按病因口径评估」，不是「选术式」。

    五条 P1 是并列义务、出口只有一个，所以本组除了守出口，还守「被遮蔽的义务
    仍然出现在缺口里」——那是这一层不丢信息的唯一保障。
    """

    def test_saccular_goes_to_special_morphology_assessment(self) -> None:
        outcome = d5_3(_d5_state(cl={"saccular": "明确"}))
        assert outcome.verdicts["D5.3.R01"] is True
        assert outcome.terminal is not None and "特殊形态" in outcome.terminal

    def test_pseudo_goes_to_cause_workup(self) -> None:
        outcome = d5_3(_d5_state(cl={"pseudo": "明确"}))
        assert outcome.verdicts["D5.3.R02"] is True
        assert outcome.terminal is not None and "病因" in outcome.terminal

    def test_inflammation_goes_to_specialist_assessment(self) -> None:
        outcome = d5_3(_d5_state(n={"inflammatory_ctx": "临床确认"}))
        assert outcome.verdicts["D5.3.R03"] is True
        assert outcome.terminal is not None and "鉴别感染" in outcome.terminal

    def test_inflammation_with_unexcluded_infection_keeps_both_labels(self) -> None:
        outcome = d5_3(
            _d5_state(n={"inflammatory_ctx": "影像高度提示", "infection_ctx": "高度怀疑"})
        )
        assert outcome.verdicts["D5.3.R04"] is True
        assert outcome.terminal is not None and "双标签" in outcome.terminal

    def test_unassessed_infection_also_keeps_both_labels(self) -> None:
        """感染「没记录」也等于「未排除」——不能因此取消感染疑似标签。"""
        outcome = d5_3(
            _d5_state(
                cl={"infection": "未评价"},
                n={"inflammatory_ctx": "临床确认", "infection_ctx": None},
            )
        )
        assert outcome.verdicts["D5.3.R04"] is True

    def test_excluded_infection_unlocks_the_pure_inflammatory_path(self) -> None:
        outcome = d5_3(_d5_state(n={"inflammatory_ctx": "临床确认"}))
        assert outcome.verdicts["D5.3.R04"] is False
        assert outcome.verdicts["D5.3.R03"] is True

    def test_below_threshold_with_symptoms_opens_the_anti_inflammatory_path(self) -> None:
        outcome = d5_3(
            _d5_state(
                n={"inflammatory_ctx": "临床确认"},
                inf={
                    "aaa_true_diam": 48.0,
                    "mantle_sign": "明确存在",
                    "symptom_pattern": "持续/反复腹背痛",
                },
            )
        )
        assert outcome.verdicts["D5.3.R09"] is True
        assert outcome.terminal is not None and "抗炎" in outcome.terminal

    def test_true_diameter_not_total_diameter_decides_the_threshold(self) -> None:
        """总径 56 mm、真实瘤径 48 mm → 仍算「低于阈值」。

        规格 Rec 145 明写修复指征不得把瘤周炎症/壁水肿算入直径。用总径判会把
        这批本该先抗炎的病例直接推进手术。
        """
        outcome = d5_3(
            _d5_state(
                cl={"aaa_diam": 56.0},
                n={"inflammatory_ctx": "临床确认"},
                inf={
                    "aaa_true_diam": 48.0,
                    "mantle_sign": "明确存在",
                    "symptom_pattern": "持续/反复腹背痛",
                },
            )
        )
        assert outcome.verdicts["D5.3.R09"] is True

    def test_true_diameter_above_threshold_skips_the_anti_inflammatory_path(self) -> None:
        outcome = d5_3(
            _d5_state(
                n={"inflammatory_ctx": "临床确认"},
                inf={
                    "aaa_true_diam": 62.0,
                    "mantle_sign": "明确存在",
                    "symptom_pattern": "持续/反复腹背痛",
                },
            )
        )
        assert outcome.verdicts["D5.3.R09"] is False
        assert outcome.verdicts["D5.3.R03"] is True

    def test_missing_true_diameter_blocks_the_low_threshold_claim(self) -> None:
        outcome = d5_3(
            _d5_state(
                n={"inflammatory_ctx": "临床确认"},
                inf={
                    "mantle_sign": "明确存在",
                    "symptom_pattern": "持续/反复腹背痛",
                    "aaa_true_diam": None,
                },
            )
        )
        assert outcome.verdicts["D5.3.R09"] is None
        assert any("真实瘤径" in gap for gap in outcome.gaps)

    def test_hydronephrosis_alone_counts_as_an_inflammatory_symptom(self) -> None:
        outcome = d5_3(
            _d5_state(
                n={"inflammatory_ctx": "临床确认"},
                inf={
                    "aaa_true_diam": 45.0,
                    "mantle_sign": "明确存在",
                    "symptom_pattern": "无",
                    "organ_involvement": ["肾积水"],
                },
            )
        )
        assert outcome.verdicts["D5.3.R09"] is True

    def test_mantle_sign_is_required_for_the_anti_inflammatory_path(self) -> None:
        outcome = d5_3(
            _d5_state(
                n={"inflammatory_ctx": "临床确认"},
                inf={
                    "aaa_true_diam": 45.0,
                    "mantle_sign": "未见",
                    "symptom_pattern": "体重下降",
                },
            )
        )
        assert outcome.verdicts["D5.3.R09"] is False

    def test_no_special_lesion_returns_to_d11(self) -> None:
        outcome = d5_3(_d5_state())
        assert outcome.verdicts["D5.3.R91"] is True
        assert outcome.next_node == "D1.1"

    def test_unassessed_fields_block_when_nothing_is_established(self) -> None:
        outcome = d5_3(
            _d5_state(
                cl={"saccular": None, "pseudo": None, "pau": None},
                n={"inflammatory_ctx": None},
            )
        )
        assert outcome.verdicts["D5.3.R90"] is True

    def test_established_shape_is_not_blocked_by_unassessed_inflammation(self) -> None:
        outcome = d5_3(_d5_state(cl={"saccular": "明确"}, n={"inflammatory_ctx": "未评价"}))
        # R01 是 P1，命中后 R90（P2）根本不求值——这正是「有亚型就不阻断」的效果
        assert outcome.verdicts.get("D5.3.R90") is not True
        assert outcome.terminal is not None and "特殊形态" in outcome.terminal

    def test_shadowed_obligations_still_show_up_in_gaps(self) -> None:
        """炎症确认 + 囊状并存：出口给炎症，但囊状的取证义务不能跟着消失。

        ``_d53_gaps`` 按**字段**枚举而不是按命中的规则枚举，所以 ``R01`` 没被
        求值时，``D5.SACCULAR_DEF`` 这类囊状侧缺口仍然出现在清单里。
        """
        outcome = d5_3(
            _d5_state(
                cl={"saccular": "明确"},
                n={"inflammatory_ctx": "临床确认"},
                d5={"D5.SACCULAR_DEF": None},
            )
        )
        assert outcome.terminal is not None and "炎症" in outcome.terminal
        assert "D5.3.R01" not in outcome.verdicts
        assert any("D5.SACCULAR_DEF" in gap for gap in outcome.gaps)

    def test_inf_fields_come_from_the_case_model_not_the_channel(self) -> None:
        """``INF.*`` 的证据明细在 ``VascCase.inf`` 里，不要求调用方再传一遍。"""
        state = _d5_state(
            n={"inflammatory_ctx": "临床确认"},
            inf={"mantle_sign": "明确存在", "wall_fibrosis": "明确", "aaa_true_diam": 44.0},
        )
        assert state.node_field("INF.MANTLE_SIGN") is None
        assert _mantle(state) == "明确存在"
        assert not any("INF.MANTLE_SIGN" in gap for gap in d5_3(state).gaps)


class TestD5DegradedInputs:
    """D5 的残缺输入与读取分支 —— 「差什么」必须能读出来，通道也要能补位。"""

    def test_classified_infection_blocks_the_clean_inflammatory_path(self) -> None:
        """``CL.INFECTION`` 已分类成立时，影像侧写「无相关征象」不能反推排除。"""
        outcome = d5_3(
            _d5_state(
                cl={"infection": "原生感染明确"},
                n={"inflammatory_ctx": "临床确认", "infection_ctx": "无相关征象"},
            )
        )
        assert outcome.verdicts["D5.3.R04"] is True

    def test_unexcluded_infection_is_named_in_d51_gaps(self) -> None:
        outcome = d5_1(_d5_state(cl={"pau": "明确", "infection": "未评价"}))
        assert any("感染证据未排除" in gap for gap in outcome.gaps)

    def test_organ_involvement_can_come_from_the_channel_as_a_scalar(self) -> None:
        """通道里写成单值字符串时按一项处理 —— 不能逐字符拆开。"""
        outcome = d5_3(
            _d5_state(
                n={"inflammatory_ctx": "临床确认"},
                inf={"organ_involvement": None, "aaa_true_diam": 45.0, "mantle_sign": "明确存在"},
                d5={"INF.ORGAN_INVOLVEMENT": "肾积水", "INF.SYMPTOM_PATTERN": "无"},
            )
        )
        assert outcome.verdicts["D5.3.R09"] is True

    def test_missing_organ_involvement_is_not_treated_as_absent(self) -> None:
        outcome = d5_3(
            _d5_state(
                n={"inflammatory_ctx": "临床确认"},
                inf={"organ_involvement": None},
                d5={"INF.ORGAN_INVOLVEMENT": None},
            )
        )
        assert any("INF.ORGAN_INVOLVEMENT" in gap for gap in outcome.gaps)

    def test_malperfusion_can_come_from_the_channel(self) -> None:
        outcome = d5_2(
            _d5_state(cl={"pau": "明确"}, c={"malperfusion": None}, d5={"C.MALPERFUSION": ["肾"]})
        )
        assert outcome.verdicts["D5.2.R02"] is True

    def test_complexity_node_without_a_lesion_returns_the_lesion_verdict(self) -> None:
        """``R03`` 先判「有没有壁结构病变」，没有时直接把结论传出去。"""
        from src.tools.guideline_engine.nodes.d5_special import _d52_r03

        assert _d52_r03(_d5_state()) is False

    def test_recorded_handles_lists_and_numbers(self) -> None:
        from src.tools.guideline_engine.nodes.d5_special import _recorded

        listed = _d5_state(cl={"pau": "明确"}, d5={"D5.PAU_DEPTH": ["深 8mm"]})
        empty = _d5_state(cl={"pau": "明确"}, d5={"D5.PAU_DEPTH": []})
        numeric = _d5_state(cl={"pau": "明确"}, d5={"D5.PAU_DEPTH": 8.0})
        assert _recorded(listed, "D5.PAU_DEPTH") is True
        assert _recorded(empty, "D5.PAU_DEPTH") is None
        assert _recorded(numeric, "D5.PAU_DEPTH") is True

    def test_r90_guard_yields_false_once_a_shape_is_established(self) -> None:
        """拦截函数本身的双向：有亚型 → 不阻断（槽位上它不会被求值，故单测）。"""
        from src.tools.guideline_engine.nodes.d5_special import _d53_r90

        assert _d53_r90(_d5_state(cl={"saccular": "明确"})) is False
        assert _d53_r90(_d5_state(cl={"saccular": None}, n={"inflammatory_ctx": None})) is True

    def test_complexity_fallback_is_always_true(self) -> None:
        """``D5.2.R91`` 是结构兜底：判据恒真，才能在新增字段后仍有人接住。"""
        from src.tools.guideline_engine.nodes.d5_special import _d52_r91

        assert _d52_r91(_d5_state(cl={"pau": "明确"})) is True


# ===========================================================================
# D6 主动脉修复术后
# ===========================================================================
#: 「一次干净的随访」基线 —— 各测试在此基础上单点覆盖。
_D6_PLAN: dict[str, object] = {
    "D6.REPAIR_DATE": "2021-05-10（术后约 5.5 年）",
    "D6.BASELINE_CTA": "已完成",
    "D6.EVAR_RISK_GROUP": "低风险",
    "D6.OSR_SURVEILLANCE": "约每5年",
    "D6.EVAR_SURVEILLANCE": "低风险低频",
    "D6.SAC_SHRINKAGE": 12.0,
    "D6.SEAL_LENGTH": "近端 18mm",
    "D6.NECK_DILATION": "否",
    "D6.TYPE13_EXCLUDED": "已排除",
    "D6.TYPE4_CONTEXT": "早期术后",
    "D6.TYPE5_CONTEXT": "未达到",
    "D6.OCCULT_LEAK_WORKUP": "未完成",
    "D6.MIGRATION": "无",
    "D6.PARA_ANASTOMOTIC": "否",
    "D6.REINTERVENTION_HISTORY": "未处理",
    "D6.LIMB_OCCLUSION": "无+无症状",
    "D6.ANATOMICAL_CHANGE": "无变化",
    "D6.AEF_POSTOP": ["无"],
    "D6.PARA_INFECT": "无支持证据",
}


def _d6_state(
    cl: dict[str, object] | None = None,
    n: dict[str, object] | None = None,
    c: dict[str, object] | None = None,
    s: dict[str, object] | None = None,
    v: dict[str, object] | None = None,
    d6: dict[str, object] | None = None,
) -> GuidelineState:
    overrides: dict[str, object] = {
        "cl": {"prev_evor": "EVAR", "post_reason": "常规随访", "diag_multi": None, **(cl or {})},
        "n": {**_CLEAN_N, **(n or {})},
        "c": {**_CLEAN_C, **(c or {})},
    }
    for key, value in (("s", s), ("v", v)):
        if value:
            overrides[key] = value
    case = make_case(**overrides)
    channels: dict[str, dict[str, object]] = {}
    for spec_id, value in {**_D6_PLAN, **(d6 or {})}.items():
        channels.setdefault(spec_id.split(".", 1)[0], {})[spec_id] = value
    return GuidelineState(case=case, route=route(case), node_inputs=channels)


#: 一次「干净随访」的解剖/CTA 侧基线（内漏无、瘤囊 0、结构无、灌注无）。
_CLEAN_N: dict[str, object] = {"endoleak_type": "无", "sac_growth": 0.0, "graft_major": "无"}
_CLEAN_C: dict[str, object] = {"malperfusion": ["无"]}


class TestD61FollowUpTriage:
    """D6.1 是术后随访的分诊台，同时是**随访计划的生成点**。

    本组守两件事：① 出口优先级（急症 > 结构/感染 > 内漏 > 常规）；② 常规出口的
    计划文案必须与「修复类型 + 风险组」一致——那是这个节点唯一真正的交付物。
    """

    def test_clean_follow_up_reaches_the_monitoring_exit(self) -> None:
        outcome = d6_1(_d6_state())
        assert outcome.verdicts["D6.1.R91"] is True
        assert outcome.terminal is not None and "低风险" in outcome.terminal

    def test_low_risk_plan_mentions_the_five_year_review(self) -> None:
        outcome = d6_1(_d6_state())
        assert outcome.terminal is not None and "5 年复查" in outcome.terminal

    def test_high_risk_plan_asks_for_annual_imaging(self) -> None:
        outcome = d6_1(_d6_state(d6={"D6.EVAR_RISK_GROUP": "高风险"}))
        assert outcome.terminal is not None and "至少年度" in outcome.terminal

    def test_missing_baseline_cta_blocks_the_low_frequency_plan(self) -> None:
        """30 天基线 CTA 没做 → 不许直接给「低频随访」方案。"""
        outcome = d6_1(_d6_state(d6={"D6.BASELINE_CTA": "未完成"}))
        assert outcome.verdicts["D6.1.R04"] is False
        assert outcome.terminal is not None and "基线 CTA 未完成" in outcome.terminal
        assert any("D6.BASELINE_CTA" in gap for gap in outcome.gaps)

    def test_osr_gets_the_five_year_surveillance_plan(self) -> None:
        outcome = d6_1(_d6_state(cl={"prev_evor": "OSR"}, d6={"D6.OSR_SURVEILLANCE": "约每5年"}))
        assert outcome.verdicts["D6.1.R06"] is True
        assert outcome.terminal is not None and "OSR 术后" in outcome.terminal

    def test_complex_evar_gets_its_own_plan(self) -> None:
        """分支表为空 → 归「其他复杂构型」（R07），不是 FEVAR/BEVAR（R08）。"""
        outcome = d6_1(_d6_state(d6={"D6.EVAR_SURVEILLANCE": "复杂EVAR个体化"}))
        assert outcome.verdicts["D6.1.R07"] is True
        assert outcome.terminal is not None and "个体化长期影像" in outcome.terminal

    def test_unstratified_risk_group_is_named_in_the_plan(self) -> None:
        outcome = d6_1(_d6_state(d6={"D6.EVAR_RISK_GROUP": "未分层"}))
        assert outcome.terminal is not None and "未分层" in outcome.terminal
        assert any("D6.EVAR_RISK_GROUP" in gap for gap in outcome.gaps)

    def test_endoleak_goes_to_d62(self) -> None:
        outcome = d6_1(_d6_state(n={"endoleak_type": "II"}))
        assert outcome.verdicts["D6.1.R02"] is True
        assert outcome.next_node == "D6.2"

    def test_sac_change_alone_goes_to_d62(self) -> None:
        outcome = d6_1(_d6_state(cl={"post_reason": "瘤囊变化"}, n={"sac_growth": 12.0}))
        assert outcome.verdicts["D6.1.R02"] is True
        assert outcome.next_node == "D6.2"

    def test_graft_failure_goes_to_d63(self) -> None:
        outcome = d6_1(_d6_state(n={"endoleak_type": "III", "graft_major": "组件分离"}))
        assert outcome.verdicts["D6.1.R03"] is True
        assert outcome.next_node == "D6.3"

    def test_graft_failure_outranks_the_endoleak_branch(self) -> None:
        """结构故障与内漏并存时先走 D6.3：D6.2 处理不了组件分离。"""
        outcome = d6_1(_d6_state(n={"endoleak_type": "II", "graft_major": "移位"}))
        assert outcome.next_node == "D6.3"
        assert "D6.1.R02" not in outcome.verdicts

    def test_infection_goes_to_d63_then_d43(self) -> None:
        """感染的完整链路：D6.1 → D6.3 → D4.3（单值是 next_node 的限制）。

        修前路径里还有 ``D1.1``：术后病人同时拿到 ``D1`` 与 ``D6`` 两个标签，
        病种引擎从两个入口各走一遍，报告里出现两套互相矛盾的随访建议
        （按直径复查 vs 内漏监测）。``G08.R01/R02`` 加上「未做过修复」前置后，
        术后只走 ``D6``，这条断言也随之收紧。
        """
        case = _d6_state(n={"endoleak_type": "无", "infection_ctx": "高度怀疑"}).case
        report = run(ToolRequest(case=case, route_result=route(case), node_inputs={"D6": _D6_PLAN}))
        assert report.path == ("D6.1", "D6.3", "D4.3")

    def test_new_rupture_escalates(self) -> None:
        outcome = d6_1(_d6_state(cl={"aaa_rupture": "明确"}))
        assert outcome.verdicts["D6.1.R01"] is True
        assert outcome.escalation == "G05"

    def test_active_gi_bleed_escalates(self) -> None:
        outcome = d6_1(_d6_state(s={"gi_bleed": ["黑便"]}))
        assert outcome.verdicts["D6.1.R01"] is True
        assert outcome.escalation == "G05"

    def test_shock_alone_escalates(self) -> None:
        outcome = d6_1(_d6_state(v={"sbp": 70.0}))
        assert outcome.verdicts["D6.1.R01"] is True

    def test_unknown_endoleak_blocks_the_routine_exit(self) -> None:
        outcome = d6_1(_d6_state(n={"endoleak_type": None, "sac_growth": 0.0, "graft_major": "无"}))
        assert outcome.verdicts["D6.1.R90"] is True
        assert outcome.terminal is not None and "人工复核" in outcome.terminal

    def test_missing_repair_date_does_not_block(self) -> None:
        """手术日期缺失只影响随访间隔，不改变「这次有没有并发症」——故只进缺口。

        照规格 F 列的字面把它列进阻断，会把所有未录日期的首次导入病例都停在
        人工复核，而其中多数只是随访间隔算不出来。
        """
        outcome = d6_1(_d6_state(d6={"D6.REPAIR_DATE": None}))
        assert outcome.verdicts["D6.1.R91"] is True
        assert any("D6.REPAIR_DATE" in gap for gap in outcome.gaps)

    def test_shrinkage_supports_the_low_risk_downgrade_only_with_other_conditions(self) -> None:
        outcome = d6_1(_d6_state())
        assert outcome.verdicts["D6.1.R09"] is True
        off = d6_1(_d6_state(d6={"D6.EVAR_RISK_GROUP": "高风险"}))
        assert off.verdicts["D6.1.R09"] is False

    def test_post_reason_is_a_single_value_in_the_model(self) -> None:
        """规格写多选，模型只收单值 —— 传列表会被类型系统挡住。"""
        with pytest.raises(ValidationError):
            make_case(cl={"post_reason": ["内漏"]})


class TestD62Endoleak:
    """D6.2 按内漏类型分流。三个 10 mm 阈值都来自 ``SAC_GROWTH_MM``。"""

    def test_type_one_goes_to_reintervention(self) -> None:
        outcome = d6_2(_d6_state(n={"endoleak_type": "I"}))
        assert outcome.verdicts["D6.2.R01"] is True
        assert outcome.terminal is not None and "再干预" in outcome.terminal

    def test_type_three_takes_the_same_branch(self) -> None:
        outcome = d6_2(_d6_state(n={"endoleak_type": "III"}))
        assert outcome.verdicts["D6.2.R01"] is True

    def test_type_two_with_growth_and_exclusion_goes_to_intervention(self) -> None:
        outcome = d6_2(_d6_state(n={"endoleak_type": "II", "sac_growth": 12.0}))
        assert outcome.verdicts["D6.2.R02"] is True
        assert outcome.terminal is not None and "Type II" in outcome.terminal

    def test_type_two_without_growth_is_not_an_indication(self) -> None:
        outcome = d6_2(_d6_state(n={"endoleak_type": "II", "sac_growth": 3.0}))
        assert outcome.verdicts["D6.2.R02"] is False

    def test_type_two_growth_without_excluding_one_three_is_held(self) -> None:
        """未排除 I/III 型 → 不得把增长归因于 Type II。"""
        outcome = d6_2(
            _d6_state(
                n={"endoleak_type": "II", "sac_growth": 12.0},
                d6={"D6.TYPE13_EXCLUDED": "未排除"},
            )
        )
        assert outcome.verdicts["D6.2.R02"] is False
        assert any("TYPE13_EXCLUDED" in gap for gap in outcome.gaps)

    def test_type_four_early_is_observed(self) -> None:
        outcome = d6_2(
            _d6_state(
                n={"endoleak_type": "IV", "sac_growth": 1.0},
                d6={"D6.TYPE4_CONTEXT": "早期术后"},
            )
        )
        assert outcome.verdicts["D6.2.R05"] is True
        assert outcome.terminal is not None and "观察" in outcome.terminal

    def test_type_four_with_growth_does_not_get_the_observe_exit(self) -> None:
        outcome = d6_2(_d6_state(n={"endoleak_type": "IV", "sac_growth": 12.0}))
        assert outcome.verdicts["D6.2.R05"] is False

    def test_no_visible_leak_but_growth_escalates_the_imaging(self) -> None:
        outcome = d6_2(_d6_state(n={"endoleak_type": "无", "sac_growth": 12.0}))
        assert outcome.verdicts["D6.2.R06"] is True
        assert outcome.terminal is not None and "多模态" in outcome.terminal

    def test_no_visible_leak_growth_with_active_infection_is_not_attributed_to_leak(self) -> None:
        outcome = d6_2(
            _d6_state(
                n={"endoleak_type": "无", "sac_growth": 12.0, "infection_ctx": "高度怀疑"},
            )
        )
        assert outcome.verdicts["D6.2.R06"] is False

    def test_migration_goes_to_the_structure_node(self) -> None:
        outcome = d6_2(
            _d6_state(n={"endoleak_type": "无"}, d6={"D6.MIGRATION": "相对固定标志 >10mm"})
        )
        assert outcome.verdicts["D6.2.R07"] is True
        assert outcome.next_node == "D6.3"

    def test_short_seal_without_leak_needs_seal_improvement(self) -> None:
        outcome = d6_2(_d6_state(n={"endoleak_type": "无"}, d6={"D6.SEAL_LENGTH": "近端 8mm"}))
        assert outcome.verdicts["D6.2.R03"] is True
        assert outcome.terminal is not None and "密封" in outcome.terminal

    def test_neck_dilation_alone_triggers_the_seal_branch(self) -> None:
        outcome = d6_2(_d6_state(n={"endoleak_type": "无"}, d6={"D6.NECK_DILATION": "是"}))
        assert outcome.verdicts["D6.2.R03"] is True

    def test_suspected_type_is_treated_as_unresolved(self) -> None:
        outcome = d6_2(_d6_state(n={"endoleak_type": "疑似"}))
        assert outcome.verdicts["D6.2.R04"] is True
        assert outcome.terminal is not None and "人工复核" in outcome.terminal

    def test_no_leak_no_growth_reaches_observation(self) -> None:
        outcome = d6_2(_d6_state())
        assert outcome.verdicts["D6.2.R91"] is True

    def test_missing_seal_length_is_a_gap_not_a_silent_normal(self) -> None:
        outcome = d6_2(_d6_state(n={"endoleak_type": "无"}, d6={"D6.SEAL_LENGTH": None}))
        assert any("D6.SEAL_LENGTH" in gap for gap in outcome.gaps)


class TestD63StructureAndTargetVessel:
    """D6.3 的两个 P0-A 先于一切：术后 AEF 出血与急性缺血。"""

    def test_graft_infection_goes_to_d43(self) -> None:
        outcome = d6_3(_d6_state(n={"infection_ctx": "医师明确"}))
        assert outcome.verdicts["D6.3.R01"] is True
        assert outcome.next_node == "D4.3"

    def test_fistula_goes_to_d43(self) -> None:
        outcome = d6_3(_d6_state(cl={"fistula": "主动脉肠瘘"}))
        assert outcome.verdicts["D6.3.R01"] is True
        assert outcome.next_node == "D4.3"

    def test_target_vessel_occlusion_goes_to_revascularisation(self) -> None:
        """闭塞但**无临床灌注障碍** → 及时评估再血管化，不是急症（后者由 R06 接）。"""
        outcome = d6_3(_d6_state(c={"branch": {"左肾动脉": "闭塞"}}))
        assert outcome.verdicts["D6.3.R02"] is True
        assert outcome.terminal is not None and "再血管化" in outcome.terminal

    def test_target_vessel_occlusion_with_malperfusion_is_a_p0(self) -> None:
        """规格 M 列：「下一节点/终点：**G03** / 动作：急症→G03」——不是 G05。

        G03 是重跑 21 条急诊筛查，G05 是直接进急诊模式；两者不是同一条路。
        """
        outcome = d6_3(_d6_state(c={"branch": {"左肾动脉": "闭塞"}, "malperfusion": ["肾"]}))
        assert outcome.verdicts["D6.3.R06"] is True
        assert outcome.escalation == "G03"
        assert outcome.next_nodes == ("G03",)

    def test_para_anastomotic_aneurysm_goes_to_reintervention_review(self) -> None:
        outcome = d6_3(
            _d6_state(
                cl={"prev_evor": "OSR"},
                d6={"D6.PARA_ANASTOMOTIC": "明确", "D6.PARA_INFECT": None},
            )
        )
        assert outcome.verdicts["D6.3.R03"] is True
        assert any("感染" in gap for gap in outcome.gaps)

    def test_graft_component_separation_needs_reintervention_review(self) -> None:
        outcome = d6_3(_d6_state(n={"graft_major": "组件分离"}))
        assert outcome.verdicts["D6.3.R04"] is True

    def test_postop_aef_with_bleeding_escalates_and_goes_to_d43(self) -> None:
        outcome = d6_3(
            _d6_state(
                s={"gi_bleed": ["黑便"]},
                n={"infection_ctx": "医师明确"},
                d6={"D6.AEF_POSTOP": ["GI出血", "腔外气体"]},
            )
        )
        assert outcome.verdicts["D6.3.R08"] is True
        assert outcome.escalation == "G05"
        assert outcome.next_node == "D4.3"

    def test_acute_limb_ischemia_escalates(self) -> None:
        outcome = d6_3(_d6_state(s={"limb": ["苍白", "运动障碍"]}))
        assert outcome.verdicts["D6.3.R06"] is True
        assert outcome.escalation == "G03"

    def test_clean_structure_reaches_follow_up(self) -> None:
        outcome = d6_3(_d6_state())
        assert outcome.verdicts["D6.3.R91"] is True
        assert outcome.terminal is not None and "随访" in outcome.terminal

    def test_unknown_structure_blocks_the_clean_conclusion(self) -> None:
        outcome = d6_3(_d6_state(n={"graft_major": "疑似"}))
        assert outcome.verdicts["D6.3.R90"] is True

    def test_empty_branch_table_is_not_a_gap(self) -> None:
        """标准 EVAR 没有分支靶血管要评估，空 ``C.BRANCH`` 不该报缺口。"""
        outcome = d6_3(_d6_state())
        assert not any("C.BRANCH" in gap for gap in outcome.gaps)

    def test_recorded_branch_without_patency_is_a_gap(self) -> None:
        outcome = d6_3(_d6_state(c={"branch": {"左肾动脉": "不可评价"}, "malperfusion": ["无"]}))
        assert any("C.BRANCH" in gap for gap in outcome.gaps)


class TestD6EdgeBranches:
    """D6 的边角取舍 —— 「判不了」与「不适用」必须能分开。"""

    def test_complex_non_branching_repair_gets_the_individualised_plan(self) -> None:
        """分支表空 → 非分支复杂构型（R07），文案里没有「30 天 + 1 年」。"""
        outcome = d6_1(_d6_state(d6={"D6.EVAR_SURVEILLANCE": "复杂EVAR个体化"}))
        assert outcome.verdicts["D6.1.R07"] is True
        assert outcome.verdicts["D6.1.R08"] is False
        assert outcome.terminal is not None and "个体化长期影像" in outcome.terminal
        assert "30 天内与 1 年" not in outcome.terminal

    def test_branching_repair_gets_the_30_day_plus_1_year_plan(self) -> None:
        outcome = d6_1(
            _d6_state(
                c={"branch": {"左肾动脉": "通畅"}},
                d6={"D6.EVAR_SURVEILLANCE": "复杂EVAR个体化"},
            )
        )
        assert outcome.verdicts["D6.1.R08"] is True
        assert outcome.terminal is not None and "30 天内与 1 年 CTA" in outcome.terminal

    def test_failure_group_is_routed_to_reintervention_not_routine(self) -> None:
        outcome = d6_1(_d6_state(d6={"D6.EVAR_RISK_GROUP": "失败组"}))
        assert outcome.terminal is not None and "再干预评估" in outcome.terminal

    def test_unrepaired_case_is_reported_as_a_gap(self) -> None:
        """没有既往修复史却走到术后节点 —— 前提缺失必须点名。"""
        outcome = d6_1(_d6_state(cl={"prev_evor": "无"}))
        assert any("CL.PREV_EVOR" in gap for gap in outcome.gaps)

    def test_osr_without_a_plan_is_a_gap(self) -> None:
        outcome = d6_1(_d6_state(cl={"prev_evor": "OSR"}, d6={"D6.OSR_SURVEILLANCE": None}))
        assert any("D6.OSR_SURVEILLANCE" in gap for gap in outcome.gaps)

    def test_suspected_fistula_type_is_named(self) -> None:
        outcome = d6_1(_d6_state(cl={"fistula": "疑似"}))
        assert any("CL.FISTULA_TYPE" in gap for gap in outcome.gaps)

    def test_all_follow_up_state_missing_is_listed_field_by_field(self) -> None:
        outcome = d6_1(
            _d6_state(
                n={
                    "endoleak_type": None,
                    "sac_growth": None,
                    "graft_major": None,
                    "infection_ctx": None,
                },
                c={"malperfusion": None},
            )
        )
        joined = " ".join(outcome.gaps)
        assert "N.ENDOLEAK_TYPE" in joined
        assert "N.SAC_GROWTH" in joined
        assert "N.GRAFT_MAJOR" in joined
        assert "N.INFECTION_CTX" in joined
        assert "C.MALPERFUSION" in joined

    def test_growth_alone_with_incomparable_growth_still_reaches_d62(self) -> None:
        """就诊原因写了「瘤囊变化」但增长值不可比 → 仍进 D6.2 去把数值补出来。

        「不可比」不是「没变化」：把它挡在分诊台上，等于让唯一的客观依据消失。
        """
        outcome = d6_1(_d6_state(cl={"post_reason": "瘤囊变化"}, n={"sac_growth": None}))
        assert outcome.verdicts["D6.1.R02"] is True
        assert outcome.next_node == "D6.2"

    def test_risk_group_unrecorded_is_unknown_not_low(self) -> None:
        from src.tools.guideline_engine.nodes.d6_postop import _d61_r05

        assert _d61_r05(_d6_state(d6={"D6.EVAR_RISK_GROUP": None})) is None

    def test_shrinkage_below_threshold_does_not_support_the_downgrade(self) -> None:
        outcome = d6_1(_d6_state(d6={"D6.SAC_SHRINKAGE": 4.0}))
        assert outcome.verdicts["D6.1.R09"] is False

    def test_missing_shrinkage_is_unknown(self) -> None:
        from src.tools.guideline_engine.nodes.d6_postop import _d61_r09

        assert _d61_r09(_d6_state(d6={"D6.SAC_SHRINKAGE": None})) is None

    def test_number_parser_rejects_booleans_and_text_without_digits(self) -> None:
        from src.tools.guideline_engine.nodes.d6_postop import _number

        assert _number(True) is None
        assert _number("未测") is None
        assert _number("近端 8mm") == 8.0

    def test_uncertain_migration_is_unknown_not_negative(self) -> None:
        from src.tools.guideline_engine.nodes.d6_postop import _migration_significant

        assert _migration_significant(_d6_state(d6={"D6.MIGRATION": "未知"})) is None
        assert _migration_significant(_d6_state(d6={"D6.MIGRATION": None})) is None

    def test_negative_branch_values_are_not_occlusion(self) -> None:
        from src.tools.guideline_engine.nodes.d6_postop import _branch_occlusion

        assert _branch_occlusion(_d6_state(c={"branch": {"左肾动脉": "通畅"}})) is False

    def test_type_four_without_context_asks_for_it(self) -> None:
        outcome = d6_2(
            _d6_state(n={"endoleak_type": "IV", "sac_growth": 2.0}, d6={"D6.TYPE4_CONTEXT": None})
        )
        assert any("D6.TYPE4_CONTEXT" in gap for gap in outcome.gaps)

    def test_unresolved_endoleak_is_diagnostic_uncertainty(self) -> None:
        """已知有内漏（就诊原因写了）但分不出型 → 不能按 Type II 归因。"""
        from src.tools.guideline_engine.nodes.d6_postop import _d62_r04

        state = _d6_state(cl={"post_reason": "内漏"}, n={"endoleak_type": None})
        assert _d62_r04(state) is True

    def test_no_endoleak_means_no_diagnostic_uncertainty(self) -> None:
        from src.tools.guideline_engine.nodes.d6_postop import _d62_r04

        assert _d62_r04(_d6_state(n={"endoleak_type": None})) is False

    def test_missing_seal_and_migration_are_reported(self) -> None:
        outcome = d6_2(
            _d6_state(n={"endoleak_type": "无"}, d6={"D6.SEAL_LENGTH": None, "D6.MIGRATION": None})
        )
        joined = " ".join(outcome.gaps)
        assert "D6.SEAL_LENGTH" in joined
        assert "D6.MIGRATION" in joined

    def test_structure_node_skips_p0_without_a_repair_history(self) -> None:
        from src.tools.guideline_engine.nodes.d6_postop import _d63_r08

        assert _d63_r08(_d6_state(cl={"prev_evor": "无"}, s={"gi_bleed": ["黑便"]})) is False

    def test_structure_gaps_cover_infection_fistula_and_perfusion(self) -> None:
        outcome = d6_3(
            _d6_state(
                n={"infection_ctx": None},
                c={"malperfusion": None},
            )
        )
        joined = " ".join(outcome.gaps)
        assert "N.INFECTION_CTX" in joined
        assert "C.MALPERFUSION" in joined

    def test_unresolved_fistula_status_is_named(self) -> None:
        outcome = d6_3(_d6_state(cl={"fistula": "未知"}))
        assert any("CL.FISTULA" in gap for gap in outcome.gaps)

    def test_reintervention_history_gap_for_para_anastomotic_aneurysm(self) -> None:
        outcome = d6_3(
            _d6_state(
                cl={"prev_evor": "OSR"},
                d6={"D6.PARA_ANASTOMOTIC": "明确", "D6.REINTERVENTION_HISTORY": None},
            )
        )
        assert any("D6.REINTERVENTION_HISTORY" in gap for gap in outcome.gaps)


class TestSharedEmergencyPrimitives:
    """公共层的急诊原语 —— D2/D3/D4/D6 共用同一份口径。"""

    def test_empty_malperfusion_list_is_unknown(self) -> None:
        from src.tools.guideline_engine.nodes.common import malperfusion

        state = _d6_state(c={"malperfusion": []})
        assert malperfusion(state) is None

    def test_sac_growth_is_none_when_incomparable(self) -> None:
        from src.tools.guideline_engine.nodes.common import sac_growth

        assert sac_growth(_d6_state(n={"sac_growth": None})) is None

    def test_sac_growing_is_unknown_without_a_value(self) -> None:
        from src.tools.guideline_engine.nodes.common import sac_growing

        assert sac_growing(_d6_state(n={"sac_growth": None})) is None

    def test_multi_field_wraps_a_scalar(self) -> None:
        from src.tools.guideline_engine.nodes.common import multi_field

        state = _d6_state(d6={"D6.AEF_POSTOP": 7})
        assert multi_field(state, "D6.AEF_POSTOP") == [7]


# ===========================================================================
# D7 髂动脉瘤
# ===========================================================================
#: 「一次干净的监测随访」基线 —— 各测试在此基础上单点覆盖。
_D7_PLAN: dict[str, object] = {
    "D7.SYMPTOM_COMPRESSION": ["无"],
    "D7.ANEURYSM_TYPE": ["退变性"],
    "D7.DIAM_METHOD": "超声",
    "D7.LIFE_EXPECTANCY": "较长",
    "D7.PREFERENCE": "倾向监测",
    "D7.GROWTH_RATE": "0",
    "D7.SURVEILLANCE_ELIGIBILITY": "有潜在修复获益",
    "D7.TARGET_SEAL": "可用",
    "D7.PELVIC_PLAN": "不适用",
    "D7.IIA_PRESERVATION": "双侧保留",
    "D7.IBE_IFU": "CE；X 型号；IFU v2.1；符合",
    "D7.OPEN_RECON": "近远端吻合区",
}


def _d7_state(
    cl: dict[str, object] | None = None,
    n: dict[str, object] | None = None,
    d7: dict[str, object] | None = None,
) -> GuidelineState:
    """构造 D7 病例：默认「左侧 CIA 28 mm、无症状、退变性」。

    三处基线偏离 ``make_case()``，每一处都有理由：

    · ``iliac_site = ["左侧", "CIA"]`` —— 基线是 ``["无"]``，那是「明确没有」；
    · ``aaa_reported = "否定"`` 且 ``diag_multi = None`` —— 基线写「确认」会让
      ``D7.1.R02`` 恒为 True（变成测「合并 AAA」而不是测本组想测的分流），
      ``diag_multi = ["D1"]`` 会先跑一遍 D1 链并污染 ``path``；
    · ``iliac_diam = {"左侧CIA": 28.0}`` —— 基线是空字典（无直径）。
    """
    overrides: dict[str, object] = {
        "cl": {
            "iliac_site": ["左侧", "CIA"],
            "iliac_diam": {"左侧CIA": 28.0},
            "aaa_reported": "否定",
            "diag_multi": None,
            **(cl or {}),
        },
    }
    if n:
        overrides["n"] = n
    case = make_case(**overrides)
    channels: dict[str, dict[str, object]] = {}
    for spec_id, value in {**_D7_PLAN, **(d7 or {})}.items():
        channels.setdefault(spec_id.split(".", 1)[0], {})[spec_id] = value
    return GuidelineState(case=case, route=route(case), node_inputs=channels)


class TestD71Triage:
    """D7.1 是髂动脉瘤的分诊台：先排除急症，再分病种，最后进 D7.2。"""

    def test_confirmed_aneurysm_with_diameter_goes_to_d72(self) -> None:
        outcome = d7_1(_d7_state())
        assert outcome.verdicts["D7.1.R03"] is True
        assert outcome.next_node == "D7.2"

    def test_missing_diameter_still_goes_to_d72(self) -> None:
        """直径没测 → R03 不成立，由 R91 接住（补测是 D7.2 的事，不是分诊台的）。"""
        outcome = d7_1(_d7_state(cl={"iliac_diam": {}}))
        assert outcome.verdicts["D7.1.R03"] is False
        assert outcome.verdicts["D7.1.R91"] is True
        assert outcome.next_node == "D7.2"

    def test_side_without_segment_is_unresolved(self) -> None:
        """只有「左侧」不知道哪一段 —— 不猜，转人工复核。"""
        outcome = d7_1(_d7_state(cl={"iliac_site": ["左侧"]}))
        assert outcome.verdicts["D7.1.R90"] is True
        assert outcome.terminal is not None and "人工复核" in outcome.terminal

    def test_segment_without_side_is_unresolved(self) -> None:
        """只有「CIA」不知道哪一侧 —— 同上。"""
        outcome = d7_1(_d7_state(cl={"iliac_site": ["CIA"]}))
        assert outcome.verdicts["D7.1.R90"] is True

    def test_explicit_no_iliac_aneurysm_does_not_block(self) -> None:
        """明确写「无」不是「判不了」—— R90 为 False，落到全局兜底。"""
        outcome = d7_1(_d7_state(cl={"iliac_site": ["无"]}))
        assert outcome.verdicts["D7.1.R90"] is False
        assert outcome.terminal is not None and "人工复核" in outcome.terminal

    def test_rupture_escalates(self) -> None:
        outcome = d7_1(_d7_state(cl={"aaa_rupture": "明确"}))
        assert outcome.verdicts["D7.1.R01"] is True
        assert outcome.escalation == "G05"

    def test_shock_escalates(self) -> None:
        case = make_case(cl={"iliac_site": ["右侧", "IIA"], "diag_multi": None}, v={"sbp": 70.0})
        outcome = d7_1(GuidelineState(case=case, route=route(case), node_inputs={}))
        assert outcome.verdicts["D7.1.R01"] is True
        assert outcome.escalation == "G05"

    def test_unresolved_rupture_status_blocks(self) -> None:
        outcome = d7_1(_d7_state(cl={"aaa_rupture": "无法评价"}))
        assert outcome.verdicts["D7.1.R90"] is True
        # 「无法评价」不是「未见」—— R01 只能给 UNKNOWN，不能给 False
        assert outcome.verdicts["D7.1.R01"] is not True

    def test_pseudoaneurysm_goes_to_d5(self) -> None:
        outcome = d7_1(_d7_state(cl={"pseudo": "明确"}))
        assert outcome.verdicts["D7.1.R05"] is True
        assert outcome.next_node == "D5.1"

    def test_infection_goes_to_d4(self) -> None:
        outcome = d7_1(_d7_state(cl={"infection": "原生感染明确"}))
        assert outcome.verdicts["D7.1.R05"] is True
        assert outcome.next_node == "D4.1"

    def test_doctor_infection_judgement_also_routes_to_d4(self) -> None:
        """``CL.INFECTION`` 写「无证据」、但医生在 ``N.INFECTION_CTX`` 写「高度怀疑」。

        D4 与 D6 都把这种病例当感染处理（``_infection_signal`` / ``_infection_suspected``
        读医生判断）。D7 若只看分型字段，会把它当退变性病变一路走到 D7.3，
        在报告里给出「髂动脉腔内修复：可行候选」——与 D4 的「EVAR 仅作桥接」相反。
        """
        outcome = d7_1(_d7_state(cl={"infection": "无证据"}, n={"infection_ctx": "高度怀疑"}))
        assert outcome.verdicts["D7.1.R05"] is True
        assert outcome.next_node == "D4.1"

    def test_postop_anastomotic_goes_to_d6(self) -> None:
        outcome = d7_1(
            _d7_state(
                n={"prev_repair": "EVAR"},
                d7={"D7.ANEURYSM_TYPE": ["术后吻合口旁"]},
            )
        )
        assert outcome.verdicts["D7.1.R05"] is True
        assert outcome.next_node == "D6.1"

    def test_r05_entry_records_the_actual_target(self) -> None:
        """留痕里的去向必须与实际一致 —— 否则报告说「去 D4.1」而实际去了 D5.1。"""
        outcome = d7_1(_d7_state(cl={"pseudo": "明确"}))
        entry = next(e for e in outcome.entries if e.rule_id == "D7.1.R05")
        assert entry.next_node == outcome.next_node == "D5.1"

    def test_repair_history_alone_does_not_trigger_r05(self) -> None:
        """有修复史 ≠ 吻合口旁瘤：类型没记录时只进缺口，不改道。"""
        outcome = d7_1(_d7_state(n={"prev_repair": "EVAR"}))
        assert outcome.verdicts["D7.1.R05"] is False
        assert outcome.next_node == "D7.2"

    def test_aaa_comorbidity_is_display_only(self) -> None:
        """合并 AAA 是并行信号，不是改道 —— R02 留痕为真，出口仍是 D7.2。"""
        outcome = d7_1(_d7_state(cl={"aaa_reported": "确认"}))
        assert outcome.verdicts["D7.1.R02"] is True
        assert outcome.next_node == "D7.2"
        assert any("D1/D2/D3 病种并行" in gap for gap in outcome.gaps)

    def test_repaired_aaa_is_not_a_current_comorbidity(self) -> None:
        """「既往已修复」不等于当前并存 AAA（规格 CL.AAA_REPORTED 原话）。"""
        case = make_case(
            cl={"iliac_site": ["左侧", "CIA"], "aaa_reported": "未述及", "diag_multi": None},
            d={"dx_context": "既往已修复"},
        )
        state = GuidelineState(case=case, route=route(case), node_inputs={})
        assert d7_1(state).verdicts["D7.1.R02"] is not True

    def test_symptom_is_display_only(self) -> None:
        """有症状改变的是「阈值还成不成立」，不是「去哪」。"""
        outcome = d7_1(_d7_state(d7={"D7.SYMPTOM_COMPRESSION": ["盆腔/腹股沟疼痛"]}))
        assert outcome.verdicts["D7.1.R04"] is True
        assert outcome.next_node == "D7.2"


class TestD72ThresholdAndSurveillance:
    """D7.2 的两条硬口径：40 mm 是修复评估入口，监测间隔按 Rec134 三档。"""

    def test_40mm_goes_to_d73(self) -> None:
        outcome = d7_2(_d7_state(cl={"iliac_diam": {"左侧CIA": 40.0}}))
        assert outcome.verdicts["D7.2.R02"] is True
        assert outcome.next_node == "D7.3"

    def test_39mm_stays_in_surveillance(self) -> None:
        outcome = d7_2(_d7_state(cl={"iliac_diam": {"左侧CIA": 39.0}}))
        assert outcome.verdicts["D7.2.R03"] is True
        assert outcome.terminal is not None and "每年超声随访" in outcome.terminal

    def test_28mm_gets_the_two_year_interval(self) -> None:
        outcome = d7_2(_d7_state(cl={"iliac_diam": {"左侧CIA": 28.0}}))
        assert outcome.terminal is not None and "每 2 年超声随访" in outcome.terminal

    def test_22mm_gets_the_three_year_interval(self) -> None:
        outcome = d7_2(_d7_state(cl={"iliac_diam": {"左侧CIA": 22.0}}))
        assert outcome.terminal is not None and "每 3 年超声随访" in outcome.terminal

    def test_18mm_does_not_use_the_standard_interval(self) -> None:
        outcome = d7_2(_d7_state(cl={"iliac_diam": {"左侧CIA": 18.0}}))
        assert outcome.verdicts["D7.2.R07"] is True
        assert outcome.terminal is not None and "不套用既定间隔" in outcome.terminal

    def test_symptom_below_threshold_goes_to_d73(self) -> None:
        """未达 40 mm 但有症状 → 不自动进普通监测（R06 是 P0-A）。"""
        outcome = d7_2(
            _d7_state(
                cl={"iliac_diam": {"左侧CIA": 28.0}},
                d7={"D7.SYMPTOM_COMPRESSION": ["神经压迫"]},
            )
        )
        assert outcome.verdicts["D7.2.R06"] is True
        assert outcome.next_node == "D7.3"

    def test_complex_etiology_below_threshold_goes_to_d73(self) -> None:
        outcome = d7_2(
            _d7_state(
                cl={"iliac_diam": {"左侧CIA": 28.0}, "pseudo": "疑似"},
            )
        )
        assert outcome.verdicts["D7.2.R06"] is True
        assert outcome.next_node == "D7.3"

    def test_r06_does_not_shadow_r02_above_threshold(self) -> None:
        """R06 的判据带「低于阈值」前提，否则达阈值的病例会全被它截走。"""
        outcome = d7_2(
            _d7_state(
                cl={"iliac_diam": {"左侧CIA": 45.0}},
                d7={"D7.SYMPTOM_COMPRESSION": ["栓塞"]},
            )
        )
        assert outcome.verdicts["D7.2.R06"] is False
        assert outcome.verdicts["D7.2.R02"] is True

    def test_acute_ischemia_escalates_and_still_reaches_d73(self) -> None:
        """急症回流与术式评估是「同时」，不是「改道」。"""
        outcome = d7_2(
            _d7_state(
                cl={"iliac_diam": {"左侧CIA": 28.0}},
                d7={"D7.SYMPTOM_COMPRESSION": ["急性肢体缺血"]},
            )
        )
        assert outcome.escalation == "G03"
        assert outcome.next_node == "D7.3"

    def test_incomparable_growth_asks_for_remeasure(self) -> None:
        outcome = d7_2(_d7_state(d7={"D7.GROWTH_RATE": "不可比"}))
        assert outcome.verdicts["D7.2.R05"] is True
        assert outcome.terminal is not None and "不可比" in outcome.terminal

    def test_comparable_growth_does_not_trigger_remeasure(self) -> None:
        """R05 的判据是「不可比才命中」—— 取反写错会让每个病例都被打回复测。"""
        outcome = d7_2(_d7_state())
        assert outcome.verdicts["D7.2.R05"] is False
        assert outcome.verdicts["D7.2.R03"] is True

    def test_missing_method_asks_for_remeasure(self) -> None:
        outcome = d7_2(_d7_state(d7={"D7.DIAM_METHOD": None, "D7.GROWTH_RATE": None}))
        assert outcome.verdicts["D7.2.R04"] is True
        assert outcome.terminal is not None and "复测" in outcome.terminal

    def test_missing_diameter_blocks(self) -> None:
        outcome = d7_2(_d7_state(cl={"iliac_diam": {}}))
        assert outcome.verdicts["D7.2.R90"] is True
        assert outcome.terminal is not None and "人工复核" in outcome.terminal

    def test_unrecorded_symptom_blocks(self) -> None:
        outcome = d7_2(_d7_state(d7={"D7.SYMPTOM_COMPRESSION": ["未知"]}))
        assert outcome.verdicts["D7.2.R90"] is True

    def test_life_expectancy_modifies_the_plan(self) -> None:
        outcome = d7_2(_d7_state(d7={"D7.LIFE_EXPECTANCY": "有限"}))
        assert outcome.terminal is not None and "寿命预期有限" in outcome.terminal

    def test_limited_surveillance_eligibility_prompts_discussion(self) -> None:
        outcome = d7_2(_d7_state(d7={"D7.SURVEILLANCE_ELIGIBILITY": "有限"}))
        assert outcome.terminal is not None and "实际获益有限" in outcome.terminal

    def test_patient_preference_reaches_the_plan(self) -> None:
        outcome = d7_2(_d7_state(d7={"D7.PREFERENCE": "倾向修复"}))
        assert outcome.terminal is not None and "共同决策" in outcome.terminal

    def test_single_segment_is_flagged(self) -> None:
        """只测了一段 → 最大径可能取自非目标段，必须提示逐段测量。"""
        outcome = d7_2(_d7_state(cl={"iliac_diam": {"左侧CIA": 28.0}}))
        assert any("只记录了 1 个血管段" in gap for gap in outcome.gaps)

    def test_maximal_segment_decides_the_threshold(self) -> None:
        """多段并存时取最大段 —— 38 mm 与 41 mm 并存要判达阈值。"""
        outcome = d7_2(_d7_state(cl={"iliac_diam": {"左侧CIA": 41.0, "右侧CIA": 22.0}}))
        assert outcome.verdicts["D7.2.R02"] is True


class TestD73Candidates:
    """D7.3 的三列候选 + 盆腔灌注保护这条硬线。"""

    def test_all_conditions_met_lists_feasible_endovascular(self) -> None:
        outcome = d7_3(_d7_state())
        status = {c.procedure: c.status for c in outcome.candidates}
        assert status["髂动脉腔内修复"] is CandidateStatus.FEASIBLE

    def test_seal_insufficient_excludes_endovascular(self) -> None:
        outcome = d7_3(_d7_state(d7={"D7.TARGET_SEAL": "不足"}))
        status = {c.procedure: c.status for c in outcome.candidates}
        assert status["髂动脉腔内修复"] is CandidateStatus.EXCLUDED

    def test_bilateral_occlusion_excludes_endovascular(self) -> None:
        outcome = d7_3(_d7_state(n={"access_occlusion": "双侧闭塞"}))
        status = {c.procedure: c.status for c in outcome.candidates}
        assert status["髂动脉腔内修复"] is CandidateStatus.EXCLUDED

    def test_bilateral_iia_without_a_plan_pauses_for_mdt(self) -> None:
        outcome = d7_3(
            _d7_state(
                n={"iliac_bilat": "双侧"},
                d7={"D7.PELVIC_PLAN": "未确定", "D7.IIA_PRESERVATION": "未定"},
            )
        )
        assert outcome.verdicts["D7.3.R03"] is True
        assert outcome.terminal is not None and "MDT" in outcome.terminal

    def test_bilateral_iia_with_a_branch_plan_proceeds(self) -> None:
        outcome = d7_3(
            _d7_state(
                n={"iliac_bilat": "双侧"},
                d7={"D7.PELVIC_PLAN": "分支器械"},
            )
        )
        assert outcome.verdicts["D7.3.R03"] is False

    def test_pelvic_risk_downgrades_a_feasible_candidate(self) -> None:
        """内髂方案没定，「可行」就建立在一个未成立的前提上 —— 降为条件性。"""
        outcome = d7_3(
            _d7_state(
                n={"iliac_bilat": "双侧"},
                d7={"D7.PELVIC_PLAN": "未确定", "D7.IIA_PRESERVATION": "未定"},
            )
        )
        status = {c.procedure: c.status for c in outcome.candidates}
        assert status["髂动脉腔内修复"] is CandidateStatus.CONDITIONAL

    def test_ifu_verified_makes_branch_feasible(self) -> None:
        outcome = d7_3(_d7_state())
        status = {c.procedure: c.status for c in outcome.candidates}
        assert status["髂分支器械（IBE/IBD）"] is CandidateStatus.FEASIBLE

    def test_ifu_unverified_makes_branch_conditional(self) -> None:
        outcome = d7_3(_d7_state(d7={"D7.IBE_IFU": "CE；X 型号；IFU v2.1；未核验"}))
        status = {c.procedure: c.status for c in outcome.candidates}
        assert status["髂分支器械（IBE/IBD）"] is CandidateStatus.CONDITIONAL

    def test_ifu_not_matching_is_false_not_unknown(self) -> None:
        """「不符合」里含「符合」—— 先判不符合再判符合，否则每个都成「适配」。"""
        from src.tools.guideline_engine.nodes.d7_iliac import _ifu_ok

        state = _d7_state(d7={"D7.IBE_IFU": "CE；X 型号；IFU v2.1；不符合"})
        assert _ifu_ok(state) is False

    def test_seal_blocked_offers_open_reconstruction(self) -> None:
        outcome = d7_3(_d7_state(d7={"D7.TARGET_SEAL": "病变累及"}))
        status = {c.procedure: c.status for c in outcome.candidates}
        assert status["开放/杂交髂动脉重建"] is CandidateStatus.MDT

    def test_no_open_indication_excludes_open(self) -> None:
        outcome = d7_3(_d7_state())
        status = {c.procedure: c.status for c in outcome.candidates}
        assert status["开放/杂交髂动脉重建"] is CandidateStatus.EXCLUDED

    def test_cta_unevaluable_withholds_all_candidates(self) -> None:
        """连 CTA 都不可评价时，三列候选全是猜的 —— 空元组本身就是结论。"""
        outcome = d7_3(_d7_state(n={"cta_status": "缺失"}))
        assert outcome.verdicts["D7.3.R90"] is True
        assert outcome.candidates == ()

    @pytest.mark.parametrize("status", ["部分", "冲突"])
    def test_incomplete_cta_gives_draft_candidates(self, status: str) -> None:
        """CTA 部分可评价 / 结果冲突 —— 给草案，但必须说清「为什么是草案」。

        修前：``R91`` 拿 ``cta_evaluable`` 当判据，这两种状态返回 ``None`` →
        本节点一条规则都不命中 → 落到全局兜底，报告上是**通用「人工复核」文案 +
        三条完整候选**。读者既看不出被复核的原因，也看不出候选来自残缺影像。
        """
        outcome = d7_3(_d7_state(n={"cta_status": status}))
        assert outcome.verdicts["D7.3.R90"] is False
        assert outcome.verdicts["D7.3.R04"] is True
        assert outcome.terminal is not None and "候选草案" in outcome.terminal
        assert outcome.candidates
        assert any("部分可评价或结果冲突" in gap for gap in outcome.gaps)

    def test_incomplete_cta_marks_candidates_conditional(self) -> None:
        """草案里的候选只能是「条件性」——影像没看全，说「可行」越权。"""
        outcome = d7_3(_d7_state(n={"cta_status": "部分"}))
        statuses = {c.procedure: c.status for c in outcome.candidates}
        assert statuses["髂动脉腔内修复"] is CandidateStatus.CONDITIONAL

    def test_unverified_detail_gives_draft_only(self) -> None:
        outcome = d7_3(_d7_state(d7={"D7.IBE_IFU": "未核验"}))
        assert outcome.verdicts["D7.3.R90"] is False
        assert outcome.verdicts["D7.3.R04"] is True
        assert outcome.terminal is not None and "候选草案" in outcome.terminal

    def test_symptom_note_reaches_the_gaps(self) -> None:
        outcome = d7_3(_d7_state(d7={"D7.SYMPTOM_COMPRESSION": ["静脉压迫"]}))
        assert any("不受 40 mm 阈值限制" in gap for gap in outcome.gaps)

    def test_symptom_note_reaches_the_terminal(self) -> None:
        """18 mm 但有压迫时，终点不能只写「术式方向已列出」——会被读成「按直径该修」。"""
        outcome = d7_3(_d7_state(d7={"D7.SYMPTOM_COMPRESSION": ["输尿管梗阻"]}))
        assert outcome.terminal is not None and "不依赖 40 mm 直径阈值" in outcome.terminal

    def test_non_degenerative_lesion_is_capped_at_mdt(self) -> None:
        """感染性/假性/吻合口旁：解剖再「合适」也不给「可行候选」。

        否则报告会同时出现「髂动脉腔内修复：可行候选」与 D4 的「EVAR 仅作桥接」，
        两条相反结论并列（规格 D7.1.R05：不按普通阈值独立决定）。
        """
        outcome = d7_3(_d7_state(cl={"infection": "原生感染明确"}))
        statuses = {c.procedure: c.status for c in outcome.candidates}
        assert statuses["髂动脉腔内修复"] is CandidateStatus.MDT
        assert statuses["髂分支器械（IBE/IBD）"] is CandidateStatus.MDT
        assert any("封顶在「需 MDT 评估」" in gap for gap in outcome.gaps)


class TestD7ModelVocabGuards:
    """规格写法 ≠ 模型 ``Literal`` —— 这些测试防「为对齐规格去改模型字面量」。

    改模型字面量不会让任何测试变红（判定层用 ``eq`` 比对，写错只是恒 False），
    所以必须显式钉死「模型实际收哪些值」。这条注释是给未来的自己看的：
    **不要**为了让规则看起来「对得上规格」而改 ``Literal``。
    """

    def test_iliac_site_is_a_list_of_sides_and_segments(self) -> None:
        """规格写复合值「左CIA」，模型是侧别+段两个元素。"""
        with pytest.raises(ValidationError):
            make_case(cl={"iliac_site": ["左CIA"]})
        case = make_case(cl={"iliac_site": ["左侧", "CIA"]})
        assert case.cl.iliac_site == ["左侧", "CIA"]

    def test_iliac_diam_is_a_mapping_not_a_scalar(self) -> None:
        with pytest.raises(ValidationError):
            make_case(cl={"iliac_diam": 28.0})
        case = make_case(cl={"iliac_diam": {"左侧CIA": 28.0}})
        assert case.cl.iliac_diam == {"左侧CIA": 28.0}

    def test_iia_has_no_suspected_value(self) -> None:
        """规格有「可疑/不可评价」，模型只有四态。"""
        with pytest.raises(ValidationError):
            make_case(n={"iia_left": "可疑"})

    def test_iliac_bilat_uses_buxiang_not_weizhi(self) -> None:
        with pytest.raises(ValidationError):
            make_case(n={"iliac_bilat": "未知"})
        assert make_case(n={"iliac_bilat": "不详"}).n.iliac_bilat == "不详"

    def test_aaa_reported_uses_queren_not_mingque(self) -> None:
        """规格写「明确」，模型写「确认」—— 照规格抄会让 D7.1.R02 恒 False。"""
        with pytest.raises(ValidationError):
            make_case(cl={"aaa_reported": "明确"})
        assert make_case(cl={"aaa_reported": "确认"}).cl.aaa_reported == "确认"


class TestD7DegradedInputs:
    """``D7.*`` 通道全空时不得崩，且结论必须落到「待补」而不是编一个默认值。"""

    def test_d71_survives_an_empty_channel(self) -> None:
        case = make_case(cl={"iliac_site": ["左侧", "CIA"], "diag_multi": None})
        outcome = d7_1(GuidelineState(case=case, route=route(case), node_inputs={}))
        assert outcome.next_node == "D7.2"
        assert any("D7.SYMPTOM_COMPRESSION" in gap for gap in outcome.gaps)

    def test_d72_with_an_empty_channel_reports_gaps(self) -> None:
        case = make_case(
            cl={"iliac_site": ["左侧", "CIA"], "iliac_diam": {"左侧CIA": 28.0}, "diag_multi": None}
        )
        outcome = d7_2(GuidelineState(case=case, route=route(case), node_inputs={}))
        assert outcome.verdicts["D7.2.R90"] is True
        assert any("D7.DIAM_METHOD" in gap for gap in outcome.gaps)

    def test_d73_with_an_empty_channel_gives_draft_candidates(self) -> None:
        case = make_case(
            cl={"iliac_site": ["左侧", "CIA"], "iliac_diam": {"左侧CIA": 42.0}, "diag_multi": None}
        )
        outcome = d7_3(GuidelineState(case=case, route=route(case), node_inputs={}))
        assert outcome.verdicts["D7.3.R04"] is True
        assert outcome.candidates
        assert any("D7.TARGET_SEAL" in gap for gap in outcome.gaps)

    def test_site_falls_back_to_the_channel(self) -> None:
        """模型字段为空时读通道 —— 评估时现场补录侧别/段的场景。"""
        case = make_case(
            cl={"iliac_site": None, "iliac_diam": {"左侧CIA": 28.0}, "diag_multi": None}
        )
        state = GuidelineState(
            case=case,
            route=route(case),
            node_inputs={"CL": {"CL.ILIAC_SITE": ["左侧", "CIA"]}},
        )
        assert d7_1(state).next_node == "D7.2"

    def test_diameters_fall_back_to_the_channel(self) -> None:
        case = make_case(cl={"iliac_site": ["左侧", "CIA"], "iliac_diam": None, "diag_multi": None})
        state = GuidelineState(
            case=case,
            route=route(case),
            node_inputs={"CL": {"CL.ILIAC_DIAM": {"左侧CIA": 42.0}}},
        )
        assert d7_2(state).next_node == "D7.3"

    def test_diameters_accept_a_single_number_in_the_channel(self) -> None:
        """通道里写单个数字时归一成「未标注段」——不静默丢掉。"""
        from src.tools.guideline_engine.nodes.d7_iliac import _iliac_diameter

        case = make_case(cl={"iliac_site": ["左侧", "CIA"], "iliac_diam": None})
        state = GuidelineState(
            case=case, route=route(case), node_inputs={"CL": {"CL.ILIAC_DIAM": 41.0}}
        )
        assert _iliac_diameter(state) == ("未标注段", 41.0)

    def test_empty_site_list_is_unresolved_not_negative(self) -> None:
        outcome = d7_1(_d7_state(cl={"iliac_site": []}))
        assert outcome.verdicts["D7.1.R90"] is True

    def test_empty_symptom_list_is_unresolved(self) -> None:
        outcome = d7_2(_d7_state(d7={"D7.SYMPTOM_COMPRESSION": []}))
        assert outcome.verdicts["D7.2.R90"] is True

    def test_aaa_context_confirms_the_comorbidity(self) -> None:
        """报告未述及时看 D.DX_CONTEXT：确认/疑似都算并存。"""
        case = make_case(
            cl={"iliac_site": ["左侧", "CIA"], "aaa_reported": "未述及", "diag_multi": None},
            d={"dx_context": "疑似"},
        )
        state = GuidelineState(case=case, route=route(case), node_inputs={})
        assert d7_1(state).verdicts["D7.1.R02"] is True

    def test_missing_iia_status_makes_pelvic_risk_unknown(self) -> None:
        outcome = d7_3(_d7_state(n={"iia_left": None, "iia_right": None}))
        assert outcome.verdicts["D7.3.R90"] is True
        assert outcome.candidates == ()

    def test_repair_history_without_a_type_is_named_in_the_gaps(self) -> None:
        outcome = d7_1(_d7_state(n={"prev_repair": "EVAR"}, d7={"D7.ANEURYSM_TYPE": None}))
        assert any("N.PREV_REPAIR 阳性" in gap for gap in outcome.gaps)

    def test_plan_without_a_diameter_asks_for_measurement(self) -> None:
        from src.tools.guideline_engine.nodes.d7_iliac import _surveillance_plan

        case = make_case(cl={"iliac_site": ["左侧", "CIA"], "iliac_diam": {}})
        plan = _surveillance_plan(GuidelineState(case=case, route=route(case)))
        assert "直径未测" in plan

    def test_plan_names_every_unrecorded_modifier(self) -> None:
        """三个修饰项都没记时，计划要把它们逐项点名，而不是只给间隔。"""
        outcome = d7_2(
            _d7_state(
                d7={
                    "D7.LIFE_EXPECTANCY": None,
                    "D7.SURVEILLANCE_ELIGIBILITY": None,
                    "D7.PREFERENCE": "未讨论",
                }
            )
        )
        assert outcome.terminal is not None
        assert "生命预期未评估" in outcome.terminal
        assert "监测适宜性未评价" in outcome.terminal
        assert "共同决策" in outcome.terminal

    def test_bilateral_disease_with_a_branch_plan_keeps_the_candidate_feasible(self) -> None:
        """有分支器械方案时 R03 不暂停，腔内仍是可行候选。

        这条同时守住「不要再加一层降级」：R01/R06 的判据里已经含盆腔灌注前提，
        再叠一次「双侧就降档」会把已经规划好保髂的病例误降。
        """
        outcome = d7_3(_d7_state(n={"iliac_bilat": "双侧"}, d7={"D7.PELVIC_PLAN": "分支器械"}))
        statuses = {c.procedure: c.status for c in outcome.candidates}
        assert outcome.verdicts["D7.3.R03"] is False
        assert statuses["髂动脉腔内修复"] is CandidateStatus.FEASIBLE

    def test_already_assessed_comorbidity_continues_the_d7_trunk(self) -> None:
        """D4 标签已先跑过时，D7.1.R05 不再重复进入 —— 继续 D7 主干。"""
        case = make_case(
            cl={
                "iliac_site": ["左侧", "CIA"],
                "iliac_diam": {"左侧CIA": 38.0},
                "infection": "原生感染明确",
                "diag_multi": None,
            }
        )
        report = run(ToolRequest(case=case, route_result=route(case), node_inputs={"D7": _D7_PLAN}))
        assert "D4.1" in report.path
        index = report.path.index("D7.1")
        assert report.path[index + 1] == "D7.2"

    def test_unassessed_comorbidity_is_named_as_skipped(self) -> None:
        """目标病种本轮没跑过 → 直接进入它，并把「D7.2/D7.3 未执行」写进缺口。

        端到端不可达：假性髂动脉瘤本身会让 ``G08`` 给出 D5 标签，D5 链在 D7 之前
        就跑完了。所以这条守的是节点层的出口选择，走单测。
        """
        outcome = d7_1(_d7_state(cl={"pseudo": "明确"}))
        assert outcome.next_node == "D5.1"
        assert any("D7.2 与 D7.3 本轮未执行" in gap for gap in outcome.gaps)

    def test_end_to_end_path_reaches_d73(self) -> None:
        """42 mm 的髂动脉瘤：D7.1 → D7.2 → D7.3，一路跑通。"""
        case = make_case(
            cl={
                "iliac_site": ["左侧", "CIA"],
                "iliac_diam": {"左侧CIA": 42.0},
                "aaa_reported": "否定",
                "diag_multi": None,
            }
        )
        report = run(ToolRequest(case=case, route_result=route(case), node_inputs={"D7": _D7_PLAN}))
        # 基线病例的 AAA 42 mm 会同时拿到 D1 标签（G08 允许多病种），
        # 所以只断言 D7 这三跳；D1 链跑不跑不影响本条要验的事。
        assert report.path[-3:] == ("D7.1", "D7.2", "D7.3")
        assert report.terminal is not None
        assert report.candidates


class TestD7InternalBranches:
    """``D7`` 内部判据的边角分支 —— 多数被更高优先级规则遮蔽，只能单测。"""

    def test_unrecorded_site_everywhere_is_unknown(self) -> None:
        from src.tools.guideline_engine.nodes.d7_iliac import _iliac_confirmed

        case = make_case(cl={"iliac_site": None, "diag_multi": None})
        state = GuidelineState(case=case, route=route(case), node_inputs={})
        assert _iliac_confirmed(state) is None

    def test_other_measurement_method_is_incomparable(self) -> None:
        from src.tools.guideline_engine.nodes.d7_iliac import _growth_comparable

        assert _growth_comparable(_d7_state(d7={"D7.DIAM_METHOD": "其他"})) is False

    def test_unrecorded_access_is_unknown(self) -> None:
        from src.tools.guideline_engine.nodes.d7_iliac import _access_ok

        assert _access_ok(_d7_state(n={"access_occlusion": None})) is None
        assert _access_ok(_d7_state(n={"access_occlusion": "未知"})) is None

    def test_a_named_side_alone_counts_as_a_preservation_plan(self) -> None:
        """``PELVIC_PLAN`` 还没定，但 ``IIA_PRESERVATION`` 已写明保留哪侧 → 有方案。"""
        from src.tools.guideline_engine.nodes.d7_iliac import _pelvic_protected

        state = _d7_state(d7={"D7.PELVIC_PLAN": "未确定", "D7.IIA_PRESERVATION": "左侧保留"})
        assert _pelvic_protected(state) is True

    def test_ifu_text_without_a_verdict_is_unknown(self) -> None:
        from src.tools.guideline_engine.nodes.d7_iliac import _ifu_ok

        assert _ifu_ok(_d7_state(d7={"D7.IBE_IFU": "CE；X 型号"})) is None

    def test_r03_yields_false_once_rupture_is_established(self) -> None:
        """破裂时 R03 必须为 False —— 槽位上它本来就不会被求值（R01 先命中）。"""
        from src.tools.guideline_engine.nodes.d7_iliac import _d71_r03

        assert _d71_r03(_d7_state(cl={"aaa_rupture": "明确"})) is False

    def test_r04_yields_false_when_the_method_is_recorded(self) -> None:
        from src.tools.guideline_engine.nodes.d7_iliac import _d72_r04

        assert _d72_r04(_d7_state()) is False

    def test_r90_blocks_on_a_half_recorded_site(self) -> None:
        from src.tools.guideline_engine.nodes.d7_iliac import _d72_r90

        assert _d72_r90(_d7_state(cl={"iliac_site": ["左侧"]})) is True

    def test_r90_blocks_on_an_unresolved_rupture(self) -> None:
        from src.tools.guideline_engine.nodes.d7_iliac import _d72_r90

        assert _d72_r90(_d7_state(cl={"aaa_rupture": "无法评价"})) is True

    def test_r91_catches_a_case_whose_etiology_cannot_be_judged(self) -> None:
        """病因整块判不了时 R03 给 None，由 R91 这个恒真兜底接住。"""
        outcome = d7_2(
            _d7_state(
                cl={"pseudo": None, "infection": None},
                n={"prev_repair": None},
                d7={"D7.ANEURYSM_TYPE": None},
            )
        )
        assert outcome.verdicts["D7.2.R03"] is None
        assert outcome.verdicts["D7.2.R91"] is True
        assert outcome.terminal is not None and "每 2 年超声随访" in outcome.terminal

    def test_r04_triggers_on_an_undecided_pelvic_plan(self) -> None:
        from src.tools.guideline_engine.nodes.d7_iliac import _d73_r04

        state = _d7_state(
            d7={"D7.TARGET_SEAL": "可用", "D7.PELVIC_PLAN": "未确定", "D7.IIA_PRESERVATION": "未定"}
        )
        assert _d73_r04(state) is True


class TestClosingAdvice:
    """``_closing_advice`` 的四条分支 —— 报告的最后一句话从哪来。"""

    def _state(self, *outcomes):
        from src.tools.guideline_engine.engine import _closing_advice
        from src.tools.guideline_engine.state import GuidelineState

        state = GuidelineState(case=make_case(), route=route(make_case()))
        state.visited.extend(outcomes)
        return _closing_advice(state)

    def test_no_visit_yields_nothing(self) -> None:
        assert self._state() is None

    def test_the_last_terminal_wins(self) -> None:
        from src.tools.guideline_engine.state import NodeOutcome

        first = NodeOutcome(node_id="D1.1", verdicts={}, entries=(), terminal="先前的终点")
        last = NodeOutcome(node_id="D1.2", verdicts={}, entries=(), terminal="末端终点")
        assert self._state(first, last) == "末端终点"

    def test_a_node_without_any_hit_yields_nothing(self) -> None:
        """既没终点也没命中规则 —— 宁可空着，也不编一句。"""
        from src.tools.guideline_engine.state import NodeOutcome

        last = NodeOutcome(node_id="D1.2", verdicts={"D1.2.R91": None}, entries=())
        assert self._state(last) is None

    def test_an_escalation_only_node_speaks_for_itself(self) -> None:
        from src.tools.guideline_engine.state import GuidelineEntry, NodeOutcome

        entry = GuidelineEntry(
            node_id="D1.2",
            rule_id="D1.2.R05",
            source_row="D1.2!9",
            statement="某条判据成立 → 去某处",
            verdict=True,
        )
        assert (
            self._state(NodeOutcome(node_id="D1.2", verdicts={"D1.2.R05": True}, entries=(entry,)))
            == "D1.2：某条判据成立 → 去某处"
        )


class TestSpecNextNodes:
    """``RuleSlot.next_nodes`` 必须逐字等于规格 M 列「下一节点/终点」那一段。

    为什么需要这条守门：``next_node`` 是单值，而规格那一列普遍多值。代码只记
    一个时，丢掉的去处既不报错也不失败 —— 只有拿规格原文对账才发现得了。
    本轮就是靠它查出 ``D4.3.R01`` 丢了 G03 与 D4.3、``D3.1.R02`` 多了 D3.2、
    ``D5.1.R05`` 多了 D5.2、``D6.3.R06`` 把 G03 写成 G05。

    规格文件不在本机时跳过（CI 无桌面文件）；本地跑则严格对账。
    """

    SPEC = SPEC_XLSX

    @staticmethod
    def _slots() -> tuple[RuleSlot, ...]:
        out: list[RuleSlot] = []
        for mod in (
            d1_asymptomatic,
            d2_symptomatic,
            d3_ruptured,
            d4_infected,
            d5_special,
            d6_postop,
            d7_iliac,
        ):
            for name in dir(mod):
                obj = getattr(mod, name)
                if name.endswith("_SLOTS") and isinstance(obj, tuple):
                    out.extend(obj)
        return tuple(out)

    @classmethod
    def _spec_values(cls) -> dict[str, tuple[str, str]]:
        """规则 → (值域元组, 动作原文)。值域按「、」「/」拆。"""
        if cls.SPEC is None:
            pytest.skip("规格 Excel 既不在仓库 docs/ 也不在桌面，跳过逐条对账")
        from openpyxl import load_workbook

        wb = load_workbook(cls.SPEC, read_only=True, data_only=True)
        out: dict[str, tuple[str, str]] = {}
        for sheet in wb.sheetnames:
            if not re.fullmatch(r"D\d\.\d", sheet):
                continue
            for row in wb[sheet].iter_rows(min_row=1, values_only=True):
                rid = str(row[0] or "").strip()
                if not re.fullmatch(r"D\d\.\d\.R\d+", rid):
                    continue
                raw = re.sub(r"\s+", " ", str(row[12] or ""))
                head, _, action = raw.partition("动作：")
                nodes = head.replace("下一节点/终点：", "").strip()
                values = tuple(n.strip() for n in re.split(r"[、/]", nodes) if n.strip())
                out[rid] = (values, action.strip())
        return out

    def test_next_nodes_match_the_spec_column(self) -> None:
        """只对**显式声明了值域**的槽位对账。

        为什么不全查：``terminal`` 型槽位的 ``exit_value`` 是给医生看的终点描述
        （「监测／风险管理（未达修复阈值）」），``next`` 型里也有一部分如此；
        规格 M 列那一格写的却是节点 ID 或「终点动作/人工处理」。两者不是同一层
        语言，机械对账会得到一堆假差异。真正会丢信息的是 ``escalate`` 型 ——
        它的值域全是节点 ID，代码只记一个就会静默丢去处，本测试专查这一段。
        """
        spec = self._spec_values()
        wrong: list[str] = []
        for slot in self._slots():
            if not slot.next_nodes:  # 未声明 = 规格那格只有一个值，由 exit_value 承担
                continue
            expected = spec.get(slot.rule_id)
            if expected is None:
                wrong.append(f"{slot.rule_id}: 规格里找不到这一行")
                continue
            if set(slot.next_nodes) != set(expected[0]):
                wrong.append(f"{slot.rule_id}: 代码 {slot.next_nodes}，规格 {expected[0]}")
        assert not wrong, "\n".join(wrong)

    def test_escalate_slots_record_the_full_domain(self) -> None:
        """``escalate`` 型槽位必须填 ``next_nodes`` —— 不填等于「规格只给了一个值」。"""
        missing = [
            s.rule_id for s in self._slots() if s.exit_kind == "escalate" and not s.next_nodes
        ]
        assert not missing, f"缺少 next_nodes：{missing}"

    def test_multi_node_domains_are_declared(self) -> None:
        """规格值域里出现**多于一个**病种节点时，必须声明 ``next_nodes``。

        单值域不强制：``exit_value`` 就是那个值，缺省时 ``next_nodes`` 也回落成它，
        没有信息可丢。纯终点描述（「终点动作/人工处理」）解析不出节点 ID，豁免。

        为什么要这条：``next_nodes`` 当初只加在 ``escalate`` 型上，``next`` /
        ``terminal`` 型的多值域**一直没登记**，扫描出来是 14 条（不是拍脑袋的 7 条）。
        后果是报告里只剩一个去处，读者看不出规格还允许别的分支。

        注意它只查「有没有登记」，不要求把每个分支都走一遍：``EITHER`` 的其余值
        是**同一规则在不同证据下的替代去处**，本病例没命中就不该走。
        """
        spec = self._spec_values()
        node_id = re.compile(r"^D\d\.\d$")
        missing: list[str] = []
        for slot in self._slots():
            if slot.exit_kind not in ("next", "terminal") or slot.next_nodes:
                continue
            expected = spec.get(slot.rule_id)
            if expected is None:
                continue
            nodes = tuple(v for v in expected[0] if node_id.match(v))
            if len(nodes) > 1:
                missing.append(f"{slot.rule_id}: 规格值域 {nodes}，代码未声明 next_nodes")
        assert not missing, "\n".join(missing)

    def test_parallel_slots_are_not_terminal(self) -> None:
        """``PARALLEL`` 声明的另一条分支必须真能走到。

        ``terminal`` 会让 ``_walk`` 停在本节点，此时写「并行 D4.1」只是在报告里
        声明了一句系统不会执行的话——``D4.1.R05`` 修前就是这样：报告写着
        「D4.2 与 D5.3 并行评估」，实际只停在 D4.1。

        ``FOLLOW_UP`` 不受此约束：它的语义本来就是「先做 A，B 是之后的事」，
        停在 A 的终点、把 B 留给下一轮是正确行为。
        """
        bad = [
            s.rule_id
            for s in self._slots()
            if s.link is Link.PARALLEL and s.exit_kind == "terminal"
        ]
        assert not bad, f"并行出口却是 terminal：{bad}"

    def test_link_kinds_are_used_consistently(self) -> None:
        """关系与值域的形状必须自洽（措辞不规整，故只查结构不查文义）。

        · ``EITHER`` 允许单值：规格写「A；急症→B」，命中时条件已落定，只剩一个去处；
        · ``PARALLEL`` / ``FOLLOW_UP`` 至少要两个值 —— 一个值谈不上「并行」或「后续」。
        """
        bad = [
            f"{s.rule_id}: {s.link.value} 却有 {len(s.next_nodes)} 个值"
            for s in self._slots()
            if s.next_nodes and s.link in (Link.PARALLEL, Link.FOLLOW_UP) and len(s.next_nodes) < 2
        ]
        assert not bad, "\n".join(bad)


class TestSpecRuleRows:
    """``RULE_ROWS`` 的源行必须等于该 rule_id 在 V3.6.2 工作簿里的**实际行**。

    为什么需要它：``source_row`` 会随 ``GuidelineEntry`` 一起进报告与审计，
    是「这条结论出自规格第几行」的唯一凭据。规格插行后行号会静默漂移 ——
    D3.2 / D3.3 就漂了 9 条，而 172 条判据测试照样全绿（它们只断言 rule_id）。
    """

    SPEC = SPEC_XLSX

    def test_source_rows_match_the_workbook(self) -> None:
        if self.SPEC is None:
            pytest.skip("规格 Excel 既不在仓库 docs/ 也不在桌面，跳过逐条对账")
        from openpyxl import load_workbook

        wb = load_workbook(self.SPEC, read_only=True, data_only=True)
        actual: dict[str, str] = {}
        for sheet in SPEC_NODE_SHEETS:
            for i, row in enumerate(wb[sheet].iter_rows(min_row=1, values_only=True), start=1):
                rid = str(row[0] or "").strip()
                if rid in RULE_ROWS:
                    actual[rid] = f"{sheet}!{i}"

        wrong = [
            f"{rid}: 代码 {row}，规格 {actual.get(rid) or '规格里没有这一行'}"
            for rid, row in RULE_ROWS.items()
            if actual.get(rid) != row
        ]
        assert not wrong, "\n".join(wrong)


class TestEngineWideContracts:
    """跨节点的结构契约 —— 单条规则都对、组合起来却断链的那类缺陷。"""

    def test_report_terminal_names_the_last_node_that_escalated(self) -> None:
        """末端节点只给回流信号时，终点必须描述**它**，不是上游节点的结论。

        ``D4.1.R91``（无感染证据 → 回 G08）是 ``exit_kind="escalate"`` 且无
        ``also_next``：它没有 ``terminal``。修复前报告会退回去取上一个带终点的
        节点，于是「回 G08」的病例，末尾写的是上游那条分支的终点——描述另一条
        分支，读者会当成结论。
        """
        case = make_case(
            cl={"aaa_reported": "否定", "diag_multi": None, "infection": "无证据"},
            n={"infection_ctx": "高度怀疑"},
        )
        report = run(ToolRequest(case=case, route_result=route(case)))
        assert report.path[-1] == "D4.1"
        assert report.terminal is not None
        assert report.terminal.startswith("D4.1：")
        assert "下一节点：D4.2、D4.3、G08" in report.terminal
        # 措辞只陈述规格 M 列写了什么，不声称系统执行了跨层跳转：
        # ``_walk`` 只在病种节点之间走，G03/G05/G06/G08 都不在其中。
        assert "已回流" not in report.terminal
        assert "应回流" not in report.terminal

    def test_the_parallel_branches_both_really_run(self) -> None:
        """报告写「并行评估」，两个节点就都得真的执行过。

        ``D4.1.R05`` 修前是 ``terminal`` + 一句「D4.2 与 D5.3 并行评估」——
        文字写了，流程没动。``engine._walk`` 现在按 ``link is PARALLEL`` 展开。
        """
        case = make_case(
            cl={
                "aaa_reported": "否定",
                "diag_multi": None,
                "infection": "未评价",
                "fistula": "否",
            },
            n={"inflammatory_ctx": "影像高度提示", "infection_ctx": "高度怀疑"},
        )
        report = run(ToolRequest(case=case, route_result=route(case)))
        assert "D4.1" in report.path
        assert "D4.2" in report.path, "并行的一支没执行"
        assert "D5.3" in report.path, "并行的另一支没执行"

    def test_infection_verdicts_are_monotonic_across_nodes(self) -> None:
        """公共判据为真时，D4/D6/D7 三处都不得说「没有感染」。

        三者问的是同一件事，只是各自在公共判据上多收或少收一档（D4 加影像组合
        证据、D6 加「仅非特异征象」、D7 只用公共判据）。所以**不允许**出现
        「公共判据说有、某个病种说没有」——那正是修复前 D7 的错。
        """
        from src.tools.guideline_engine.nodes.common import infection_typed
        from src.tools.guideline_engine.nodes.d4_infected import _infection_signal
        from src.tools.guideline_engine.nodes.d6_postop import _infection_suspected
        from src.tools.guideline_engine.nodes.d7_iliac import _infection_present

        samples = [
            {"cl": {"infection": "无证据"}, "n": {"infection_ctx": "高度怀疑"}},
            {"cl": {"infection": "无证据"}, "n": {"infection_ctx": "医师明确"}},
            {"cl": {"infection": "原生感染疑似"}, "n": {"infection_ctx": "无相关征象"}},
            {"cl": {"infection": "移植物感染疑似"}, "n": {"infection_ctx": "未知"}},
            {"cl": {"infection": "无证据"}, "n": {"infection_ctx": "无相关征象"}},
        ]
        for overrides in samples:
            case = make_case(**overrides)
            state = GuidelineState(case=case, route=route(case))
            typed = infection_typed(state)
            if typed is True:
                assert _infection_signal(state) is True, overrides
                assert _infection_suspected(state) is True, overrides
                assert _infection_present(state) is True, overrides

    def test_every_next_exit_names_a_real_node(self) -> None:
        """``exit_kind="next"`` 的 ``exit_value`` 必须是 ``NODES`` 里的键。

        ``GuidelineEntry.next_node`` 直接取自这里，``engine._walk`` 拿它查
        ``NODES``；查不到就报「该节点尚未实现」。所以写病种名（``"D4"``）而
        不写节点键（``"D4.1"``），报告里就会出现一条**根本不存在的缺口**。
        D7.1.R05 曾犯过这个错，靠运行期 ``replace`` 兜住 —— 声明与行为不一致，
        改一处兜底就漏。
        """
        import importlib
        import pkgutil

        import src.tools.guideline_engine.nodes as nodes_pkg

        bad: list[str] = []
        for info in pkgutil.iter_modules(nodes_pkg.__path__):
            module = importlib.import_module(f"{nodes_pkg.__name__}.{info.name}")
            for attr, value in vars(module).items():
                if not attr.endswith("_SLOTS"):
                    continue
                for slot in value:
                    if slot.exit_kind == "next" and slot.exit_value not in NODES:
                        bad.append(f"{slot.rule_id} -> {slot.exit_value}")
        assert bad == []

    def test_every_case_field_read_is_declared_as_consumed(self) -> None:
        """代码里读的 ``case.<域>.<字段>`` 必须出现在 ``SPEC.consumes``。

        ``consumes`` 是输入契约：上游改字段名时靠它判断影响面。漏声明的字段
        一旦改名，工具A 会静默读到 ``None``，规则全部退化成「判不了」而不报错。
        （反向不检查：``case.inf.*`` 与 ``iia_left/right`` 是经 ``getattr`` 动态
        读的，正则抓不到 —— 那种情况由字段存在性断言覆盖。）
        """
        import pathlib
        import re

        from src.tools.guideline_engine.spec import SPEC

        root = pathlib.Path(__file__).resolve().parents[1] / "src/tools/guideline_engine/nodes"
        pattern = re.compile(r"state\.case\.([a-z]+)\.([a-z_0-9]+)")
        used = {
            f"case.{m.group(1)}.{m.group(2)}"
            for path in root.glob("*.py")
            for m in pattern.finditer(path.read_text(encoding="utf-8"))
        }
        assert sorted(used - set(SPEC.consumes)) == []
