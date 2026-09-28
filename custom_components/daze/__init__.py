"""Init for Daze Wallbox integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .api import ApiAuthError, ApiError
from .const import (
    CONF_DEVICE_PROFILE,
    CONF_EVSE_NAME,
    CONF_FIRMWARE_VERSION,
    CONF_NETWORK_UID,
    CONF_SERIAL_NUMBER,
    CONF_SOFTWARE_VERSION,
    DOMAIN,
    PLATFORMS,
    SERVICE_SET_CHARGING_CURRENT,
    SERVICE_START_CHARGE,
    SERVICE_STOP_CHARGE,
)
from .coordinator import DazeDataUpdateCoordinator, async_setup_coordinator

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

    # Store coordinator and API client in hass.data for entity platforms
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "api_client": coordinator.api_client,
        "serial_number": entry.data[CONF_SERIAL_NUMBER],
        "network_uid": entry.data[CONF_NETWORK_UID],
    }

    # Forward setup to entity platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register update listener for config entry changes
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Register services
    _async_register_services(hass, entry, coordinator)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Daze Wallbox config entry."""
    _LOGGER.debug("Unloading Daze Wallbox config entry %s", entry.entry_id)

    # Stop anything the coordinator has scheduled before tearing the
    # entry down. An options change reloads the entry, so without this
    # the old coordinator keeps firing against a closed client.
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if entry_data is not None:
        coordinator: DazeDataUpdateCoordinator = entry_data["coordinator"]
        coordinator.async_shutdown_timers()

    # Unload entity platforms
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )

    if unload_ok:
        # Clean up stored data
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle config entry update (e.g., re-auth token update)."""
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

    async def _handle_start_charge(call: ServiceCall) -> None:
        """Start charging."""
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
