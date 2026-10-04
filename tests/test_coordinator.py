"""Exercise the coordinator's session cache and background retry chain.

Two parts of coordinator.py had no coverage at all before this file,
and both are the kind that fail silently:

- ``_async_fetch_sessions`` distinguishes "could not read the history"
  from "there is no history", because assigning an empty list over a
  good one drops ``lifetime_energy`` to zero. That sensor is
  ``total_increasing``, so Home Assistant reads the drop as a meter
  reset and counts the whole lifetime again when the endpoint recovers.
  The 404 branch used to return ``[]`` and cause exactly that.

- ``async_retry_in_background`` keeps a refused command going for
  roughly seven and a half minutes across five attempts, superseding
  an older chain when a newer command arrives and standing down on
  unload. Every one of those paths is timer-driven, so nothing about
  it is observable from the entity tests.

Home Assistant is replaced with the smallest stubs the module touches,
following test_init_entry.py. ``async_call_later`` records each
scheduled callback instead of running it, so a test fires the chain by
hand and can assert the exact delay sequence.

Run with pytest, or standalone:

    python3 tests/test_coordinator.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "daze"
PKG_NAME = "daze_coordinator_under_test"


# ------------------------------------------------------------------
# Home Assistant stubs
# ------------------------------------------------------------------

# Every (delay, callback) handed to async_call_later, newest last.
SCHEDULED: list[tuple[int, Any]] = []


class StubDataUpdateCoordinator:
    """Stand-in for DataUpdateCoordinator.

    Needed so the real class statement can be executed at import time.
    The constructor signature mirrors the real one loosely enough for
    the coordinator's own ``super().__init__`` call to land.
    """

    def __class_getitem__(cls, _item: Any) -> Any:
        return cls

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.hass = args[0] if args else None
        self.data: dict[str, Any] | None = None
        self.refresh_count = 0

    async def async_request_refresh(self) -> None:
        """Record that a refresh was asked for."""
        self.refresh_count += 1


def _module(name: str, **attributes: Any) -> types.ModuleType:
    """Build a stub module with the given attributes."""
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _async_call_later(hass: Any, delay: int, action: Any) -> Any:
    """Record a scheduled callback and return its canceller."""
    entry = (delay, action)
    SCHEDULED.append(entry)

    def cancel() -> None:
        if entry in SCHEDULED:
            SCHEDULED.remove(entry)

    return cancel


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
        async_call_later=_async_call_later,
        async_track_state_change_event=lambda hass, entities, cb: (
            lambda: None
        ),
    )
    _module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=StubDataUpdateCoordinator,
        UpdateFailed=type("UpdateFailed", (Exception,), {}),
    )


_install_homeassistant_stubs()


def _load_coordinator() -> types.ModuleType:
    """Load the real coordinator module against the stubs above."""
    package = types.ModuleType(PKG_NAME)
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules[PKG_NAME] = package

    for name in ("const", "payload", "models", "optimistic"):
        spec = importlib.util.spec_from_file_location(
            f"{PKG_NAME}.{name}", PACKAGE_DIR / f"{name}.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{PKG_NAME}.{name}"] = module
        spec.loader.exec_module(module)

    spec = importlib.util.spec_from_file_location(
        f"{PKG_NAME}.api",
        PACKAGE_DIR / "api" / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR / "api")],
    )
    assert spec and spec.loader
    api_module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG_NAME}.api"] = api_module
    spec.loader.exec_module(api_module)

    spec = importlib.util.spec_from_file_location(
        f"{PKG_NAME}.api.auth", PACKAGE_DIR / "api" / "auth.py"
    )
    assert spec and spec.loader
    auth_module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG_NAME}.api.auth"] = auth_module
    spec.loader.exec_module(auth_module)

    spec = importlib.util.spec_from_file_location(
        f"{PKG_NAME}.coordinator", PACKAGE_DIR / "coordinator.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG_NAME}.coordinator"] = module
    spec.loader.exec_module(module)
    return module


coordinator_module = _load_coordinator()
api_module = sys.modules[f"{PKG_NAME}.api"]
const = sys.modules[f"{PKG_NAME}.const"]
models = sys.modules[f"{PKG_NAME}.models"]

DazeDataUpdateCoordinator = coordinator_module.DazeDataUpdateCoordinator
RechargeSession = models.RechargeSession
ApiError = api_module.ApiError
ApiAuthError = api_module.ApiAuthError
ApiNotFoundError = api_module.ApiNotFoundError
BACKGROUND_RETRY_DELAYS = const.BACKGROUND_RETRY_DELAYS


# ------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------


class FakeApiClient:
    """An API client whose session endpoint does whatever a test says."""

    def __init__(self, sessions_result: Any = None) -> None:
        self.sessions_result = sessions_result
        self.session_calls = 0

    async def async_get_recharge_sessions(
        self, network_uid: str
    ) -> list[dict[str, Any]]:
        """Return the configured sessions, or raise the configured error."""
        self.session_calls += 1
        if isinstance(self.sessions_result, Exception):
            raise self.sessions_result
        return self.sessions_result or []


def build(sessions_result: Any = None) -> Any:
    """Construct a coordinator with every timer stubbed out."""
    SCHEDULED.clear()
    client = FakeApiClient(sessions_result)
    return DazeDataUpdateCoordinator(
        hass=object(),
        api_client=client,
        serial_number="SER1",
        network_uid="net-1",
    )


def session(energy_wh: float) -> Any:
    """Build a RechargeSession carrying just an energy figure."""
    return RechargeSession(session_uid="s1", energy_wh=energy_wh)


def run(awaitable: Any) -> Any:
    """Drive a coroutine to completion."""
    return asyncio.run(awaitable)


def fire_next() -> None:
    """Run the callback scheduled most recently, as the timer would."""
    assert SCHEDULED, "nothing was scheduled"
    _delay, action = SCHEDULED.pop(0)
    run(action(None))


# ------------------------------------------------------------------
# Session cache
# ------------------------------------------------------------------


def test_a_successful_fetch_returns_the_sessions() -> None:
    """The ordinary path parses the payload into RechargeSessions."""
    coordinator = build([{"uid": "s1", "energyWh": 1200}])

    result = run(coordinator._async_fetch_sessions())

    assert result is not None
    assert len(result) == 1


def test_a_404_does_not_report_an_empty_history() -> None:
    """A 404 must not read as "there are no sessions".

    Returning [] here is what overwrote a good cached history and
    dropped lifetime_energy to zero on a total_increasing sensor.
    """
    coordinator = build(ApiNotFoundError("404"))

    result = run(coordinator._async_fetch_sessions())

    assert result is None, (
        "a 404 reported an empty history instead of an unreadable one"
    )


def test_a_404_leaves_a_previously_fetched_history_intact() -> None:
    """The cache survives the endpoint starting to 404.

    This is the failure in full: the history is read once, the endpoint
    then 404s, and the lifetime figure must not collapse to zero.
    """
    coordinator = build(ApiNotFoundError("404"))
    coordinator._cached_sessions = [session(5000.0), session(3000.0)]

    result = run(coordinator._async_fetch_sessions())
    if result is not None:
        coordinator._cached_sessions = result

    fields = coordinator._compute_session_fields(
        coordinator._cached_sessions
    )
    assert fields["lifetime_energy"] == 8000.0, (
        "a 404 reset the lifetime energy of a charger that had history"
    )
    assert fields["total_sessions"] == 2


def test_a_404_backs_off_rather_than_retrying_every_poll() -> None:
    """The hourly back-off is armed so the log is not spammed."""
    coordinator = build(ApiNotFoundError("404"))
    coordinator._next_session_fetch = 0.0

    run(coordinator._async_fetch_sessions())

    assert coordinator._next_session_fetch > 0.0


def test_a_404_logs_only_once() -> None:
    """The "unavailable" notice is not repeated on every back-off."""
    coordinator = build(ApiNotFoundError("404"))

    run(coordinator._async_fetch_sessions())
    assert coordinator._sessions_missing_logged is True

    # A second call must not reset the latch.
    run(coordinator._async_fetch_sessions())
    assert coordinator._sessions_missing_logged is True


def test_a_transient_api_error_keeps_the_previous_history() -> None:
    """An ApiError is not evidence that the history is gone."""
    coordinator = build(ApiError("boom"))

    assert run(coordinator._async_fetch_sessions()) is None


def test_an_auth_error_keeps_the_previous_history() -> None:
    """Nor is an auth error on the secondary endpoint."""
    coordinator = build(ApiAuthError("401"))

    assert run(coordinator._async_fetch_sessions()) is None


def test_a_recovered_endpoint_clears_the_missing_latch() -> None:
    """Once the history reads again, the notice can be logged afresh."""
    coordinator = build(ApiNotFoundError("404"))
    run(coordinator._async_fetch_sessions())
    assert coordinator._sessions_missing_logged is True

    coordinator._api_client.sessions_result = [{"uid": "s1", "energyWh": 10}]
    run(coordinator._async_fetch_sessions())

    assert coordinator._sessions_missing_logged is False


def test_an_empty_history_is_still_reported_as_empty() -> None:
    """A genuine empty list from a working endpoint is not None.

    The 404 fix must not blunt the real "no sessions yet" answer, or a
    brand-new charger would hold whatever the cache happened to have.
    """
    coordinator = build([])

    assert run(coordinator._async_fetch_sessions()) == []


# ------------------------------------------------------------------
# Background retry
# ------------------------------------------------------------------


def test_a_retry_is_scheduled_at_the_first_configured_delay() -> None:
    """The chain opens at BACKGROUND_RETRY_DELAYS[0], not immediately."""
    coordinator = build()

    async def action() -> None:
        return None

    coordinator.async_retry_in_background(
        key="k", action=action, description="setting the current"
    )

    assert len(SCHEDULED) == 1
    assert SCHEDULED[0][0] == BACKGROUND_RETRY_DELAYS[0]


def test_a_failing_chain_walks_the_configured_delays() -> None:
    """Each failure queues the next delay in order."""
    coordinator = build()
    attempts = {"n": 0}

    async def always_fails() -> None:
        attempts["n"] += 1
        raise ApiError("still refused")

    coordinator.async_retry_in_background(
        key="k", action=always_fails, description="setting the current"
    )

    seen = [SCHEDULED[0][0]]
    for _ in range(len(BACKGROUND_RETRY_DELAYS) - 1):
        fire_next()
        seen.append(SCHEDULED[0][0])

    assert seen == list(BACKGROUND_RETRY_DELAYS)
    assert attempts["n"] == len(BACKGROUND_RETRY_DELAYS) - 1


def test_an_exhausted_chain_reports_failure_once() -> None:
    """on_failure fires after the last attempt, and nothing is requeued."""
    coordinator = build()
    failures: list[str] = []

    async def always_fails() -> None:
        raise ApiError("still refused")

    coordinator.async_retry_in_background(
        key="k",
        action=always_fails,
        description="setting the current",
        on_failure=failures.append,
    )

    for _ in range(len(BACKGROUND_RETRY_DELAYS)):
        fire_next()

    assert len(failures) == 1, f"on_failure fired {len(failures)} times"
    assert SCHEDULED == [], "a chain that gave up left a timer armed"
    assert "k" not in coordinator._pending_retries


def test_a_successful_attempt_stops_the_chain() -> None:
    """A command that lands is not retried again."""
    coordinator = build()
    calls = {"n": 0}

    async def succeeds() -> None:
        calls["n"] += 1

    coordinator.async_retry_in_background(
        key="k", action=succeeds, description="setting the current"
    )
    fire_next()

    assert calls["n"] == 1
    assert SCHEDULED == []
    assert "k" not in coordinator._pending_retries


def test_a_successful_attempt_forces_a_fresh_charger_record() -> None:
    """The EVSE cache is invalidated so the change can be confirmed.

    The settings a command changes live only in the cached EVSE
    record, so leaving it in place would show the pre-command value
    for up to EVSE_FETCH_INTERVAL.
    """
    coordinator = build()
    coordinator._next_evse_fetch = 1e12

    async def succeeds() -> None:
        return None

    coordinator.async_retry_in_background(
        key="k", action=succeeds, description="setting the current"
    )
    fire_next()

    assert coordinator._next_evse_fetch == 0.0
    assert coordinator.refresh_count == 1


def test_a_newer_command_supersedes_an_older_one() -> None:
    """Two requests under one key leave a single live chain."""
    coordinator = build()
    ran: list[str] = []

    async def first() -> None:
        ran.append("first")

    async def second() -> None:
        ran.append("second")

    coordinator.async_retry_in_background(
        key="k", action=first, description="first"
    )
    coordinator.async_retry_in_background(
        key="k", action=second, description="second"
    )

    assert len(SCHEDULED) == 1, "the superseded chain kept its timer"
    fire_next()
    assert ran == ["second"]


def test_separate_keys_do_not_supersede_each_other() -> None:
    """A current change and a charge change run independently."""
    coordinator = build()

    async def noop() -> None:
        return None

    coordinator.async_retry_in_background(
        key="serial:current", action=noop, description="current"
    )
    coordinator.async_retry_in_background(
        key="serial:charge", action=noop, description="charge"
    )

    assert len(SCHEDULED) == 2


def test_cancelling_a_chain_stops_it() -> None:
    """async_cancel_background_retry disarms the pending attempt."""
    coordinator = build()

    async def noop() -> None:
        return None

    coordinator.async_retry_in_background(
        key="k", action=noop, description="setting the current"
    )
    coordinator.async_cancel_background_retry("k")

    assert SCHEDULED == []
    assert "k" not in coordinator._pending_retries


def test_cancelling_an_unknown_key_is_harmless() -> None:
    """Cancelling what was never scheduled does not raise."""
    coordinator = build()

    coordinator.async_cancel_background_retry("never-scheduled")


def test_a_chain_cancelled_mid_flight_does_not_requeue() -> None:
    """A cancel landing during an attempt ends the chain.

    The attempt is in flight for tens of seconds, so this window is
    real: rescheduling from inside it would undo both the supersede
    on a newer command and the shutdown on unload.
    """
    coordinator = build()

    async def fails_after_being_cancelled() -> None:
        coordinator.async_cancel_background_retry("k")
        raise ApiError("refused")

    coordinator.async_retry_in_background(
        key="k",
        action=fails_after_being_cancelled,
        description="setting the current",
    )
    fire_next()

    assert SCHEDULED == [], "a cancelled chain rearmed itself"


def test_a_chain_cancelled_mid_flight_does_not_refresh() -> None:
    """A superseded attempt that succeeds does not trigger a refresh."""
    coordinator = build()

    async def succeeds_after_being_cancelled() -> None:
        coordinator.async_cancel_background_retry("k")

    coordinator.async_retry_in_background(
        key="k",
        action=succeeds_after_being_cancelled,
        description="setting the current",
    )
    fire_next()

    assert coordinator.refresh_count == 0
    assert SCHEDULED == []


def test_shutdown_stands_every_chain_down() -> None:
    """Unloading the entry leaves no retry firing at a closed client."""
    coordinator = build()

    async def noop() -> None:
        return None

    coordinator.async_retry_in_background(
        key="serial:current", action=noop, description="current"
    )
    coordinator.async_retry_in_background(
        key="serial:charge", action=noop, description="charge"
    )

    coordinator.async_shutdown_timers()

    assert SCHEDULED == []
    assert coordinator._pending_retries == {}


# ------------------------------------------------------------------
# A history that was never readable is not a history of zero
#
# The 404 tests above cover the case where a good history is already
# cached. They do not cover the one this charger is actually in: the
# endpoint has 404'd from the first poll, so the cache has never held
# anything. The coordinator's own log line promises the session and
# lifetime sensors "will stay empty" in that situation, and before
# these tests it published a hard zero instead -- on sensors declared
# total_increasing, which is the meter-reset reading the 404 fix
# exists to prevent.
# ------------------------------------------------------------------


def test_a_history_never_successfully_read_reports_no_lifetime() -> None:
    """Never read is unknown, and unknown is not zero.

    A fabricated 0.0 on a total_increasing sensor is not a harmless
    placeholder: Home Assistant's statistics engine reads the step
    down to zero as a meter reset, so the whole lifetime figure is
    counted a second time if the endpoint ever starts answering.
    """
    coordinator = build(ApiNotFoundError("404"))

    run(coordinator._async_fetch_sessions())
    fields = coordinator.session_fields()

    assert fields["lifetime_energy"] is None, (
        "an unreadable history published a lifetime of zero"
    )
    assert fields["total_sessions"] is None, (
        "an unreadable history published a session count of zero"
    )


def test_a_successful_empty_history_still_reports_zero() -> None:
    """A fresh charger genuinely has zero sessions, and says so.

    The guard above must key on whether the history was ever read,
    not on whether it is empty, or a brand-new install never reports
    the zero that is its honest answer.
    """
    coordinator = build([])

    run(coordinator._async_fetch_sessions())
    fields = coordinator.session_fields()

    assert fields["lifetime_energy"] == 0.0, (
        "a charger with a readable but empty history reported unknown"
    )
    assert fields["total_sessions"] == 0


def test_a_404_after_a_good_read_keeps_reporting_the_good_figures() -> None:
    """Once read, the history stays reported through later 404s.

    Guards the seam between the two rules above: the "never read"
    flag must latch on the first success and stay latched, or a
    later 404 would blank a lifetime figure that is still known.
    """
    coordinator = build([{"sessionUid": "s1", "energyInWh": 5000.0}])
    first = run(coordinator._async_fetch_sessions())
    assert first is not None
    coordinator._cached_sessions = first

    coordinator._api_client.sessions_result = ApiNotFoundError("404")
    coordinator._next_session_fetch = 0.0
    result = run(coordinator._async_fetch_sessions())
    if result is not None:
        coordinator._cached_sessions = result

    fields = coordinator.session_fields()
    assert fields["lifetime_energy"] == 5000.0, (
        "a 404 blanked a lifetime figure that had already been read"
    )
    assert fields["total_sessions"] == 1


def _main() -> int:
    """Run every test in this module and report results."""
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]

    failures = 0
    for test in tests:
        SCHEDULED.clear()
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
