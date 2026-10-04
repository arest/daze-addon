"""Every name the code asks for must exist in the English translations.

An entity that sets ``translation_key`` is asking Home Assistant to
look the name up. For a custom integration that lookup reads
``translations/<language>.json``. ``strings.json`` at the component
root is the source file a core integration's translations are built
from, and is not what gets read at runtime, so an integration that
ships only ``strings.json`` has entities that ask for a name nobody
can answer.

Home Assistant's fallback for an unanswered lookup is the device name
on its own, which is not an error and does not appear in the log. So
the whole device fills with entities called "Daze HomeTT", every one
of them identical, and the only signal is a user saying they cannot
find a sensor. That happened: the names were added in 65dd791 and
shipped without an en.json, and the sensor stayed invisible across an
upgrade that was supposed to fix exactly that.

These tests walk the keys the code actually asks for, rather than
comparing the two files to each other, because two files that agree
with one another and with nothing the code requests would pass a
comparison and still leave the entity nameless.

Run with pytest, or standalone:

    python3 tests/test_translations.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "daze"
TRANSLATIONS = PACKAGE / "translations"


def _english() -> dict[str, Any]:
    """The English translations Home Assistant will actually read."""
    return json.loads((TRANSLATIONS / "en.json").read_text())


def _literal_translation_keys() -> set[str]:
    """Translation keys written as literals in the platform modules."""
    keys: set[str] = set()
    for path in PACKAGE.glob("*.py"):
        text = path.read_text()
        keys |= set(
            re.findall(r'_attr_translation_key\s*=\s*["\'](\w+)["\']', text)
        )
        keys |= set(
            re.findall(r'translation_key\s*=\s*["\'](\w+)["\']', text)
        )
    return keys


def _catalog_keys() -> set[str]:
    """Sensor keys, which sensor.py passes through as translation keys."""
    source = (PACKAGE / "sensor_catalog.py").read_text()
    return set(re.findall(r'key="(\w+)"', source))


def _declared_entity_names(translations: dict[str, Any]) -> set[str]:
    """Every entity key that has a name in a translations file."""
    declared: set[str] = set()
    for entities in translations.get("entity", {}).values():
        for key, body in entities.items():
            if isinstance(body, dict) and body.get("name"):
                declared.add(key)
    return declared


def test_an_english_translations_file_is_shipped() -> None:
    """The file Home Assistant reads has to be in the package.

    strings.json is not a substitute. It is the build-time source for
    core integrations and is never loaded for a custom one.
    """
    path = TRANSLATIONS / "en.json"

    assert path.is_file(), (
        "custom_components/daze/translations/en.json is missing, so every "
        "entity that sets a translation_key falls back to the bare device "
        "name on an English installation"
    )


def test_every_entity_name_the_code_asks_for_is_answered() -> None:
    """The lookup the entities perform must succeed.

    Checked against the keys the code requests, not against
    strings.json, so the two files agreeing with each other cannot
    stand in for either of them answering the code.
    """
    wanted = _literal_translation_keys() | _catalog_keys()
    # Not an entity; it names the options list on the supply-phases
    # selector, which the selector test below covers instead.
    wanted.discard("supply_phases")

    missing = sorted(wanted - _declared_entity_names(_english()))

    assert not missing, (
        "entities ask for names that en.json does not define, so they "
        f"will show as the device name: {missing}"
    )


def test_the_supply_phases_selector_has_its_options_in_english() -> None:
    """The options flow dropdown is unreadable without these.

    Without them the user picks between two untranslated keys when
    answering the question that gates solar control.
    """
    selector = _english().get("selector", {}).get("supply_phases", {})
    options = selector.get("options", {})

    assert "single" in options and "three" in options, (
        f"supply_phases options missing from en.json: {sorted(options)}"
    )


def test_the_config_flow_errors_are_translated() -> None:
    """A raw error key is what the user sees when setup fails.

    These are the strings shown at the exact moment the integration is
    least able to explain itself, so an untranslated "invalid_token"
    is worse here than almost anywhere else.
    """
    errors = _english().get("config", {}).get("error", {})
    expected = {"invalid_token", "network_error", "no_networks", "no_evses"}

    missing = sorted(expected - set(errors))

    assert not missing, f"config flow errors missing from en.json: {missing}"


def test_every_shipped_language_parses() -> None:
    """A malformed translations file takes the whole integration down."""
    for path in sorted(TRANSLATIONS.glob("*.json")):
        try:
            json.loads(path.read_text())
        except json.JSONDecodeError as err:
            raise AssertionError(f"{path.name} is not valid JSON: {err}")


def test_english_covers_every_entity_the_other_languages_name() -> None:
    """English is the fallback, so it cannot be the thinnest file.

    A key translated into Italian but absent from English leaves an
    English user with no name for an entity an Italian user can read,
    which is the inversion of how the fallback is supposed to work.
    """
    english = _declared_entity_names(_english())

    for path in sorted(TRANSLATIONS.glob("*.json")):
        if path.name == "en.json":
            continue

        other = _declared_entity_names(json.loads(path.read_text()))
        missing = sorted(other - english)

        assert not missing, (
            f"{path.name} names entities en.json does not: {missing}"
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
