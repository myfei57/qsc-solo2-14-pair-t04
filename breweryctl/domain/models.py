"""领域数据模型与状态枚举。"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BatchStage(str, Enum):
    """批次在全流程中的阶段。"""

    CREATED = "created"
    MASHING = "mashing"
    BOILING = "boiling"
    WHIRLPOOL = "whirlpool"
    COOLING = "cooling"
    FERMENTING = "fermenting"
    MATURING = "maturing"
    COMPLETED = "completed"
    ABORTED = "aborted"


class MashStage(str, Enum):
    """糖化锅状态机。"""

    AWAITING_WATER = "awaiting_water"
    WATER_CONFIRMED = "water_confirmed"
    CHARGED = "charged"
    HEATING = "heating"
    RESTING = "resting"
    FILTERED = "filtered"
    FAILED = "failed"


class FermentStage(str, Enum):
    """发酵罐状态机。"""

    IDLE = "idle"
    SANITIZED = "sanitized"
    FILLED = "filled"
    PITCHED = "pitched"
    FERMENTING = "fermenting"
    MATURED = "matured"


class CipStage(str, Enum):
    """CIP 清洗回路状态机。"""

    IDLE = "idle"
    PRERINSE = "prerinse"
    CAUSTIC = "caustic"
    INTERMEDIATE_RINSE = "intermediate_rinse"
    ACID = "acid"
    FINAL_RINSE = "final_rinse"
    COMPLETE = "complete"


class HopStatus(str, Enum):
    """酒花添加状态。"""

    PENDING = "pending"
    ADDED = "added"
    MISSED = "missed"


class AlarmSeverity(str, Enum):
    """告警等级。"""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlarmStatus(str, Enum):
    """告警生命周期。"""

    ACTIVE = "active"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class RecipeStatus(str, Enum):
    """配方发布状态。"""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ValveState(str, Enum):
    """阀门状态。"""

    CLOSED = "closed"
    OPEN = "open"
    FAULT = "fault"


class PressureState(str, Enum):
    """发酵罐压力状态。"""

    NORMAL = "normal"
    RELIEVING = "relieving"
    LATCHED = "latched"


class ReadingQuality(str, Enum):
    """温度采样质量。"""

    GOOD = "good"
    SUSPECT = "suspect"
    REJECTED = "rejected"


class BoilerState(str, Enum):
    """锅炉运行状态机。"""

    STANDBY = "standby"      # 停炉待命，可被调度
    PURGING = "purging"      # 点火前吹扫
    FIRING = "firing"        # 燃烧供汽
    BANKED = "banked"        # 压火/解列，保留余压但不供汽
    LOCKED = "locked"        # 跳闸挂牌，未复位前禁止点火


class InterlockKind(str, Enum):
    """越线处置类型。"""

    LOW_WATER = "low_water"
    LOW_LOW_WATER = "low_low_water"
    HIGH_WATER = "high_water"
    HIGH_HIGH_WATER = "high_high_water"
    STEAM_HIGH = "steam_high"


class InterlockState(str, Enum):
    """处置单生命周期。"""

    ACTIVE = "active"        # 处置中，仍有自动/人工步骤未完成或条件未恢复
    RECOVERED = "recovered"  # 非跳闸类，工艺量回到安全区后自动闭环
    SUPERSEDED = "superseded"  # 被更高优先级的跳闸处置单取代
    LOCKED = "locked"        # 跳闸类，自动步完成后锅炉挂牌
    RESET = "reset"          # 跳闸锅炉已人工复位


class StepStatus(str, Enum):
    """处置单步骤状态。"""

    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"


class DocMixin:
    """把数据类转换为可持久化文档。"""

    def to_doc(self) -> dict[str, Any]:
        """转成可直接写入快照的字典。"""

        return dataclasses.asdict(self)  # type: ignore[call-overload]

@dataclass
class RecipeStep(DocMixin):
    """糖化配方中的一个升温或保温步骤。"""

    position: int
    name: str
    target_temp_c: float
    minutes: float


@dataclass
class HopAddition(DocMixin):
    """酒花添加计划，或某批次的一次实际投加。"""

    batch_id: str
    position: int
    name: str
    amount_g: float
    window_start_min: float
    window_end_min: float
    status: str = HopStatus.PENDING.value
    added_at: str | None = None
    actual_minute: float | None = None
    operator: str | None = None


@dataclass
class Recipe(DocMixin):
    """配方主体的当前版本快照。"""

    id: str
    name: str
    style: str
    brewery_id: str
    volume_l: float
    boil_minutes: float
    og_target: float
    fg_target: float
    ibu_target: float
    mash_steps: list[dict[str, Any]] = field(default_factory=list)
    hop_schedule: list[dict[str, Any]] = field(default_factory=list)
    current_version: int = 1
    status: str = RecipeStatus.DRAFT.value
    created_at: str = ""
    published_at: str | None = None
    versions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MashRun(DocMixin):
    """糖化锅一次投料运行。"""

    id: str
    batch_id: str
    stage: str
    water_target_c: float
    water_temp_c: float | None = None
    water_probe_id: str | None = None
    water_confirmed_at: str | None = None
    grain_kg: float = 0.0
    charged_at: str | None = None
    setpoint_c: float | None = None
    heating_at: str | None = None
    resting_at: str | None = None
    filtered_at: str | None = None
    failure_reason: str | None = None
    updated_at: str = ""


@dataclass
class WortRun(DocMixin):
    """麦汁过滤与浓度记录。"""

    id: str
    batch_id: str
    run_off_l: float = 0.0
    gravity_plato: float = 0.0
    ph: float = 5.4
    filtered_at: str | None = None
    transferred_at: str | None = None
    updated_at: str = ""


@dataclass
class BoilRun(DocMixin):
    """煮沸锅运行状态。"""

    id: str
    batch_id: str
    minutes_target: float
    stage: str = "idle"
    heat_on_at: str | None = None
    boiling_at: str | None = None
    whirlpool_at: str | None = None
    completed_at: str | None = None
    updated_at: str = ""


@dataclass
class FermentTank(DocMixin):
    """发酵罐当前状态。"""

    id: str
    code: str
    brewery_id: str
    capacity_l: float
    stage: str = FermentStage.IDLE.value
    batch_id: str | None = None
    sanitized_at: str | None = None
    cip_certificate_id: str | None = None
    filled_at: str | None = None
    pitched_at: str | None = None
    fermenting_at: str | None = None
    matured_at: str | None = None
    updated_at: str = ""


@dataclass
class TempProbe(DocMixin):
    """温度探头标定基线。"""

    id: str
    brewery_id: str
    location: str
    baseline_c: float
    last_value_c: float | None = None
    last_seen_at: str | None = None
    calibrated_at: str = ""
    samples: int = 0
    suspect: bool = False


@dataclass
class TempReading(DocMixin):
    """一次温度采样。"""

    id: str
    probe_id: str
    batch_id: str | None
    value_c: float
    deviation_c: float
    quality: str
    taken_at: str


@dataclass
class TempSetpoint(DocMixin):
    """某个批次的温控目标。"""

    batch_id: str
    target_c: float
    cooling: bool = False
    reached_at: str | None = None
    updated_at: str = ""


@dataclass
class PressureLatch(DocMixin):
    """发酵罐压力联锁状态。"""

    tank_id: str
    brewery_id: str = ""
    state: str = PressureState.NORMAL.value
    pressure_bar: float = 0.0
    latch_reason: str | None = None
    latched_at: str | None = None
    released_at: str | None = None
    reset_by: str | None = None
    updated_at: str = ""


@dataclass
class Valve(DocMixin):
    """发酵罐相关阀门。"""

    id: str
    tank_id: str
    purpose: str
    state: str = ValveState.CLOSED.value
    updated_at: str = ""


@dataclass
class CipCycle(DocMixin):
    """一次 CIP 清洗过程。"""

    id: str
    circuit_id: str
    tank_id: str
    stage: str = CipStage.IDLE.value
    completed_stages: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str | None = None
    operator: str = ""
    updated_at: str = ""


@dataclass
class CipCircuit(DocMixin):
    """CIP 回路定义。"""

    id: str
    code: str
    brewery_id: str
    tanks: list[str] = field(default_factory=list)
    flow_m3h: float = 0.0
    updated_at: str = ""


@dataclass
class CipCertificate(DocMixin):
    """清洗合格凭证，转罐前必须有效。"""

    id: str
    tank_id: str
    cycle_id: str
    issued_at: str
    expires_at: str
    verified_stages: list[str] = field(default_factory=list)


@dataclass
class Alarm(DocMixin):
    """告警记录。"""

    id: str
    brewery_id: str
    source: str
    severity: str
    code: str
    message: str
    status: str = AlarmStatus.ACTIVE.value
    latching: bool = False
    context: dict[str, Any] = field(default_factory=dict)
    raised_at: str = ""
    acknowledged_at: str | None = None
    resolved_at: str | None = None
    resolution_note: str | None = None


@dataclass
class AuditEntry(DocMixin):
    """不可变审计记录。"""

    id: str
    brewery_id: str
    batch_id: str | None
    actor: str
    action: str
    detail: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = ""


@dataclass
class Boiler(DocMixin):
    """蒸汽锅炉台账与当前运行状态。"""

    id: str
    code: str
    brewery_id: str
    rating_kgh: float
    priority: int = 100
    state: str = BoilerState.STANDBY.value
    water_pct: float | None = None
    steam_bar: float | None = None
    burner_on: bool = False
    feedwater_open: bool = False
    blowdown_open: bool = False
    steam_isolated: bool = False
    tagged: bool = False
    last_reading_at: str | None = None
    online_since: str | None = None
    updated_at: str = ""


@dataclass
class SteamConsumer(DocMixin):
    """用汽设备（糖化锅、煮沸锅、CIP 等）的需求登记。"""

    id: str
    code: str
    brewery_id: str
    name: str
    demand_kgh: float
    active: bool = False
    batch_id: str | None = None
    claimed_at: str | None = None
    claimed_by: str | None = None
    updated_at: str = ""


@dataclass
class SteamHeader(DocMixin):
    """厂区蒸汽联箱（分汽缸）的最新测点。"""

    brewery_id: str
    pressure_bar: float | None = None
    updated_at: str | None = None


@dataclass
class InterlockStep(DocMixin):
    """安全处置单中的一个固定步骤。"""

    order: int
    key: str
    label: str
    automatic: bool
    status: str = StepStatus.PENDING.value
    done_at: str | None = None
    actor: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class InterlockCase(DocMixin):
    """一次越线后的安全处置单，全过程留档。"""

    id: str
    boiler_id: str
    brewery_id: str
    kind: str
    tripping: bool
    trigger_value: float
    threshold: float
    state: str = InterlockState.ACTIVE.value
    reason: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    raised_at: str = ""
    recovered_at: str | None = None
    locked_at: str | None = None
    reset_at: str | None = None
    reset_by: str | None = None
    updated_at: str = ""


@dataclass
class Batch(DocMixin):
    """一个酿造批次的全流程状态。"""

    id: str
    code: str
    brewery_id: str
    recipe_id: str
    recipe_version: int
    volume_l: float
    stage: str = BatchStage.CREATED.value
    mash_id: str | None = None
    wort_id: str | None = None
    boil_id: str | None = None
    tank_id: str | None = None
    cip_certificate_id: str | None = None
    priority: str = "normal"
    notes: str = ""
    abort_reason: str | None = None
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
