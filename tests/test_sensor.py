"""Tests for the EVSE Sensor Catalog module."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "daze"
    / "sensor_catalog.py"
)
_spec = importlib.util.spec_from_file_location("daze_sensor_catalog", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
sensor_catalog = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sensor_catalog
_spec.loader.exec_module(sensor_catalog)

EVSE_SENSOR_CATALOG = sensor_catalog.EVSE_SENSOR_CATALOG
EVSE_STATUS_MAP = sensor_catalog.EVSE_STATUS_MAP
get_evse_status = sensor_catalog.get_evse_status
get_next_scheduled_charge = sensor_catalog.get_next_scheduled_charge
presence_on_off = sensor_catalog.presence_on_off
validate_sensor_catalog = sensor_catalog.validate_sensor_catalog


def _spec_by_key(key: str):
    for spec in EVSE_SENSOR_CATALOG:
        if spec.key == key:
            return spec
    raise KeyError(key)


SAMPLE_DATA = {
    "instantPowerAsWatt": 3500,
    "deliveredEnergyAsWattHour": 15000,
    "lastChargingCurrentInstantL1": 5000,
    "lastChargingCurrentInstantL2": 5100,
    "lastChargingCurrentInstantL3": 4900,
    "lastACVoltageL1": 230,
    "lastACVoltageL2": 231,
    "lastACVoltageL3": 229,
    # The field names the Daze API actually returns. An earlier copy of
    # this fixture used the upstream guesses (boardTemperature,
    # gridMaxPower, is_photovoltaic), which no longer match the catalog
    # and so asserted nothing: every value_fn returned None.
    "lastBoardL1Temperature": 32,
    "lastCaseTemperature": 28,
    "evseStatus": "charging",
    "supplyGridMaxPower": 22000,
    "photovoltaic": True,
    "evseIsThreePhase": False,
    "last_session_energy": 18000,
    "last_session_duration": 120,
    "last_session_cost": 4.2,
    "last_session_start": "2026-09-15T09:00:00+00:00",
    "last_session_end": "2026-09-15T11:00:00+00:00",
    "lifetime_energy": 500000,
    "total_sessions": 42,
    "nextScheduledCharge": "2026-09-16T22:00:00+00:00",
}


class TestCatalogStability:
    def test_key_set_is_stable(self) -> None:
        assert {s.key for s in EVSE_SENSOR_CATALOG} == {
            "instant_power",
            "delivered_energy",
            "charging_current_l1",
            "charging_current_l2",
            "charging_current_l3",
            "ac_voltage_l1",
            "ac_voltage_l2",
            "ac_voltage_l3",
            "board_temperature",
            "case_temperature",
            "evse_status",
            "grid_max_power",
            "is_photovoltaic",
            "is_three_phase",
            "lifetime_energy",
            "total_sessions",
        }

    def test_key_count_is_stable(self) -> None:
        assert len(EVSE_SENSOR_CATALOG) == 16

    def test_no_duplicate_keys(self) -> None:
        validate_sensor_catalog(EVSE_SENSOR_CATALOG)

    def test_duplicate_keys_fail_fast(self) -> None:
        duplicate_catalog = EVSE_SENSOR_CATALOG + (EVSE_SENSOR_CATALOG[0],)
        with pytest.raises(ValueError, match="Duplicate EVSE sensor keys"):
            validate_sensor_catalog(duplicate_catalog)

    def test_metadata_parity_for_core_sensors(self) -> None:
        assert _spec_by_key("instant_power").device_class == "power"
        assert _spec_by_key("instant_power").state_class == "measurement"
        assert (
            _spec_by_key("instant_power").native_unit_of_measurement == "W"
        )

        assert _spec_by_key("delivered_energy").device_class == "energy"
        assert (
            _spec_by_key("delivered_energy").state_class
            == "total_increasing"
        )
        assert (
            _spec_by_key("delivered_energy").native_unit_of_measurement
            == "Wh"
        )

        assert _spec_by_key("grid_max_power").entity_category == "diagnostic"


class TestValueExtraction:
    def test_measurements(self) -> None:
        assert _spec_by_key("instant_power").value_fn(SAMPLE_DATA) == 3500
        assert _spec_by_key("delivered_energy").value_fn(SAMPLE_DATA) == 15000
        assert _spec_by_key("charging_current_l1").value_fn(SAMPLE_DATA) == 5000
        assert _spec_by_key("ac_voltage_l1").value_fn(SAMPLE_DATA) == 230
        assert _spec_by_key("board_temperature").value_fn(SAMPLE_DATA) == 32

    def test_diagnostics(self) -> None:
        assert _spec_by_key("grid_max_power").value_fn(SAMPLE_DATA) == 22000
        assert _spec_by_key("is_photovoltaic").value_fn(SAMPLE_DATA) == "on"
        assert _spec_by_key("is_three_phase").value_fn(SAMPLE_DATA) == "off"

    def test_sessions(self) -> None:
        """The per-session sensors were removed from the catalog.

        They read None until the first charge completed, so only the
        two cumulative totals remain. The coordinator still computes
        the per-session fields; nothing in the catalog exposes them.
        """
        assert _spec_by_key("lifetime_energy").value_fn(SAMPLE_DATA) == 500000
        assert _spec_by_key("total_sessions").value_fn(SAMPLE_DATA) == 42

        for removed in (
            "last_session_energy",
            "last_session_duration",
            "last_session_cost",
            "last_session_start",
            "last_session_end",
            "next_scheduled_charge",
        ):
            with pytest.raises(KeyError):
                _spec_by_key(removed)

    def test_missing_values_return_none(self) -> None:
        empty: dict[str, Any] = {}
        for spec in EVSE_SENSOR_CATALOG:
            assert spec.value_fn(empty) is None


class TestStatusAndHelpers:
    def test_evse_status_mapping(self) -> None:
        assert get_evse_status({"evseStatus": "charging"}) == "charging"
        assert get_evse_status({"evseStatus": "PLAY_CHARGE"}) == "charging"
        # Reported as its own state rather than folded into idle: the
        # charger passes through it after a start, before the car draws,
        # and the charge switch holds on through it.
        assert get_evse_status({"evseStatus": "waiting_for_car"}) == "waiting_for_ev"
        assert get_evse_status({"evseStatus": None}) is None
        assert get_evse_status({}) is None

    def test_status_map_values_are_canonical(self) -> None:
        canonical = {
            "idle",
            "charging",
            "paused",
            "error",
            "offline",
            "waiting_for_ev",
        }
        assert set(EVSE_STATUS_MAP.values()).issubset(canonical)

    def test_presence_on_off(self) -> None:
        assert presence_on_off({"k": True}, "k") == "on"
        assert presence_on_off({"k": False}, "k") == "off"
        assert presence_on_off({"k": None}, "k") is None
        assert presence_on_off({}, "k") is None

    def test_next_scheduled_charge_fallback_order(self) -> None:
        """Earlier keys win, and every answer is a real datetime.

        A timestamp sensor rejects a bare string with "Invalid
        datetime", so the helper parses rather than passing the API
        value through. The strings here are ISO timestamps for that
        reason: opaque placeholders would parse to None and the
        fallback order would stop being observable.
        """
        first = "2026-09-16T22:00:00+00:00"
        later = "2026-09-17T07:30:00+00:00"

        chosen = get_next_scheduled_charge(
            {"nextScheduledCharge": first, "scheduleTime": later}
        )
        assert chosen == datetime(2026, 9, 16, 22, 0, tzinfo=timezone.utc)

        assert get_next_scheduled_charge({"scheduleTime": later}) == datetime(
            2026, 9, 17, 7, 30, tzinfo=timezone.utc
        )
        assert get_next_scheduled_charge({}) is None

    def test_next_scheduled_charge_rejects_an_unparseable_value(self) -> None:
        """A value that is not a time is dropped, not handed on.

        nextScheduleInfo arrives as an object, and passing it (or any
        other non-timestamp) to the sensor made Home Assistant refuse
        every state write.
        """
        assert get_next_scheduled_charge({"scheduleTime": "A"}) is None
        assert get_next_scheduled_charge({"nextScheduleInfo": {}}) is None
