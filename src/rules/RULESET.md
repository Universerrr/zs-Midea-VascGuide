# RULESET —— 66 条判定规则 ↔ 函数 ↔ V3.6.2 源行

> **这是规格的第二真源。** 代码是规格的实现，本表是二者的对照凭据。
> 改规则必须同时改三处：`router.py` 的函数、本表对应行、`RULESET_VERSION` 与 `RULESET_CHANGELOG`。
>
> 机械守门（`tests/test_router_branches.py`）：
> `len(RULES) == 66`；编号按域连续（判据是「已实现 + `PENDING_RULES`」的并集，当前 `PENDING_RULES` 为空）；
> 每条 `source` 形如 `G03!9`，且**与规格工作簿里该 rule_id 的实际行号逐条相等**
> （`TestRuleRegistry::test_source_rows_match_the_workbook` 直接读 Excel 对账）；
> `HITS` 表覆盖**除 6 条依赖运行时状态者外**的全部规则；空病例上任何规则都不得返回 `True`
> （`G09.R08` 例外：它回答的就是「什么都没有」）；本表的每个 rule_id 都必须能在代码里找到。

## 范围

| 节点 | 条数 | 是否实现 | 理由 |
|---|---|---|---|
| G00 / G01 / G02 | 9 | ❌ | 属于「读取资料 / 证据质量 / 事实抽取」，归 LLM 抽取层 |
| **G03** | **21** | ✅ | 急诊判定（R01–R21） |
| **G04** | **21** | ✅ | 复杂度判定（R01–R20 + 工程补充 R21） |
| **G08** | **15** | ✅ | 病种分类（R01–R15） |
| **G09** | **9** | ✅ | 分类收尾与回流（R01–R09） |
| 合计实现 | **66** | | 规格 65 条 + 工程补齐 1 条（`G04.R21`） |

G03 的 `P01–P04` 是**优先级桶**，不是规则：它们只用来给 R01–R21 分组并决定出口顺序，
不写进 `RULES` 字典（判定产物，不进模型、不进哈希 —— F6）。

## 求值口径（写代码时照此实现）

- **三值**：`True` / `False` / `None`(=UNKNOWN)。Kleene 合取：任一 `False` → `False`；
  否则任一 `None` → `None`；全 `True` → `True`。析取：任一 `True` → `True`；否则任一 `None` → `None`。
- **UNKNOWN 不短路、不当 False**：`None` 不等于"没命中"，也不等于"阴性"。
- **不确定值**（`UNCERTAIN_TOKENS`：可疑/疑似/未评价/不可评价/未知…）一律先折成 `UNKNOWN`，
  永不参与"成立/不成立"的判断。**只有明确命中才算命中**。
- **出口六值**：`G05` / `G06` / `G07` / `人工复核` / `资料核实` / `无目标病变`，与 `RouteKind`
  一一对应（MODE 对应前三个，REVIEW / VERIFY / NO_TARGET 各对应后三个）。「无目标病变」是
  V3.6.2 新增的终末出口：**查清了没有病** ≠ **查不清**，前者不需要人再复核分类。
- **优先级**：`G03 回流 > G06 > VERIFY`。

---

## G03 · 急诊判定（21 条）

出口顺序：P01（R01–R10/R16/R20，→G05）→ P02（R11/R12/R17/R18/R19/R21，→人工复核）
→ P03（R13/R14，→资料核实）→ P04（R15，放行至 G04）。
**同桶内按 rule_id 升序取第一个命中**；P01 命中即 `G05`，不再看 P02–P04。

> 分组以**每条规则行自己的 P 标记 + M 列出口**为准，不以 `G03.P0x` 分组行的 F 列清单
> 为准 —— 两者在 `G03.R12` 上已不同步：分组行仍写 `R11;R17–R19;R21`，但 R12 单行
> 已标 P1、出口 REVIEW。`G03!20` 的原话是「紧急复核，不能降为常规」，影像不可靠时
> **不得**据此给出术式方向，所以它留在 P02。

| rule_id | 函数名 | 判据（可实现形式） | 出口 | 桶 | 源行 |
|---|---|---|---|---|---|
| G03.R01 | `g03_r01_direct_extravasation` | 有效增强 CTA（`q.cta_enhance=适当` 且 `q.cta_phase=动脉期`）+ `c.extrav=明确` + `c.extrav_contig=明确连续` + `c.source_kind∈{正式报告,专家确认}` | G05 | P01 | G03!9 |
| G03.R02 | `g03_r02_aneurysm_with_hematoma` | AAA 已确认（`cl.aaa_reported=确认` / `d.dx_context∈{确认,既往已修复}` / `cl.aaa_diam≥30`，任一）+ `c.hematoma=明确` + `c.hematoma_adj=明确相邻` + `c.source_kind∈{正式报告,专家确认}` | G05 | P01 | G03!10 |
| G03.R03 | `g03_r03_rupture_reported` | `c.rupture_report=明确破裂` + `q.idmatch=一致` | G05 | P01 | G03!11 |
| G03.R04 | `g03_r04_suspected_rupture_unstable` | 〔`q.idmatch=一致` 且（`d.dx_context=疑似` 或 `c.rupture_report=疑似破裂`）—— 即 R16 的前提〕且〔`v.sbp<80` 或 `v.conscious_change=新发下降`〕。**不独立诊断破裂**；无主动脉疑虑的低血压归 R20 | G05 | P01 | G03!12 |
| G03.R05 | `g03_r05_aaa_acute_pain_attributed` | AAA 确认/既往 + `s.pain_current∈{当前新发,本次进展}` + 部位含腹/背/侧腹/腰 + `s.pain_attrib∈{明确相关,怀疑相关}` | G05 | P01 | G03!13 |
| G03.R06 | `g03_r06_aaa_sudden_pain_unattributed` | AAA 确认/既往 + `pain_current∈{当前新发,本次进展}` + 部位命中 + `s.pain_onset=突然` + `s.pain_attrib=未判断` | G05 | P01 | G03!14 |
| G03.R07 | `g03_r07_rupture_not_excluded` | `c.rupture_report=疑似破裂` 或 `c.extrav=可疑` 或 `c.hematoma=可疑` 或（`c.source_kind=算法候选` 且 `q.report_vs_algo∈{仅算法,冲突}`） | G05 | P01 | G03!15 |
| G03.R08 | `g03_r08_graft_with_gi_bleed` | `d.surg` 含 EVAR/OSR 或 `c.graft=有` + `s.gi_bleed` ⊆ 含呕血/黑便/便血 | G05 | P01 | G03!16 |
| G03.R09 | `g03_r09_acute_limb_ischemia` | `s.limb` 含痛/苍白/发凉 + （含运动障碍/麻木 或 `s.pulse_deficit∈{有,缺失}` 或 `c.branch` 相应动脉闭塞） | G05 | P01 | G03!17 |
| G03.R10 | `g03_r10_news2_critical` | `NEWS2 总分 ≥ 7`（同一评估轮次完整可算） | G05 | P01 | G03!18 |
| G03.R11 | `g03_r11_news2_urgent_review` | `NEWS2 总分 5–6` 或 任一可评价单项 `=3` | 人工复核 | P02 | G03!19 |
| G03.R12 | `g03_r12_no_enhanced_cta_with_clues` | AAA 已确认 +（急性腹背侧腰痛 或 `s.syncope∈{肯定,可疑}`）+（`q.cta_enhance∈{不足,无动脉期}` 或 `q.cta_phase∈{平扫,未知}`） | 人工复核 | P02 | G03!20 |
| G03.R13 | `g03_r13_key_fact_conflict` | `q.idmatch=不一致` 或 `q.conflict` 含数值/身份/语义/时间 或 `q.report_vs_algo=冲突` | 资料核实 | P03 | G03!21 |
| G03.R14 | `g03_r14_screen_incomplete` | `q.current_assess≠本次已评估` 或 疼痛/晕厥/消化道出血/肢体状态任一 `UNKNOWN` 或 `v.sbp`/`v.hr`/`v.acvpu` 无有效值 | 资料核实 | P03 | G03!22 |
| G03.R15 | `g03_r15_no_emergency_release` | P01/P02/P03 全部明确 `False`（无 UNKNOWN）+ 身份一致 + 筛查完成 + 症状/晕厥/出血/肢体明确无危险 + 生命体征有效 | G04 | P04 | G03!23 |
| G03.R16 | `g03_r16_suspected_rupture_reported` | `q.idmatch=一致` 且（本次签署临床记录明确疑似 rAAA 或 影像报告疑似破裂）**不要求 SBP<80** | G05 | P01 | G03!24 |
| G03.R17 | `g03_r17_hb_drop_comparable` | `hb_comparable=True` 且 `hb_confound=False` 且 `hb-hb_prev≥30 g/L` 且 `0<间隔≤48h` | 人工复核 | P02 | G03!25 |
| G03.R18 | `g03_r18_severe_sudden_pain_no_aaa` | 身份一致 + 无已知 AAA（`d.dx_context=无相关记录`）+ 疼痛新发/加重 + 部位命中 + 突发 +（`pain_severity≥7` 或 明示剧烈） | 人工复核 | P02 | G03!26 |
| G03.R19 | `g03_r19_hb_drop_uncertain` | `hb-hb_prev≥30` 且 `间隔≤48h` 但（`hb_comparable≠True` 或 `hb_confound≠False`）；只登记"差异待查" | 人工复核 | P02 | G03!27 |
| G03.R20 | `g03_r20_severe_hypotension` | `v.sbp<80`（可靠、同一患者、本次）**不依赖是否已知 AAA** | G05 | P01 | G03!28 |
| G03.R21 | `g03_r21_lactate_above_uln` | 两者可信且同次、单位一致：`lab.lact > lab.lact_uln` | 人工复核 | P02 | G03!29 |

> 表中「出口」列写的是**规格 M 列的去处**。`G03.R15` 那格写 `G04` —— 它是**流程下一站**
> （进入复杂度判定），不是路由出口；路由出口由 G04 判定后才产生（`G06`/`G07`/资料核实）。
> 故 `RuleSpec.exit` 记 `G06`：一处记「去哪儿判」，一处记「判完算哪类」，两者不冲突。

## G04 · 复杂度判定（21 条）

出口顺序：阻断型 `R01–R12`、`R18` 任一命中 → **G06 复杂**；加分型 `R15–R17`、`R21`
命中 → **G06 复杂**（其 `UNKNOWN` **不阻断**放行，规格明写「仅因未做遗传检测/CRP
不强制所有常规病例升复杂」）；否则 `R13` 命中 → **资料核实**；否则 `R14` 命中 →
**G07 指南**（`R19` 与 `R14` 同出口，依据更具体，命中时由它署名）。

`R14` 的放行条件只取阻断型的判定结果：**必须全部明确 `False`**，任一条 `UNKNOWN`
都不得放行（`n.seal_ifu=未核验` 时 `R08` 返回 UNKNOWN —— 没核验 ≠ 符合 IFU）。

| rule_id | 函数名 | 判据（可实现形式） | 出口 | 源行 |
|---|---|---|---|---|
| G04.R01 | `g04_r01_short_neck` | `4 ≤ n.neck_len ≤ 10` 且 `renal_l/renal_r=未受累` | G06 | G04!42 |
| G04.R02 | `g04_r02_juxtarenal` | `0 ≤ n.neck_len < 4` 且 `renal_l/renal_r=未受累` | G06 | G04!43 |
| G04.R03 | `g04_r03_pararenal` | （`renal_l=明确受累` 或 `renal_r=明确受累`）且 `n.sma=未受累` | G06 | G04!44 |
| G04.R04 | `g04_r04_visceral_pararenal` | （`renal_l` 或 `renal_r=明确受累`）且 `n.sma=明确受累` 且 `n.ca=未受累` | G06 | G04!45 |
| G04.R05 | `g04_r05_taaa_iv_extension` | 肾 + SMA + 腹腔干均受累 且 `n.thoracic=否`（符合项目内Ⅳ型定义） | G06（+标记） | G04!46 |
| G04.R06 | `g04_r06_beyond_project_boundary` | `n.thoracic=是`（明显累及降胸主动脉 → 超出本项目边界，提示专科路径，**不得归Ⅳ型**） | G06 | G04!47 |
| G04.R07 | `g04_r07_infection_or_fistula` | `n.infection_ctx∈{医师明确,高度怀疑}` 或 `n.fistula_ctx∈{明确,疑似}` | G06 | G04!48 |
| G04.R08 | `g04_r08_ifu_mismatch` | `n.seal_ifu=不符`（仅当已指定有效地区/版本/型号并完成核验） | G06 | G04!49 |
| G04.R09 | `g04_r09_iliac_limitation` | `n.iliac_bilat=双侧` 且（`iia_left` 或 `iia_right ∈{闭塞,严重狭窄}`） | G06 | G04!50 |
| G04.R10 | `g04_r10_access_occlusion` | `n.access_occlusion=双侧闭塞` | G06 | G04!51 |
| G04.R11 | `g04_r11_prior_repair_reintervention` | 有既往修复史（`cl.prev_evor` 或 `n.prev_repair` ∈ {EVAR,OSR,其他}，与 `R19` 同一判据 `_has_prior_repair`）且（`n.endoleak_type∈{I,III}` 或 `n.graft_major∈{组件分离,移位,明显闭塞}` 或 `n.sac_growth≥10`） | G06 | G04!52 |
| G04.R12 | `g04_r12_physiologic_risk` | `n.egfr<30` 或 `n.dialysis=是` 或 `n.heart_class∈{III,IV}` 或 `n.resp_support∈{长期吸氧,无创通气,有创通气}` | G06 | G04!53 |
| G04.R13 | `g04_r13_insufficient_for_low_complexity` | AAA 确认/疑似 且（`n.cta_status∈{缺失,部分,冲突}` 或 `neck_len`/`renal_l`/`renal_r`/`sma`/`ca` 任一缺失或不可评价 —— 二者都由 `_missing` 一并判） | 资料核实 | G04!54 |
| G04.R14 | `g04_r14_low_complexity_to_guideline` | **阻断型**（R01–R12、R18）全部明确 `False` 且 `n.cta_status∈{完整,部分}` | G07 | G04!55 |
| G04.R15 | `g04_r15_genetic_confirmed` | `n.genetic_ctx=确诊`（由 `GEN.*` 明细派生） | G06（+病因标签） | G04!56 |
| G04.R16 | `g04_r16_inflammatory` | `n.inflammatory_ctx∈{临床确认,影像高度提示}`（由 `INF.*` 明细派生；单纯 CRP/ESR 高不触发） | G06（+D5 子型） | G04!57 |
| G04.R17 | `g04_r17_genetic_suspected` | 有主动脉病变 且（遗传证据支持「疑似」：`gen.syndrome_type=疑似` / `family_aneurysm=阳性` / `syndromic_features` 非空 / `aaa_age_onset<60`，与 `_derive_genetic_ctx` 共用 `_genetic_suspected`；或抽取层直接给 `n.genetic_ctx=疑似遗传背景`）→ 标"疑似遗传背景" | G06 | G04!58 |
| G04.R18 | `g04_r18_inflammation_vs_infection` | `inflammatory_ctx∈{可疑,影像高度提示,临床确认}` 且 `infection_ctx∈{医师明确,高度怀疑}` → 双标签并存、不互相覆盖（**阻断型**：UNKNOWN 不放行） | G06 | G04!59 |
| G04.R19 | `g04_r19_prior_repair_unremarkable` | 有既往修复史且内漏/结构异常/瘤囊增长/感染/瘘**全部明确为无** → 不因手术史判复杂，走 G07 且依据记为「术后随访」；任一异常项缺失即 UNKNOWN，不按阴性处理 | G07 | G04!61 |
| G04.R20 | `g04_r20_comorbidity_with_primary` | AAA 已确认 且 遗传/炎症/感染任一有证据 → 病因标签与主病种**各自保留**（出口 G08 多标签），不取代 D1–D7 | G08 | G04!62 |
| G04.R21 | `g04_r21_malperfusion` | 取 `_materialize_malperfusion` 的三值：**只有抽取值** `c.malperfusion` 含任一器官才为真；仅 `c.branch` 狭窄/闭塞一律 UNKNOWN（规格 `G04!J40` 明文「不能仅凭解剖狭窄推断器官缺血」）。工程补齐规则，不是 ESVS 原文 | G06 | G04!40 |

## G08 · 病种分类（15 条）

出口是**标签集合**（`cl.diag_multi` / `k.class_output`），不是模式。允许多标签共存。

| rule_id | 函数名 | 判据（可实现形式） | 产出 | 源行 |
|---|---|---|---|---|
| G08.R01 | `g08_r01_d1_asymptomatic` | `cl.aaa_reported=确认`（或 `cl.aaa_diam≥30`）且 `cl.symptom_aaa∈{无当前症状,明确归因其他}` 且 `cl.aaa_rupture=未见相关证据` 且**未做过主动脉修复**（术后是 D6，不是原发无症状 AAA） | D1 | G08!24 |
| G08.R02 | `g08_r02_d2_symptomatic_unruptured` | AAA 确认 且 `cl.symptom_aaa∈{医师确认相关,医师怀疑相关}` 且 `cl.aaa_rupture≠明确` 且**未做过主动脉修复** | D2 | G08!25 |
| G08.R03 | `g08_r03_d3_ruptured` | `cl.aaa_rupture∈{明确,疑似}` → 对应"确认 / 疑似"；疑似时保持 G05 优先 | D3 | G08!26 |
| G08.R04 | `g08_r04_d4_infected` | `cl.infection` ∈ `INFECTION_LABELS`（原生/移植物 × 明确/疑似）或 `cl.fistula` ∈ `FISTULA_LABELS` —— 取值表在 `router.py` 只有一份 | D4 | G08!27 |
| G08.R05 | `g08_r05_d5_special` | `cl.pau/imh/iaad∈{明确,可疑}` 或 `cl.saccular/pseudo∈{明确,疑似}` | D5 | G08!28 |
| G08.R06 | `g08_r06_d6_postop` | 有既往修复史（`cl.prev_evor` 或 `n.prev_repair` ∈ {EVAR,OSR,其他}）→ D6。「既往支架」与「已确诊并发症」分开：有没有并发症由 D6.1 分诊 | D6 | G08!29 |
| G08.R07 | `g08_r07_d7_iliac` | `cl.iliac_site` 含左侧/右侧/CIA/IIA/EIA 任一（医师/影像确认髂动脉瘤） | D7 | G08!30 |
| G08.R08 | `g08_r08_taaa_iv_label` | `n.thoracic=否` 且 内脏段（SMA + 腹腔干）均受累且至少一侧肾动脉受累 → 标"Ⅳ型TAAA扩展"，**不设 D8** | 扩展标签 | G08!31 |
| G08.R09 | `g08_r09_multi_label_conflict` | `x.labels` ∪ `cl.diag_multi` 中同属 D1/D2/D3 的 ≥2 个 → 冲突，D3 优先；遗传标签不取代主病种 | 冲突标记 | G08!32 |
| G08.R10 | `g08_r10_inflammatory_as_d5_subtype` | `n.inflammatory_ctx∈{临床确认,影像高度提示,可疑}` → D5（炎症性 AAA 子型；「可疑」也补标签、状态为疑似） | D5 子型 | G08!33 |
| G08.R11 | `g08_r11_genetic_as_modifier` | `n.genetic_ctx∈{确诊,疑似遗传背景}` → 输出"遗传背景：确诊/疑似"修饰标签，**保持 D1–D7 编号、不新增 D8** | 病因标签 | G08!34 |
| G08.R12 | `g08_r12_infection_origin` | `cl.infection` 落在原生/移植物四值之一 → 部位可定档；未评价/不确定/缺填 → UNKNOWN；明确无证据但**有移植物** → UNKNOWN（VGEI 可无典型征象，不能凭「这次没看到」排除）。入口细化由 D4.1 分诊，本规则只留痕 | 证据定档 | G08!36 |
| G08.R13 | `g08_r13_postop_label_not_complication` | 有修复史且 I/III/IV/V 型内漏、瘤囊增长 ≥10mm、结构故障、感染、瘘**均未确诊** → 进 D6.1 常规随访；有确诊并发症时本条不成立（由 G08.R06 给 D6 标签、D6.1 分诊） | 证据定档 | G08!37 |
| G08.R14 | `g08_r14_no_target_candidate` | 身份/时间可靠 + 覆盖充分 + CTA 可评价 + `aaa_reported=否定` + 破裂/症状/感染/瘘/髂瘤全阴 + 无既往修复 → **暂定**「无目标病变」候选交 G09.R07；「未述及/覆盖不足」不算阴性 | G09 候选 | G08!23 |
| G08.R15 | `g08_r15_unclassifiable_candidate` | 未形成任何标签 且（CTA 非完整 / 覆盖非完整 / `aaa_reported=未述及` / 部位含未知）→ **暂定**「未分类」候选交 G09.R08；有标签时为 False | G09 候选 | G08!22 |

## G09 · 分类收尾（9 条）

执行顺序（顺序本身是判据，不要重排）：

```
G09.R05/R06  分类中新发现的感染/瘘、术后异常 → 补标签并留痕
G09.R01      新急症 → 回 G03/G05（最优先）
G09.R02      身份/来源冲突 → 隔离核实
G09.R04      正常出口的求值（留痕）
G09.R03      标签冲突 → 人工复核
G09.R07/R08  一个标签都没有时的终审：无目标病变 / 未分类
```

R04 与 R03 是同一判据的正反面（`k.diag_conflict`），R04 求值后不返回，
由 R03 决定是否复核 —— 这样审计里两条都有留痕。

| rule_id | 函数名 | 判据（可实现形式） | 出口 | 源行 |
|---|---|---|---|---|
| G09.R01 | `g09_r01_new_emergency_reflow` | `k.new_urgent=是`（分类期出现新的外溢/休克/意识变化/急性出血证据） | 回 G03 → G05 | G09!10 |
| G09.R02 | `g09_r02_identity_conflict` | 身份或来源冲突（`q.idmatch=不一致` 或 `q.conflict` 含身份）→ 隔离不可信记录，保留其他已确认警报 | 资料核实 | G09!11 |
| G09.R03 | `g09_r03_diagnosis_conflict` | 同一病灶关键病种标签冲突（`k.diag_conflict=是`）→ 不得自动择一 | 人工复核 | G09!12 |
| G09.R04 | `g09_r04_classify_and_stop` | 无新急症 + 无未解决关键冲突 + 至少一个可信（确认/疑似）病种标签 → 输出标签集合，本轮停止 | 输出 D1–D7 | G09!13 |
| G09.R05 | `g09_r05_infection_or_fistula_reflow` | 当前有感染（原生/移植物，含疑似）或主动脉相关瘘证据 → 记 trace 并保留 D4 路由。**不补 D4 标签**（那会与 G08.R04 重复入口）；急症回流由 G09.R01 承担 | G05（急症时） | G09!14 |
| G09.R06 | `g09_r06_postop_abnormality` | 有修复史 且（内漏 I–V/疑似、瘤囊增长 ≥10mm、结构故障、感染征象）→ 确保 D6 标签，进术后流程 | D6 | G09!15 |
| G09.R07 | `g09_r07_no_target_disease_terminal` | `no_target_candidate`（G08.R14）成立 且 覆盖完整 + CTA 完整 + 身份/时间可信 + 无新急症 + 无既往修复 → **终末出口「无目标病变」**，停止 D1–D7 分流 | 无目标病变 | G09!9 |
| G09.R08 | `g09_r08_unclassifiable` | 无可信标签（或冲突未解决）→ 输出「未分类」转资料核实，列出待核字段；**不强行选 D1–D7** | 资料核实 | G09!8 |
| G09.R09 | `g09_r09_multilabel_coexistence` | 标签集合 ≥2 个且**不同属** D1/D2/D3 中 ≥2 个 → 允许共存（True，不升级成冲突）；返回值是「允许共存」而不是「存在冲突」，与 `G08.R09` 共用 `_exclusive_states` | 冲突白名单 | G09!7 |

---

## 三个「不是规则」的标识

66 条之外，`RouteResult.rule_id` 还可能是下面三个值 —— 它们**不是判定规则**，
所以不在 `RULES` 里，但必须在测试的白名单中显式登记：

| 标识 | 含义 | 为什么不能借某条规则的 id |
|---|---|---|
| `MANUAL.URGENCY` | 医生置位 `urgency_flag`，跳过自动判定直接进 G05 | 它是人工覆盖，不是规则判的 |
| `FALLBACK.VERIFY` | 没有任何规则能判定（存在决定性 UNKNOWN）→ 资料核实 | 借 `G03.R15` / `G04.R14` 会让本表的出口列说谎：那两条的出口是 G06 / G07，不是资料核实 |
| `GUARD.D3_LABEL` | 病种标签含 D3，但急诊路由没触发 → 人工复核 | 借 `G08.R03` 说不通：它判的是「有破裂标签」，不判「该不该走急诊」；借 `G03` 也说不通：它一条都没命中。这是两个判定产物之间的矛盾，不是任何一条规则的结论 |

## 出口汇总

| 出口 | 由谁产生 | RouteKind |
|---|---|---|
| G05 急诊 | G03 P01 全部 + `G09.R01`（分类期新急症）+ `urgency_flag` 置位（越级） | MODE / 急诊模式 |
| 人工复核 | G03 P02、`G09.R03`、`G09.R09` 判出的真冲突、`GUARD.D3_LABEL`（D3 标签与非急诊模式矛盾） | REVIEW |
| 资料核实 | G03 P03、`G04.R13`、`G09.R02`、`G09.R08`、`FALLBACK.VERIFY`（无规则可判时） | VERIFY |
| G06 复杂 | G04 阻断型（R01–R12、R18）与加分型（R15–R17、R21）命中 | MODE / 复杂模式 |
| G07 指南 | `G04.R14`（低复杂）；`G04.R19` 命中时由它署名 | MODE / 指南模式 |
| 无目标病变 | `G09.R07`（G08.R14 候选 + 终审全绿） | NO_TARGET |

**G07 是排他分支，不是兜底。** 真正的兜底是 `资料核实`——资料不足时不下结论。
`NO_TARGET` 的优先级垫底（`OUTCOME_PRIORITY`）：任何一条危险证据都不该被「没病」盖掉。

`G09.R02` 在端到端**不可达**：`G03.R13` 在判定之前就拦下了身份冲突。
这属分层拦截的必然结果（规格要求 G09 按 R01→R02→R03→R04 短路，故代码里保留该分支），
`test_router_branches.py::TestG09Orchestration` 记录了这个事实，避免以后误判为漏测。
