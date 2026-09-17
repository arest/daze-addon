"""EVSE Sensor Catalog for Daze Wallbox.

This module is the canonical sensor catalog interface shared by runtime
sensor adapters and tests.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
    "waiting_for_car": "idle",
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
    "nextScheduledCharge",
    "scheduledChargeTime",
    "scheduledStart",
    "scheduleTime",
)


def get_next_scheduled_charge(data: dict[str, Any]) -> Any | None:
    """Return the first present schedule timestamp field."""
    for key in _SCHEDULED_CHARGE_KEYS:
        value = data.get(key)
        if value is not None:
            return value
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
        value_fn=lambda data: data.get("boardTemperature"),
    ),
    EVSESensorSpec(
        key="case_temperature",
        device_class="temperature",
        state_class="measurement",
        native_unit_of_measurement="°C",
        value_fn=lambda data: data.get("caseTemperature"),
    ),
    EVSESensorSpec(
        key="evse_status",
        device_class="enum",
        options=("idle", "charging", "paused", "error", "offline"),
        value_fn=get_evse_status,
    ),
    EVSESensorSpec(
        key="grid_max_power",
        device_class="power",
        native_unit_of_measurement="W",
        entity_category="diagnostic",
        value_fn=lambda data: data.get("gridMaxPower"),
    ),
    EVSESensorSpec(
        key="is_photovoltaic",
        device_class="enum",
        entity_category="diagnostic",
        options=("on", "off"),
        value_fn=lambda data: presence_on_off(data, "is_photovoltaic"),
    ),
    EVSESensorSpec(
        key="is_three_phase",
        device_class="enum",
        entity_category="diagnostic",
        options=("on", "off"),
        value_fn=lambda data: presence_on_off(data, "evseIsThreePhase"),
    ),
    EVSESensorSpec(
        key="last_session_energy",
        device_class="energy",
        state_class="total_increasing",
        native_unit_of_measurement="Wh",
        value_fn=lambda data: data.get("last_session_energy"),
    ),
    EVSESensorSpec(
        key="last_session_duration",
        native_unit_of_measurement="min",
        value_fn=lambda data: data.get("last_session_duration"),
    ),
    EVSESensorSpec(
        key="last_session_cost",
        device_class="monetary",
        native_unit_of_measurement="EUR",
        value_fn=lambda data: data.get("last_session_cost"),
    ),
    EVSESensorSpec(
        key="last_session_start",
        device_class="timestamp",
        value_fn=lambda data: data.get("last_session_start"),
    ),
    EVSESensorSpec(
        key="last_session_end",
        device_class="timestamp",
        value_fn=lambda data: data.get("last_session_end"),
    ),
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
    EVSESensorSpec(
        key="next_scheduled_charge",
        device_class="timestamp",
        entity_category="diagnostic",
        value_fn=get_next_scheduled_charge,
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
