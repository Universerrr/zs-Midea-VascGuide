"""路由分支测试 —— V3.6.2 **66 条判定规则**的逐条覆盖 + 三值语义守卫。

分三层（刻意分开：失败时含义不同）：

1. **注册表守卫**：``RULES`` 与 V3.6.2 是否对得上（条数 / 编号 / 源行 / 出口）。
   这层失败 = 规格漂移，不是逻辑 bug。
2. **逐条命中**（``HITS``）：每条规则至少一个「明确命中」用例。逐条直接调规则函数、
   不经 ``route()`` 编排 —— 否则前面规则先命中就再也走不到后面，覆盖是假的。
3. **三值语义**：``UNKNOWN`` 不短路、不确定值只产生 UNKNOWN、未提及 ≠ 阴性。

依赖运行时状态的 6 条（``G04.R14``、``G08.R09``、``G09.R03``、``G09.R04``、
``G09.R07``、``G09.R09``）单独测：它们读的是编排层写进 ``_Ctx`` 的中间结果，
不是病例字段，无法用「一条用例一个 dict」表达。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import SPEC_XLSX, make_case

from src.models.preop.case import VascCase
from src.models.preop.recommendation import RouteMode
from src.rules.router import (
    G03_P01,
    G03_P02,
    G03_P03,
    G04_BLOCKING,
    G04_EXTRA,
    G08_RULES,
    GUARD_D3_LABEL,
    LABEL_BY_RULE,
    OUTCOME_PRIORITY,
    PENDING_RULES,
    RULE_SOURCE,
    RULES,
    RouteKind,
    _and,
    _Ctx,
    _eq,
    _not,
    _or,
    _read_path,
    route,
)

VALID_EXITS = {"G05", "G06", "G07", "G08", "人工复核", "资料核实", "无目标病变"}
"""规则行 M 列声明过的出口。``G08`` 只被 ``G04.R20`` 用（它属于分类阶段）；
``无目标病变`` 只被 ``G09.R07`` 用（V3.6.2 新增的终末出口）。"""


def verdict_of(rule_id: str, **overrides: object) -> bool | None:
    """直接调规则函数求值（跳过编排，避免前置规则抢先命中）。"""
    return RULES[rule_id].func(_Ctx(make_case(**overrides)))  # type: ignore[arg-type]


# ===========================================================================
# 一、注册表守卫
# ===========================================================================
class TestRuleRegistry:
    def test_rule_count_is_66(self) -> None:
        """V3.6.2 判定层 65 条 + 工程补齐 1 条（``G04.R21``）= 66。

        55 → 66 的变化全部来自规格升级：``G04.R19/R20``、``G08.R12–R15``、
        ``G09.R05–R09`` 共 11 条，由 ``PENDING_RULES`` 清空来证明已补齐。
        """
        assert len(RULES) == 66

    @pytest.mark.parametrize(
        ("domain", "expected"), [("G03", 21), ("G04", 21), ("G08", 15), ("G09", 9)]
    )
    def test_rule_count_per_domain(self, domain: str, expected: int) -> None:
        assert sum(1 for rid in RULES if rid.startswith(f"{domain}.")) == expected

    def test_ids_are_contiguous(self) -> None:
        """编号必须连续：缺号通常意味着规格里有条规则被漏实现。

        判据是「已实现 + ``PENDING_RULES``」的并集——``PENDING_RULES`` 显式登记
        规格有、代码暂缺的条目，所以缺口不能靠留白蒙混，只能靠删清单来消除。
        """
        for domain in ("G03", "G04", "G08", "G09"):
            ids = {rid for rid in (*RULES, *PENDING_RULES) if rid.startswith(f"{domain}.")}
            got = sorted(int(rid.split(".R")[1]) for rid in ids)
            assert got == list(range(1, len(got) + 1)), f"{domain} 编号不连续：{got}"

    def test_every_rule_declares_a_source_cell(self) -> None:
        """``source`` 是第二真源的凭据：必须形如 ``G03!9``，能回指 V3.6.2 单元格。"""
        bad = [rid for rid, spec in RULES.items() if not spec.source or "!" not in spec.source]
        assert not bad, bad

    def test_source_cell_domain_matches_rule_domain(self) -> None:
        bad = [rid for rid, spec in RULES.items() if not spec.source.startswith(rid.split(".")[0])]
        assert not bad, bad

    def test_source_rows_match_the_workbook(self) -> None:
        """``source`` 里的行号必须等于该 rule_id 在 V3.6.2 工作簿里的**实际行**。

        为什么需要这条：``source`` 平时只是一个字符串，规格插行/删行后 66 个行号
        会集体静默漂移，而所有判据测试照样全绿。V3.6.2 升级时就漂了 29 条，
        直到逐条对账才发现——那本该是机器的事。

        ``G04.R21`` 是工程补齐规则（规格里没有同 ID 的行），它的 ``source``
        指向所消费的字段定义行，因此改为校验「那一格确实是 ``C.MALPERFUSION``」。
        """
        spec_path = SPEC_XLSX
        if spec_path is None:
            pytest.skip("规格 Excel 既不在仓库 docs/ 也不在桌面，跳过逐条对账")
        from openpyxl import load_workbook

        wb = load_workbook(spec_path, read_only=True, data_only=True)
        actual: dict[str, str] = {}
        for sheet in ("G03", "G04", "G08", "G09"):
            for i, row in enumerate(wb[sheet].iter_rows(min_row=1, values_only=True), start=1):
                rid = str(row[0] or "").strip()
                if rid in RULES:
                    actual[rid] = f"{sheet}!{i}"

        wrong = [
            f"{rid}: 代码 {spec.source}，规格 {actual.get(rid) or '规格里没有这一行'}"
            for rid, spec in RULES.items()
            if rid != "G04.R21" and actual.get(rid) != spec.source
        ]
        assert not wrong, "\n".join(wrong)

        sheet, _, row = RULES["G04.R21"].source.partition("!")
        cell = wb[sheet].cell(row=int(row), column=1).value
        assert str(cell).strip() == "C.MALPERFUSION", cell

    def test_every_rule_has_a_valid_exit(self) -> None:
        assert {spec.exit for spec in RULES.values()} <= VALID_EXITS

    def test_every_rule_declares_fields(self) -> None:
        bad = [rid for rid, spec in RULES.items() if not spec.fields]
        assert not bad, bad

    def test_declared_fields_are_readable_paths(self) -> None:
        """``fields`` 是缺口清单的来源；路径写错的话缺口永远为空（静默失效）。"""
        case = make_case()
        bad: list[str] = []
        for rid, spec in RULES.items():
            for path in spec.fields:
                try:
                    _read_path(case, path)
                except Exception:  # noqa: BLE001 - 路径错就是错，异常类型不重要
                    bad.append(f"{rid}::{path}")
        assert not bad, bad

    def test_ruleset_md_documents_every_rule(self) -> None:
        """代码是第一真源，``RULESET.md`` 是第二真源；两边必须一一对应。"""
        md = (Path(__file__).resolve().parent.parent / "src" / "rules" / "RULESET.md").read_text(
            encoding="utf-8"
        )
        missing = sorted(rid for rid in RULES if f"| {rid} |" not in md)
        assert not missing, f"RULESET.md 缺：{missing}"

    def test_every_rule_is_reachable_in_pipeline(self) -> None:
        """每条规则都必须被某个编排桶或显式调用点覆盖；漏进桶 = 永远不执行。"""
        covered = set(G03_P01) | set(G03_P02) | set(G03_P03) | set(G04_BLOCKING) | set(G04_EXTRA)
        covered |= set(G08_RULES)
        covered |= {"G03.R15", "G04.R13", "G04.R14", "G04.R19", "G04.R20"}
        covered |= {"G08.R09", "G08.R10", "G08.R11", "G08.R12", "G08.R13", "G08.R14", "G08.R15"}
        covered |= {"G09.R01", "G09.R02", "G09.R03", "G09.R04"}
        covered |= {"G09.R05", "G09.R06", "G09.R07", "G09.R08", "G09.R09"}
        # G04.R20 在 ``_finish_mode`` 的分类阶段复记（出口写的就是 G08）
        missing = sorted(set(RULES) - covered)
        assert not missing, f"未进任何桶：{missing}"


class TestRuleSourceRegistry:
    """规则来源性质登记（V3.6 起）—— 它决定「本结论是不是指南说的」。"""

    def test_every_rule_is_registered(self) -> None:
        assert set(RULE_SOURCE) == set(RULES)

    def test_every_rule_uses_a_known_source_kind(self) -> None:
        allowed = {"ESVS原文", "工程安全规则", "其他指南"}
        bad = {rid: kind for rid, (kind, _) in RULE_SOURCE.items() if kind not in allowed}
        assert not bad, bad

    def test_esvs_rules_cite_a_section(self) -> None:
        """声称取自 ESVS 就必须指得出章节，否则与工程规则无异。"""
        bad = [rid for rid, (kind, sec) in RULE_SOURCE.items() if kind == "ESVS原文" and not sec]
        assert not bad, bad

    def test_non_esvs_rules_carry_no_section(self) -> None:
        """反向也要守：**工程规则**挂一个章节号，等于给自拟规则盖章成指南原文。

        「其他指南」例外 —— 它本来就指别家指南的条款（ACC/AHA 推荐号、
        ESVS 2026 VGEI 共识），章节号正是它可溯源的凭据。所以这里只查
        ``工程安全规则``，不查 ``其他指南``。
        """
        bad = [rid for rid, (kind, sec) in RULE_SOURCE.items() if kind == "工程安全规则" and sec]
        assert not bad, bad

    def test_source_kind_distribution_matches_v36(self) -> None:
        """数字取自 V3.6 表「备注／依据」列。改动等于改规格，必须同步这里。"""
        tally = {"ESVS原文": 0, "工程安全规则": 0, "其他指南": 0}
        for kind, _ in RULE_SOURCE.values():
            tally[kind] += 1
        assert tally == {"ESVS原文": 31, "工程安全规则": 29, "其他指南": 6}

    def test_emergency_tier_is_mostly_engineering_rules(self) -> None:
        """G03 急诊层只有 6 条是 ESVS 原文 —— 这条断言的意义是防止有人
        日后把工程规则改标成指南原文，让论文里的说法悄悄变得「更有依据」。"""
        g03 = {rid: RULE_SOURCE[rid][0] for rid in RULE_SOURCE if rid.startswith("G03.")}
        assert sum(1 for k in g03.values() if k == "ESVS原文") == 6


class TestNextNodesContract:
    """``next_nodes``：本次结论要走的全部节点（V3.6 起）。"""

    def test_next_nodes_always_starts_with_the_primary_exit(self) -> None:
        """主出口必须是序列的第一个 —— 否则「本次去哪」与「还要去哪」会打架。"""
        for overrides in ({}, {"c": {"rupture_report": "明确破裂"}}, {"n": {"neck_len": 8.0}}):
            result = route(make_case(**overrides))
            assert result.next_nodes, overrides
            assert result.next_nodes[0] == result.next_node

    def test_single_exit_rules_degrade_to_a_one_element_sequence(self) -> None:
        """现有 54 条各走一处，序列长度必须是 1（多出来的元素只能是编造的）。"""
        for overrides in ({}, {"c": {"rupture_report": "明确破裂"}}):
            assert len(route(make_case(**overrides)).next_nodes) == 1

    def test_next_nodes_covers_triage_result(self) -> None:
        """回归：急诊 + 病种流程并存时（V3.6 D3.1.R01「G05 并进入 D3.2」）
        ``next_nodes`` 要能表达两个动作，而不是丢一个。此处先守住「不丢主出口」，
        D3.x 落地后再把多节点用例补进来。"""
        result = route(make_case(c={"rupture_report": "明确破裂"}))
        assert result.next_node == "G05"
        assert result.next_nodes == ["G05"]


# ===========================================================================
# 二、三值原语（Kleene）
# ===========================================================================
class TestThreeValuedPrimitives:
    """``None`` 是 UNKNOWN：不是「没命中」，也不是「阴性」。"""

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            ((True, True), True),
            ((True, False), False),
            ((True, None), None),
            ((False, None), False),  # False 压过 UNKNOWN：已确定不成立
            ((None, None), None),
        ],
    )
    def test_and(self, args: tuple[bool | None, ...], expected: bool | None) -> None:
        assert _and(*args) is expected

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            ((True, None), True),  # True 压过 UNKNOWN：已确定成立
            ((False, False), False),
            ((False, None), None),
            ((None, None), None),
        ],
    )
    def test_or(self, args: tuple[bool | None, ...], expected: bool | None) -> None:
        assert _or(*args) is expected

    @pytest.mark.parametrize(("value", "expected"), [(True, False), (False, True), (None, None)])
    def test_not(self, value: bool | None, expected: bool | None) -> None:
        assert _not(value) is expected

    def test_eq_explicit_listing_wins(self) -> None:
        """规格把「疑似」当**阳性触发**（G03.R04/R07/G08.R03）——显式列出即命中。"""
        assert _eq("疑似", "疑似") is True

    def test_eq_uncertain_value_is_unknown(self) -> None:
        """规则没显式要「疑似」时，它只能是 UNKNOWN，不能支撑任何结论。"""
        assert _eq("疑似", "确认") is None
        assert _eq("未评价", "确认") is None

    def test_eq_known_negative_is_false(self) -> None:
        assert _eq("未见", "明确") is False

    def test_eq_missing_is_unknown(self) -> None:
        assert _eq(None, "明确") is None


#: 空病例上**应当**命中的规则 —— 它们回答的正是「什么都没有」这件事本身。
#: ``G09.R08``：没有任何可信标签 → 未分类，这是正确结论，不是「未知当成成立」。
EMPTY_CASE_HITS: frozenset[str] = frozenset({"G09.R08"})


class TestUnknownDoesNotShortCircuit:
    @pytest.mark.parametrize("rule_id", sorted(set(RULES) - EMPTY_CASE_HITS))
    def test_empty_case_never_hits(self, rule_id: str) -> None:
        """空病例：所有规则都不得返回 True —— 未知绝不能当成立。"""
        verdict = RULES[rule_id].func(_Ctx(VascCase(case_id="EMPTY")))
        assert verdict is not True, f"{rule_id} 在空病例上命中了 —— 未知被当成了成立"

    def test_empty_case_routes_to_verify(self) -> None:
        """兜底是资料核实，不是指南（G07 是排他分支）。"""
        result = route(VascCase(case_id="EMPTY"))
        assert result.kind is RouteKind.VERIFY
        assert result.next_node == "资料核实"
        assert result.confidence_level == "低"
        assert result.evidence_gaps

    def test_missing_neck_length_blocks_low_complexity(self) -> None:
        """颈长缺失不得判低复杂（G04.R13）。"""
        assert verdict_of("G04.R13", n={"neck_len": None}) is True

    def test_unmentioned_is_not_negative(self) -> None:
        """``pain_present`` 未提及 ≠ 「否」：R15 的放行 gate 必须因此失效。"""
        assert verdict_of("G03.R15", s={"pain_present": None}) is None


# ===========================================================================
# 三、54 条逐条命中
# ===========================================================================
HITS: list[tuple[str, dict[str, object]]] = [
    # ---- G03 急诊（21）----
    (
        "G03.R01",
        {
            "q": {"cta_enhance": "适当", "cta_phase": "动脉期"},
            "c": {"extrav": "明确", "extrav_contig": "明确连续", "source_kind": "正式报告"},
        },
    ),
    (
        "G03.R02",
        {
            "c": {
                "aorta": ["腹主动脉瘤"],
                "hematoma": "明确",
                "hematoma_adj": "明确相邻",
                "source_kind": "专家确认",
            }
        },
    ),
    ("G03.R03", {"c": {"rupture_report": "明确破裂"}}),
    ("G03.R04", {"d": {"dx_context": "疑似"}, "v": {"sbp": 70.0}}),
    (
        "G03.R05",
        {"s": {"pain_current": "当前新发", "pain_site": ["背"], "pain_attrib": "明确相关"}},
    ),
    (
        "G03.R06",
        {
            "s": {
                "pain_current": "本次进展",
                "pain_site": ["腰"],
                "pain_onset": "突然",
                "pain_attrib": "未判断",
            }
        },
    ),
    ("G03.R07", {"c": {"rupture_report": "疑似破裂"}}),
    ("G03.R08", {"d": {"surg": ["EVAR"]}, "s": {"gi_bleed": ["黑便"]}}),
    ("G03.R09", {"s": {"limb": ["痛", "苍白"], "pulse_deficit": "缺失"}}),
    ("G03.R10", {"v": {"spo2": 91.0, "rr": 22.0, "sbp": 105.0, "hr": 95.0}}),
    ("G03.R11", {"v": {"spo2": 92.0, "rr": 22.0, "hr": 95.0}}),
    ("G03.R12", {"s": {"syncope": "肯定"}, "q": {"cta_enhance": "不足"}}),
    ("G03.R13", {"q": {"idmatch": "不一致"}}),
    ("G03.R14", {"q": {"current_assess": "未评估"}}),
    ("G03.R15", {}),
    ("G03.R16", {"d": {"dx_context": "疑似"}}),
    (
        "G03.R17",
        {"lab": {"hb": 85.0, "hb_prev": 120.0, "hb_comparable": True, "hb_confound": False}},
    ),
    (
        "G03.R18",
        {
            "d": {"dx_context": "无相关记录"},
            "s": {
                "pain_current": "当前新发",
                "pain_site": ["背"],
                "pain_onset": "突然",
                "pain_severity": 8,
            },
        },
    ),
    ("G03.R19", {"lab": {"hb": 85.0, "hb_prev": 120.0, "hb_comparable": None}}),
    ("G03.R20", {"v": {"sbp": 70.0}}),
    ("G03.R21", {"lab": {"lact": 3.0, "lact_uln": 2.0}, "q": {"unit": "有效"}}),
    # ---- G04 复杂（18，R14 另行测试）----
    ("G04.R01", {"n": {"neck_len": 8.0}}),
    ("G04.R02", {"n": {"neck_len": 2.0}}),
    ("G04.R03", {"n": {"renal_l": "明确受累"}}),
    ("G04.R04", {"n": {"renal_l": "明确受累", "sma": "明确受累"}}),
    (
        "G04.R05",
        {"n": {"renal_l": "明确受累", "sma": "明确受累", "ca": "明确受累", "thoracic": "否"}},
    ),
    ("G04.R06", {"n": {"thoracic": "是"}}),
    ("G04.R07", {"n": {"infection_ctx": "医师明确"}}),
    ("G04.R08", {"n": {"seal_ifu": "不符合"}}),
    ("G04.R09", {"n": {"iliac_bilat": "双侧", "iia_left": "闭塞"}}),
    ("G04.R10", {"n": {"access_occlusion": "双侧闭塞"}}),
    ("G04.R11", {"n": {"prev_repair": "EVAR", "endoleak_type": "I"}}),
    ("G04.R12", {"n": {"egfr": 25.0}}),
    ("G04.R13", {"n": {"neck_len": None}}),
    ("G04.R15", {"n": {"genetic_ctx": "确诊"}}),
    ("G04.R16", {"n": {"inflammatory_ctx": "影像高度提示"}}),
    ("G04.R17", {"gen": {"aaa_age_onset": 45}}),
    ("G04.R18", {"n": {"inflammatory_ctx": "可疑", "infection_ctx": "高度怀疑"}}),
    ("G04.R21", {"c": {"malperfusion": ["下肢"]}}),
    # ---- G08 分类（11，R09 另行测试）----
    ("G08.R01", {}),
    ("G08.R02", {"cl": {"symptom_aaa": "医师确认相关"}}),
    ("G08.R03", {"cl": {"aaa_rupture": "明确"}}),
    ("G08.R04", {"cl": {"infection": "原生感染明确"}}),
    ("G08.R05", {"cl": {"pau": "明确"}}),
    ("G08.R06", {"cl": {"prev_evor": "EVAR", "post_reason": "内漏"}}),
    ("G08.R07", {"cl": {"iliac_site": ["左侧", "CIA"]}}),
    (
        "G08.R08",
        {"n": {"thoracic": "否", "sma": "明确受累", "ca": "明确受累", "renal_l": "明确受累"}},
    ),
    ("G08.R10", {"n": {"inflammatory_ctx": "临床确认"}}),
    ("G08.R11", {"n": {"genetic_ctx": "疑似遗传背景"}}),
    # ---- G09 收尾（4，R03/R04 另行测试）----
    ("G09.R01", {"k": {"new_urgent": "是"}}),
    ("G09.R02", {"q": {"idmatch": "不一致"}}),
    # ---- V3.6.2 新增（9 条可直接命中；G09.R07/R09 见 STATEFUL_RULES）----
    ("G04.R19", {"cl": {"prev_evor": "EVAR"}}),
    ("G04.R20", {"n": {"inflammatory_ctx": "影像高度提示"}}),
    ("G08.R12", {"cl": {"infection": "原生感染明确"}}),
    ("G08.R13", {"cl": {"prev_evor": "EVAR"}}),
    (
        "G08.R14",
        {
            "cl": {
                "aaa_reported": "否定",
                "aaa_rupture": "未见相关证据",
                "symptom_aaa": "无当前症状",
                "infection": "无证据",
                "fistula": "否",
                "iliac_site": ["无"],
                "prev_evor": "无",
            }
        },
    ),
    ("G08.R15", {"n": {"cta_status": "部分"}}),
    ("G09.R05", {"cl": {"infection": "原生感染明确"}}),
    ("G09.R06", {"cl": {"prev_evor": "EVAR"}, "n": {"endoleak_type": "I"}}),
    ("G09.R08", {}),
]

STATEFUL_RULES = frozenset({"G04.R14", "G08.R09", "G09.R03", "G09.R04", "G09.R07", "G09.R09"})
"""依赖前置运行时状态、无法用单个病例直接命中的规则。

``G09.R07`` 读 ``no_target_candidate``（由 G08.R14 在分类阶段写入）；
``G09.R09`` 读 ``labels``（多标签是前序规则累积的结果）。两者的命中证据
由 ``TestMultilabelAndNoTarget`` 用完整 ``route()`` 覆盖。
"""


class TestEveryRuleHits:
    @pytest.mark.parametrize(("rule_id", "overrides"), HITS, ids=[h[0] for h in HITS])
    def test_rule_hits(self, rule_id: str, overrides: dict[str, object]) -> None:
        assert verdict_of(rule_id, **overrides) is True, f"{rule_id} 未按预期命中"

    def test_hit_table_covers_every_independently_testable_rule(self) -> None:
        """``HITS`` 漏一条 = 那条规则没有命中证据。"""
        assert {rid for rid, _ in HITS} == set(RULES) - STATEFUL_RULES

    def test_hit_table_is_not_self_contradictory(self) -> None:
        """不许重复 rule_id —— 重复会掩盖「改了一条忘改另一条」。"""
        ids = [rid for rid, _ in HITS]
        assert len(ids) == len(set(ids))


# ===========================================================================
# 四、依赖运行时状态的 4 条
# ===========================================================================
class TestStatefulRules:
    def _ctx(self, **overrides: object) -> _Ctx:
        return _Ctx(make_case(**overrides))  # type: ignore[arg-type]

    def test_r14_needs_blocking_results(self) -> None:
        """R14 的判据是「上面 12 条都没命中」；没跑过就无从判断 → UNKNOWN。"""
        assert RULES["G04.R14"].func(self._ctx()) is None

    def test_r14_releases_when_all_blocking_are_false(self) -> None:
        ctx = self._ctx()
        ctx.g04_blocking = [False] * len(G04_BLOCKING)
        assert RULES["G04.R14"].func(ctx) is True

    def test_r14_does_not_release_when_one_is_unknown(self) -> None:
        """有一条判不了（UNKNOWN）就不得放行 —— 「判不了」不能当成「没命中」。"""
        ctx = self._ctx()
        ctx.g04_blocking = [False] * (len(G04_BLOCKING) - 1) + [None]
        assert RULES["G04.R14"].func(ctx) is not True

    def test_r14_does_not_release_without_cta(self) -> None:
        ctx = self._ctx(n={"cta_status": "缺失"})
        ctx.g04_blocking = [False] * len(G04_BLOCKING)
        assert RULES["G04.R14"].func(ctx) is False

    def test_r09_detects_conflict_from_own_labels(self) -> None:
        ctx = self._ctx()
        ctx.labels = ["D1", "D3"]
        assert RULES["G08.R09"].func(ctx) is True

    def test_r09_detects_conflict_from_prefilled_labels(self) -> None:
        """抽取层预填的 ``cl.diag_multi`` 同样可能自相矛盾，不能只信自己算的。"""
        assert verdict_of("G08.R09", cl={"diag_multi": ["D1", "D2"]}) is True

    def test_r09_single_label_is_not_conflict(self) -> None:
        ctx = self._ctx()
        ctx.labels = ["D1"]
        assert RULES["G08.R09"].func(ctx) is False

    def test_r03_reads_diag_conflict_state(self) -> None:
        ctx = self._ctx()
        ctx.diag_conflict = True
        assert RULES["G09.R03"].func(ctx) is True
        ctx.diag_conflict = False
        assert RULES["G09.R03"].func(ctx) is False
        ctx.diag_conflict = None
        assert RULES["G09.R03"].func(ctx) is None

    def test_r04_requires_at_least_one_label(self) -> None:
        """冲突已排除但一个标签都没有 → 确定「不能分类」，是 False 不是 UNKNOWN。"""
        ctx = self._ctx()
        ctx.diag_conflict = False
        assert RULES["G09.R04"].func(ctx) is False
        ctx.labels = ["D1"]
        assert RULES["G09.R04"].func(ctx) is True

    def test_r04_is_unknown_while_conflict_unresolved(self) -> None:
        ctx = self._ctx()
        ctx.labels = ["D1"]
        ctx.diag_conflict = None
        assert RULES["G09.R04"].func(ctx) is None

    def test_label_mapping_is_total(self) -> None:
        """产出标签的 G08 规则必须在 ``LABEL_BY_RULE`` 里有映射，否则标签被静默丢掉。"""
        producers = {
            "G08.R01",
            "G08.R02",
            "G08.R03",
            "G08.R04",
            "G08.R05",
            "G08.R06",
            "G08.R07",
            "G08.R08",
            "G08.R10",
        }
        assert producers <= set(LABEL_BY_RULE)


# ===========================================================================
# 五、编排：出口优先级与兜底
# ===========================================================================
class TestExitOrchestration:
    def test_clean_case_goes_guideline(self) -> None:
        result = route(make_case())
        assert result.kind is RouteKind.MODE
        assert result.mode is RouteMode.GUIDELINE
        assert result.next_node == "G07"

    def test_hypotension_goes_emergency(self) -> None:
        result = route(make_case(v={"sbp": 70.0}))
        assert result.mode is RouteMode.EMERGENCY
        assert result.next_node == "G05"

    def test_short_neck_goes_complex(self) -> None:
        result = route(make_case(n={"neck_len": 8.0}))
        assert result.mode is RouteMode.COMPLEX
        assert result.next_node == "G06"

    def test_p01_beats_p02(self) -> None:
        """同一病例同时满足急诊与人工复核条件时，急诊优先。"""
        result = route(make_case(v={"sbp": 70.0}, lab={"hb": 85.0, "hb_prev": 120.0}))
        assert result.next_node == "G05"

    def test_review_beats_verify(self) -> None:
        """P02（人工复核）压过 P03（资料核实）。"""
        result = route(
            make_case(lab={"hb": 85.0, "hb_prev": 120.0}, q={"current_assess": "未评估"})
        )
        assert result.next_node == "人工复核"

    def test_manual_urgency_overrides_everything(self) -> None:
        result = route(make_case(urgency_flag=True))
        assert result.mode is RouteMode.EMERGENCY
        assert result.rule_id == "MANUAL.URGENCY"

    def test_manual_urgency_reason_is_explicit(self) -> None:
        assert "人工置位" in route(make_case(urgency_flag=True)).reason

    def test_outcome_priority_matches_documented_order(self) -> None:
        assert OUTCOME_PRIORITY[RouteKind.REVIEW] > OUTCOME_PRIORITY[RouteKind.MODE]
        assert OUTCOME_PRIORITY[RouteKind.MODE] > OUTCOME_PRIORITY[RouteKind.VERIFY]

    def test_guideline_is_exclusive_not_fallback(self) -> None:
        """资料不足时不得落到 G07 —— 真正的兜底是资料核实。"""
        for case in (
            VascCase(case_id="EMPTY"),
            make_case(n={"neck_len": None}),
            make_case(n={"cta_status": "缺失"}),
        ):
            assert route(case).next_node != "G07"

    def test_deterministic(self) -> None:
        case = make_case(n={"neck_len": 8.0})
        first, second = route(case), route(case)
        assert (first.rule_id, first.next_node, first.class_output) == (
            second.rule_id,
            second.next_node,
            second.class_output,
        )


# ===========================================================================
# 六、B6：量化值缺失时的回落
# ===========================================================================
class TestMeasurementFallback:
    def test_fallback_registers_data_source_gap(self) -> None:
        """缺量化值时读报告值，但必须留痕，不能与量化值混为一谈。"""
        case = make_case(cl={"aaa_diam": None}, c={"maxd": 42.0})
        gaps = route(case).evidence_gaps
        assert any("回落" in g or "c.maxd" in g for g in gaps), gaps

    def test_no_fallback_when_measurement_present(self) -> None:
        from src.models.preop.measurement import VascMeasurement

        case = make_case(cl={"aaa_diam": None}, c={"maxd": 42.0})
        measurement = VascMeasurement(case_id="M", max_diameter_mm=45.0, method="vmtk")
        gaps = route(case, measurement).evidence_gaps
        assert not any("回落" in g for g in gaps), gaps


# ===========================================================================
# 七、标签与置信度
# ===========================================================================
class TestLabelsAndConfidence:
    def test_clean_case_gets_d1(self) -> None:
        assert route(make_case()).class_output == ["D1"]

    def test_ruptured_gets_d3(self) -> None:
        assert "D3" in route(make_case(cl={"aaa_rupture": "明确"})).class_output

    def test_conflicting_labels_go_to_review(self) -> None:
        result = route(make_case(cl={"diag_multi": ["D1", "D3"]}))
        assert result.kind is RouteKind.REVIEW
        assert result.rule_id == "G09.R03"

    def test_inflammatory_yields_d5_subtype(self) -> None:
        assert "D5" in route(make_case(n={"inflammatory_ctx": "影像高度提示"})).class_output

    def test_suspected_rupture_gets_d3_label(self) -> None:
        """``G03.R16`` 医生明确怀疑破裂 —— 该进 D3，不是「病种未分类」。

        修前 ``class_output`` 只认 ``D3_EMERGENCY_RULES`` 那八条，R16 不在其中，
        于是「医生写着疑似破裂」的病例进急诊模式后工具A 一个节点都跑不了，
        输出退化成「病种未分类：病种流程未给出结论」。规格 D3 sheet 的对象
        本就包含「疑似破裂」，这条必须归 D3。
        """
        result = route(make_case(d={"dx_context": "疑似"}))
        assert result.rule_id == "G03.R16"
        assert result.mode is RouteMode.EMERGENCY
        assert result.class_output == ["D3"]

    def test_graft_with_gi_bleed_gets_d6_label(self) -> None:
        """``G03.R08`` 移植物史 + 消化道出血 —— 主动脉肠瘘是**术后**并发症。

        归 D6 不是新判断：``D6.3.R01`` 的判据里本来就有 ``active_gi_bleed``，
        ``D6.1`` 也把「感染/瘘」路由到 ``D6.3``。修前预置标签写死成 ``D3``，
        这条根本拿不到标签。
        """
        result = route(make_case(d={"surg": ["EVAR"]}, s={"gi_bleed": ["黑便"]}, c={"graft": "有"}))
        assert result.rule_id == "G03.R08"
        assert result.mode is RouteMode.EMERGENCY
        assert result.class_output == ["D6"]

    def test_every_emergency_trigger_has_a_decided_label(self) -> None:
        """``G03.P01`` 的每条规则都必须「有病种标签」或「明确登记无病种」。

        这是防复发的那一条：留空与「忘了登记」在运行期看起来一样，
        两者都会让工具A 静默空转。``router`` 的导入期自检守同一件事，
        这里再从外部断言一次，确保自检本身没被悄悄删掉。
        """
        from src.rules.router import EMERGENCY_LABELS, EMERGENCY_NO_LABEL, G03_P01

        undecided = set(G03_P01) - set(EMERGENCY_LABELS) - set(EMERGENCY_NO_LABEL)
        assert undecided == set(), f"这些急诊规则命中后会空转：{sorted(undecided)}"
        # 两侧不得重叠：同一规则既预置标签又声明「无病种」是自相矛盾。
        assert set(EMERGENCY_LABELS) & set(EMERGENCY_NO_LABEL) == set()

    def test_genetic_does_not_add_d8(self) -> None:
        """遗传只作病因修饰标签，**不新增 D8**（D1–D7 编号必须保持）。"""
        allowed = {"D1", "D2", "D3", "D4", "D5", "D6", "D7", "Ⅳ型TAAA扩展"}
        result = route(make_case(n={"genetic_ctx": "确诊"}))
        assert set(result.class_output) <= allowed

    def test_confidence_is_high_when_no_gap(self) -> None:
        assert route(make_case()).confidence_level == "高"

    def test_confidence_is_low_when_unknown_decisive(self) -> None:
        assert route(VascCase(case_id="EMPTY")).confidence_level == "低"

    def test_ruleset_version_is_recorded(self) -> None:
        from src.rules.version import RULESET_VERSION

        assert route(make_case()).ruleset_version == RULESET_VERSION


# ===========================================================================
# 八、三值原语的边界（这些分支不写测试就等于没守）
# ===========================================================================
class TestPrimitiveEdgeCases:
    def test_has_none_list_is_unknown(self) -> None:
        from src.rules.router import _has

        assert _has(None, "痛") is None

    def test_has_uncertain_token_is_unknown(self) -> None:
        from src.rules.router import _has

        assert _has(["未知"], "痛") is None

    def test_has_known_negative_is_false(self) -> None:
        from src.rules.router import _has

        assert _has(["无"], "痛") is False

    def test_absent_requires_an_explicit_record(self) -> None:
        """空列表 → False：**没有记录 ≠ 明确阴性**。"""
        from src.rules.router import _absent

        assert _absent([], ("呕血",)) is False
        assert _absent(None, ("呕血",)) is False

    def test_absent_true_only_when_explicitly_clean(self) -> None:
        from src.rules.router import _absent

        assert _absent(["无"], ("呕血",)) is True

    def test_absent_uncertain_is_unknown(self) -> None:
        from src.rules.router import _absent

        assert _absent(["未知"], ("呕血",)) is None

    def test_absent_dangerous_present_is_false(self) -> None:
        from src.rules.router import _absent

        assert _absent(["呕血"], ("呕血",)) is False

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_is_not_a_number(self, value: bool) -> None:
        """``True`` 会被当成 1 —— 布尔绝不能混进数值判据。"""
        from src.rules.router import _num

        assert _num(value) is None

    def test_branch_occlusion_three_states(self) -> None:
        from src.rules.router import _branch_occluded

        assert _branch_occluded(_Ctx(make_case(c={"branch": {}}))) is None
        occluded = _Ctx(make_case(c={"branch": {"SMA": "闭塞", "CA": "通畅"}}))
        assert _branch_occluded(occluded) is True
        unclear = _Ctx(make_case(c={"branch": {"SMA": "未知"}}))
        assert _branch_occluded(unclear) is None
        clear = _Ctx(make_case(c={"branch": {"SMA": "通畅"}}))
        assert _branch_occluded(clear) is False


class TestDerivedStates:
    """物化派生量（不进模型、不进哈希，只参与本次判定）。"""

    def test_news2_scale2_is_unknown(self) -> None:
        """用了未实现的新量表 → 宁可没有分数，也不能拿错量表算。"""
        assert _Ctx(make_case(v={"spo2_scale": "Scale2经确认"})).news2 is None

    def test_genetic_derived_from_syndrome(self) -> None:
        ctx = _Ctx(make_case(n={"genetic_ctx": None}, gen={"syndrome_type": "Marfan"}))
        assert ctx.genetic_ctx == "确诊"

    def test_genetic_derived_from_variant(self) -> None:
        ctx = _Ctx(make_case(n={"genetic_ctx": None}, gen={"variant_class": "可能致病"}))
        assert ctx.genetic_ctx == "确诊"

    def test_genetic_suspected_from_family_history(self) -> None:
        ctx = _Ctx(make_case(n={"genetic_ctx": None}, gen={"family_aneurysm": "阳性"}))
        assert ctx.genetic_ctx == "疑似遗传背景"

    def test_genetic_clear_when_explicitly_negative(self) -> None:
        ctx = _Ctx(make_case(n={"genetic_ctx": None}, gen={"family_aneurysm": "明确无相关史"}))
        assert ctx.genetic_ctx == "无已知阳性线索"

    def test_genetic_unknown_when_nothing_recorded(self) -> None:
        # 注意：``make_case(gen={})`` 是**域级浅更新**，不会清空基线，必须逐键置 None
        blank = {
            "syndrome_type": None,
            "variant_class": None,
            "family_aneurysm": None,
            "syndromic_features": None,
            "aaa_age_onset": None,
        }
        ctx = _Ctx(make_case(n={"genetic_ctx": None}, gen=blank))
        assert ctx.genetic_ctx is None

    def test_inflammatory_high_from_mantle_sign(self) -> None:
        ctx = _Ctx(make_case(n={"inflammatory_ctx": None}, inf={"mantle_sign": "明确存在"}))
        assert ctx.inflammatory_ctx == "影像高度提示"

    def test_inflammatory_suspected_from_wall_fibrosis(self) -> None:
        ctx = _Ctx(make_case(n={"inflammatory_ctx": None}, inf={"wall_fibrosis": "可疑"}))
        assert ctx.inflammatory_ctx == "可疑"

    def test_inflammatory_clear_when_both_negative(self) -> None:
        ctx = _Ctx(
            make_case(
                n={"inflammatory_ctx": None}, inf={"mantle_sign": "未见", "wall_fibrosis": "未见"}
            )
        )
        assert ctx.inflammatory_ctx == "无当前支持证据"

    def test_crp_alone_does_not_trigger_inflammation(self) -> None:
        """G04.R16 明文：单纯 CRP/ESR 升高不触发。

        基线里 ``mantle_sign`` / ``wall_fibrosis`` 是「未见」，必须先置 None ——
        ``make_case(inf={...})`` 是**域级浅更新**，不写进 dict 的键保持基线值。
        """
        ctx = _Ctx(
            make_case(
                n={"inflammatory_ctx": None},
                inf={
                    "mantle_sign": None,
                    "wall_fibrosis": None,
                    "inflamm_markers": "CRP 88 mg/L（参考上限 5）",
                },
            )
        )
        assert ctx.inflammatory_ctx is None

    def test_materialized_state_wins_over_derivation(self) -> None:
        """抽取层给了主状态就以它为准，不再拿明细覆写。"""
        ctx = _Ctx(make_case(n={"genetic_ctx": "未评价"}, gen={"syndrome_type": "Marfan"}))
        assert ctx.genetic_ctx == "未评价"

    # ---- C.MALPERFUSION（V3.6.2 唯一没有规则行的字段，走派生物化通路）----
    def test_malperfusion_extracted_organ_is_true(self) -> None:
        assert _Ctx(make_case(c={"malperfusion": ["下肢"]})).malperfusion is True

    def test_malperfusion_extracted_negative_is_false(self) -> None:
        assert _Ctx(make_case(c={"malperfusion": ["无"]})).malperfusion is False

    def test_malperfusion_extracted_uncertain_is_unknown(self) -> None:
        """「疑似」是「有值但值本身不确定」→ 只能 UNKNOWN，不能当阳性。"""
        assert _Ctx(make_case(c={"malperfusion": ["疑似"]})).malperfusion is None

    def test_malperfusion_conflict_is_unknown(self) -> None:
        assert _Ctx(make_case(c={"malperfusion": ["下肢", "证据冲突"]})).malperfusion is None

    def test_malperfusion_organs_are_carried(self) -> None:
        ctx = _Ctx(make_case(c={"malperfusion": ["肾", "下肢"]}))
        assert ctx.malperfusion_organs == frozenset({"肾", "下肢"})

    def test_malperfusion_branch_occlusion_alone_is_never_true(self) -> None:
        """规格 G04!J40 明文：不能仅凭解剖狭窄自动推断器官缺血。

        这条断言是方案乙的护栏——日后谁把派生改成「闭塞即阳性」，它会先红。
        """
        ctx = _Ctx(make_case(c={"malperfusion": None, "branch": {"SMA": "闭塞"}}))
        assert ctx.malperfusion is None

    def test_malperfusion_all_patent_without_symptoms_is_false(self) -> None:
        ctx = _Ctx(
            make_case(
                c={"malperfusion": None, "branch": {"SMA": "通畅", "CA": "通畅"}},
                s={"limb": ["无"]},
            )
        )
        assert ctx.malperfusion is False

    def test_malperfusion_missing_branch_data_is_unknown(self) -> None:
        ctx = _Ctx(make_case(c={"malperfusion": None, "branch": {}}))
        assert ctx.malperfusion is None

    def test_malperfusion_unevaluable_branch_is_unknown(self) -> None:
        """「不可评价」不是「通畅」—— 影像没覆盖到的分支不能当阴性用。"""
        ctx = _Ctx(
            make_case(
                c={"malperfusion": None, "branch": {"SMA": "不可评价", "CA": "通畅"}},
                s={"limb": ["无"]},
            )
        )
        assert ctx.malperfusion is None

    def test_malperfusion_limb_signs_block_the_negative_derivation(self) -> None:
        """分支通畅但有肢体症状 → 不能判「无」：症状不是影像能解释完的。"""
        ctx = _Ctx(
            make_case(
                c={"malperfusion": None, "branch": {"CIA": "通畅"}},
                s={"limb": ["苍白"]},
            )
        )
        assert ctx.malperfusion is None


# ===========================================================================
# 九、G09 编排（回流 / 身份冲突）
# ===========================================================================
class TestG09Orchestration:
    def test_new_urgent_reflows_to_emergency(self) -> None:
        """分类期出现新危险证据 → 立即回流 G05，不因分类进行中而延迟。"""
        result = route(make_case(k={"new_urgent": "是"}))
        assert (result.next_node, result.rule_id) == ("G05", "G09.R01")

    def test_identity_conflict_is_caught_before_classification(self) -> None:
        """``G09.R02`` 端到端**不可达**：``G03.R13`` 在判定前就拦下了身份冲突。

        这不是缺陷，是分层拦截的必然结果——记录下来，免得以后有人以为漏测。
        规则本身另有单元测试（``TestEveryRuleHits`` / ``TestStatefulRules``）。
        """
        result = route(make_case(q={"idmatch": "不一致"}))
        assert result.rule_id == "G03.R13"
        assert result.next_node == "资料核实"

    def test_identity_conflict_by_field_flag_also_caught_early(self) -> None:
        result = route(make_case(q={"conflict": ["身份"]}))
        assert result.rule_id == "G03.R13"


#: 破裂证据四项全部留空 —— 不是「明确未见」，是**没记录**。后者才是否认破裂。
BLANK_RUPTURE_IMAGING = {
    "extrav": None,
    "hematoma": None,
    "rupture_report": None,
    "contain": None,
}


class TestExitConsistency:
    """出口自洽性：D3 标签与「非急诊模式」不可能同时为真。

    这组用例守的是一个**规格没有写、但两句规格话都默认成立**的不变量：

    - ``G08!32``：「D1/D2/D3 互相冲突需复核，**D3 危险优先**」；
    - ``G08.R03``：「疑似时**保持 G05 优先**」。

    两句话都假定「出 D3 时急诊已经触发」。而 ``G03`` 的 21 条规则里**没有一条读**
    ``CL.AAA_RUPTURE``（只认 ``C.EXTRAV``/``C.HEMATOMA``/``C.RUPTURE_REPORT``
    与「临床疑似」）。于是「分类标签写了破裂、影像明细没填」的病例会一路走到
    G07 指南模式，再由 ``G08.R03`` 补上 D3 —— 模式是择期、标签是破裂，
    报告写成「指南模式 · 破裂 AAA：EVAR 一线」。

    修法不是替 G03 判急症（那是规格外的第二处判据），而是拒绝按常规继续。
    """

    def _ruptured(self, rupture: str):
        # ``diag_multi`` 必须显式清空：基线预填 ``["D1"]``，否则会先被
        # G09.R03 判成「D1 与 D3 冲突」→ 人工复核，把这条路径盖住。
        return route(
            make_case(
                cl={"aaa_rupture": rupture, "diag_multi": None},
                c=BLANK_RUPTURE_IMAGING,
            )
        )

    def test_confirmed_rupture_without_emergency_route_is_refused(self) -> None:
        result = self._ruptured("明确")
        assert (result.kind, result.rule_id) == (RouteKind.REVIEW, GUARD_D3_LABEL)
        assert result.mode is None, "不能还挂着指南/复杂模式"
        assert result.next_node == "人工复核"
        assert result.class_output == ["D3"], "标签要保留，供复核者看到系统判了什么"
        assert result.confidence_level == "低"
        assert any("不一致" in gap for gap in result.evidence_gaps)
        assert "D3" in result.reason and "急诊路由未触发" in result.reason

    def test_suspected_rupture_takes_the_same_exit(self) -> None:
        """疑似破裂同样不能落到择期路径 —— ``G08.R03`` 明写「疑似时保持 G05 优先」。"""
        result = self._ruptured("疑似")
        assert (result.kind, result.rule_id) == (RouteKind.REVIEW, GUARD_D3_LABEL)

    def test_a_real_emergency_is_not_downgraded(self) -> None:
        """有影像证据的破裂照常走 G05 —— 自洽性检查不得误伤真急诊。"""
        result = route(
            make_case(
                cl={"aaa_rupture": "明确", "diag_multi": None},
                c={"extrav": "明确", "extrav_contig": "明确连续"},
            )
        )
        assert (result.kind, result.mode) == (RouteKind.MODE, RouteMode.EMERGENCY)
        assert result.rule_id == "G03.R01"

    def test_cases_without_d3_are_untouched(self) -> None:
        """没有 D3 标签的病例一律不受影响（基线走指南、短颈走复杂）。"""
        assert route(make_case()).rule_id == "G04.R14"
        assert route(make_case(n={"neck_len": 8.0})).rule_id == "G04.R01"


# ===========================================================================
# 十、判定产物（不进 VascCase，随 RouteResult 下去）
# ===========================================================================
#: Ⅳ 型 TAAA 的四个关键段：胸段不扩张 + SMA + 腹腔干 + 一侧肾动脉 明确受累。
TAAA_IV_PATTERN = {
    "thoracic": "否",
    "sma": "明确受累",
    "ca": "明确受累",
    "renal_l": "明确受累",
}


class TestJudgmentProducts:
    """``CL.TAAA_IV`` —— Ⅳ 型 TAAA 解剖扩展。

    它是**判定产物**（映射表 ``01!163``）：不进 ``VascCase``、不进 160 字段清单，
    由 ``G08.R08`` 算出后挂在 ``RouteResult.cl_taaa_iv``。下游（指南引擎）按
    「条目子集」需要它，从 ``route_result`` 取，不从 ``case`` 取——否则就等于
    承认它是一份病例事实，而它是规则算出来的。
    """

    def test_taaa_grade_maps_three_states(self) -> None:
        """三值 → 四值：成立=符合，不成立=不符合，未判定=不可评价。"""
        from src.rules.router import _taaa_grade

        assert _taaa_grade(True) == "符合"
        assert _taaa_grade(False) == "不符合"
        assert _taaa_grade(None) == "不可评价"

    def test_iv_pattern_is_confirmed(self) -> None:
        result = route(make_case(n=TAAA_IV_PATTERN))
        assert result.cl_taaa_iv == "符合"
        assert "Ⅳ型TAAA扩展" in result.class_output

    def test_clean_case_is_negative_not_unknown(self) -> None:
        """四个关键段都「未受累」→ 明确排除，必须与「不可评价」分开。"""
        assert route(make_case()).cl_taaa_iv == "不符合"

    def test_thoracic_involvement_is_negative(self) -> None:
        """胸段受累就不是 Ⅳ 型（Ⅳ 型要求膈下、胸段不扩张）。"""
        result = route(make_case(n={**TAAA_IV_PATTERN, "thoracic": "是"}))
        assert result.cl_taaa_iv == "不符合"

    def test_emergency_exit_carries_no_judgment_product(self) -> None:
        """急诊出口不跑 G08 → 值为 None（不是「不符合」，是**根本没判**）。"""
        result = route(make_case(v={"sbp": 70.0}))
        assert result.next_node == "G05"
        assert result.cl_taaa_iv is None

    def test_missing_field_goes_to_verify_not_unknown_grade(self) -> None:
        """缺关键字段 → 转资料核实，而不是给出「不可评价」的分型。

        原因：``G08.R08`` 与 ``G04.R03`` 读同一批字段（thoracic/sma/ca/renal）。
        任一为 UNKNOWN，``G04`` 的阻断型规则就是 UNKNOWN；``G04.R14`` 用
        ``all(v is False)`` 放行 → 不放行 → 兜底「资料核实」，根本到不了 G08。

        推论：**「不可评价」端到端不可达**，只能由 ``_taaa_grade`` 的分支覆盖。
        这不是缺陷，正是「资料不足不下结论」红线的表现——记在这里，免得以后
        有人以为漏测。
        """
        result = route(make_case(n={**TAAA_IV_PATTERN, "sma": None}))
        assert result.mode is None
        assert result.next_node == "资料核实"
        assert result.cl_taaa_iv is None
