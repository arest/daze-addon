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


def test_only_one_config_entry_is_allowed() -> None:
    """Two entries would make every service call ambiguous.

    The three services are registered at domain level and act on the
    coordinator captured when they were registered — there is no entity
    or device target in their schema. With a second entry,
    ``daze.start_charge`` reaches whichever charger registered last,
    silently, and a reload swaps which one that is. Enforced in the
    manifest so Home Assistant refuses the second entry in the UI
    rather than leaving it to the flow.

    The manifest flag is the whole of it. An earlier version of this
    test also asserted a ``single_instance_allowed`` string in each
    locale file, on the assumption that the flag would otherwise abort
    with an untranslated reason. It does not: Home Assistant raises
    that abort itself, from two sites in ``config_entries.py``, both
    passing ``translation_domain=HOMEASSISTANT_DOMAIN``. The frontend
    resolves ``component.homeassistant.config.abort.single_instance_
    allowed`` and never looks at this integration's copy, so the
    strings were dead and the assertion gave false assurance that an
    explanation reached the user.
    """
    import json

    manifest = json.loads((PACKAGE_DIR / "manifest.json").read_text())
    assert manifest.get("single_config_entry") is True, (
        "manifest does not restrict the integration to one entry"
    )

    for filename in _translation_files():
        data = json.loads((PACKAGE_DIR / filename).read_text())
        assert "single_instance_allowed" not in data["config"]["abort"], (
            f"{filename} defines single_instance_allowed, which Home "
            "Assistant resolves from its own domain — the string is "
            "dead and will never be shown"
        )


def _declared_translation_keys() -> set[str]:
    """Every translation key the integration actually asks Home Assistant for.

    Two sources: the literal ``_attr_translation_key`` on each entity
    class, and the sensor catalog, whose key is passed through as the
    description's translation_key.

    Note what this cannot see. Reading the catalog assumes the
    pass-through in ``sensor._to_description`` exists; delete that one
    argument and this set is unchanged, so the locale check below stays
    green while every sensor loses its name. Verified by mutation, and
    it is why ``test_every_sensor_description_carries_its_translation_key``
    in tests/test_sensor_filtering.py asserts against the built
    descriptions instead. This module has no Home Assistant stubs and
    so cannot import sensor.py to do that itself.
    """
    import re

    keys: set[str] = set()
    for path in PACKAGE_DIR.glob("*.py"):
        keys |= set(
            re.findall(
                r'_attr_translation_key = "([^"]+)"', path.read_text()
            )
        )

    catalog = _load("daze_catalog_for_names", "sensor_catalog.py")
    keys |= {spec.key for spec in catalog.EVSE_SENSOR_CATALOG}
    return keys


def _translation_files() -> list[str]:
    """Every file that has to carry the names, discovered not listed.

    Previously this was the hard-coded pair ("strings.json",
    "translations/it.json"), which is how an integration whose entity
    names were entirely missing in English passed this suite 15 out
    of 15. Two things were wrong with that list and both mattered:

    - ``en.json`` was absent, and it is the file Home Assistant
      actually reads for an English installation. ``strings.json`` is
      the build-time source a core integration's translations are
      generated from and is never loaded for a custom one, so the
      list checked one real locale and one file that is not a locale
      at all -- while the test's own name promised "every locale".

    - Being a list at all, a language added later is unchecked until
      somebody remembers to extend it, which is the same failure
      waiting to happen again.

    Globbing the directory fixes both. A new locale is covered the
    moment it is added, and the file that is read at runtime cannot
    be the one left out.
    """
    translations = sorted(
        f"translations/{path.name}"
        for path in (PACKAGE_DIR / "translations").glob("*.json")
    )
    assert translations, "no translation files found — the scan is broken"
    assert "translations/en.json" in translations, (
        "translations/en.json is missing; every entity falls back to the "
        "bare device name on an English installation"
    )
    # strings.json is kept in the sweep deliberately. It is not loaded
    # at runtime, but it is the source the locale files are generated
    # from, so a key missing here is a key the next generated locale
    # will be missing too.
    return ["strings.json", *translations]


def _defined_names(filename: str) -> set[str]:
    """Every entity name key defined in a translation file."""
    import json

    data = json.loads((PACKAGE_DIR / filename).read_text())
    return {key for platform in data["entity"].values() for key in platform}


def test_every_translation_key_resolves_in_every_locale() -> None:
    """A declared key with no matching name is a silently unnamed entity.

    This is not hypothetical. Before 2026-10-04 no entity in this
    integration set a translation key at all, so all 23 names in
    strings.json bound to nothing and every entity fell back to its
    device_class default — "Power", "Current", "Energy" — which is what
    the operator saw in the UI. Nothing failed, because nothing checked
    that the two halves met.

    Asserted in both directions and across every file that carries
    the names -- discovered by globbing translations/, not listed --
    because each direction is a different defect: a declared key with no name shows
    the fallback, and a defined name with no key is dead weight that
    outlives the entity it was written for.
    """
    declared = _declared_translation_keys()
    assert declared, "no translation keys found — the scan itself is broken"

    for filename in _translation_files():
        defined = _defined_names(filename)
        assert not declared - defined, (
            f"{filename}: declared but undefined: "
            f"{sorted(declared - defined)}"
        )
        assert not defined - declared, (
            f"{filename}: defined but unused: {sorted(defined - declared)}"
        )


import ast


def _class_declares(node: ast.ClassDef, attribute: str) -> bool:
    """Whether this class body assigns the attribute."""
    for statement in node.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        else:
            continue
        if any(
            isinstance(t, ast.Name) and t.id == attribute for t in targets
        ):
            return True
    return False


def _resolves(
    classes: dict[str, ast.ClassDef],
    name: str,
    attribute: str,
    seen: set[str] | None = None,
) -> bool:
    """Whether the class or any in-module base assigns the attribute."""
    seen = seen if seen is not None else set()
    if name in seen or name not in classes:
        return False
    seen.add(name)
    node = classes[name]
    if _class_declares(node, attribute):
        return True
    return any(
        isinstance(base, ast.Name)
        and _resolves(classes, base.id, attribute, seen)
        for base in node.bases
    )


def _declared_key(node: ast.ClassDef) -> str | None:
    """The translation key literal this class itself assigns."""
    for statement in node.body:
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if (
                isinstance(target, ast.Name)
                and target.id == "_attr_translation_key"
                and isinstance(statement.value, ast.Constant)
            ):
                return str(statement.value.value)
    return None


def test_every_entity_class_declares_a_translation_key() -> None:
    """has_entity_name without a name or key falls back to device_class.

    Every entity class in this integration sets
    ``_attr_has_entity_name = True``. That tells Home Assistant the
    entity supplies only its own name and the device supplies the rest
    — so a class that then supplies neither a name nor a translation
    key gets the device_class default instead, which is how 23 written
    names went unused for the life of the project.

    The sensor platform is exempt by construction: its single entity
    class takes the key from the catalog through the description rather
    than from a class attribute.

    Resolved through the class hierarchy rather than counted. This was
    two regex counts compared for equality, which reads the file as a
    bag of lines: a shared base declaring has_entity_name once for two
    subclasses that each declare their own key gives 1 against 2 and
    fails, though every entity is correctly named. Counting also
    passes on the inverse — one class with a key and another with
    none — whenever the totals happen to match.

    What actually matters is per-class and inherited: a class that is
    instantiated must resolve a translation key somewhere in its own
    bases. Abstract bases are exempt, because nothing instantiates
    them; a class that is nobody's base is not.
    """
    for filename in ("switch.py", "number.py", "select.py"):
        tree = ast.parse((PACKAGE_DIR / filename).read_text())
        classes = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
        }
        in_module_bases = {
            base.id
            for node in classes.values()
            for base in node.bases
            if isinstance(base, ast.Name)
        }

        concrete = []
        for name in classes:
            if name in in_module_bases:
                continue  # abstract: nothing instantiates it
            if not _resolves(classes, name, "_attr_has_entity_name"):
                continue  # not an entity class
            assert _resolves(classes, name, "_attr_translation_key"), (
                f"{filename}: {name} sets has_entity_name but resolves no "
                "translation key, so it falls back to its device_class "
                "default instead of a written name"
            )
            concrete.append(name)

        # Each concrete entity needs its own key, not merely some key.
        # Sharing became possible when these classes gained a common
        # base: a key set on the base resolves for every subclass, so
        # each one satisfies the assertion above while the device shows
        # two entities under one name.
        keys = [k for k in (_declared_key(classes[n]) for n in concrete) if k]
        duplicates = {k for k in keys if keys.count(k) > 1}
        assert not duplicates, (
            f"{filename}: translation key reused by sibling entities, "
            f"which renders them identically: {sorted(duplicates)}"
        )
        unkeyed = [n for n in concrete if _declared_key(classes[n]) is None]
        assert not unkeyed, (
            f"{filename}: {unkeyed} inherit a translation key rather than "
            "declaring one, so they share a name with their sibling"
        )


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
