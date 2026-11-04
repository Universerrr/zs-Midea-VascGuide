"""V3.6.2 术前字段模型 —— **164 字段 / 14 个按域子模型**。

口径（对着 V3.6.2 工作簿逐条数出来，不是估算）::

    172（V3.6.2 字段 ID 去重）
      − 3  判定产物：N.COMPLEX_STATUS、CL.TAAA_IV、R.CLINICAL_AORTIC_SUSPECT
      − 3  物化派生：L.HB_DELTA、V.NEWS2_SCORE、V.NEWS2_SINGLE3
      − 1  并入同名上位字段：CL.FISTULA_TYPE→CL.FISTULA（状态另有 N.FISTULA_CTX）
      − 2  归病种通道：INF.IGG4、INF.PET_MRI（D5.3 专用，走 ``node_inputs``）
      + 1  补入：N.ACCESS_TORTUOSITY（规格未单列，入路评估要用）
      = 164

**``S.GI_BLEED`` / ``S.LIMB`` 为什么必须是独立字段（2026-11-04 改）**：
规格把「状态」与「明细」分成两列（``S.GI_BLEED`` 五态 + ``S.GI_BLEED_TYPE`` 多选），
而在所有判据规则的「允许值」列里出现的是**状态**：``G03.R08`` 只写 ``S.GI_BLEED``，
``G03.R15`` 只写 ``S.GI_BLEED;S.LIMB``，``S.GI_BLEED_TYPE`` 仅由抽取层 ``G02.R04`` 读。
合并成一格时判据只能读明细，等于读错了列；更直接的后果是「疑似消化道出血」
无处安放（明细里没有这个取值），只能退化成「未记录」。

**``N.EVAR_IFU_STATUS`` 为什么不再并入 ``N.SEAL_IFU``（2026-11-04 改）**：
两者覆盖面不同——前者是整体适配（近远端密封 + 瘤颈 + 分支 + 髂股入路 + 组件兼容），
后者只管近端密封区。规格在 ``N.SEAL_IFU`` 的判据列写死了「**只代表近端密封项，
不代表整体 EVAR 适配**」。合并后 D2.3 / D3.2 共 12 条规则在按规格判「整体」，
实际拿到的却是「近端」，会把「近端合格」渲染成「IFU 符合」。故拆为独立字段。

**为什么判定产物不进模型**：它们是规则算出来的，不是抽取出来的病例事实。
放进来会让「输入」与「输出」混在一处，也会让字段数随规则变化而漂移。
它们是 ``RouteResult`` 的字段，由需要的一方从 ``route_result`` 取。

**为什么病种域字段不进模型**：``D1.*`` / ``D3.*`` 是专科评估时医生填的记录，
不影响路由，也不该进路由哈希；它们走 ``ToolRequest.node_inputs`` 通道。
详见 :class:`~src.tools.guideline_engine.state.GuidelineState`。

**落点纪律（W12）**：14 个子模型类与组装类 :class:`VascCase` 全部写在**本文件**，
不新建 ``v31/`` 子包；导入路径 ``from src.models.preop import VascCase`` 全程不变。

三条语义约定（写规则时照此实现）
--------------------------------
1. **缺失 ≠ 阴性**：未提及一律 ``None``。禁止用 ``False`` / ``"无"`` 表达「没记录」。
2. **不确定值**是「有值但值本身不确定」（可疑 / 疑似 / 未评价 / 不可评价 / 未知…），
   由 :data:`UNCERTAIN_TOKENS` 显式枚举；判定层只能把它算成 UNKNOWN，**永不产生成立/不成立**。
3. **有阈值的判断存原值**（颈长、Hb、SBP、eGFR…），阈值留在规则层；
   只有「本身无阈值」的事实才用布尔 / 三态枚举。反例 ``S.PAIN_SEVERE_SCREEN`` 保留但**仅展示**。

非域字段（不计入 164）
----------------------
``case_id``（系统身份）、``urgency_flag``（人工覆盖通道）、``urgency_flag_audit``（留痕）、
``field_evidence``（字段级证据链）挂在 :class:`VascCase` 上，属于模型级的横切属性。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..audit import FieldProvenance, UrgencyFlagAudit

if TYPE_CHECKING:  # 仅类型检查用，运行期延迟导入（见 derive()）
    from ...rules.router import RouteResult

# ===========================================================================
# 复用的取值别名（字段专属取值直接写 Literal，跨字段复用的提为别名 —— W4）
# ===========================================================================
Tri = Literal["是", "否", "未知"]
"""通用三态：是与否之外必须有「未知」，不得缺省为否。"""

Involved = Literal["明确受累", "未受累", "可疑", "不可评价"]
"""分支开口受累（N.RENAL_R / RENAL_L / SMA / CA 共用）。"""

LesionGrade = Literal["明确", "可疑", "阴性", "不可评价"]
"""病变有无四态（CL.PAU / IMH / IAAD 共用）。"""

CtaGrade = Literal["明确", "可疑", "未见", "不可评价"]
"""CTA 影像征象四态（C.EXTRAV / HEMATOMA / WALL / CONTAIN / AEF_SIGN 共用）。"""

Label = Literal["D1", "D2", "D3", "D4", "D5", "D6", "D7", "Ⅳ型TAAA扩展"]
"""病种标签（G08 / K.CLASS_OUTPUT 共用）。"""

IfuStatus = Literal["符合", "不符合", "未核验", "无法评价", "未选择型号"]
"""IFU 核验结论五态 —— **逐字**取规格「允许值」列。

``N.SEAL_IFU``（``G04!11`` / ``D1.3!3``）与 ``N.EVAR_IFU_STATUS``（``D2.3!3`` /
``D3.2!3``）共用这一套取值，但**不是同一个字段**：

    ``N.SEAL_IFU``        只判**近端密封区**。规格 G 列末句：「只代表近端密封项，
                          **不代表整体 EVAR 适配**」。
    ``N.EVAR_IFU_STATUS`` 判**整体**适配：近远端密封、瘤颈、分支、髂股入路、
                          组件兼容与限制，且绑定品牌/型号/地区注册/IFU 版本。

为什么必须是两个字段而不是一个：整体符合 ⇒ 近端必然符合，反之不成立。合并后
「近端合格」会被当成「整体合格」，而 ``D2.3.R06`` 明令「IFU 缺失、地区不适用或
型号未指定 → 只标记『待核验候选』，**不得输出『符合 IFU』**」。
"""


# ===========================================================================
# 域基类
# ===========================================================================
class _StrictDomain(BaseModel):
    """14 个域子模型的共同基类：**禁止未知字段**。

    为什么必须有：pydantic 默认静默忽略多余键。抽取层把 ``n.neck_len`` 拼成
    ``n.neck_length`` 时，不会报错，只会**丢事实**——比报错危险得多，因为整条
    判定链会拿 ``None`` 继续跑，最后安静地给出一个「资料不足」的结论。
    加上 ``extra="forbid"`` 后，拼错立刻 ValidationError。

    放在基类上而不是逐个类声明，是为了让「严格」只有一个真源：新增域时想忘都忘不掉。
    """

    model_config = ConfigDict(extra="forbid")


# ===========================================================================
# P. 患者与就诊（4）
# ===========================================================================
class PatientDomain(_StrictDomain):
    """P. 患者身份与本次就诊（4 字段）。"""

    id: str | None = None  # 患者唯一标识（院内主索引；不得输出姓名）
    visit: str | None = None  # 本次就诊号（episode 唯一键）
    age: int | None = None  # 就诊年龄（≥0）
    sex: Literal["男", "女", "其他记录", "未知"] | None = None  # 记录的性别


# ===========================================================================
# T. 时间（1）
# ===========================================================================
class TimeDomain(_StrictDomain):
    """T. 决策时间（1 字段）。"""

    now: str | None = None  # 决策时间（ISO 8601：YYYY-MM-DDThh:mm:ss±hh:mm）


# ===========================================================================
# D. 原始来源与引用（11）
# ===========================================================================
class SourceDomain(_StrictDomain):
    """D. 原始来源与引用（11 字段）。只存证据指针，不存正文的临床结论。"""

    note: str | None = None  # 病历原文证据（文书ID + 段落定位）
    vital: list[str] | None = None  # 生命体征记录组（事件列表）
    lab: list[str] | None = None  # 检验原始记录组（样本ID + 结果列表）
    cta: str | None = None  # CTA DICOM 索引（StudyUID + SeriesUID，不存影像）
    report: str | None = None  # 影像报告原文（报告ID + 段落定位）
    surg: list[str] | None = None  # 主动脉既往治疗（无/EVAR/OSR/未知）
    med: list[str] | None = None  # 当前用药（名称 + 剂量 + 时间）
    intervention: list[str] | None = None  # 复苏与支持记录（类型 + 时间 + 剂量）
    dx: Literal["肯定", "疑似", "已排除", "既往"] | None = None  # 已知主动脉疾病
    allergy: Literal["有", "无明确记录", "未知"] | None = None  # 造影剂相关反应史
    dx_context: Literal["确认", "既往已修复", "疑似", "无相关记录", "未知"] | None = (
        None  # AAA 证据来源与确定度（确认/既往已修复/疑似/无相关记录/未知）
    )


# ===========================================================================
# Q. 证据质量（16）
# ===========================================================================
class EvidenceDomain(_StrictDomain):
    """Q. 证据质量与冲突（16 字段）。

    ⚠️ ``value_status`` 只是**逐项展示派生**，判定层不读（D3 裁定）：
    状态的真源是各字段自身的枚举，不是这张 map。
    """

    idmatch: Literal["一致", "不一致", "无法证明"] | None = None  # 患者-检查身份一致
    visitmatch: Literal["本次", "既往", "外院", "他人", "不一致", "未知"] | None = (
        None  # 本次就诊关联（本次/既往/外院/他人/不一致）
    )
    time: Literal["有效", "错序", "缺失", "冲突"] | None = None  # 时间戳可靠性
    unit: Literal["有效", "可转换", "冲突", "无单位"] | None = None  # 单位核对
    neg: Literal["肯定", "否定", "不确定", "未述及"] | None = None  # 否定状态
    history: Literal["当前", "既往", "计划", "无法判别"] | None = None  # 既往与当前
    experiencer: Literal["本人当前", "本人既往", "家族", "他人", "未知"] | None = (
        None  # 信息归属者（本人当前/本人既往/家族/他人）
    )
    conflict: list[Literal["无", "数值", "身份", "语义", "时间"]] | None = None  # 多来源冲突
    cta_phase: Literal["动脉期", "平扫", "其他", "未知"] | None = (
        None  # CTA 增强时相（动脉期才可评价某些征象）
    )
    cta_quality: Literal["可评价", "局部可评价", "不可评价"] | None = None  # 影像整体可评价性
    datasource: str | None = None  # 每项事实的证据指针（Q.DATASOURCE）
    value_status: Literal["阳性", "阴性", "可疑", "未知", "未检查", "不可评价", "冲突"] | None = (
        None  # 逐项值状态：仅展示派生，判定层不读（D3 裁定）
    )
    current_assess: Literal["本次已评估", "未评估", "仅历史", "未知"] | None = (
        None  # 本次急症筛查的时间口径（本次已评估/仅历史）
    )
    cta_cover: Literal["完整", "局部", "未覆盖", "未知"] | None = None  # 病变与周围组织覆盖
    cta_enhance: Literal["适当", "不足", "无动脉期", "未知"] | None = None  # 动脉期增强质量
    report_vs_algo: Literal["一致", "冲突", "仅报告", "仅算法", "均无", "未知"] | None = (
        None  # 影像报告与算法结论是否一致
    )


# ===========================================================================
# S. 症状与查体（20）
# ===========================================================================
class SymptomDomain(_StrictDomain):
    """S. 症状与查体（20 字段）。"""

    pain_present: Literal["是", "否", "可疑", "未知"] | None = None  # 当前疼痛存在
    pain_site: (
        list[Literal["上腹", "脐周", "下腹", "侧腹", "腰", "背", "胸", "腹股沟", "其他"]] | None
    ) = None  # 疼痛部位（可多选）
    pain_onset: Literal["突然", "逐渐", "不详"] | None = None  # 疼痛起病方式
    pain_start: str | None = None  # 疼痛开始时间（绝对/相对 + 时间锚点）
    pain_duration: float | None = None  # 疼痛持续时间（小时，≥0）
    pain_severity: int | None = None  # 疼痛强度 NRS 0–10（主观原词归 D.NOTE）
    pain_trend: Literal["持续", "加重", "缓解", "间歇", "复发", "未知"] | None = (
        None  # 疼痛变化趋势
    )
    pain_radiate: list[str] | None = None  # 疼痛放射部位
    pain_attrib: Literal["明确相关", "怀疑相关", "明确其他原因", "未判断"] | None = (
        None  # 医师判断疼痛与 AAA 的相关性（未判断 ≠ 无关）
    )
    syncope: Literal["肯定", "否定", "可疑", "未知"] | None = None  # 晕厥或短暂意识丧失
    gi_bleed: Literal["是", "否", "疑似", "未知", "证据冲突"] | None = (
        None  # 本次是否存在消化道出血（S.GI_BLEED，规格原文五态）
    )
    gi_bleed_type: list[Literal["呕血", "黑便", "便血", "其他出血表现", "无", "未知"]] | None = (
        None  # 出血表现类型（S.GI_BLEED_TYPE，可多选；「无」为已知阴性）
    )
    limb: Literal["是", "否", "疑似", "未评估", "证据冲突"] | None = (
        None  # 本次是否存在急性肢体缺血/外周栓塞表现（S.LIMB，规格原文五态）
    )
    limb_signs: (
        list[Literal["疼痛", "苍白", "发凉", "麻木", "无力", "运动障碍", "其他", "无", "未知"]]
        | None
    ) = None  # 急性肢体缺血具体体征（S.LIMB_SIGNS，可多选；「无」为已知阴性）
    pulse_deficit: Literal["有", "减弱", "缺失", "无法评价", "未查"] | None = (
        None  # 外周搏动检查结果
    )
    infection: list[Literal["存在", "无", "可疑", "未知"]] | None = None  # 感染症状与证据
    abd_tender: Literal["阳性", "阴性", "未查", "不可靠"] | None = None  # 腹部触痛/搏动性肿块
    pain_current: Literal["当前新发", "本次进展", "长期不变", "明确无", "未知"] | None = (
        None  # 疼痛是否属本次新发/进展（未提及 ≠ 无）
    )
    aaa_tender: Literal["明确阳性", "阴性", "未查", "不确定"] | None = None  # AAA 部位触痛
    pain_severe_screen: bool | None = None  # 疼痛是否达明显严重（**仅展示，判定不读**）


# ===========================================================================
# V. 生命体征（17）
# ===========================================================================
class VitalDomain(_StrictDomain):
    """V. 生命体征（17 字段）。NEWS2 总分为物化派生，不在此处（见 NodeAudit）。"""

    sbp: float | None = None  # 收缩压 mmHg
    dbp: float | None = None  # 舒张压 mmHg
    hr: float | None = None  # 心率 次/分
    rr: float | None = None  # 呼吸频率 次/分
    spo2: float | None = None  # 脉氧 %
    oxygen: Literal["是", "否", "未知"] | None = None  # 是否补充氧气
    spo2_scale: Literal["Scale1", "Scale2经确认", "未定"] | None = None  # NEWS2 血氧量表
    temp: float | None = None  # 体温 ℃
    acvpu: Literal["A", "C", "V", "P", "U", "无法评价"] | None = None  # 意识 ACVPU
    conscious_change: Literal["新发下降", "无新变化", "基线未知"] | None = (
        None  # 意识变化（新发下降才参与急症判据）
    )
    perfusion: list[Literal["阳性", "阴性", "未查"]] | None = None  # 低灌注表现
    urine: float | None = None  # 尿量 ml/h
    vaso: list[str] | None = None  # 血管活性药（用药列表）
    resus: list[str] | None = None  # 复苏相关干预（时间 + 治疗量）
    sbp_prev: float | None = None  # 先前可比收缩压 mmHg
    sbp_min: float | None = None  # 就诊期最低收缩压 mmHg
    hr_prev: float | None = None  # 先前可比心率 次/分


# ===========================================================================
# L. 实验室（11）
# ===========================================================================
class LabDomain(_StrictDomain):
    """L. 实验室（11 字段）。Hb 变化量 L.HB_DELTA 为物化派生，不在此处。"""

    hb: float | None = None  # 当前血红蛋白 g/L
    hb_ref_low: float | None = None  # Hb 实验室参考下限 g/L
    hb_prev: float | None = None  # 既往可比血红蛋白 g/L
    hb_comparable: bool | None = None  # 两次 Hb 是否有足够可比性（抽取层事实）
    hb_confound: bool | None = None  # Hb 变化有无已知治疗/采样干扰（抽取层事实）
    lact: float | None = None  # 乳酸 mmol/L
    lact_uln: float | None = None  # 乳酸参考上限 mmol/L
    creat: float | None = None  # 肌酐 µmol/L（**不用于反算 eGFR**）
    inr: float | None = None  # INR
    plt: float | None = None  # 血小板 ×10⁹/L
    blood_trans: Literal["存在", "无记录", "未知"] | None = None  # 输血与采血关系


# ===========================================================================
# C. CTA 与影像（19）
# ===========================================================================
class CtaDomain(_StrictDomain):
    """C. CTA 报告与影像征象（20 字段）。

    ⚠️ ``maxd`` 是**报告文字里读到的最大外径**，与量化值 ``CL.AAA_DIAM`` 不是一回事：
    前者只用于交叉校验（不一致 → ``Q.CONFLICT`` → G09），**修复阈值只认后者**。
    """

    aorta: list[str] | None = None  # 主动脉病变存在及部位（部位 + 病变）
    maxd: float | None = None  # 报告最大外径 mm（不可测 → None）
    extrav: CtaGrade | None = None  # 血管外造影剂外溢
    hematoma: CtaGrade | None = None  # 主动脉周围/腹膜后血肿
    wall: CtaGrade | None = None  # 主动脉壁完整性
    contain: CtaGrade | None = None  # 包裹性破裂征象
    stranding: Literal["有", "无", "可疑", "不可评价"] | None = None  # 周围脂肪模糊
    rup_context: Literal["有关", "无关", "不确定"] | None = None  # 影像破裂关联性
    branch: dict[str, str] | None = None  # 分支灌注（部位 → 通畅/狭窄/闭塞/可疑/不可评价）
    malperfusion: (
        list[Literal["肾", "肠系膜", "下肢", "其他器官", "无", "疑似", "未知", "证据冲突"]] | None
    ) = None  # 器官/肢体灌注障碍（C.MALPERFUSION）；分支解剖异常不得单独确认，见 router 物化
    graft: Literal["有", "无", "可疑", "未知"] | None = None  # 已有主动脉移植物
    endoleak: Literal["I", "II", "III", "其他", "可疑", "未评价"] | None = None  # 术后内漏可疑
    graft_fail: list[Literal["明确", "可疑", "未见", "不可评价"]] | None = None  # 移植物结构异常
    aef_sign: CtaGrade | None = None  # 主动脉肠瘘相关影像线索
    image_location: Literal["完整", "缺失"] | None = None  # 影像证据定位
    extrav_contig: Literal["明确连续", "可疑", "不连续", "未评价"] | None = None  # 外溢与管腔连续
    hematoma_adj: Literal["明确相邻", "可疑", "不相邻", "未评价"] | None = None  # 血肿紧邻病变
    hematoma_type: str | None = None  # 血肿位置 + 密度（结构化描述）
    rupture_report: Literal["明确破裂", "疑似破裂", "未提及", "明确未见"] | None = (
        None  # 医师报告的破裂诊断（「明确破裂」即阳性触发）
    )
    source_kind: Literal["正式报告", "专家确认", "算法候选", "无", "未知"] | None = (
        None  # CTA 征象证据类型（正式报告/专家确认/算法候选）
    )


# ===========================================================================
# N. 解剖与病生理（28）
# ===========================================================================
class AnatomyDomain(_StrictDomain):
    """N. 解剖、合并症与病因状态（28 字段）。N.COMPLEX_STATUS 为判定产物，不在此处。"""

    cta_status: Literal["完整", "部分", "缺失", "冲突"] | None = None  # 判断复杂解剖的 CTA
    neck_len: float | None = None  # 肾下瘤颈长度 mm
    renal_r: Involved | None = None  # 右肾动脉开口受累
    renal_l: Involved | None = None  # 左肾动脉开口受累
    sma: Involved | None = None  # SMA 开口受累
    ca: Involved | None = None  # 腹腔干开口受累
    thoracic: Literal["是", "否", "不可评价"] | None = None  # 是否明显累及降胸主动脉
    neck_d: float | None = None  # 近端瘤颈最大直径 mm
    neck_angle: float | None = None  # 近端瘤颈角度 度（0–180）
    seal_ifu: IfuStatus | None = None  # N.SEAL_IFU：**仅近端密封区**是否符合 IFU
    evar_ifu_status: IfuStatus | None = (
        None  # N.EVAR_IFU_STATUS：**整体** EVAR 器械适配（含髂股入路/组件兼容）
    )
    iliac_bilat: Literal["双侧", "单侧", "无", "不详"] | None = None  # 双侧髂动脉瘤/重要累及
    iia_left: Literal["通畅", "闭塞", "严重狭窄", "未知"] | None = None  # 左内髂灌注
    iia_right: Literal["通畅", "闭塞", "严重狭窄", "未知"] | None = None  # 右内髂灌注
    access_occlusion: Literal["双侧闭塞", "单侧闭塞", "未见闭塞", "未知"] | None = (
        None  # 双侧常规髂股通路是否存在闭塞
    )
    access_tortuosity: Literal["双侧严重迂曲", "单侧严重迂曲", "未见严重迂曲", "未知"] | None = (
        None  # 双侧髂动脉入路迂曲（规格未单列；工具A 入路评估消费，路由层不读）
    )
    genetic_ctx: Literal["确诊", "疑似遗传背景", "无已知阳性线索", "未评价", "证据冲突"] | None = (
        None  # 遗传背景主状态（由 GEN.* 明细派生；不新增 D8，作病因修饰）
    )
    inflammatory_ctx: (
        Literal["临床确认", "影像高度提示", "可疑", "无当前支持证据", "未评价", "冲突"] | None
    ) = None  # 炎症性 AAA 主状态（由 INF.* 派生；单纯 CRP/ESR 高不触发）
    infection_ctx: Literal["医师明确", "高度怀疑", "仅非特异征象", "无相关征象", "未知"] | None = (
        None  # 感染性主动脉疾病主状态（与炎症性 AAA 分开，不互相覆盖）
    )
    fistula_ctx: Literal["明确", "疑似", "未见", "未知"] | None = None  # 主动脉相关瘘
    prev_repair: Literal["EVAR", "OSR", "其他", "无", "未知"] | None = None  # 既往修复
    endoleak_type: Literal["I", "II", "III", "IV", "V", "疑似", "无", "未知"] | None = (
        None  # 本次内漏类型（Ⅰ/Ⅱ/Ⅲ/Ⅳ/Ⅴ，与 C.ENDOLEAK 分工：此处为分类口径）
    )
    sac_growth: float | None = None  # 瘤囊增长 mm（**仅在可比时填写**，不可比 → None）
    graft_major: Literal["组件分离", "移位", "明显闭塞", "无", "疑似", "未知"] | None = (
        None  # 移植物结构重大异常（组件分离/移位/明显闭塞）
    )
    egfr: float | None = None  # 本次 eGFR（直读化验单，不由肌酐反算）
    dialysis: Tri | None = None  # 当前维持透析
    heart_class: Literal["I", "II", "III", "IV", "未评价"] | None = None  # NYHA 心功能分级
    resp_support: Literal["无", "长期吸氧", "无创通气", "有创通气", "未知"] | None = (
        None  # 基线呼吸支持依赖
    )


# ===========================================================================
# GEN. 遗传证据明细（5）—— 证据明细层，规则不直接读，供 N.GENETIC_CTX 派生
# ===========================================================================
class GeneticDomain(_StrictDomain):
    """GEN. 遗传证据明细（5 字段）。三层结构：证据明细 → 主状态 → 规则。"""

    syndrome_type: (
        Literal["Marfan", "Loeys-Dietz", "血管型EDS", "其他确诊综合征", "疑似", "不详"] | None
    ) = None  # 已记录的具体遗传综合征名称
    aaa_age_onset: int | None = None  # 首次确诊 AAA 年龄（0–120）
    family_aneurysm: Literal["阳性", "明确无相关史", "未询问", "不详"] | None = (
        None  # 家族中是否有动脉瘤或主动脉夹层（「未询问」≠ 阴性）
    )
    syndromic_features: list[str] | None = None  # 提示单基因综合征的体征
    variant_class: (
        Literal["致病", "可能致病", "意义未明(VUS)", "良性或可能良性", "未检测"] | None
    ) = None  # 已发现的遗传变异及致病性（VUS 不等同致病）


# ===========================================================================
# INF. 炎症证据明细（6）—— 同上，供 N.INFLAMMATORY_CTX 派生
# ===========================================================================
class InflammatoryDomain(_StrictDomain):
    """INF. 炎症性 AAA 证据明细（6 字段）。"""

    mantle_sign: Literal["明确存在", "可疑", "未见", "无法评价"] | None = None  # 瘤周套袖征
    wall_fibrosis: Literal["明确", "可疑", "未见", "不可评价"] | None = None  # 瘤壁增厚/纤维化
    organ_involvement: (
        list[
            Literal[
                "输尿管包绕",
                "肾积水",
                "十二指肠受累",
                "下腔静脉或左肾静脉受累",
                "无",
                "未知",
            ]
        ]
        | None
    ) = None  # 周围结构受累（输尿管包绕/肾积水/十二指肠等）
    inflamm_markers: str | None = None  # CRP/ESR 数值 + 参考上限 + 采样时间（源表拼写为 INFLAMM_）
    symptom_pattern: Literal["持续/反复腹背痛", "体重下降", "无", "未知"] | None = (
        None  # 慢性腹背侧痛及体重下降（炎症性 AAA 的支持线索）
    )
    aaa_true_diam: float | None = None  # 排除瘤周炎症与壁水肿后的真实直径 mm（ESVS Rec 145）


# ===========================================================================
# CL. 病种分类（18）
# ===========================================================================
class ClassifyDomain(_StrictDomain):
    """CL. 病种分类标签（18 字段）。CL.TAAA_IV 为判定产物，不在此处。"""

    aorta_location: list[Literal["腹主动脉", "髂动脉", "胸腹主动脉", "无", "未知"]] | None = (
        None  # 病变血管部位（可多选）
    )
    aaa_diam: float | None = None  # 腹主动脉最大外径 mm（**修复阈值只认此量化值**）
    aaa_reported: Literal["确认", "疑似", "否定", "未述及"] | None = (
        None  # 医生/影像报告是否明确诊断 AAA（与 C.MAXD 报告值配套）
    )
    aaa_rupture: Literal["明确", "疑似", "未见相关证据", "无法评价"] | None = (
        None  # 破裂或疑似破裂状态（CL. 侧分类口径，与 C.RUPTURE_REPORT 分工）
    )
    symptom_aaa: (
        Literal["医师确认相关", "医师怀疑相关", "明确归因其他", "未评价", "无当前症状"] | None
    ) = None  # 腹背痛等症状能否归因于 AAA（未评价 ≠ 无症状）
    infection: (
        Literal[
            "原生感染明确",
            "原生感染疑似",
            "移植物感染明确",
            "移植物感染疑似",
            "无证据",
            "未评价",
        ]
        | None
    ) = None  # 原生主动脉或移植物感染状态
    # 类型取规格原文五态；「明确/疑似」的状态在 N.FISTULA_CTX（``n.fistula_ctx``），
    # 这里保留「疑似」只为兼容旧输入（拆分见台账 B 项）。
    fistula: (
        Literal["主动脉肠瘘", "主动脉腔静脉瘘", "其他腔脏器瘘", "未确定", "疑似", "否", "未知"]
        | None
    ) = None  # 主动脉相关瘘的解剖类型
    pau: LesionGrade | None = None  # 穿透性主动脉溃疡
    imh: LesionGrade | None = None  # 主动脉壁内血肿
    iaad: LesionGrade | None = None  # 孤立性腹主动脉夹层
    saccular: Literal["明确", "疑似", "否", "未知"] | None = None  # 囊状主动脉瘤
    pseudo: Literal["明确", "疑似", "否", "未知"] | None = None  # 假性动脉瘤
    iliac_site: list[Literal["左侧", "右侧", "CIA", "IIA", "EIA", "疑似", "无"]] | None = (
        None  # 髂动脉瘤侧别与部位
    )
    iliac_diam: dict[str, float] | None = None  # 髂动脉各段外径（侧别 → mm）
    prev_evor: Literal["EVAR", "OSR", "其他", "无", "未知"] | None = (
        None  # 既往 EVAR 或 OSR 史（「无」为明确阴性）
    )
    post_reason: (
        Literal[
            "内漏",
            "瘤囊变化",
            "感染",
            "再狭窄或闭塞",
            "假性动脉瘤",
            "常规随访",
            "其他",
            "不详",
        ]
        | None
    ) = None  # 此次就诊是否评估术后并发症（无既往修复时为空，不算缺口）
    diag_quality: Literal["确认", "疑似", "未评价", "矛盾"] | None = None  # 标签证据等级
    diag_multi: list[Label] | None = None  # 本次疾病多标签集合


# ===========================================================================
# K. 分类检查（5）—— 由 route() 内部算出并回填（W11）
# ===========================================================================
class CheckDomain(_StrictDomain):
    """K. 分类期检查（5 字段）。

    ``new_urgent`` 是**入参**（分类期间是否出现新危险证据，由调用方置位）；
    其余 4 个由 ``route()`` 内部算出，经 :func:`apply_route_result` 回填，供审计复核。
    """

    new_urgent: Tri | None = None  # 分类期间是否出现新危险证据（route() 入参，由调用方置位）
    identity_conflict: Tri | None = None  # 参与分类的关键数据是否归属冲突（route() 算出后回填）
    diag_conflict: Tri | None = None  # 病种标签间是否存在实质冲突（route() 算出后回填）
    classifiable: Literal["确定", "疑似", "不能分类"] | None = (
        None  # 是否已形成至少一个病种候选标签（route() 算出后回填）
    )
    class_output: list[Label] | None = None  # 待交付病种标签集合


# ===========================================================================
# R. 结论（2）
# ===========================================================================
class ResultDomain(_StrictDomain):
    """R. 路由结论（2 字段）。R.CLINICAL_AORTIC_SUSPECT 为判定产物，不在此处。"""

    aortic_emergency: Literal["明确危险", "高度疑似", "不能排除", "未触发", "冲突"] | None = (
        None  # 主动脉急症风险分级（route() 算出后回填）
    )
    next: Literal["G05", "G06", "G07", "人工复核", "资料核实"] | None = None  # 下一节点


# ===========================================================================
# 组装
# ===========================================================================
class VascCase(BaseModel):
    """V3.6.2 术前病例 —— 164 个路由字段（14 个域子模型）+ 4 个非域字段。

    访问形式：``case.n.neck_len`` / ``case.cl.aaa_diam`` / ``case.p.age``。

    ⚠️ 本模型**只管输入**：工具产物（VascMeasurement / AnatomyScore /
    RiskAssessment / FinalRecommendation）各自独立成模型，**不回写** VascCase。
    """

    case_id: str = Field(description="系统病例号（审计身份，与 P.ID 患者标识不同）")

    model_config = ConfigDict(extra="forbid")
    """禁止未知字段：抽取层拼错字段名必须立刻报错，不能静默丢弃（否则等于丢事实）。"""

    p: PatientDomain = Field(default_factory=PatientDomain)
    t: TimeDomain = Field(default_factory=TimeDomain)
    d: SourceDomain = Field(default_factory=SourceDomain)
    q: EvidenceDomain = Field(default_factory=EvidenceDomain)
    s: SymptomDomain = Field(default_factory=SymptomDomain)
    v: VitalDomain = Field(default_factory=VitalDomain)
    lab: LabDomain = Field(default_factory=LabDomain)
    c: CtaDomain = Field(default_factory=CtaDomain)
    n: AnatomyDomain = Field(default_factory=AnatomyDomain)
    gen: GeneticDomain = Field(default_factory=GeneticDomain)
    inf: InflammatoryDomain = Field(default_factory=InflammatoryDomain)
    cl: ClassifyDomain = Field(default_factory=ClassifyDomain)
    k: CheckDomain = Field(default_factory=CheckDomain)
    r: ResultDomain = Field(default_factory=ResultDomain)

    # ---- 非域字段（不计入 164）----
    urgency_flag: bool = Field(
        default=False,
        description="人工置位：医生主动标记「本次必须按急症处理」（可越级进 G05）。",
    )
    urgency_flag_audit: UrgencyFlagAudit | None = Field(
        default=None, description="urgency_flag 的置位留痕（置位者 + 时间 + 理由）。"
    )
    field_evidence: dict[str, FieldProvenance] = Field(
        default_factory=dict, description="字段级证据链（键为域字段点路径，如 n.neck_len）。"
    )

    @classmethod
    def domain_field_names(cls) -> dict[str, list[str]]:
        """返回 ``{域键: [字段名…]}``，供计数断言与字段参与度探针使用。"""
        return {
            "p": list(PatientDomain.model_fields),
            "t": list(TimeDomain.model_fields),
            "d": list(SourceDomain.model_fields),
            "q": list(EvidenceDomain.model_fields),
            "s": list(SymptomDomain.model_fields),
            "v": list(VitalDomain.model_fields),
            "lab": list(LabDomain.model_fields),
            "c": list(CtaDomain.model_fields),
            "n": list(AnatomyDomain.model_fields),
            "gen": list(GeneticDomain.model_fields),
            "inf": list(InflammatoryDomain.model_fields),
            "cl": list(ClassifyDomain.model_fields),
            "k": list(CheckDomain.model_fields),
            "r": list(ResultDomain.model_fields),
        }


# 各域字段数（与 V3.6.2 一一对应；改动必须同步本文件顶部的口径算式）
DOMAIN_FIELD_COUNTS: dict[str, int] = {
    "p": 4,
    "t": 1,
    "d": 11,
    "q": 16,
    "s": 20,
    "v": 17,
    "lab": 11,
    "c": 20,
    "n": 28,
    "gen": 5,
    "inf": 6,
    "cl": 18,
    "k": 5,
    "r": 2,
}
TOTAL_FIELD_COUNT = sum(DOMAIN_FIELD_COUNTS.values())  # = 164


def _check_field_counts() -> None:
    """导入期自检：域字段数与声明值必须一致（防止改字段忘了改计数）。"""
    actual = {k: len(v) for k, v in VascCase.domain_field_names().items()}
    if actual != DOMAIN_FIELD_COUNTS:
        diff = {
            k: (DOMAIN_FIELD_COUNTS.get(k), n)
            for k, n in actual.items()
            if DOMAIN_FIELD_COUNTS.get(k) != n
        }
        raise RuntimeError(f"域字段数与 DOMAIN_FIELD_COUNTS 不一致：{diff}")
    if sum(actual.values()) != TOTAL_FIELD_COUNT:
        raise RuntimeError(f"字段总数应为 {TOTAL_FIELD_COUNT}，实际 {sum(actual.values())}")


_check_field_counts()


# ===========================================================================
# 不确定值白名单（F4）—— 「有值但值本身不确定」，判定层只能算成 UNKNOWN
# ===========================================================================
UNCERTAIN_TOKENS: frozenset[str] = frozenset(
    {
        "可疑",
        "疑似",
        "未评价",
        "不可评价",
        "未检查",
        "未描述",
        "无法评价",
        "无法证明",
        "不确定",
        "未知",
        "不详",
        "未核验",
        "未选择型号",
        "未提及",
        "证据冲突",
        "冲突",
    }
)
"""显式枚举的「不确定取值」。**禁止按关键词自动推断**（同一个「无」在不同字段语义相反）。

本集合只收「本身就不确定」的词；``未见`` / ``无`` / ``阴性`` / ``未受累`` / ``未触发``
都是**确定的阴性结论**，不在其中——混进来会把已知阴性误判成 UNKNOWN。
"""


def is_uncertain(value: object) -> bool:
    """判断一个字段值是否属于「不确定」。``None`` 也算不确定（缺失）。"""
    if value is None:
        return True
    token = getattr(value, "value", value)
    return isinstance(token, str) and token in UNCERTAIN_TOKENS


# ===========================================================================
# 判定结果回填（不重新判定，只搬运）
# ===========================================================================
def apply_route_result(case: VascCase, result: RouteResult) -> VascCase:
    """把 :class:`~src.rules.router.RouteResult` 的结论回填到 ``case``（**就地**）。

    ⚠️ 只做搬运：不重新判定、不读原文、不改阈值。
    ⚠️ 为什么独立成公开函数：编排层要「只跑一次 ``route()``，同时回填 case 与产出
    FinalRecommendation」；抽出来两处共用，避免抄第二份回填代码。

    回填范围（只写**模型里存在**的字段）：``r.next`` / ``r.aortic_emergency`` / ``k.*``。
    **``CL.TAAA_IV`` 不在回填之列** —— 它是判定产物，``VascCase`` 里没有对应字段，
    只存在于 ``RouteResult.cl_taaa_iv``，由需要它的工具从 ``route_result`` 直接读取。
    """
    case.r.next = result.next_node
    case.r.aortic_emergency = result.emergency_level
    case.k.identity_conflict = result.k_identity_conflict
    case.k.diag_conflict = result.k_diag_conflict
    case.k.classifiable = result.k_classifiable
    case.k.class_output = list(result.class_output) if result.class_output else None
    return case


def derive(case: VascCase) -> VascCase:
    """确定性派生入口：跑一遍 ``route()`` 并回填。

    ⚠️ 纯确定性代码，禁止引入任何 LLM 客户端（审计红线）。
    ⚠️ 真正的判定逻辑在 ``src/rules/router.py``，此处只做「执行 + 回填」。
    """
    # 延迟导入：避免 models ← rules ← models 的循环引用
    from ...rules.router import route

    return apply_route_result(case, route(case))


__all__ = [
    "DOMAIN_FIELD_COUNTS",
    "TOTAL_FIELD_COUNT",
    "UNCERTAIN_TOKENS",
    "VascCase",
    "apply_route_result",
    "derive",
    "is_uncertain",
]
