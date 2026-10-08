"""Tests for solar_attributes — the solar surplus sensor attribute formatter.

Pure function so it runs without pytest or Home Assistant. Covers:
  - mode Enum → clean string (SolarMode.SIMULATE → "simulate")
  - decision None → "none" / "" / None
  - decision with action/reason/target → all three exposed
  - surplus_w passthrough (positive, negative, zero, None)
  - attribute dict has exactly five keys
  - keys are strings and stable across repeated calls
"""

from __future__ import annotations

import importlib.util
import sys
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "daze"


class SolarMode(Enum):
    """Mirror of solar_controller.SolarMode so solar.py loads."""
    OFF = "off"
    SIMULATE = "simulate"
    ACTIVE = "active"


def _load_solar():
    spec = importlib.util.spec_from_file_location(
        "daze_solar_for_test", PACKAGE_DIR / "solar.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["daze_solar_for_test"] = mod
    spec.loader.exec_module(mod)
    return mod


solar = _load_solar()
solar_attributes = solar.solar_attributes
SolarAction = solar.SolarAction
SolarDecision = solar.SolarDecision


# -- mode names -----------------------------------------------------------

def test_mode_off() -> None:
    assert solar_attributes(SolarMode.OFF, None, None)["mode"] == "off"

def test_mode_simulate() -> None:
    assert solar_attributes(SolarMode.SIMULATE, None, None)["mode"] == "simulate"

def test_mode_active() -> None:
    assert solar_attributes(SolarMode.ACTIVE, None, None)["mode"] == "active"

def test_mode_non_enum_passthrough() -> None:
    assert solar_attributes("custom", None, None)["mode"] == "custom"


# -- none decision --------------------------------------------------------

def test_none_decision_fields() -> None:
    attrs = solar_attributes(SolarMode.SIMULATE, None, 4000.0)
    assert attrs["last_decision"] == "none"
    assert attrs["reason"] == ""
    assert attrs["target_watts"] is None

def test_none_decision_still_has_mode_and_surplus() -> None:
    attrs = solar_attributes(SolarMode.SIMULATE, None, 4000.0)
    assert attrs["mode"] == "simulate"
    assert attrs["surplus_w"] == 4000.0


# -- decision values ------------------------------------------------------

def test_start_decision() -> None:
    decision = SolarDecision(
        action=SolarAction.START,
        target_watts=4500,
        reason="surplus 5000 W above floor",
    )
    attrs = solar_attributes(SolarMode.SIMULATE, decision, 5000.0)
    assert attrs["last_decision"] == "start"
    assert attrs["reason"] == "surplus 5000 W above floor"
    assert attrs["target_watts"] == 4500

def test_stop_decision_no_target() -> None:
    decision = SolarDecision(
        action=SolarAction.STOP,
        target_watts=None,
        reason="grid surplus negative",
    )
    attrs = solar_attributes(SolarMode.SIMULATE, decision, -200.0)
    assert attrs["last_decision"] == "stop"
    assert attrs["reason"] == "grid surplus negative"
    assert attrs["target_watts"] is None

def test_set_decision_with_target() -> None:
    decision = SolarDecision(
        action=SolarAction.SET,
        target_watts=2000,
        reason="surplus changed to 2000 W",
    )
    attrs = solar_attributes(SolarMode.SIMULATE, decision, 2000.0)
    assert attrs["last_decision"] == "set"
    assert attrs["reason"] == "surplus changed to 2000 W"
    assert attrs["target_watts"] == 2000

def test_nothing_decision() -> None:
    decision = SolarDecision(
        action=SolarAction.NOTHING,
        target_watts=None,
        reason="already at target",
    )
    attrs = solar_attributes(SolarMode.SIMULATE, decision, 3900.0)
    assert attrs["last_decision"] == "nothing"
    assert attrs["reason"] == "already at target"
    assert attrs["target_watts"] is None


# -- surplus_w -------------------------------------------------------------

def test_surplus_positive() -> None:
    assert solar_attributes(SolarMode.OFF, None, 4200.0)["surplus_w"] == 4200.0

def test_surplus_negative() -> None:
    assert solar_attributes(SolarMode.OFF, None, -500.0)["surplus_w"] == -500.0

def test_surplus_zero() -> None:
    assert solar_attributes(SolarMode.OFF, None, 0.0)["surplus_w"] == 0.0

def test_surplus_none() -> None:
    assert solar_attributes(SolarMode.OFF, None, None)["surplus_w"] is None


# -- attribute integrity ---------------------------------------------------

def test_exactly_five_keys() -> None:
    assert len(solar_attributes(SolarMode.OFF, None, None)) == 5

def test_expected_keys_present() -> None:
    expected = {"mode", "last_decision", "reason", "surplus_w", "target_watts"}
    assert set(solar_attributes(SolarMode.OFF, None, None).keys()) == expected

def test_all_keys_are_strings() -> None:
    attrs = solar_attributes(SolarMode.SIMULATE, None, 100.0)
    for k in attrs:
        assert isinstance(k, str)

def test_stable_across_calls() -> None:
    decision = SolarDecision(
        action=SolarAction.SET,
        target_watts=3000,
        reason="surplus at 3000 W",
    )
    a1 = solar_attributes(SolarMode.ACTIVE, decision, 3100.0)
    a2 = solar_attributes(SolarMode.ACTIVE, decision, 3100.0)
    assert a1 == a2

def test_attributes_keys_same_with_or_without_decision() -> None:
    decision = SolarDecision(
        action=SolarAction.START,
        target_watts=4000,
        reason="start",
    )
    with_d = solar_attributes(SolarMode.SIMULATE, decision, 5000.0)
    without_d = solar_attributes(SolarMode.SIMULATE, None, 5000.0)
    assert set(with_d.keys()) == set(without_d.keys())


# -- full simulation scenario ----------------------------------------------

def test_full_cycle() -> None:
    decision = SolarDecision(
        action=SolarAction.SET,
        target_watts=3500,
        reason="surplus 3500 W, current at 3000 W",
    )
    attrs = solar_attributes(SolarMode.SIMULATE, decision, 4000.0)
    assert attrs["mode"] == "simulate"
    assert attrs["last_decision"] == "set"
    assert attrs["reason"] == "surplus 3500 W, current at 3000 W"
    assert attrs["target_watts"] == 3500
    assert attrs["surplus_w"] == 4000.0

    decision2 = SolarDecision(
        action=SolarAction.STOP,
        target_watts=None,
        reason="surplus collapsed below floor",
    )
    attrs2 = solar_attributes(SolarMode.SIMULATE, decision2, -100.0)
    assert attrs2["mode"] == "simulate"
    assert attrs2["last_decision"] == "stop"
    assert attrs2["reason"] == "surplus collapsed below floor"
    assert attrs2["target_watts"] is None
    assert attrs2["surplus_w"] == -100.0

    attrs3 = solar_attributes(SolarMode.SIMULATE, None, 0.0)
    assert attrs3["last_decision"] == "none"
    assert attrs3["reason"] == ""


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
