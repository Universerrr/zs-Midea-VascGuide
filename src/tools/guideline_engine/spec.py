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
        # D1 诊断分层：症状/破裂/形态/直径；D3 破裂证据
        "case.cl.aaa_rupture",
        "case.cl.aaa_reported",
        "case.cl.aaa_diam",
        "case.cl.iliac_diam",
        "case.cl.symptom_aaa",
        "case.cl.saccular",
        "case.cl.pseudo",
        "case.p.sex",
        # 年龄**故意不声明**：D1.2 明写「不以年龄单项终止修复评估」，判据里
        # 一个字都不读 ``case.p.age``。声明一个从不读取的字段会让输入契约说谎。
        # D2 症状确认（S.PAIN_ATTRIB / AAA_TENDER / LIMB 的模型侧字段）
        "case.s.pain_present",
        "case.s.pain_attrib",
        "case.s.aaa_tender",
        "case.s.limb",
        # D3 破裂的影像证据；D2.1 的壁外出血判据（WALL / CONTAIN）
        "case.c.extrav",
        "case.c.hematoma",
        "case.c.wall",
        "case.c.contain",
        "case.c.rupture_report",
        # D3 患者状态
        "case.v.sbp",
        "case.v.conscious_change",
        # D1.3 / D3.2 解剖适配
        "case.n.cta_status",
        "case.n.neck_len",
        "case.n.neck_d",
        "case.n.neck_angle",
        "case.n.seal_ifu",
        "case.n.evar_ifu_status",
        "case.n.access_occlusion",
        "case.n.access_tortuosity",
        "case.n.renal_r",
        "case.n.renal_l",
        "case.n.sma",
        "case.n.ca",
        "case.n.thoracic",
        # 病因修饰（D1.1 遗传线索 / D1.2 遗传分支 / 特殊形态分流）
        "case.n.genetic_ctx",
        "case.n.inflammatory_ctx",
        "case.n.infection_ctx",
        "case.gen.aaa_age_onset",
        # D4 感染分型与瘘（CL.INFECTION / CL.FISTULA / N.PREV_REPAIR / C.GRAFT / C.AEF_SIGN）
        "case.cl.infection",
        "case.cl.fistula",
        "case.n.prev_repair",
        "case.c.graft",
        "case.c.aef_sign",
        "case.s.gi_bleed",
        # D5 特殊病变：壁结构病变（PAU/IMH/IAAD）、形态（囊状/假性）、器官灌注
        "case.cl.pau",
        "case.cl.imh",
        "case.cl.iaad",
        "case.c.malperfusion",
        # D5.3 炎症性 AAA：证据明细在病例模型里（INF.* 不另立通道）
        "case.inf.mantle_sign",
        "case.inf.wall_fibrosis",
        "case.inf.organ_involvement",
        "case.inf.aaa_true_diam",
        "case.inf.symptom_pattern",
        # D7 髂动脉瘤：侧别+血管段（两个独立取值）、逐段直径、内髂与入路状态
        "case.cl.iliac_site",
        "case.n.iliac_bilat",
        "case.n.iia_left",
        "case.n.iia_right",
        # D7.1.R02 判「当前是否并存未修复 AAA」：既往已修复不算（规格原话）
        "case.d.dx_context",
        # D6 术后：内漏分型、移植物失败、瘤囊增长、分支灌注、术后原因字段
        # （``post_reason`` 是单值枚举，规格写成多选 —— 见 D6 模块文档）
        "case.cl.post_reason",
        "case.cl.prev_evor",
        "case.n.endoleak_type",
        "case.n.graft_major",
        "case.n.sac_growth",
        "case.c.branch",
    ),
    produces="病种层结论 + 缺口 + 决策路径 + 术式候选（GuidelineReport）",
    depends_on=(),
    blocked_reason=None,
)
