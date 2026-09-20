"""运行时配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .validators import require_int, require_number, require_text

ENV_PREFIX = "BREWERYCTL_"


@dataclass(frozen=True)
class Settings:
    """平台启动参数与工艺阈值。"""

    host: str = "127.0.0.1"
    port: int = 8080
    data_dir: Path = Path("var/breweryctl")
    fsync: bool = True
    max_active_batches: int = 4
    temp_tolerance_c: float = 0.8
    pitch_temp_max_c: float = 12.0
    cip_certificate_ttl_min: int = 240
    pressure_limit_bar: float = 1.8
    hop_window_slack_min: float = 5.0
    steam_setpoint_bar: float = 0.70
    steam_stage_on_bar: float = 0.60
    steam_stage_off_bar: float = 0.80
    steam_stage_delay_min: float = 2.0
    steam_high_pressure_bar: float = 0.85
    steam_trip_pressure_bar: float = 0.90
    steam_low_water_pct: float = 50.0
    steam_low_low_water_pct: float = 30.0
    steam_high_water_pct: float = 80.0
    steam_post_purge_min: float = 5.0
    steam_flame_grace_min: float = 0.5
    log_level: str = "INFO"

    def validate(self) -> "Settings":
        """校验配置取值并返回自身，便于启动时链式调用。"""

        require_text(self.host, field="host", max_length=120)
        require_int(self.port, field="port", minimum=0, maximum=65535)
        require_int(self.max_active_batches, field="max_active_batches", minimum=1, maximum=64)
        require_number(self.temp_tolerance_c, field="temp_tolerance_c", minimum=0.05, maximum=10.0)
        require_number(self.pitch_temp_max_c, field="pitch_temp_max_c", minimum=2.0, maximum=30.0)
        require_int(self.cip_certificate_ttl_min, field="cip_certificate_ttl_min", minimum=5, maximum=2880)
        require_number(self.pressure_limit_bar, field="pressure_limit_bar", minimum=0.1, maximum=10.0)
        require_number(self.hop_window_slack_min, field="hop_window_slack_min", minimum=0.0, maximum=60.0)
        require_number(self.steam_setpoint_bar, field="steam_setpoint_bar", minimum=0.1, maximum=5.0)
        require_number(self.steam_stage_on_bar, field="steam_stage_on_bar", minimum=0.05, maximum=5.0)
        require_number(self.steam_stage_off_bar, field="steam_stage_off_bar", minimum=0.05, maximum=5.0)
        require_number(self.steam_high_pressure_bar, field="steam_high_pressure_bar", minimum=0.05, maximum=5.0)
        require_number(self.steam_trip_pressure_bar, field="steam_trip_pressure_bar", minimum=0.05, maximum=5.0)
        require_number(self.steam_low_water_pct, field="steam_low_water_pct", minimum=5.0, maximum=95.0)
        require_number(self.steam_low_low_water_pct, field="steam_low_low_water_pct", minimum=1.0, maximum=60.0)
        require_number(self.steam_high_water_pct, field="steam_high_water_pct", minimum=40.0, maximum=99.0)
        require_number(self.steam_post_purge_min, field="steam_post_purge_min", minimum=0.5, maximum=60.0)
        require_number(self.steam_flame_grace_min, field="steam_flame_grace_min", minimum=0.0, maximum=5.0)
        if not self.steam_stage_on_bar < self.steam_setpoint_bar < self.steam_stage_off_bar:
            raise ValidationError(
                "蒸汽阈值必须满足 stage_on < setpoint < stage_off",
                stage_on=self.steam_stage_on_bar,
                setpoint=self.steam_setpoint_bar,
                stage_off=self.steam_stage_off_bar,
            )
        if not self.steam_stage_off_bar <= self.steam_high_pressure_bar <= self.steam_trip_pressure_bar:
            raise ValidationError(
                "蒸汽压力阈值必须满足 stage_off <= high <= trip",
                stage_off=self.steam_stage_off_bar,
                high=self.steam_high_pressure_bar,
                trip=self.steam_trip_pressure_bar,
            )
        if not self.steam_low_low_water_pct < self.steam_low_water_pct < self.steam_high_water_pct:
            raise ValidationError(
                "水位阈值必须满足 low_low < low < high",
                low_low=self.steam_low_low_water_pct,
                low=self.steam_low_water_pct,
                high=self.steam_high_water_pct,
            )
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValidationError("log_level 取值不合法", field="log_level", value=self.log_level)
        return self

    def ensure_layout(self) -> dict[str, str]:
        """创建数据目录并返回关键路径。"""

        root = self.data_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        (root / "snapshots").mkdir(exist_ok=True)
        return {
            "data_dir": str(root),
            "snapshot": str(root / "state.json"),
            "journal": str(root / "journal.jsonl"),
        }

    def with_overrides(self, **overrides: Any) -> "Settings":
        """返回带命令行覆盖值的新配置对象。"""

        clean = {key: value for key, value in overrides.items() if value is not None}
        return replace(self, **clean).validate()

    def describe(self) -> dict[str, Any]:
        """输出可公开的配置摘要。"""

        return {
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "fsync": self.fsync,
            "max_active_batches": self.max_active_batches,
            "temp_tolerance_c": self.temp_tolerance_c,
            "pitch_temp_max_c": self.pitch_temp_max_c,
            "cip_certificate_ttl_min": self.cip_certificate_ttl_min,
            "pressure_limit_bar": self.pressure_limit_bar,
            "hop_window_slack_min": self.hop_window_slack_min,
            "steam_setpoint_bar": self.steam_setpoint_bar,
            "steam_stage_on_bar": self.steam_stage_on_bar,
            "steam_stage_off_bar": self.steam_stage_off_bar,
            "steam_stage_delay_min": self.steam_stage_delay_min,
            "steam_high_pressure_bar": self.steam_high_pressure_bar,
            "steam_trip_pressure_bar": self.steam_trip_pressure_bar,
            "steam_low_water_pct": self.steam_low_water_pct,
            "steam_low_low_water_pct": self.steam_low_low_water_pct,
            "steam_high_water_pct": self.steam_high_water_pct,
            "steam_post_purge_min": self.steam_post_purge_min,
            "steam_flame_grace_min": self.steam_flame_grace_min,
            "log_level": self.log_level.upper(),
        }

    @classmethod
    def from_env(cls) -> "Settings":
        """从 ``BREWERYCTL_*`` 环境变量读取配置。"""

        base = cls()
        text_keys = ("host", "log_level")
        int_keys = ("port", "max_active_batches", "cip_certificate_ttl_min")
        float_keys = (
            "temp_tolerance_c",
            "pitch_temp_max_c",
            "pressure_limit_bar",
            "hop_window_slack_min",
            "steam_setpoint_bar",
            "steam_stage_on_bar",
            "steam_stage_off_bar",
            "steam_stage_delay_min",
            "steam_high_pressure_bar",
            "steam_trip_pressure_bar",
            "steam_low_water_pct",
            "steam_low_low_water_pct",
            "steam_high_water_pct",
            "steam_post_purge_min",
            "steam_flame_grace_min",
        )
        values: dict[str, Any] = {}
        for key in text_keys:
            raw = os.environ.get(ENV_PREFIX + key.upper())
            if raw is not None:
                values[key] = raw
        for key in int_keys:
            raw = os.environ.get(ENV_PREFIX + key.upper())
            if raw is not None:
                values[key] = int(raw)
        for key in float_keys:
            raw = os.environ.get(ENV_PREFIX + key.upper())
            if raw is not None:
                values[key] = float(raw)
        data_dir = os.environ.get(ENV_PREFIX + "DATA_DIR")
        if data_dir:
            values["data_dir"] = Path(data_dir)
        fsync = os.environ.get(ENV_PREFIX + "FSYNC")
        if fsync is not None:
            values["fsync"] = fsync.strip().lower() not in {"0", "false", "no"}
        return base.with_overrides(**values)
