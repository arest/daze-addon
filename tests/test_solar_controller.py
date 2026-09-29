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
    _module("homeassistant.helpers.event", async_call_later=async_call_later)


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


def build(data: dict[str, Any] | None = None) -> tuple[Any, Any, Any]:
    """Build a controller wired to stubs."""
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
    controller._started_at = 0.0

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
    controller._started_at = 0.0

    controller._check_ignored_start(controller_module.time.monotonic())

    assert controller._backoff_until == 0.0


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
