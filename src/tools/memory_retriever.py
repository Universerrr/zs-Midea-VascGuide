"""工具D · 记忆检索器 —— case 摘要 → 历史相似病例 / 既往证据。

定位：
    「病例锚定」在检索侧的落点：找到与本例相似的历史病例，把既往决策与结局
    作为参考证据。它与工具A 的区别是**经验 vs 条文**——A 回答「指南怎么说」，
    D 回答「类似病例当时怎么做、结果如何」。

输入契约：
    ``case 摘要`` —— 由 case 的判别性字段拼成的检索查询（病种 / 分型 / 直径区间 /
    合并症 / 术式史）。**不要**把整个 case 序列化后做向量化：结构化字段里的
    ``case_id``、时间戳噪音会稀释相似度。

输出契约：
    ``tuple[SimilarCase, ...]``，每条含 ``case_ref``（脱敏后编号）、
    ``similarity``、``key_differences``、``outcome``。

实现要点（验收标准）：
    1. 需要**已脱敏**的历史病例库与向量检索索引。当前无该数据集，
       检索质量（相似度阈值、召回率）无从评估——这是本工具阻塞的实质。
    2. 检索结果**不得直接进入结论**，只能作为「参考证据」列在证据链里。
       历史病例存在选择偏倚与结局缺失，把它当推荐依据等于用回顾性数据
       替代前瞻性证据；这一条必须写在输出层的措辞里。
    3. ``key_differences`` 不可省：只给相似度、不给差异，等于鼓励
       「相似所以照做」。审计时要能一眼看到「哪里不一样」。
    4. 检索是只读操作，不影响判定层。**不得**让检索结果反向修改 ``case``。
"""

from __future__ import annotations

from typing import Any

from .base import ToolRequest, ToolSpec

__all__ = ["SPEC", "run"]

SPEC = ToolSpec(
    key="memory_retriever",
    label="工具D · 记忆检索器",
    consumes=("case 摘要",),
    produces="历史相似病例 / 既往证据（并入 FinalRecommendation）",
    depends_on=(),
    blocked_reason=(
        "需已脱敏的历史病例库与向量检索索引；当前无该数据集，"
        "检索质量（相似度阈值、召回率）无从评估。"
    ),
)


def run(request: ToolRequest) -> Any:
    """按 case 摘要检索历史相似病例。

    参数：
        request: 使用 ``request.case`` 的判别性字段构造查询。

    返回：
        ``tuple[SimilarCase, ...]``，按相似度降序。

    抛出：
        NotImplementedError: 历史病例库缺失（说明文字即阻塞原因）。
    """
    raise NotImplementedError(f"{SPEC.label} 未实现：{SPEC.blocked_reason}")
