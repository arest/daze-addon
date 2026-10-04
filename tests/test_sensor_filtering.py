"""QA tests for sensor catalog cleanliness and the L2/L3 phase filter.

Runs standalone or via tests/run_all.py (pytest not required).

An earlier version of this module asserted that certain strings appeared
in the text of sensor.py. Every one of those assertions passed with the
phase check inverted — the exact regression they were named for — which
is the failure shape rule 3 of the project's QA notes describes. They
are replaced here by calls into the predicate itself, each of which was
confirmed to fail under that inversion.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "daze"


# ------------------------------------------------------------------
# Loading
#
# sensor.py imports Home Assistant, which is not installed. Only the
# names sensor.py resolves at import time are stubbed; the predicate
# under test touches none of them.
# ------------------------------------------------------------------


def _module(name: str, **attributes: Any) -> types.ModuleType:
    """Register a stub module under ``name``."""
    module = types.ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    sys.modules[name] = module
    return module


def _install_stubs() -> None:
    """Install the minimum Home Assistant surface sensor.py imports.

    Registered unconditionally rather than skipped when a
    ``homeassistant`` key already exists. Under a whole-tree pytest run
    another suite gets there first with a thinner set — test_entities.py
    installs a ``homeassistant.const`` without UnitOfElectricPotential
    and a ``SensorEntityDescription`` of plain ``object``, neither of
    which sensor.py can import against — so deferring to it failed
    collection for this module while running it alone passed. Each
    suite binds the names it needs immediately after installing them,
    so overwriting here cannot disturb a suite already loaded, and one
    loaded later installs its own in turn.
    """
    _module("homeassistant")
    _module("homeassistant.components")
    _module("homeassistant.helpers")

    class _StrEnum(str):
        pass

    # sensor.py subclasses this with @dataclass(frozen=True,
    # kw_only=True), so the base has to carry the fields it sets.
    @dataclass(frozen=True, kw_only=True)
    class _SensorEntityDescription:
        key: str
        device_class: Any = None
        state_class: Any = None
        native_unit_of_measurement: Any = None
        entity_category: Any = None
        options: Any = None

    _module(
        "homeassistant.components.sensor",
        SensorDeviceClass=type(
            "SensorDeviceClass",
            (),
            {
                name: _StrEnum(name.lower())
                for name in (
                    "POWER",
                    "ENERGY",
                    "CURRENT",
                    "VOLTAGE",
                    "TEMPERATURE",
                    "ENUM",
                    "MONETARY",
                    "TIMESTAMP",
                )
            },
        ),
        SensorEntity=object,
        SensorEntityDescription=_SensorEntityDescription,
        SensorStateClass=type(
            "SensorStateClass",
            (),
            {
                "MEASUREMENT": _StrEnum("measurement"),
                "TOTAL_INCREASING": _StrEnum("total_increasing"),
            },
        ),
    )
    _module(
        "homeassistant.const",
        EntityCategory=type(
            "EntityCategory", (), {"DIAGNOSTIC": _StrEnum("diagnostic")}
        ),
        UnitOfElectricCurrent=type(
            "UnitOfElectricCurrent", (), {"MILLIAMPERE": _StrEnum("mA")}
        ),
        UnitOfElectricPotential=type(
            "UnitOfElectricPotential", (), {"VOLT": _StrEnum("V")}
        ),
        UnitOfEnergy=type("UnitOfEnergy", (), {"WATT_HOUR": _StrEnum("Wh")}),
        UnitOfPower=type("UnitOfPower", (), {"WATT": _StrEnum("W")}),
        UnitOfTemperature=type(
            "UnitOfTemperature", (), {"CELSIUS": _StrEnum("°C")}
        ),
        UnitOfTime=type("UnitOfTime", (), {"MINUTES": _StrEnum("min")}),
    )
    _module(
        "homeassistant.helpers.device_registry", DeviceInfo=dict
    )
    _module(
        "homeassistant.helpers.restore_state",
        RestoreEntity=type("RestoreEntity", (), {}),
    )
    _module(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=type(
            "CoordinatorEntity", (), {"__class_getitem__": classmethod(lambda c, i: c)}
        ),
    )


def _load(name: str, filename: str) -> Any:
    """Load one integration module as part of a stub package."""
    package = "daze_sensor_under_test"
    if package not in sys.modules:
        parent = types.ModuleType(package)
        parent.__path__ = [str(PACKAGE_DIR)]
        sys.modules[package] = parent

    full = f"{package}.{name}"
    if full in sys.modules:
        return sys.modules[full]

    spec = importlib.util.spec_from_file_location(
        full, PACKAGE_DIR / filename
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


_install_stubs()
_load("const", "const.py")
_load("payload", "payload.py")
_load("models", "models.py")
_load("sensor_catalog", "sensor_catalog.py")

catalog = sys.modules["daze_sensor_under_test.sensor_catalog"]


def _load_sensor_module() -> Any:
    """Load sensor.py, stubbing the coordinator import it needs."""
    package = "daze_sensor_under_test"
    _module(
        f"{package}.coordinator",
        DazeCoordinatorData=dict,
        DazeDataUpdateCoordinator=type("DazeDataUpdateCoordinator", (), {}),
    )
    return _load("sensor", "sensor.py")


sensor = _load_sensor_module()
is_unreportable_phase_sensor = sensor.is_unreportable_phase_sensor


# ------------------------------------------------------------------
# The L2/L3 phase filter
#
# Each test below fails when `is not True` is weakened to `not`, or the
# sense of the check is inverted. Confirmed by mutation, not assumed.
# ------------------------------------------------------------------

SINGLE_PHASE: dict[str, Any] = {
    "evseIsThreePhase": False,
    "lastACVoltageL1": 230,
    "lastACVoltageL2": 1,
    "lastACVoltageL3": 7,
    "lastChargingCurrentInstantL1": 17211,
    "lastChargingCurrentInstantL2": 0,
    "lastChargingCurrentInstantL3": 0,
}

THREE_PHASE: dict[str, Any] = {
    "evseIsThreePhase": True,
    "lastACVoltageL1": 230,
    "lastACVoltageL2": 231,
    "lastACVoltageL3": 229,
    "lastChargingCurrentInstantL1": 16000,
    "lastChargingCurrentInstantL2": 16000,
    "lastChargingCurrentInstantL3": 16000,
}

PHASE_UNKNOWN: dict[str, Any] = {
    "lastACVoltageL1": 230,
    "lastACVoltageL2": 1,
    "lastACVoltageL3": 7,
}

L2_L3_KEYS = (
    "ac_voltage_l2",
    "ac_voltage_l3",
    "charging_current_l2",
    "charging_current_l3",
)


def test_single_phase_withholds_every_l2_l3_sensor() -> None:
    """The 1 V and 7 V readings must not reach Home Assistant.

    Measured on a DT01: with nothing connected to L2 or L3 the charger
    still reports 1 V and 7 V, which a user reasonably reads as a
    wiring fault.
    """
    for key in L2_L3_KEYS:
        assert is_unreportable_phase_sensor(key, SINGLE_PHASE) is True, key


def test_three_phase_publishes_every_l2_l3_sensor() -> None:
    """A real three-phase install must keep its L2/L3 readings.

    This is the half an inverted check breaks silently: the junk
    disappears either way, so only this direction notices.
    """
    for key in L2_L3_KEYS:
        assert is_unreportable_phase_sensor(key, THREE_PHASE) is False, key


def test_an_absent_phase_flag_withholds_rather_than_guesses() -> None:
    """Unknown phase count is not an invitation to publish.

    Absence of information is never grounds for acting, and publishing
    a reading whose meaning is unverified is the action here.
    """
    for key in L2_L3_KEYS:
        assert is_unreportable_phase_sensor(key, PHASE_UNKNOWN) is True, key


def test_l1_is_never_withheld() -> None:
    """L1 is real on every charger, single- or three-phase."""
    for data in (SINGLE_PHASE, THREE_PHASE, PHASE_UNKNOWN):
        assert is_unreportable_phase_sensor("ac_voltage_l1", data) is False
        assert (
            is_unreportable_phase_sensor("charging_current_l1", data) is False
        )


def test_unrelated_sensors_are_never_withheld() -> None:
    """The filter must not reach beyond the four keys it names."""
    for key in (
        "instant_power",
        "delivered_energy",
        "board_temperature",
        "evse_status",
        "grid_max_power",
    ):
        assert is_unreportable_phase_sensor(key, SINGLE_PHASE) is False, key


def test_a_truthy_non_true_flag_does_not_count_as_three_phase() -> None:
    """Only a real boolean True publishes.

    The API has been seen returning strings where booleans were
    expected. "False" is truthy, so a plain `if data.get(...)` would
    publish junk on a single-phase charger reporting the string.
    """
    for impostor in ("False", "false", "0", 0, "", None):
        data = {**SINGLE_PHASE, "evseIsThreePhase": impostor}
        assert (
            is_unreportable_phase_sensor("ac_voltage_l2", data) is True
        ), impostor


def test_the_filtered_keys_all_exist_in_the_catalog() -> None:
    """A typo'd key would silently filter nothing at all."""
    catalog_keys = {spec.key for spec in catalog.EVSE_SENSOR_CATALOG}

    for key in sensor.PHASE_2_3_SENSOR_KEYS:
        assert key in catalog_keys, f"{key} is not a catalog sensor"


# ------------------------------------------------------------------
# Catalog integrity
# ------------------------------------------------------------------


def test_catalog_has_no_duplicate_keys() -> None:
    """A duplicate key means two entities fight over one unique_id."""
    catalog.validate_sensor_catalog()


def test_catalog_keeps_the_live_metrics() -> None:
    """The sensors a charging session is actually read through."""
    keys = {spec.key for spec in catalog.EVSE_SENSOR_CATALOG}

    for expected in (
        "instant_power",
        "delivered_energy",
        "charging_current_l1",
        "ac_voltage_l1",
        "board_temperature",
        "evse_status",
    ):
        assert expected in keys, expected


def test_catalog_drops_the_per_session_sensors() -> None:
    """They read "Unknown" until a session completes and never recover.

    The recharge-session endpoint returns 404 on this account, so these
    five never populate at all. An entity that is permanently unknown
    is indistinguishable from a broken one.
    """
    keys = {spec.key for spec in catalog.EVSE_SENSOR_CATALOG}

    removed = {
        "last_session_energy",
        "last_session_duration",
        "last_session_cost",
        "last_session_start",
        "last_session_end",
    }

    assert removed & keys == set(), f"still present: {sorted(removed & keys)}"


def test_restore_state_keys_all_name_a_real_sensor() -> None:
    """A restore key naming a removed sensor restores nothing.

    Dropping the per-session sensors left their keys behind in
    RESTORE_STATE_KEYS, where they are silently inert.
    """
    keys = {spec.key for spec in catalog.EVSE_SENSOR_CATALOG}
    orphans = sorted(catalog.RESTORE_STATE_KEYS - keys)

    assert not orphans, f"RESTORE_STATE_KEYS names removed sensors: {orphans}"


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
