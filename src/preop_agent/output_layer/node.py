"""输出层节点 —— 把判定结果聚合为 :class:`FinalRecommendation`（决策产物）。

本层做五件事（全部**机械可验证**，无一处自由裁量）：
    ① ``trace → decision_path``：``RuleTrace`` → ``DecisionStep``，**含被否决项**
       （审计要求：不得只留命中项）。三值结果原样保留 —— ``None`` 是「未判定」，
       转成 ``False`` 会把「资料不足」伪装成「已排除」；
    ② ``ruleset_version``：直接取自 :class:`RouteResult` 的复合版本串；
    ③ ``input_snapshot_hash``：取自输入层算好的输入快照哈希；
    ④ ``evidence_chain``：三层可逐环核对 —— **结论 → 每条规则的实际判定与它读到的
       字段值 → ``VascCase.field_evidence`` 里的原文出处**。被否决的规则同样入链
       （不得只留命中项），否则「为什么排除其他分支」无从复核；
    ⑤ ``tool_findings``：各工具产物的要点摘要（见下节）。

## 为什么必须读 ``tool_outputs``

执行层把工具产物写进 ``state.tool_outputs``，但**工具产物不会自己走进结论**。
本层若不读它，工具算出的东西就只存在于状态与 checkpoint 里 —— 医生看不到、
证据链里也没有它，等于白算。故本层显式消费 ``tool_outputs``，转成
:class:`ToolFinding`（呈现条目）。

刻意**不把原始数值抄进结论**：``tool_outputs`` 是唯一真源，摘要只做渲染
（枚举取中文值、数值原样、缺失不补），抄一份就成两处真相。

与 ``evidence_chain`` 的分工：
    ``evidence_chain`` = **判定依据**（为什么这样路由）；
    ``tool_findings``  = **测量产物**（影像/评分/检索各算出了什么）。
    两者不互相复制 —— 同一条内容出现在两处，日后改一处就会不一致。

## 一处已落地、一处留白

**已落地：主建议改为机械装配。**
    ``primary_recommendation`` 不再是占位符，而是把既有结论排成一句话
    （见 :func:`_build_primary_recommendation`）：:

        急诊模式 · 破裂／疑似破裂 AAA：EVAR 候选（IFU 待核验，术者确认）；待补 6 项关键资料

    **它仍不含任何临床主张**：只重排 ``route_result`` 与 ``tool_outputs`` 里
    已有的内容，本层不引入任何「如果 A 则建议 B」的分支。

    ``treatment_options`` 取工具A 的 ``candidates``，每项是「术式：状态（原因）」，
    如 ``EVAR：条件性候选（IFU 未核验）``。**不到**「EVAR，经右股动脉，用 XX 型号」
    这一层——规格有 5 处明文禁止工具A 走到器械型号（``D1.3.R03/R06/R90``、
    ``D7.3.R01/R04``），那属于手术规划，不在本系统范围。粒度由 ``D1.3.R91`` 定义：
    「分别列出 EVAR/OSR 的可行候选、条件性候选和排除原因」。

**留白二：LLM 输出转译。**
    ``translated_text`` 保持 ``None``（见 :data:`TRANSLATION_BLOCKED_REASON`）。
    该字段属于「LLM 只出现在两处」红线中的第二处（输出转译），但它的价值是
    **措辞润色**，前提是主建议的措辞已由临床确认；当前文案是机械拼接，
    还没有到"需要润色"的状态。

治理红线：
    本层是**聚合与转写**，不是判定。不得在此引入任何新的判定分支或阈值——
    任何「如果 A 则 B」的临床逻辑一旦出现在这里，就等于绕过 ``src/rules`` 另立了一处规则。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any, Final

from pydantic import BaseModel

from ...models.preop.case import VascCase
from ...models.preop.recommendation import (
    DecisionStep,
    EmergencyBrief,
    EvidenceItem,
    FinalRecommendation,
    RouteMode,
    ToolFinding,
)
from ...rules.router import RULES, RouteKind, RouteResult, RuleTrace, _read_path
from ..execution_layer.registry import SPEC_BY_KEY
from ..state import PreopState

__all__ = [
    "PRIMARY_RECOMMENDATION_PLACEHOLDER",
    "TRANSLATION_BLOCKED_REASON",
    "TRANSLATION_IMPLEMENTED",
    "build_tool_findings",
    "output_node",
]

#: 单条摘要最多呈现多少个字段（防长模型把结论撑爆；超出部分只报总数）。
MAX_SUMMARY_FIELDS: Final[int] = 10

#: 摘要中跳过的字段：外键对结论读者无信息量（``case_id`` 已在结论顶层）。
SUMMARY_SKIPPED_FIELDS: Final[frozenset[str]] = frozenset({"case_id"})

#: 非模型产物（不该发生，但兜底）的摘要截断长度。
MAX_FALLBACK_SUMMARY_CHARS: Final[int] = 120

#: 三值判定 → 中文。``None`` 必须单独措辞：它是「未判定」，不是「不成立」。
VERDICT_TEXT: Final[Mapping[bool | None, str]] = {True: "成立", False: "不成立", None: "无法判定"}

#: 主建议的兜底文本 —— 仅在**既无模式也无工具结论**时出现。
#: 正常情况下主建议由 :func:`_build_primary_recommendation` 装配。
PRIMARY_RECOMMENDATION_PLACEHOLDER: str = (
    "〔无法给出结论〕本次运行既未进入任何路由模式，也没有病种流程产物。"
    "请检查路由层输出与工具执行记录，不要据本字段做任何临床判断。"
)

#: 病种标签 → 中文名。**逐字取自规格 G08**「D1无症状AAA；D2症状性未破裂AAA；
#: D3破裂/疑似破裂AAA；D4感染/相关瘘；D5 PAU/IMH/IAAD/囊状/假性/炎症性病变；
#: D6术后并发症；D7髂动脉瘤」，不另行命名。
DISEASE_LABELS: Final[dict[str, str]] = {
    "D1": "无症状 AAA",
    "D2": "症状性未破裂 AAA",
    "D3": "破裂／疑似破裂 AAA",
    "D4": "感染／相关瘘",
    "D5": "PAU／IMH／IAAD／囊状／假性／炎症性病变",
    # 「术后并发症」是 D6 的一部分而不是它的全部：常规随访的术后病人没有
    # 并发症，用「并发症」命名会让一句话结论读起来像已经确诊了。
    "D6": "主动脉修复术后",
    "D7": "髂动脉瘤",
}

#: LLM 输出转译是否已实现（当前为 ``False``）。
TRANSLATION_IMPLEMENTED: bool = False

#: 输出转译未实现的原因。
TRANSLATION_BLOCKED_REASON: str = (
    "LLM 输出转译未实现：转译只做措辞润色、不改结论，其输入应是**已确定的结论文本**。"
    "当前 primary_recommendation 是机械装配的（模式 · 病种 · 终点 · 待补数），"
    "措辞尚未经临床确认；在此之上做润色会掩盖「哪些话是系统拼的、哪些是模型写的」。"
)


def _render(value: Any) -> str:
    """把字段值渲染为人类可读字符串（枚举取中文值，列表/字典展开）。

    空容器要显式说「（空）」：``d.surg = `` 这种右值留白的写法在证据链里
    会被误读成「漏了」，而它其实是一个已求值但为空的字段。
    """
    if value is None:
        return "（未提及）"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (list, tuple)):
        return "、".join(_render(v) for v in value) or "（空）"
    if isinstance(value, dict):
        return "、".join(f"{key}={_render(item)}" for key, item in value.items()) or "（空）"
    return str(value)


def _render_scalar(value: Any) -> str:
    """标量渲染：枚举取中文值、布尔转「是/否」、其余原样输出。"""
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _field_label(name: str, description: str | None) -> str:
    """字段展示名：优先用 ``Field(description=...)``，缺失时退回字段名。

    复用 pydantic 的 description 而非另建一份中文标签表 —— 标签只存在一处，
    改字段说明时摘要自动跟上，不会两处漂移。
    """
    return (description or "").strip().rstrip("。；;") or name


def _summarize_guideline_report(product: Any) -> str | None:
    """工具A 的产物按「终点 → 路径 → 命中规则 → 缺口数」渲染。

    为什么用鸭子类型而不是 ``isinstance``：``output_layer`` 不该 import ``src.tools``
    ——工具层是可替换的，编排层只认产物形状。``path`` / ``terminal`` / ``entries``
    这三个属性同时出现的类型，目前只有 ``GuidelineReport``。

    不这么做的后果是本轮实测到的：整份报告被 ``repr()`` 截成一行，
    术式方向（``terminal``）和缺口全部看不见。
    """
    path = getattr(product, "path", None)
    entries = getattr(product, "entries", None)
    gaps = getattr(product, "gaps", None)
    if not isinstance(path, tuple) or not isinstance(entries, tuple) or not isinstance(gaps, tuple):
        return None

    parts = [
        f"终点 = {getattr(product, 'terminal', None) or '未给出'}",
        f"路径 = {' → '.join(path) or '未进入节点'}",
    ]
    hits = [
        entry.rule_id
        for entry in entries
        if getattr(entry, "verdict", None) is True and hasattr(entry, "rule_id")
    ]
    if hits:
        parts.append(f"命中 = {'、'.join(hits)}")
    if gaps:
        # 列前两条明细：只说「缺口 6 项」等于把待补资料藏起来，
        # 而 G05 明确要求输出「待补关键资料」。
        head = "；".join(gaps[:2])
        more = f" 等 {len(gaps)} 项" if len(gaps) > 2 else ""
        parts.append(f"缺口 = {head}{more}")
    else:
        parts.append("缺口 = 无")
    return "；".join(parts)


def _summarize_product(product: Any) -> str:
    """把一个工具产物机械渲染成「字段 = 值；…」摘要（不改数、不推断）。"""
    if product is None:
        return "（无产出）"
    guideline = _summarize_guideline_report(product)
    if guideline is not None:
        return guideline
    if not isinstance(product, BaseModel):
        text = repr(product)
        if len(text) <= MAX_FALLBACK_SUMMARY_CHARS:
            return text
        return text[:MAX_FALLBACK_SUMMARY_CHARS] + "…"

    parts: list[str] = []
    for name, info in type(product).model_fields.items():
        if name in SUMMARY_SKIPPED_FIELDS:
            continue
        value = getattr(product, name, None)
        # 只呈现标量：嵌套模型与列表（如中心线剖面）对结论读者是噪声。
        if value is None or not isinstance(value, (str, bool, int, float, Enum)):
            continue
        parts.append(f"{_field_label(name, info.description)} = {_render_scalar(value)}")

    if not parts:
        return "（无标量产出）"
    shown = parts[:MAX_SUMMARY_FIELDS]
    if len(parts) > MAX_SUMMARY_FIELDS:
        shown.append(f"等共 {len(parts)} 项")
    return "；".join(shown)


def build_tool_findings(tool_outputs: Mapping[str, Any]) -> list[ToolFinding]:
    """``tool_outputs`` → 呈现条目列表。

    按工具键**升序**呈现（而非执行顺序）：``final_recommendation`` 参与审计哈希，
    顺序一旦依赖字典插入顺序，调度顺序变化就会改变哈希。排序把这一层不确定性挡在外面。
    """
    findings: list[ToolFinding] = []
    for key in sorted(tool_outputs):
        spec = SPEC_BY_KEY.get(key)
        findings.append(
            ToolFinding(
                tool_key=key,
                label=spec.label if spec is not None else key,
                summary=_summarize_product(tool_outputs[key]),
            )
        )
    return findings


def _to_decision_steps(trace: Sequence[RuleTrace]) -> list[DecisionStep]:
    """``RuleTrace`` → ``DecisionStep``：**保留全部被否决步骤**，三值结果原样保留。

    映射是机械的（但不再 1:1，因为 ``RuleTrace`` 只有三格）：

    ============  =====================================
    DecisionStep   来源
    ============  =====================================
    order          轨迹中的位置（从 1 起）
    condition      ``RULES[rule_id].name``（规则名）
    result         ``verdict``（True / False / None 三值）
    rule_ref       ``rule_id``
    note           ``RULES[rule_id].source``（V3.6.2 源单元格）
    ============  =====================================
    """
    steps: list[DecisionStep] = []
    for index, entry in enumerate(trace, start=1):
        spec = RULES.get(entry.rule_id)
        steps.append(
            DecisionStep(
                order=index,
                condition=spec.name if spec else entry.rule_id,
                result=entry.verdict,
                rule_ref=entry.rule_id,
                note=spec.source if spec else None,
            )
        )
    return steps


def _build_evidence(case: VascCase, result: RouteResult) -> list[EvidenceItem]:
    """构造证据链：**结论 → 规则 → 字段值 → 原文出处**，四环各自可独立核对。

    证据链回答的是「这个结论怎么推出来的」，不是「记录有没有被改过」（后者是
    ``audit_trail`` 的哈希链）。因此它必须做到 **每一环都能被第三个人复算**：

    ==========  ===========================================================
    环节        内容 / 来源
    ==========  ===========================================================
    ① 结论      ``result.reason`` + 命中规则 ``result.rule_id``
    ② 规则      ``result.trace`` 逐条（**含被否决项**），每条写明判定结果
                与它读到的字段实际值
    ③ 字段      字段清单取自 ``RULES[rule_id].fields`` —— 规则自己声明的输入，
                不在此处另立一份「这条规则读什么」的映射（那是第二处真源）
    ④ 原文      ``case.field_evidence[path].source_quote``，缺则留空
    ==========  ===========================================================

    ② 与 ④ 刻意分成两段而不是逐字段嵌套：同一字段可能被多条规则读
    （如 ``cl.aaa_rupture`` 同时被 ``G08.R01`` / ``G08.R03`` 读），
    若把原文挂在每条规则下会重复 N 次；单独成段则只出现一次。

    字段值一律经 :func:`_read_path` 现读 ``case``，不缓存在 ``RouteResult`` 里 ——
    ``case`` 本身就是判定时的输入快照，两处存同一事实只会漂移。
    """
    items: list[EvidenceItem] = []

    mode_text = result.mode.value if result.mode else result.next_node
    items.append(
        EvidenceItem(
            statement=f"路由结论：{mode_text}；依据：{result.reason}",
            rule_ref=result.rule_id,
        )
    )

    # ② 每条已求值规则一行：判定结果 + 它声明读取的字段实际值
    for entry in result.trace:
        spec = RULES.get(entry.rule_id)
        verdict = VERDICT_TEXT[entry.verdict]
        if spec is None:
            # 规则表里查不到（不应发生）——如实标注，不编造字段清单。
            items.append(
                EvidenceItem(
                    statement=f"{entry.rule_id} → {verdict}｜（规则未登记，无法列出读取字段）",
                    rule_ref=entry.rule_id,
                )
            )
            continue
        # 不设截断：证据链的价值就是完整可核对，「等共 N 项」会让复算断在半路。
        # 当前 66 条规则最多读 7 个字段（G03.R10 的 NEWS2 各项），一条一行仍可读。
        pairs = [f"{path} = {_render(_read_path(case, path))}" for path in spec.fields]
        detail = "；".join(pairs) if pairs else "（本规则不读模型字段）"
        items.append(
            EvidenceItem(
                statement=f"{entry.rule_id} {spec.name} → {verdict}｜{detail}",
                rule_ref=entry.rule_id,
            )
        )

    # ④ 原文出处：每个带出处的字段单独一条，附写入者（归责必需）
    for field_name in sorted(case.field_evidence):
        provenance = case.field_evidence[field_name]
        items.append(
            EvidenceItem(
                statement=(
                    f"字段 {field_name} = {_render(_read_path(case, field_name))}"
                    f"（写入者：{provenance.writer.value}）"
                ),
                source=provenance.source_quote,
            )
        )

    return items


def _build_primary_recommendation(
    result: RouteResult,
    tool_outputs: Mapping[str, Any],
    tool_skips: Mapping[str, str] | None = None,
) -> str:
    """把已判出的结论排成**一句话** —— 只重排，不新增判断。

    构成：``<模式> · <病种>：<工具A 终点>；待补 N 项关键资料、M 项工具缺口``

    为什么要分两类计数：它们补法不同 —— 资料缺口靠补检查/补记录，工具缺口靠
    开发（当前 B/C/D/E 四个工具尚未实现）。合成一个数字会让「3 项」看起来
    很快能补齐，而其中 4 项其实补不出来。数字必须与五段式 ``pending`` 的
    构成对得上，否则读报告的人会以为下面列漏了。

    三段的来源都是既有结构：``route_result.mode`` / ``class_output`` /
    ``tool_outputs['guideline_engine'].terminal``。本函数**不含任何**
    「如果 A 则建议 B」的分支——那属于判定层，写在这里就是第二处真源。

    为什么必须做这一步：``route_result`` 和 ``tool_outputs`` 都是结构化对象，
    医生打开 CLI 看到的是字段名和枚举值。把结论排成一句话是**呈现**，不是判定
    ——这与 ``_summarize_product`` 对数值摘要做的事性质相同。

    为什么 ``translated_text`` 仍是 ``None``：本函数产出的是**事实排列**，
    不是润色。LLM 转译要落在"已定稿的结论文本"上，而这里的措辞仍需临床确认。
    """
    if result.kind is RouteKind.REVIEW:
        return f"人工复核：{result.reason}"
    if result.kind is RouteKind.VERIFY:
        return f"资料核实：{result.reason}"
    if result.kind is RouteKind.NO_TARGET:
        # 「查清了没有」与「查不清」是两句话：前者不需要人再复核分类，
        # 只需要临床继续找其他病因。措辞照抄 G09.R07 的出口动作，不自拟。
        return (
            "无目标病变：目标主动脉及髂动脉区域已充分评价，未见本项目目标病变"
            "（暂定；由临床团队继续其他病因诊断）"
        )

    head = result.mode.value if result.mode is not None else "未定模式"
    diseases = (
        "、".join(DISEASE_LABELS.get(tag, tag) for tag in result.class_output) or "病种未分类"
    )

    report = tool_outputs.get("guideline_engine")
    terminal = getattr(report, "terminal", None)
    gaps = getattr(report, "gaps", ()) or ()

    parts: list[str] = []
    if gaps:
        parts.append(f"{len(gaps)} 项关键资料")
    if tool_skips:
        parts.append(f"{len(tool_skips)} 项工具缺口")
    tail = f"；待补 {'、'.join(parts)}" if parts else ""

    if not terminal:
        # 急诊 + 零标签：系统只完成了分流，病种级建议根本不存在。
        # 不能与「病种流程跑过但没给终点」共用一句话 —— 前者需要人接手，
        # 后者是流程内部的问题。缺口计数照挂：这一句本身没有结论，
        # 「待补 5 项」正是读者最需要知道的部分。
        if result.mode is RouteMode.EMERGENCY and not result.class_output:
            return f"{head} · 病种未分类：仅完成分流，无病种级建议，需人工判断{tail}"
        return f"{head} · {diseases}：病种流程未给出结论{tail}"

    return f"{head} · {diseases}：{terminal}{tail}"


def _build_treatment_options(tool_outputs: Mapping[str, Any]) -> list[str]:
    """把工具A 的术式候选渲染成治疗方案清单。

    澄清一个容易搞错的粒度问题：本字段**不该**到「EVAR，经右股动脉，用 XX 型号」
    这一层。规格有 5 处明文禁止工具A 走到器械型号（``D1.3.R03``「不在本节点选
    具体型号」、``D1.3.R06``「不确定具体 SKU」、``D1.3.R90``「不批准具体术式或
    器械适配」、``D7.3.R01``「不选品牌/SKU」、``D7.3.R04``「不批准最终器械/术式
    方案」）——那属于手术规划，不在本系统范围内。

    规格要求的形态由 ``D1.3.R91`` 给出：「分别列出 EVAR/OSR 的**可行候选**、
    **条件性候选**和**排除原因**」。所以清单每项是「术式：状态（原因）」而不是
    一串术式名——后者会把「候选方向」伪装成「已定方案」。

    渲染交给候选对象自己的 ``render()``：本层不 import ``src.tools``（工具层可
    替换），也不自己拼串（措辞两处维护必然漂移）。
    """
    report = tool_outputs.get("guideline_engine")
    candidates = getattr(report, "candidates", ()) or ()
    return [
        candidate.render()
        for candidate in candidates
        if callable(getattr(candidate, "render", None))
    ]


#: 交接接收方 ← 由哪个 ``D3.*`` 字段的状态决定。
#:
#: 只报「这一方确认了没有」，不写医嘱 —— 「通知手术室」是流程，不是本系统的
#: 输出；系统能说的是「资源栏里有没有这一项」。
_HANDOFF_SOURCES: tuple[tuple[str, str], ...] = (
    ("血管外科（术者）· 手术室 · ICU", "D3.TEAM_RESOURCE"),
    ("输血科（备血/大量输血）", "D3.BLOOD_PREP"),
    ("麻醉（生理策略）", "D3.ANAESTHESIA_PLAN"),
)

#: 「已知事实」最多摘几条 —— 急诊场景下超过这个数读者不会往下看。
_MAX_BRIEF_FACTS: Final = 6


def _d3_input(node_inputs: Mapping[str, Any], spec_id: str) -> Any:
    """取一个 ``D3.*`` 录入值；没有就是 ``None``（不猜、不补默认）。"""
    channel = node_inputs.get("D3")
    return channel.get(spec_id) if isinstance(channel, Mapping) else None


def _build_danger(result: RouteResult, report: Any) -> list[str]:
    """危险提示 —— 命中的 P0 规则，**照抄规格措辞**。

    两处来源，都不重写文案：

    · 路由层的触发规则（``RULES[rule_id].name`` 就是规格 F 列那句话）；
    · 工具A 里 ``priority == "P0-A"`` 且命中为真的条目 —— 取 ``statement``
      箭头**之前**那半句，规格的 statement 一律写成「条件 → 动作」，
      前半句就是危险本身。

    为什么不自己写「主动脉破裂，立即通知…」这类短句：那是诊疗建议文案，
    属于项目三处留白之一，本层无权编写。照抄规格至少保证「说的和写的是同一句」。
    """
    lines: list[str] = []
    spec = RULES.get(result.rule_id)
    if spec is not None:
        # 紧急级别（``明确危险/高度疑似/不能排除``）与病种层的 ``P0-A`` 是**两套刻度**，
        # 放在同一位置会读成「同一件事的两个等级」。故级别放括号里做注。
        lines.append(f"{spec.name}（{result.rule_id} · {result.emergency_level}）")

    for entry in getattr(report, "entries", ()) or ():
        if entry.priority != "P0-A" or entry.verdict is not True:
            continue
        head = str(entry.statement).split("→")[0].strip()
        if head:
            lines.append(f"{entry.priority}｜{head}（{entry.rule_id}）")
    return lines


def _vital_fact(case: VascCase) -> str | None:
    """本次生命体征 —— 只报数值，**不判**是否达到休克阈值。

    为什么放在「已知事实」而不是「危险提示」：判「SBP 70 算不算危险」是
    ``router.SBP_SHOCK`` 的职责，展示层再做一次比较就是第二处判据。但数值本身
    必须出现 —— 破裂病例的报告里看不到血压，等于让读者自己去翻病历。
    """
    parts: list[str] = []
    if case.v.sbp is not None:
        parts.append(f"收缩压 {case.v.sbp} mmHg")
    if case.v.hr is not None:
        parts.append(f"心率 {case.v.hr} 次/分")
    if case.v.conscious_change is not None:
        parts.append(f"意识状态 {case.v.conscious_change}")
    return f"本次生命体征：{'；'.join(parts)}" if parts else None


def _build_handoff(node_inputs: Mapping[str, Any]) -> list[str]:
    """交接 —— 每个接收方「确认了没有」，按字段实际状态说，不编内容。"""
    lines: list[str] = []
    for role, spec_id in _HANDOFF_SOURCES:
        value = _d3_input(node_inputs, spec_id)
        status = "未记录 → 待确认" if value is None else f"已记录：{value}"
        lines.append(f"{role}：{status}")
    return lines


def _merge_pending(gaps: Sequence[str], tool_skips: Mapping[str, str]) -> list[str]:
    """把「字段缺口」与「工具没产出」合成一份待补清单。

    两类原因措辞分开，因为补法不同：

    · 未实现 → 「工具尚未实现」：补资料也补不出来，属于能力缺口；
    · 本次失败 → 原样带异常信息：补齐上游或修好数据后可能就拿到了。

    为什么要并进来：五段式这一段回答的是「还差什么才能动手」。急诊里
    「CTA 没量化」与「某个字段没填」对决策的影响是同一件事——术式方向都
    建立在更少的信息上。分开写在两个地方，读报告的人只会看到一半。
    """
    pending = list(gaps)
    for key in sorted(tool_skips):
        spec = SPEC_BY_KEY.get(key)
        label = spec.label if spec is not None else key
        reason = tool_skips[key]
        note = "工具尚未实现" if reason.startswith("未实现") else reason
        line = f"{label}：本次未获取（{note}）"
        if line not in pending:
            pending.append(line)
    return pending


def build_emergency_brief(
    result: RouteResult,
    tool_outputs: Mapping[str, Any],
    evidence: Sequence[EvidenceItem],
    node_inputs: Mapping[str, Any],
    case: VascCase,
    tool_skips: Mapping[str, str] | None = None,
) -> EmergencyBrief | None:
    """急诊五段式出口 —— 只在急诊模式下生成，其余模式返回 ``None``。

    为什么择期不给 ``None`` 以外的值：五段的结构是「先报危险」。择期病例没有
    危险段可填，硬填一个空列表会让报告出现一个空标题，读者会理解成
    「系统检查过，没有危险」——而实际上系统根本没做这项检查。
    """
    if result.mode is not RouteMode.EMERGENCY:
        return None

    report = tool_outputs.get("guideline_engine")
    facts = [item.statement for item in evidence if item.statement]
    vital = _vital_fact(case)
    if vital is not None:
        facts.insert(0, vital)
    candidates = [
        candidate.render()
        for candidate in (getattr(report, "candidates", ()) or ())
        if callable(getattr(candidate, "render", None))
    ]
    terminal = getattr(report, "terminal", None)

    return EmergencyBrief(
        danger=_build_danger(result, report),
        known_facts=facts[:_MAX_BRIEF_FACTS],
        # 缺口不截断：它就是「还差什么才能动手」的清单，少一条都是漏。
        # 这里同时并入「工具没产出」的原因 —— 对急诊来说，「CTA 没量化」与
        # 「某个字段没填」是同一件事的两面：都意味着方案建立在更少的信息上。
        pending=_merge_pending(
            [
                # 路由层的「未匹配病种流程」不在 ``report.gaps`` 里（那是工具A
                # 自己的缺口清单），但它是急诊五段式最该被看到的一条：结论只到
                # 分流层，病种级建议根本不存在。不并进来，R09/R10/R20 的待补段
                # 就只剩工具缺口，读起来像「只差几个工具」。
                *(g for g in result.evidence_gaps if "未匹配 D1–D7" in g),
                *(getattr(report, "gaps", ()) or ()),
            ],
            tool_skips or {},
        ),
        # 有候选就列候选；一个候选都没有时退到终点一句话，避免空段。
        rapid_assessment=candidates or ([str(terminal)] if terminal else []),
        handoff=_build_handoff(node_inputs),
    )


def output_node(state: PreopState) -> dict[str, Any]:
    """输出层节点：聚合出 :class:`FinalRecommendation`。

    参数：
        state: 至少含 ``case``（已回填）、``route_result``、``input_snapshot_hash``；
            ``tool_outputs`` 可缺省（缺省即「本次没有任何工具产出」，``tool_findings`` 为空）。

    返回：
        含 ``final_recommendation`` 的状态增量。
    """
    case = state["case"]
    result = state["route_result"]
    # 证据链只算一次：``evidence_chain`` 与「已知事实」段读的是同一份，
    # 各算一遍会让两处有机会分叉。
    evidence = _build_evidence(case, result)

    recommendation = FinalRecommendation(
        case_id=case.case_id,
        route_mode=result.mode,
        # 呈现：把既有结论排成一句话（不新增判断，见 _build_primary_recommendation）。
        primary_recommendation=_build_primary_recommendation(
            result,
            state.get("tool_outputs") or {},
            state.get("tool_skips") or {},
        ),
        # 治疗方案清单 = 工具A 的术式候选（EVAR/OSR 两列 × 候选状态）。
        # 不是「具体术式」：规格禁止工具A 走到器械型号，见 _build_treatment_options。
        treatment_options=_build_treatment_options(state.get("tool_outputs") or {}),
        confidence_level=result.confidence_level,
        # 两种情形必须人工接手：
        #   · 出口本身就是 REVIEW（判定依据不足）；
        #   · 急诊模式但零病种标签 —— 病种流程一个节点都没启动（R09/R10/R20
        #     这三条确实无 D1–D7 可归），系统只完成了分流，没有给出任何术式结论。
        manual_review_required=(
            result.kind is RouteKind.REVIEW
            or (result.mode is RouteMode.EMERGENCY and not result.class_output)
        ),
        evidence_chain=evidence,
        decision_path=_to_decision_steps(result.trace),
        # 工具产物 → 呈现条目；未跑的工具不出现在这里（不编造「跑了但没结果」）。
        tool_findings=build_tool_findings(state.get("tool_outputs") or {}),
        ruleset_version=result.ruleset_version,
        input_snapshot_hash=state.get("input_snapshot_hash"),
        # 急诊五段式：危险提示 / 已知事实 / 待补关键资料 / 快速评估 / 交接。
        # 择期与人工复核路径为 None（见 build_emergency_brief 的理由）。
        emergency_brief=build_emergency_brief(
            result,
            state.get("tool_outputs") or {},
            evidence,
            state.get("node_inputs") or {},
            case,
            state.get("tool_skips") or {},
        ),
        # 留白二：等主建议措辞经临床确认后再接 LLM 转译。
        translated_text=None,
    )

    return {"final_recommendation": recommendation}
