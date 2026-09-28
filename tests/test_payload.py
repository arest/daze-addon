"""Tests for payload normalisation, using a real captured response.

The fixtures below are the actual shape returned by the Daze API for a
DT01 charger, captured while it was delivering 2688 W. Values are
verbatim apart from identifiers.

This is the bug these tests pin: every sensor read a top-level field
name, but the live metrics arrive nested under ``chargeSession``, and
the temperatures, grid limit and eco mode arrive from a different
endpoint entirely. Nothing errored, so entities were created and every
one of them read None.

Run with pytest, or standalone:

    python3 tests/test_payload.py
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "daze"


def _load(name: str, filename: str) -> Any:
    """Load a single integration module without Home Assistant."""
    spec = importlib.util.spec_from_file_location(name, PACKAGE_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


payload = _load("daze_payload_under_test", "payload.py")
catalog = _load("daze_catalog_under_test", "sensor_catalog.py")


# Captured from GET /sockets/{serial}/remoteInfo while charging.
REMOTE_INFO: dict[str, Any] = {
    "active": True,
    "chargeSession": {
        "chargeTime": "00:19:53",
        "currentlyChargingInThreePhase": False,
        "deliveredEnergyAsWattHour": 1258,
        "instantPowerAsWatt": 2688,
        "lastACVoltageL1": 233,
        "lastACVoltageL2": 1,
        "lastACVoltageL3": 7,
        "lastChargingCurrentInstantL1": 11677,
        "lastChargingCurrentInstantL2": 0,
        "lastChargingCurrentInstantL3": 0,
        "lastMaxChargingCurrent": 11739,
        "sessionId": 1790524789000,
        "startTime": "2026-09-27T15:59:49Z",
        "user": None,
    },
    "evseIsThreePhase": False,
    "evseState": 3,
    "evseSuspensionReason": 0,
    "evseSystemError": 0,
    "isPaused": False,
    "isScheduledPaused": False,
    "isSmartTariffPaused": False,
    "nextScheduleInfo": None,
    "smartTariffBatteryInfo": None,
}

# Captured from GET /networks/{uid}/evses?includeEcoInfo=true.
EVSE_RECORD: dict[str, Any] = {
    "active": True,
    "deviceProfile": "DT01",
    "ecoModeEnabled": False,
    "evseIsThreePhase": False,
    "evseName": "Daze HomeTT",
    "firmwareVersion": "13.3.0",
    "lastMaxInstallationCurrent": 32000,
    "lastStatus": 3,
    "maxExternalChargingCurrentInMilliAmps": 11739,
    "operationMode": 0,
    "photovoltaic": True,
    "schedules": [],
    "serialNumber": "TESTSERIAL",
    "softwareVersion": "22.4.0",
    "sockets": [
        {
            "active": True,
            "isPrimary": True,
            "lastACVoltageL1": 233,
            "lastBoardL1Temperature": 34,
            "lastCaseTemperature": 40,
            "lastChargingCurrentInstantL1": 11677,
            "lastEnergy": 1258,
            "lastMaxChargingCurrent": 11739,
            "lastPower": 2688,
            "lastStatus": 3,
            "maxExternalChargingCurrentInMilliAmps": 11739,
            "operationMode": 0,
            "serialNumber": "TESTSERIAL",
        }
    ],
    "supplyGridMaxPower": 3000,
}


def merged() -> dict[str, Any]:
    """Return the merged payload for the captured fixtures."""
    return payload.merge_payload(REMOTE_INFO, EVSE_RECORD)


# ------------------------------------------------------------------
# Merge behaviour
# ------------------------------------------------------------------


def test_session_metrics_are_lifted_to_top_level() -> None:
    """Live metrics nested under chargeSession must become readable."""
    data = merged()

    assert data["instantPowerAsWatt"] == 2688
    assert data["deliveredEnergyAsWattHour"] == 1258
    assert data["lastChargingCurrentInstantL1"] == 11677
    assert data["lastACVoltageL1"] == 233


def test_evse_record_supplies_fields_remote_info_lacks() -> None:
    """Temperatures, grid limit and eco mode come from the EVSE record."""
    data = merged()

    assert data["lastBoardL1Temperature"] == 34
    assert data["lastCaseTemperature"] == 40
    assert data["supplyGridMaxPower"] == 3000
    assert data["ecoModeEnabled"] is False
    assert data["photovoltaic"] is True
    assert data["maxExternalChargingCurrentInMilliAmps"] == 11739


def test_session_values_win_over_socket_snapshot() -> None:
    """The live session is fresher than the cached socket reading."""
    stale = {**EVSE_RECORD}
    stale["sockets"] = [{**EVSE_RECORD["sockets"][0], "lastACVoltageL1": 999}]

    data = payload.merge_payload(REMOTE_INFO, stale)

    assert data["lastACVoltageL1"] == 233


def test_merge_survives_a_missing_evse_record() -> None:
    """A failed EVSE fetch must not break the live metrics."""
    data = payload.merge_payload(REMOTE_INFO, None)

    assert data["instantPowerAsWatt"] == 2688
    assert data.get("lastCaseTemperature") is None


def test_merge_survives_empty_input() -> None:
    """No data at all must not raise."""
    assert payload.merge_payload(None, None) == {
        "chargeSession": None,
        "nextScheduleInfo": None,
    }


# ------------------------------------------------------------------
# Status derivation
# ------------------------------------------------------------------


def test_state_3_is_charging() -> None:
    """Confirmed against hardware delivering 2688 W."""
    assert merged()["evseStatus"] == "charging"


def test_paused_flags_win_over_state() -> None:
    """Any pause flag reports paused."""
    for flag in ("isPaused", "isScheduledPaused", "isSmartTariffPaused"):
        remote = {**REMOTE_INFO, flag: True}
        data = payload.merge_payload(remote, EVSE_RECORD)
        assert data["evseStatus"] == "paused", flag


def test_system_error_reports_error() -> None:
    """A non-zero system error outranks the state value."""
    remote = {**REMOTE_INFO, "evseSystemError": 7}
    assert payload.merge_payload(remote, EVSE_RECORD)["evseStatus"] == "error"


def test_inactive_reports_offline() -> None:
    """active=False means the charger is not reachable."""
    remote = {**REMOTE_INFO, "active": False}
    assert payload.merge_payload(remote, EVSE_RECORD)["evseStatus"] == "offline"


def test_unknown_state_reports_idle_not_charging() -> None:
    """Unconfirmed state values must never be reported as charging.

    3, 5 and 6 are confirmed and excluded; everything else is still a
    guess and must fall back to idle.
    """
    for state in (0, 1, 2, 4, 7, 99):
        remote = {**REMOTE_INFO, "evseState": state}
        data = payload.merge_payload(remote, EVSE_RECORD)
        assert data["evseStatus"] == "idle", state


def test_no_state_information_yields_no_status() -> None:
    """Absent state must not be invented."""
    assert payload.derive_status({}) is None


# ------------------------------------------------------------------
# End-to-end against the real sensor catalog
# ------------------------------------------------------------------


def test_every_live_sensor_reads_a_value() -> None:
    """The whole point: no live sensor may be None for this payload.

    Session-history sensors are excluded because they are computed by
    the coordinator from a separate endpoint.
    """
    data = merged()

    history = {
        "last_session_energy",
        "last_session_duration",
        "last_session_cost",
        "last_session_start",
        "last_session_end",
        "lifetime_energy",
        "total_sessions",
        "next_scheduled_charge",
    }

    blank = [
        spec.key
        for spec in catalog.EVSE_SENSOR_CATALOG
        if spec.key not in history and spec.value_fn(data) is None
    ]

    assert not blank, f"sensors still reading None: {blank}"


def test_catalog_reads_only_fields_the_payload_provides() -> None:
    """Guard against a value function drifting to an absent field."""
    data = merged()
    source = (PACKAGE_DIR / "sensor_catalog.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    computed = {
        "last_session_cost",
        "last_session_duration",
        "last_session_end",
        "last_session_energy",
        "last_session_start",
        "lifetime_energy",
        "total_sessions",
    }

    unknown: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
        ):
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and isinstance(
                argument.value, str
            ):
                name = argument.value
                if name not in computed and name not in data:
                    unknown.append(name)

    assert not unknown, f"catalog reads fields the API never returns: {unknown}"


def test_switch_status_check_matches_derived_status() -> None:
    """switch.py compares against the string 'charging'."""
    data = merged()
    assert str(data.get("evseStatus")).lower() == "charging"



# Captured while the charger was paused mid-session.
REMOTE_INFO_PAUSED: dict[str, Any] = {
    "active": True,
    "chargeSession": {
        "chargeTime": "00:05:19",
        "currentlyChargingInThreePhase": False,
        "deliveredEnergyAsWattHour": 223,
        "instantPowerAsWatt": 0,
        "lastACVoltageL1": 232,
        "lastACVoltageL2": 1,
        "lastACVoltageL3": 7,
        "lastChargingCurrentInstantL1": 0,
        "lastChargingCurrentInstantL2": 0,
        "lastChargingCurrentInstantL3": 0,
        "lastMaxChargingCurrent": 0,
        "sessionId": 1790529768000,
        "startTime": "2026-09-27T17:22:48Z",
        "user": None,
    },
    "evseIsThreePhase": False,
    "evseState": 6,
    "evseSuspensionReason": 3,
    "evseSystemError": 0,
    "isPaused": True,
    "isScheduledPaused": False,
    "isSmartTariffPaused": False,
    "nextScheduleInfo": None,
    "smartTariffBatteryInfo": None,
}


def test_state_6_is_paused() -> None:
    """Confirmed against hardware: state 6 with isPaused set."""
    data = payload.merge_payload(REMOTE_INFO_PAUSED, EVSE_RECORD)
    assert data["evseStatus"] == "paused"


def test_paused_state_reports_paused_without_the_flag() -> None:
    """State 6 alone must report paused, not idle.

    The flag and the state are independent fields; either on its own
    has to be enough, or a pause shows up as idle.
    """
    remote = {**REMOTE_INFO_PAUSED, "isPaused": False}
    data = payload.merge_payload(remote, EVSE_RECORD)
    assert data["evseStatus"] == "paused"


def test_paused_session_keeps_its_session_id() -> None:
    """A paused session stays open, so the ID remains available.

    This is why ErrorWrongSessionID cannot mean "no session exists":
    the resume command has a valid ID to quote.
    """
    data = payload.merge_payload(REMOTE_INFO_PAUSED, EVSE_RECORD)
    assert data["sessionId"] == 1790529768000
    assert data["instantPowerAsWatt"] == 0



def _waiting_payload() -> dict[str, Any]:
    """Return the state seen right after a start takes effect."""
    remote = {
        **REMOTE_INFO_PAUSED,
        "evseState": 5,
        "evseSuspensionReason": 0,
        "isPaused": False,
    }
    return payload.merge_payload(remote, EVSE_RECORD)


def test_state_5_is_waiting_for_the_vehicle() -> None:
    """Observed after a start: unpaused, authorised, drawing nothing.

    Distinct from idle, which means no session at all, and from
    charging, which means energy is flowing.
    """
    assert _waiting_payload()["evseStatus"] == "waiting_for_ev"


def test_switch_stays_on_while_waiting_for_the_vehicle() -> None:
    """The toggle must not snap back after a successful start.

    The charger passes through waiting-for-EV on its way to charging.
    Reporting off there would show the command as having failed.
    """
    assert payload.is_charge_enabled(_waiting_payload()) is True


def test_switch_is_on_while_charging() -> None:
    """The ordinary case still reads on."""
    assert payload.is_charge_enabled(merged()) is True


def test_switch_is_off_when_paused_or_idle() -> None:
    """A paused or idle charger is not charging."""
    paused = payload.merge_payload(REMOTE_INFO_PAUSED, EVSE_RECORD)
    assert payload.is_charge_enabled(paused) is False

    idle = payload.merge_payload(
        {**REMOTE_INFO, "evseState": 1}, EVSE_RECORD
    )
    assert payload.is_charge_enabled(idle) is False


def test_switch_state_is_unknown_without_status() -> None:
    """No status must not be reported as off."""
    assert payload.is_charge_enabled({}) is None


def test_waiting_state_is_a_declared_sensor_option() -> None:
    """An enum sensor rejects values missing from its options."""
    spec = next(
        s for s in catalog.EVSE_SENSOR_CATALOG if s.key == "evse_status"
    )
    assert spec.options is not None
    assert "waiting_for_ev" in spec.options
    assert spec.value_fn(_waiting_payload()) == "waiting_for_ev"



# ------------------------------------------------------------------
# Optimistic switch state
# ------------------------------------------------------------------


def test_no_guess_reports_the_charger() -> None:
    """With nothing commanded, the charger's reading is the answer."""
    assert payload.resolve_optimistic(None, True, False) == (True, False)
    assert payload.resolve_optimistic(None, False, False) == (False, False)
    assert payload.resolve_optimistic(None, None, False) == (None, False)


def test_guess_wins_while_the_cloud_still_reports_the_old_state() -> None:
    """This is the flip-back the optimistic state exists to prevent."""
    value, keep = payload.resolve_optimistic(True, False, False)
    assert value is True
    assert keep is True


def test_guess_is_dropped_once_the_charger_agrees() -> None:
    """Holding it longer than needed would delay real changes."""
    value, keep = payload.resolve_optimistic(True, True, False)
    assert value is True
    assert keep is False


def test_guess_is_abandoned_when_it_expires() -> None:
    """A command that silently failed must not leave the UI lying.

    Once the window passes, the charger's reading wins even though it
    contradicts what was commanded.
    """
    value, keep = payload.resolve_optimistic(True, False, True)
    assert value is False
    assert keep is False


def test_guess_survives_a_missing_reading() -> None:
    """An unknown reading is not agreement, so keep the guess."""
    value, keep = payload.resolve_optimistic(False, None, False)
    assert value is False
    assert keep is True



# ------------------------------------------------------------------
# Charging current ceiling
# ------------------------------------------------------------------


def test_ceiling_ignores_scc_limit() -> None:
    """sccLimit mirrors the current setting, so it is not a ceiling.

    On a live charger it read 11739, identical to both
    maxExternalChargingCurrentInMilliAmps and lastMaxChargingCurrent.
    Treating it as a ceiling pins the slider to wherever it already
    sits, which is worse than offering too much.
    """
    data = payload.merge_payload(
        REMOTE_INFO,
        {**EVSE_RECORD, "sccLimit": 11739, "lastMaxInstallationCurrent": 32000},
    )
    assert payload.max_charging_current(data) == 32000


def test_ceiling_follows_the_installation_rating() -> None:
    """A 16 A installation must not offer 32 A."""
    data = payload.merge_payload(
        REMOTE_INFO, {**EVSE_RECORD, "lastMaxInstallationCurrent": 16000}
    )
    assert payload.max_charging_current(data) == 16000


def test_ceiling_falls_back_to_the_installation_rating() -> None:
    """Without a reported limit, the installation rating is the best
    available answer."""
    assert (
        payload.max_charging_current({"lastMaxInstallationCurrent": 32000})
        == 32000
    )


def test_ceiling_has_a_default_before_the_first_poll() -> None:
    """The entity is built before any data arrives."""
    assert payload.max_charging_current(None) == 32000
    assert payload.max_charging_current({}) == 32000


def test_ceiling_never_drops_below_the_industry_minimum() -> None:
    """A nonsensical limit must not make the entity unusable."""
    assert (
        payload.max_charging_current({"lastMaxInstallationCurrent": 100})
        == 6000
    )


def test_ceiling_ignores_non_numeric_and_zero_values() -> None:
    """Zero appears in unset fields and must not win."""
    data = {"lastMaxInstallationCurrent": 0}
    assert payload.max_charging_current(data) == 32000

    data = {"lastMaxInstallationCurrent": None}
    assert payload.max_charging_current(data) == 32000



def test_minimum_follows_the_power_floor_not_six_amps() -> None:
    """Measured: 6400 mA was rejected, 6521 mA accepted, at 232 V.

    The charger enforces 1500 W, so the minimum current depends on the
    supply voltage. Offering a flat 6 A made the bottom of the slider
    fail every time.
    """
    data = payload.merge_payload(
        REMOTE_INFO,
        {**EVSE_RECORD, "sockets": [{"lastACVoltageL1": 232}]},
    )

    floor = payload.min_charging_current(data)

    # Above the rejected 6400, at or below the accepted 6521.
    assert 6400 < floor <= 6521
    # And genuinely over the power floor.
    assert floor * 232 / 1000 >= 1500


def test_minimum_rises_as_voltage_falls() -> None:
    """Same power, less voltage, more current."""
    low = payload.min_charging_current({"lastACVoltageL1": 220})
    high = payload.min_charging_current({"lastACVoltageL1": 245})
    assert low > high


def test_minimum_is_selectable_on_the_slider() -> None:
    """A floor between steps would be unreachable in the UI."""
    for volts in (220, 230, 232, 240, 250):
        floor = payload.min_charging_current({"lastACVoltageL1": volts})
        assert floor % payload.CURRENT_STEP_MA == 0


def test_minimum_never_below_the_evse_floor() -> None:
    """Three phase arithmetic gives a tiny current; 6 A still applies."""
    data = {"lastACVoltageL1": 232, "evseIsThreePhase": True}
    assert payload.min_charging_current(data) == 6000


def test_voltage_falls_back_when_unreported() -> None:
    """A single-phase charger reads near zero on L2 and L3."""
    assert payload.supply_voltage({}) == 230
    assert payload.supply_voltage({"lastACVoltageL1": 7}) == 230
    assert payload.supply_voltage({"lastACVoltageL1": 232}) == 232


def test_measured_boundary_is_reproduced() -> None:
    """Guard the whole rule against the captured measurement."""
    data = {"lastACVoltageL1": 232}
    floor = payload.min_charging_current(data)

    rejected = (6000, 6400)
    accepted = (6521, 8000, 32000)

    for value in rejected:
        assert value < floor, value
    for value in accepted:
        assert value >= floor, value



def test_optimistic_rule_handles_non_boolean_values() -> None:
    """The current limit and the mode lag exactly as the switch does.

    The rule was written for a boolean switch. Reusing it for a
    milliamp figure and a mode string is the point: all three read
    stale from the last poll while a change is in flight.
    """
    # A requested current, charger still reporting the old one.
    value, keep = payload.resolve_optimistic(16000, 6521, False)
    assert value == 16000
    assert keep is True

    # The charger has caught up.
    value, keep = payload.resolve_optimistic(16000, 16000, False)
    assert value == 16000
    assert keep is False

    # Held too long: reality wins even though it disagrees.
    value, keep = payload.resolve_optimistic(16000, 6521, True)
    assert value == 6521
    assert keep is False


def test_optimistic_rule_handles_mode_strings() -> None:
    """Same rule, applied to the operation mode selector."""
    value, keep = payload.resolve_optimistic("eco", "fast", False)
    assert value == "eco"
    assert keep is True

    value, keep = payload.resolve_optimistic("eco", "eco", False)
    assert value == "eco"
    assert keep is False


def test_optimistic_rule_treats_zero_as_a_real_value() -> None:
    """Zero is falsy but is a legitimate reading, not an absence."""
    value, keep = payload.resolve_optimistic(0, 6521, False)
    assert value == 0
    assert keep is True


def _main() -> int:
    """Run every test in this module and report results."""
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]

    failures = 0
    for test in tests:
        try:
            test()
        except Exception as err:  # noqa: BLE001 - standalone runner
            failures += 1
            print(f"FAIL {test.__name__}: {type(err).__name__}: {err}")
        else:
            print(f"ok   {test.__name__}")

    print(f"\n{len(tests) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
