"""术前路由端到端基线 —— V3.6.2 的**每条出口与每个优先级桶**都走到一次。

本文件关心「**走得对不对**」：给定一份病例，``route()`` 是否把它送到正确的节点、
命中的是不是该命中的那条规则、病种标签对不对。规则内部的判据细节由
``test_router_branches.py`` 逐条守着；这里只跑编排。

三处容易搞错、因此每条都单独断言的点：

- **同桶按 rule_id 升序**：短颈（R01）与肾旁（R03）同时满足时，命中 R01 而不是 R03。
- **P01 压过 P02**：低血压（P01/R20）与 Hb 快速下降（P02/R17）同时成立时，进急诊。
- **G07 是排他分支**：资料不足时必须落到「资料核实」，绝不能拿 G07 当兜底。

运行：``uv run pytest tests/test_preop.py -v``
"""

from __future__ import annotations

import pytest
from conftest import make_case

from src.models.preop.case import VascCase, derive
from src.models.preop.recommendation import RouteMode
from src.rules.router import FALLBACK_VERIFY, MANUAL_URGENCY, RouteKind, route
from src.rules.version import RULESET_VERSION


def _route(**overrides: object):
    return route(make_case(**overrides))  # type: ignore[arg-type]


# ===========================================================================
# 一、急诊（G03 → G05）
# ===========================================================================
class TestEmergency:
    def test_direct_extravasation(self) -> None:
        result = _route(
            c={
                "extrav": "明确",
                "extrav_contig": "明确连续",
                "source_kind": "正式报告",
            }
        )
        assert (result.mode, result.rule_id) == (RouteMode.EMERGENCY, "G03.R01")

    def test_aneurysm_with_hematoma(self) -> None:
        result = _route(c={"hematoma": "明确", "hematoma_adj": "明确相邻"})
        assert (result.mode, result.rule_id) == (RouteMode.EMERGENCY, "G03.R02")

    def test_reported_rupture(self) -> None:
        result = _route(c={"rupture_report": "明确破裂"})
        assert (result.mode, result.rule_id) == (RouteMode.EMERGENCY, "G03.R03")

    def test_suspected_rupture_with_shock(self) -> None:
        result = _route(d={"dx_context": "疑似"}, v={"sbp": 70.0})
        assert (result.mode, result.rule_id) == (RouteMode.EMERGENCY, "G03.R04")

    def test_attributed_acute_pain(self) -> None:
        result = _route(
            s={"pain_current": "当前新发", "pain_site": ["背"], "pain_attrib": "明确相关"}
        )
        assert (result.mode, result.rule_id) == (RouteMode.EMERGENCY, "G03.R05")

    def test_unattributed_sudden_pain(self) -> None:
        result = _route(
            s={
                "pain_current": "当前新发",
                "pain_site": ["腰"],
                "pain_onset": "突然",
                "pain_attrib": "未判断",
            }
        )
        assert (result.mode, result.rule_id) == (RouteMode.EMERGENCY, "G03.R06")

    def test_rupture_not_excluded(self) -> None:
        result = _route(c={"rupture_report": "疑似破裂"})
        assert (result.mode, result.rule_id) == (RouteMode.EMERGENCY, "G03.R07")

    def test_graft_with_gi_bleed(self) -> None:
        result = _route(d={"surg": ["EVAR"]}, s={"gi_bleed": ["黑便"]})
        assert (result.mode, result.rule_id) == (RouteMode.EMERGENCY, "G03.R08")

    def test_acute_limb_ischemia(self) -> None:
        result = _route(s={"limb": ["疼痛", "苍白"], "pulse_deficit": "缺失"})
        assert (result.mode, result.rule_id) == (RouteMode.EMERGENCY, "G03.R09")

    def test_news2_critical(self) -> None:
        result = _route(v={"spo2": 91.0, "rr": 22.0, "sbp": 105.0, "hr": 95.0})
        assert (result.mode, result.rule_id) == (RouteMode.EMERGENCY, "G03.R10")

    def test_severe_hypotension_without_known_aaa(self) -> None:
        """R20 不依赖是否已知 AAA —— 血压本身就是急症指征。"""
        result = _route(v={"sbp": 70.0})
        assert (result.mode, result.rule_id) == (RouteMode.EMERGENCY, "G03.R20")

    def test_emergency_level_is_recorded(self) -> None:
        assert _route(v={"sbp": 70.0}).emergency_level == "明确危险"

    def test_emergency_has_no_class_label(self) -> None:
        """急诊是排他前置：还没进分类就出去了，不该有 D 标签。"""
        assert _route(v={"sbp": 70.0}).class_output == []


# ===========================================================================
# 二、人工复核（G03 → 人工复核）
# ===========================================================================
class TestReview:
    def test_no_enhanced_cta_with_clues_goes_to_review(self) -> None:
        """``G03.R12`` 影像不可靠 → **人工复核**，不是急诊模式。

        V3.6.2 把它的出口从 ``G05`` 改成 ``REVIEW``（``G03!20``：「紧急复核，
        不能降为常规」）。修前它进急诊模式，D3.2 会拿那份「增强不足」的 CTA
        判出「解剖符合 EVAR」——用系统自己认为不可靠的影像给出术式方向。
        这条测试守的是「不给候选」，所以断言 ``mode is None`` 与零工具产物。
        """
        result = _route(s={"syncope": "肯定"}, q={"cta_enhance": "不足"})
        assert (result.kind, result.rule_id) == (RouteKind.REVIEW, "G03.R12")
        assert result.mode is None
        # 复核出口不预置病种标签：没有可靠影像就没有可进的病种流程。
        assert "D3" not in result.class_output

    def test_hb_drop_comparable(self) -> None:
        result = _route(
            lab={"hb": 85.0, "hb_prev": 120.0, "hb_comparable": True, "hb_confound": False}
        )
        assert (result.kind, result.rule_id) == (RouteKind.REVIEW, "G03.R17")

    def test_hb_drop_uncertain(self) -> None:
        """可比性未明只登记「差异待查」，不主张快速失血——但绝不漏复核。"""
        result = _route(lab={"hb": 85.0, "hb_prev": 120.0, "hb_comparable": None})
        assert (result.kind, result.rule_id) == (RouteKind.REVIEW, "G03.R19")

    def test_hb_drop_with_confounder(self) -> None:
        result = _route(lab={"hb": 85.0, "hb_prev": 120.0, "hb_confound": True})
        assert (result.kind, result.rule_id) == (RouteKind.REVIEW, "G03.R19")

    def test_hb_drop_below_threshold_is_not_reviewed(self) -> None:
        result = _route(lab={"hb": 100.0, "hb_prev": 120.0})
        assert result.kind is RouteKind.MODE

    def test_news2_urgent_review(self) -> None:
        result = _route(v={"spo2": 92.0, "rr": 22.0, "hr": 95.0})
        assert (result.kind, result.rule_id) == (RouteKind.REVIEW, "G03.R11")

    def test_single_parameter_three_scores_review(self) -> None:
        """总分只有 3 也进复核：单项 3 分本身就是危险信号。"""
        result = _route(v={"spo2": 90.0})
        assert (result.kind, result.rule_id) == (RouteKind.REVIEW, "G03.R11")

    def test_lactate_above_upper_limit(self) -> None:
        result = _route(lab={"lact": 3.0, "lact_uln": 2.0})
        assert (result.kind, result.rule_id) == (RouteKind.REVIEW, "G03.R21")

    def test_severe_pain_without_known_aaa(self) -> None:
        result = _route(
            d={"dx_context": "无相关记录"},
            s={
                "pain_current": "当前新发",
                "pain_site": ["背"],
                "pain_onset": "突然",
                "pain_severity": 9,
            },
        )
        assert (result.kind, result.rule_id) == (RouteKind.REVIEW, "G03.R18")

    def test_p01_beats_p02(self) -> None:
        """低血压（P01）压过 Hb 快速下降（P02）：先救命。"""
        result = _route(v={"sbp": 70.0}, lab={"hb": 85.0, "hb_prev": 120.0})
        assert result.rule_id == "G03.R20"


# ===========================================================================
# 三、资料核实（G03 / G04 / G09 → 资料核实）
# ===========================================================================
class TestVerify:
    def test_empty_case(self) -> None:
        result = route(VascCase(case_id="EMPTY"))
        assert (result.kind, result.rule_id) == (RouteKind.VERIFY, FALLBACK_VERIFY)

    def test_identity_mismatch(self) -> None:
        result = _route(q={"idmatch": "不一致"})
        assert (result.kind, result.rule_id) == (RouteKind.VERIFY, "G03.R13")

    def test_value_conflict(self) -> None:
        result = _route(q={"conflict": ["数值"]})
        assert (result.kind, result.rule_id) == (RouteKind.VERIFY, "G03.R13")

    def test_report_vs_algorithm_conflict(self) -> None:
        result = _route(q={"report_vs_algo": "冲突"})
        assert (result.kind, result.rule_id) == (RouteKind.VERIFY, "G03.R13")

    def test_screen_not_done_this_visit(self) -> None:
        result = _route(q={"current_assess": "未评估"})
        assert (result.kind, result.rule_id) == (RouteKind.VERIFY, "G03.R14")

    def test_missing_neck_length(self) -> None:
        """核心解剖缺失不得判低复杂 → G04.R13。"""
        result = _route(n={"neck_len": None})
        assert (result.kind, result.rule_id) == (RouteKind.VERIFY, "G04.R13")

    def test_cta_missing(self) -> None:
        result = _route(n={"cta_status": "缺失"})
        assert (result.kind, result.rule_id) == (RouteKind.VERIFY, "G04.R13")

    def test_unverified_ifu_does_not_release_to_guideline(self) -> None:
        """IFU 未核验 ≠ 符合 IFU：阻断型规则判不了就不许放行。"""
        result = _route(n={"seal_ifu": "未核验"})
        assert result.kind is RouteKind.VERIFY

    def test_verify_carries_gaps(self) -> None:
        assert route(VascCase(case_id="EMPTY")).evidence_gaps


# ===========================================================================
# 四、复杂（G04 → G06）
# ===========================================================================
class TestComplex:
    def test_short_neck(self) -> None:
        result = _route(n={"neck_len": 8.0})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R01")

    def test_juxtarenal(self) -> None:
        result = _route(n={"neck_len": 2.0})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R02")

    def test_pararenal(self) -> None:
        result = _route(n={"renal_l": "明确受累"})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R03")

    def test_visceral_pararenal(self) -> None:
        result = _route(n={"renal_l": "明确受累", "sma": "明确受累"})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R04")

    def test_taaa_iv_gets_extension_label(self) -> None:
        result = _route(
            n={"renal_l": "明确受累", "sma": "明确受累", "ca": "明确受累", "thoracic": "否"}
        )
        assert result.mode is RouteMode.COMPLEX
        assert "Ⅳ型TAAA扩展" in result.class_output

    def test_thoracic_extension_is_complex(self) -> None:
        result = _route(n={"thoracic": "是"})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R06")

    def test_infection(self) -> None:
        result = _route(n={"infection_ctx": "医师明确"})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R07")

    def test_fistula(self) -> None:
        result = _route(n={"fistula_ctx": "疑似"})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R07")

    def test_ifu_mismatch(self) -> None:
        result = _route(n={"seal_ifu": "不符合"})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R08")

    def test_iliac_limitation(self) -> None:
        result = _route(n={"iliac_bilat": "双侧", "iia_right": "严重狭窄"})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R09")

    def test_access_occlusion(self) -> None:
        result = _route(n={"access_occlusion": "双侧闭塞"})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R10")

    def test_type_one_endoleak_after_repair(self) -> None:
        result = _route(n={"prev_repair": "EVAR", "endoleak_type": "I"})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R11")

    def test_type_two_endoleak_without_growth_is_not_complex(self) -> None:
        result = _route(n={"prev_repair": "EVAR", "endoleak_type": "II"})
        assert result.mode is not RouteMode.COMPLEX

    def test_sac_growth(self) -> None:
        result = _route(n={"prev_repair": "EVAR", "sac_growth": 12.0})
        assert result.rule_id == "G04.R11"

    def test_low_egfr(self) -> None:
        result = _route(n={"egfr": 25.0})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R12")

    def test_dialysis(self) -> None:
        result = _route(n={"dialysis": "是"})
        assert result.mode is RouteMode.COMPLEX

    def test_nyha_three(self) -> None:
        result = _route(n={"heart_class": "III"})
        assert result.mode is RouteMode.COMPLEX

    def test_genetic_confirmed(self) -> None:
        result = _route(n={"genetic_ctx": "确诊"})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R15")

    def test_inflammatory_aaa(self) -> None:
        result = _route(n={"inflammatory_ctx": "影像高度提示"})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R16")
        assert "D5" in result.class_output

    def test_early_onset_genetic_suspicion(self) -> None:
        result = _route(gen={"aaa_age_onset": 52})
        assert (result.mode, result.rule_id) == (RouteMode.COMPLEX, "G04.R17")

    def test_inflammation_with_infection_is_dual_labelled(self) -> None:
        """炎症与感染并存时**双标签共存**，不得互相覆盖（G04.R18 / G08.R10）。"""
        result = _route(cl={"infection": "原生感染明确"}, n={"inflammatory_ctx": "影像高度提示"})
        assert result.mode is RouteMode.COMPLEX
        assert {"D4", "D5"} <= set(result.class_output)

    def test_inflammation_with_unknown_infection_blocks_release(self) -> None:
        """``G04.R18`` 标 P0-A：感染是否并存**未知**时不许放行到 G07。

        修前它在「加分型」组，而该组的规格约定是「UNKNOWN 不算命中、也不阻断」。
        于是一个炎症性 AAA 病人，只要感染状态没写，就会被当成低复杂择期处理。
        V3.6.2 的缺失列明写「关键输入缺失/未知/冲突时转 VERIFY/REVIEW，不按阴性
        处理」——它属于阻断型，不属于加分型。
        """
        blocked = _route(n={"inflammatory_ctx": "可疑", "infection_ctx": None})
        assert blocked.mode is None
        assert blocked.kind is RouteKind.VERIFY

        # 对照：感染明确为无 → 不阻断，可进指南模式。
        released = _route(n={"inflammatory_ctx": "可疑", "infection_ctx": "无相关征象"})
        assert released.mode is RouteMode.GUIDELINE

    def test_same_bucket_takes_lowest_rule_id(self) -> None:
        """G03 的 P01 桶：造影剂外溢（R01）与低血压（R20）同时成立 → 取 R01。

        G04 的阻断型规则彼此互斥（R01 要求肾动脉未受累，R03 要求受累），
        天然不会同时命中，所以「同桶取最小 ID」只能在 G03 上验证。
        """
        result = _route(
            v={"sbp": 70.0},
            c={"extrav": "明确", "extrav_contig": "明确连续", "source_kind": "正式报告"},
        )
        assert result.rule_id == "G03.R01"


# ===========================================================================
# 五、指南（G04.R14 → G07）
# ===========================================================================
class TestGuideline:
    def test_clean_case(self) -> None:
        result = _route()
        assert (result.mode, result.rule_id) == (RouteMode.GUIDELINE, "G04.R14")

    def test_large_but_anatomically_simple_aneurysm(self) -> None:
        result = _route(cl={"aaa_diam": 62.0})
        assert result.mode is RouteMode.GUIDELINE

    def test_no_gaps_and_high_confidence(self) -> None:
        result = _route()
        assert result.evidence_gaps == []
        assert result.confidence_level == "高"

    def test_gets_d1_label(self) -> None:
        assert _route().class_output == ["D1"]

    def test_untested_genetics_does_not_block_release(self) -> None:
        """规格明文：仅因未做遗传检测/CRP，不强制所有常规病例升复杂。"""
        result = _route(gen={"variant_class": "未检测"}, n={"genetic_ctx": None})
        assert result.mode is RouteMode.GUIDELINE


# ===========================================================================
# 六、分类结果
# ===========================================================================
class TestClassification:
    def test_symptomatic_aneurysm_gets_d2(self) -> None:
        result = _route(cl={"symptom_aaa": "医师确认相关"})
        assert "D2" in result.class_output

    def test_infected_gets_d4(self) -> None:
        result = _route(cl={"infection": "移植物感染明确"})
        assert "D4" in result.class_output

    def test_pau_gets_d5(self) -> None:
        result = _route(cl={"pau": "明确"})
        assert "D5" in result.class_output

    def test_postop_gets_d6(self) -> None:
        result = _route(n={"prev_repair": "EVAR"}, cl={"prev_evor": "EVAR", "post_reason": "内漏"})
        assert "D6" in result.class_output

    def test_iliac_aneurysm_gets_d7(self) -> None:
        result = _route(cl={"iliac_site": ["左侧", "CIA"]})
        assert "D7" in result.class_output

    def test_conflicting_prefilled_labels_go_to_review(self) -> None:
        result = _route(cl={"diag_multi": ["D1", "D3"]})
        assert (result.kind, result.rule_id) == (RouteKind.REVIEW, "G09.R03")

    def test_classifiable_is_positive_for_clean_case(self) -> None:
        assert _route().k_classifiable == "确定"

    def test_identity_conflict_is_reported(self) -> None:
        assert _route(case_id="X", q={"idmatch": "不一致"}).k_identity_conflict == "是"


# ===========================================================================
# 七、人工越级
# ===========================================================================
class TestManualOverride:
    def test_urgency_flag_jumps_to_emergency(self) -> None:
        result = _route(urgency_flag=True)
        assert (result.mode, result.rule_id) == (RouteMode.EMERGENCY, MANUAL_URGENCY)

    def test_urgency_flag_beats_clean_case(self) -> None:
        """哪怕其他一切都正常，人工置位也必须生效。"""
        assert _route(urgency_flag=True).next_node == "G05"


# ===========================================================================
# 八、轨迹与可复现
# ===========================================================================
class TestAuditAndDeterminism:
    def test_trace_contains_the_hitting_rule(self) -> None:
        """命中规则必须留在轨迹里 —— 它是「为什么进这个模式」的唯一凭据。"""
        result = _route(n={"neck_len": 8.0})
        assert result.rule_id in {entry.rule_id for entry in result.trace}

    def test_trace_ends_with_g09_exit_for_mode_results(self) -> None:
        """进模式后还要跑 G08/G09，所以轨迹末尾是收尾规则，不是命中规则。"""
        result = _route(n={"neck_len": 8.0})
        assert result.trace[-1].rule_id == "G09.R04"

    def test_emergency_trace_ends_at_the_hit(self) -> None:
        """急诊不进 G08/G09（排他前置），轨迹末尾就是命中规则。"""
        result = _route(v={"sbp": 70.0})
        assert result.trace[-1].rule_id == result.rule_id

    def test_guideline_trace_covers_all_g03_buckets(self) -> None:
        """进指南的结论由否决链支撑：必须能看到 G03 三桶都跑过。"""
        ids = {entry.rule_id for entry in _route().trace}
        assert ids & {"G03.R01", "G03.R02"}
        assert "G03.R15" in ids

    def test_ruleset_version_is_recorded(self) -> None:
        assert _route().ruleset_version == RULESET_VERSION

    def test_routing_is_deterministic(self) -> None:
        case = make_case(n={"neck_len": 8.0})
        first = route(case)
        second = route(case)
        assert (first.rule_id, first.next_node, first.trace) == (
            second.rule_id,
            second.next_node,
            second.trace,
        )

    def test_route_does_not_mutate_case(self) -> None:
        case = make_case()
        before = case.model_dump()
        route(case)
        assert case.model_dump() == before


# ===========================================================================
# 九、回填
# ===========================================================================
class TestDerive:
    def test_derive_fills_next_node(self) -> None:
        case = derive(make_case())
        assert case.r.next == "G07"

    def test_derive_fills_emergency_level(self) -> None:
        assert derive(make_case(v={"sbp": 70.0})).r.aortic_emergency == "明确危险"

    def test_derive_fills_class_output(self) -> None:
        assert derive(make_case()).k.class_output == ["D1"]

    def test_derive_fills_check_domain(self) -> None:
        case = derive(make_case(q={"idmatch": "不一致"}))
        assert case.k.identity_conflict == "是"

    def test_derive_returns_same_instance(self) -> None:
        case = make_case()
        assert derive(case) is case


# ===========================================================================
# 十、五出口全覆盖（防止某个出口写了但走不到）
# ===========================================================================
class TestAllExitsReachable:
    @pytest.mark.parametrize(
        ("label", "overrides", "expected"),
        [
            ("急诊", {"v": {"sbp": 70.0}}, "G05"),
            ("复杂", {"n": {"neck_len": 8.0}}, "G06"),
            ("指南", {}, "G07"),
            ("复核", {"lab": {"hb": 85.0, "hb_prev": 120.0}}, "人工复核"),
            ("核实", {"n": {"neck_len": None}}, "资料核实"),
        ],
    )
    def test_exit_reachable(self, label: str, overrides: dict, expected: str) -> None:
        assert _route(**overrides).next_node == expected, label
