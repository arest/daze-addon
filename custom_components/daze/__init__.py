"""Init for Daze Wallbox integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .api import ApiAuthError, ApiError
from .const import (
    CONF_DEVICE_PROFILE,
    CONF_EVSE_NAME,
    CONF_FIRMWARE_VERSION,
    CONF_GRID_POWER_SENSOR,
    CONF_NETWORK_UID,
    CONF_SERIAL_NUMBER,
    CONF_SOFTWARE_VERSION,
    CONF_SOLAR_RESERVE,
    CONF_SUPPLY_PHASES,
    DEFAULT_SOLAR_RESERVE,
    DOMAIN,
    PLATFORMS,
    SERVICE_SET_CHARGING_CURRENT,
    SERVICE_START_CHARGE,
    SERVICE_STOP_CHARGE,
)
from .coordinator import DazeDataUpdateCoordinator, async_setup_coordinator
from .payload import charger_offline_reason
from .solar_controller import SolarController

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Service definitions
# ------------------------------------------------------------------

SET_CHARGING_CURRENT_SCHEMA = vol.Schema({
    vol.Required("current"): vol.All(
        cv.positive_int, vol.Range(min=6000, max=32000)
    ),
})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Daze Wallbox from a config entry.

    Creates the DataUpdateCoordinator, registers the wallbox device,
    and forwards setup to all entity platforms.
    """
    _LOGGER.debug("Setting up Daze Wallbox config entry %s", entry.entry_id)

    # Create coordinator (also performs first refresh)
    coordinator = await async_setup_coordinator(hass, entry)

    # Register the wallbox device in the device registry
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data[CONF_SERIAL_NUMBER])},
        manufacturer="Daze",
        model=entry.data.get(CONF_DEVICE_PROFILE),
        name=entry.data.get(CONF_EVSE_NAME, "Daze Wallbox"),
        sw_version=entry.data.get(CONF_SOFTWARE_VERSION),
        hw_version=entry.data.get(CONF_FIRMWARE_VERSION),
        configuration_url="https://webportal.dazeservice.com",
    )

    solar_controller = SolarController(
        hass=hass,
        coordinator=coordinator,
        grid_power_entity=entry.options.get(CONF_GRID_POWER_SENSOR),
        reserve_w=entry.options.get(
            CONF_SOLAR_RESERVE, DEFAULT_SOLAR_RESERVE
        ),
        supply_phases=entry.options.get(CONF_SUPPLY_PHASES),
    )
    # The entities reach the controller through the coordinator, which
    # every one of them already holds.
    coordinator.solar_controller = solar_controller
    await solar_controller.async_start()

    # Store coordinator and API client in hass.data for entity platforms
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "api_client": coordinator.api_client,
        "serial_number": entry.data[CONF_SERIAL_NUMBER],
        "network_uid": entry.data[CONF_NETWORK_UID],
        "solar_controller": solar_controller,
        "reload_signature": _reload_signature(entry),
    }

    # Forward setup to entity platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register update listener for config entry changes
    entry.add_update_listener(_async_update_listener)

    # Register services
    _async_register_services(hass, entry, coordinator)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Daze Wallbox config entry."""
    _LOGGER.debug("Unloading Daze Wallbox config entry %s", entry.entry_id)

    # Unload entity platforms
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )

    if unload_ok:
        # Stop anything the coordinator has scheduled. An options
        # change reloads the entry, so without this the old
        # coordinator keeps firing against a closed client.
        #
        # Only once the unload has actually succeeded: a refused
        # unload leaves the entry running with this same coordinator,
        # and tearing down its timers and listeners would leave it
        # alive but inert.
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if entry_data is not None:
            controller = entry_data.get("solar_controller")
            if controller is not None:
                await controller.async_stop()

            coordinator: DazeDataUpdateCoordinator = entry_data["coordinator"]
            coordinator.async_shutdown_timers()

        # Clean up stored data. DOMAIN itself may be absent — setup can
        # raise before hass.data.setdefault(DOMAIN, {}) ever runs, and
        # this same function is what tears down after that failure —
        # so indexing hass.data[DOMAIN] directly would raise KeyError
        # here instead of finishing the unload.
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    return unload_ok


def _reload_signature(entry: ConfigEntry) -> tuple[Any, Any]:
    """Return the parts of an entry whose change needs a reload.

    The solar reserve is deliberately absent. It is applied live by the
    controller, so rewriting it is not a reason to rebuild the entry;
    everything else — credentials, the poll interval, the signed grid-power
    sensor and supply phases the controller is constructed with — is.
    """
    options = {
        key: value
        for key, value in entry.options.items()
        if key != CONF_SOLAR_RESERVE
    }
    return (dict(entry.data), options)


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle config entry update (e.g., re-auth token update)."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    signature = _reload_signature(entry)

    if entry_data is not None and entry_data.get("reload_signature") == (
        signature
    ):
        _LOGGER.debug(
            "Config entry %s changed in a way that needs no reload",
            entry.entry_id,
        )
        return

    _LOGGER.debug("Config entry updated for %s — reloading", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)


# ------------------------------------------------------------------
# Service handlers
# ------------------------------------------------------------------


def _async_register_services(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: DazeDataUpdateCoordinator,
) -> None:
    """Register HA services for the Daze Wallbox integration.

    Registers domain-level services that act on the configured wallbox.
    Since each HA instance manages at most one Daze wallbox, these
    services do not require entity targeting.
    """
    api_client = coordinator.api_client
    serial_number = coordinator.serial_number

    def _refuse_if_offline() -> None:
        """Stop a service call that cannot reach the charger.

        The entities check this before sending. Without the same check
        here an automation gets the long retry and the misleading
        service-outage error the guard was written to replace.
        """
        reason = charger_offline_reason(coordinator.data)
        if reason is not None:
            raise HomeAssistantError(
                f"The command was not sent because {reason}. "
                "Check that the wallbox has power."
            )

    async def _handle_start_charge(call: ServiceCall) -> None:
        """Start charging."""
        if coordinator.solar_controller is not None:
            coordinator.solar_controller.disarm(
                "the charge was started by a service call"
            )
        _refuse_if_offline()

        try:
            await api_client.async_start_charge(serial_number)
            await coordinator.async_request_refresh()
            coordinator.async_schedule_settle_refresh()
        except ApiAuthError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed when starting charge. "
                "Please re-authenticate the Daze integration."
            ) from err
        except ApiError as err:
            raise HomeAssistantError(
                f"Failed to start charging: {err}"
            ) from err

    async def _handle_stop_charge(call: ServiceCall) -> None:
        """Stop charging."""
        if coordinator.solar_controller is not None:
            coordinator.solar_controller.disarm(
                "the charge was stopped by a service call"
            )
        _refuse_if_offline()

        try:
            await api_client.async_stop_charge(serial_number)
            await coordinator.async_request_refresh()
            coordinator.async_schedule_settle_refresh()
        except ApiAuthError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed when stopping charge. "
                "Please re-authenticate the Daze integration."
            ) from err
        except ApiError as err:
            raise HomeAssistantError(
                f"Failed to stop charging: {err}"
            ) from err

    async def _handle_set_charging_current(call: ServiceCall) -> None:
        """Set the maximum charging current."""
        current: int = call.data["current"]
        if coordinator.solar_controller is not None:
            coordinator.solar_controller.disarm(
                "the charging current was set by a service call"
            )
        _refuse_if_offline()

        try:
            await api_client.async_set_max_charging_current(
                serial_number, current
            )
            await coordinator.async_request_refresh()
            coordinator.async_schedule_settle_refresh()
        except ApiAuthError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed when setting charging current. "
                "Please re-authenticate the Daze integration."
            ) from err
        except ApiError as err:
            raise HomeAssistantError(
                f"Failed to set charging current: {err}"
            ) from err

    # Register each service with cleanup on config entry unload
    entry.async_on_unload(
        hass.services.async_register(
            DOMAIN,
            SERVICE_START_CHARGE,
            _handle_start_charge,
            schema=vol.Schema({}),
        )
    )
    entry.async_on_unload(
        hass.services.async_register(
            DOMAIN,
            SERVICE_STOP_CHARGE,
            _handle_stop_charge,
            schema=vol.Schema({}),
        )
    )
    entry.async_on_unload(
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_CHARGING_CURRENT,
            _handle_set_charging_current,
            schema=SET_CHARGING_CURRENT_SCHEMA,
        )
    )

    _LOGGER.debug(
        "Registered Daze services for entry %s", entry.entry_id
    )
