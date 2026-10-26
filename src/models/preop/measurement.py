"""VascMeasurement —— 影像量化器（工具 B）的产物。

定位：
    VascCase 里的 `n.neck_len` / `n.neck_angle` 等只是 LLM 抽取的**粗值**，
    仅用于路由门控。本模型承载**影像量化器精密测量**的结果。

⚠️ 本模型是**工具产物**，不回写 VascCase。
⚠️ 量化本身由确定性/半确定性影像算法完成，本模型只做数据承载与校验。

## 为什么本模型里**不得出现时间戳**

本模型会经 ``state.tool_outputs`` 进入审计哈希投影（``HASHED_KEYS`` 含 ``tool_outputs``）。
一旦模型内存在「什么时候跑的」这类时间戳字段，则同一份影像、同一份病例、同一版算法，
在两次不同时刻的运行中会得到**不同的审计哈希**，直接违反「同输入同规则必同哈希」的可复现红线
—— 这正是 ``audit_trail`` 被刻意排除在哈希之外的原因（见 ``preop_agent/state.py``）。

⇒ 量化时间不属于「病例的产物」，属**运行期元信息**：由 ``execution_layer`` 的
``NodeAudit.started_at / finished_at`` 记录（``audit_trail`` 不进哈希），产物模型只存
**确定性内容**。同理，新增字段前先问一句：「这条内容会随时间或机器变化吗？」
会 —— 就不能放这里。

``method``（算法/版本）**必须保留**：它随算法版本变化，而算法版本改变**理应**改变哈希。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CenterlinePoint(BaseModel):
    """中心线上的一个采样点（用于描述迂曲度与直径剖面）。"""

    model_config = ConfigDict(extra="forbid")

    distance_mm: float = Field(..., ge=0, description="沿中心线的弧长位置（mm）。")
    diameter_mm: float = Field(..., ge=0, description="该处管腔直径（mm）。")
    z_position_mm: float | None = Field(
        default=None, description="相对参考点（如最低肾动脉）的轴向位置（mm）。"
    )


class VascMeasurement(BaseModel):
    """主动脉/髂动脉的精密三维测量结果。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., description="关联病例标识（外键指向 VascCase）。")

    # ---- 瘤体 ----
    max_diameter_mm: float | None = Field(
        default=None, ge=0, description="瘤体最大直径（精密值，mm）。"
    )
    max_diameter_slice_mm: float | None = Field(
        default=None, description="最大直径所在层位（相对参考点，mm）。"
    )
    aneurysm_length_mm: float | None = Field(default=None, ge=0, description="瘤体纵向长度（mm）。")
    volume_ml: float | None = Field(default=None, ge=0, description="瘤体容积（mL）。")
    thrombus_volume_pct: float | None = Field(
        default=None, ge=0, le=100, description="附壁血栓占瘤腔比例（%）。"
    )

    # ---- 锚定区 / 瘤颈 ----
    neck_length_mm: float | None = Field(
        default=None, ge=0, description="近端锚定区长度（精密值，mm）。"
    )
    neck_diameter_mm: float | None = Field(default=None, ge=0, description="瘤颈直径（mm）。")
    neck_angle_deg: float | None = Field(
        default=None, ge=0, le=180, description="瘤颈成角（精密值，度）。"
    )
    neck_calcification_pct: float | None = Field(
        default=None, ge=0, le=100, description="瘤颈钙化占比（%）。"
    )
    neck_thrombus_pct: float | None = Field(
        default=None, ge=0, le=100, description="瘤颈血栓占比（%）。"
    )

    # ---- 入路 ----
    access_min_diameter_mm: float | None = Field(
        default=None, ge=0, description="入路最小直径（精密值，mm）。"
    )
    access_calcification_pct: float | None = Field(
        default=None, ge=0, le=100, description="入路钙化占比（%）。"
    )
    iliac_tortuosity_deg: float | None = Field(
        default=None, ge=0, description="髂动脉最大迂曲角度（度）。"
    )

    # ---- 中心线剖面（可选，用于可视化与复核） ----
    centerline: list[CenterlinePoint] = Field(
        default_factory=list, description="中心线采样剖面（可为空）。"
    )

    # ---- 量化元信息（审计用） ----
    method: str | None = Field(default=None, description="量化方法/软件（如 CTA 自动分割 vX）。")


__all__ = ["CenterlinePoint", "VascMeasurement"]
