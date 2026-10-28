"""VascGuide 术前评估 —— 命令行入口。

用法::

    uv run python main.py --case cases/example.json          # 结论摘要 + 证据链
    uv run python main.py --case cases/example.json -v       # 展开全部规则与缺口
    uv run python main.py --case cases/example.json -vv      # 再加完整决策链
    uv run python main.py --case cases/example.json --json   # 机器可读
    uv run python main.py --dump-template > cases/my.json    # 生成 160 字段空模板

本文件的定位是**呈现**，不是判定：不含任何临床阈值或分支逻辑。
任何「如果 A 则 B」一旦出现在这里，就等于绕过 ``src/rules`` 另立一处规则。

四个刻意不做的事（都有明确的阻塞原因，不是遗漏）：

    1. **不生成自然语言诊疗建议** —— ``primary_recommendation`` 是显式占位
       （留白一：需临床确认三模式各自的标准处置路径）；
    2. **不调用 LLM** —— 抽取器（病历 → VascCase）与转译器均未实现；
    3. **不实现任何工具** —— 五个工具全部 blocked，``tool_findings`` 恒为空；
    4. **不做交互界面** —— 渲染器与可视化界面已推迟到术前 Agent 完成之后。

因此当前跑通的含义是：**能正确判定「走哪个出口、为什么、缺什么资料」**，
不是「能给出治疗方案」。

退出码：``0`` 成功 / ``1`` 输入或运行错误 / ``2`` 命令行用法错误。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from src.models.preop.case import TOTAL_FIELD_COUNT, VascCase
from src.preop_agent.graph import DEFAULT_CHECKPOINT_DB, run_preop
from src.preop_agent.state import PreopRunResult
from src.rules.version import RULESET_VERSION

__all__ = ["main"]

#: 仓库根（本文件所在目录）—— 审计库用的是相对路径，必须把它作为工作目录。
REPO_ROOT: Final[Path] = Path(__file__).resolve().parent

#: 14 个域的中文名。缺口语义靠这张表，不散落在各处。
DOMAIN_LABELS: Final[dict[str, str]] = {
    "p": "人口学",
    "t": "时间",
    "d": "病历与诊断",
    "q": "数据质量",
    "s": "症状与体征",
    "v": "生命体征",
    "lab": "检验",
    "c": "CTA 报告",
    "n": "解剖与病生理",
    "gen": "遗传背景",
    "inf": "炎症证据",
    "cl": "病种分类输入",
    "k": "分类期检查",
    "r": "路由回填",
}

#: pydantic 错误类型 → 中文提示。未收录的类型原样打印 pydantic 的英文消息。
VALIDATION_HINTS: Final[dict[str, str]] = {
    "extra_forbidden": "该域没有这个字段",
    "missing": "缺少必填字段",
    "int_parsing": "应为整数",
    "int_type": "应为整数",
    "float_parsing": "应为数值",
    "float_type": "应为数值",
    "string_type": "应为字符串",
    "bool_parsing": "应为 true / false",
    "bool_type": "应为 true / false",
    "list_type": "应为数组",
    "dict_type": "应为对象",
    "literal_error": "取值不在允许范围内",
    "string_too_short": "字符串太短",
}

SEP_HEAVY: Final[str] = "═" * 64
SEP_LIGHT: Final[str] = "─" * 56

#: 默认层每个域最多列几条缺口（超出提示 -v）。
GAPS_PER_DOMAIN: Final[int] = 3


class CaseLoadError(Exception):
    """病例加载失败：消息已是可直接展示给用户的中文。"""


# ---------------------------------------------------------------------------
# ① 参数解析
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="VascGuide 术前评估 · 命令行入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py --case cases/example.json\n"
            "  python main.py --case cases/example.json -v\n"
            "  python main.py --dump-template > cases/my-case.json\n"
        ),
    )
    parser.add_argument(
        "--case",
        metavar="PATH",
        help="病例 JSON 文件路径（用 --dump-template 生成骨架）",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="详细程度，可叠加：-v 展开全部规则与缺口，-vv 再加决策链",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出（含 route_result 与 final_recommendation）",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="不写审计库（仅限试算；正式评估必须落盘）",
    )
    parser.add_argument(
        "--thread-id",
        metavar="ID",
        help="指定审计线程标识（续跑已有运行；默认每次运行新开一条）",
    )
    parser.add_argument(
        "--dump-template",
        action="store_true",
        help=f"打印含全部 {TOTAL_FIELD_COUNT} 个字段的空病例模板后退出",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {RULESET_VERSION}")
    return parser


# ---------------------------------------------------------------------------
# ② 病例加载（含中文错误重写）
# ---------------------------------------------------------------------------
def _domain_field_names(domain: str) -> tuple[str, ...]:
    """取某个域的合法字段名，用于「你写错了字段名」这类提示。"""
    info = VascCase.model_fields.get(domain)
    if info is None:
        return ()
    annotation = info.annotation
    for candidate in getattr(annotation, "__args__", (annotation,)):
        names = getattr(candidate, "model_fields", None)
        if names:
            return tuple(names)
    return ()


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    """把 pydantic 的报错改写成临床人员能读懂的中文。

    pydantic 原样抛出的内容含 ``errors.pydantic.dev`` 链接与 ``extra_forbidden``
    这类内部类型名，对填表的人没有信息量。这里只保留「哪个字段、错在哪」。
    """
    lines = [f"病例校验失败：{path}"]
    for err in exc.errors():
        loc = tuple(err["loc"])
        field = ".".join(str(part) for part in loc)
        etype = str(err["type"])
        hint = VALIDATION_HINTS.get(etype)

        if etype == "extra_forbidden" and len(loc) >= 2:
            domain = str(loc[0])
            names = _domain_field_names(domain)
            label = DOMAIN_LABELS.get(domain, domain)
            message = f"该域没有这个字段（{label}域可用：{' / '.join(names)}）" if names else hint
        elif hint:
            message = hint
            if "input" in err and etype != "missing":
                message += f"（实际值：{err['input']!r}）"
        else:
            message = str(err["msg"])

        lines.append(f"  · {field} —— {message}")

    lines.append("提示：用 --dump-template 生成一份空模板对照填写。")
    return "\n".join(lines)


def _load_case(path: Path) -> VascCase:
    """读病例 JSON → :class:`VascCase`。所有失败都转成 :class:`CaseLoadError`。"""
    if not path.exists():
        raise CaseLoadError(f"病例文件不存在：{path}")
    if not path.is_file():
        raise CaseLoadError(f"不是文件：{path}")

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CaseLoadError(f"无法读取 {path}：{exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CaseLoadError(
            f"{path} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列 —— {exc.msg}"
        ) from exc

    if not isinstance(data, dict):
        raise CaseLoadError(
            f"{path} 的顶层必须是 JSON 对象（{{...}}），实际是 {type(data).__name__}"
        )

    try:
        return VascCase.model_validate(data)
    except ValidationError as exc:
        raise CaseLoadError(_format_validation_error(path, exc)) from exc


# ---------------------------------------------------------------------------
# ③ 结论渲染
# ---------------------------------------------------------------------------
def _render_gaps(gaps: list[str], verbose: int) -> list[str]:
    """缺口按域分组呈现：40 条裸路径平铺会把结论挤掉。"""
    if not gaps:
        return ["无"]

    grouped: dict[str, list[str]] = {}
    for path in gaps:
        grouped.setdefault(path.split(".")[0], []).append(path)

    order = list(DOMAIN_LABELS)
    lines: list[str] = []
    limit = None if verbose >= 1 else GAPS_PER_DOMAIN
    for domain in sorted(grouped, key=lambda d: order.index(d) if d in order else len(order)):
        items = grouped[domain]
        label = DOMAIN_LABELS.get(domain, domain)
        lines.append(f"  {label}（{len(items)}）")
        shown = items if limit is None else items[:limit]
        lines.extend(f"    · {path}" for path in shown)
        if limit is not None and len(items) > limit:
            lines.append(f"    …另 {len(items) - limit} 项（-v 展开）")
    return lines


def _render_evidence(state: PreopRunResult, verbose: int) -> list[str]:
    """证据链：结论 → 规则（含被否决项）→ 原文出处。

    默认层只展开**成立**的规则 —— 那才是「为什么是这个结论」的正面依据；
    「排除了哪些」属于复核视角，放到 ``-v``。
    """
    chain = state["final_recommendation"].evidence_chain
    if not chain:
        return ["  （无）"]

    conclusion, *rest = chain
    rule_items = [item for item in rest if item.source is None]
    source_items = [item for item in chain if item.source]

    lines: list[str] = [f"  {conclusion.statement}"]

    hits = [item for item in rule_items if "→ 成立" in item.statement]
    others = len(rule_items) - len(hits)
    if hits:
        lines.append("")
        lines.append("  成立：")
        lines.extend(f"    {item.statement}" for item in hits)

    if others:
        lines.append("")
        if verbose >= 1:
            lines.append("  其余规则（逐条，含被否决项）：")
            lines.extend(
                f"    {item.statement}" for item in rule_items if "→ 成立" not in item.statement
            )
        else:
            lines.append(f"  其余 {others} 条规则求值后未成立（-v 展开逐条）")

    lines.append("")
    if source_items:
        lines.append("  原文出处：")
        for item in source_items:
            lines.append(f"    {item.statement}")
            lines.append(f"      「{item.source}」")
    else:
        lines.append("  原文出处：无（抽取层未接入，本次病例由结构化字段直接构造）")
    return lines


def _render_audit(state: PreopRunResult) -> list[str]:
    """技术审计脚注：只回答「记录有没有被改过」，不回答「结论怎么来的」。"""
    trail = state["audit_trail"]
    broken = [
        f"{trail[i].node}→{trail[i + 1].node}"
        for i in range(len(trail) - 1)
        if trail[i].output_hash != trail[i + 1].input_hash
    ]
    chain_text = (
        f"{len(trail)}/{len(trail)} 节点闭合 ✓" if not broken else f"断开于 {', '.join(broken)} ✗"
    )
    persisted = state["audit_persisted"]
    snapshot = state["final_recommendation"].input_snapshot_hash or "—"
    return [
        f"  线程    {state['thread_id']}",
        f"  落盘    {'是 → ' + str(DEFAULT_CHECKPOINT_DB) if persisted else '否（--no-persist，仅供试算）'}",
        f"  快照    {snapshot[:16]}",
        f"  哈希链  {chain_text}",
    ]


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _render_conclusion(state: PreopRunResult, verbose: int) -> str:
    result = state["route_result"]
    recommendation = state["final_recommendation"]

    out: list[str] = [
        SEP_HEAVY,
        f" VascGuide 术前评估 · {recommendation.case_id}",
        f" 规则集 {result.ruleset_version}",
        SEP_HEAVY,
        "",
    ]

    # 复核 / 核实的 next_node 与 kind 同义，重复显示没有信息量。
    exit_text = result.kind.value
    if result.next_node != result.kind.value:
        exit_text += f" → {result.next_node}"
    if result.mode is not None:
        exit_text += f"（{result.mode.value}）"
    out.append(f"出口    {exit_text}")
    out.append(f"依据    {result.reason}")
    out.append(f"置信    {result.confidence_level}")
    out.append(f"标签    {'、'.join(result.class_output) if result.class_output else '无'}")

    out += ["", SEP_LIGHT, " 主建议（留白一：待临床口径）", SEP_LIGHT]
    out.append(_indent(recommendation.primary_recommendation))
    options = recommendation.treatment_options
    out.append(_indent("治疗选项：" + ("、".join(options) if options else "暂无（待补临床口径）")))
    if recommendation.translated_text is None:
        out.append(_indent("自然语言转译：未启用（留白二，LLM 转译未实现）"))

    out += ["", SEP_LIGHT, f" 资料缺口（{len(result.evidence_gaps)} 项）", SEP_LIGHT]
    out += _render_gaps(result.evidence_gaps, verbose)

    out += ["", SEP_LIGHT, " 证据链", SEP_LIGHT]
    out += _render_evidence(state, verbose)

    out += ["", SEP_LIGHT, " 审计留痕（哈希链）", SEP_LIGHT]
    out += _render_audit(state)

    if verbose >= 2:
        path_len = len(recommendation.decision_path)
        out += ["", SEP_LIGHT, f" 决策链（{path_len} 步，含被否决项）", SEP_LIGHT]
        for step in recommendation.decision_path:
            verdict = {True: "成立", False: "不成立", None: "未判定"}[step.result]
            out.append(f"  [{step.order:02d}] {step.condition} → {verdict}  ({step.rule_ref})")

    if verbose >= 1:
        findings = recommendation.tool_findings
        out += ["", SEP_LIGHT, f" 工具产物（{len(findings)} 项）", SEP_LIGHT]
        if findings:
            out.extend(f"  · {item.label}：{item.summary}" for item in findings)
        else:
            out.append("  无（本次运行没有工具产出：五个工具均 blocked）")

    out += ["", SEP_HEAVY]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# ⑤ 模板导出
# ---------------------------------------------------------------------------
def _dump_template() -> str:
    """空病例模板：160 个字段全部列出（值为 null），照着删/填。"""
    return VascCase(case_id="TEMPLATE").model_dump_json(indent=2)


# ---------------------------------------------------------------------------
# ⑥ 编排
# ---------------------------------------------------------------------------
def _ensure_repo_root() -> None:
    """审计库是相对路径，工作目录不在仓库根时切回去，避免落到别处。"""
    if Path.cwd().resolve() == REPO_ROOT:
        return
    print(f"提示：当前目录不是仓库根，已切换到 {REPO_ROOT}", file=sys.stderr)
    os.chdir(REPO_ROOT)


def _run(case: VascCase, args: argparse.Namespace) -> PreopRunResult:
    kwargs: dict[str, Any] = {}
    if args.no_persist:
        kwargs["checkpointer"] = None
    if args.thread_id:
        kwargs["thread_id"] = args.thread_id
    return run_preop(case, **kwargs)


def _to_json_payload(state: PreopRunResult) -> str:
    payload = {
        "route_result": dataclasses.asdict(state["route_result"]),
        "final_recommendation": state["final_recommendation"].model_dump(mode="json"),
        "audit": {
            "thread_id": state["thread_id"],
            "persisted": state["audit_persisted"],
            "nodes": [record.node for record in state["audit_trail"]],
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.dump_template:
        print(_dump_template())
        return 0

    if not args.case:
        # 无参数时给帮助而不是报错：IDE 里点「运行」也能看到用法。
        parser.print_help()
        return 2

    _ensure_repo_root()

    try:
        case = _load_case(Path(args.case))
    except CaseLoadError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    try:
        state = _run(case, args)
    except Exception as exc:  # noqa: BLE001 —— CLI 顶层兜底，必须给出可读消息
        print(f"✗ 运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(_to_json_payload(state) if args.json else _render_conclusion(state, args.verbose))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
