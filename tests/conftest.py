"""测试共享夹具 —— **唯一的病例构造入口**。

为什么要独立成 ``conftest.py``：V3.1(2) 重写前，``test_preop.py`` 与
``test_model_contract.py`` 各抄了一份 ``make_case``（后者的注释还写着
「与 test_preop.py 同构」）。两份一旦分叉，就会在测试层制造「同一事实两份真源」，
而测试恰恰是用来抓这种问题的。这里只留一份。

基线病例（``make_case()``）的设计目标：**恰好落在 G07 指南模式**，即

1. G03 的 21 条急诊规则**全部不命中** —— 无破裂影像征象、血压正常、
   无失血证据、无感染/瘘/内漏；且 ``G03.R15`` 放行 gate 成立（关键项均有值）。
2. G04 的 12 条阻断型规则**全部不命中** —— 颈长 25mm、无内脏段受累、
   无既往修复、无感染/内漏/Pau/liquefaction 类线索。
3. G04 的 4 条加分型（遗传/炎症）不命中，且因为它们返回 False 而非 UNKNOWN，
   不会把 ``G04.R14`` 的放行挡住。
4. ``G04.R13`` 不成立、``G04.R14`` 成立 → 进入 G07。

改用例时优先用 ``make_case(n={"neck_len": 8.0})`` 这种**单域覆盖**写法，
``n``/``cl``/``v`` 等域的键会与基线做浅合并，不必重抄整个域。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.models.preop.case import VascCase


def baseline_kwargs() -> dict[str, Any]:
    """返回基线病例的构造参数（可安全修改，每次调用都是新对象）。"""
    return {
        "case_id": "BASE-000",
        "p": {"id": "P-0001", "visit": "V-0001", "age": 72, "sex": "男"},
        "t": {"now": "2026-10-26T09:00:00+08:00"},
        "d": {
            "dx": "肯定",
            "dx_context": "确认",
            "note": "门诊病例",
            "vital": ["血压 120/70"],
            "lab": ["Hb 128 g/L"],
            "cta": "腹主动脉瘤，最大径 42mm",
            "report": "CTA 报告",
            "surg": [],
            "med": [],
            "intervention": [],
            "allergy": "无明确记录",
        },
        "q": {
            "idmatch": "一致",
            "visitmatch": "本次",
            "time": "有效",
            "unit": "有效",
            "neg": "肯定",
            "history": "当前",
            "experiencer": "本人当前",
            "conflict": ["无"],
            "cta_phase": "动脉期",
            "cta_quality": "可评价",
            "datasource": "报告值",
            "value_status": "阴性",
            "current_assess": "本次已评估",
            "cta_cover": "完整",
            "cta_enhance": "适当",
            "report_vs_algo": "仅报告",
        },
        "s": {
            "pain_present": "否",
            "pain_current": "明确无",
            "syncope": "否定",
            "gi_bleed": ["无"],
            "limb": ["无"],
            "pulse_deficit": "未查",
            "infection": ["无"],
            "abd_tender": "阴性",
            "aaa_tender": "阴性",
        },
        "v": {
            "sbp": 120.0,
            "dbp": 70.0,
            "hr": 78.0,
            "rr": 18.0,
            "spo2": 97.0,
            "oxygen": "否",
            "spo2_scale": "Scale1",
            "temp": 36.5,
            "acvpu": "A",
            "conscious_change": "无新变化",
            "perfusion": ["阴性"],
            "urine": 60.0,
            "vaso": [],
            "resus": [],
        },
        "lab": {
            "hb": 128.0,
            "hb_ref_low": 130.0,
            "hb_prev": 130.0,
            "hb_comparable": True,
            "hb_confound": False,
            "lact": 1.2,
            "lact_uln": 2.0,
            "creat": 80.0,
            "inr": 1.0,
            "plt": 220.0,
            "blood_trans": "无记录",
        },
        "c": {
            "aorta": ["腹主动脉瘤"],
            "maxd": 42.0,
            "extrav": "未见",
            "hematoma": "未见",
            "wall": "未见",
            "contain": "未见",
            "stranding": "无",
            "rup_context": "无关",
            "branch": {},
            "graft": "无",
            "endoleak": "未评价",
            "graft_fail": [],
            "aef_sign": "未见",
            "image_location": "完整",
            "extrav_contig": "不连续",
            "hematoma_adj": "不相邻",
            "rupture_report": "明确未见",
            "source_kind": "正式报告",
        },
        "n": {
            "cta_status": "完整",
            "neck_len": 25.0,
            "renal_r": "未受累",
            "renal_l": "未受累",
            "sma": "未受累",
            "ca": "未受累",
            "thoracic": "否",
            "neck_d": 22.0,
            "neck_angle": 20.0,
            "seal_ifu": "符合",
            "evar_ifu_status": "符合",
            "iliac_bilat": "无",
            "iia_left": "通畅",
            "iia_right": "通畅",
            "access_occlusion": "未见闭塞",
            "access_tortuosity": "未见严重迂曲",
            "genetic_ctx": "无已知阳性线索",
            "inflammatory_ctx": "无当前支持证据",
            "infection_ctx": "无相关征象",
            "fistula_ctx": "未见",
            "prev_repair": "无",
            "endoleak_type": "无",
            "sac_growth": 0.0,
            "graft_major": "无",
            "egfr": 85.0,
            "dialysis": "否",
            "heart_class": "II",
            "resp_support": "无",
        },
        "gen": {
            "syndrome_type": "不详",
            "aaa_age_onset": 68,
            "family_aneurysm": "明确无相关史",
            "syndromic_features": [],
            "variant_class": "未检测",
        },
        "inf": {
            "mantle_sign": "未见",
            "wall_fibrosis": "未见",
            "organ_involvement": ["无"],
            "inflamm_markers": "CRP 3 mg/L（参考上限 5）",
            "symptom_pattern": "无",
            "aaa_true_diam": 42.0,
        },
        "cl": {
            "aorta_location": ["腹主动脉"],
            "aaa_diam": 42.0,
            "aaa_reported": "确认",
            "aaa_rupture": "未见相关证据",
            "symptom_aaa": "无当前症状",
            "infection": "无证据",
            "fistula": "否",
            "pau": "阴性",
            "imh": "阴性",
            "iaad": "阴性",
            "saccular": "否",
            "pseudo": "否",
            "iliac_site": ["无"],
            "iliac_diam": {},
            "prev_evor": "无",
            "diag_quality": "确认",
            "diag_multi": ["D1"],
        },
        "k": {
            "new_urgent": "否",
            "identity_conflict": None,
            "diag_conflict": None,
            "classifiable": None,
            "class_output": None,
        },
        "r": {"aortic_emergency": None, "next": None},
    }


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """浅合并到域级：``{"n": {"neck_len": 8.0}}`` 只改一个键，其余保留基线。"""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def make_case(**overrides: Any) -> VascCase:
    """构造基线病例；``overrides`` 支持域级浅合并。"""
    return VascCase(**(_merge(baseline_kwargs(), overrides)))


@pytest.fixture
def baseline() -> VascCase:
    return make_case()


@pytest.fixture
def emergency_case() -> VascCase:
    """SBP 70 + 心率 118：直接命中 G03 的低血压规则（G05）。"""
    return make_case(case_id="BASE-EMERG", v={"sbp": 70.0, "hr": 118.0})


@pytest.fixture
def complex_case() -> VascCase:
    """短颈 8mm：命中 G04 阻断型规则（G06）。"""
    return make_case(case_id="BASE-COMPLEX", n={"neck_len": 8.0})


#: 规格工作簿的定位顺序 —— **仓库内优先**。
#:
#: 为什么要有这个常量：三条「源行对账」测试（router 66 条 / 病种 172 条 / M 列值域）
#: 都需要读工作簿。此前三处各硬编码了一份桌面绝对路径，规格一移进 ``docs/``
#: 就三处同时静默 skip —— 而对账测试一旦 skip，源行漂移就再也没有东西拦得住。
#: 统一在这里解析：先找仓库内副本，再退到旧的位置，都没有才 ``None``（调用方 skip）。
SPEC_XLSX: Path | None = next(
    (
        p
        for p in (
            Path(__file__).resolve().parents[1] / "docs" / "VascGuide_V3.6.2.xlsx",
            Path.home() / "Desktop" / "VascGuide_V3.6.2.xlsx",
        )
        if p.exists()
    ),
    None,
)
