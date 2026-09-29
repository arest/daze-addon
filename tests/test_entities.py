"""Execute the real entity classes against a stubbed Home Assistant.

The optimistic display logic lives in number.py, select.py and
switch.py, which import Home Assistant and so had never been run by any
test. That is precisely where the "I changed it and nothing happened"
bug lived, so it is worth exercising directly.

Home Assistant is replaced with the smallest stubs the entities
actually use. The integration modules themselves are the real ones.

Run with pytest, or standalone:

    python3 tests/test_entities.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, ClassVar

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "daze"


# ------------------------------------------------------------------
# Home Assistant stubs
# ------------------------------------------------------------------


class StubCoordinatorEntity:
    """Stand-in for CoordinatorEntity.

    The real class is generic, so subscripting has to work.
    """

    def __class_getitem__(cls, _item: Any) -> Any:
        """Support CoordinatorEntity[DazeDataUpdateCoordinator]."""
        return cls

    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator
        self.hass = object()
        self.state_writes = 0
        self.removers: list[Any] = []

    def async_write_ha_state(self) -> None:
        """Count frontend updates instead of performing one."""
        self.state_writes += 1

    def _handle_coordinator_update(self) -> None:
        """Base implementation does nothing here."""
        self.state_writes += 1

    async def async_added_to_hass(self) -> None:
        """Base implementation does nothing here."""

    def async_on_remove(self, remove: Any) -> None:
        """Record a teardown callback."""
        self.removers.append(remove)


class StubDataUpdateCoordinator:
    """Stand-in for DataUpdateCoordinator.

    The real class is generic, so subscripting has to work.
    """

    def __class_getitem__(cls, _item: Any) -> Any:
        """Support DataUpdateCoordinator[DazeCoordinatorData]."""
        return cls

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.data: dict[str, Any] | None = None
        self.hass = object()
        self.update_interval = None

    async def async_request_refresh(self) -> None:
        """No-op refresh."""


def _module(name: str, **attributes: Any) -> types.ModuleType:
    """Build a stub module with the given attributes."""
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def install_homeassistant_stubs() -> list[tuple[Any, Any, Any]]:
    """Register the Home Assistant modules the entities import.

    Returns:
        The list of scheduled callbacks, so tests can fire them.

    """
    scheduled: list[tuple[Any, Any, Any]] = []

    def async_call_later(hass: Any, delay: Any, action: Any) -> Any:
        """Record a scheduled callback and return a canceller."""
        entry = (hass, delay, action)
        scheduled.append(entry)

        def cancel() -> None:
            if entry in scheduled:
                scheduled.remove(entry)

        return cancel

    notifications: list[dict[str, Any]] = []

    def async_create(
        hass: Any, message: str, title: str = "", notification_id: str = ""
    ) -> None:
        """Record a notification."""
        notifications.append({"message": message, "title": title})

    _module("homeassistant")
    _module("homeassistant.components")
    _module(
        "homeassistant.components.persistent_notification",
        async_create=async_create,
        _records=notifications,
    )
    _module("homeassistant.components.number", NumberEntity=object)
    _module("homeassistant.components.select", SelectEntity=object)
    _module("homeassistant.components.switch", SwitchEntity=object)
    _module(
        "homeassistant.components.sensor",
        SensorDeviceClass=type("SensorDeviceClass", (), {}),
        SensorEntity=object,
        SensorEntityDescription=object,
        SensorStateClass=type("SensorStateClass", (), {}),
    )
    _module(
        "homeassistant.const",
        EntityCategory=type("EntityCategory", (), {"CONFIG": "config"}),
        UnitOfElectricCurrent=type(
            "UnitOfElectricCurrent", (), {"MILLIAMPERE": "mA"}
        ),
        UnitOfPower=type("UnitOfPower", (), {"WATT": "W"}),
    )
    _module(
        "homeassistant.core",
        callback=lambda fn: fn,
        HomeAssistant=object,
    )
    _module("homeassistant.config_entries", ConfigEntry=object)
    _module(
        "homeassistant.exceptions",
        ConfigEntryAuthFailed=type(
            "ConfigEntryAuthFailed", (Exception,), {}
        ),
        HomeAssistantError=type("HomeAssistantError", (Exception,), {}),
    )
    _module("homeassistant.helpers")
    _module(
        "homeassistant.helpers.aiohttp_client",
        async_get_clientsession=lambda hass: None,
    )
    _module(
        "homeassistant.helpers.event",
        async_call_later=async_call_later,
        async_track_state_change_event=lambda hass, entities, cb: (
            lambda: None
        ),
    )
    _module(
        "homeassistant.helpers.device_registry",
        DeviceInfo=dict,
        async_get=lambda hass: None,
    )
    _module(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=StubCoordinatorEntity,
        DataUpdateCoordinator=StubDataUpdateCoordinator,
        UpdateFailed=type("UpdateFailed", (Exception,), {}),
    )
    _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    _module(
        "homeassistant.helpers.restore_state", RestoreEntity=object
    )
    _module("homeassistant.helpers.config_validation", positive_int=int)

    return scheduled


SCHEDULED = install_homeassistant_stubs()


def _load_package() -> types.ModuleType:
    """Load the integration package without running its __init__."""
    package = types.ModuleType("daze_entities_under_test")
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules["daze_entities_under_test"] = package

    for name in ("const", "payload", "models"):
        spec = importlib.util.spec_from_file_location(
            f"daze_entities_under_test.{name}", PACKAGE_DIR / f"{name}.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"daze_entities_under_test.{name}"] = module
        spec.loader.exec_module(module)

    spec = importlib.util.spec_from_file_location(
        "daze_entities_under_test.api",
        PACKAGE_DIR / "api" / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR / "api")],
    )
    assert spec and spec.loader
    api_module = importlib.util.module_from_spec(spec)
    sys.modules["daze_entities_under_test.api"] = api_module
    spec.loader.exec_module(api_module)

    for name in ("coordinator", "number", "select", "switch"):
        spec = importlib.util.spec_from_file_location(
            f"daze_entities_under_test.{name}", PACKAGE_DIR / f"{name}.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"daze_entities_under_test.{name}"] = module
        spec.loader.exec_module(module)

    return package


_load_package()

from homeassistant.exceptions import HomeAssistantError

api = sys.modules["daze_entities_under_test.api"]
number_module = sys.modules["daze_entities_under_test.number"]
select_module = sys.modules["daze_entities_under_test.select"]
optimistic_module = sys.modules["daze_entities_under_test.optimistic"]
notifications = sys.modules[
    "homeassistant.components.persistent_notification"
]._records


# ------------------------------------------------------------------
# Test doubles
# ------------------------------------------------------------------


class FakeCoordinator(StubDataUpdateCoordinator):
    """Records what the entity asks the coordinator to do."""

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__()
        self.data = data
        self.refresh_delays: list[int] = []
        self.background: list[dict[str, Any]] = []
        self.limit_state = optimistic_module.OptimisticState()
        self.limit_listeners: list[Any] = []
        # Mirrors the real coordinator, which declares this so entities
        # and services can read it without getattr.
        self.solar_controller: Any = None

    def async_add_limit_listener(self, listener: Any) -> Any:
        """Register a redraw callback."""
        self.limit_listeners.append(listener)
        return lambda: self.limit_listeners.remove(listener)

    def async_notify_limit_listeners(self) -> None:
        """Redraw every registered view."""
        for listener in list(self.limit_listeners):
            listener()

    def async_schedule_refresh_in(self, delay: int) -> None:
        """Record a delayed refresh."""
        self.refresh_delays.append(delay)

    def async_schedule_settle_refresh(self) -> None:
        """Record a settle refresh."""
        self.refresh_delays.append(-1)

    def async_retry_in_background(
        self, key: str, action: Any, description: str, on_failure: Any = None
    ) -> None:
        """Record a background retry request."""
        self.background.append(
            {
                "key": key,
                "action": action,
                "description": description,
                "on_failure": on_failure,
            }
        )

    def async_cancel_background_retry(self, key: str) -> None:
        """Drop a recorded retry."""
        self.background = [b for b in self.background if b["key"] != key]


class FakeApi:
    """Records command calls and can be told to fail."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, Any]] = []

    async def async_set_max_charging_current(
        self, serial: str, current_ma: int, attempts: int = 8
    ) -> dict:
        """Record and optionally fail."""
        self.calls.append(("current", current_ma))
        if self.error is not None:
            raise self.error
        return {}

    async def async_set_eco_mode(
        self, serial: str, eco_mode_enabled: bool, attempts: int = 8
    ) -> dict:
        """Record and optionally fail."""
        self.calls.append(("eco", eco_mode_enabled))
        if self.error is not None:
            raise self.error
        return {}

    async def async_start_charge(
        self, serial: str, attempts: int = 8
    ) -> dict:
        """Record and optionally fail."""
        self.calls.append(("start", serial))
        if self.error is not None:
            raise self.error
        return {}

    async def async_stop_charge(
        self, serial: str, attempts: int = 8
    ) -> dict:
        """Record and optionally fail."""
        self.calls.append(("stop", serial))
        if self.error is not None:
            raise self.error
        return {}


BASE_DATA: dict[str, Any] = {
    "maxExternalChargingCurrentInMilliAmps": 6521,
    "lastMaxInstallationCurrent": 32000,
    "lastACVoltageL1": 232,
    "ecoModeEnabled": False,
}


def make_number(
    data: dict[str, Any] | None = None, error: Exception | None = None
) -> tuple[Any, FakeCoordinator, FakeApi]:
    """Build a number entity wired to fakes."""
    coordinator = FakeCoordinator(dict(data or BASE_DATA))
    client = FakeApi(error)
    entity = number_module.DazeWallboxNumberEntity(
        coordinator=coordinator,
        api_client=client,
        serial_number="SER1",
        device_info={},
    )
    return entity, coordinator, client


def rpc_failure() -> Exception:
    """Return the error the API raises when the link is down."""
    return api.ApiCommandRejectedError("unreachable", code=101)


def refused() -> Exception:
    """Return the error the API raises for an invalid value."""
    return api.ApiCommandRejectedError("out of range", code=369)


# ------------------------------------------------------------------
# The reported bug: changing the value appeared to do nothing
# ------------------------------------------------------------------


def test_new_current_is_shown_immediately_on_success() -> None:
    """This is the bug: the slider snapped back to the old value.

    The charger still reports 6521 until it adopts the change, so
    reading the coordinator would revert the display.
    """
    entity, coordinator, client = make_number()

    assert entity.native_value == 6521

    asyncio.run(entity.async_set_native_value(16000))

    assert client.calls == [("current", 16000)]
    assert entity.native_value == 16000, "slider reverted to the old value"
    assert coordinator.data["maxExternalChargingCurrentInMilliAmps"] == 6521


def test_new_current_is_shown_while_a_retry_runs() -> None:
    """An unreachable charger must not look like a no-op either.

    Since the background retry returns without raising, nothing else
    would tell the user their change is still pending.
    """
    notifications.clear()
    entity, coordinator, _ = make_number(error=rpc_failure())

    asyncio.run(entity.async_set_native_value(16000))

    assert len(coordinator.background) == 1
    assert entity.native_value == 16000
    assert not notifications, "an in-flight retry must not raise an error"


def test_display_returns_to_reality_once_the_charger_agrees() -> None:
    """Holding the guess longer would mask later external changes."""
    entity, coordinator, _ = make_number()

    asyncio.run(entity.async_set_native_value(16000))
    assert entity.native_value == 16000

    coordinator.data["maxExternalChargingCurrentInMilliAmps"] = 16000
    entity._handle_coordinator_update()

    assert coordinator.limit_state.pending is False
    assert entity.native_value == 16000


def test_display_is_dropped_when_every_retry_fails() -> None:
    """A change that never landed must not be shown indefinitely."""
    notifications.clear()
    entity, coordinator, _ = make_number(error=rpc_failure())

    asyncio.run(entity.async_set_native_value(16000))
    assert entity.native_value == 16000

    # Simulate the coordinator exhausting its background attempts.
    coordinator.background[0]["on_failure"]("could not be delivered")

    assert entity.native_value == 6521
    assert len(notifications) == 1
    assert "could not be delivered" in notifications[0]["message"]


def test_a_refusal_is_reported_rather_than_retried() -> None:
    """Out of range is final: retrying it wastes minutes."""
    notifications.clear()
    entity, coordinator, _ = make_number(error=refused())

    asyncio.run(entity.async_set_native_value(32000))

    assert coordinator.background == []
    assert len(notifications) == 1
    assert entity.native_value == 6521


def test_setting_the_same_value_sends_nothing() -> None:
    """Re-selecting the current value must not hit the API."""
    entity, _, client = make_number()

    asyncio.run(entity.async_set_native_value(6521))

    assert client.calls == []


def test_a_refresh_is_scheduled_rather_than_run_immediately() -> None:
    """Refreshing at once reads the state from before the change."""
    entity, coordinator, _ = make_number()

    asyncio.run(entity.async_set_native_value(16000))

    assert coordinator.refresh_delays == [10]


def test_bounds_come_from_the_charger() -> None:
    """The floor follows the 1500 W minimum at the measured voltage."""
    entity, _, _ = make_number()

    assert entity.native_min_value == 6500
    assert entity.native_max_value == 32000


# ------------------------------------------------------------------
# The same behaviour on the mode selector
# ------------------------------------------------------------------


def make_select(
    error: Exception | None = None,
) -> tuple[Any, FakeCoordinator, FakeApi]:
    """Build a select entity wired to fakes."""
    coordinator = FakeCoordinator(dict(BASE_DATA))
    client = FakeApi(error)
    entity = select_module.DazeWallboxSelectEntity(
        coordinator=coordinator,
        api_client=client,
        serial_number="SER1",
        device_info={},
    )
    return entity, coordinator, client


def test_new_mode_is_shown_immediately() -> None:
    """The selector reverted for the same reason the slider did."""
    entity, coordinator, client = make_select()

    assert entity.current_option == "fast"

    asyncio.run(entity.async_select_option("eco"))

    assert client.calls == [("eco", True)]
    assert entity.current_option == "eco"
    assert coordinator.data["ecoModeEnabled"] is False


def test_new_mode_is_held_while_a_retry_runs() -> None:
    """An unreachable charger must not revert the selection."""
    entity, coordinator, _ = make_select(error=rpc_failure())

    asyncio.run(entity.async_select_option("eco"))

    assert len(coordinator.background) == 1
    assert entity.current_option == "eco"



# ------------------------------------------------------------------
# The power view of the same setting
# ------------------------------------------------------------------


POWER_DATA: dict[str, Any] = {
    "maxExternalChargingCurrentInMilliAmps": 6521,
    "lastMaxInstallationCurrent": 32000,
    "lastACVoltageL1": 236,
}


def make_power(
    error: Exception | None = None,
) -> tuple[Any, FakeCoordinator, FakeApi]:
    """Build a power entity wired to fakes."""
    coordinator = FakeCoordinator(dict(POWER_DATA))
    client = FakeApi(error)
    entity = number_module.DazeWallboxPowerEntity(
        coordinator=coordinator,
        api_client=client,
        serial_number="SER1",
        device_info={},
    )
    return entity, coordinator, client


def test_power_entity_reports_the_limit_in_watts() -> None:
    """6521 mA at 236 V is about 1539 W."""
    entity, _, _ = make_power()
    assert entity.native_value == 1539


def test_setting_power_sends_the_converted_current() -> None:
    """Reproduces the change verified against the charger.

    Asking for 4000 W at 236 V sent 16900 mA, which was accepted and
    read back unchanged.
    """
    entity, _, client = make_power()

    asyncio.run(entity.async_set_native_value(4000))

    assert client.calls == [("current", 16900)]
    assert entity.native_value == 3988


def test_power_entity_shows_the_request_immediately() -> None:
    """Same display rule as the current entity."""
    entity, coordinator, _ = make_power()

    asyncio.run(entity.async_set_native_value(4000))

    # The charger still reports the old current.
    assert coordinator.data["maxExternalChargingCurrentInMilliAmps"] == 6521
    assert entity.native_value == 3988


def test_power_entity_bounds_come_from_the_charger() -> None:
    """1.5 kW floor and the installation rating, at 236 V."""
    entity, _, _ = make_power()

    assert entity.native_min_value == 1600
    assert entity.native_max_value == 7500


def test_power_entity_retries_an_unreachable_charger() -> None:
    """Shares the background retry with the current entity."""
    notifications.clear()
    entity, coordinator, _ = make_power(error=rpc_failure())

    asyncio.run(entity.async_set_native_value(4000))

    assert len(coordinator.background) == 1
    assert entity.native_value == 3988
    assert not notifications


def test_power_and_current_entities_agree() -> None:
    """They are two views of one setting and must not disagree."""
    power, coordinator, _ = make_power()
    current = number_module.DazeWallboxNumberEntity(
        coordinator=coordinator,
        api_client=FakeApi(),
        serial_number="SER1",
        device_info={},
    )

    assert current.native_value == 6521
    assert power.native_value == 1539

    coordinator.data["maxExternalChargingCurrentInMilliAmps"] = 16900

    assert current.native_value == 16900
    assert power.native_value == 3988



# ------------------------------------------------------------------
# Refusing bad values without a round trip
# ------------------------------------------------------------------


GRID_LIMITED: dict[str, Any] = {
    "maxExternalChargingCurrentInMilliAmps": 16900,
    "lastMaxInstallationCurrent": 32000,
    "lastACVoltageL1": 236,
    "supplyGridMaxPower": 3000,
    "dpm": True,
}


def test_a_current_below_the_floor_is_never_sent() -> None:
    """The API answers 422 for this; the bounds are already known."""
    notifications.clear()
    entity, _, client = make_number(data=GRID_LIMITED)

    asyncio.run(entity.async_set_native_value(6000))

    assert client.calls == [], "a known-bad value must not reach the API"
    assert len(notifications) == 1
    assert "below" in notifications[0]["message"]


def test_a_current_above_the_installation_rating_is_never_sent() -> None:
    """Same, at the other end."""
    notifications.clear()
    entity, _, client = make_number(data=GRID_LIMITED)

    asyncio.run(entity.async_set_native_value(40000))

    assert client.calls == []
    assert len(notifications) == 1
    assert "above" in notifications[0]["message"]


def test_a_valid_current_is_still_sent() -> None:
    """The guard must not block values the charger accepts."""
    notifications.clear()
    entity, _, client = make_number(data=GRID_LIMITED)

    asyncio.run(entity.async_set_native_value(20000))

    assert client.calls == [("current", 20000)]
    assert not notifications


def test_exceeding_the_grid_cap_is_advisory_not_blocking() -> None:
    """The charger accepts it and throttles the draw instead.

    A 7552 W limit was accepted by a charger reporting a 3000 W supply
    cap, so refusing to send it would be wrong.
    """
    notifications.clear()
    entity, _, client = make_number(data=GRID_LIMITED)

    asyncio.run(entity.async_set_native_value(32000))

    assert client.calls == [("current", 32000)]
    assert not notifications, "the grid cap must not raise an error"


def test_power_entity_clamps_rather_than_refusing() -> None:
    """A wattage outside the range is corrected, not rejected.

    watts_to_milliamps clamps before the value is validated, which is
    deliberate: a round figure near a boundary should charge at the
    nearest legal rate rather than fail. The validation behind it is a
    guard against inconsistent bounds, not the primary path.
    """
    notifications.clear()
    entity, coordinator, client = make_power()

    asyncio.run(entity.async_set_native_value(500))

    floor_ma = number_module.min_charging_current(coordinator.data)
    assert client.calls == [("current", floor_ma)]
    assert not notifications


def test_power_entity_never_sends_below_the_charger_floor() -> None:
    """Whatever is asked for, the sent value must be acceptable."""
    for requested in (0, 100, 500, 1000, 1400):
        notifications.clear()
        entity, coordinator, client = make_power()

        asyncio.run(entity.async_set_native_value(requested))

        assert len(client.calls) == 1, requested
        sent = client.calls[0][1]
        assert (
            number_module.validate_charging_current(sent, coordinator.data)
            is None
        ), (requested, sent)


# ------------------------------------------------------------------
# Shared optimistic state
# ------------------------------------------------------------------


def test_shared_state_shows_the_request_until_reality_agrees() -> None:
    """One implementation now serves all four controls."""
    state = optimistic_module.OptimisticState()

    assert state.resolve(6521) == 6521

    state.request(16000)
    assert state.resolve(6521) == 16000
    assert state.pending is True

    assert state.resolve(16000) == 16000
    assert state.pending is False


def test_shared_state_tolerates_a_drifting_measurement() -> None:
    """Watts derive from a live voltage, so equality never holds.

    This is what made the power entity stick: both sides recomputed
    from a reading that moves by a volt between polls.
    """
    state = optimistic_module.OptimisticState(tolerance=100)

    state.request(3988)
    assert state.resolve(4000) == 4000
    assert state.pending is False


def test_shared_state_without_tolerance_demands_equality() -> None:
    """A switch or a mode must match exactly."""
    state = optimistic_module.OptimisticState()

    state.request("eco")
    assert state.resolve("fast") == "eco"
    assert state.resolve("eco") == "eco"
    assert state.pending is False


def test_shared_state_holds_longer_while_a_retry_is_queued() -> None:
    """A queued retry means the request is genuinely outstanding."""

    quick = optimistic_module.OptimisticState()
    quick.request(True, awaiting_retry=False)

    patient = optimistic_module.OptimisticState()
    patient.request(True, awaiting_retry=True)

    # Neither has expired yet, but the caps differ.
    assert quick.expired() is False
    assert patient.expired() is False

    quick._since -= optimistic_module.OPTIMISTIC_STATE_TIMEOUT + 1
    patient._since -= optimistic_module.OPTIMISTIC_STATE_TIMEOUT + 1

    assert quick.expired() is True
    assert patient.expired() is False, "a pending retry must extend the hold"


def test_shared_state_hold_is_capped() -> None:
    """A superseded retry never reports back, so the hold must end.

    Without a cap the entity would show a stale request until Home
    Assistant restarts.
    """
    state = optimistic_module.OptimisticState()
    state.request(True, awaiting_retry=True)

    state._since -= optimistic_module.MAX_OPTIMISTIC_HOLD + 1

    assert state.expired() is True
    assert state.resolve(False) is False


def test_shared_state_ignores_an_unknown_reading() -> None:
    """No reading is not agreement."""
    state = optimistic_module.OptimisticState()

    state.request(16000)
    assert state.resolve(None) == 16000
    assert state.pending is True



def test_setting_power_updates_the_current_view_at_once() -> None:
    """One setting, two views: they must not disagree.

    Both read the same field, so they converge on the next poll
    anyway. The point is that they agree immediately, rather than
    showing different figures for the ten seconds until then.
    """
    coordinator = FakeCoordinator(dict(POWER_DATA))
    client = FakeApi()

    power = number_module.DazeWallboxPowerEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )
    current = number_module.DazeWallboxNumberEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )
    asyncio.run(power.async_added_to_hass())
    asyncio.run(current.async_added_to_hass())

    assert current.native_value == 6521
    assert power.native_value == 1539

    asyncio.run(power.async_set_native_value(4000))

    # The charger still reports the old figure.
    assert coordinator.data["maxExternalChargingCurrentInMilliAmps"] == 6521
    assert power.native_value == 3988
    assert current.native_value == 16900, "the current view did not follow"


def test_setting_current_updates_the_power_view_at_once() -> None:
    """The same in the other direction."""
    coordinator = FakeCoordinator(dict(POWER_DATA))
    client = FakeApi()

    power = number_module.DazeWallboxPowerEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )
    current = number_module.DazeWallboxNumberEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )
    asyncio.run(power.async_added_to_hass())
    asyncio.run(current.async_added_to_hass())

    asyncio.run(current.async_set_native_value(16900))

    assert current.native_value == 16900
    assert power.native_value == 3988, "the power view did not follow"


def test_the_untouched_view_is_told_to_redraw() -> None:
    """Agreeing internally is not enough; the frontend must be told."""
    coordinator = FakeCoordinator(dict(POWER_DATA))
    client = FakeApi()

    power = number_module.DazeWallboxPowerEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )
    current = number_module.DazeWallboxNumberEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )
    asyncio.run(power.async_added_to_hass())
    asyncio.run(current.async_added_to_hass())

    before = current.state_writes
    asyncio.run(power.async_set_native_value(4000))

    assert current.state_writes > before


def test_both_views_settle_together() -> None:
    """Once the charger agrees, neither should still be guessing."""
    coordinator = FakeCoordinator(dict(POWER_DATA))
    client = FakeApi()

    power = number_module.DazeWallboxPowerEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )
    asyncio.run(power.async_added_to_hass())

    asyncio.run(power.async_set_native_value(4000))
    assert coordinator.limit_state.pending is True

    coordinator.data["maxExternalChargingCurrentInMilliAmps"] = 16900
    power._handle_coordinator_update()

    assert coordinator.limit_state.pending is False



def test_a_command_is_not_sent_to_a_silent_charger() -> None:
    """Spending 33 seconds of retries on a powered-off charger is waste.

    The API keeps serving the last known record, so the command is
    accepted and then fails against a device that is not there. The
    resulting message blamed the Daze service rather than the power
    supply.
    """
    from datetime import datetime, timedelta, timezone

    stale = (datetime.now(timezone.utc) - timedelta(minutes=40))
    notifications.clear()

    entity, _, client = make_number(
        data={
            **BASE_DATA,
            "lastAttributesUpdatedOn": stale.isoformat().replace(
                "+00:00", "Z"
            ),
        }
    )

    asyncio.run(entity.async_set_native_value(16000))

    assert client.calls == [], "nothing should be sent to a silent charger"
    assert len(notifications) == 1
    assert "power" in notifications[0]["message"].lower()


def test_a_command_is_sent_to_a_reporting_charger() -> None:
    """The guard must not block a charger that is present."""
    from datetime import datetime, timezone

    notifications.clear()
    entity, _, client = make_number(
        data={
            **BASE_DATA,
            "lastAttributesUpdatedOn": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
    )

    asyncio.run(entity.async_set_native_value(16000))

    assert client.calls == [("current", 16000)]
    assert not notifications



def test_correcting_a_power_value_back_is_still_sent() -> None:
    """Dragging back to the starting value must not be swallowed.

    The power entity compared against the charger's reading rather
    than what it was displaying. After a pending change, correcting
    the slider back matched the stale reading, so nothing was sent and
    the charger stayed on the intermediate value the user had already
    moved away from.
    """
    coordinator = FakeCoordinator(dict(POWER_DATA))
    coordinator.data["maxExternalChargingCurrentInMilliAmps"] = 16900
    client = FakeApi()

    power = number_module.DazeWallboxPowerEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )
    asyncio.run(power.async_added_to_hass())

    # Drop it, then immediately put it back.
    asyncio.run(power.async_set_native_value(1600))
    first = list(client.calls)
    asyncio.run(power.async_set_native_value(3988))

    assert len(client.calls) == len(first) + 1, (
        "the corrective change was dropped"
    )
    assert client.calls[-1][1] == 16900


def test_setting_the_displayed_value_again_sends_nothing() -> None:
    """The guard must still suppress a genuine no-op."""
    coordinator = FakeCoordinator(dict(POWER_DATA))
    coordinator.data["maxExternalChargingCurrentInMilliAmps"] = 16900
    client = FakeApi()

    power = number_module.DazeWallboxPowerEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )
    asyncio.run(power.async_added_to_hass())

    asyncio.run(power.async_set_native_value(3988))

    assert client.calls == []


def test_switch_clears_its_pending_state_when_retries_fail() -> None:
    """Otherwise the toggle asserts a state the user was told failed.

    Every other control cleared on failure; the switch only notified,
    and the hold had just been extended from 20 to 525 seconds.
    """
    coordinator = FakeCoordinator(dict(BASE_DATA))
    client = FakeApi(error=rpc_failure())

    switch_module = sys.modules["daze_entities_under_test.switch"]
    entity = switch_module.DazeWallboxSwitchEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )

    notifications.clear()
    asyncio.run(entity.async_turn_on())

    assert coordinator.background, "expected a queued retry"
    assert entity.is_on is True

    coordinator.background[0]["on_failure"]("could not be delivered")

    assert entity.is_on is not True, "the toggle still asserts the command"
    assert len(notifications) == 1


def test_a_manual_limit_change_disarms_solar_control() -> None:
    """Touching the control means you want manual control.

    The controller writes through the API client, never the entity, so
    any write arriving here is by definition external. That makes the
    rule mechanical rather than a flag that could be wrong.
    """
    class Ctl:
        mode = "active"
        disarmed = False

        def disarm(self, reason: str) -> None:
            self.disarmed = True

    coordinator = FakeCoordinator(dict(BASE_DATA))
    coordinator.solar_controller = Ctl()
    entity, _, _ = make_number()
    entity.coordinator = coordinator

    asyncio.run(entity.async_set_native_value(16000))

    assert coordinator.solar_controller.disarmed is True


def test_a_manual_charge_toggle_disarms_solar_control() -> None:
    """The switch is a control too.

    Without this the user presses the toggle, and the next tick — at
    most two minutes later — sees a connected car and sustained surplus
    and commands the opposite. Solar control would be fighting the
    person holding the button.
    """
    class Ctl:
        disarmed = False

        def disarm(self, reason: str) -> None:
            self.disarmed = True

    coordinator = FakeCoordinator(dict(BASE_DATA))
    coordinator.solar_controller = Ctl()
    client = FakeApi()

    switch_module = sys.modules["daze_entities_under_test.switch"]
    entity = switch_module.DazeWallboxSwitchEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )

    asyncio.run(entity.async_turn_on())

    assert coordinator.solar_controller.disarmed is True


def test_a_manual_power_change_disarms_solar_control() -> None:
    """The power view of the same setting is a control too.

    number.py and switch.py wire the same helper onto four call sites
    in total; the current entity and the start toggle are covered
    above. This is the power entity's own copy, not shared code, so it
    needs its own regression test.
    """
    class Ctl:
        disarmed = False

        def disarm(self, reason: str) -> None:
            self.disarmed = True

    entity, coordinator, _ = make_power()
    coordinator.solar_controller = Ctl()

    asyncio.run(entity.async_set_native_value(4000))

    assert coordinator.solar_controller.disarmed is True


def test_a_manual_charge_stop_disarms_solar_control() -> None:
    """Stopping a charge is a control too, and the direction that
    matters most: without this, the car the person just told to stop
    is restarted by solar control within two minutes, against the
    person who is standing right there.
    """
    class Ctl:
        disarmed = False

        def disarm(self, reason: str) -> None:
            self.disarmed = True

    data = dict(BASE_DATA)
    data["evseStatus"] = "charging"
    coordinator = FakeCoordinator(data)
    coordinator.solar_controller = Ctl()
    client = FakeApi()

    switch_module = sys.modules["daze_entities_under_test.switch"]
    entity = switch_module.DazeWallboxSwitchEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )

    asyncio.run(entity.async_turn_off())

    assert coordinator.solar_controller.disarmed is True


def test_a_rejected_limit_change_still_disarms_solar_control() -> None:
    """A value the charger will refuse still counts as taking over.

    _disarm_solar is called before the validation check, not after: a
    user who types a current above the installation rating has still
    expressed the intent to take over, and solar control overwriting
    it a second later is exactly what the rule exists to prevent.
    Pins the placement rather than just the presence — every value in
    the tests above happens to be one the charger accepts, so moving
    the call after validation would still pass them all.
    """
    class Ctl:
        disarmed = False

        def disarm(self, reason: str) -> None:
            self.disarmed = True

    coordinator = FakeCoordinator(dict(BASE_DATA))
    coordinator.solar_controller = Ctl()
    entity, _, client = make_number()
    entity.coordinator = coordinator

    asyncio.run(entity.async_set_native_value(999999))

    assert coordinator.solar_controller.disarmed is True
    assert client.calls == [], "an invalid value must never reach the API"


def test_an_offline_charge_start_still_disarms_solar_control() -> None:
    """An unreachable charger does not cancel out the user's intent.

    _disarm_solar is called before the offline check, not after: a
    user whose charger is briefly unreachable has still expressed the
    intent to take over. Pins the placement — every switch test above
    uses a reachable charger, so moving the call after the offline
    check would still pass them all.
    """
    class Ctl:
        disarmed = False

        def disarm(self, reason: str) -> None:
            self.disarmed = True

    data = {"evseStatus": "idle", "active": False}
    coordinator = FakeCoordinator(data)
    coordinator.solar_controller = Ctl()
    client = FakeApi()

    switch_module = sys.modules["daze_entities_under_test.switch"]
    entity = switch_module.DazeWallboxSwitchEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )

    asyncio.run(entity.async_turn_on())

    assert coordinator.solar_controller.disarmed is True
    assert client.calls == [], "an offline charger must never be sent a command"


def test_an_offline_charge_stop_still_disarms_solar_control() -> None:
    """Same placement guarantee on the stop side, the direction the
    switch's own docstring names as the one that matters most.
    """
    class Ctl:
        disarmed = False

        def disarm(self, reason: str) -> None:
            self.disarmed = True

    data = {"evseStatus": "charging", "active": False}
    coordinator = FakeCoordinator(data)
    coordinator.solar_controller = Ctl()
    client = FakeApi()

    switch_module = sys.modules["daze_entities_under_test.switch"]
    entity = switch_module.DazeWallboxSwitchEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )

    asyncio.run(entity.async_turn_off())

    assert coordinator.solar_controller.disarmed is True
    assert client.calls == [], "an offline charger must never be sent a command"


def test_solar_select_offers_three_modes() -> None:
    """One control with three states, so 'dry run on, solar off'
    cannot be expressed."""
    select_mod = sys.modules["daze_entities_under_test.select"]
    assert select_mod.SOLAR_MODE_OPTIONS == ["off", "simulate", "active"]


def _solar_select(configured: bool = False) -> tuple[Any, Any]:
    """Build the solar select over a controller double."""
    select_mod = sys.modules["daze_entities_under_test.select"]

    class Ctl:
        def __init__(self) -> None:
            self.configured = configured
            self.mode = None

        def add_listener(self, cb):
            return lambda: None

        @property
        def unsupported_reason(self):
            if not self.configured:
                return "no grid sensors have been chosen"
            return None

    controller = Ctl()
    entity = select_mod.DazeSolarControlSelect(
        coordinator=FakeCoordinator(dict(BASE_DATA)),
        controller=controller,
        serial_number="SER1",
        device_info={},
    )
    return entity, controller


def test_solar_select_is_unavailable_without_sensors() -> None:
    """Both grid sensors are required before it can do anything."""
    entity, _ = _solar_select(configured=False)

    assert entity.available is False


def test_solar_select_refuses_to_arm_without_sensors() -> None:
    """Availability is a hint to the dashboard, not a gate.

    A service call or an automation reaches async_select_option
    whatever the entity reports, so the refusal the spec requires —
    "both are required before solar control can leave off" — has to be
    enforced in the method that acts, and explained where the caller
    can see it. Asserting `available is False` instead would pass
    against a select that happily arms itself with no sensors at all.

    Checked for both non-off options, not just "active": narrowing the
    guard to `option == "active"` would let a user or automation select
    "simulate" with no grid sensors configured. The controller would
    then tick, find nothing to read, and do nothing — while the select
    still displays "simulate", as though a dry run were under way. That
    is "leaving off" in every way that matters, just quietly.
    """
    for option in ("simulate", "active"):
        entity, controller = _solar_select(configured=False)

        raised = False
        try:
            asyncio.run(entity.async_select_option(option))
        except HomeAssistantError:
            raised = True

        assert raised, f"arming without sensors was not refused for {option!r}"
        assert controller.mode is None, "the mode was changed anyway"


def test_solar_select_arms_once_the_sensors_are_there() -> None:
    """The refusal must not be a blanket one."""
    entity, controller = _solar_select(configured=True)

    asyncio.run(entity.async_select_option("simulate"))

    assert controller.mode is not None
    assert controller.mode.value == "simulate"


def test_setting_the_reserve_writes_it_to_config_entry_options() -> None:
    """The write half of the restart guarantee: this only proves the
    number entity persists what it is given.

    An in-memory-only reserve returns to 0 W on every restart, and 0 W
    means the house gets nothing before the car does — a setting that
    exists to hold power back must not quietly stop holding it. But
    that guarantee has two halves, and this test cannot see the other
    one: nothing here restarts anything or re-reads the option back
    into a controller. The read half — `async_setup_entry` passing
    `entry.options.get(CONF_SOLAR_RESERVE, DEFAULT_SOLAR_RESERVE)` into
    `SolarController(...)` on the next setup — is covered separately by
    `test_async_setup_entry_seeds_the_controllers_reserve_from_options`
    in tests/test_init_entry.py, the only place in the tree that calls
    `async_setup_entry` at all. Together the two are the round trip;
    apart, each name says only what its own body checks.
    """
    number_mod = sys.modules["daze_entities_under_test.number"]
    const_mod = sys.modules["daze_entities_under_test.const"]

    class Ctl:
        reserve_w = 0.0

    class FakeEntry:
        options: ClassVar[dict[str, Any]] = {"poll_interval": 30}

    class FakeEntries:
        def __init__(self) -> None:
            self.updated: list[dict[str, Any]] = []

        def async_update_entry(self, entry, options=None, **kwargs):
            entry.options = options
            self.updated.append(options)

    class FakeHass:
        def __init__(self) -> None:
            self.config_entries = FakeEntries()

    entry = FakeEntry()
    entity = number_mod.DazeSolarReserveEntity(
        coordinator=FakeCoordinator(dict(BASE_DATA)),
        controller=Ctl(),
        entry=entry,
        serial_number="SER1",
        device_info={},
    )
    entity.hass = FakeHass()

    asyncio.run(entity.async_set_native_value(1500))

    assert entity.native_value == 1500
    assert entry.options[const_mod.CONF_SOLAR_RESERVE] == 1500
    # The rest of the options must survive the write, or saving a
    # reserve would silently drop the user's grid sensors.
    assert entry.options["poll_interval"] == 30


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
