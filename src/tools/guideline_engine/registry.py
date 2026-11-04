"""工具A 的节点与规则登记表 —— 「实现了哪些」的唯一真源。

为什么要有这张表，而不是让 ``engine.py`` 直接去 import 各病种模块：
    规格有 21 个病种 sheet / 172 条规则，当前全部登记（21 个节点 / 172 条）。
    缺口如果只存在于「没写的文件」里，它就不出现在任何 review 里——没人会去
    数「应该有 21 个目录却只有 7 个」。把规格条数与登记条数写在一起，
    ``tests/`` 一断言，缺口就变成看得见的数字。

    **「登记」不等于「在真实路径上可执行」**，三个数不要混：
    172 条全部登记；**167 条结构上可达**；**5 条结构上不可达**（``D3.3`` 全节点
    无自动入边——规格该 sheet 的 M 列每行都是「人工决策／医生确认／终点动作」）。
    这 5 条的判据都是完整的，只由单测覆盖。
    另有一层是**测试覆盖**：端到端用例走主路径，其余分支靠直接调 ``judge`` 的单测
    覆盖。这一层随用例增删而变，故不在文档里写死条数。
    可达性口径写在各节点模块的 ``RULE_ROWS`` 注释里，不在这里重复。

为什么用「按 sheet 记条数」而不是列出 172 个待实现 ID：
    待实现 ID 的全集是规格的一部分，逐字抄进代码等于把规格复制一份——改规格
    时两处会漂移。记条数只要求「不超、不重、编号在该 sheet 的合法范围内」，
    这已经能挡住「多写一条」「重号」「编号跳到别的 sheet」三类真实错误。

新增一个病种节点：在 ``nodes/`` 加模块 → 本表 ``NODES`` 合并 → 若有规则，
同时把 ``RULE_ROWS`` 并入。``test_guideline_registry`` 会检查三者的对应关系。
"""

from __future__ import annotations

from .nodes.d1_asymptomatic import NODES as _D1_NODES
from .nodes.d1_asymptomatic import RULE_ROWS as _D1_RULES
from .nodes.d2_symptomatic import NODES as _D2_NODES
from .nodes.d2_symptomatic import RULE_ROWS as _D2_RULES
from .nodes.d3_ruptured import NODES as _D3_NODES
from .nodes.d3_ruptured import RULE_ROWS as _D3_RULES
from .nodes.d4_infected import NODES as _D4_NODES
from .nodes.d4_infected import RULE_ROWS as _D4_RULES
from .nodes.d5_special import NODES as _D5_NODES
from .nodes.d5_special import RULE_ROWS as _D5_RULES
from .nodes.d6_postop import NODES as _D6_NODES
from .nodes.d6_postop import RULE_ROWS as _D6_RULES
from .nodes.d7_iliac import NODES as _D7_NODES
from .nodes.d7_iliac import RULE_ROWS as _D7_RULES
from .state import NodeFn

__all__ = [
    "IMPLEMENTED_RULE_COUNT",
    "NODES",
    "RULE_ROWS",
    "SPEC_NODE_SHEETS",
    "SPEC_RULE_COUNTS",
    "SPEC_RULE_TOTAL",
]

#: 规格里全部 21 个病种 sheet（V3.6.2）。顺序即 D1→D7。
SPEC_NODE_SHEETS: tuple[str, ...] = (
    "D1.1",
    "D1.2",
    "D1.3",
    "D2.1",
    "D2.2",
    "D2.3",
    "D3.1",
    "D3.2",
    "D3.3",
    "D4.1",
    "D4.2",
    "D4.3",
    "D5.1",
    "D5.2",
    "D5.3",
    "D6.1",
    "D6.2",
    "D6.3",
    "D7.1",
    "D7.2",
    "D7.3",
)

#: 每个 sheet 的规则条数（V3.6.2 实测，含各自的 R90/R91）。
SPEC_RULE_COUNTS: dict[str, int] = {
    "D1.1": 10,
    "D1.2": 11,
    "D1.3": 10,
    "D2.1": 8,
    "D2.2": 8,
    "D2.3": 8,
    "D3.1": 7,
    "D3.2": 8,
    "D3.3": 5,
    "D4.1": 7,
    "D4.2": 7,
    "D4.3": 11,
    "D5.1": 7,
    "D5.2": 6,
    "D5.3": 7,
    "D6.1": 11,
    "D6.2": 9,
    "D6.3": 8,
    "D7.1": 7,
    "D7.2": 9,
    "D7.3": 8,
}

SPEC_RULE_TOTAL: int = 172
"""规格病种规则总数。**改这个数等于改规格**，须同步 V3.6.2 并 bump 版本。"""

assert len(SPEC_NODE_SHEETS) == 21, f"规格 21 个病种 sheet，实际 {len(SPEC_NODE_SHEETS)}"
assert sum(SPEC_RULE_COUNTS.values()) == SPEC_RULE_TOTAL, (
    f"逐 sheet 条数之和应为 {SPEC_RULE_TOTAL}，实际 {sum(SPEC_RULE_COUNTS.values())}"
)

#: 已实现的节点（键与规格 sheet 名一一对应）。
NODES: dict[str, NodeFn] = {
    **_D1_NODES,
    **_D2_NODES,
    **_D3_NODES,
    **_D4_NODES,
    **_D5_NODES,
    **_D6_NODES,
    **_D7_NODES,
}

#: 已实现规则的 ``rule_id → 源行``（形如 ``D3.1!10``）。
RULE_ROWS: dict[str, str] = {
    **_D1_RULES,
    **_D2_RULES,
    **_D3_RULES,
    **_D4_RULES,
    **_D5_RULES,
    **_D6_RULES,
    **_D7_RULES,
}

IMPLEMENTED_RULE_COUNT: int = len(RULE_ROWS)

assert set(NODES) <= set(SPEC_NODE_SHEETS), (
    f"节点名不在规格 sheet 列表内：{set(NODES) - set(SPEC_NODE_SHEETS)}"
)
for _rid, _row in RULE_ROWS.items():
    _prefix = _rid.rsplit(".R", 1)[0]
    assert _prefix in SPEC_NODE_SHEETS, f"规则 {_rid} 的前缀 {_prefix} 不是规格 sheet"
    assert _row.startswith(f"{_prefix}!"), f"规则 {_rid} 的源行 {_row} 与所属 sheet 不符"
