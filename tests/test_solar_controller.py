"""Tests for the solar controller against a stubbed Home Assistant.

The controller is where the decision meets real sensors and a real API
client, so these cover the joins: reading the sensors, assembling the
state, honouring simulate, and not fighting the retry machinery.
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


class StubState:
    """A Home Assistant state object."""

    def __init__(
        self, state: str, attributes: dict[str, Any] | None = None
    ) -> None:
        self.state = state
        self.attributes = attributes or {}


class StubStates:
    """The subset of hass.states the controller uses."""

    def __init__(self) -> None:
        self._states: dict[str, StubState] = {}

    def set(
        self,
        entity_id: str,
        value: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Set a state.

        Carries the previous attributes forward when none are given,
        matching real Home Assistant: Entity.__async_calculate_state
        sets unit_of_measurement outside the availability branch, so
        an entity going unavailable does not drop its own unit. A stub
        that dropped it on every call let a test believe it was
        checking an unparseable value when it was actually exercising
        the unit guard instead.
        """
        if attributes is None:
            previous = self._states.get(entity_id)
            attributes = dict(previous.attributes) if previous else {}
        self._states[entity_id] = StubState(value, attributes)

    def get(self, entity_id: str) -> StubState | None:
        """Return a state, or None if unknown."""
        return self._states.get(entity_id)


class StubHass:
    """Just enough of HomeAssistant for the controller."""

    def __init__(self) -> None:
        self.states = StubStates()


# Populated by the async_call_later stub below, and cleared by any test
# that needs to observe the timer lifecycle in isolation.
SCHEDULED: list[tuple[Any, Any]] = []


def _install_stubs() -> None:
    """Register the Home Assistant modules the controller imports."""
    def _module(name: str, **attributes: Any) -> None:
        module = types.ModuleType(name)
        for key, value in attributes.items():
            setattr(module, key, value)
        sys.modules[name] = module

    def async_call_later(hass: Any, delay: Any, action: Any) -> Any:
        entry = (delay, action)
        SCHEDULED.append(entry)

        def cancel() -> None:
            if entry in SCHEDULED:
                SCHEDULED.remove(entry)

        return cancel

    _module("homeassistant")
    _module("homeassistant.core", HomeAssistant=StubHass, callback=lambda fn: fn)
    _module("homeassistant.helpers")
    _module(
        "homeassistant.helpers.event",
        async_call_later=async_call_later,
        async_track_state_change_event=lambda hass, entities, cb: (
            lambda: None
        ),
    )


_install_stubs()


def _load_package() -> None:
    """Load the integration modules the controller needs."""
    package = types.ModuleType("daze_solar_ctl")
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules["daze_solar_ctl"] = package

    for name in ("const", "payload", "optimistic", "solar"):
        spec = importlib.util.spec_from_file_location(
            f"daze_solar_ctl.{name}", PACKAGE_DIR / f"{name}.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"daze_solar_ctl.{name}"] = module
        spec.loader.exec_module(module)

    spec = importlib.util.spec_from_file_location(
        "daze_solar_ctl.solar_controller",
        PACKAGE_DIR / "solar_controller.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["daze_solar_ctl.solar_controller"] = module
    spec.loader.exec_module(module)


_load_package()

solar = sys.modules["daze_solar_ctl.solar"]
optimistic = sys.modules["daze_solar_ctl.optimistic"]
controller_module = sys.modules["daze_solar_ctl.solar_controller"]
api_module = sys.modules["daze_solar_ctl.api"]


class FakeApi:
    """Records the commands the controller issues."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def async_set_max_charging_current(
        self, serial: str, current_ma: int, attempts: int = 8
    ) -> dict:
        self.calls.append(("current", current_ma))
        return {}

    async def async_start_charge(self, serial: str, attempts: int = 8) -> dict:
        self.calls.append(("start", serial))
        return {}

    async def async_stop_charge(self, serial: str, attempts: int = 8) -> dict:
        self.calls.append(("stop", serial))
        return {}


class FakeCoordinator:
    """The coordinator surface the controller touches."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.api_client = FakeApi()
        self.serial_number = "SER1"
        self.limit_state = optimistic.OptimisticState()
        self.refresh_delays: list[int] = []
        self.background_retries: list[tuple[str, str]] = []
        self.cancelled_retries: list[str] = []

    def async_schedule_refresh_in(self, delay: int) -> None:
        self.refresh_delays.append(delay)

    def async_retry_in_background(
        self,
        key: str,
        action: Any,
        description: str,
        on_failure: Any = None,
    ) -> None:
        """Record a hand-off instead of actually retrying anything."""
        self.background_retries.append((key, description))

    def async_cancel_background_retry(self, key: str) -> None:
        """Record a cancellation instead of actually dropping one."""
        self.cancelled_retries.append(key)


CHARGING_DATA: dict[str, Any] = {
    "active": True,
    "lastAttributesUpdatedOn": None,
    "evseStatus": "charging",
    "evseState": 3,
    "instantPowerAsWatt": 3000,
    "maxExternalChargingCurrentInMilliAmps": 13000,
    "lastMaxInstallationCurrent": 32000,
    "lastACVoltageL1": 230,
    "ecoModeEnabled": False,
    "schedules": [],
    "chargeSession": {"sessionId": 1},
}

# A car plugged in but not drawing power: a session is open, but the
# charger has not been told to start.
NOT_CHARGING_DATA: dict[str, Any] = {
    "active": True,
    "lastAttributesUpdatedOn": None,
    "evseStatus": "idle",
    "evseState": 1,
    "instantPowerAsWatt": 0,
    "maxExternalChargingCurrentInMilliAmps": 13000,
    "lastMaxInstallationCurrent": 32000,
    "lastACVoltageL1": 230,
    "ecoModeEnabled": False,
    "schedules": [],
    "chargeSession": {"sessionId": 1},
}


def build(
    data: dict[str, Any] | None = None,
    supply_phases: str | None = "single",
) -> tuple[Any, Any, Any]:
    """Build a controller wired to stubs.

    Declares a single-phase supply unless a test says otherwise: that
    is the ordinary installation, and the alternatives each have a test
    of their own below.
    """
    hass = StubHass()
    hass.states.set(
        "sensor.grid_import", "0", {"unit_of_measurement": "W"}
    )
    hass.states.set(
        "sensor.grid_export", "5000", {"unit_of_measurement": "W"}
    )

    coordinator = FakeCoordinator(dict(data or CHARGING_DATA))
    controller = controller_module.SolarController(
        hass=hass,
        coordinator=coordinator,
        import_entity="sensor.grid_import",
        export_entity="sensor.grid_export",
        supply_phases=supply_phases,
    )
    return controller, coordinator, hass


def test_surplus_uses_both_sensors_and_the_car_draw() -> None:
    """3000 W drawn plus 5000 W exported is 8000 W available."""
    controller, _, _ = build()
    controller.mode = controller_module.SolarMode.SIMULATE
    asyncio.run(controller.async_tick())
    assert controller.surplus_w == 8000


def test_off_is_the_default_and_does_nothing_observable() -> None:
    """Ships off: no sensor read and no notification until opted in."""
    controller, coordinator, _ = build()
    seen: list[int] = []
    controller.add_listener(lambda: seen.append(1))

    assert controller.mode is controller_module.SolarMode.OFF

    asyncio.run(controller.async_tick())

    assert controller.surplus_w is None
    assert seen == []
    assert coordinator.api_client.calls == []


def test_simulate_decides_but_sends_nothing() -> None:
    """The default on first enable. It must be genuinely inert."""
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.SIMULATE

    asyncio.run(controller.async_tick())

    assert coordinator.api_client.calls == []
    assert controller.last_decision is not None


def test_off_does_not_even_decide() -> None:
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.OFF

    asyncio.run(controller.async_tick())

    assert coordinator.api_client.calls == []


def test_active_follows_surplus() -> None:
    """13000 mA at 230 V is about 2990 W; 8000 W of surplus should
    raise it, and the request is made in milliamps."""
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    # The first tick already commands: the fixture is already charging
    # and 8000 W clears the deadband against its ~2990 W limit
    # immediately. The second tick checks that this holds — a repeated
    # SET at the same surplus — not that the smoother needed seeding.
    asyncio.run(controller.async_tick())
    asyncio.run(controller.async_tick())

    assert any(call[0] == "current" for call in coordinator.api_client.calls)


def test_a_missing_sensor_stops_nothing() -> None:
    """Absence of information is never grounds for acting.

    Asserts what was actually *observed*, not merely what was sent:
    with this fixture's numbers, a sensor patched to read 0 instead of
    failing lands inside the deadband and sends nothing either way, so
    only checking `calls == []` cannot tell the two apart.
    """
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE
    hass.states.set(
        "sensor.grid_export", "unavailable", {"unit_of_measurement": "W"}
    )

    asyncio.run(controller.async_tick())

    assert coordinator.api_client.calls == []
    assert controller.surplus_w is None
    assert controller.last_decision is None


def test_a_pending_command_is_not_piled_on() -> None:
    """The integration already retries in the background for minutes."""
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE
    coordinator.limit_state.request(20000)

    asyncio.run(controller.async_tick())
    asyncio.run(controller.async_tick())

    assert coordinator.api_client.calls == []


def test_missing_car_draw_while_charging_skips_the_cycle() -> None:
    """instantPowerAsWatt is missing whenever the active charge session
    drops out of a single poll (see payload.merge_payload). Treating
    that as zero misreads the car's own draw as surplus that vanished,
    which can stop a car that is still charging — the exact failure
    this project has already hit once.
    """
    data = dict(CHARGING_DATA)
    del data["instantPowerAsWatt"]
    controller, coordinator, hass = build(data)
    controller.mode = controller_module.SolarMode.ACTIVE
    hass.states.set(
        "sensor.grid_import", "0", {"unit_of_measurement": "W"}
    )
    hass.states.set(
        "sensor.grid_export", "0", {"unit_of_measurement": "W"}
    )

    asyncio.run(controller.async_tick())

    assert controller.surplus_w is None
    assert controller.last_decision is None
    assert coordinator.api_client.calls == []


def test_car_draw_accepts_a_numeric_string() -> None:
    """The API is not guaranteed to report this field as a number."""
    data = dict(CHARGING_DATA)
    data["instantPowerAsWatt"] = "3000"
    controller, _, hass = build(data)
    controller.mode = controller_module.SolarMode.SIMULATE
    hass.states.set(
        "sensor.grid_import", "0", {"unit_of_measurement": "W"}
    )
    hass.states.set(
        "sensor.grid_export", "0", {"unit_of_measurement": "W"}
    )

    asyncio.run(controller.async_tick())

    assert controller.surplus_w == 3000


def test_the_reserve_lowers_the_target() -> None:
    """The house gets its share before the car does.

    Compared against an identical controller with no reserve, rather
    than asserting an exact figure: the target is also clamped to the
    charger's ceiling, so the difference is not simply the reserve.
    """
    plain, _, _ = build()
    plain.mode = controller_module.SolarMode.ACTIVE

    withheld, _, _ = build()
    withheld.mode = controller_module.SolarMode.ACTIVE
    withheld.reserve_w = 2000

    for _ in range(2):
        asyncio.run(plain.async_tick())
        asyncio.run(withheld.async_tick())

    assert plain.last_decision is not None
    assert withheld.last_decision is not None

    # The reserve must not change what surplus is, only what the car
    # is allowed to take of it.
    assert plain.surplus_w == withheld.surplus_w == 8000

    plain_target = plain.last_decision.target_watts
    withheld_target = withheld.last_decision.target_watts
    assert plain_target is not None
    assert withheld_target is not None
    assert withheld_target < plain_target


def test_listeners_are_told_after_a_tick() -> None:
    """The entities redraw from this rather than polling the object."""
    controller, _, _ = build()
    controller.mode = controller_module.SolarMode.SIMULATE
    seen: list[int] = []
    controller.add_listener(lambda: seen.append(1))

    asyncio.run(controller.async_tick())

    assert seen


def test_a_kilowatt_sensor_is_converted_to_watts() -> None:
    """4.0 kW exported is 4000 W, the same signal a W sensor would give."""
    controller, _, hass = build()
    controller.mode = controller_module.SolarMode.SIMULATE
    hass.states.set(
        "sensor.grid_export", "4.0", {"unit_of_measurement": "kW"}
    )
    hass.states.set(
        "sensor.grid_import", "0", {"unit_of_measurement": "W"}
    )

    asyncio.run(controller.async_tick())

    # 3000 W drawn (charging) plus 4000 W (4.0 kW) exported is 7000 W.
    assert controller.surplus_w == 7000


def test_an_unrecognised_unit_stops_nothing() -> None:
    """A power sensor with no known unit is treated as unreadable.

    Misreading a kW sensor as watts would understate surplus by 1000x
    and is silent and permanent for that installation, so a sensor
    whose scale is unknown must not be acted on at all.
    """
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE
    hass.states.set(
        "sensor.grid_export", "5000", {"unit_of_measurement": "lux"}
    )

    asyncio.run(controller.async_tick())

    assert coordinator.api_client.calls == []
    assert controller.surplus_w is None
    assert controller.last_decision is None


def test_a_blind_period_does_not_accrue_toward_stopping() -> None:
    """Time the sensors could not be read must not count toward the
    stop delay once they return.

    Without this, ten minutes spent unable to read the sensors reads
    as ten minutes sustained below the floor the moment they recover,
    and stops a car on the strength of a period nobody observed.
    """
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [2_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        # Already running long enough that the minimum-run gate is not
        # what is blocking the stop this test is checking.
        controller._started_at = clock[0] - solar.MIN_RUN_SECONDS - 1

        # Below the floor: the car draws exactly what is imported.
        hass.states.set(
            "sensor.grid_import", "3000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_tick())

        # Ten minutes pass with the export sensor unreadable.
        clock[0] += 600
        hass.states.set("sensor.grid_export", "unavailable")
        asyncio.run(controller.async_tick())

        # It returns, still below the floor, an instant later.
        clock[0] += 1
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_tick())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert coordinator.api_client.calls == []


def test_start_waits_for_the_confirmation_delay_then_starts() -> None:
    """The controller's own bookkeeping — not just decide() — must be
    exercised: above-threshold timing and elapsed time have to be the
    controller's real values, or this would start on the first tick or
    never start at all.
    """
    controller, coordinator, _ = build(NOT_CHARGING_DATA)
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [3_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        asyncio.run(controller.async_tick())
        assert coordinator.api_client.calls == []

        clock[0] += solar.START_DELAY_SECONDS + 1
        asyncio.run(controller.async_tick())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert any(call[0] == "start" for call in coordinator.api_client.calls)


def test_commands_this_hour_is_tracked_and_enforced() -> None:
    """If the hourly command count were not real bookkeeping, the rate
    backstop in decide() could never engage."""
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    now = controller_module.time.monotonic()
    controller._command_times = [now] * solar.MAX_COMMANDS_PER_HOUR

    asyncio.run(controller.async_tick())

    assert coordinator.api_client.calls == []


def test_no_coordinator_data_reads_as_not_reachable() -> None:
    """Before the first successful poll, an absent payload must not be
    assumed reachable merely for lack of evidence otherwise.

    charger_offline_reason itself returns None for an empty payload
    (no known reason to think it is offline), which without this
    special case would make an unpolled charger look reachable by
    luck rather than by design.

    Exercises _build_state directly rather than through a full tick:
    with an entirely empty payload, _car_draw_w's own guard (an
    unknown charging status is not evidence of zero draw) now makes
    async_tick exit even earlier, at the car-draw check, so decide()
    is never reached from a live tick for this exact case any more.
    The charger_reachable guard this test protects is still correct
    and still reachable if that earlier guard is ever relaxed, so it
    is checked at its own layer instead of one that can no longer
    reach it.
    """
    controller, coordinator, _ = build()
    coordinator.data = {}

    state = controller._build_state(5000.0, controller_module.time.monotonic())

    assert state.charger_reachable is False


def test_a_failed_command_is_handed_to_the_background_retry() -> None:
    """A stuck link must not raise out of the tick, and must be handed
    to the coordinator's own background retry rather than retried
    here."""
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    async def _rpc_failure(
        serial: str, current_ma: int, attempts: int = 8
    ) -> dict:
        raise api_module.ApiCommandRejectedError(
            "unreachable", code=api_module.COMMAND_ERROR_CODE_RPC_FAILURE
        )

    coordinator.api_client.async_set_max_charging_current = _rpc_failure

    asyncio.run(controller.async_tick())

    assert coordinator.background_retries != []


def test_a_failed_command_still_counts_against_the_hourly_backstop() -> None:
    """A command that keeps failing must still count as an attempt, or
    it retries every tick and the hourly backstop never engages."""
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    async def _rejected(
        serial: str, current_ma: int, attempts: int = 8
    ) -> dict:
        raise api_module.ApiCommandRejectedError("rejected", code=None)

    coordinator.api_client.async_set_max_charging_current = _rejected

    asyncio.run(controller.async_tick())

    assert len(controller._command_times) == 1


def test_a_queued_current_does_not_start_the_car_at_the_old_limit() -> None:
    """A current-set only queued for the background retry has not
    reached the charger yet. Starting anyway would run the car at
    whatever limit it already had — importing from the grid, the one
    outcome pure-solar mode exists to prevent — so "queued" must not
    be read as "sent".
    """
    controller, coordinator, _ = build(NOT_CHARGING_DATA)
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [4_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        asyncio.run(controller.async_tick())  # waiting to confirm
        assert coordinator.api_client.calls == []

        clock[0] += solar.START_DELAY_SECONDS + 1

        async def _rpc_failure(
            serial: str, current_ma: int, attempts: int = 8
        ) -> dict:
            raise api_module.ApiCommandRejectedError(
                "unreachable", code=api_module.COMMAND_ERROR_CODE_RPC_FAILURE
            )

        coordinator.api_client.async_set_max_charging_current = _rpc_failure
        asyncio.run(controller.async_tick())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert coordinator.background_retries != []
    assert not any(call[0] == "start" for call in coordinator.api_client.calls)


def test_solar_current_retries_share_the_manual_entities_key() -> None:
    """number.py cancels a background retry by f"{serial}:current"
    after a successful manual set (see number.py:234, 252). Solar's
    own current-setting retries must be filed under that same key, or
    a manual override does not supersede a queued solar command — it
    can land minutes later and silently undo the override.
    """
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    async def _rpc_failure(
        serial: str, current_ma: int, attempts: int = 8
    ) -> dict:
        raise api_module.ApiCommandRejectedError(
            "unreachable", code=api_module.COMMAND_ERROR_CODE_RPC_FAILURE
        )

    coordinator.api_client.async_set_max_charging_current = _rpc_failure

    asyncio.run(controller.async_tick())

    assert coordinator.background_retries != []
    key, _ = coordinator.background_retries[-1]
    assert key == f"{coordinator.serial_number}:current"


def test_solar_charge_retries_share_the_manual_switch_key() -> None:
    """Mirrors the current-setting case for start and stop: switch.py
    cancels its own manual toggle's retry under f"{serial}:charge", so
    a queued solar STOP must be filed there too.
    """
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [5_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        controller._started_at = clock[0] - solar.MIN_RUN_SECONDS - 1
        hass.states.set(
            "sensor.grid_import", "3000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_tick())  # starts the below-floor timer

        clock[0] += solar.STOP_DELAY_SECONDS + 1

        async def _rpc_failure(serial: str, attempts: int = 8) -> dict:
            raise api_module.ApiCommandRejectedError(
                "unreachable", code=api_module.COMMAND_ERROR_CODE_RPC_FAILURE
            )

        coordinator.api_client.async_stop_charge = _rpc_failure
        asyncio.run(controller.async_tick())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert coordinator.background_retries != []
    key, _ = coordinator.background_retries[-1]
    assert key == f"{coordinator.serial_number}:charge"


def test_unknown_charging_status_does_not_assume_zero_draw() -> None:
    """An unknown status is not evidence the car draws nothing; only a
    confirmed not-charging status is. Otherwise an assumed zero draw,
    computed from a payload that may be gone a moment later, still
    feeds the five-minute average for the ticks that follow it.
    """
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.SIMULATE
    coordinator.data = {}

    asyncio.run(controller.async_tick())

    assert controller.surplus_w is None


def test_a_timeout_does_not_raise_out_of_the_tick() -> None:
    """No total timeout is configured on the session, so aiohttp's own
    default eventually raises a bare TimeoutError, outside the API's
    exception hierarchy. Autonomous code needs a wider net than a
    service call a human is waiting on, or this is an unhandled task
    exception in the event loop every two minutes.
    """
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    async def _timeout(
        serial: str, current_ma: int, attempts: int = 8
    ) -> dict:
        raise asyncio.TimeoutError()

    coordinator.api_client.async_set_max_charging_current = _timeout

    asyncio.run(controller.async_tick())

    assert len(controller._command_times) == 1


def test_a_start_counts_two_attempts_not_one() -> None:
    """A START issues both a current-set and a start-charge — two real
    API calls — and each must count on its own, or a start-heavy
    failure mode burns the hourly budget at half the real rate.
    """
    controller, coordinator, _ = build(NOT_CHARGING_DATA)
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [6_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        asyncio.run(controller.async_tick())  # waiting to confirm
        clock[0] += solar.START_DELAY_SECONDS + 1
        asyncio.run(controller.async_tick())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert len(coordinator.api_client.calls) == 2
    assert len(controller._command_times) == 2


def test_async_start_schedules_and_async_stop_cancels() -> None:
    """Teardown must actually cancel the pending timer, not merely stop
    scheduling new ones from here on."""
    controller, _, _ = build()
    SCHEDULED.clear()

    asyncio.run(controller.async_start())
    assert len(SCHEDULED) == 1

    asyncio.run(controller.async_stop())
    assert SCHEDULED == []


def test_async_stop_during_an_in_flight_tick_does_not_rearm() -> None:
    """A tick already running has already cleared its own timer handle,
    so async_stop() finds nothing to cancel — it must still prevent
    the cycle re-arming itself once that tick finishes, or a stopped
    controller keeps commanding hardware every tick with no handle left
    to cancel it.
    """
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE
    SCHEDULED.clear()

    # Seed a reading so the in-flight tick actually reaches a command.
    asyncio.run(controller.async_tick())

    async def _stop_mid_command(
        serial: str, current_ma: int, attempts: int = 8
    ) -> dict:
        await controller.async_stop()
        return {}

    coordinator.api_client.async_set_max_charging_current = _stop_mid_command

    controller._schedule_tick()
    assert len(SCHEDULED) == 1
    _, action = SCHEDULED[0]

    asyncio.run(action(None))

    # No second timer was armed by the tick that was stopped mid-flight.
    assert len(SCHEDULED) == 1


def test_a_car_that_ignores_a_start_triggers_a_backoff() -> None:
    """A finished car stops drawing while surplus is still high, so a
    naive controller restarts it until sunset."""
    data = dict(CHARGING_DATA)
    data["evseStatus"] = "idle"
    data["instantPowerAsWatt"] = 0
    controller, _, _ = build(data)
    controller.mode = controller_module.SolarMode.ACTIVE

    # Pretend a start was issued a while ago and the car never drew.
    controller._start_issued_at = 0.0

    asyncio.run(controller.async_tick())

    assert controller._backoff_until > 0, "no back-off was armed"


def test_an_unknown_draw_does_not_trigger_a_backoff() -> None:
    """A missing or unparseable instantPowerAsWatt while the charger is
    mid-session is not evidence the car stopped drawing — it is
    evidence the payload dropped out, which _car_draw_w already treats
    as unknown. Backing off on that would arm an hour-long pause on a
    car that may be drawing fine.

    Called directly rather than through async_tick: a tick with an
    unknown car draw already bails out earlier, at _read_surplus, so
    the backoff check's own None-handling would otherwise never be
    exercised at all.
    """
    data = dict(CHARGING_DATA)
    del data["instantPowerAsWatt"]
    controller, _, _ = build(data)
    controller.mode = controller_module.SolarMode.SIMULATE
    controller._start_issued_at = 0.0

    controller._check_ignored_start(
        controller_module.time.monotonic(), car_connected=True
    )

    assert controller._backoff_until == 0.0


def test_the_draw_grace_period_is_honoured() -> None:
    """A car passes through the wait-for-EV state on very nearly every
    successful start — evseState 5, "session live and authorised, the
    car has not begun drawing yet" (payload.py:33-37). Arming an
    hour-long back-off before the grace period has actually elapsed
    would fire on that state on its own.
    """
    data = dict(CHARGING_DATA)
    data["evseStatus"] = "idle"
    data["instantPowerAsWatt"] = 0
    controller, _, _ = build(data)
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [12_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        controller._start_issued_at = clock[0]
        clock[0] += solar.DRAW_GRACE_SECONDS - 1
        controller._check_ignored_start(clock[0], car_connected=True)
    finally:
        controller_module.time.monotonic = original_monotonic

    assert controller._backoff_until == 0.0


def test_a_sustained_idle_start_does_not_wait_for_the_rate_limit() -> None:
    """decide() has no memory of already having started: while the
    charger stays idle with surplus sustained it returns START on
    every tick. Resetting the draw-grace clock on each of those would
    mean it never elapses, so the back-off would never arm — leaving
    the hourly rate limit, a backstop against bugs and not a
    substitute for this, to blunt the loop instead, twenty commands
    and half an hour late instead of one start and two commands.

    _carry_out also declines to resend a START while one is already
    outstanding and its grace has not elapsed (fix round 2's I4), so
    the trace is pinned at exactly one start rather than merely
    "fewer than the rate limit" — a bound loose enough that a bug
    reintroducing three or four repeat starts would still pass it.
    """
    controller, coordinator, _ = build(NOT_CHARGING_DATA)
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [13_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        asyncio.run(controller.async_tick())  # waiting to confirm
        clock[0] += solar.START_DELAY_SECONDS + 1
        asyncio.run(controller.async_tick())  # first start

        for _ in range(4):
            clock[0] += solar.TICK_SECONDS
            asyncio.run(controller.async_tick())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert controller._backoff_until > 0
    assert len(coordinator.api_client.calls) == 2
    assert coordinator.api_client.calls == [
        ("current", 21700),
        ("start", coordinator.serial_number),
    ]


def test_a_queued_start_does_not_arm_the_draw_grace_clock() -> None:
    """The current-set half of START already required a direct `True`
    send before cancelling its retry; the start-charge half must hold
    itself to the same standard for arming the draw-grace clock. A
    queued start (None) has not reached the charger, so there is
    nothing yet for the car to have ignored — and the chain backing it
    up runs out to +465s, well past the 300s grace, so treating it as
    issued would let the back-off arm while the queued start is still
    trying to land.
    """
    controller, coordinator, _ = build(NOT_CHARGING_DATA)
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [14_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        asyncio.run(controller.async_tick())  # waiting to confirm
        clock[0] += solar.START_DELAY_SECONDS + 1

        async def _rpc_failure(serial: str, attempts: int = 8) -> dict:
            raise api_module.ApiCommandRejectedError(
                "unreachable", code=api_module.COMMAND_ERROR_CODE_RPC_FAILURE
            )

        coordinator.api_client.async_start_charge = _rpc_failure
        asyncio.run(controller.async_tick())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert coordinator.background_retries != []
    assert controller._start_issued_at is None


def test_a_charge_not_issued_by_us_does_not_arm_the_backoff() -> None:
    """_started_at, the minimum-run clock, can be seeded from a charge
    already running when the controller starts (Task 10 does this at
    boot, so a healthy charge is not stopped moments after restart).
    That charge was never issued by us, so a car sitting at 0 W must
    not be judged against a start that never happened — only
    _start_issued_at, set solely by a start _carry_out itself sent,
    may arm the draw-grace back-off.
    """
    data = dict(CHARGING_DATA)
    data["instantPowerAsWatt"] = 0
    controller, _, _ = build(data)
    controller.mode = controller_module.SolarMode.ACTIVE

    # Simulate Task 10's boot-time seeding of the minimum-run clock
    # from an already-running charge, with no start of ours behind it.
    controller._started_at = 0.0
    assert controller._start_issued_at is None

    asyncio.run(controller.async_tick())

    assert controller._backoff_until == 0.0


def test_simulate_mode_does_not_check_for_an_ignored_start() -> None:
    """A dry run must not arm a real hour-long back-off from a start
    that was itself only simulated — the car was never actually told
    to charge, so treating it as ignored is not a faithful preview.
    """
    data = dict(CHARGING_DATA)
    data["evseStatus"] = "idle"
    data["instantPowerAsWatt"] = 0
    controller, _, _ = build(data)
    controller.mode = controller_module.SolarMode.SIMULATE
    controller._start_issued_at = 0.0

    asyncio.run(controller.async_tick())

    assert controller._backoff_until == 0.0


def test_an_unplugged_car_does_not_arm_the_backoff() -> None:
    """A car unplugged shortly after a solar start satisfies every
    condition the arming check looks for on its own: the session
    disappears, evseStatus reads idle, is_charge_enabled is False, and
    _car_draw_w correctly reads 0 W. But the car did not ignore the
    start, it left — arming an hour-long back-off for that reason
    would ignore forty more minutes of sun for something that never
    happened.
    """
    data = dict(CHARGING_DATA)
    data["evseStatus"] = "idle"
    data["instantPowerAsWatt"] = 0
    data["chargeSession"] = None
    controller, _, _ = build(data)
    controller.mode = controller_module.SolarMode.ACTIVE
    controller._start_issued_at = 0.0

    asyncio.run(controller.async_tick())

    assert controller._backoff_until == 0.0
    assert controller._start_issued_at is None


def test_the_backoff_mark_is_cleared_once_armed() -> None:
    """If the mark survived arming, the first tick after the back-off
    itself expires would re-read the still-idle car and arm another
    hour without ever issuing a new start — a permanent, silent,
    zero-command back-off that never charges again that day.
    """
    data = dict(CHARGING_DATA)
    data["evseStatus"] = "idle"
    data["instantPowerAsWatt"] = 0
    controller, _, _ = build(data)
    controller.mode = controller_module.SolarMode.ACTIVE
    controller._start_issued_at = 0.0

    now = controller_module.time.monotonic()
    controller._check_ignored_start(now, car_connected=True)
    assert controller._backoff_until > 0

    controller._backoff_until = 0.0  # pretend the hour has passed
    controller._check_ignored_start(now, car_connected=True)

    assert controller._backoff_until == 0.0


def test_stopping_clears_the_draw_grace_mark() -> None:
    """A mark left over from a previous start would anchor the next
    start's draw-grace clock to the wrong start: the very next tick
    could evaluate a 120-second-old start against an already-expired
    grace, and arm an hour-long back-off on the wait-for-EV state
    every start passes through.

    The mark is set only moments before the STOP-triggering tick — and
    left unset for the tick that starts the below-floor timer — so
    that its own grace has not elapsed by the time STOP is carried
    out. Otherwise _check_ignored_start's confirmed-draw clear (see
    test_a_confirmed_draw_clears_the_waiting_to_see_mark) would clear
    it first in the same tick, on this fixture's steady 3000 W draw,
    and the STOP branch's own clear would never be exercised at all.
    """
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [21_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        controller._started_at = clock[0] - solar.MIN_RUN_SECONDS - 1
        hass.states.set(
            "sensor.grid_import", "3000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_tick())  # starts the below-floor timer

        clock[0] += solar.STOP_DELAY_SECONDS + 1
        # A start issued moments before this tick, well within its own
        # grace — so _check_ignored_start itself takes no action here.
        controller._start_issued_at = clock[0] - 1
        asyncio.run(controller.async_tick())  # stops
    finally:
        controller_module.time.monotonic = original_monotonic

    assert any(call[0] == "stop" for call in coordinator.api_client.calls)
    assert controller._start_issued_at is None


def test_a_confirmed_draw_clears_the_waiting_to_see_mark() -> None:
    """A car that charged fine and later finishes must not be judged
    against the start that got it going in the first place. Once the
    car is seen drawing, the mark has to clear, or a later idle tick
    reads a stale mark and arms the back-off for a reason that already
    resolved.
    """
    controller, _, _ = build()  # CHARGING_DATA: instantPowerAsWatt=3000
    controller.mode = controller_module.SolarMode.ACTIVE
    controller._start_issued_at = 0.0

    now = controller_module.time.monotonic()
    controller._check_ignored_start(now, car_connected=True)

    assert controller._backoff_until == 0.0
    assert controller._start_issued_at is None


def test_a_mode_round_trip_clears_the_draw_grace_mark() -> None:
    """The mode setter already resets the above/below threshold timers
    on any change; a start issued before an OFF detour must not
    survive it, or the first tick back in ACTIVE evaluates a stale
    start against an already-expired grace and arms an hour-long
    back-off from a start that is an hour irrelevant.
    """
    controller, _, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE
    controller._start_issued_at = 0.0

    controller.mode = controller_module.SolarMode.OFF
    controller.mode = controller_module.SolarMode.ACTIVE

    assert controller._start_issued_at is None


def test_a_queued_set_does_not_cancel_its_own_retry() -> None:
    """A SET that fails and gets queued for the background retry must
    not immediately cancel that same retry — _send_command's None
    means only queued, not sent, so cancelling it here would discard
    the one thing still trying to apply the change.
    """
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    async def _rpc_failure(
        serial: str, current_ma: int, attempts: int = 8
    ) -> dict:
        raise api_module.ApiCommandRejectedError(
            "unreachable", code=api_module.COMMAND_ERROR_CODE_RPC_FAILURE
        )

    coordinator.api_client.async_set_max_charging_current = _rpc_failure

    asyncio.run(controller.async_tick())

    key = f"{coordinator.serial_number}:current"
    assert coordinator.background_retries != []
    assert any(k == key for k, _ in coordinator.background_retries)
    assert key not in coordinator.cancelled_retries


def test_a_successful_set_cancels_its_background_retry() -> None:
    """number.py cancels its own retry key after a successful manual
    set (number.py:233-235). Solar's direct sends share that same key
    and must do the same, or a retry queued from an earlier failure
    can land after a later, successful command and overwrite it.
    """
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    asyncio.run(controller.async_tick())

    assert any(call[0] == "current" for call in coordinator.api_client.calls)
    assert (
        f"{coordinator.serial_number}:current" in coordinator.cancelled_retries
    )


def test_a_successful_start_cancels_both_background_retries() -> None:
    """A START issues both a current-set and a start-charge; a
    successful direct send of either must cancel that key's own queued
    retry, the same as number.py and switch.py already do for their
    own manual commands.
    """
    controller, coordinator, _ = build(NOT_CHARGING_DATA)
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [7_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        asyncio.run(controller.async_tick())  # waiting to confirm
        clock[0] += solar.START_DELAY_SECONDS + 1
        asyncio.run(controller.async_tick())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert any(call[0] == "start" for call in coordinator.api_client.calls)
    assert (
        f"{coordinator.serial_number}:current" in coordinator.cancelled_retries
    )
    assert (
        f"{coordinator.serial_number}:charge" in coordinator.cancelled_retries
    )


def test_a_successful_stop_cancels_its_background_retry() -> None:
    """Mirrors switch.py's own cancel after a successful manual stop
    (switch.py:218-220): a solar-issued STOP that reaches the charger
    must cancel any retry still queued under the same charge key.
    """
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [8_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        controller._started_at = clock[0] - solar.MIN_RUN_SECONDS - 1
        hass.states.set(
            "sensor.grid_import", "3000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_tick())  # starts the below-floor timer

        clock[0] += solar.STOP_DELAY_SECONDS + 1
        asyncio.run(controller.async_tick())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert any(call[0] == "stop" for call in coordinator.api_client.calls)
    assert (
        f"{coordinator.serial_number}:charge" in coordinator.cancelled_retries
    )


def test_disarming_clears_the_clocks_a_rearm_would_misread() -> None:
    """Disarming ends the episode, not just the mode.

    A start this controller issued, and the back-off that start could
    still arm, must not survive into the next time solar control is
    switched on. Left behind, a start issued at noon and abandoned at
    12:01 is judged at 14:00 against a car that has long since
    finished, arming a 60-minute back-off for a start nobody is
    waiting on.

    _collapsed_since must go with it for the same reason, and the cost
    of missing it is worse: disarmed during a collapse and re-armed an
    hour later with the raw reading still below the floor, a surviving
    anchor lets _track_thresholds set _below_since an hour in the past,
    seconds_below_threshold is already past the 600s stop delay, and
    the very first tick issues an immediate STOP on a charge the user
    just re-armed solar control to manage.
    """
    controller, _, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE
    controller._start_issued_at = 100.0
    controller._backoff_until = 1e9
    controller._started_at = 100.0
    controller._collapsed_since = 100.0

    controller.disarm("the charging limit was set manually")

    assert controller.mode is controller_module.SolarMode.OFF
    assert controller._start_issued_at is None
    assert controller._backoff_until == 0.0
    assert controller._started_at is None
    assert controller._collapsed_since is None


def test_disarming_clears_the_threshold_timers_too() -> None:
    """The above/below-floor clocks must not survive a disarm either.

    _track_thresholds only ever sets these from None, never restarts
    them while already running, so a stale _above_since left behind by
    a skipped clear would read as "surplus has been sufficient since
    before the disarm" the moment solar control is re-armed — skipping
    the confirmation delay the design requires before the first start.
    """
    controller, _, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE
    controller._above_since = 50.0
    controller._below_since = 60.0

    controller.disarm("the charging limit was set manually")

    assert controller._above_since is None
    assert controller._below_since is None


def test_an_unplug_inside_the_grace_does_not_survive_to_punish_a_reconnect() -> (
    None
):
    """The guard order in _check_ignored_start matters, and the
    existing suite cannot tell the orderings apart: its one test that
    exercises the disconnected-car guard sets _start_issued_at to a
    timestamp whose grace has already long expired, so it passes
    whichever guard runs first.

    Misplaced (grace checked before car_connected): a car unplugged at
    T+60, still inside the 300 s grace, hits the grace guard first and
    returns without clearing _start_issued_at. The mark survives. A
    different car reconnecting later is then declined a start by
    _carry_out's own outstanding-start guard, and once grace+30s
    arrives with the stale mark still set and the reconnected car not
    yet drawing, an hour-long back-off arms — punishing the new car for
    the departed one's start.

    Correct (car_connected checked before grace): the unplug at T+60
    clears the mark immediately regardless of how little of the grace
    has elapsed, so there is nothing left for grace+30s to arm.
    """
    controller, _, _ = build(NOT_CHARGING_DATA)
    controller.mode = controller_module.SolarMode.ACTIVE

    start = 15_000_000.0
    controller._start_issued_at = start

    # The car unplugs 60s in — well inside the 300s grace.
    controller._check_ignored_start(start + 60, car_connected=False)
    assert controller._start_issued_at is None

    # A different car reconnects; with the mark already clear this is
    # a no-op either way.
    controller._check_ignored_start(start + 120, car_connected=True)

    # 30s past where the original start's grace would have elapsed.
    controller._check_ignored_start(
        start + solar.DRAW_GRACE_SECONDS + 30, car_connected=True
    )

    assert controller._backoff_until == 0.0


def test_a_collapse_starts_the_stop_clock_when_it_happens() -> None:
    """The ten-minute stop delay must run from the collapse, not from
    whenever the fast path's own evaluation actually gets around to it.

    The sensor event lands well inside the fast path's own one-tick
    spacing guard (10 s after the last evaluation, against a 120 s
    guard), so it records the collapse but defers evaluating it; the
    mark is only picked up by an ordinary tick a full TICK_SECONDS
    later. An implementation that anchored the stop clock to whichever
    "now" happened to be running at evaluation time, rather than to
    the collapse itself, would stamp it with that later tick instead —
    a difference this test can see only because the two are forced
    apart by more than a spacing guard's width. A collapse observed and
    evaluated in the same instant cannot tell these two apart, which is
    why that shape is deliberately avoided here.

    Asserting the mark itself rather than "a stop was sent": no stop
    can be sent at the moment of a collapse — the smoothed figure is
    still healthy, which is the whole reason this path exists — so a
    test that looked for a command would pass against an
    implementation that did nothing at all.
    """
    controller, _, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [20_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        controller._started_at = clock[0] - solar.MIN_RUN_SECONDS - 1

        # A healthy reading, so the average is not already near the
        # floor and the fast path's own smoothed-figure check does not
        # short-circuit before the spacing guard is even reached.
        asyncio.run(controller.async_tick())

        # Well inside the fast path's spacing guard: the collapse is
        # recorded, but the evaluation it would otherwise trigger is
        # deferred to the next ordinary tick.
        clock[0] += 10
        collapse_at = clock[0]
        hass.states.set(
            "sensor.grid_import", "4000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_sensor_changed())

        # The ordinary tick that actually evaluates the collapse,
        # a full tick's width after it happened.
        clock[0] += solar.TICK_SECONDS
        asyncio.run(controller.async_tick())

        assert controller._below_since == collapse_at, (
            "the stop clock did not start at the collapse: "
            f"{controller._below_since} instead of {collapse_at}"
        )
    finally:
        controller_module.time.monotonic = original_monotonic


def test_a_collapse_is_evaluated_once_not_on_every_sensor_update() -> None:
    """A grid sensor reporting every ten seconds updates six times a
    minute, and the raw reading stays below the floor for as long as
    the average takes to catch up. Without a latch each of those
    updates runs a full evaluation, and each can rewrite the limit:
    the twenty-command hourly backstop is spent in minutes, and it is
    then not there for the stop when the stop finally comes.

    Each repeat update here is spaced a tick-and-a-bit apart — wider
    than the fast path's own one-tick minimum-spacing guard — so that
    guard alone would permit a fresh evaluation every time. Only the
    latch (armed once per collapse, cleared solely on recovery) can be
    what holds the command count flat across them; a version with the
    spacing guard but no latch would still pass a run of updates packed
    inside one tick's width, which is why none are here.
    """
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [21_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        controller._started_at = clock[0] - solar.MIN_RUN_SECONDS - 1
        asyncio.run(controller.async_tick())

        clock[0] += solar.TICK_SECONDS + 1
        hass.states.set(
            "sensor.grid_import", "4000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_sensor_changed())

        after_first = len(coordinator.api_client.calls)

        # The sensor keeps reporting the same collapsed figures, each
        # update further apart than the fast path's own spacing guard —
        # so only the latch, not that guard, can be holding this flat.
        for _ in range(3):
            clock[0] += solar.TICK_SECONDS + 5
            asyncio.run(controller.async_sensor_changed())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert len(coordinator.api_client.calls) == after_first, (
        "the fast path fired again while the same collapse was still "
        "being counted"
    )


def test_a_recovery_re_arms_the_fast_path() -> None:
    """A kettle is not a collapse.

    When the raw reading comes back above the floor the stop clock must
    let go of it. Otherwise a dozen three-kilowatt kitchen dips over an
    afternoon add up to ten minutes "below the floor" and stop a charge
    that never wanted for surplus.
    """
    controller, _, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [22_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        controller._started_at = clock[0] - solar.MIN_RUN_SECONDS - 1
        asyncio.run(controller.async_tick())

        clock[0] += solar.TICK_SECONDS + 1
        hass.states.set(
            "sensor.grid_import", "4000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_sensor_changed())
        assert controller._below_since is not None

        # The kettle switches off.
        clock[0] += 30
        hass.states.set(
            "sensor.grid_import", "0", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "5000", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_sensor_changed())
        assert controller._collapsed_since is None

        clock[0] += solar.TICK_SECONDS
        asyncio.run(controller.async_tick())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert controller._below_since is None, (
        "the stop clock is still anchored to a collapse that recovered"
    )


def test_a_rise_does_not_trigger_an_immediate_evaluation() -> None:
    """Otherwise every sensor update rewrites the charger's limit."""
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    for _ in range(3):
        asyncio.run(controller.async_tick())

    before = len(coordinator.api_client.calls)
    hass.states.set("sensor.grid_export", "9000")

    asyncio.run(controller.async_sensor_changed())

    assert len(coordinator.api_client.calls) == before


def test_a_sensor_event_during_a_tick_does_not_start_a_second_one() -> None:
    """async_tick has two callers now, and an API call is an await.

    A sensor event arriving while a tick waits on the charger would
    otherwise run a second evaluation against the same coordinator
    data: both append to _command_times, both reach the same branch,
    and both send the same command. The clock is advanced past the
    minimum spacing inside the call on purpose, so that only the
    re-entrancy guard can be what stops it.
    """
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [23_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    reentered: list[int] = []

    async def _set_current_then_collapse(
        serial: str, current_ma: int, attempts: int = 8
    ) -> dict:
        coordinator.api_client.calls.append(("current", current_ma))
        hass.states.set(
            "sensor.grid_import", "4000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        clock[0] += solar.TICK_SECONDS + 1
        await controller.async_sensor_changed()
        reentered.append(1)
        return {}

    coordinator.api_client.async_set_max_charging_current = (
        _set_current_then_collapse
    )

    try:
        asyncio.run(controller.async_tick())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert reentered == [1], "the sensor event never arrived mid-tick"
    assert len(coordinator.api_client.calls) == 1, (
        "a second evaluation ran inside the first and commanded again"
    )


def test_async_start_still_clears_the_stopped_flag() -> None:
    """async_start is rewritten in this task, and the flag it sets is
    easy to drop on the way past: no other test builds a controller,
    stops it and starts it again, so nothing else would notice.
    """
    controller, _, _ = build()
    SCHEDULED.clear()

    asyncio.run(controller.async_stop())
    assert controller._stopped is True

    asyncio.run(controller.async_start())

    assert controller._stopped is False
    assert len(SCHEDULED) == 1


def test_async_stop_cancels_the_sensor_listener() -> None:
    """async_stop must cancel the sensor-change subscription itself,
    not only the tick timer — otherwise a stopped controller goes on
    reacting to every sensor update indefinitely, the tick's own
    _stopped re-arm check notwithstanding.
    """
    controller, _, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE
    SCHEDULED.clear()

    asyncio.run(controller.async_start())

    cancelled: list[bool] = []
    controller._cancel_listener = lambda: cancelled.append(True)

    asyncio.run(controller.async_stop())

    assert cancelled == [True], "async_stop did not cancel the listener"
    assert controller._cancel_listener is None


def test_a_sensor_event_after_stop_does_not_evaluate() -> None:
    """async_stop cancels the sensor subscription, but not atomically
    with setting _stopped — an event already dispatched can still land
    here in the gap. async_sensor_changed must honour _stopped itself,
    mirroring _schedule_tick's own re-arm check, or that race runs a
    full evaluation — and can issue a command — on a controller that
    believes it has been torn down.

    The clock is advanced past the fast path's own one-tick spacing
    guard before the event arrives, so nothing but the _stopped check
    can be what prevents the evaluation this test looks for.
    """
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [26_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        asyncio.run(controller.async_tick())
        before = len(coordinator.api_client.calls)

        asyncio.run(controller.async_stop())
        assert controller._stopped is True

        clock[0] += solar.TICK_SECONDS + 1
        hass.states.set(
            "sensor.grid_import", "4000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_sensor_changed())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert len(coordinator.api_client.calls) == before, (
        "a sensor event evaluated and commanded after the controller "
        "was stopped"
    )
    assert controller._collapsed_since is None, (
        "the anchor was set on a torn-down controller"
    )


def test_a_fresh_collapse_within_one_tick_still_waits_for_the_spacing_guard() -> (
    None
):
    """The latch stops a *sustained* collapse from re-evaluating on
    every sensor update, but it is cleared the instant surplus
    recovers — so it cannot protect against a raw reading oscillating
    across the floor faster than one tick. Each down-crossing there is
    a fresh collapse, latched and re-armed in the same breath, and only
    the minimum-spacing guard is left to stop each one running a full
    evaluation — the brief's own "six evaluations a minute and the
    hourly backstop spent in about three".

    Both the recovery and the second collapse land well inside one
    tick of the first evaluation, so only the spacing guard — not the
    latch, which the recovery has already cleared — can be what holds
    the command count flat across them.
    """
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [27_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        controller._started_at = clock[0] - solar.MIN_RUN_SECONDS - 1
        asyncio.run(controller.async_tick())

        clock[0] += solar.TICK_SECONDS + 1
        hass.states.set(
            "sensor.grid_import", "4000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_sensor_changed())  # evaluates now

        after_first = len(coordinator.api_client.calls)

        # A kettle-fast oscillation: recovers, then collapses again,
        # both well inside the one tick the spacing guard enforces.
        clock[0] += 5
        hass.states.set(
            "sensor.grid_import", "0", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "5000", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_sensor_changed())
        assert controller._collapsed_since is None, "did not re-arm"

        clock[0] += 5
        hass.states.set(
            "sensor.grid_import", "4000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_sensor_changed())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert len(coordinator.api_client.calls) == after_first, (
        "a fresh collapse inside one tick evaluated again, unguarded "
        "by the minimum-spacing check"
    )


def test_a_tick_only_recovery_clears_the_collapse_anchor() -> None:
    """_track_thresholds must clear _collapsed_since itself once the
    raw reading recovers, not rely on async_sensor_changed to have
    done it first: a recovery observed only by an ordinary tick, with
    no sensor event in between, must still let go of the anchor.

    The consequence of losing this is worse than a wrong stop delay:
    with _collapsed_since stuck non-None, "available >= floor_w and
    self._collapsed_since is None" can never be taken again,
    _above_since is reset to None on every evaluation instead,
    seconds_above_threshold never reaches the start delay, and the
    controller can never start a charge again for as long as the
    process runs.
    """
    controller, _, hass = build(NOT_CHARGING_DATA)
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [28_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        hass.states.set(
            "sensor.grid_import", "4000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_tick())  # collapses, through a tick
        assert controller._collapsed_since is not None

        hass.states.set(
            "sensor.grid_import", "0", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "5000", {"unit_of_measurement": "W"}
        )
        clock[0] += solar.TICK_SECONDS
        asyncio.run(controller.async_tick())  # recovers, through a tick

        assert controller._collapsed_since is None, (
            "the anchor survived a recovery observed only by a tick"
        )
        assert controller._above_since is not None, (
            "above-threshold timing never resumed after a tick-only "
            "recovery"
        )
    finally:
        controller_module.time.monotonic = original_monotonic


def test_a_blind_tick_does_not_reset_the_fast_paths_spacing_clock() -> None:
    """_last_evaluation is the fast path's own spacing clock. A tick
    that could not read its sensors observed nothing, so it must not
    reset that clock anyway — doing so defers a genuine collapse a
    full tick on the strength of a cycle that never actually looked.
    """
    controller, _, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [29_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        asyncio.run(controller.async_tick())  # a real evaluation

        # Just past one tick's width: a real collapse landing here
        # should be free to evaluate immediately, not deferred on the
        # strength of the blind tick that follows.
        clock[0] += solar.TICK_SECONDS + 1
        blind_at = clock[0]
        hass.states.set("sensor.grid_export", "unavailable")
        asyncio.run(controller.async_tick())  # observes nothing

        clock[0] += 1
        collapse_at = clock[0]
        hass.states.set(
            "sensor.grid_import", "4000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_sensor_changed())

        assert controller._last_evaluation != blind_at, (
            "the blind tick's own timestamp reset the spacing clock"
        )
        assert controller._last_evaluation == collapse_at, (
            "a collapse just past one tick's width was deferred anyway"
        )
    finally:
        controller_module.time.monotonic = original_monotonic


def test_an_undeclared_supply_refuses_to_run() -> None:
    """The Daze payload cannot tell us how many phases feed the house,
    so the user is asked. Until they answer, an unanswered question is
    not evidence of a single-phase supply: guessing wrong loads one
    phase with the whole of a netted three-phase surplus.
    """
    controller, _, _ = build(supply_phases=None)

    assert controller.unsupported_reason is not None
    assert "phase" in controller.unsupported_reason


def test_three_phase_supply_with_a_single_phase_charger_is_refused() -> None:
    """Grid meters usually report net across phases, so the surplus
    can exist mostly on phases the charger cannot reach."""
    data = dict(CHARGING_DATA)
    data["evseIsThreePhase"] = False
    controller, _, _ = build(data, supply_phases="three")

    assert controller.unsupported_reason is not None
    assert "phase" in controller.unsupported_reason


def test_a_matched_single_phase_pair_is_supported() -> None:
    data = dict(CHARGING_DATA)
    data["evseIsThreePhase"] = False
    controller, _, _ = build(data, supply_phases="single")

    assert controller.unsupported_reason is None


def test_a_three_phase_charger_on_a_three_phase_supply_is_supported() -> None:
    """The refusal is about the mismatch, not about three phases."""
    data = dict(CHARGING_DATA)
    data["evseIsThreePhase"] = True
    controller, _, _ = build(data, supply_phases="three")

    assert controller.unsupported_reason is None


def test_eco_mode_refuses_to_arm() -> None:
    """The spec asks for this three times: the charger's own eco mode
    is controlling it, so solar control stands down and says so rather
    than quietly deciding nothing every two minutes for ever.
    """
    data = dict(CHARGING_DATA)
    data["ecoModeEnabled"] = True
    controller, _, _ = build(data)

    assert controller.unsupported_reason is not None
    assert "eco" in controller.unsupported_reason


def test_a_charger_schedule_refuses_to_arm() -> None:
    data = dict(CHARGING_DATA)
    data["schedules"] = [{"id": 1}]
    controller, _, _ = build(data)

    assert controller.unsupported_reason is not None
    assert "schedule" in controller.unsupported_reason


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
