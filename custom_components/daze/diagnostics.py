"""Diagnostics support for the Daze Wallbox integration.

Provides the ``async_get_config_entry_diagnostics`` handler for the HA
diagnostics endpoint. Exposes operational metadata grouped by category
— no secrets, no token strings.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from homeassistant.helpers import entity_registry as er

from .const import COGNITO_BASE_URL, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import DazeDataUpdateCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict:
    """Return diagnostics for a Daze Wallbox config entry.

    Returns serialisable data grouped into categories:
    - ``auth``: token metadata (not the tokens themselves)
    - ``coordinator``: polling status and timing
    - ``entities``: entity inventory by platform
    - ``config_entry``: non-sensitive entry metadata

    Args:
        hass: The HomeAssistant instance.
        entry: The config entry to diagnose.

    Returns:
        A JSON-serialisable dict.

    """
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DazeDataUpdateCoordinator = entry_data["coordinator"]
    api_client = coordinator.api_client
    auth_client = api_client.auth_client

    now = time.time()

    return {
        "auth": {
            "issuer": COGNITO_BASE_URL,
            "token_expired": auth_client.is_token_expired(),
            "token_expiry_timestamp": auth_client.token_expiry,
            "token_age_seconds": (
                now - (auth_client.token_expiry - 3600)
                if auth_client.token_expiry
                else None
            ),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_success_time": coordinator.last_success_time,
            "last_fail_time": coordinator.last_fail_time,
            "total_updates": coordinator.total_updates,
            "consecutive_failures": coordinator.consecutive_failures,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "serial_number": coordinator.serial_number,
            "network_uid": coordinator.network_uid,
        },
        "entities": _async_get_entity_inventory(hass, entry),
        "config_entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "source": str(entry.source),
            "unique_id": entry.unique_id,
            "disabled_by": str(entry.disabled_by) if entry.disabled_by else None,
        },
    }


def _async_get_entity_inventory(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, int]:
    """Count entities by platform for a config entry.

    Args:
        hass: The HomeAssistant instance.
        entry: The config entry to inspect.

    Returns:
        A dict mapping platform names to entity counts.

    """
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)

    inventory: dict[str, int] = {}
    for entity in entities:
        platform = entity.platform
        inventory[platform] = inventory.get(platform, 0) + 1

    return inventory
