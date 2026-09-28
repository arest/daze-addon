"""Invariants that must hold for any charger, not just the one tested.

The bounds on the charging current are computed rather than fixed, so
they can be wrong in ways a single captured payload will not reveal: a
different supply voltage, a smaller installation, or a three-phase
unit. These tests sweep those inputs and assert the properties that
have to hold in every case.

They complement test_payload.py, which pins behaviour against captured
responses from one specific charger.

Run with pytest, or standalone:

    python3 tests/test_qa_invariants.py
"""

from __future__ import annotations

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


payload = _load("daze_payload_qa", "payload.py")

# Realistic supply voltages, from a sagging rural feed to a strong one.
VOLTAGES = (207, 220, 228, 230, 232, 240, 245, 253)

# Installation ratings seen on domestic wallboxes.
INSTALLATIONS = (10000, 13000, 16000, 20000, 25000, 32000)


def _payload(volts: int, installation: int, three_phase: bool = False) -> dict:
    """Build a minimal payload with the fields the bounds depend on."""
    return {
        "lastACVoltageL1": volts,
        "lastMaxInstallationCurrent": installation,
        "evseIsThreePhase": three_phase,
    }


# ------------------------------------------------------------------
# Bound invariants
# ------------------------------------------------------------------


def test_minimum_never_exceeds_maximum() -> None:
    """A slider whose floor is above its ceiling cannot be rendered."""
    for volts in VOLTAGES:
        for installation in INSTALLATIONS:
            data = _payload(volts, installation)
            low = payload.min_charging_current(data)
            high = payload.max_charging_current(data)
            assert low <= high, (volts, installation, low, high)


def test_every_offered_value_clears_the_power_floor() -> None:
    """The slider must not offer a value the charger will reject.

    This is the bug that started this: 6 A was offered and rejected
    because it fell under the charger's 1500 W minimum.
    """
    for volts in VOLTAGES:
        data = _payload(volts, 32000)
        low = payload.min_charging_current(data)
        watts = low * volts / 1000
        assert watts >= payload.MIN_CHARGING_POWER_W, (volts, low, watts)


def test_minimum_is_not_needlessly_high() -> None:
    """Rounding up must cost at most one step.

    A floor set too high silently removes usable charging rates.
    """
    step = payload.CURRENT_STEP_MA
    for volts in VOLTAGES:
        data = _payload(volts, 32000)
        low = payload.min_charging_current(data)
        exact = payload.MIN_CHARGING_POWER_W / volts * 1000
        if low > payload.ABSOLUTE_MIN_CHARGING_CURRENT_MA:
            assert low - exact < step, (volts, low, exact)


def test_bounds_land_on_selectable_steps() -> None:
    """A bound between steps is unreachable in the frontend."""
    step = payload.CURRENT_STEP_MA
    for volts in VOLTAGES:
        for installation in INSTALLATIONS:
            data = _payload(volts, installation)
            assert payload.min_charging_current(data) % step == 0
            assert payload.max_charging_current(data) % step == 0


def test_maximum_never_exceeds_the_installation_rating() -> None:
    """Offering more than the installation allows invites a rejection."""
    for installation in INSTALLATIONS:
        data = _payload(230, installation)
        assert payload.max_charging_current(data) <= max(
            installation, payload.ABSOLUTE_MIN_CHARGING_CURRENT_MA
        )


def test_minimum_moves_monotonically_with_voltage() -> None:
    """Higher voltage needs less current for the same power."""
    floors = [
        payload.min_charging_current(_payload(volts, 32000))
        for volts in sorted(VOLTAGES)
    ]
    assert floors == sorted(floors, reverse=True), floors


def test_three_phase_falls_back_to_the_evse_floor() -> None:
    """Three-phase arithmetic gives a current below any EVSE minimum."""
    for volts in VOLTAGES:
        data = _payload(volts, 32000, three_phase=True)
        assert (
            payload.min_charging_current(data)
            == payload.ABSOLUTE_MIN_CHARGING_CURRENT_MA
        )


# ------------------------------------------------------------------
# Robustness against missing or nonsense data
# ------------------------------------------------------------------


def test_bounds_survive_every_degenerate_payload() -> None:
    """Bounds are read before the first poll and from partial data."""
    degenerate: tuple[dict | None, ...] = (
        None,
        {},
        {"lastACVoltageL1": None},
        {"lastACVoltageL1": 0},
        {"lastACVoltageL1": "230"},
        {"lastMaxInstallationCurrent": None},
        {"lastMaxInstallationCurrent": 0},
        {"lastMaxInstallationCurrent": -5},
        {"lastACVoltageL1": 7, "lastMaxInstallationCurrent": 32000},
    )

    for data in degenerate:
        low = payload.min_charging_current(data)
        high = payload.max_charging_current(data)
        assert low >= payload.ABSOLUTE_MIN_CHARGING_CURRENT_MA, data
        assert high >= low, data


# ------------------------------------------------------------------
# Status invariants
# ------------------------------------------------------------------


def test_status_is_always_a_declared_option() -> None:
    """An enum sensor rejects a value outside its options."""
    catalog = _load("daze_catalog_qa", "sensor_catalog.py")
    spec = next(
        s for s in catalog.EVSE_SENSOR_CATALOG if s.key == "evse_status"
    )
    assert spec.options is not None
    allowed = set(spec.options)

    for state in range(12):
        for paused in (True, False):
            for error in (0, 4):
                for active in (True, False):
                    data = payload.merge_payload(
                        {
                            "evseState": state,
                            "isPaused": paused,
                            "evseSystemError": error,
                            "active": active,
                        },
                        None,
                    )
                    status = data.get("evseStatus")
                    assert status in allowed, (state, paused, error, active)


def test_switch_state_agrees_with_the_status() -> None:
    """The switch must never claim on while the status says paused."""
    for state in range(12):
        data = payload.merge_payload({"evseState": state}, None)
        status = data.get("evseStatus")
        enabled = payload.is_charge_enabled(data)

        if status in ("paused", "idle", "offline", "error"):
            assert enabled is False, (state, status)
        else:
            assert enabled is True, (state, status)



def test_floor_never_excludes_a_configured_value() -> None:
    """A slider that omits the charger's own setting is broken.

    Sweeps settings against the voltage-less case, which is when the
    computed floor is least trustworthy.
    """
    for configured in (6000, 6200, 6521, 6600, 8000, 16000, 32000):
        data = {"maxExternalChargingCurrentInMilliAmps": configured}
        floor = payload.min_charging_current(data)
        assert floor <= max(
            configured, payload.ABSOLUTE_MIN_CHARGING_CURRENT_MA
        ), (configured, floor)


def test_clamping_only_ever_lowers_the_floor() -> None:
    """Knowing the setting must not raise the minimum."""
    for configured in (6000, 6521, 16000, 32000):
        bare = payload.min_charging_current({"lastACVoltageL1": 232})
        with_setting = payload.min_charging_current(
            {
                "lastACVoltageL1": 232,
                "maxExternalChargingCurrentInMilliAmps": configured,
            }
        )
        assert with_setting <= bare, (configured, bare, with_setting)


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
