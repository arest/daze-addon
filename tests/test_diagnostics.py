"""Tests for the diagnostics handler.

Runs standalone or via tests/run_all.py (pytest not required).

Diagnostics output is the one payload in this integration a user is
actively encouraged to copy out of Home Assistant and paste into a
public issue. The first test below is therefore the point of the
module: whatever else changes, no token may appear in it.

The handler imports Home Assistant, which is not installed. Only the
names it resolves are stubbed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "daze"


# ------------------------------------------------------------------
# Stubs
# ------------------------------------------------------------------


def _module(name: str, **attributes: Any) -> types.ModuleType:
    """Register a stub module under ``name``."""
    module = types.ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    sys.modules[name] = module
    return module


class _Registry:
    """Stand-in for the entity registry."""

    def __init__(self, entries: list[Any] | None = None) -> None:
        self.entries = entries or []


_REGISTRY = _Registry()


def _install_stubs() -> None:
    """Install the Home Assistant surface diagnostics.py imports.

    Registered unconditionally. Another suite in the same interpreter
    may have installed a thinner ``homeassistant`` first, and deferring
    to it is what broke collection for test_sensor_filtering.py under a
    whole-tree pytest run. The names this module needs are bound
    immediately below, so overwriting cannot disturb a suite already
    loaded.
    """
    _module("homeassistant")
    _module("homeassistant.helpers")
    _module(
        "homeassistant.helpers.entity_registry",
        async_get=lambda hass: _REGISTRY,
        async_entries_for_config_entry=lambda registry, entry_id: (
            registry.entries
        ),
    )


def _load(name: str, filename: str) -> Any:
    """Load one integration module as part of a stub package."""
    package = "daze_diagnostics_under_test"
    if package not in sys.modules:
        parent = types.ModuleType(package)
        parent.__path__ = [str(PACKAGE_DIR)]
        sys.modules[package] = parent

    full = f"{package}.{name}"
    spec = importlib.util.spec_from_file_location(
        full, PACKAGE_DIR / filename
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


_install_stubs()
const = _load("const", "const.py")
diagnostics = _load("diagnostics", "diagnostics.py")


# ------------------------------------------------------------------
# Fakes
#
# Values below are fabricated. The two token strings are the shape a
# real Cognito token has, so a leak is detectable, and nothing else.
# ------------------------------------------------------------------

FAKE_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.FAKEACCESSTOKENFORTESTS.0000"
FAKE_REFRESH_TOKEN = "eyJjdHkiOiJKV1QifQ.FAKEREFRESHTOKENFORTESTS.1111"


class FakeAuthClient:
    """Carries tokens, as the real one does."""

    def __init__(self, token_expiry: float | None = 1_000_000.0) -> None:
        self._access_token = FAKE_ACCESS_TOKEN
        self._refresh_token = FAKE_REFRESH_TOKEN
        self.token_expiry = token_expiry

    @property
    def access_token(self) -> str:
        """Return the access token."""
        return self._access_token

    def is_token_expired(self) -> bool:
        """Return whether the token has expired."""
        return False

    def async_get_tokens_for_store(self) -> dict[str, Any]:
        """Return the storable token dict, as the real client does."""
        return {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "token_expiry": self.token_expiry,
        }


class FakeApiClient:
    """Exposes the auth client, as the real one does."""

    def __init__(self, auth_client: FakeAuthClient) -> None:
        self.auth_client = auth_client


class FakeCoordinator:
    """Minimal coordinator surface the handler reads."""

    def __init__(
        self,
        auth_client: FakeAuthClient | None = None,
        update_interval: timedelta | None = timedelta(seconds=30),
    ) -> None:
        self.api_client = FakeApiClient(auth_client or FakeAuthClient())
        self.last_update_success = True
        self.last_success_time = 1_000.0
        self.last_fail_time = None
        self.total_updates = 42
        self.consecutive_failures = 0
        self.update_interval = update_interval
        self.serial_number = "SER1"
        self.network_uid = "NET1"


class FakeEntry:
    """Minimal config entry surface the handler reads."""

    def __init__(self) -> None:
        self.entry_id = "entry-1"
        self.title = "Daze HomeTT"
        self.source = "user"
        self.unique_id = "SER1"
        self.disabled_by = None
        self.data = {
            "access_token": FAKE_ACCESS_TOKEN,
            "refresh_token": FAKE_REFRESH_TOKEN,
            "serial_number": "SER1",
        }


class FakeHass:
    """Carries the integration's entry data."""

    def __init__(self, coordinator: FakeCoordinator, entry: FakeEntry) -> None:
        self.data = {
            const.DOMAIN: {entry.entry_id: {"coordinator": coordinator}}
        }


class FakeRegistryEntry:
    """One registry row, with the only field the handler reads."""

    def __init__(self, platform: str) -> None:
        self.platform = platform


def _run(
    coordinator: FakeCoordinator | None = None,
    entry: FakeEntry | None = None,
    registry_entries: list[Any] | None = None,
) -> dict[str, Any]:
    """Call the handler against fakes and return its output."""
    coordinator = coordinator or FakeCoordinator()
    entry = entry or FakeEntry()
    _REGISTRY.entries = registry_entries or []

    return asyncio.run(
        diagnostics.async_get_config_entry_diagnostics(
            FakeHass(coordinator, entry), entry
        )
    )


# ------------------------------------------------------------------
# The point of the module
# ------------------------------------------------------------------


def test_no_token_reaches_the_diagnostics_output() -> None:
    """A user pastes this into a public issue. It cannot carry tokens.

    Serialised rather than walked key by key, so a token appearing
    under a key nobody thought of is still caught.
    """
    blob = json.dumps(_run(), default=str)

    assert FAKE_ACCESS_TOKEN not in blob, "access token leaked"
    assert FAKE_REFRESH_TOKEN not in blob, "refresh token leaked"


def test_no_token_shaped_string_reaches_the_output() -> None:
    """Catches a token that is transformed rather than copied whole.

    A truncation or a prefix slice would pass the equality check above
    while still disclosing key material, so the JWT marker is checked
    independently of the exact fake values.
    """
    blob = json.dumps(_run(), default=str)

    assert "eyJ" not in blob, "a JWT-shaped string reached the output"


def test_the_leak_check_can_actually_see_a_token() -> None:
    """Guards the two tests above against a fake that carries nothing.

    If FakeAuthClient stopped holding tokens, both leak tests would
    pass for the wrong reason and keep passing forever.
    """
    auth = FakeAuthClient()

    assert FAKE_ACCESS_TOKEN in json.dumps(auth.async_get_tokens_for_store())
    assert "eyJ" in auth.access_token


# ------------------------------------------------------------------
# Shape and content
# ------------------------------------------------------------------


def test_every_documented_section_is_present() -> None:
    """The docstring promises four sections; absence breaks consumers."""
    result = _run()

    for section in ("auth", "coordinator", "entities", "config_entry"):
        assert section in result, section


def test_the_whole_payload_is_json_serialisable() -> None:
    """Home Assistant serialises this; a stray object raises at download.

    ``default=str`` is deliberately not passed here — that is what the
    leak tests use to be thorough, and it would mask exactly the
    failure this test exists to catch.
    """
    json.dumps(_run())


def test_coordinator_state_is_reported_not_invented() -> None:
    """The figures must come from the coordinator, not a default."""
    coordinator = FakeCoordinator()
    coordinator.total_updates = 7
    coordinator.consecutive_failures = 3
    coordinator.last_fail_time = 1_234.0

    reported = _run(coordinator=coordinator)["coordinator"]

    assert reported["total_updates"] == 7
    assert reported["consecutive_failures"] == 3
    assert reported["last_fail_time"] == 1_234.0
    assert reported["serial_number"] == "SER1"
    assert reported["network_uid"] == "NET1"


def test_the_poll_interval_is_reported_in_seconds() -> None:
    """A timedelta is not serialisable; it has to be converted."""
    coordinator = FakeCoordinator(update_interval=timedelta(seconds=45))

    assert _run(coordinator=coordinator)["coordinator"][
        "update_interval_seconds"
    ] == 45.0


def test_an_absent_poll_interval_reports_none_rather_than_raising() -> None:
    """update_interval is Optional on the base coordinator."""
    coordinator = FakeCoordinator(update_interval=None)

    assert (
        _run(coordinator=coordinator)["coordinator"][
            "update_interval_seconds"
        ]
        is None
    )


# ------------------------------------------------------------------
# Token expiry arithmetic
# ------------------------------------------------------------------


def test_an_absent_expiry_reports_no_age_rather_than_raising() -> None:
    """token_expiry is None until the first refresh.

    Subtracting from None raises, and a diagnostics handler that
    raises is one the user cannot send when they most need to.
    """
    coordinator = FakeCoordinator(auth_client=FakeAuthClient(token_expiry=None))

    auth = _run(coordinator=coordinator)["auth"]

    assert auth["token_expiry_timestamp"] is None
    assert auth["token_age_seconds"] is None


def test_a_known_expiry_reports_an_age() -> None:
    """The age is derived from the expiry and the one-hour token life."""
    coordinator = FakeCoordinator(
        auth_client=FakeAuthClient(token_expiry=2_000_000_000.0)
    )

    auth = _run(coordinator=coordinator)["auth"]

    assert auth["token_expiry_timestamp"] == 2_000_000_000.0
    assert auth["token_age_seconds"] is not None


def test_the_issuer_is_reported_without_credentials() -> None:
    """The issuer URL is useful and carries nothing secret."""
    assert _run()["auth"]["issuer"] == const.COGNITO_BASE_URL


# ------------------------------------------------------------------
# Entity inventory
# ------------------------------------------------------------------


def test_entities_are_counted_per_platform() -> None:
    """The inventory is the quickest read on a half-loaded setup."""
    entries = [
        FakeRegistryEntry("sensor"),
        FakeRegistryEntry("sensor"),
        FakeRegistryEntry("sensor"),
        FakeRegistryEntry("switch"),
        FakeRegistryEntry("number"),
        FakeRegistryEntry("number"),
    ]

    inventory = _run(registry_entries=entries)["entities"]

    assert inventory == {"sensor": 3, "switch": 1, "number": 2}


def test_an_empty_registry_reports_an_empty_inventory() -> None:
    """A setup that registered nothing must report that, not crash."""
    assert _run(registry_entries=[])["entities"] == {}


def test_config_entry_metadata_is_reported() -> None:
    """Enough to identify the entry, and nothing from entry.data.

    entry.data holds both tokens. The handler must never reach into
    it, which the leak tests enforce; this one fixes what it does
    report instead.
    """
    reported = _run()["config_entry"]

    assert reported["entry_id"] == "entry-1"
    assert reported["title"] == "Daze HomeTT"
    assert reported["unique_id"] == "SER1"
    assert reported["disabled_by"] is None


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
