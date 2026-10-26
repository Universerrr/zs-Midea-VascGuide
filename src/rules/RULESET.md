# RULESET —— 54 条判定规则 ↔ 函数 ↔ V3.1(2) 源行

> **这是规格的第二真源。** 代码是规格的实现，本表是二者的对照凭据。
> 改规则必须同时改三处：`router.py` 的函数、本表对应行、`RULESET_VERSION` 与 `RULESET_CHANGELOG`。
>
> 机械守门（`tests/test_router_branches.py`）：
> `len(RULES) == 54`；编号按域连续；每条 `source` 形如 `G03!9` 且域名匹配；
> `HITS` 表覆盖**除 4 条依赖运行时状态者外**的全部规则；空病例上任何规则都不得返回 `True`；
> 本表的每个 rule_id 都必须能在代码里找到（`test_ruleset_md_documents_every_rule`）。

## 范围

| 节点 | 条数 | 是否实现 | 理由 |
|---|---|---|---|
| G00 / G01 / G02 | 9 | ❌ | 属于「读取资料 / 证据质量 / 事实抽取」，归 LLM 抽取层 |
| **G03** | **21** | ✅ | 急诊判定（R01–R21） |
| **G04** | **18** | ✅ | 复杂度判定（R01–R18） |
| **G08** | **11** | ✅ | 病种分类（R01–R11） |
| **G09** | **4** | ✅ | 分类收尾与回流（R01–R04） |
| 合计实现 | **54** | | 

G03 的 `P01–P04` 是**优先级桶**，不是规则：它们只用来给 R01–R21 分组并决定出口顺序，
不写进 `RULES` 字典（判定产物，不进模型、不进哈希 —— F6）。

## 求值口径（写代码时照此实现）

- **三值**：`True` / `False` / `None`(=UNKNOWN)。Kleene 合取：任一 `False` → `False`；
  否则任一 `None` → `None`；全 `True` → `True`。析取：任一 `True` → `True`；否则任一 `None` → `None`。
- **UNKNOWN 不短路、不当 False**：`None` 不等于"没命中"，也不等于"阴性"。
- **不确定值**（`UNCERTAIN_TOKENS`：可疑/疑似/未评价/不可评价/未知…）一律先折成 `UNKNOWN`，
  永不参与"成立/不成立"的判断。**只有明确命中才算命中**。
- **出口五值**：`G05` / `G06` / `G07` / `人工复核` / `资料核实`，与 `RouteKind` 一一对应
  （MODE 对应前三个，REVIEW 对应人工复核，VERIFY 对应资料核实）。
- **优先级**：`G03 回流 > G06 > VERIFY`。

---

## G03 · 急诊判定（21 条）

出口顺序：P01（R01–R10/R12/R16/R20，→G05）→ P02（R11/R17/R18/R19/R21，→人工复核）
→ P03（R13/R14，→资料核实）→ P04（R15，放行至 G04）。
**同桶内按 rule_id 升序取第一个命中**；P01 命中即 `G05`，不再看 P02–P04。

| rule_id | 函数名 | 判据（可实现形式） | 出口 | 桶 | 源行 |
|---|---|---|---|---|---|
| G03.R01 | `g03_r01_direct_extravasation` | 有效增强 CTA（`q.cta_enhance=适当` 且 `q.cta_phase=动脉期`）+ `c.extrav=明确` + `c.extrav_contig=明确连续` + `c.source_kind∈{正式报告,专家确认}` | G05 | P01 | G03!9 |
| G03.R02 | `g03_r02_aneurysm_with_hematoma` | AAA 明确（`c.aorta` 含腹主动脉 且 `cl.aaa_reported=确认`）+ `c.hematoma=明确` + `c.hematoma_adj=明确相邻` + 报告/专家确认 | G05 | P01 | G03!10 |
| G03.R03 | `g03_r03_rupture_reported` | `c.rupture_report=明确破裂` + `q.idmatch=一致` | G05 | P01 | G03!11 |
| G03.R04 | `g03_r04_suspected_rupture_unstable` | 〔`d.dx_context=疑似` 或（AAA 确认/既往 且（急性腹背侧腰痛 或 `s.syncope∈{肯定,可疑}`））〕且〔`v.sbp<80` 或 `v.conscious_change=新发下降`〕 | G05 | P01 | G03!12 |
| G03.R05 | `g03_r05_aaa_acute_pain_attributed` | AAA 确认/既往 + `s.pain_current∈{当前新发,本次进展}` + 部位含腹/背/侧腹/腰 + `s.pain_attrib∈{明确相关,怀疑相关}` | G05 | P01 | G03!13 |
| G03.R06 | `g03_r06_aaa_sudden_pain_unattributed` | AAA 确认/既往 + `pain_current∈{当前新发,本次进展}` + 部位命中 + `s.pain_onset=突然` + `s.pain_attrib=未判断` | G05 | P01 | G03!14 |
| G03.R07 | `g03_r07_rupture_not_excluded` | `c.rupture_report=疑似破裂` 或 `c.extrav=可疑` 或 `c.hematoma=可疑` 或（`c.source_kind=算法候选` 且 `q.report_vs_algo∈{仅算法,冲突}`） | G05 | P01 | G03!15 |
| G03.R08 | `g03_r08_graft_with_gi_bleed` | `d.surg` 含 EVAR/OSR 或 `c.graft=有` + `s.gi_bleed` ⊆ 含呕血/黑便/便血 | G05 | P01 | G03!16 |
| G03.R09 | `g03_r09_acute_limb_ischemia` | `s.limb` 含痛/苍白/发凉 + （含运动障碍/麻木 或 `s.pulse_deficit∈{有,缺失}` 或 `c.branch` 相应动脉闭塞） | G05 | P01 | G03!17 |
| G03.R10 | `g03_r10_news2_critical` | `NEWS2 总分 ≥ 7`（同一评估轮次完整可算） | G05 | P01 | G03!18 |
| G03.R11 | `g03_r11_news2_urgent_review` | `NEWS2 总分 5–6` 或 任一可评价单项 `=3` | 人工复核 | P02 | G03!19 |
| G03.R12 | `g03_r12_no_enhanced_cta_with_clues` | AAA 确认/既往 +（急性腹背侧腰痛 或 晕厥）+ `q.cta_enhance∈{不足,无动脉期}` 或 `q.cta_phase∈{平扫,未知}` | G05 | P01 | G03!20 |
| G03.R13 | `g03_r13_key_fact_conflict` | `q.idmatch=不一致` 或 `q.conflict` 含数值/身份/语义/时间 或 `q.report_vs_algo=冲突` | 资料核实 | P03 | G03!21 |
| G03.R14 | `g03_r14_screen_incomplete` | `q.current_assess≠本次已评估` 或 疼痛/晕厥/消化道出血/肢体状态任一 `UNKNOWN` 或 `v.sbp`/`v.hr`/`v.acvpu` 无有效值 | 资料核实 | P03 | G03!22 |
| G03.R15 | `g03_r15_no_emergency_release` | P01/P02/P03 全部明确 `False`（无 UNKNOWN）+ 身份一致 + 筛查完成 + 症状/晕厥/出血/肢体明确无危险 + 生命体征有效 | G04 | P04 | G03!23 |
| G03.R16 | `g03_r16_suspected_rupture_reported` | `q.idmatch=一致` 且（本次签署临床记录明确疑似 rAAA 或 影像报告疑似破裂）**不要求 SBP<80** | G05 | P01 | G03!24 |
| G03.R17 | `g03_r17_hb_drop_comparable` | `hb_comparable=True` 且 `hb_confound=False` 且 `hb-hb_prev≥30 g/L` 且 `0<间隔≤48h` | 人工复核 | P02 | G03!25 |
| G03.R18 | `g03_r18_severe_sudden_pain_no_aaa` | 身份一致 + 无已知 AAA（`d.dx_context=无相关记录`）+ 疼痛新发/加重 + 部位命中 + 突发 +（`pain_severity≥7` 或 明示剧烈） | 人工复核 | P02 | G03!26 |
| G03.R19 | `g03_r19_hb_drop_uncertain` | `hb-hb_prev≥30` 且 `间隔≤48h` 但（`hb_comparable≠True` 或 `hb_confound≠False`）；只登记"差异待查" | 人工复核 | P02 | G03!27 |
| G03.R20 | `g03_r20_severe_hypotension` | `v.sbp<80`（可靠、同一患者、本次）**不依赖是否已知 AAA** | G05 | P01 | G03!28 |
| G03.R21 | `g03_r21_lactate_above_uln` | 两者可信且同次、单位一致：`lab.lact > lab.lact_uln` | 人工复核 | P02 | G03!29 |

## G04 · 复杂度判定（18 条）

出口顺序：`R01–R12`、`R15–R18` 任一命中 → **G06 复杂**；
否则 `R13` 命中 → **资料核实**；否则 `R14` 命中 → **G07 指南**。
`R14` 末尾的 gate：存在早发/家族史/典型炎症影像等阳性线索时，**先跑 R15–R18**，不得直接判低复杂。

| rule_id | 函数名 | 判据（可实现形式） | 出口 | 源行 |
|---|---|---|---|---|
| G04.R01 | `g04_r01_short_neck` | `4 ≤ n.neck_len ≤ 10` 且 `renal_l/renal_r=未受累` | G06 | G04!40 |
| G04.R02 | `g04_r02_juxtarenal` | `0 ≤ n.neck_len < 4` 且 `renal_l/renal_r=未受累` | G06 | G04!41 |
| G04.R03 | `g04_r03_pararenal` | （`renal_l=明确受累` 或 `renal_r=明确受累`）且 `n.sma=未受累` | G06 | G04!42 |
| G04.R04 | `g04_r04_visceral_pararenal` | 肾动脉开口受累 且 `n.sma=明确受累` 且 `n.ca=未受累` | G06 | G04!43 |
| G04.R05 | `g04_r05_taaa_iv_extension` | 肾 + SMA + 腹腔干均受累 且 `n.thoracic=否`（符合项目内Ⅳ型定义） | G06（+标记） | G04!44 |
| G04.R06 | `g04_r06_beyond_project_boundary` | `n.thoracic=是` 或 Ⅳ型定义不满足但内脏段均受累 → 提示专科路径，**不得归Ⅳ型** | G06 | G04!45 |
| G04.R07 | `g04_r07_infection_or_fistula` | `n.infection_ctx∈{医师明确,高度怀疑}` 或 `n.fistula_ctx∈{明确,疑似}` | G06 | G04!46 |
| G04.R08 | `g04_r08_ifu_mismatch` | `n.seal_ifu=不符`（仅当已指定有效地区/版本/型号并完成核验） | G06 | G04!47 |
| G04.R09 | `g04_r09_iliac_limitation` | `n.iliac_bilat=双侧` 且（`iia_left` 或 `iia_right ∈{闭塞,严重狭窄}`） | G06 | G04!48 |
| G04.R10 | `g04_r10_access_occlusion` | `n.access_occlusion=双侧闭塞` | G06 | G04!49 |
| G04.R11 | `g04_r11_prior_repair_reintervention` | `n.prev_repair≠无` 且（`n.endoleak_type∈{I,III}` 或 `n.graft_major∈{组件分离,移位,明显闭塞}` 或 `n.sac_growth≥10`） | G06 | G04!50 |
| G04.R12 | `g04_r12_physiologic_risk` | `n.egfr<30` 或 `n.dialysis=是` 或 `n.heart_class∈{III,IV}` 或 `n.resp_support∈{长期吸氧,无创通气,有创通气}` | G06 | G04!51 |
| G04.R13 | `g04_r13_insufficient_for_low_complexity` | AAA 确认/疑似 且（`n.cta_status∈{缺失,部分,冲突}` 或 `neck_len`/`renal_l`/`renal_r`/`sma`/`ca` 任一 `None` 或 `不可评价`） | 资料核实 | G04!52 |
| G04.R14 | `g04_r14_low_complexity_to_guideline` | 上述复杂规则均明确 `False` + 核心影像足够 + 无 R15–R18 阳性线索 | G07 | G04!53 |
| G04.R15 | `g04_r15_genetic_confirmed` | `n.genetic_ctx=确诊`（由 `GEN.*` 明细派生） | G06（+病因标签） | G04!54 |
| G04.R16 | `g04_r16_inflammatory` | `n.inflammatory_ctx∈{临床确认,影像高度提示}`（由 `INF.*` 明细派生；单纯 CRP/ESR 高不触发） | G06（+D5 子型） | G04!55 |
| G04.R17 | `g04_r17_genetic_suspected` | 有主动脉病变 且（`gen.aaa_age_onset<60` 或 `gen.family_aneurysm=阳性` 或 `gen.syndromic_features` 非空/阳性 或 医生明确怀疑遗传病）→ 标"疑似遗传背景" | G06 | G04!56 |
| G04.R18 | `g04_r18_inflammation_vs_infection` | `inflammatory_ctx∈{可疑,影像高度提示,临床确认}` 且 `infection_ctx∈{疑似,医师明确,高度怀疑}` → 双标签并存、不互相覆盖 | G06 | G04!57 |

## G08 · 病种分类（11 条）

出口是**标签集合**（`cl.diag_multi` / `k.class_output`），不是模式。允许多标签共存。

| rule_id | 函数名 | 判据（可实现形式） | 产出 | 源行 |
|---|---|---|---|---|
| G08.R01 | `g08_r01_d1_asymptomatic` | `cl.aaa_reported=确认`（或 `cl.aaa_diam≥30`）且 `cl.symptom_aaa∈{无当前症状,明确归因其他}` 且 `cl.aaa_rupture=未见相关证据` | D1 | G08!21 |
| G08.R02 | `g08_r02_d2_symptomatic_unruptured` | AAA 确认 且 `cl.symptom_aaa∈{医师确认相关,医师怀疑相关}` 且 `cl.aaa_rupture≠明确` | D2 | G08!22 |
| G08.R03 | `g08_r03_d3_ruptured` | `cl.aaa_rupture∈{明确,疑似}` → 对应"确认 / 疑似"；疑似时保持 G05 优先 | D3 | G08!23 |
| G08.R04 | `g08_r04_d4_infected` | `cl.infection≠无证据/未评价` 或 `cl.fistula∈{主动脉肠瘘,主动脉腔静脉瘘,其他瘘,疑似}` | D4 | G08!24 |
| G08.R05 | `g08_r05_d5_special` | `cl.pau/imh/iaad∈{明确,可疑}` 或 `cl.saccular/pseudo∈{明确,疑似}` | D5 | G08!25 |
| G08.R06 | `g08_r06_d6_postop` | `cl.prev_evor≠无` 且 `cl.post_reason` 非空（术后随访或并发症评估） | D6 | G08!26 |
| G08.R07 | `g08_r07_d7_iliac` | `cl.iliac_site` 非空且非"无"（医师/影像确认髂动脉瘤） | D7 | G08!27 |
| G08.R08 | `g08_r08_taaa_iv_label` | G04 判定 `thoracic=否` 且 内脏段均受累（复用 G04.R05 结果）→ 标"Ⅳ型TAAA扩展"，**不设 D8** | 扩展标签 | G08!28 |
| G08.R09 | `g08_r09_multi_label_conflict` | 同一 AAA 同时命中 D1/D2/D3 中 ≥2 个 → `k.diag_conflict=是`，D3 优先；遗传标签不取代主病种 | 冲突标记 | G08!29 |
| G08.R10 | `g08_r10_inflammatory_as_d5_subtype` | `n.inflammatory_ctx∈{临床确认,影像高度提示}` → D5（炎症性 AAA 子型）；`可疑` → D5 疑似 | D5 子型 | G08!30 |
| G08.R11 | `g08_r11_genetic_as_modifier` | `n.genetic_ctx∈{确诊,疑似遗传背景}` → 输出"遗传背景：确诊/疑似"修饰标签，**保持 D1–D7 编号、不新增 D8** | 病因标签 | G08!31 |

## G09 · 分类收尾（4 条）

按 R01 → R02 → R03 → R04 顺序短路；R04 是正常出口。

| rule_id | 函数名 | 判据（可实现形式） | 出口 | 源行 |
|---|---|---|---|---|
| G09.R01 | `g09_r01_new_emergency_reflow` | `k.new_urgent=是`（分类期出现新的外溢/休克/意识变化/急性出血证据） | 回 G03 → G05 | G09!7 |
| G09.R02 | `g09_r02_identity_conflict` | 身份或来源冲突（`q.idmatch=不一致` 或 `q.conflict` 含身份）→ 隔离不可信记录，保留其他已确认警报 | 资料核实 | G09!8 |
| G09.R03 | `g09_r03_diagnosis_conflict` | 同一病灶关键病种标签冲突（`k.diag_conflict=是`）→ 不得自动择一 | 人工复核 | G09!9 |
| G09.R04 | `g09_r04_classify_and_stop` | 无新急症 + 无未解决关键冲突 + 至少一个可信（确认/疑似）病种标签 → 输出标签集合，本轮停止 | 输出 D1–D7 | G09!10 |

---

## 两个「不是规则」的标识

54 条之外，`RouteResult.rule_id` 还可能是下面两个值 —— 它们**不是判定规则**，
所以不在 `RULES` 里，但必须在测试的白名单中显式登记：

| 标识 | 含义 | 为什么不能借某条规则的 id |
|---|---|---|
| `MANUAL.URGENCY` | 医生置位 `urgency_flag`，跳过自动判定直接进 G05 | 它是人工覆盖，不是规则判的 |
| `FALLBACK.VERIFY` | 没有任何规则能判定（存在决定性 UNKNOWN）→ 资料核实 | 借 `G03.R15` / `G04.R14` 会让本表的出口列说谎：那两条的出口是 G06 / G07，不是资料核实 |

## 出口汇总

| 出口 | 由谁产生 | RouteKind |
|---|---|---|
| G05 急诊 | G03 P01 全部 + `urgency_flag` 置位（越级） | MODE / 急诊模式 |
| 人工复核 | G03 P02、G09.R03 | REVIEW |
| 资料核实 | G03 P03、G04.R13、G09.R02、`FALLBACK.VERIFY`（无规则可判时） | VERIFY |
| G06 复杂 | G04 复杂命中（含 R15–R18） | MODE / 复杂模式 |
| G07 指南 | G04.R14 | MODE / 指南模式 |

**G07 是排他分支，不是兜底。** 真正的兜底是 `资料核实`——资料不足时不下结论。

`G09.R02` 在端到端**不可达**：`G03.R13` 在判定之前就拦下了身份冲突。
这属分层拦截的必然结果（规格要求 G09 按 R01→R02→R03→R04 短路，故代码里保留该分支），
`test_router_branches.py::TestG09Orchestration` 记录了这个事实，避免以后误判为漏测。
