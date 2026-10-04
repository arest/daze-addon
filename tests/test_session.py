"""Tests for the session model and the coordinator's session fields.

Runs standalone or via tests/run_all.py (pytest not required).

This module previously defined its own copy of RechargeSession,
_parse_datetime, _safe_float and compute_session_fields, and tested
those. None of its 27 tests could fail on a change to the integration:
removing the lifetime sum from the real
DazeDataUpdateCoordinator._compute_session_fields left all 27 passing
while test_coordinator.py caught it immediately.

The copy had also drifted. Its _parse_datetime called
datetime.fromtimestamp(value) with no tz, where models.py passes
tz=timezone.utc, so the copy produced naive local datetimes and the
real code produces aware UTC ones. The tests did not notice, because
they asserted only isinstance(result, datetime).

Both are now imported from the integration, and the tz difference the
copy hid is asserted directly.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "daze"
PKG_NAME = "daze_session_under_test"


# ------------------------------------------------------------------
# Loading
#
# models.py imports nothing from Home Assistant. coordinator.py does,
# so the names it resolves at import time are stubbed. Registered
# unconditionally: deferring to a thinner set installed by another
# suite is what broke collection for test_sensor_filtering.py under a
# whole-tree pytest run.
# ------------------------------------------------------------------


def _module(name: str, **attributes: Any) -> types.ModuleType:
    """Register a stub module under ``name``."""
    module = types.ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    sys.modules[name] = module
    return module


class _StubDataUpdateCoordinator:
    """Stand-in for the Home Assistant base coordinator."""

    def __init__(self, hass: Any, logger: Any, **kwargs: Any) -> None:
        self.hass = hass
        self.logger = logger
        self.name = kwargs.get("name")
        self.update_interval = kwargs.get("update_interval")
        self.data: Any = None
        self.last_update_success = True

    def __class_getitem__(cls, item: Any) -> type:
        return cls


def _install_homeassistant_stubs() -> None:
    """Register just enough of Home Assistant to import coordinator.py."""
    _module("homeassistant")
    _module("homeassistant.core", HomeAssistant=object, callback=lambda fn: fn)
    _module("homeassistant.config_entries", ConfigEntry=object)
    _module(
        "homeassistant.exceptions",
        ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}),
        HomeAssistantError=type("HomeAssistantError", (Exception,), {}),
    )
    _module("homeassistant.helpers")
    _module(
        "homeassistant.helpers.aiohttp_client",
        async_get_clientsession=lambda hass: None,
    )
    _module(
        "homeassistant.helpers.event",
        async_call_later=lambda hass, delay, action: (lambda: None),
        async_track_state_change_event=lambda hass, entities, cb: (
            lambda: None
        ),
    )
    _module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=_StubDataUpdateCoordinator,
        UpdateFailed=type("UpdateFailed", (Exception,), {}),
    )


def _load_integration() -> tuple[Any, Any]:
    """Load the real models and coordinator modules.

    Returns:
        The ``models`` and ``coordinator`` modules.

    """
    _install_homeassistant_stubs()

    package = types.ModuleType(PKG_NAME)
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules[PKG_NAME] = package

    def _sub(name: str, relative: str, search: list[str] | None = None) -> Any:
        spec = importlib.util.spec_from_file_location(
            f"{PKG_NAME}.{name}",
            PACKAGE_DIR / relative,
            submodule_search_locations=search,
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{PKG_NAME}.{name}"] = module
        spec.loader.exec_module(module)
        return module

    for name in ("const", "payload", "models", "optimistic"):
        _sub(name, f"{name}.py")

    _sub("api", "api/__init__.py", [str(PACKAGE_DIR / "api")])
    _sub("api.auth", "api/auth.py")

    return (
        sys.modules[f"{PKG_NAME}.models"],
        _sub("coordinator", "coordinator.py"),
    )


models, coordinator_module = _load_integration()

RechargeSession = models.RechargeSession
_parse_datetime = models._parse_datetime
_safe_float = models._safe_float

# The real implementation, reached through the class that owns it.
compute_session_fields = (
    coordinator_module.DazeDataUpdateCoordinator._compute_session_fields
)


# ------------------------------------------------------------------
# Sample data
# ------------------------------------------------------------------

COMPLETED_SESSION: dict[str, Any] = {
    "sessionUid": "sess-001",
    "startDate": "2026-05-01T14:00:00Z",
    "endDate": "2026-05-01T16:30:00Z",
    "energyInWh": 15000,
    "totalCost": 3.75,
    "currency": "EUR",
    "status": "completed",
    "evseSerialNumber": "DAZE-12345",
}

IN_PROGRESS_SESSION: dict[str, Any] = {
    "sessionUid": "sess-002",
    "startDate": "2026-05-18T10:00:00Z",
    "endDate": None,
    "energyInWh": 8000,
    "totalCost": None,
    "currency": "EUR",
    "status": "charging",
    "evseSerialNumber": "DAZE-12345",
}

PARTIAL_DICT: dict[str, Any] = {"sessionUid": "sess-003"}

TIMESTAMP_INT_DICT: dict[str, Any] = {
    "sessionUid": "sess-004",
    "startDate": 1715000000,
    "endDate": 1715010000,
    "energyInWh": 10000,
    "totalCost": 2.50,
    "currency": "EUR",
    "status": "completed",
}


def _session(**overrides: Any) -> Any:
    """Build a RechargeSession directly, bypassing from_dict."""
    defaults: dict[str, Any] = {
        "session_uid": "sess",
        "start_time": None,
        "end_time": None,
        "energy_wh": None,
        "cost": None,
    }
    defaults.update(overrides)
    return RechargeSession(**defaults)


# ------------------------------------------------------------------
# The module under test is the real one
# ------------------------------------------------------------------


def test_the_model_under_test_is_the_integrations_own() -> None:
    """Guards against this file drifting back into a local copy.

    The copy this module used to carry passed every test here while
    the real code was broken, so the coupling is the thing most worth
    asserting.
    """
    assert RechargeSession.__module__.endswith("models")
    assert models.__file__ == str(PACKAGE_DIR / "models.py")
    assert compute_session_fields.__module__.endswith("coordinator")


# ------------------------------------------------------------------
# RechargeSession.from_dict
# ------------------------------------------------------------------


def test_a_completed_session_maps_every_field() -> None:
    """The ordinary case: all fields present and well formed."""
    session = RechargeSession.from_dict(COMPLETED_SESSION)

    assert session.session_uid == "sess-001"
    assert session.energy_wh == 15000
    assert session.cost == 3.75
    assert session.currency == "EUR"
    assert session.status == "completed"
    assert session.evse_serial == "DAZE-12345"
    assert session.is_in_progress is False


def test_an_in_progress_session_has_no_end_and_reports_itself() -> None:
    """A charge under way has no endDate and no cost yet."""
    session = RechargeSession.from_dict(IN_PROGRESS_SESSION)

    assert session.end_time is None
    assert session.cost is None
    assert session.is_in_progress is True


def test_a_completed_status_ends_a_session_without_an_end_time() -> None:
    """Status settles it when endDate is missing but the charge ended."""
    session = RechargeSession.from_dict(
        {**IN_PROGRESS_SESSION, "status": "completed"}
    )

    assert session.end_time is None
    assert session.is_in_progress is False


def test_an_empty_payload_does_not_raise() -> None:
    """The API shape is not guaranteed; a crash here kills the poll."""
    session = RechargeSession.from_dict({})

    assert session.session_uid == ""
    assert session.start_time is None
    assert session.energy_wh is None


def test_a_partial_payload_keeps_what_it_has() -> None:
    """An in-progress session legitimately lacks most fields."""
    session = RechargeSession.from_dict(PARTIAL_DICT)

    assert session.session_uid == "sess-003"
    assert session.cost is None


def test_the_raw_payload_is_preserved() -> None:
    """Diagnostics read fields the model does not name."""
    session = RechargeSession.from_dict(COMPLETED_SESSION)

    assert session.raw == COMPLETED_SESSION


def test_from_dict_never_raises_on_junk() -> None:
    """Every field wrong at once must still produce a session."""
    for junk in (
        {"sessionUid": None},
        {"energyInWh": "not a number"},
        {"startDate": "not a date"},
        {"totalCost": object()},
        {"startDate": [], "endDate": {}},
    ):
        RechargeSession.from_dict(junk)


# ------------------------------------------------------------------
# Datetime parsing
#
# The tz assertions below are the ones the old copy could not make.
# ------------------------------------------------------------------


def test_an_iso_string_parses_to_an_aware_datetime() -> None:
    """Z-suffixed ISO 8601 is what the API returns."""
    result = _parse_datetime("2026-05-01T14:00:00Z")

    assert result == datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)
    assert result.tzinfo is not None


def test_an_integer_timestamp_parses_as_utc_not_local() -> None:
    """The drift the old copy carried, asserted directly.

    models.py passes tz=timezone.utc; the copy called fromtimestamp
    with no tz and produced a naive local datetime. Only an equality
    check against a known instant can tell those apart — isinstance
    cannot, which is why the copy survived.
    """
    result = _parse_datetime(1715000000)

    assert result == datetime(2024, 5, 6, 12, 53, 20, tzinfo=timezone.utc)
    assert result.tzinfo is timezone.utc


def test_a_float_timestamp_parses_as_utc_too() -> None:
    """Floats arrive from JSON wherever the API emits fractions."""
    result = _parse_datetime(1715000000.0)

    assert result == datetime(2024, 5, 6, 12, 53, 20, tzinfo=timezone.utc)
    assert result.tzinfo is timezone.utc


def test_a_datetime_passes_through_unchanged() -> None:
    """Already-parsed values must not be reparsed."""
    now = datetime.now(timezone.utc)

    assert _parse_datetime(now) is now


def test_an_unparseable_string_is_none_not_an_exception() -> None:
    """A bad date must not take the whole session list with it."""
    for bad in ("not a date", "", "2026-13-45T99:99:99Z"):
        assert _parse_datetime(bad) is None, bad


def test_none_parses_to_none() -> None:
    """Absent is absent, not epoch zero."""
    assert _parse_datetime(None) is None


def test_a_wrong_type_parses_to_none() -> None:
    """Lists and dicts appear where scalars were expected."""
    for bad in ([], {}, object()):
        assert _parse_datetime(bad) is None


# ------------------------------------------------------------------
# Float coercion
# ------------------------------------------------------------------


def test_a_float_passes_through() -> None:
    """The ordinary case."""
    assert _safe_float(3.75) == 3.75


def test_an_integer_becomes_a_float() -> None:
    """Energy arrives as an int and is compared against floats."""
    result = _safe_float(15000)

    assert result == 15000.0
    assert isinstance(result, float)


def test_a_numeric_string_becomes_a_float() -> None:
    """The API is not guaranteed to quote numbers consistently."""
    assert _safe_float("3.75") == 3.75


def test_a_non_numeric_value_is_none() -> None:
    """None, not zero. Zero is a reading; absent is not."""
    for bad in ("abc", "", [], {}, object()):
        assert _safe_float(bad) is None, bad


def test_none_coerces_to_none() -> None:
    """Absent cost is absent, not free."""
    assert _safe_float(None) is None


# ------------------------------------------------------------------
# Coordinator session fields
# ------------------------------------------------------------------


def test_no_sessions_reports_zeroes_and_nones() -> None:
    """A fresh install has no history and must not crash on it."""
    fields = compute_session_fields([])

    assert fields["total_sessions"] == 0
    assert fields["lifetime_energy"] == 0.0
    assert fields["last_session_energy"] is None
    assert fields["last_session_cost"] is None
    assert fields["last_session_duration"] is None


def test_the_newest_session_is_the_one_reported() -> None:
    """Sessions arrive newest-first; index 0 is "last session"."""
    newest = _session(session_uid="new", energy_wh=1000.0, cost=1.0)
    older = _session(session_uid="old", energy_wh=2000.0, cost=2.0)

    fields = compute_session_fields([newest, older])

    assert fields["last_session_energy"] == 1000.0
    assert fields["last_session_cost"] == 1.0


def test_lifetime_energy_sums_every_session() -> None:
    """Not just the last one, and not just the completed ones."""
    sessions = [_session(energy_wh=float(n)) for n in (1000, 2000, 3000)]

    fields = compute_session_fields(sessions)

    assert fields["lifetime_energy"] == 6000.0
    assert fields["total_sessions"] == 3


def test_a_session_with_no_energy_does_not_break_the_sum() -> None:
    """A None reading is skipped, not coerced to zero and added."""
    sessions = [_session(energy_wh=1000.0), _session(energy_wh=None)]

    fields = compute_session_fields(sessions)

    assert fields["lifetime_energy"] == 1000.0
    assert fields["total_sessions"] == 2


def test_a_closed_session_reports_its_duration_in_minutes() -> None:
    """Start and end both known: the arithmetic is exact."""
    start = datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 1, 16, 30, tzinfo=timezone.utc)

    fields = compute_session_fields([_session(start_time=start, end_time=end)])

    assert fields["last_session_duration"] == 150.0


def test_an_open_session_measures_duration_against_now() -> None:
    """The in-progress path, and the one tz actually breaks.

    It computes ``datetime.now(timezone.utc) - last.start_time``. A
    naive start time raises TypeError there, which is what the old
    copy's naive fromtimestamp would eventually have produced.
    """
    start = datetime.now(timezone.utc) - timedelta(minutes=30)

    fields = compute_session_fields([_session(start_time=start)])

    assert fields["last_session_duration"] is not None
    assert 29.0 <= fields["last_session_duration"] <= 31.0


def test_a_timestamp_sourced_session_survives_the_duration_path() -> None:
    """End to end: int timestamps through from_dict into the duration.

    This is the join the copy hid. If _parse_datetime ever returns a
    naive datetime again, the subtraction against an aware now() in
    the open-session branch raises.
    """
    session = RechargeSession.from_dict(
        {"sessionUid": "s", "startDate": int(
            (datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp()
        )}
    )

    fields = compute_session_fields([session])

    assert fields["last_session_duration"] is not None
    assert 9.0 <= fields["last_session_duration"] <= 11.0


def test_a_session_with_neither_time_reports_no_duration() -> None:
    """No start means nothing to measure from."""
    fields = compute_session_fields([_session()])

    assert fields["last_session_duration"] is None


def test_api_order_is_preserved_rather_than_sorted() -> None:
    """Re-sorting would change which session counts as the last."""
    first = _session(session_uid="first", energy_wh=1.0)
    second = _session(session_uid="second", energy_wh=2.0)

    fields = compute_session_fields([first, second])

    assert fields["last_session_energy"] == 1.0


def test_a_long_history_aggregates_correctly() -> None:
    """Guards the sum against an accumulator bug at scale."""
    sessions = [_session(energy_wh=float(n)) for n in range(10_000)]

    fields = compute_session_fields(sessions)

    assert fields["total_sessions"] == 10_000
    assert fields["lifetime_energy"] == sum(float(n) for n in range(10_000))
    assert fields["last_session_energy"] == 0.0


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
