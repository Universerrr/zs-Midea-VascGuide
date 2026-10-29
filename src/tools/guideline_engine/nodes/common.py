"""病种层公共设施 —— 优先级求值、缺口汇总、出户口径。

为什么需要这一层：
    21 个病种 sheet 的规则都按同一套优先级执行（``NODE.ORDER`` 明文：
    「按 P0→P1→P2→P3 执行，P0 危险先输出且不得被低优先级覆盖」），并且每个
    sheet 都自带 ``R90``（P2 数据质量阻断）与 ``R91``（P3 出口）两条结构性规则。
    把这套执行语义写一次，7 个病种文件只声明「哪个规则、什么优先级、命中后去哪」。

优先级与出口的对应（规格 ``NODE.ORDER`` 各 sheet 一致）::

    P0-A  急症危险    → escalate 到 G05（急诊），可同时 next 到下一节点
    P1    重要分支    → next 或 terminal（急诊术式备选 / 专科评估）
    P2    数据阻断    → terminal「人工复核」+ gaps（对应 R90）
    P3    常规出口    → next / terminal（对应 R91 与各病种出口规则）

三条不变量（都有测试守着）：
    1. **P0 先于一切**：按序执行，P0 命中即返回，低优先级不得覆盖它；
    2. **UNKNOWN 不是 False**：``verdict is None`` 的规则继续往下跑，不被当否定；
    3. **全部落空不等于放行**：无任何出口时落到「人工复核」，不默认阴性。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from ....models.preop.case import UNCERTAIN_TOKENS
from ..state import GuidelineEntry, GuidelineState, NodeOutcome

__all__ = [
    "PRIORITY_ORDER",
    "ExitKind",
    "Priority",
    "RuleSlot",
    "evaluate_node",
    "gap_if_missing",
]

Priority = Literal["P0-A", "P1", "P2", "P3"]

#: 数字越小越先执行；规格的 ``P0-A/P0-G > P1 > P2 > P3``。
PRIORITY_ORDER: dict[str, int] = {"P0-A": 0, "P1": 1, "P2": 2, "P3": 3}

ExitKind = Literal["next", "escalate", "terminal"]


@dataclass(frozen=True, slots=True)
class RuleSlot:
    """一条病种规则的声明。

    ``exit_kind`` 与 ``exit_value`` 决定命中后去哪：

    · ``escalate``：回流判定层（``"G05"`` 急诊 / ``"G03"`` 重新筛查）——
      与 ``next`` **可以共存**：规格多条规则写的是「G05 急诊模式并进入 D3.2」，
      即「同时通知急诊路径」和「继续查 D3.2」，两者不矛盾；
    · ``next``：继续下一个病种节点；
    · ``terminal``：落到人工决策（``"人工复核"`` / ``"术者确认"`` / ``"监测"``）。
      与 ``next`` 互斥——既终止又继续会让「本次到底去哪」失去主语。
    """

    rule_id: str
    priority: Priority
    source_row: str
    statement: str
    judge: Callable[[GuidelineState], bool | None]
    exit_kind: ExitKind
    exit_value: str
    also_next: str | None = None
    """``escalate`` 时附带的下一个病种节点（规格「→G05 并进入 D3.2」）。"""
    gaps: tuple[str, ...] = ()


def evaluate_node(
    node_id: str,
    state: GuidelineState,
    slots: Sequence[RuleSlot],
) -> NodeOutcome:
    """按优先级执行规则，取**第一条命中**的出口。

    参数：
        node_id: 当前节点（``"D3.1"``）。
        state: 运行状态（只读，不改）。
        slots: 本节点的规则声明，顺序无关（内部按 ``priority`` 排序）。

    返回：
        该节点的 :class:`NodeOutcome`，含全部已执行规则的判定留痕。

    为什么未命中的规则也进 ``entries``：审计要回答的是「为什么没走那条路」，
    而不只是「走了哪条路」。只记命中项会让复核者看不到被否决的链条。
    """
    ordered = sorted(slots, key=lambda s: PRIORITY_ORDER[s.priority])
    verdicts: dict[str, bool | None] = {}
    entries: list[GuidelineEntry] = []

    for slot in ordered:
        verdict = slot.judge(state)
        verdicts[slot.rule_id] = verdict
        entries.append(
            GuidelineEntry(
                node_id=node_id,
                rule_id=slot.rule_id,
                source_row=slot.source_row,
                statement=slot.statement,
                verdict=verdict,
                next_node=slot.also_next or (slot.exit_value if slot.exit_kind == "next" else None),
            )
        )
        if verdict is True:
            return NodeOutcome(
                node_id=node_id,
                verdicts=verdicts,
                entries=tuple(entries),
                next_node=slot.also_next or (slot.exit_value if slot.exit_kind == "next" else None),
                escalation=slot.exit_value if slot.exit_kind == "escalate" else None,
                terminal=slot.exit_value if slot.exit_kind == "terminal" else None,
                gaps=slot.gaps,
            )

    # 全部规则都未命中（None 或 False）→ 规格要求「未知不等于阴性」，
    # 不得默认放行，落到人工复核。
    return NodeOutcome(
        node_id=node_id,
        verdicts=verdicts,
        entries=tuple(entries),
        terminal="人工复核",
        gaps=(f"{node_id}：全部规则均未命中，无可放行的确定性依据",),
    )


def gap_if_missing(label: str, value: object) -> str | None:
    """字段缺失或取值本身不确定时，返回一条缺口描述；否则 ``None``。

    只用于**生成待补清单**，不参与判定——判定一律走 ``eq`` / ``has`` 等三值原语。
    分开的理由：缺口要给人看（要写清字段名和实际取值），判定要给机器算。
    """
    if value is None:
        return f"{label}：未记录"
    token = getattr(value, "value", value)
    if isinstance(token, str) and token in UNCERTAIN_TOKENS:
        return f"{label}：{token}"
    return None
