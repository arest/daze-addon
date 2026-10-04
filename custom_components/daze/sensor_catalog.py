"""EVSE Sensor Catalog for Daze Wallbox.

This module is the canonical sensor catalog interface shared by runtime
sensor adapters and tests.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

type ValueFn = Callable[[dict[str, Any]], Any | None]


@dataclass(frozen=True, kw_only=True)
class EVSESensorSpec:
    """Canonical EVSE sensor specification."""

    key: str
    device_class: str | None = None
    state_class: str | None = None
    native_unit_of_measurement: str | None = None
    entity_category: str | None = None
    options: tuple[str, ...] | None = None
    value_fn: ValueFn = lambda data: None


EVSE_STATUS_MAP: dict[str, str] = {
    "idle": "idle",
    "charging": "charging",
    "paused": "paused",
    "error": "error",
    "offline": "offline",
    "waiting_for_ev": "waiting_for_ev",
    "waiting_for_car": "waiting_for_ev",
    "waiting_for_charge": "idle",
    "play_charge": "charging",
    "pause_charge": "paused",
    "stop_charge": "idle",
}


def get_evse_status(data: dict[str, Any]) -> str | None:
    """Map raw EVSE status to canonical HA values."""
    raw = data.get("evseStatus")
    if raw is None:
        return None
    return EVSE_STATUS_MAP.get(str(raw).lower(), str(raw).lower())


def presence_on_off(data: dict[str, Any], key: str) -> str | None:
    """Return 'on'/'off' for optional boolean-ish fields."""
    val = data.get(key)
    if val is None:
        return None
    return "on" if bool(val) else "off"


_SCHEDULED_CHARGE_KEYS: tuple[str, ...] = (
    "nextScheduleInfo",
    "nextScheduledCharge",
    "scheduledChargeTime",
    "scheduledStart",
    "scheduleTime",
)


# Fields a schedule object might carry the start time under. The
# charger reported nextScheduleInfo as null whenever it was observed,
# so the shape is unconfirmed and every candidate is tried.
_SCHEDULE_TIME_FIELDS = (
    "startTime",
    "start",
    "scheduledStart",
    "nextStart",
    "time",
)


def _as_datetime(value: Any) -> datetime | None:
    """Coerce an API value into a timezone-aware datetime.

    A timestamp sensor requires a datetime. Returning the raw string or
    epoch the API provides raises "Invalid datetime" on every state
    write, which is the same failure that returning the nested object
    caused.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if isinstance(value, (int, float)):
        # Milliseconds if it is far too large to be seconds.
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    return None


def get_next_scheduled_charge(data: dict[str, Any]) -> Any | None:
    """Return the next scheduled charge time, if one is set.

    nextScheduleInfo arrives as an object rather than a timestamp, and
    merge_payload preserves it as one. Handing that object to a
    timestamp sensor makes Home Assistant reject every state write with
    "Invalid datetime", so the timestamp is extracted from it and
    anything that is not a scalar is discarded.
    """
    for key in _SCHEDULED_CHARGE_KEYS:
        value = data.get(key)

        if value is None:
            continue

        if isinstance(value, dict):
            for field in _SCHEDULE_TIME_FIELDS:
                parsed = _as_datetime(value.get(field))
                if parsed is not None:
                    return parsed
            continue

        parsed = _as_datetime(value)
        if parsed is not None:
            return parsed

    return None


EVSE_SENSOR_CATALOG: tuple[EVSESensorSpec, ...] = (
    EVSESensorSpec(
        key="instant_power",
        device_class="power",
        state_class="measurement",
        native_unit_of_measurement="W",
        value_fn=lambda data: data.get("instantPowerAsWatt"),
    ),
    EVSESensorSpec(
        key="delivered_energy",
        device_class="energy",
        state_class="total_increasing",
        native_unit_of_measurement="Wh",
        value_fn=lambda data: data.get("deliveredEnergyAsWattHour"),
    ),
    EVSESensorSpec(
        key="charging_current_l1",
        device_class="current",
        state_class="measurement",
        native_unit_of_measurement="mA",
        value_fn=lambda data: data.get("lastChargingCurrentInstantL1"),
    ),
    EVSESensorSpec(
        key="charging_current_l2",
        device_class="current",
        state_class="measurement",
        native_unit_of_measurement="mA",
        value_fn=lambda data: data.get("lastChargingCurrentInstantL2"),
    ),
    EVSESensorSpec(
        key="charging_current_l3",
        device_class="current",
        state_class="measurement",
        native_unit_of_measurement="mA",
        value_fn=lambda data: data.get("lastChargingCurrentInstantL3"),
    ),
    EVSESensorSpec(
        key="ac_voltage_l1",
        device_class="voltage",
        state_class="measurement",
        native_unit_of_measurement="V",
        value_fn=lambda data: data.get("lastACVoltageL1"),
    ),
    EVSESensorSpec(
        key="ac_voltage_l2",
        device_class="voltage",
        state_class="measurement",
        native_unit_of_measurement="V",
        value_fn=lambda data: data.get("lastACVoltageL2"),
    ),
    EVSESensorSpec(
        key="ac_voltage_l3",
        device_class="voltage",
        state_class="measurement",
        native_unit_of_measurement="V",
        value_fn=lambda data: data.get("lastACVoltageL3"),
    ),
    EVSESensorSpec(
        key="board_temperature",
        device_class="temperature",
        state_class="measurement",
        native_unit_of_measurement="°C",
        value_fn=lambda data: data.get("lastBoardL1Temperature"),
    ),
    EVSESensorSpec(
        key="case_temperature",
        device_class="temperature",
        state_class="measurement",
        native_unit_of_measurement="°C",
        value_fn=lambda data: data.get("lastCaseTemperature"),
    ),
    EVSESensorSpec(
        key="evse_status",
        device_class="enum",
        options=(
            "idle",
            "waiting_for_ev",
            "charging",
            "paused",
            "error",
            "offline",
        ),
        value_fn=get_evse_status,
    ),
    EVSESensorSpec(
        key="grid_max_power",
        device_class="power",
        native_unit_of_measurement="W",
        entity_category="diagnostic",
        value_fn=lambda data: data.get("supplyGridMaxPower"),
    ),
    EVSESensorSpec(
        key="is_photovoltaic",
        device_class="enum",
        entity_category="diagnostic",
        options=("on", "off"),
        value_fn=lambda data: presence_on_off(data, "photovoltaic"),
    ),
    EVSESensorSpec(
        key="is_three_phase",
        device_class="enum",
        entity_category="diagnostic",
        options=("on", "off"),
        value_fn=lambda data: presence_on_off(data, "evseIsThreePhase"),
    ),
    # Session-dependent sensors removed — they show "Unknown" until the
    # first charge completes and provide no value in the meantime.
    EVSESensorSpec(
        key="lifetime_energy",
        device_class="energy",
        state_class="total_increasing",
        native_unit_of_measurement="Wh",
        value_fn=lambda data: data.get("lifetime_energy"),
    ),
    EVSESensorSpec(
        key="total_sessions",
        state_class="total_increasing",
        value_fn=lambda data: data.get("total_sessions"),
    ),
)


RESTORE_STATE_KEYS: frozenset[str] = frozenset(
    {
        "delivered_energy",
        "lifetime_energy",
        "total_sessions",
        "last_session_energy",
        "last_session_cost",
        "last_session_duration",
    }
)


def validate_sensor_catalog(
    catalog: tuple[EVSESensorSpec, ...] = EVSE_SENSOR_CATALOG,
) -> None:
    """Fail fast when the catalog interface has duplicate keys."""
    seen: set[str] = set()
    duplicates: set[str] = set()

    for spec in catalog:
        if spec.key in seen:
            duplicates.add(spec.key)
        seen.add(spec.key)

    if duplicates:
        dupes = ", ".join(sorted(duplicates))
        raise ValueError(f"Duplicate EVSE sensor keys: {dupes}")


validate_sensor_catalog()
