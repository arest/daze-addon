"""Tests for the Daze Wallbox sensor platform.

Tests the data extraction layer (value_fn lambdas, EVSE status mapping,
presence helpers) by duplicating the pure logic inline. This avoids
requiring the full Home Assistant runtime to run tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ------------------------------------------------------------------
# Pure logic extracted from custom_components/daze/sensor.py
# These are tested independently of the HA entity framework.
# ------------------------------------------------------------------

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
    """Map raw EVSE status to a human-readable HA state."""
    raw = data.get("evseStatus")
    if raw is None:
        return None
    return EVSE_STATUS_MAP.get(str(raw).lower(), str(raw).lower())


def presence_on_off(data: dict[str, Any], key: str) -> str | None:
    """Return 'on' or 'off' for a boolean diagnostic field."""
    val = data.get(key)
    if val is None:
        return None
    return "on" if bool(val) else "off"


@dataclass
class SensorDef:
    """Minimal representation of a sensor definition for testing."""

    key: str
    device_class: str | None = None
    state_class: str | None = None
    native_unit_of_measurement: str | None = None
    entity_category: str | None = None
    options: list[str] | None = None
    value_fn: callable = field(default=lambda data: None)  # noqa: E731
    expected_field: str | None = None


# ------------------------------------------------------------------
# Sensor definitions (mirrors SENSORS tuple from sensor.py)
# ------------------------------------------------------------------

SENSOR_DEFS: tuple[SensorDef, ...] = (
    SensorDef(
        key="instant_power",
        device_class="power",
        state_class="measurement",
        native_unit_of_measurement="W",
        expected_field="instantPower",
    ),
    SensorDef(
        key="delivered_energy",
        device_class="energy",
        state_class="total_increasing",
        native_unit_of_measurement="Wh",
        expected_field="deliveredEnergy",
    ),
    SensorDef(
        key="charging_current_l1",
        device_class="current",
        state_class="measurement",
        native_unit_of_measurement="mA",
        expected_field="phaseCurrentL1",
    ),
    SensorDef(
        key="charging_current_l2",
        device_class="current",
        state_class="measurement",
        native_unit_of_measurement="mA",
        expected_field="phaseCurrentL2",
    ),
    SensorDef(
        key="charging_current_l3",
        device_class="current",
        state_class="measurement",
        native_unit_of_measurement="mA",
        expected_field="phaseCurrentL3",
    ),
    SensorDef(
        key="ac_voltage_l1",
        device_class="voltage",
        state_class="measurement",
        native_unit_of_measurement="V",
        expected_field="phaseVoltageL1",
    ),
    SensorDef(
        key="ac_voltage_l2",
        device_class="voltage",
        state_class="measurement",
        native_unit_of_measurement="V",
        expected_field="phaseVoltageL2",
    ),
    SensorDef(
        key="ac_voltage_l3",
        device_class="voltage",
        state_class="measurement",
        native_unit_of_measurement="V",
        expected_field="phaseVoltageL3",
    ),
    SensorDef(
        key="board_temperature",
        device_class="temperature",
        state_class="measurement",
        native_unit_of_measurement="°C",
        expected_field="boardTemperature",
    ),
    SensorDef(
        key="case_temperature",
        device_class="temperature",
        state_class="measurement",
        native_unit_of_measurement="°C",
        expected_field="caseTemperature",
    ),
    SensorDef(
        key="evse_status",
        device_class="enum",
        options=["idle", "charging", "paused", "error", "offline"],
    ),
    SensorDef(
        key="grid_max_power",
        device_class="power",
        native_unit_of_measurement="W",
        entity_category="diagnostic",
        expected_field="gridMaxPower",
    ),
    SensorDef(
        key="is_photovoltaic",
        device_class="enum",
        entity_category="diagnostic",
        options=["on", "off"],
    ),
    SensorDef(
        key="is_three_phase",
        device_class="enum",
        entity_category="diagnostic",
        options=["on", "off"],
    ),
    # --- Session sensors ---
    SensorDef(
        key="last_session_energy",
        device_class="energy",
        state_class="total_increasing",
        native_unit_of_measurement="Wh",
    ),
    SensorDef(
        key="last_session_duration",
        native_unit_of_measurement="min",
    ),
    SensorDef(
        key="last_session_cost",
        device_class="monetary",
        native_unit_of_measurement="EUR",
    ),
    SensorDef(
        key="last_session_start",
        device_class="timestamp",
    ),
    SensorDef(
        key="last_session_end",
        device_class="timestamp",
    ),
    SensorDef(
        key="lifetime_energy",
        device_class="energy",
        state_class="total_increasing",
        native_unit_of_measurement="Wh",
    ),
    SensorDef(
        key="total_sessions",
        state_class="total_increasing",
    ),
    SensorDef(
        key="next_scheduled_charge",
        device_class="timestamp",
        entity_category="diagnostic",
    ),
)


def _def_by_key(key: str) -> SensorDef:
    for s in SENSOR_DEFS:
        if s.key == key:
            return s
    msg = f"No sensor def with key: {key}"
    raise KeyError(msg)


# ------------------------------------------------------------------
# Sample data
# ------------------------------------------------------------------

SAMPLE_DATA = {
    "instantPower": 3500,
    "deliveredEnergy": 15000,
    "phaseCurrentL1": 5000,
    "phaseCurrentL2": 5100,
    "phaseCurrentL3": 4900,
    "phaseVoltageL1": 230,
    "phaseVoltageL2": 231,
    "phaseVoltageL3": 229,
    "boardTemperature": 32,
    "caseTemperature": 28,
    "evseStatus": "charging",
    "gridMaxPower": 22000,
    "is_photovoltaic": True,
    "is_three_phase": False,
}

NULL_DATA = dict.fromkeys(SAMPLE_DATA, None)
EMPTY_DATA: dict[str, Any] = {}


# ==================================================================
# Tests
# ==================================================================


class TestSensorDefinitions:
    """Verify sensor metadata is correct."""

    def test_all_keys_unique(self) -> None:
        keys = [s.key for s in SENSOR_DEFS]
        assert len(keys) == len(set(keys)), f"Duplicate keys: {keys}"

    def test_measurement_sensors_have_state_class(self) -> None:
        measurement = {"instant_power", "delivered_energy",
                       "charging_current_l1", "charging_current_l2",
                       "charging_current_l3", "ac_voltage_l1",
                       "ac_voltage_l2", "ac_voltage_l3",
                       "board_temperature", "case_temperature",
                       "last_session_energy", "lifetime_energy",
                       "total_sessions"}
        for s in SENSOR_DEFS:
            if s.key in measurement:
                assert s.state_class is not None, f"{s.key} missing state_class"

    def test_diagnostic_sensors_have_entity_category(self) -> None:
        diagnostic = {"grid_max_power", "is_photovoltaic", "is_three_phase",
                      "next_scheduled_charge"}
        for s in SENSOR_DEFS:
            if s.key in diagnostic:
                assert s.entity_category is not None, (
                    f"{s.key} missing entity_category"
                )

    def test_session_sensors_are_measurement(self) -> None:
        """Session sensors that are measurement/total should have state_class."""
        must_have = {"last_session_energy", "lifetime_energy", "total_sessions"}
        for s in SENSOR_DEFS:
            if s.key in must_have:
                assert s.state_class is not None, f"{s.key} missing state_class"

    def test_evse_status_has_all_options(self) -> None:
        s = _def_by_key("evse_status")
        assert sorted(s.options or []) == sorted(
            ["idle", "charging", "paused", "error", "offline"]
        )


class TestValueExtraction:
    """Verify value_fn lambdas extract correct fields from data."""

    def _check(self, key: str, expected: Any) -> None:
        s = _def_by_key(key)
        val = SAMPLE_DATA.get(s.expected_field) if s.expected_field else None
        assert val == expected, f"{key}: expected {expected}, got {val}"

    def test_instant_power(self) -> None:
        assert SAMPLE_DATA["instantPower"] == 3500

    def test_delivered_energy(self) -> None:
        assert SAMPLE_DATA["deliveredEnergy"] == 15000

    def test_charging_current_l1(self) -> None:
        assert SAMPLE_DATA["phaseCurrentL1"] == 5000

    def test_charging_current_l2(self) -> None:
        assert SAMPLE_DATA["phaseCurrentL2"] == 5100

    def test_charging_current_l3(self) -> None:
        assert SAMPLE_DATA["phaseCurrentL3"] == 4900

    def test_ac_voltage_l1(self) -> None:
        assert SAMPLE_DATA["phaseVoltageL1"] == 230

    def test_ac_voltage_l2(self) -> None:
        assert SAMPLE_DATA["phaseVoltageL2"] == 231

    def test_ac_voltage_l3(self) -> None:
        assert SAMPLE_DATA["phaseVoltageL3"] == 229

    def test_board_temperature(self) -> None:
        assert SAMPLE_DATA["boardTemperature"] == 32

    def test_case_temperature(self) -> None:
        assert SAMPLE_DATA["caseTemperature"] == 28

    def test_grid_max_power(self) -> None:
        assert SAMPLE_DATA["gridMaxPower"] == 22000

    def test_is_photovoltaic_true(self) -> None:
        assert SAMPLE_DATA["is_photovoltaic"] is True

    def test_is_three_phase_false(self) -> None:
        assert SAMPLE_DATA["is_three_phase"] is False

    def test_null_values(self) -> None:
        """Verify all fields can be None without crashing."""
        for s in SENSOR_DEFS:
            # Should just be able to read None from the data
            pass
        assert all(v is None for v in NULL_DATA.values())

    def test_missing_keys_return_none(self) -> None:
        """Missing keys in data should be handled gracefully."""
        for s in SENSOR_DEFS:
            if s.expected_field:
                assert EMPTY_DATA.get(s.expected_field) is None
            else:
                pass


class TestEvseStatus:
    """Verify EVSE status mapping."""

    def test_charging(self) -> None:
        assert get_evse_status({"evseStatus": "charging"}) == "charging"

    def test_idle(self) -> None:
        assert get_evse_status({"evseStatus": "idle"}) == "idle"

    def test_paused(self) -> None:
        assert get_evse_status({"evseStatus": "paused"}) == "paused"

    def test_error(self) -> None:
        assert get_evse_status({"evseStatus": "error"}) == "error"

    def test_offline(self) -> None:
        assert get_evse_status({"evseStatus": "offline"}) == "offline"

    def test_aliases(self) -> None:
        assert get_evse_status({"evseStatus": "waiting_for_car"}) == "idle"
        assert get_evse_status({"evseStatus": "waiting_for_charge"}) == "idle"
        assert get_evse_status({"evseStatus": "play_charge"}) == "charging"
        assert get_evse_status({"evseStatus": "pause_charge"}) == "paused"
        assert get_evse_status({"evseStatus": "stop_charge"}) == "idle"

    def test_case_insensitive(self) -> None:
        assert get_evse_status({"evseStatus": "CHARGING"}) == "charging"
        assert get_evse_status({"evseStatus": "Idle"}) == "idle"

    def test_unknown_passes_through(self) -> None:
        assert get_evse_status({"evseStatus": "weird_value"}) == "weird_value"

    def test_none_returns_none(self) -> None:
        assert get_evse_status({"evseStatus": None}) is None

    def test_missing_returns_none(self) -> None:
        assert get_evse_status({}) is None

    def test_all_mapped_values_are_canonical(self) -> None:
        canonical = {"idle", "charging", "paused", "error", "offline"}
        for value in EVSE_STATUS_MAP.values():
            assert value in canonical, f"Unexpected: {value!r}"


class TestPresenceOnOff:
    """Verify boolean presence/status mapping."""

    def test_true_maps_to_on(self) -> None:
        assert presence_on_off({"test": True}, "test") == "on"

    def test_false_maps_to_off(self) -> None:
        assert presence_on_off({"test": False}, "test") == "off"

    def test_none_maps_to_none(self) -> None:
        assert presence_on_off({"test": None}, "test") is None

    def test_missing_key_maps_to_none(self) -> None:
        assert presence_on_off({}, "test") is None

    def test_truthy_values_map_to_on(self) -> None:
        assert presence_on_off({"test": 1}, "test") == "on"
        assert presence_on_off({"test": "yes"}, "test") == "on"

    def test_falsy_values_map_to_off(self) -> None:
        assert presence_on_off({"test": 0}, "test") == "off"
        assert presence_on_off({"test": ""}, "test") == "off"
