"""Select platform for Daze Wallbox.

Exposes a select entity to choose the wallbox operation mode: fast, eco,
or scheduled. Fast mode disables eco mode, eco mode enables it, and
scheduled mode is reserved for future schedule-based control.
"""

from __future__ import annotations

# Pyright (with homeassistant-stubs) models some entity accessors as
# cached_property fields; Home Assistant integrations override them with
# @property methods by design.
# pyright: reportIncompatibleVariableOverride=false
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.components.select import SelectEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import (
    COMMAND_ERROR_CODE_RPC_FAILURE,
    ApiAuthError,
    ApiCommandRejectedError,
    ApiError,
)
from .const import (
    DOMAIN,
    INLINE_COMMAND_ATTEMPTS,
    POST_COMMAND_REFRESH_DELAY,
)
from .coordinator import DazeDataUpdateCoordinator
from .optimistic import OptimisticState
from .payload import charger_offline_reason

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)

# Operation mode options exposed in the HA frontend
OPTION_FAST = "fast"
OPTION_ECO = "eco"
OPTION_SCHEDULED = "scheduled"

ATTR_OPTIONS = [OPTION_FAST, OPTION_ECO, OPTION_SCHEDULED]


def _current_option_from_data(data: dict[str, Any]) -> str:
    """Derive the current operation mode from coordinator data.

    Uses ``ecoModeEnabled`` and ``operationMode`` fields to determine
    the current mode:
    - ``ecoModeEnabled`` is True → eco mode
    - ``operationMode`` may indicate scheduled or fast otherwise

    Falls back to "fast" if no data is available.
    """
    eco_enabled = data.get("ecoModeEnabled")
    if eco_enabled is True:
        return OPTION_ECO

    mode = data.get("operationMode")
    if mode is not None:
        mode_str = str(mode).lower()
        if mode_str in ATTR_OPTIONS:
            return mode_str

    return OPTION_FAST


_MODE_TO_ECO: dict[str, bool | None] = {
    OPTION_FAST: False,
    OPTION_ECO: True,
    OPTION_SCHEDULED: None,  # not yet mapped to an API call
}


class DazeWallboxSelectEntity(
    CoordinatorEntity[DazeDataUpdateCoordinator], SelectEntity
):
    """Select entity to choose the Daze wallbox operation mode."""

    _attr_has_entity_name = True
    _attr_options = ATTR_OPTIONS

    def __init__(
        self,
        coordinator: DazeDataUpdateCoordinator,
        api_client: Any,
        serial_number: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialise the select entity.

        Args:
            coordinator: The Daze data coordinator.
            api_client: The Daze API client.
            serial_number: The wallbox serial number.
            device_info: Device info for the wallbox device registry.

        """
        super().__init__(coordinator)
        self._api_client = api_client
        self._serial_number = serial_number
        self._attr_unique_id = f"{serial_number}_operation_mode"
        self._attr_device_info = device_info
        self._optimistic = OptimisticState(tolerance=0)

    @property
    def current_option(self) -> str | None:
        """Return the current operation mode.

        Shows the requested mode while a change is in flight, for the
        same reason as the current limit: the charger lags, and a
        background retry can take minutes, so reading the last poll
        would revert the selection and look like nothing happened.
        """
        return self._optimistic.resolve(self._reported_option)

    @property
    def _reported_option(self) -> str | None:
        """Return the mode the charger last reported."""
        if self.coordinator.data is None:
            return None
        return _current_option_from_data(self.coordinator.data)

    def _show_requested(self, option: str, awaiting_retry: bool) -> None:
        """Display a requested mode and re-read the charger later."""
        self._optimistic.request(option, awaiting_retry)
        self.async_write_ha_state()
        self.coordinator.async_schedule_refresh_in(POST_COMMAND_REFRESH_DELAY)

    def _clear_requested(self, message: str) -> None:
        """Drop a pending mode and explain why."""
        self._optimistic.clear()
        self.async_write_ha_state()
        self._notify_error(message)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Stop showing the request once the charger reports it."""
        self._optimistic.settle(self._reported_option)
        super()._handle_coordinator_update()

    async def async_select_option(self, option: str) -> None:
        """Set the operation mode on the wallbox.

        Maps the selected option to the appropriate API call:
        - "fast" → disables eco mode
        - "eco" → enables eco mode
        - "scheduled" → currently unsupported, shows notification
        """
        if option not in ATTR_OPTIONS:
            _LOGGER.warning(
                "Unknown operation mode option: %s", option
            )
            return

        eco_value = _MODE_TO_ECO.get(option)
        if eco_value is None:
            # "scheduled" mode — not yet supported via API
            _LOGGER.warning(
                "Scheduled operation mode is not yet supported via the "
                "Daze API on wallbox %s",
                self._serial_number,
            )
            self._notify_error(
                "Scheduled operation mode is not yet supported via the "
                "Daze API. Please use 'Fast' or 'Eco' mode."
            )
            return

        offline = charger_offline_reason(self.coordinator.data)
        if offline is not None:
            _LOGGER.info("Not sending: %s", offline)
            self._notify_error(
                f"The command was not sent because {offline}. "
                "Check that the wallbox has power."
            )
            return

        try:
            _LOGGER.info(
                "Setting operation mode to '%s' on wallbox %s "
                "(ecoModeEnabled=%s)",
                option,
                self._serial_number,
                eco_value,
            )
            await self._api_client.async_set_eco_mode(
                self._serial_number,
                eco_value,
                attempts=INLINE_COMMAND_ATTEMPTS,
            )
            self.coordinator.async_cancel_background_retry(
                f"{self._serial_number}:mode"
            )
            self._show_requested(option, awaiting_retry=False)
        except ApiAuthError as err:
            _LOGGER.warning(
                "Auth error setting operation mode on %s: %s",
                self._serial_number,
                err,
            )
            self._notify_error(
                "Authentication failed when trying to change the "
                "operation mode. Please re-authenticate the integration."
            )
        except ApiCommandRejectedError as err:
            if err.code == COMMAND_ERROR_CODE_RPC_FAILURE:
                self.coordinator.async_retry_in_background(
                    key=f"{self._serial_number}:mode",
                    action=lambda: self._api_client.async_set_eco_mode(
                        self._serial_number,
                        eco_value,
                        attempts=INLINE_COMMAND_ATTEMPTS,
                    ),
                    description=f"Setting the operation mode to {option}",
                    on_failure=self._clear_requested,
                )
                self._show_requested(option, awaiting_retry=True)
                return

            _LOGGER.info(
                "Charger refused the mode change on %s: %s",
                self._serial_number,
                err,
            )
            self._notify_error(str(err))
        except ApiError as err:
            _LOGGER.warning(
                "API error setting operation mode on %s: %s",
                self._serial_number,
                err,
            )
            self._notify_error(
                "Failed to change the operation mode. "
                f"Error: {err}"
            )

    def _notify_error(self, message: str) -> None:
        """Show a persistent notification in the HA frontend."""
        persistent_notification.async_create(
            self.hass,
            message,
            title="Daze Wallbox — Operation Mode Error",
            notification_id=f"daze_select_error_{self._serial_number}",
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Daze Wallbox select entity.

    Reads the coordinator, API client, serial number, and device info
    from ``hass.data`` and registers the select entity.
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
            DazeWallboxSelectEntity(
                coordinator=coordinator,
                api_client=api_client,
                serial_number=serial_number,
                device_info=device_info,
            )
        ]
    )
