"""Switch platform for Daze Wallbox.

Exposes a switch entity to start and stop charging. The switch state
reflects the live ``evseStatus`` field from the coordinator data:
charging → on, all others → off.
"""

from __future__ import annotations

# Pyright (with homeassistant-stubs) models some entity accessors as
# cached_property fields; Home Assistant integrations override them with
# @property methods by design.
# pyright: reportIncompatibleVariableOverride=false
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ApiAuthError, ApiCommandRejectedError, ApiError
from .const import DOMAIN
from .coordinator import DazeDataUpdateCoordinator
from .payload import is_charge_enabled

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)

CHARGING_STATE = "charging"


class DazeWallboxSwitchEntity(
    CoordinatorEntity[DazeDataUpdateCoordinator], SwitchEntity
):
    """Switch to start/stop charging on a Daze wallbox."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DazeDataUpdateCoordinator,
        api_client: Any,
        serial_number: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialise the switch entity.

        Args:
            coordinator: The Daze data coordinator.
            api_client: The Daze API client.
            serial_number: The wallbox serial number.
            device_info: Device info for the wallbox device registry.

        """
        super().__init__(coordinator)
        self._api_client = api_client
        self._serial_number = serial_number
        self._attr_unique_id = f"{serial_number}_charge_switch"
        self._attr_device_info = device_info

    @property
    def _session_id(self) -> int | None:
        """Return the current charge session ID, if one is open.

        The play and stop commands act on a session and must name
        it; without it the API answers 422 ErrorWrongSessionID.
        """
        if self.coordinator.data is None:
            return None
        session_id = self.coordinator.data.get("sessionId")
        return session_id if isinstance(session_id, int) else None

    @property
    def is_on(self) -> bool | None:
        """Return True while a charge is authorised and under way.

        Includes the waiting-for-EV state. The charger passes through
        it after a start takes effect, before the car begins drawing.
        Reporting off there would make the toggle snap back moments
        after the user switched it on, even though the command worked.
        """
        if self.coordinator.data is None:
            return None
        return is_charge_enabled(self.coordinator.data)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start charging on the wallbox."""
        # Already charging — idempotent no-op
        if self.is_on:
            _LOGGER.debug(
                "Switch turn_on called but already charging — skipping"
            )
            return

        try:
            _LOGGER.info(
                "Starting charge on wallbox %s", self._serial_number
            )
            await self._api_client.async_start_charge(
                self._serial_number, self._session_id
            )
            await self.coordinator.async_request_refresh()
            self.coordinator.async_schedule_settle_refresh()
        except ApiAuthError as err:
            _LOGGER.warning(
                "Auth error starting charge on %s: %s",
                self._serial_number,
                err,
            )
            self._notify_error(
                "Authentication failed when trying to start charging. "
                "Please re-authenticate the integration."
            )
        except ApiCommandRejectedError as err:
            # The charger explained why; relay that rather
            # than the stock 'check the car is connected'.
            _LOGGER.info(
                "Charger refused the command on %s: %s",
                self._serial_number,
                err,
            )
            self._notify_error(str(err))
        except ApiError as err:
            _LOGGER.warning(
                "API error starting charge on %s: %s",
                self._serial_number,
                err,
            )
            self._notify_error(
                "Failed to start charging. "
                "Check that the car is connected and try again. "
                f"Error: {err}"
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop charging on the wallbox."""
        # Already stopped — idempotent no-op
        if self.is_on is False or self.is_on is None:
            _LOGGER.debug(
                "Switch turn_off called but not charging — skipping"
            )
            return

        try:
            _LOGGER.info(
                "Stopping charge on wallbox %s", self._serial_number
            )
            await self._api_client.async_stop_charge(
                self._serial_number, self._session_id
            )
            await self.coordinator.async_request_refresh()
            self.coordinator.async_schedule_settle_refresh()
        except ApiAuthError as err:
            _LOGGER.warning(
                "Auth error stopping charge on %s: %s",
                self._serial_number,
                err,
            )
            self._notify_error(
                "Authentication failed when trying to stop charging. "
                "Please re-authenticate the integration."
            )
        except ApiCommandRejectedError as err:
            # The charger explained why; relay that rather
            # than the stock 'check the car is connected'.
            _LOGGER.info(
                "Charger refused the command on %s: %s",
                self._serial_number,
                err,
            )
            self._notify_error(str(err))
        except ApiError as err:
            _LOGGER.warning(
                "API error stopping charge on %s: %s",
                self._serial_number,
                err,
            )
            self._notify_error(
                "Failed to stop charging. "
                f"Error: {err}"
            )

    def _notify_error(self, message: str) -> None:
        """Show a persistent notification in the HA frontend."""
        persistent_notification.async_create(
            self.hass,
            message,
            title="Daze Wallbox — Charge Control Error",
            notification_id=f"daze_switch_error_{self._serial_number}",
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Daze Wallbox switch entity.

    Reads the coordinator, API client, serial number, and device info
    from ``hass.data`` and registers the switch.
    """
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DazeDataUpdateCoordinator = entry_data["coordinator"]
    api_client = entry_data["api_client"]
    serial_number: str = entry_data["serial_number"]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, serial_number)},
    )

    async_add_entities(
        [
            DazeWallboxSwitchEntity(
                coordinator=coordinator,
                api_client=api_client,
                serial_number=serial_number,
                device_info=device_info,
            )
        ]
    )
