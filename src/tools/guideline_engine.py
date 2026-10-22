"""工具A · 指南引擎 —— 病种 + 解剖分型 + 直径 → 指南推荐条目。

定位：
    把「病例落在哪一档」翻译成「指南怎么说」。这是唯一能产出**指南原文级**依据的
    工具，也是「病例锚定」在输出侧的落点：每条推荐都要能回溯到
    指南章节 → 推荐序号 → DOI。

输入契约（``SPEC.consumes``）：
    ``case.disease_category``  病种 —— 决定查哪一族条目
    ``case.anatomy_level``     解剖分型 —— 决定条目子集
    ``case.max_diameter_mm``   主动脉最大径 —— 决定 A01（小型）/ A02（较大）档位

输出契约：
    ``tuple[GuidelineEntry, ...]``，按相关度降序。每条至少含四个属性：

    ``section``             章节 / 推荐序号（如 ``§4.4; Rec 22``）
    ``recommendation``      推荐原文
    ``evidence_attribute``  证据属性枚举（指南直接来源 / 工程设计来源 /
                            工程条件·非 ESVS 统一阈值 / 临床扩展待审核）
    ``doi``                 文献 DOI

    空元组表示「没有命中条目」。**不得**用泛泛而谈的默认文案填充——
    「查不到」本身就是要如实上报的结论。

实现要点（验收标准）：
    1. 条目来源是 ``docs/路由表2.xlsx`` 的 ``07_指南索引``（S01–S15），
       **不是** ``docs/诊断决策路由.pdf``。后者是 VascGuide 自己的决策流程图
       （N0~N9 + D1~D7），零推荐等级、零证据级别、零文献引用，把它结构化
       产出的东西不会超过现有 ``router.py``。
    2. ``evidence_attribute`` 必须是枚举而非自由文本。``07_指南索引`` 已把
       「ESVS 原文」与「工程条件·非 ESVS 统一阈值」（如 S13 的 15mm/45°/7mm）
       分开标注；输出侧若合并成一段话，审计时无法判断哪条真是指南说的。
    3. 每条带 ``doi``。当前单一指南源为 ESVS 2024（Wanhainen et al.，
       ``10.1016/j.ejvs.2023.11.002``）。
    4. 条目库需要「结构化入库 + 检索接口」，而不是运行时读 Excel：
       ``docs/`` 是口径原件，运行时依赖它会让审计无法复现（文件会被改）。
    5. **已知缺口（待口径裁定，勿擅自补）**：``consumes`` 未含
       ``iliac_diameter_mm``，而设计表第 9 条要求髂瘤 40mm 单独分层。
       该阈值当前无处落地——补齐前不要在本工具里硬编码 40mm，
       也不要把直径判定搬回 ``router.py``（``05_说明与待确认`` 第 17 条与
       ``03_P1-P8判定映射`` 的 P7 行对此**自相矛盾**，须先裁定）。
"""

from __future__ import annotations

from typing import Any

from .base import ToolRequest, ToolSpec

__all__ = ["SPEC", "run"]

SPEC = ToolSpec(
    key="guideline_engine",
    label="工具A · 指南引擎",
    consumes=("case.disease_category", "case.anatomy_level", "case.max_diameter_mm"),
    produces="指南推荐条目（并入 FinalRecommendation）",
    depends_on=(),
    blocked_reason=(
        "需先确定接入哪一部/哪几部指南及其版本（SVS / ESVS / NICE …），"
        "并把指南条目结构化为可检索的条目库；口径未定。"
    ),
)


def run(request: ToolRequest) -> Any:
    """查条目库，返回本病例命中的指南条目。

    参数：
        request: 至少需 ``case.disease_category`` / ``case.anatomy_level`` /
            ``case.max_diameter_mm``。

    返回：
        ``tuple[GuidelineEntry, ...]``，无命中时为空元组。

    抛出：
        NotImplementedError: 条目库尚未建立（说明文字即阻塞原因）。
    """
    raise NotImplementedError(f"{SPEC.label} 未实现：{SPEC.blocked_reason}")
