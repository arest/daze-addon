"""Tests for the options flow against a stubbed Home Assistant.

Follows the same approach as test_entities.py, test_solar_controller.py
and test_init_entry.py: the real config_flow.py is imported and
exercised, with Home Assistant and voluptuous replaced by the smallest
stubs the module actually touches.

Run with pytest, or standalone:

    python3 tests/test_config_flow.py
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
PKG_NAME = "daze_config_flow_under_test"

# ------------------------------------------------------------------
# Home Assistant / voluptuous stubs
# ------------------------------------------------------------------

def _module(name: str, **attributes: Any) -> types.ModuleType:
    """Build a stub module with the given attributes."""
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module

class _StubConfigFlow:
    """Stand-in for homeassistant.config_entries.ConfigFlow.

    DazeConfigFlow is declared as
    ``class DazeConfigFlow(ConfigFlow, domain=DOMAIN)`` — a class
    keyword argument evaluated at class-definition time, which the
    real ConfigFlow consumes through __init_subclass__. Plain
    ``object`` rejects unknown keyword arguments there and would raise
    on import.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__()

class _StubOptionsFlow:
    """Stand-in for homeassistant.config_entries.OptionsFlow.

    Real enough to observe what DazeOptionsFlowHandler does: records
    the title and data an async_create_entry call is given, and the
    step_id an async_show_form call is given, rather than performing
    any real flow-result machinery.
    """

    def async_create_entry(
        self, *, title: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(
        self, *, step_id: str, data_schema: Any, **kwargs: Any
    ) -> dict[str, Any]:
        return {"type": "form", "step_id": step_id, "data_schema": data_schema}

class _SelectorConfig:
    """Stand-in for EntitySelectorConfig / SelectSelectorConfig."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

class _Selector:
    """Stand-in for EntitySelector / SelectSelector."""

    def __init__(self, config: Any) -> None:
        self.config = config

class _SelectSelectorMode:
    DROPDOWN = "dropdown"

def _install_stubs() -> None:
    """Register just enough of Home Assistant and voluptuous to import
    the real config_flow.py.
    """
    _module("homeassistant")
    _module("homeassistant.core", HomeAssistant=object, callback=lambda fn: fn)
    _module(
        "homeassistant.config_entries",
        ConfigEntry=object,
        ConfigFlow=_StubConfigFlow,
        ConfigFlowResult=object,
        OptionsFlow=_StubOptionsFlow,
    )
    _module("homeassistant.helpers")
    _module(
        "homeassistant.helpers.aiohttp_client",
        async_get_clientsession=lambda hass: None,
    )
    _module(
        "homeassistant.helpers.selector",
        EntitySelector=_Selector,
        EntitySelectorConfig=_SelectorConfig,
        SelectSelector=_Selector,
        SelectSelectorConfig=_SelectorConfig,
        SelectSelectorMode=_SelectSelectorMode,
    )

    # voluptuous is a real dependency of the running integration but is
    # not installed in this environment. async_step_init's schema is
    # built and thrown away (this file never submits it for real
    # validation — user_input is handed to the handler directly), so
    # trivial passthroughs are enough to import it and build the form.
    _module(
        "voluptuous",
        Schema=lambda schema: schema,
        Required=lambda key, default=None: key,
        Optional=lambda key, **kwargs: key,
        All=lambda *validators: validators,
        Range=lambda **kwargs: None,
        Coerce=lambda type_: type_,
        In=lambda options: options,
        Invalid=type("Invalid", (Exception,), {}),
    )

_install_stubs()

def _load_package() -> types.ModuleType:
    """Load the real package, including config_flow.py, without going
    through custom_components.daze so the stubs above are the only
    Home Assistant this run ever sees.
    """
    package = types.ModuleType(PKG_NAME)
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules[PKG_NAME] = package

    for name in ("const", "payload"):
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
        f"{PKG_NAME}.config_flow",
        PACKAGE_DIR / "config_flow.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    assert spec and spec.loader
    config_flow_module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG_NAME}.config_flow"] = config_flow_module
    spec.loader.exec_module(config_flow_module)

    return config_flow_module

config_flow = _load_package()
const = sys.modules[f"{PKG_NAME}.const"]

# ------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------

class FakeConfigEntry:
    """Stand-in for a ConfigEntry, holding only what the handler reads."""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = {}
        self.options = dict(options or {})

def _handler(options: dict[str, Any] | None = None) -> Any:
    entry = FakeConfigEntry(options)
    return config_flow.DazeOptionsFlowHandler(entry)

# ------------------------------------------------------------------
# Saving the form merges rather than replaces
# ------------------------------------------------------------------

def test_saving_the_form_preserves_the_solar_reserve() -> None:
    """The form has no field for the solar reserve — Task 7's reserve
    entity writes it to these same options directly — so a save that
    replaces the options outright drops it back to 0 W. This task gives
    every existing user a reason to reopen this form: solar control now
    refuses to arm until the supply question is answered, and a user
    who already set a 2000 W house reserve must not lose it silently at
    the exact moment they are doing that.
    """
    handler = _handler({const.CONF_SOLAR_RESERVE: 2000, "poll_interval": 30})

    result = asyncio.run(
        handler.async_step_init({"poll_interval": 45})
    )

    assert result["data"][const.CONF_SOLAR_RESERVE] == 2000, (
        "the solar reserve was dropped by a save that did not mention it"
    )
    assert result["data"]["poll_interval"] == 45, (
        "the field the form actually submitted was not applied"
    )

def test_saving_the_form_applies_a_submitted_field_over_the_old_value() -> (
    None
):
    """The merge must not go the other way: a field the form did
    submit has to win over whatever was already stored, or "saving"
    the form would not actually change anything.
    """
    handler = _handler({"poll_interval": 30})

    result = asyncio.run(handler.async_step_init({"poll_interval": 60}))

    assert result["data"]["poll_interval"] == 60

def test_options_form_has_one_signed_grid_power_selector() -> None:
    """Options expose one optional power entity, not the removed pair."""
    result = asyncio.run(_handler().async_step_init())
    schema = result["data_schema"]

    assert const.CONF_GRID_POWER_SENSOR in schema
    assert "grid_import_sensor" not in schema
    assert "grid_export_sensor" not in schema
    grid_selector = schema[const.CONF_GRID_POWER_SENSOR]
    assert grid_selector.config.kwargs == {
        "domain": "sensor",
        "device_class": "power",
    }


def test_clearing_a_sensor_actually_clears_it() -> None:
    """Every field this form owns — the grid power sensor and the
    supply-phases question — is vol.Optional with no default, so a
    user clearing one in the frontend omits it from user_input rather
    than submitting an empty value. A blanket merge of the old options
    over the submitted ones reads that omission as "unchanged" and
    silently restores the stale entity_id, making a configured sensor
    impossible to clear once set — even though the spec calls empty a
    supported configuration. Only the solar reserve, the one key this
    form does not own, may survive an omission this way.
    """
    handler = _handler(
        {
            const.CONF_GRID_POWER_SENSOR: "sensor.grid_power",
            "poll_interval": 30,
        }
    )

    # The user cleared the grid power sensor picker and saved: the
    # frontend omits a cleared vol.Optional field entirely rather than
    # submitting it as empty.
    result = asyncio.run(
        handler.async_step_init(
            {
                "poll_interval": 30,
            }
        )
    )

    assert const.CONF_GRID_POWER_SENSOR not in result["data"], (
        "a cleared sensor was silently restored from the stale options"
    )

# ------------------------------------------------------------------
# Re-authentication
#
# The flow Home Assistant starts when the stored tokens stop working,
# and the one path in config_flow.py that must update an existing
# entry rather than create a second one. It had no coverage at all,
# despite expiring tokens being this integration's most common
# failure: the access token lives an hour, and a refresh token that
# is revoked or rotated out sends every user through here.
# ------------------------------------------------------------------


EVSE_RECORD: dict[str, Any] = {
    "evseName": "Daze HomeTT",
    "serialNumber": "SER1",
    "deviceProfile": "DT01",
    "firmwareVersion": "1.2.3",
    "softwareVersion": "4.5.6",
}


class FakeAuthClient:
    """An auth client whose token validation does what a test says."""

    def __init__(self, access_token: str, refresh_token: str) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token

    async def async_validate_tokens(self, session: Any) -> bool:
        if isinstance(FakeAuthClient.result, Exception):
            raise FakeAuthClient.result
        return True

    result: ClassVar[Any] = None


class FakeApiClient:
    """Returns one account, one network and one charger."""

    def __init__(self, auth_client: Any, session: Any) -> None:
        self.auth_client = auth_client

    async def async_get_user_info(self) -> dict[str, Any]:
        return {"email": "someone@example.com"}

    async def async_get_networks(self, email: str) -> list[dict[str, Any]]:
        return [{"uid": "net-1", "name": "Home"}]

    async def async_get_evses(self, network_uid: str) -> list[dict[str, Any]]:
        return [dict(EVSE_RECORD)]


class FakeConfigEntries:
    """Records the entry updates and reloads a re-auth performs."""

    def __init__(self) -> None:
        self.updated: list[tuple[Any, dict[str, Any]]] = []
        self.reloaded: list[str] = []

    def async_update_entry(self, entry: Any, data: dict[str, Any]) -> None:
        entry.data = dict(data)
        self.updated.append((entry, dict(data)))

    async def async_reload(self, entry_id: str) -> None:
        self.reloaded.append(entry_id)

    def async_get_entry(self, entry_id: str) -> Any:
        return self.entries.get(entry_id)

    entries: ClassVar[dict[str, Any]] = {}


class FakeHass:
    """Just the config_entries registry the re-auth path reaches for."""

    def __init__(self) -> None:
        self.config_entries = FakeConfigEntries()


class ExistingEntry:
    """The entry already set up, which a re-auth must update in place."""

    def __init__(self) -> None:
        self.entry_id = "ENTRY1"
        self.title = "Daze HomeTT"
        self.data: dict[str, Any] = {
            const.CONF_ACCESS_TOKEN: "old-access",
            const.CONF_REFRESH_TOKEN: "old-refresh",
            const.CONF_SERIAL_NUMBER: "SER1",
        }
        self.options: dict[str, Any] = {}


def _reauth_flow() -> tuple[Any, Any, Any]:
    """Build a config flow already in a re-authentication."""
    FakeAuthClient.result = None

    flow = config_flow.DazeConfigFlow()
    hass = FakeHass()
    entry = ExistingEntry()
    FakeConfigEntries.entries = {entry.entry_id: entry}

    flow.hass = hass
    flow.context = {"entry_id": entry.entry_id, "source": "reauth"}
    flow.unique_ids: list[Any] = []

    async def _set_unique_id(unique_id: Any) -> None:
        flow.unique_ids.append(unique_id)

    flow.async_set_unique_id = _set_unique_id
    flow._abort_if_unique_id_configured = lambda: (_ for _ in ()).throw(
        AssertionError(
            "a re-authentication went down the new-entry path and "
            "aborted itself as a duplicate"
        )
    )
    flow.created: list[dict[str, Any]] = []
    flow.aborted: list[str] = []
    flow.forms: list[str] = []

    def _create_entry(*, title: str, data: dict[str, Any]) -> dict[str, Any]:
        flow.created.append({"title": title, "data": data})
        return {"type": "create_entry"}

    def _abort(*, reason: str) -> dict[str, Any]:
        flow.aborted.append(reason)
        return {"type": "abort", "reason": reason}

    def _show_form(*, step_id: str, **kwargs: Any) -> dict[str, Any]:
        flow.forms.append(step_id)
        return {"type": "form", "step_id": step_id, **kwargs}

    flow.async_create_entry = _create_entry
    flow.async_abort = _abort
    flow.async_show_form = _show_form

    config_flow.DazeAuthClient = FakeAuthClient
    config_flow.DazeApiClient = FakeApiClient

    return flow, hass, entry


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def _complete_reauth(
    flow: Any,
    access_token: str = "new-access",
    refresh_token: str = "new-refresh",
) -> Any:
    """Walk a re-auth all the way through, as the user would.

    Re-authentication is not a single step: the token form hands off
    to network selection, which hands off to confirmation, and only
    the confirm step writes the entry. A test that stops after the
    token form observes none of that.
    """
    result = _run(
        flow.async_step_user(
            {
                const.CONF_ACCESS_TOKEN: access_token,
                const.CONF_REFRESH_TOKEN: refresh_token,
            }
        )
    )
    if result.get("step_id") != "network":
        return result

    result = _run(flow.async_step_network({const.CONF_NETWORK_UID: "net-1"}))
    if result.get("step_id") != "confirm":
        return result

    return _run(
        flow.async_step_confirm({const.CONF_EVSE_NAME: "Daze HomeTT"})
    )


def test_reauth_picks_up_the_entry_it_was_started_for() -> None:
    """The flow has to know which entry it is re-authenticating.

    Without it the confirm step takes the new-entry branch and tries
    to add a second copy of a charger that is already configured.
    """
    flow, _hass, entry = _reauth_flow()

    _run(flow.async_step_reauth())

    assert flow._reauth_entry is entry, (
        "the re-auth flow did not find the entry it was started for"
    )


def test_reauth_asks_for_tokens_again() -> None:
    """Re-auth starts at the token form, not at network selection."""
    flow, _hass, _entry = _reauth_flow()

    result = _run(flow.async_step_reauth())

    assert result["step_id"] == "user", (
        f"re-auth opened the {result.get('step_id')!r} step"
    )


def test_reauth_updates_the_existing_entry_rather_than_adding_one() -> None:
    """The whole point of the path: one charger, new tokens.

    Creating an entry here would leave the user with a duplicate
    device and the original still broken.
    """
    flow, hass, entry = _reauth_flow()
    _run(flow.async_step_reauth())

    _complete_reauth(flow)

    assert flow.created == [], "re-authentication created a second entry"
    assert len(hass.config_entries.updated) == 1, (
        "re-authentication did not update the existing entry"
    )

    _updated_entry, data = hass.config_entries.updated[0]
    assert data[const.CONF_ACCESS_TOKEN] == "new-access"
    assert data[const.CONF_REFRESH_TOKEN] == "new-refresh"


def test_reauth_reloads_the_entry_so_the_new_tokens_take_effect() -> None:
    """Rewriting the tokens is not enough on its own.

    The coordinator holds an auth client built from the old ones, so
    without the reload the entities keep failing with the credentials
    the user has just replaced.
    """
    flow, hass, entry = _reauth_flow()
    _run(flow.async_step_reauth())

    _complete_reauth(flow)

    assert hass.config_entries.reloaded == [entry.entry_id], (
        "the re-authenticated entry was never reloaded"
    )


def test_reauth_finishes_by_aborting_as_successful() -> None:
    """Home Assistant closes the repair on this specific reason."""
    flow, _hass, _entry = _reauth_flow()
    _run(flow.async_step_reauth())

    _complete_reauth(flow)

    assert flow.aborted == ["reauth_successful"], (
        f"re-auth ended with {flow.aborted!r}"
    )


def test_reauth_with_still_invalid_tokens_stays_on_the_form() -> None:
    """A rejected token re-prompts rather than updating the entry.

    The user pasting a token that is itself expired is the likely
    mistake here, and writing it over the stored one would replace a
    broken credential with a different broken credential.
    """
    flow, hass, _entry = _reauth_flow()
    _run(flow.async_step_reauth())
    FakeAuthClient.result = config_flow.AuthError("expired")

    result = _complete_reauth(flow, "also-expired", "also-expired")

    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "invalid_token"
    assert hass.config_entries.updated == [], (
        "a rejected token was written over the stored one"
    )


def test_reauth_strips_a_pasted_token_before_validating_it() -> None:
    """Tokens are copied out of a browser, and arrive wrapped.

    The stripping exists in the user step already; this pins it on
    the re-auth path, which is where a hand-pasted token is most
    likely to come from.
    """
    flow, hass, _entry = _reauth_flow()
    _run(flow.async_step_reauth())

    _complete_reauth(flow, '  "new-access"\n', '"new-refresh"  ')

    _updated_entry, data = hass.config_entries.updated[0]
    assert data[const.CONF_ACCESS_TOKEN] == "new-access", (
        f"stored {data[const.CONF_ACCESS_TOKEN]!r} verbatim"
    )
    assert data[const.CONF_REFRESH_TOKEN] == "new-refresh"


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
