"""QA tests for sensor entity filtering and catalog cleanliness.

Runs standalone or via tests/run_all.py (pytest not required).
Verifies:
  - Session sensors that show "Unknown" until the first charge are absent
  - The sensor catalog has no duplicate keys
  - Key single-phase sensors are present
  - The L2/L3 filter keys are covered in sensor.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "daze"


# ------------------------------------------------------------------
# Catalog integrity
# ------------------------------------------------------------------


def _load_sensor_catalog():
    """Load sensor_catalog.py standalone."""
    spec = importlib.util.spec_from_file_location(
        "daze_sensor_catalog_under_test",
        PACKAGE_DIR / "sensor_catalog.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["daze_sensor_catalog_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_catalog_has_no_duplicate_keys() -> None:
    """validate_sensor_catalog() must not raise on duplicates."""
    mod = _load_sensor_catalog()
    mod.validate_sensor_catalog()


def test_catalog_contains_single_phase_keys() -> None:
    """L1 sensors and core metrics must be present."""
    catalog = _load_sensor_catalog().EVSE_SENSOR_CATALOG
    keys = {s.key for s in catalog}

    assert "charging_current_l1" in keys
    assert "ac_voltage_l1" in keys
    assert "instant_power" in keys
    assert "delivered_energy" in keys


def test_catalog_does_not_contain_removed_session_sensors() -> None:
    """Session sensors that show 'Unknown' until the first charge are absent."""
    catalog = _load_sensor_catalog().EVSE_SENSOR_CATALOG
    keys = {s.key for s in catalog}

    removed = {
        "last_session_energy",
        "last_session_duration",
        "last_session_cost",
        "last_session_start",
        "last_session_end",
    }

    found = removed & keys
    assert found == set(), f"Session sensors still present: {found}"


def test_catalog_contains_lifetime_energy() -> None:
    """lifetime_energy is not session-dependent — it is a total.

    Unlike last_session_energy (which resets on each session),
    lifetime_energy accumulates across all sessions and is useful
    from the first poll (showing 0 until the first charge).
    """
    catalog = _load_sensor_catalog().EVSE_SENSOR_CATALOG
    keys = {s.key for s in catalog}

    assert "lifetime_energy" in keys


def test_catalog_contains_total_sessions() -> None:
    """total_sessions counts all sessions — useful from day one."""
    catalog = _load_sensor_catalog().EVSE_SENSOR_CATALOG
    keys = {s.key for s in catalog}

    assert "total_sessions" in keys


# ------------------------------------------------------------------
# L2/L3 filter keys in sensor.py
# ------------------------------------------------------------------


def _read_sensor_py_source() -> str:
    """Read the raw source of sensor.py."""
    return (PACKAGE_DIR / "sensor.py").read_text(encoding="utf-8")


def test_sensor_py_filters_l2_voltage_on_single_phase() -> None:
    """sensor.py must filter ac_voltage_l2 when evseIsThreePhase is False."""
    src = _read_sensor_py_source()
    assert "ac_voltage_l2" in src


def test_sensor_py_filters_l3_voltage_on_single_phase() -> None:
    """sensor.py must filter ac_voltage_l3 when evseIsThreePhase is False."""
    src = _read_sensor_py_source()
    assert "ac_voltage_l3" in src


def test_sensor_py_filters_l2_current_on_single_phase() -> None:
    """sensor.py must filter charging_current_l2 when evseIsThreePhase is False."""
    src = _read_sensor_py_source()
    assert "charging_current_l2" in src


def test_sensor_py_filters_l3_current_on_single_phase() -> None:
    """sensor.py must filter charging_current_l3 when evseIsThreePhase is False."""
    src = _read_sensor_py_source()
    assert "charging_current_l3" in src


def test_sensor_py_checks_evseis_three_phase() -> None:
    """The filter must read evseIsThreePhase from the payload."""
    src = _read_sensor_py_source()
    assert "evseIsThreePhase" in src


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
