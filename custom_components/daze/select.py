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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
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


SOLAR_MODE_OPTIONS = ["off", "simulate", "active"]


class DazeSolarControlSelect(
    CoordinatorEntity[DazeDataUpdateCoordinator], SelectEntity, RestoreEntity
):
    """Arm solar control, in simulation or for real.

    A single tri-state rather than a switch plus a dry-run flag, so the
    meaningless combination cannot be selected.
    """

    _attr_has_entity_name = True
    _attr_options = SOLAR_MODE_OPTIONS

    def __init__(
        self,
        coordinator: DazeDataUpdateCoordinator,
        controller: Any,
        serial_number: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialise the control.

        Args:
            coordinator: The Daze data coordinator.
            controller: The solar controller to drive.
            serial_number: The wallbox serial number.
            device_info: Device info for the device registry.

        """
        super().__init__(coordinator)
        self._controller = controller
        self._serial_number = serial_number
        self._attr_unique_id = f"{serial_number}_solar_control"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Redraw when the controller decides something, and remember
        the mode across a restart.

        Restoring writes straight to the controller rather than
        through async_select_option, so it cannot raise at startup: a
        setup that is temporarily unsupported — the charger has not
        polled yet, say — must come back as the user left it and be
        refused later by the guard in the tick (_async_evaluate's own
        stand-down), not lose the setting because of a race with the
        first refresh.

        No stored state at all is a different case from a restart: it
        is this select existing for the first time, which the spec's
        Rollout section calls "first enable" and asks to land in
        simulate, not active — the controller's own constructor
        default of off is what a fresh install shows before this
        entity has ever run once.
        """
        await super().async_added_to_hass()
        self.async_on_remove(
            self._controller.add_listener(self.async_write_ha_state)
        )

        from .solar_controller import SolarMode

        last = await self.async_get_last_state()
        if last is not None and last.state in SOLAR_MODE_OPTIONS:
            self._controller.mode = SolarMode(last.state)
        elif last is None:
            self._controller.mode = SolarMode.SIMULATE

    @property
    def available(self) -> bool:
        """Usable only where solar control could actually run."""
        return self._controller.unsupported_reason is None

    @property
    def current_option(self) -> str | None:
        """Return the controller's mode."""
        mode = self._controller.mode
        return mode.value if mode is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the last decision, so the feature can be understood."""
        decision = self._controller.last_decision
        return {
            "surplus_w": self._controller.surplus_w,
            "last_action": decision.action.value if decision else None,
            "last_reason": decision.reason if decision else None,
        }

    async def async_select_option(self, option: str) -> None:
        """Set the mode, refusing to arm where it cannot work.

        `available` is a hint for the dashboard. A service call or an
        automation arrives here whatever the entity reports, so the
        refusal has to be enforced in the method that acts — and
        raised, not logged, because the caller asked for something and
        is entitled to know it did not happen, and why.
        """
        from .solar_controller import SolarMode

        if option != "off":
            reason = self._controller.unsupported_reason
            if reason is not None:
                raise HomeAssistantError(
                    f"Solar control cannot be armed: {reason}."
                )

        self._controller.mode = SolarMode(option)
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Daze Wallbox select entities.

    Reads the coordinator, API client, serial number, and device info
    from ``hass.data`` and registers the select entities.
    """
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DazeDataUpdateCoordinator = entry_data["coordinator"]
    api_client = entry_data["api_client"]
    serial_number: str = entry_data["serial_number"]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, serial_number)},
    )

    entities: list[SelectEntity] = [
        DazeWallboxSelectEntity(
            coordinator=coordinator,
            api_client=api_client,
            serial_number=serial_number,
            device_info=device_info,
        )
    ]

    solar_controller = entry_data.get("solar_controller")
    if solar_controller is not None:
        entities.append(
            DazeSolarControlSelect(
                coordinator=coordinator,
                controller=solar_controller,
                serial_number=serial_number,
                device_info=device_info,
            )
        )

    async_add_entities(entities)
