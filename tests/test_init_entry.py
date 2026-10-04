"""Execute the real config-entry lifecycle against a stubbed Home Assistant.

Nothing in the test tree had ever imported ``custom_components/daze/
__init__.py`` before this file, so nothing verified three decisions Task
6 made there:

- The indentation of the teardown call in ``async_unload_entry`` — the
  brief says in terms that one level out and a second unload (or an
  unload after a failed setup) raises on ``None.get(...)`` before the
  rest of teardown runs, leaving the coordinator's timers firing
  against a closed client.
- ``_reload_signature`` excluding the solar reserve — dropping that
  filter makes every step of the reserve slider tear the integration
  down and rebuild it, which is exactly the symptom Step 8 exists to
  prevent.
- The three service handlers disarming solar control before refusing
  an offline command — without it, a service call no longer hands
  control back to whoever issued it.

Home Assistant is replaced with the smallest stubs the module actually
touches, following the same approach as test_entities.py and
test_solar_controller.py. The integration modules themselves are real;
only ``async_setup_coordinator`` (which would otherwise need a working
DataUpdateCoordinator and a live API client) is left uncalled — the
functions under test here never call it, so it does not need to work,
only to import.

Run with pytest, or standalone:

    python3 tests/test_init_entry.py
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "daze"
PKG_NAME = "daze_init_under_test"


# ------------------------------------------------------------------
# Home Assistant stubs
# ------------------------------------------------------------------


class StubDataUpdateCoordinator:
    """Stand-in for DataUpdateCoordinator.

    Only needed so ``class DazeDataUpdateCoordinator(DataUpdateCoordinator
    [DazeCoordinatorData])`` can be defined at import time. Never
    instantiated here: every test below supplies its own lightweight
    fake coordinator instead of the real one.
    """

    def __class_getitem__(cls, _item: Any) -> Any:
        return cls

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.data: dict[str, Any] | None = None


def _module(name: str, **attributes: Any) -> types.ModuleType:
    """Build a stub module with the given attributes."""
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _install_homeassistant_stubs() -> None:
    """Register just enough of Home Assistant and voluptuous to import
    the real __init__.py, coordinator.py and solar_controller.py.
    """
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
        DataUpdateCoordinator=StubDataUpdateCoordinator,
        UpdateFailed=type("UpdateFailed", (Exception,), {}),
    )
    _module("homeassistant.helpers.config_validation", positive_int=int)
    _module(
        "homeassistant.helpers.device_registry",
        async_get=lambda hass: None,
        DeviceInfo=dict,
    )

    # voluptuous is a real dependency of the running integration but is
    # not installed in this environment; the schemas it builds are
    # never exercised here (services are invoked by calling the
    # captured handler directly, bypassing Home Assistant's own schema
    # validation), so trivial passthroughs are enough to import it.
    _module(
        "voluptuous",
        Schema=lambda schema: schema,
        Required=lambda key: key,
        All=lambda *validators: validators,
        Range=lambda **kwargs: None,
    )


_install_homeassistant_stubs()


def _load_package() -> types.ModuleType:
    """Load the real package, including its __init__.py, without going
    through custom_components.daze so the stubs above are the only
    Home Assistant this run ever sees.
    """
    package = types.ModuleType(PKG_NAME)
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules[PKG_NAME] = package

    for name in ("const", "payload", "models", "optimistic", "solar"):
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

    for name in ("coordinator", "solar_controller"):
        spec = importlib.util.spec_from_file_location(
            f"{PKG_NAME}.{name}", PACKAGE_DIR / f"{name}.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{PKG_NAME}.{name}"] = module
        spec.loader.exec_module(module)

    # Finally, the real __init__.py itself — its relative imports
    # (.api, .const, .coordinator, .payload, .solar_controller) resolve
    # against the submodules already registered above.
    spec = importlib.util.spec_from_file_location(
        PKG_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    assert spec and spec.loader
    init_module = importlib.util.module_from_spec(spec)
    sys.modules[PKG_NAME] = init_module
    spec.loader.exec_module(init_module)

    return init_module


daze_init = _load_package()
const = sys.modules[f"{PKG_NAME}.const"]

DOMAIN = const.DOMAIN
CONF_SOLAR_RESERVE = const.CONF_SOLAR_RESERVE


# ------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------


class FakeEntry:
    """Stand-in for a ConfigEntry."""

    def __init__(
        self,
        entry_id: str = "entry1",
        data: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        self.entry_id = entry_id
        self.data = dict(data or {})
        self.options = dict(options or {})
        self.unload_callbacks: list[Any] = []
        self.update_listeners: list[Any] = []

    def async_on_unload(self, callback: Any) -> None:
        """Record a callback to run on unload."""
        self.unload_callbacks.append(callback)

    def add_update_listener(self, listener: Any) -> Any:
        """Record an options-update listener."""
        self.update_listeners.append(listener)
        return lambda: self.update_listeners.remove(listener)


class FakeConfigEntries:
    """Stand-in for hass.config_entries."""

    def __init__(self, unload_ok: bool = True) -> None:
        self.unload_ok = unload_ok
        self.reload_calls: list[str] = []
        self.forward_calls: list[Any] = []

    async def async_forward_entry_setups(
        self, entry: Any, platforms: Any
    ) -> None:
        self.forward_calls.append((entry, platforms))

    async def async_unload_platforms(self, entry: Any, platforms: Any) -> bool:
        return self.unload_ok

    async def async_reload(self, entry_id: str) -> None:
        self.reload_calls.append(entry_id)


class FakeServices:
    """Stand-in for hass.services, recording registered handlers."""

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def async_register(
        self, domain: str, service: str, handler: Any, schema: Any = None
    ) -> Any:
        self.handlers[service] = handler
        return object()


class FakeHass:
    """Stand-in for HomeAssistant, holding only what these tests touch."""

    def __init__(self, unload_ok: bool = True) -> None:
        self.data: dict[str, Any] = {}
        self.config_entries = FakeConfigEntries(unload_ok)
        self.services = FakeServices()


class FakeServiceCall:
    """Stand-in for a ServiceCall."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = dict(data or {})


class FakeSolarController:
    """Records whether it was disarmed or stopped."""

    def __init__(self) -> None:
        self.disarmed_reasons: list[str] = []
        self.stopped = False

    def disarm(self, reason: str) -> None:
        self.disarmed_reasons.append(reason)

    async def async_stop(self) -> None:
        self.stopped = True


class FakeCoordinatorHandle:
    """Records whether its timers were shut down."""

    def __init__(self) -> None:
        self.shutdown_called = False

    def async_shutdown_timers(self) -> None:
        self.shutdown_called = True


class FakeApiClient:
    """Records the command calls a service handler makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def async_start_charge(self, serial: str) -> dict:
        self.calls.append(("start", serial))
        return {}

    async def async_stop_charge(self, serial: str) -> dict:
        self.calls.append(("stop", serial))
        return {}

    async def async_set_max_charging_current(
        self, serial: str, current: int
    ) -> dict:
        self.calls.append(("current", current))
        return {}


class FakeServiceCoordinator:
    """The coordinator surface _async_register_services touches."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.api_client = FakeApiClient()
        self.serial_number = "SER1"
        self.solar_controller: Any = None
        self.refresh_calls = 0
        self.settle_calls = 0

    async def async_request_refresh(self) -> None:
        self.refresh_calls += 1

    def async_schedule_settle_refresh(self) -> None:
        self.settle_calls += 1


class FakeDeviceRegistry:
    """Stand-in for the device registry `dr.async_get(hass)` returns."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def async_get_or_create(self, **kwargs: Any) -> None:
        self.created.append(kwargs)


REACHABLE_DATA: dict[str, Any] = {"active": True}
OFFLINE_DATA: dict[str, Any] = {"active": False}


def _register(
    data: dict[str, Any],
) -> tuple[FakeHass, FakeEntry, FakeServiceCoordinator]:
    """Build a hass/entry/coordinator triple with services registered."""
    hass = FakeHass()
    entry = FakeEntry()
    coordinator = FakeServiceCoordinator(data)
    coordinator.solar_controller = FakeSolarController()
    daze_init._async_register_services(hass, entry, coordinator)
    return hass, entry, coordinator


# ------------------------------------------------------------------
# async_setup_entry
# ------------------------------------------------------------------


async def _fake_async_setup_coordinator(
    hass: Any, entry: Any
) -> FakeServiceCoordinator:
    """Stand in for the real coordinator construction async_setup_entry
    calls first.

    The real ``async_setup_coordinator`` builds an auth client, an API
    client and performs a live first refresh — none of that is what
    this test is about, and none of it is safe to run here. Swapped in
    by monkeypatching ``daze_init.async_setup_coordinator`` for the
    single test that needs ``async_setup_entry`` to run end to end.
    """
    return FakeServiceCoordinator(dict(REACHABLE_DATA))


def test_async_setup_entry_seeds_the_controllers_reserve_from_options() -> (
    None
):
    """The read half of the restart guarantee: a reserve persisted to
    the config entry's options must reach the controller on the next
    setup, not just default back to 0 W.

    ``tests/test_entities.py``'s
    ``test_setting_the_reserve_writes_it_to_config_entry_options``
    already covers the write half — the number entity persisting a new
    value. Nothing before this test called ``async_setup_entry`` at
    all, so the read half —
    ``reserve_w=entry.options.get(CONF_SOLAR_RESERVE,
    DEFAULT_SOLAR_RESERVE)`` in the ``SolarController(...)`` call — was
    unverified. Dropping that keyword (the constructor already
    defaults ``reserve_w`` to 0.0 on its own) passed every other test
    in the tree: every restart would then silently hand the house's
    entire reserved share to the car.

    ``async_setup_coordinator`` and the device registry are
    monkeypatched for the duration of this one test — the former would
    otherwise need a live API client and network access, the latter is
    stubbed globally to return ``None`` since no other test in this
    file calls ``async_get_or_create`` on it.
    """
    entry = FakeEntry(
        data={
            const.CONF_SERIAL_NUMBER: "SER1",
            const.CONF_NETWORK_UID: "NET1",
        },
        options={
            CONF_SOLAR_RESERVE: 1500,
            const.CONF_GRID_POWER_SENSOR: "sensor.grid_power",
        },
    )
    hass = FakeHass()

    original_setup_coordinator = daze_init.async_setup_coordinator
    original_async_get = daze_init.dr.async_get
    daze_init.async_setup_coordinator = _fake_async_setup_coordinator
    daze_init.dr.async_get = lambda _hass: FakeDeviceRegistry()
    try:
        asyncio.run(daze_init.async_setup_entry(hass, entry))
    finally:
        daze_init.async_setup_coordinator = original_setup_coordinator
        daze_init.dr.async_get = original_async_get

    controller = hass.data[DOMAIN][entry.entry_id]["solar_controller"]
    assert controller.reserve_w == 1500, (
        "the persisted reserve never reached the controller"
    )
    assert controller._grid_power_entity == "sensor.grid_power", (
        "the signed grid-power sensor was not wired into the controller"
    )


# ------------------------------------------------------------------
# _reload_signature
# ------------------------------------------------------------------


def test_reload_signature_ignores_only_the_solar_reserve() -> None:
    """The solar reserve is applied live and must not force a reload;
    every other option is a real configuration change and must still
    be seen.
    """
    entry = FakeEntry(
        data={"access_token": "a"},
        options={CONF_SOLAR_RESERVE: 500, "poll_interval": 30},
    )

    before = daze_init._reload_signature(entry)

    entry.options[CONF_SOLAR_RESERVE] = 1500
    assert daze_init._reload_signature(entry) == before, (
        "a reserve-only change must not alter the reload signature"
    )

    entry.options["poll_interval"] = 60
    assert daze_init._reload_signature(entry) != before, (
        "a real option change must still alter the reload signature"
    )


def test_a_reserve_only_options_change_does_not_reload_the_entry() -> None:
    """Wires _reload_signature into _async_update_listener: this is the
    behaviour Step 8 actually exists to produce, not just the pure
    function it is built from.
    """
    entry = FakeEntry(
        data={"access_token": "a"},
        options={CONF_SOLAR_RESERVE: 500, "poll_interval": 30},
    )
    hass = FakeHass()
    hass.data[DOMAIN] = {
        entry.entry_id: {
            "reload_signature": daze_init._reload_signature(entry),
        }
    }

    entry.options[CONF_SOLAR_RESERVE] = 1500
    asyncio.run(daze_init._async_update_listener(hass, entry))

    assert hass.config_entries.reload_calls == []


def test_a_real_options_change_still_reloads_the_entry() -> None:
    """The other half of Step 8's contract: a genuine configuration
    change (here, the poll interval) must still trigger a reload.
    """
    entry = FakeEntry(
        data={"access_token": "a"},
        options={CONF_SOLAR_RESERVE: 500, "poll_interval": 30},
    )
    hass = FakeHass()
    hass.data[DOMAIN] = {
        entry.entry_id: {
            "reload_signature": daze_init._reload_signature(entry),
        }
    }

    entry.options["poll_interval"] = 60
    asyncio.run(daze_init._async_update_listener(hass, entry))

    assert hass.config_entries.reload_calls == [entry.entry_id]


# ------------------------------------------------------------------
# async_unload_entry
# ------------------------------------------------------------------


def test_unload_stops_the_controller_and_the_coordinators_timers() -> None:
    """Both halves of Step 7's teardown run in the ordinary case."""
    entry = FakeEntry()
    hass = FakeHass(unload_ok=True)
    controller = FakeSolarController()
    coordinator_handle = FakeCoordinatorHandle()
    hass.data[DOMAIN] = {
        entry.entry_id: {
            "solar_controller": controller,
            "coordinator": coordinator_handle,
        }
    }

    asyncio.run(daze_init.async_unload_entry(hass, entry))

    assert controller.stopped is True
    assert coordinator_handle.shutdown_called is True
    assert entry.entry_id not in hass.data[DOMAIN]


def test_a_second_unload_does_not_raise() -> None:
    """entry_data is None on a second unload: DOMAIN is present in
    hass.data (the first unload's own cleanup line put it there, or
    left it there empty), but this entry's own key is already gone.
    One level of indentation out, ``entry_data.get(...)`` becomes
    ``None.get(...)`` and raises before the rest of teardown —
    including the coordinator's own ``async_shutdown_timers`` — ever
    runs.
    """
    entry = FakeEntry()
    hass = FakeHass(unload_ok=True)
    hass.data[DOMAIN] = {}  # this entry already popped, DOMAIN remains

    # Must not raise.
    result = asyncio.run(daze_init.async_unload_entry(hass, entry))

    assert result is True


def test_an_unload_after_a_failed_setup_does_not_raise() -> None:
    """entry_data is None for a different reason here: setup raised
    before ``hass.data.setdefault(DOMAIN, {})`` ever ran, so DOMAIN
    itself is missing from hass.data, not just this entry's key.

    A fix for the second-unload case that indexes ``hass.data[DOMAIN]``
    directly to clean up — rather than going through ``.get(DOMAIN,
    {})`` the way the read above it already does — passes the
    second-unload test above while still raising ``KeyError`` here.
    """
    entry = FakeEntry()
    hass = FakeHass(unload_ok=True)
    assert DOMAIN not in hass.data  # setup never got far enough to set it

    # Must not raise.
    result = asyncio.run(daze_init.async_unload_entry(hass, entry))

    assert result is True


# ------------------------------------------------------------------
# Service handlers disarm solar control
# ------------------------------------------------------------------


def test_start_charge_service_disarms_solar_before_refusing_offline() -> None:
    """A person calling daze.start_charge has taken over, even if the
    charger happens to be briefly unreachable at that exact moment.
    """
    hass, _entry, coordinator = _register(OFFLINE_DATA)

    with contextlib.suppress(daze_init.HomeAssistantError):
        asyncio.run(hass.services.handlers[const.SERVICE_START_CHARGE](
            FakeServiceCall()
        ))

    assert coordinator.solar_controller.disarmed_reasons != []
    assert coordinator.api_client.calls == [], (
        "an offline charger must never be sent a command"
    )


def test_stop_charge_service_disarms_solar_before_refusing_offline() -> None:
    """Same guarantee on the stop side."""
    hass, _entry, coordinator = _register(OFFLINE_DATA)

    with contextlib.suppress(daze_init.HomeAssistantError):
        asyncio.run(hass.services.handlers[const.SERVICE_STOP_CHARGE](
            FakeServiceCall()
        ))

    assert coordinator.solar_controller.disarmed_reasons != []
    assert coordinator.api_client.calls == []


def test_set_charging_current_service_disarms_solar_before_refusing_offline() -> (
    None
):
    """Same guarantee on the set-current service."""
    hass, _entry, coordinator = _register(OFFLINE_DATA)

    with contextlib.suppress(daze_init.HomeAssistantError):
        asyncio.run(
            hass.services.handlers[const.SERVICE_SET_CHARGING_CURRENT](
                FakeServiceCall({"current": 10000})
            )
        )

    assert coordinator.solar_controller.disarmed_reasons != []
    assert coordinator.api_client.calls == []


def test_reachable_service_calls_still_disarm_and_still_send() -> None:
    """The disarm must not come at the expense of the ordinary path:
    a reachable charger still gets the command after being disarmed.
    """
    hass, _entry, coordinator = _register(REACHABLE_DATA)

    asyncio.run(
        hass.services.handlers[const.SERVICE_SET_CHARGING_CURRENT](
            FakeServiceCall({"current": 10000})
        )
    )

    assert coordinator.solar_controller.disarmed_reasons != []
    assert coordinator.api_client.calls == [("current", 10000)]


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
