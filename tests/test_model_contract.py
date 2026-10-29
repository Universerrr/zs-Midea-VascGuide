"""数据模型契约测试 —— V3.1(2) 的 **161 个字段**是否被如实建模。

与 v1.x（51 个平铺字段）的关键差别：

1. **域结构**：字段不再是平铺的 ``case.age``，而是 ``case.p.age``。域键与字段数由
   ``case.py::DOMAIN_FIELD_COUNTS`` 唯一定义并在导入期自检；本文件**再独立数一遍**，
   防止「自检表与实际字段一起漂移」这种自证式失效。
2. **枚举用 ``Literal``**（W4）：字段专属枚举把中文值直接写进注解，跨字段复用的才用
   ``StrEnum``。所以这里不再断言 ``AnatomyLevel`` 之类的共享枚举，改为断言注解里的
   允许值集合。
3. **不确定值白名单**（F4）：``疑似/可疑/未评价/不可评价/未知`` 是「有值但值本身不确定」，
   只能产生 UNKNOWN；而 ``未见/无/阴性/未受累`` 是**已知阴性**，绝不能进白名单 ——
   混进去会把明确阴性误判成 UNKNOWN。

本文件只测**模型层**（数据能不能装、装错会不会报），不测规则对错。
"""

from __future__ import annotations

import ast
import typing
from enum import StrEnum
from pathlib import Path

import pytest
from conftest import make_case
from pydantic import ValidationError

from src.models.audit import FieldProvenance, FieldWriter, UrgencyFlagAudit
from src.models.preop.case import (
    DOMAIN_FIELD_COUNTS,
    TOTAL_FIELD_COUNT,
    UNCERTAIN_TOKENS,
    VascCase,
    derive,
    is_uncertain,
)
from src.models.preop.recommendation import RouteMode
from src.rules.version import RULESET_CHANGELOG, RULESET_VERSION, SPEC_VERSION

# 旧版（v1.x）的平铺字段名。V3.1(2) 重写时**直接删除、不留过渡态**，这里列出来做反查：
# 任何一个重新出现在 VascCase 顶层，都是「同一事实两份真源」回流。
LEGACY_FLAT_FIELDS = frozenset(
    {
        "age",
        "sex",
        "temporal_status",
        "symptom_aaa_related",
        "symptom_type",
        "rupture_status",
        "prior_repair",
        "disease_category",
        "max_diameter_mm",
        "neck_length_mm",
        "neck_angle_deg",
        "access_diameter_mm",
        "anatomy_level",
        "lesion_morphology",
        "lesion_subtype",
        "complex_anatomy",
        "is_special_lesion",
        "special_lesion_type",
        "infection_evidence",
        "inflammatory_evidence",
        "genetic_syndrome",
        "malignancy",
        "active_hemorrhage",
        "acute_limb_ischemia",
        "septic_shock",
        "visceral_malperfusion",
        "hemodynamic_instability",
        "pain_cause_attributed",
        "growth_rate_mm_per_year",
        "neck_thrombus_calc_pct",
        "iliac_diameter_mm",
        "endoleak_type",
        "tortuosity",
        "ckd_stage",
        "heart_failure_nyha",
        "copd",
        "cad",
        "severe_comorbidity",
        "complex_surgery_history",
        "postop_complication",
        "evidence_conflict",
        "diagnosis_certainty",
        "missing_critical_fields",
        "manual_review_required",
        "confidence",
        "route_mode",
        "guideline_eligible",
    }
)

NON_DOMAIN_FIELDS = frozenset({"case_id", "urgency_flag", "urgency_flag_audit", "field_evidence"})


def _domain_models() -> dict[str, type]:
    """``{域键: 子模型类}`` —— 从 VascCase 注解里取，不硬编码类名。"""
    return {
        key: info.annotation  # type: ignore[misc]
        for key, info in VascCase.model_fields.items()
        if key not in NON_DOMAIN_FIELDS
    }


def _annotations(cls: type) -> dict[str, object]:
    """取注解原样（模块开了 ``from __future__ import annotations``，即字符串形式）。"""
    return dict(getattr(cls, "__annotations__", {}))


def _literal_values(annotation: object) -> frozenset[str] | None:
    """从 ``Literal[...] | None`` / ``list[Literal[...]]`` 里抽出允许值集合。"""
    if typing.get_origin(annotation) is typing.Literal:
        return frozenset(str(v) for v in typing.get_args(annotation))
    for arg in typing.get_args(annotation):
        found = _literal_values(arg)
        if found is not None:
            return found
    return None


_CASE_SOURCE = Path(__file__).resolve().parent.parent / "src" / "models" / "preop" / "case.py"
"""字段是否写了说明，只能从源码读 —— 见 ``test_every_field_is_documented``。"""


def _domain_annotations_in_source() -> list[tuple[str, str, int, int]]:
    """``[(类名, 字段名, 起始行, 结束行)]``，行号从 1 起。"""
    tree = ast.parse(_CASE_SOURCE.read_text(encoding="utf-8"))
    out: list[tuple[str, str, int, int]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name.endswith("Domain"):
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    out.append(
                        (node.name, stmt.target.id, stmt.lineno, stmt.end_lineno or stmt.lineno)
                    )
    return out


def _all_domain_fields_in_source() -> list[tuple[str, str]]:
    return [(cls, name) for cls, name, _, _ in _domain_annotations_in_source()]


def _fields_without_comment() -> list[tuple[str, str]]:
    """返回没有行内注释的字段（注释可能落在跨行注解的**任意一行**上）。"""
    lines = _CASE_SOURCE.read_text(encoding="utf-8").splitlines()
    out: list[tuple[str, str]] = []
    for cls, name, start, end in _domain_annotations_in_source():
        if not any("#" in lines[i - 1] for i in range(start, end + 1)):
            out.append((cls, name))
    return out


# ===========================================================================
# 一、字段清单
# ===========================================================================
class TestFieldInventory:
    def test_total_is_161(self) -> None:
        assert TOTAL_FIELD_COUNT == 161

    def test_domain_counts_match_declaration(self) -> None:
        """独立数一遍：自检表说多少，实际就得是多少。"""
        actual = {k: len(v.model_fields) for k, v in _domain_models().items()}
        assert actual == DOMAIN_FIELD_COUNTS

    @pytest.mark.parametrize(("domain", "expected"), sorted(DOMAIN_FIELD_COUNTS.items()))
    def test_each_domain_count(self, domain: str, expected: int) -> None:
        assert len(_domain_models()[domain].model_fields) == expected

    def test_fourteen_domains(self) -> None:
        assert len(DOMAIN_FIELD_COUNTS) == 14

    def test_domain_counts_sum_to_total(self) -> None:
        assert sum(DOMAIN_FIELD_COUNTS.values()) == TOTAL_FIELD_COUNT

    def test_no_legacy_flat_fields_return(self) -> None:
        leaked = LEGACY_FLAT_FIELDS & set(VascCase.model_fields)
        assert not leaked, f"旧版平铺字段回流：{sorted(leaked)}"

    def test_only_four_non_domain_fields(self) -> None:
        assert set(VascCase.model_fields) - set(DOMAIN_FIELD_COUNTS) == NON_DOMAIN_FIELDS

    def test_every_field_is_documented(self) -> None:
        """每个字段都要有一句话说明，且必须**写在源码里**。

        为什么用行内注释而不是 ``Field(description=...)``：161 个字段逐个包一层
        ``Field`` 会把文件撑成两倍长，而当前阶段注释的唯一消费者是**读代码的人**。
        代价是运行时读不到（``model_json_schema()`` 里没有），所以这里用 AST 直接
        读源码兜住：将来要接 UI 表单或抽取层 prompt 自动生成时，得先升级成
        ``Field(description=...)``，这条测试会强制那次升级被看见。
        """
        missing = sorted(f"{cls}::{field}" for cls, field in _fields_without_comment())
        assert not missing, f"缺说明：{missing}"

    def test_documented_field_count_matches_model(self) -> None:
        """注释检查本身不能失效：解析出来的字段数必须等于 161。"""
        assert len(_fields_without_comment()) <= TOTAL_FIELD_COUNT
        assert len(_all_domain_fields_in_source()) == TOTAL_FIELD_COUNT

    def test_every_field_defaults_to_unmentioned(self) -> None:
        """默认必须是「未提及」——不能默认成阴性/正常，否则等于凭空造事实。"""
        wrong: list[str] = []
        for domain, cls in _domain_models().items():
            for name, info in cls.model_fields.items():
                if info.is_required():
                    wrong.append(f"{domain}.{name}：必填")
                elif info.get_default(call_default_factory=True) not in (None, []):
                    wrong.append(f"{domain}.{name}：默认 {info.get_default()!r}")
        assert not wrong, wrong


# ===========================================================================
# 二、不确定值语义（F4）
# ===========================================================================
class TestUncertainSemantics:
    @pytest.mark.parametrize("token", sorted(UNCERTAIN_TOKENS))
    def test_whitelist_members_are_uncertain(self, token: str) -> None:
        assert is_uncertain(token) is True

    @pytest.mark.parametrize(
        "token",
        ["未见", "无", "阴性", "未受累", "通畅", "无当前症状", "明确未见", "正常"],
    )
    def test_known_negatives_are_not_uncertain(self, token: str) -> None:
        """已知阴性 ≠ 不确定。混进白名单会把「明确没有」误判成 UNKNOWN。"""
        assert is_uncertain(token) is False

    def test_none_counts_as_uncertain(self) -> None:
        """``None`` 是「未提及」——同样不能拿去支撑任何结论，归入不确定。"""
        assert is_uncertain(None) is True

    @pytest.mark.parametrize("value", [0, True, 3.5, 0.0])
    def test_non_string_values_are_not_uncertain(self, value: object) -> None:
        assert is_uncertain(value) is False

    def test_whitelist_is_finite_and_curated(self) -> None:
        assert isinstance(UNCERTAIN_TOKENS, frozenset)
        assert 5 <= len(UNCERTAIN_TOKENS) <= 30


# ===========================================================================
# 三、严格性
# ===========================================================================
class TestStrictness:
    def test_unknown_top_level_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VascCase(case_id="X", age=70)  # type: ignore[call-arg]

    def test_unknown_domain_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VascCase(case_id="X", n={"neck_length": 25.0})  # type: ignore[arg-type]

    def test_invalid_literal_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VascCase(case_id="X", p={"sex": "男性"})  # type: ignore[arg-type]

    def test_partial_domain_keeps_other_defaults(self) -> None:
        case = VascCase(case_id="X", n={"neck_len": 25.0})
        assert case.n.neck_len == 25.0
        assert case.n.renal_l is None  # 未提及，不是「未受累」

    def test_string_numbers_are_coerced(self) -> None:
        case = VascCase(case_id="X", v={"sbp": "118.5"})  # type: ignore[dict-item]
        assert case.v.sbp == pytest.approx(118.5)

    def test_non_numeric_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VascCase(case_id="X", v={"sbp": "约一百二"})  # type: ignore[dict-item]

    def test_every_domain_forbids_extra(self) -> None:
        for domain, cls in _domain_models().items():
            assert cls.model_config.get("extra") == "forbid", f"{domain} 未禁止未知字段"


# ===========================================================================
# 四、枚举注解（W4：字段专属用 Literal）
# ===========================================================================
class TestLiteralEnums:
    def _result_field(self, name: str) -> object:
        return _domain_models()["r"].model_fields[name].annotation

    def test_next_node_has_five_exits(self) -> None:
        """Q1 裁定：五出口；规格原文「允许值」列只写了三个，备注又写五出口。"""
        assert _literal_values(self._result_field("next")) == frozenset(
            {"G05", "G06", "G07", "人工复核", "资料核实"}
        )

    def test_aortic_emergency_levels(self) -> None:
        assert _literal_values(self._result_field("aortic_emergency")) == frozenset(
            {"明确危险", "高度疑似", "不能排除", "未触发", "冲突"}
        )

    def test_extra_detail_domains_are_present(self) -> None:
        """GEN. / INF. 是 V3.1(2) 新增的证据明细层：规则不直接读，供 N.*_CTX 派生。"""
        assert {"gen", "inf"} <= set(DOMAIN_FIELD_COUNTS)

    def test_uncertain_values_can_be_entered(self) -> None:
        """不确定值必须能**装进模型**（有值但值本身不确定），不能因为不确定就禁止录入。"""
        texts = " ".join(
            str(f) for cls in _domain_models().values() for f in _annotations(cls).values()
        )
        for token in ("疑似", "可疑", "未评价", "不可评价"):
            assert token in texts, f"没有任何字段允许录入「{token}」"

    def test_route_mode_is_a_str_enum_with_three_values(self) -> None:
        """``RouteMode`` 是跨字段复用的枚举（W4：只有字段专属才用 ``Literal``）。

        它定义在 ``recommendation.py``（``FinalRecommendation.route_mode`` 的取值域），
        不在任何「共享枚举文件」里——那种容器在 2026-10-26 已撤销。三个值是规格的
        全部模式出口（G05 / G06 / G07），少一个就有一条模式走不通。
        """
        assert issubclass(RouteMode, StrEnum)
        assert {m.value for m in RouteMode} == {"急诊模式", "复杂模式", "指南模式"}


# ===========================================================================
# 五、审计留痕
# ===========================================================================
class TestAuditProvenance:
    def test_field_evidence_defaults_empty(self) -> None:
        assert VascCase(case_id="X").field_evidence == {}

    def test_field_evidence_uses_dotted_path(self) -> None:
        case = make_case(
            field_evidence={
                "n.neck_len": FieldProvenance(source_quote="瘤颈长 25mm", writer=FieldWriter.LLM)
            }
        )
        assert case.field_evidence["n.neck_len"].source_quote == "瘤颈长 25mm"

    def test_field_provenance_requires_writer(self) -> None:
        with pytest.raises(ValidationError):
            FieldProvenance(source_quote="无出处")  # type: ignore[call-arg]

    def test_field_provenance_forbids_extra(self) -> None:
        with pytest.raises(ValidationError):
            FieldProvenance(writer=FieldWriter.LLM, unknown_key=1)  # type: ignore[call-arg]

    def test_urgency_flag_defaults_false(self) -> None:
        assert VascCase(case_id="X").urgency_flag is False

    def test_urgency_flag_audit_records_three_things(self) -> None:
        """O01 审计要求：置位者 + 时间 + 理由，缺一不可。"""
        audit = UrgencyFlagAudit(
            set_by=FieldWriter.HUMAN,
            set_at="2026-10-26T09:00:00+08:00",
            reason="门诊初筛标记",
        )
        assert audit.set_by is FieldWriter.HUMAN
        assert audit.set_at.startswith("2026-10-26")
        assert audit.reason


# ===========================================================================
# 六、回填（apply_route_result / derive）
# ===========================================================================
class TestApplyRouteResult:
    def test_derive_fills_result_and_check_domains(self) -> None:
        case = derive(make_case())
        assert case.r.next == "G07"
        assert case.r.aortic_emergency == "未触发"
        assert case.k.classifiable == "确定"
        assert case.k.class_output == ["D1"]

    def test_derive_returns_same_instance(self) -> None:
        case = make_case()
        assert derive(case) is case

    def test_derive_is_idempotent(self) -> None:
        """回填只搬结论、不重新判定；跑两次结果必须一样。"""
        case = make_case()
        derive(case)
        first = (case.r.next, list(case.k.class_output or []))
        derive(case)
        assert (case.r.next, list(case.k.class_output or [])) == first

    def test_derive_leaves_inputs_untouched(self) -> None:
        case = make_case()
        before = case.model_dump(exclude={"r", "k"})
        derive(case)
        assert case.model_dump(exclude={"r", "k"}) == before


# ===========================================================================
# 七、进哈希的模型不得含时间戳（D-16）
# ===========================================================================
class TestHashSafety:
    def test_no_datetime_typed_field(self) -> None:
        """``audit_trail`` 被刻意剔除出哈希，所以模型里只要有 datetime，就会让
        「同输入不同哈希」。时间一律用 ISO 字符串（``t.now`` / ``set_at``），
        格式化交给渲染层。"""
        offenders = [
            f"{domain}.{name}: {annotation}"
            for domain, cls in _domain_models().items()
            for name, annotation in _annotations(cls).items()
            if "datetime.datetime" in str(annotation) or "datetime.date" in str(annotation)
        ]
        assert not offenders, offenders

    def test_no_measured_at_like_field(self) -> None:
        names = {n for cls in _domain_models().values() for n in cls.model_fields}
        assert "measured_at" not in names
        assert "created_at" not in names

    def test_case_is_round_trippable(self) -> None:
        case = make_case()
        assert VascCase.model_validate(case.model_dump()) == case


# ===========================================================================
# 八、命名
# ===========================================================================
class TestDomainNaming:
    def test_domain_keys_are_ascii_and_short(self) -> None:
        for key in DOMAIN_FIELD_COUNTS:
            assert key.isascii()
            assert len(key) <= 4

    def test_lab_domain_is_not_l(self) -> None:
        """``l`` 会触发 ruff E741（ambiguous variable name），统一用 ``lab``。"""
        assert "lab" in DOMAIN_FIELD_COUNTS
        assert "l" not in DOMAIN_FIELD_COUNTS

    def test_inflammation_markers_spelling_matches_spec(self) -> None:
        """源表 G04!37 写的是 ``INF.INFLAMM_MARKERS``（两个 M 后直接下划线）。"""
        assert "inflamm_markers" in _domain_models()["inf"].model_fields


# ===========================================================================
# 九、规则版本契约
# ===========================================================================
class TestVersionContract:
    """版本串进审计哈希，历史结论靠它复现 —— 所以它必须是「主动确认」的。"""

    def test_ruleset_version_is_pinned(self) -> None:
        """硬编码版本串：改了规则就必须显式改这里。

        这不是重复劳动 —— 它的作用是让「版本变了」成为一个需要人确认的动作，
        而不是顺手改一个常量就悄悄过关。
        """
        assert RULESET_VERSION == "v362@v3.6.2"

    def test_spec_version_appears_in_ruleset_version(self) -> None:
        assert SPEC_VERSION in RULESET_VERSION

    def test_current_version_has_a_changelog_entry(self) -> None:
        """bump 了却没写变更日志 = 后人无从知道「为什么变」。"""
        assert RULESET_VERSION in RULESET_CHANGELOG
        assert RULESET_CHANGELOG[RULESET_VERSION].strip()
