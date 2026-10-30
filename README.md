# VascGuide AI Agent

> 面向 **主动脉瘤（Aortic Aneurysm）** 的临床决策支持智能体 —— **术前—术中—术后三 Agent 闭环架构**。
> 设计原则：**「规则主导，LLM 为辅」** —— 决策树是司令员，LLM 是发言人。

---

## 一、项目背景

VascGuide 面向主动脉瘤（AAA / TAA）全周期管理，覆盖 **术前决策 → 术中导航 → 术后随访** 三个环节，
形成可追踪、可审计的闭环：

- **术前决策 Agent**：输入术前病例文本 + 影像，输出 **个体化循证诊疗方案 + 证据链**。
  采用四层流水线：`Input → Router → Execution → Aggregator & Output`。
- **术中导航 Agent**：输入 DSA 影像 + 生命体征 + 手术阶段信号，实时输出 **确定性指令 + 证据链**。
- **术后随访 Agent**：输入术后病例文本 + 影像，输出 **随访规划 + 异常预警**，纵向追踪全周期结局。

三个 Agent 共享一条完整数据链（术前 → 术中 → 术后全程可追踪），随访结果写回记忆库，
成为术前 / 术后相似病例检索的决策参考基准；术后检出异常时触发 **二次干预评估**，
复用术前决策树重算治疗方案，形成闭环。

### 核心治理红线

> **LLM 仅出现于「输入抽取」与「输出转译」两处。**
> 路由、解剖评分、风险分层、随访规划、异常检测、决策树仲裁 **全部为确定性代码**（无 LLM 参与），
> 且全程由 LangGraph Checkpoint 保证可审计、所有建议均附证据链。

---

## 二、当前阶段

> **确定性内核（数据模型 + 规则层 + 编排层）已落地并通过验证；LLM 接入与确定性工具均未开始。**
> **术前路由的规格已升级为 V3.1（2），重写路线已定案，见下节。**

### 📐 V3.1(2) 重写（2026-10-26 完成）

术前路由的规格真源为 **V3.1（2）**（`VascGuide_V3.1 (2).xlsx`，**169 字段 + 63 条规则**），
全部裁定记录集中在一份《字段与规则映射表》里。两者分工必须分清：

| 文件 | 角色 | 是否进运行路径 |
|---|---|---|
| `VascGuide_V3.1 (2).xlsx` | **规格真源**：字段定义、规则判据以它为准 | 否（只读不改） |
| `VascGuide_V3.1_字段与规则映射表.xlsx` | 裁定记录 + 拆解底稿 + 现有代码差异分析（13 个 sheet） | 否（**不是运行时数据源**） |

> 两个 Excel 都在**仓库之外**（本地规格文件），不是代码资产，不进 git；本仓库只承载实现。

**落地结果**：

- **字段**：`case.py` 就地重建为 **161 字段 / 14 个按域子模型**（`case.n.neck_len`），手写不生成。
  口径：170（169 + 补入 `N.ACCESS_TORTUOSITY`）− 7 判定产物 − 3 物化派生 = 160。
  域子模型共用一个 `_StrictDomain` 基类（`extra="forbid"`）：抽取层拼错字段名会立刻报错，
  而不是静默丢事实。
- **规则**：`router.py` 就地重写为 **55 条判定规则**（G03 21 + G04 19 + G08 11 + G09 4）。
  G00–G02 的 9 条属「读取资料 / 证据质量 / 事实抽取」，归 LLM 抽取层，不进路由。
  `RULESET.md` 逐条登记 `rule_id ↔ 函数 ↔ V3.1(2) 源行`，测试守着两边不漂移。
- **为什么要写进代码而不是数据表**：V3.1(2) 的判据是自然语言（「允许值」列一列四义、
  AND/OR 混写、gate 只在自然语言里），无法机械转成数据行；拆解成本与直接写代码相当，
  而代码有类型检查与调试优势。
- **三值求值**：`True` / `False` / `None`(UNKNOWN)，Kleene 合取。**UNKNOWN 不短路、不当 False**；
  不确定值（可疑/疑似/未评价…）只能产生 UNKNOWN，但**规则显式列出时算命中**
  （规格把「医生明确写疑似 rAAA」当阳性触发）。
- **两个非规则标识**：`MANUAL.URGENCY`（医生置位 `urgency_flag` 越级）与
  `FALLBACK.VERIFY`（没有任何规则能判定 → 资料核实）。兜底不借 `G03.R15` / `G04.R14` 的 id，
  否则 `RULESET.md` 的出口列会说谎。
- **验收标准**（不再是「测试全绿」这一条）：55 条规则的三态用例、161 字段契约断言、
  架构守卫（判定层禁 LLM / 审计哈希链闭合 / 进哈希模型无时间戳）。

### ✅ 已完成

| 模块 | 内容 |
|---|---|
| **术前数据模型（5 个）** | `VascCase`（**161 字段 / 14 个按域子模型**）、`VascMeasurement`、`AnatomyScore`、`RiskAssessment`、`FinalRecommendation` |
| **枚举（2 个，各归其位）** | `FieldWriter`（写入者归责，定义在 `audit.py`）、`RouteMode`（路由模式三值，定义在 `recommendation.py`）。事实字段的取值域改用字段级 `Literal`（中文值直写）——V3.1(2) 的「允许值」列一列四义，单个 `StrEnum` 表达不了 |
| **审计留痕结构** | `FieldProvenance`（字段原文出处 / 写入者 / 是否被人工覆写）、`UrgencyFlagAudit` |
| **确定性规则层** | `router.py`（V3.1(2) 的 55 条判定规则 + 三值求值）+ `RULESET.md`（规则↔源行对照）+ `version.py`（规则版本），详见「四」 |
| **编排层（LangGraph）** | 四层节点 `input → router → execution → output` 串成 `StateGraph` 并接 SQLite Checkpoint；每节点自动留痕（节点名 / 起止时间 / 输入输出哈希），审计链可**逐环校验**，详见「五」 |
| **测试与工具链** | **1079 项测试**（86 路由基线 + 84 数据契约 + 115 编排层 + 220 规则分支 + 121 结论自证契约 + 12 CLI + 445 工具A）、`ruff` 无告警、覆盖率 **99%**（`router.py` 99%、`audit.py` 与 `recommendation.py` 100%、病种节点全部 ≥97%） |

### ⏳ 尚未开始

缺口分四类，**性质不同，都不应仓促补齐**——每一类都已在代码里写成显式的「不可用 + 原因」，
而不是留一个看起来能跑的空壳。

**① 等外部资源（技术缺口）**

- **工具A 已实现 D1–D7 全部 21 个病种节点（169 / 172 条规则）；B/C/D/E 仍未实现**（实现位置 `src/tools/<tool>.py`；工具A 是 `src/tools/guideline_engine/` 包）。未实现的工具都在
  自己的 `SPEC` 里写明了**具体缺什么**：指南引擎缺「接入哪部指南 + 条目结构化方式」、
  影像量化器缺「CTA 数据 + 分割与中心线算法」、解剖评分器与风险计算器缺「临床量表与模型系数」、
  记忆检索器缺「历史病例库与检索索引」。
  因此 `tool_outputs` 目前只含工具A 一项 —— **其余宁可空着，也不凭空造评分**。
  执行层已按路由模式调度（`execution_layer/plan.py` 的 `MODE_PLANS`）：
  **急诊 ∅ / 指南 A+B / 复杂全量**，三者是包含关系，不是并列关系。

**② 等临床口径（内容缺口）**

- **治疗方案清单的粒度上限**：`FinalRecommendation.treatment_options` 的每项是
  「术式：候选状态（原因）」，如 `EVAR：条件性候选（IFU 未核验）`。**不会**到
  「EVAR，经右股动脉，用 XX 型号」——规格有 5 处明文禁止工具A 走到器械选型
  （`D1.3.R03/R06/R90`、`D7.3.R01/R04`），那属于手术规划，不在本系统范围。
  形态由 `D1.3.R91` 定义：「分别列出 EVAR/OSR 的**可行候选**、**条件性候选**和**排除原因**」。
  `primary_recommendation` 同为**机械装配**既有结论，不含任何治疗主张。

**③ 等评估集（能力缺口）**

- **输入层 LLM 抽取器**与**输出层 LLM 转译** —— 全项目**仅有的两个**允许出现 LLM 的节点。
  二者均已留出接口并主动抛出 `NotImplementedError`，异常信息里写明「为什么现在不能做」：
  抽取器必须先有**标注病例评估集**才能度量字段级准确率，否则「LLM 只用于两处」这条红线
  虽在形式上成立、却无法被验证；转译只做措辞润色，得先有 ② 定稿的结论文本才有内容可转译。

**④ 其他**

- **术中 / 术后 Agent** —— `intraop_agent/`、`postop_agent/` 目前仅为目录骨架，无实现。

推进顺序遵循「**确定性内核优先**」：先落地数据模型、规则层与编排骨架，
**LLM 只在最后接入两端的抽取与转译节点**，中间管道保持零 LLM 参与。

---

## 三、目录结构

```
zs-Midea VascGuide/
├── docs/                        # 架构文档、临床指南（如 ESVS 2024）
├── src/
│   ├── core/                    # 核心引擎预留位〔骨架，当前未使用；见下方「说明」第 4 条〕
│   ├── models/                  # Pydantic 数据模型
│   │   ├── __init__.py          # ✅ 统一导出（模型 + 枚举 + 审计结构）
│   │   ├── audit.py             # ✅ 审计留痕结构（FieldProvenance / UrgencyFlagAudit）+ FieldWriter
│   │   ├── preop/               # ✅ 术前模型包（含 __init__.py 聚合导出）
│   │   │   ├── case.py          # ✅ VascCase（161 字段 / 14 个域子模型）+ derive() + apply_route_result()
│   │   │   ├── measurement.py   # ✅ VascMeasurement + CenterlinePoint（仅模型，无计算）
│   │   │   ├── assessment.py    # ✅ AnatomyScore + RiskAssessment（仅模型，无计算）
│   │   │   └── recommendation.py   # ✅ FinalRecommendation + EvidenceItem + DecisionStep + ToolFinding + RouteMode
│   │   ├── intraop.py           # IntraOpIntent（预留，空占位）
│   │   └── postop.py            # PostOpCase / FollowUpPlan / FollowUpStatus（预留，空占位）
│   ├── rules/                   # ✅ 确定性规则层（详见「四」；无 __init__.py）
│   │   ├── router.py            # ✅ 路由判定流水线（V3.1(2)：55 条判定规则 + 三值求值）
│   │   ├── version.py           # ✅ 规则版本号（RULESET_VERSION = v362@v3.6.2，进哈希）
│   │   └── RULESET.md           # ✅ 55 条规则 ↔ 函数名 ↔ V3.1(2) 源行 对照表（防规格漂移）
│   ├── tools/                   # ✅ 5 个确定性工具（一工具一模块，全部未实现）
│   │   ├── base.py              # ✅ ToolSpec / ToolRequest / MissingUpstreamError
│   │   ├── guideline_engine/    # ✅ 工具A · 指南引擎（21 节点 / 172 条中已实现 21 / 169，差 D3.3 的三条裁决留痕规则）
│   │   │   ├── __init__.py      # ✅ 公开接口：只 re-export SPEC / run（零逻辑）
│   │   │   ├── spec.py          # ✅ ToolSpec 实例（单列以避免 __init__ ↔ engine 循环导入）
│   │   │   ├── engine.py        # ✅ 节点遍历（深度优先 + 去重）+ 出口汇总
│   │   │   ├── state.py         # ✅ GuidelineState / NodeOutcome / GuidelineReport
│   │   │   ├── registry.py      # ✅ 规格 21 sheet / 172 条 与 已实现规模 的对照
│   │   │   └── nodes/           # 病种节点（按规格 sheet 分文件）
│   │   │       ├── common.py    # ✅ 优先级求值（P0→P3）+ 缺口 + 出户口径
│   │   │       ├── d1_asymptomatic.py # ✅ D1.1–D1.3（31 条全实现：阈值分层 / 获益风险 / 术式候选）
│   │   │       ├── d2_symptomatic.py # ✅ D2.1–D2.3（24 条全实现：症状归因 / 紧急度 / 术式候选）
│   │   │       ├── d4_infected.py    # ✅ D4.1–D4.3（25 条全实现：感染分型 / 感染源控制 / MAGIC）
│   │   │       ├── d5_special.py     # ✅ D5.1–D5.3（20 条全实现：特殊病变分诊 / 复杂性 / 炎症与形态）
│   │   │       ├── d6_postop.py      # ✅ D6.1–D6.3（28 条全实现：术后分诊 / 内漏与瘤囊 / 结构与靶血管）
│   │   │       └── d3_ruptured.py # ✅ D3.1–D3.3（20 条中实现 17 条）
│   │   ├── imaging_quantifier.py # 工具B · 影像量化器（阻塞：CTA 数据 + 分割算法）
│   │   ├── anatomy_scorer.py    # 工具C · 解剖评分器（阻塞：评分量表）
│   │   ├── memory_retriever.py  # 工具D · 记忆检索器（阻塞：历史病例库）
│   │   └── risk_calculator.py   # 工具E · 风险计算器（阻塞：风险模型系数）
│   ├── utils/                   # 工具函数（LLM 调用封装、Checkpoint 管理）〔骨架〕
│   ├── preop_agent/             # ✅ 术前决策 Agent（四层 + 编排，详见「五」）
│   │   ├── state.py             # ✅ PreopState + NodeAudit + stable_hash() + audited()
│   │   ├── graph.py             # ✅ StateGraph 装配 + SQLite Checkpoint + 序列化白名单
│   │   ├── input_layer/node.py  # ✅ 归一化 + 输入快照哈希（LLM 抽取器留空并显式抛错）
│   │   ├── router_layer/node.py # ✅ 对 src/rules/router.py 的薄封装（临床规则 0 行）
│   │   ├── execution_layer/     # ✅ 按模式调度（plan.py 计划表 / registry.py 登记表 / node.py 编排）
│   │   └── output_layer/node.py # ✅ 聚合 FinalRecommendation + 承接 tool_outputs → tool_findings
│   ├── intraop_agent/           # 术中导航 Agent〔骨架〕
│   │   ├── localization/        # 术中定位
│   │   ├── assessment/          # 术中评估
│   │   ├── monitoring/          # 生命体征监测
│   │   └── decision_tree/       # 确定性决策树
│   └── postop_agent/            # 术后随访 Agent〔骨架〕
│       ├── case_update/         # ① 病例更新
│       ├── risk_stratify/       # ② 风险分层
│       ├── followup_plan/       # ③ 随访规划
│       ├── anomaly_monitor/     # ④ 异常监控
│       ├── status_manager/      # ⑤ 状态管理
│       └── output_translate/    # ⑥ 输出转译
├── tests/                       # 测试（无 __init__.py，见「八、开发约定」）
│   ├── conftest.py              # ✅ 唯一病例构造入口：baseline_kwargs() + make_case()（域级浅合并）
│   ├── test_preop.py            # ✅ 86 项路由端到端基线（五出口 / 同桶优先级 / 标签）
│   ├── test_model_contract.py   # ✅ 84 项数据契约（161 字段 / 不确定值白名单 / 严格模式）
│   ├── test_preop_graph.py      # ✅ 115 项编排层 + 调度表 + 依赖排序 + Checkpoint 往返 + 架构守卫
│   ├── test_router_branches.py  # ✅ 220 项规则分支（55 条逐条命中 + 三值语义 + 派生量）
│   ├── test_router_note_contract.py # ✅ 121 项结论自证契约（rule_id / reason / gaps / trace）
│   ├── test_guideline_engine.py # ✅ 445 项工具A 契约（D1–D7 七条路径 + 粒度红线 / 不越权 / 缺口可见）
│   └── test_main.py             # ✅ 12 项 CLI 契约（错误提示可读性 / JSON 与文本同源 / 副作用边界）
├── cases/                       # 病例样例（JSON，直接喂给 main.py）
│   └── example.json             # ✅ 完整病例：走指南模式、零缺口、含 3 条原文出处
├── pyproject.toml               # 依赖与项目元数据（唯一"真源"）
├── uv.lock                      # 全量精确锁定（含传递依赖，由 uv 生成）
├── main.py                      # ✅ 命令行入口（病例 JSON → 结论 + 证据链 + 审计落盘）
├── LICENSE                      # MIT
├── .gitignore                   # Python 模板
├── .python-version              # 解释器版本锁定（3.13.14）
└── README.md                    # 本文档 —— 全项目唯一说明入口
```

**图例**：✅ = 已实现并通过测试；〔骨架〕= 只有目录结构，无业务逻辑；其余为预留文件。

> **说明**
> 1. 本仓库根目录即为项目根目录，**必须从仓库根运行**（`pyproject.toml` 中 `[tool.uv] package = false`，不是可安装包）。
> 2. **目录职责说明统一由本文档承载**，不再在子目录下另建 `README.md`（单一真源，避免多处维护漂移）。
> 3. **`__init__.py` 不是必需品**（PEP 420 命名空间包无它也能导入）。当前实际分布：
>    `src/models/` 与 `src/models/preop/` 保留（承载聚合 re-export）；
>    `src/rules/` 已移除（无 re-export 消费者）；`tests/` 已移除（非运行时包）。
>    是否省略的判据见「八、开发约定」。
> 4. **`src/core/` 目前是空预留位**。编排层（`state.py` / `graph.py`）落在了
>    `src/preop_agent/` 内，理由是现阶段只有术前 Agent 有编排需求，`PreopState` 也是
>    术前专属；放到 `src/core/` 反而会把「谁是术前专属、谁是三 Agent 共用」搅混。
>    若将来术中 / 术后也需要编排（届时 `NodeAudit` / `stable_hash` / `audited` 确实是
>    三 Agent 共用的），再把这三样上移到 `src/core/`，`PreopState` 留在原处。

---

## 四、核心模块：规则层（`src/rules/`）

> **治理红线：本层是判定层，禁止 `import` 任何 LLM 客户端。**
> LLM 仅允许出现在 `preop_agent/input_layer/`（输入抽取）与 `preop_agent/output_layer/`（输出转译）两处。
> 路由、解剖评分、风险分层、随访规划、异常检测、决策树仲裁 **全部为确定性代码**。

本目录承载 VascGuide 的**全部确定性规则**，是「规则主导，LLM 为辅」这条红线的物理落点。
它是一段**纯粹的确定性函数库**（性质等同 `import math`），**不是**交给 LLM 自主选择调用的工具（tool）。

### 1. 文件

| 文件 | 职责 | 状态 |
|---|---|---|
| `router.py` | 路由判定流水线（纯函数，无副作用、无 IO、无全局状态） | ✅ V3.1(2) 的 55 条判定规则（G03 21 / G04 19 / G08 11 / G09 4） |
| `version.py` | 规则版本号注册表（审计必需，进哈希） | ✅ `RULESET_VERSION = v362@v3.6.2` |
| `RULESET.md` | 55 条规则 ↔ 函数名 ↔ V3.1(2) 源行 的对照表 | ✅ 与代码逐条对应（测试守着） |

### 2. 路由做什么（以及不做什么）

`route(VascCase) → RouteResult`。它只回答**一个问题**：「这个病例该走哪条路？」

```
事实层输入 ──▶ G03 急诊(P01→P02→P03→P04) ──▶ G04 复杂(R01–R12/R15–R18→R13→R14) ──▶ G08 分类 ──▶ G09 收尾
                         │                                    │
                    G05 / 人工复核 / 资料核实            G06 / G07 / 资料核实
```

- **短路即优先级**：G03 的四个桶依次求值，命中即返回；`G03 回流 > G06 > VERIFY`。
  「急诊 > 复杂 > 指南」不是配置项，而是**代码的物理结构**。
- **兜底方向是保守的**：没有任何规则能判定 → `FALLBACK.VERIFY`（资料核实），
  **不默认「稳定」、也不拿 G07 当兜底**（G07 是排他分支：要求核心影像可评价）。
- **三值求值**：`True` / `False` / `None`(UNKNOWN)。`None` 既不是「没命中」也不是「阴性」，
  它只会让结论停在「资料核实」。
- **每一步都留痕**：逐条写入 `RuleTrace`（规则编号 / 三值结果 / 声明出口），**含被否决的步骤**；
  输出层再映射成 `DecisionStep`（序号 / 规则名 / 结果 / 规则编号 / V3.1(2) 源行）。
- **置信度是分级不是点估计**（B4/B5 裁定）：`高`（无缺口）/ `中`（有缺口）/ `低`（有决定性未判定项）。

### 3. 设计原则

1. **纯函数、无全局状态、无 IO** —— 相同输入必然得到相同输出（可复现）。
2. **输出结构化中间结果**（含逐条判定轨迹），供审计落盘。
3. **阈值 / 编号一律与 `VascGuide_V3.1 (2).xlsx` 对齐**（规格真源，仓库外的本地文件；有疑问先写进映射表，不回改源表）。

### 4. 变更纪律（审计红线）

> 改动任何**阈值 / 病种归属 / 覆盖规则**后，**必须**在 `src/rules/version.py` 手动 bump 对应版本号，
> 并在 `RULESET_CHANGELOG` 记录原因。否则历史结论无法追溯复现。

当前版本串见 `src/rules/version.py` 的 `RULESET_VERSION`（**单一真源**，本文件不复述
具体取值，避免与代码不一致）。需要时直接打印：

```bash
uv run python -c "from src.rules.version import RULESET_VERSION, RULESET_CHANGELOG; print(RULESET_VERSION)"
```

由于本层是一个**独立、可被工具扫描的目录**，这条纪律可以自动化检查，例如：

```
改动了 src/rules/** 但未改动 src/rules/version.py  →  构建失败
```

### 5. 与 `preop_agent/router_layer/` 的关系

`preop_agent/router_layer/` 必须是本目录的**薄封装** —— 只负责「图编排的胶水」
（State ↔ `VascCase` 翻译、调用 `route()`、写 Checkpoint），**严禁重复实现任何规则逻辑**。

| | `src/rules/`（本目录） | `preop_agent/router_layer/` |
|---|---|---|
| 关注 | 临床判定逻辑 | 图编排的胶水 |
| 输入 / 输出 | `VascCase` → `RouteResult` | LangGraph `State` → State + Checkpoint |
| 依赖 | 仅 `models`（零 IO / 零 LLM） | LangGraph、Checkpoint、日志 |
| 临床规则行数 | 本目录 | **0 行** |

`route()` 存在**两个调用者**：`VascCase.derive()`（服务无编排场景，如 CLI / 单测）与
`router_layer` 节点（服务 LangGraph 编排）。**两处都必须是薄封装。**

> **为什么规则不直接写在 `router_layer/` 里？** 因为三期 Agent 共享同一套临床口径，
> 且规则必须能独立测试 —— 86 项路由端到端测试 0.3 秒跑完，不需要 LangGraph、不需要数据库、
> 不需要任何 API Key（相比之下编排层测试要 4 秒）。
> 绑定到 `preop_agent/` 下会导致跨层循环依赖、版本号漂移，并使「规则变更纪律」无法被机器检查。

---

## 五、核心模块：编排层（`src/preop_agent/`）

编排层做三件事：**把四层节点串成图**、**为每一步留痕**、**把整份状态落盘到 SQLite**。

### 1. 文件

| 文件 | 职责 |
|---|---|
| `state.py` | `PreopState`（状态契约）、`NodeAudit`（节点留痕）、`stable_hash()`、`audited()` 包装器 |
| `graph.py` | `build_preop_graph()` 装配、`sqlite_checkpointer()` 落盘、`run_preop()` 入口、序列化白名单 |
| `input_layer/node.py` | 确定性归一化 + 输入快照哈希；LLM 抽取器留空 |
| `router_layer/node.py` | 对 `src/rules/router.py` 的薄封装（**临床规则 0 行**） |
| `execution_layer/plan.py` | `MODE_PLANS`：三模式各跑哪些工具（**唯一真源**） |
| `execution_layer/registry.py` | `TOOL_REGISTRY` / `SPEC_BY_KEY` / `run_tool()` 分派 |
| `execution_layer/node.py` | `execution_order()` 拓扑排序 + `execution_node()` 按模式调度 |
| `tools/<tool>.py` | 5 个确定性工具的实现位置（一工具一模块；工具A 为包且已实现 D1–D7 分支，B/C/D/E 未实现） |
| `output_layer/node.py` | 聚合 `FinalRecommendation`，并把 `tool_outputs` 渲染为 `tool_findings` |

### 2. 图与状态

```
START ─▶ input_layer ─▶ router_layer ─▶ execution_layer ─▶ output_layer ─▶ END
```

状态字段按推进顺序增长：`case` →（归一化）→ `case_id` / `input_snapshot_hash`
→ `route_result` → `tool_outputs` → `final_recommendation`，而 `audit_trail` 全程追加。

⚠️ **`tool_outputs` 不会自己走进结论**：输出层必须显式把它渲染成
`FinalRecommendation.tool_findings`（每个工具一条：键 + 中文名 + 要点摘要），
否则工具算了也「没有观众」。摘要只做机械渲染（枚举取中文值、`None` 不补占位、
嵌套列表不进摘要），**原始数值仍以 `tool_outputs` 为唯一真源**，不抄第二份。

两个刻意的设计：

- **`audit_trail` 用 `operator.add` reducer** —— 每个节点只负责「追加自己那一条」，
  合并交给框架，节点之间不必读彼此的轨迹，天然无读写竞争。
- **`input_snapshot_hash` 在输入层计算** —— 那时 `case` 还是纯输入事实；
  路由回填之后 `case` 已被写入派生字段，再取哈希就不叫「输入快照」了。

### 3. 审计链：可逐环校验

`audited()` 包装器让**每个节点自动生成**一条记录（节点名 / 起止时间 / 输入哈希 / 输出哈希），
节点作者**无从遗漏** —— 留痕是框架行为，不是人工纪律。

输入哈希与输出哈希取**同一口径**（都是「状态投影」的哈希，且剔除时间戳），
因此成立一条可验证的不变量：

```
audit_trail[i].output_hash == audit_trail[i+1].input_hash
```

即「上一节点的输出状态」就是「下一节点的输入状态」。测试逐环断言这条等式
（`test_audit_chain_closes_ring_by_ring`），任何一环被篡改都会立刻暴露。

哈希**排除** `audit_trail` 本身（它含时间戳），因此「同输入 + 同规则版本 → 同哈希」，
重复执行结果可复现（`test_hashes_are_reproducible_across_runs`）。

**同理，进哈希的产物模型里不得有任何时间戳字段**（如「量化时间」）。`tool_outputs` 在
哈希投影内，产物里带一个 `measured_at`，就会让同一份影像在两次不同时刻运行时得到不同哈希 ——
这与排除 `audit_trail` 是同一个理由。量化时间属**运行期元信息**，由 `audit_trail` 的
`NodeAudit` 记录；而 `method`（算法/版本）**必须保留**：算法变了，结论可信度就该变，
哈希理应随之改变。守门测试：`test_hashed_product_models_carry_no_timestamp_fields`。

### 4. 落盘策略与运行身份

「全程可审计」是红线，因此 `run_preop()` 的核心策略是**默认即安全**：

| 调用方式 | 行为 |
|---|---|
| `run_preop(case)` | **落盘**到 `.workbuddy/state/preop_checkpoints.sqlite`（该目录已被 `.gitignore` 忽略） |
| `run_preop(case, checkpointer=None)` | **不落盘**，并 `logging.warning` **出声** —— 仅限测试 / what-if 试算 / 压测 |
| `run_preop(case, thread_id="T")` | 以传入值为准（调用方**主动要求 resume / 回放**） |

改造前 `checkpointer` 默认 `None`，即「不传就不落盘」，而且**毫无提示** —— 属静默
fail-open，与红线直接冲突。现在改成 fail-loud：想放弃落盘，必须显式写出来并留下告警。

**运行身份与审计身份分开。** 不传 `thread_id` 时派生
`preop:{case_id}:{run_id}`（`run_id` = UTC 微秒时间戳 + 进程内序号）：

- **为什么不能只派生 `preop:{case_id}`**：`thread_id` 在 LangGraph 里是**状态机线程**，
  对同一 thread 再 `invoke` 是 **resume（续跑）**，不是「新开一次运行」。于是带
  `operator.add` reducer 的 `audit_trail` 会把上一次的记录一并带回（4 帧 → 8 帧），
  审计链不变量随之断裂；病例被修订后重跑，断链出现得更早。
- **为什么要带序号**：分区键一旦碰撞，两次运行会被**静默合并**成一条线程 —— 正是要
  消除的那类缺陷。时间戳保证**可排序**，序号保证**同进程内不重复**。
- **可检索性不受影响**：按前缀 `preop:{case_id}:` 即可取回该病例的**全部运行史**
  （由 `test_runs_of_one_case_are_retrievable_by_prefix` 守住）。

本次实际使用的 `thread_id` 与 `audit_persisted` 一起由返回值
（`PreopRunResult`）回传 —— 派生值含时间戳、调用方算不出来，若不回传，自动派生的
线程就成了「写进去但捞不出来」的死记录。这两个键都属**运行期元信息**：不进
`PreopState`、不进哈希、不进 checkpoint（各有测试守住）。

**审计库只用于审计，不得当作病例数据源。** 每条落盘记录里都含系统**自己的判定结论**
（`route_result` / `final_recommendation`），它的身份是「某次决策发生过的证据」，
不是「可供检索的病例样本」。拿它当病例库会同时坏掉两件事：库里混进示例 / 试算 /
测试产生的**非担责记录**（它们本不该进）；以及**循环论证** —— 用系统自己的输出
去支撑系统自己的下一次输出。因此纪律是：

- 默认库只放**真实发生、要担责**的记录；演示 / what-if / 测试一律
  `checkpointer=None`（审计判据见下）；
- 将来若需要「历史病例库 / 相似病例检索」，必须是**独立、已脱敏**的数据源，
  与审计库是两条链路。

判据（**双向**）：这条结论三个月后被质疑，我要不要拿它当依据？要 → 必须落盘；
不要 → 必须不落盘。**该进的不进 = 红线失效；不该进的进了 = 稀释审计库可信度。**

### 5. 三条被机器守住的架构红线

规则不靠自觉，靠测试。以下三条由 `tests/test_preop_graph.py` 机械执行：

| 守卫 | 做法 |
|---|---|
| **薄封装不藏规则** | AST 扫描 `router_layer/node.py`：代码里不得出现任何**数值常量**（阈值）、病种 / 解剖 / 处置**关键词**、以及**规则编号** |
| **判定层零 LLM** | AST 扫描 `src/rules/`、`src/tools/` 与 `router_layer/` `execution_layer/` `output_layer/` 的全部 `import`，出现任何 LLM 客户端 SDK 即失败 |
| **序列化白名单完整** | 从真实 state 反推全部自定义类型，与 `build_checkpoint_serde()` 的白名单比对；漏注册即失败（详见本节第 6 小节） |

`input_layer/` 是唯一豁免的层（它是被允许的 LLM 入口），且其抽取函数当前主动抛错。

### 6. 一个必须显式处理的序列化坑

LangGraph 用 msgpack 序列化状态。对本项目自定义类型（pydantic 模型、dataclass、枚举），
它当前的行为是「**放行但告警**」，并提示：

> Deserializing unregistered type … This will be blocked in a future version.

也就是说：**不注册也能跑，但未来某个版本会直接阻断反序列化**，届时历史 Checkpoint
全部读不回来 —— 审计链会在「升级」这个动作上悄悄断掉。故 `graph.py` 通过
`build_checkpoint_serde()` 显式声明全部允许类型（`allowed_msgpack_modules`）。
这不是可选优化，而是审计红线要求的防御。

**白名单是递归展开的，不是手写清单。** `allowed_msgpack_types()` 从一组根模型出发，
递归收集其**嵌套引用**的全部自定义类型（含 `list[X]` / `X | None` 等容器写法）。
原因是一个实测教训：手写清单时最常漏的恰恰是**嵌在模型内部的类型** ——
初版就漏掉了 `FinalRecommendation` 里的 `DecisionStep` / `EvidenceItem` 与
`VascMeasurement` 里的 `CenterlinePoint`。递归展开后，「凡是 state 里可达的自定义类型
都被登记」成为**构造性事实**，不再依赖开发者每次新增模型时记得回来补一行。

**这一条由测试守住**（`test_allowlist_covers_every_type_in_state`）：测试从一次真实运行
的 state 里**反推**出全部自定义类型，再与白名单比对，因此新增模型若漏注册会立即失败。
另有一个阳性对照（`test_canary_unregistered_types_are_observably_flagged`）绕开白名单
构造 saver，证明「零告警」这个断言不是空的。

> ⚠️ **一个反直觉的实测结论**：`LANGGRAPH_STRICT_MSGPACK=true` **不是**更可靠的守卫。
> 实测在该模式下，未注册类型的 state 依然能正确往返，而且**不再产生任何告警** ——
> 既不报错也不提示，风险反而更隐蔽。所以真正的防线只有「白名单完整 + 测试比对」，
> 不要指望这个环境变量替你发现问题。

### 7. 编排层当前的三处留白

| 留白 | 位置 | 当前状态 |
|---|---|---|
| 治疗方案清单 | `output_layer` 的 `treatment_options` | 已产出「术式：候选状态（原因）」；**粒度上限**由规格 5 处禁止条款决定，不是待办 |
| LLM 输出转译 | `FinalRecommendation.translated_text` | 保持 `None` |
| B/C/D/E 四个工具 | `src/tools/<tool>.py` 的 `SPEC.blocked_reason` | 每个工具写明阻塞原因；工具A 已实现，故 `tool_outputs` 含 `guideline_engine` |

**为什么宁可留白**：这三处一旦填上，就等于往代码里引入了一批**新的「规则内容」**
（评分口径、建议文案）。按本项目的变更纪律，那必须新增一张版本表并注册进
`src/rules/version.py`，否则审计链恰在这一点断开。所以正确顺序是**先定口径、再写代码**。

---

## 六、快速开始

> **环境要求：Python 3.13.x**（本项目锁定 `3.13.14`，见 `.python-version` 与「七、依赖版本策略」）。
> 依赖与环境由 **uv** 管理（`pyproject.toml` + `uv.lock`），一条命令即可同步。

```bash
# 0) 如未安装 uv：
#    Windows(PowerShell): irm https://astral.sh/uv/install.ps1 | iex
#    macOS / Linux:       curl -LsSf https://astral.sh/uv/install.sh | sh

# 1) 建环境 + 安装全部依赖（≈ npm install）
uv sync

# 2) 跑一个示例病例（≈ npm run）
uv run python main.py --case cases/example.json
```

> - uv 会自动创建 `.venv/` 并按 `uv.lock` **精确安装**，**无需手动 `activate`**；
> - 首次克隆若尚无 `uv.lock`，执行 `uv sync` 会自动生成。

预期输出：该病例的出口（指南模式 / `G04.R14`）、置信度、病种标签、资料缺口，
以及**证据链**（结论 → 每条规则读了哪些字段 → 原文字段出处）与审计留痕（哈希链闭合校验）。

### 用命令行跑病例

```bash
uv run python main.py --case cases/example.json        # 结论 + 证据链（默认层）
uv run python main.py --case cases/example.json -v     # 展开全部规则与全部缺口
uv run python main.py --case cases/example.json -vv    # 再加完整决策链（含被否决项）
uv run python main.py --case cases/example.json --json # 机器可读（route_result + final_recommendation）
uv run python main.py --dump-template > cases/my.json  # 生成 161 字段空模板
```

| 开关 | 作用 |
|---|---|
| `-v` / `-vv` | 递增详细度。默认层只展开**成立**的规则；`-v` 展开全部 49 条（含被否决项）；`-vv` 加 `decision_path` |
| `--json` | 输出 `route_result` + `final_recommendation` + `audit`，与文本渲染同源 |
| `--no-persist` | 不写审计库。**仅限试算** —— 正式评估必须落盘，否则事后无法追溯 |
| `--thread-id` | 指定审计线程（续跑已有运行；默认每次运行新开一条） |

**退出码**：`0` 成功 / `1` 输入或运行错误 / `2` 命令行用法错误。无参数运行时打印帮助并返回 `2`。

> **病例 JSON 是手工填的**：LLM 抽取器（病历原文 → `VascCase`）尚未实现，所以目前
> 病例由结构化字段直接构造。这不影响跑通 —— `cases/example.json` 就是一份完整病例。
> 缺字段不会报错，而是被判为 **UNKNOWN** 并汇总进「资料缺口」，逐次补齐即可看到结论变化。

### 在自己的代码里调用（编排层）

需要嵌入自己的程序时，直接调 `run_preop`，`VascCase` 的 161 个字段**按域分组**，
取值用**中文字面量**直写（V3.1(2) 的「允许值」原文）：

> 域前缀：`p` 人口学 / `t` 时间基准 / `d` 原始文书 / `q` 数据质量 / `s` 症状体征 / `v` 生命体征 /
> `lab` 化验 / `c` CTA 报告文字 / `n` 解剖（颈·肾·髂·入路）/ `gen` 遗传 / `inf` 炎症 /
> `cl` 分型输入 / `k` 会话级标记（`K.*`）/ `r` 路由回填（`R.*`）。

```python
from src.models.preop import VascCase
from src.preop_agent.graph import run_preop, sqlite_checkpointer

case = VascCase(
    case_id="DEMO-001",
    p={"age": 72, "sex": "男"},
    d={"dx": "肯定", "cta": "腹主动脉瘤，最大径 42mm"},
    q={"idmatch": "一致", "time": "有效", "cta_quality": "可评价"},
    s={"pain_present": "否", "syncope": "否定", "abd_tender": "阴性", "aaa_tender": "阴性"},
    v={"sbp": 120.0, "dbp": 70.0, "hr": 78.0, "rr": 18.0, "spo2": 97.0, "temp": 36.5, "acvpu": "A"},
    n={
        "cta_status": "完整",
        "neck_len": 25.0,
        "neck_d": 22.0,
        "neck_angle": 20.0,
        "renal_r": "未受累",
        "renal_l": "未受累",
        "seal_ifu": "符合",
        "access_occlusion": "未见闭塞",
    },
)

# 不传 checkpointer → 默认落盘到 .workbuddy/state/preop_checkpoints.sqlite
state = run_preop(case)

r = state["route_result"]
print(r.kind, r.rule_id, r.next_node)  # 资料核实 FALLBACK.VERIFY 资料核实
print(r.confidence_level, len(r.evidence_gaps))  # 低 32
print([record.node for record in state["audit_trail"]])  # 四个节点
print(state["thread_id"])  # 本次落在哪条线程（派生值，须回传）
print(state["audit_persisted"])  # True
```

> **这个出口不是故障**：上例刻意只填关键域，缺的 32 项（破裂征象、疼痛细节、Hb 系列等）被判为
> **UNKNOWN 而不是「阴性」**，汇总进 `evidence_gaps` 后走「资料核实」—— 这正是「未提及 ≠ 无」的兑现：
> 宁可让人去补资料，也不允许缺项悄悄算作阴性。
> 字段补齐后同一套代码即落到「指南模式」：以 `tests/conftest.py` 的 `baseline_kwargs()`（161 字段全有值的
> 基线）为例，`route_mode` 为 **指南模式**、命中规则 `G04.R14`、`decision_path` 49 步（含被否决项）。

换一个**独立连接**读回同一份 state —— 这就是「全程可审计」的兑现：

```python
from src.preop_agent.graph import build_preop_graph, thread_config

with sqlite_checkpointer() as reopened:
    app = build_preop_graph(checkpointer=reopened)
    values = app.get_state(thread_config(state["thread_id"])).values
    print(values["case"].case_id)  # DEMO-001
```

> **字段访问一律带域前缀**：`case.p.age` / `case.n.neck_len`，域内字段平铺中文字面量（`case.p.sex == "男"`）。
> **不存在** `src.models.enums` 这类共享枚举模块 —— V3.1(2) 的「允许值」列一列四义，共享枚举表达不了，
> 故每个字段用自己的 `Literal`；仅有的两个跨字段枚举各归其消费者：`FieldWriter` 在 `audit.py`、
> `RouteMode` 在 `preop/recommendation.py`。

> 脚本须**从仓库根**运行（`uv run python xxx.py` 或 `python -m`），否则 `import src` 会失败。

### 常用命令（测试 / 检查）

```bash
# 跑全部测试（应为 712 passed, 4 skipped）
uv run pytest tests/ -q

# 静态检查（应为 All checks passed）
uv run ruff check .

# 格式检查（不改文件；加 --diff 可预览改动）
uv run ruff format --check .
uv run ruff format .                    # 真正改写文件

# 覆盖率（按需，比朴素跑慢约 7 倍，会在仓库根生成 .coverage）
uv run pytest tests/ --cov --cov-report=term-missing
```

> 也可以用解释器直接调用（等价）：`.venv/Scripts/python.exe -m pytest tests/ -q`。
> **`pytest` 必须能从仓库根导入 `src`**，因此 `pyproject.toml` 保留了
> `[tool.pytest.ini_options] pythonpath = ["."]` —— 移除它会让裸命令 `pytest` 报
> `ModuleNotFoundError: No module named 'src'`（详见「八、开发约定」）。

### 查看测试结果

- **逐条通过/失败**：`pytest tests/ -v`，或 `--junit-xml=report.xml` 导出后转 HTML；
- **覆盖了哪些代码**：`--cov-report=html` 生成 `htmlcov/index.html`，红色行即未被测试执行到的分支。

> ⚠️ **覆盖率 ≠ 测试结果**：前者衡量「代码有多少行被执行到」，后者衡量「每个用例过没过」。
> 两者互补 —— 一个看「测了没有」，一个看「测的对不对」。当前 712 项全绿、覆盖率 99%。

---

## 七、依赖版本策略（已锁定）

> 锁定日期：**2026-10-09**。锁定后如无必要请勿升级，避免版本漂移导致安装/运行不一致。

### 1. 解释器版本

| 项目 | 版本 | 说明 |
|---|---|---|
| 目标 | **Python 3.13.x** | 锁定 `3.13.14`（实测通过），见 `.python-version` |
| 下限 | ≥ 3.12 | 由 `numpy 2.5.x` 决定（其要求 `>=3.12`） |
| 上限 | < 3.14 | `langgraph 1.2.x` 官方分类器目前止于 3.13，故 3.14 暂不采用 |

### 2. 运行时依赖（精确锁定）

| 包 | 锁定版本 | 作用 | 对 Python 要求 |
|---|---|---|---|
| langgraph | `1.2.14` | 编排 / 状态机 / Checkpoint | ≥ 3.10 |
| langchain | `1.4.4` | Agent 框架（1.x） | ≥ 3.10, < 4 |
| langchain-core | `1.6.9` | 生态基座（兼容性锚点） | ≥ 3.10, < 4 |
| langchain-openai | `1.7.0` | OpenAI / DeepSeek 兼容接入 | ≥ 3.10 |
| langgraph-checkpoint-sqlite | `3.1.1` | Checkpoint 落 SQLite（全程可审计） | ≥ 3.10 |
| python-dotenv | `1.2.4` | 读取 `.env` 配置 | ≥ 3.10 |
| pydantic | `2.14.0` | 数据模型 / 校验 | ≥ 3.10 |
| numpy | `2.5.3` | 数值计算 | ≥ 3.12 |
| scikit-learn | `1.9.1` | 风险计算（logistic）等 | ≥ 3.11 |

### 3. 兼容性要点（必须同时满足）

- **Python 下限由 numpy 决定**：`numpy 2.5.3` 要求 `>=3.12`，`scikit-learn 1.9.1` 要求 `>=3.11`，其余库仅要求 `>=3.10`。取交集 → **Python 3.12 ~ 3.13**，本项目锁定 **3.13.14**。
- **LangChain 家族必须「同代」**：`langchain` / `langchain-core` / `langchain-openai` 互为依赖，必须同时处于 **1.x** 世代。一旦混入旧版 `langchain-core`（0.x），安装即报依赖冲突 —— 这是最常见的坑。
- **langgraph 与 langchain 同代**：LangChain 1.x 的 agent 运行在 LangGraph 之上，本组合取 `langgraph 1.2.x` + `langchain 1.x`。
- **pydantic 必须 v2**：`langchain-core 1.x` 依赖 pydantic v2，本组合用 `2.14.0`；切勿混入 pydantic v1（且 pydantic v1 不兼容 Python ≥3.14）。
- **numpy 2.x 与 scikit-learn 1.9.x 兼容**：sklearn 要求 `numpy>=1.24.1`、`scipy>=1.10.0`，`numpy 2.5.3` 满足。
- **依赖真源与锁定**：直接依赖写在 `pyproject.toml`；`uv.lock` 锁定**全部（含传递）**依赖，二者均入库，保证任何人 `uv sync` 得到一致结果。
- **传递依赖**：`scipy`、`openai`、`pydantic-core`、`joblib` 等**无需手写**，由 `uv.lock` 统一精确锁定。

### 4. 安装与「完全复现」

```bash
uv sync              # 按 pyproject.toml + uv.lock 建环境并精确安装（≈ npm install / npm ci）
uv lock              # 依赖变更后刷新 uv.lock（≈ 更新 package-lock.json）
uv add <pkg>         # 新增依赖（自动写入 pyproject.toml 并刷新锁 ≈ npm install <pkg>）
uv add --dev <pkg>   # 新增开发依赖
uv run <cmd>         # 在项目环境中执行命令（≈ npm run）
```

### 5. 复核与注意

```bash
uv run python -V     # 应输出 Python 3.13.x
uv tree              # 查看依赖树
uv pip check         # 检查是否存在依赖冲突
```

- 上述版本为 **2026-10-09** 各库当前最新稳定版。
- ✅ **已用 `uv lock` 实际解析验证**（2026-10-09）：共 **65 个包**全部解析成功、无依赖冲突，上表 9 个直接依赖版本与 `uv.lock` **完全一致**。
- 传递依赖由 `uv.lock` 精确锁定，例如 `scipy 1.18.1`、`openai 3.26.1`、`pydantic-core 2.50.0`、`langgraph-checkpoint 4.2.0`；开发依赖 `pytest 9.1.1`、`pytest-cov 7.1.0`、`ruff 0.16.10`。

---

## 八、开发约定

- **规则优先**：凡属确定性判定（路由 / 评分 / 分层 / 规划 / 预警 / 仲裁）一律以确定性代码实现，不交由 LLM。
- **LLM 边界**：LLM 仅承担输入抽取与输出转译，且不得改写数值与结论。
- **规格真源唯一**：判定口径以 `VascGuide_V3.1 (2).xlsx` 为准，**只读不改**；所有澄清、裁定、差异分析
  一律写进《字段与规则映射表》，不回写规格。映射表**不是运行时数据源**——程序不读 Excel。
- **代码亦是规格的第二真源**：规则写进代码后，每条规则函数必须标注其 V3.1(2) 来源（rule_id + 源行号），
  并由 `RULES` 字典 + 数量断言（`len(RULES) == 54`）机械守住，防止「代码与规格漂移」。
- **判定层禁 LLM**：`src/rules/**` 及全部判定层函数**禁止 import 任何 LLM 客户端** —— 硬性约束（见「四」）。
- **规则变更纪律**：改动任何阈值 / 病种归属 / 覆盖规则后，**必须** bump `src/rules/version.py` 的版本号
  并在 `RULESET_CHANGELOG` 记录原因；否则历史结论无法追溯复现。
- **可审计**：全链路使用 LangGraph Checkpoint 留存状态，所有建议附证据链，可追溯至原始字段出处。
- **模型与规则分离**：`models/` 只管结构，`rules/` 只管判定；依赖方向单向 `models → rules`，
  跨层引用用**延迟导入**破循环（见 `case.py::derive()` 内部导入）。
- **`VascCase` 管输入不管产物**：工具产物各自独立成模型，**不回写 `VascCase`**。
- **文档单一真源**：目录职责统一在本文档「三、目录结构」维护，**不在子目录下另建 `README.md`**，
  避免多处维护导致漂移。
- **增量填充**：按「确定性内核优先」顺序推进，每阶段须保证测试全绿 + `ruff` 无告警。
  **例外**：V3.1(2) 重写期间（见「二」）测试与被测代码**同批重写**，红灯是预期状态；
  但 `ruff` 仍须无告警。
- **`__init__.py` 非必需**：Python 3 命名空间包（PEP 420）无它也能导入。是否保留按实际需要判断 ——
  需要 **re-export / 显式包边界** 时保留；纯标记用途可省略。
  省略前须确认两点：①本包无 `from .pkg import X` 形式的 re-export 消费者（否则直接 ImportError）；
  ②目录内还有其他文件（**git 不跟踪空目录**，否则目录会从版本库消失）。
  若省略的是 pytest 直接运行的目录，须保留 `pyproject.toml` 的 `pythonpath = ["."]`。

---

## 九、许可证

本项目基于 [MIT License](./LICENSE) 开源。
