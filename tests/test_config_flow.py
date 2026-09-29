"""Tests for the options flow against a stubbed Home Assistant.

Follows the same approach as test_entities.py, test_solar_controller.py
and test_init_entry.py: the real config_flow.py is imported and
exercised, with Home Assistant and voluptuous replaced by the smallest
stubs the module actually touches.

Run with pytest, or standalone:

    python3 tests/test_config_flow.py
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

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

    import asyncio

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

    import asyncio

    result = asyncio.run(handler.async_step_init({"poll_interval": 60}))

    assert result["data"]["poll_interval"] == 60


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
