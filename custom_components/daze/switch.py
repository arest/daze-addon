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
        self._optimistic = OptimisticState(tolerance=0)

    @property
    def is_on(self) -> bool | None:
        """Return True while a charge is authorised and under way.

        Includes the waiting-for-EV state. The charger passes through
        it after a start takes effect, before the car begins drawing.

        Immediately after a command, the commanded value is reported
        instead of the charger's reading. The cloud API takes several
        seconds to reflect a change, so reporting the reading during
        that window shows the old state and makes the toggle flip back.
        """
        actual = (
            is_charge_enabled(self.coordinator.data)
            if self.coordinator.data is not None
            else None
        )

        return self._optimistic.resolve(actual)

    @property
    def assumed_state(self) -> bool:
        """Tell the frontend when the shown state is a guess."""
        return self._optimistic.pending

    def _set_optimistic(
        self, value: bool, awaiting_retry: bool = False
    ) -> None:
        """Show the commanded state now and re-read the charger later.

        Refreshing immediately is worse than not refreshing at all: the
        cloud still reports the old state, so the entity would flip
        back before settling.
        """
        self._optimistic.request(value, awaiting_retry)
        self.async_write_ha_state()
        self.coordinator.async_schedule_refresh_in(
            POST_COMMAND_REFRESH_DELAY
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Drop the guess once the charger agrees with it."""
        actual = (
            is_charge_enabled(self.coordinator.data)
            if self.coordinator.data is not None
            else None
        )
        self._optimistic.settle(actual)
        super()._handle_coordinator_update()

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
            # No session ID passed: the client reads a current one.
            # The coordinator's copy can name a session that has ended.
            await self._api_client.async_start_charge(
                self._serial_number, attempts=INLINE_COMMAND_ATTEMPTS
            )
            # Supersede any queued retry, or it would re-apply the
            # opposite command minutes from now.
            self.coordinator.async_cancel_background_retry(
                f"{self._serial_number}:charge"
            )
            self._set_optimistic(True)
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
            if self._retry_in_background(err, True):
                return
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
                self._serial_number, attempts=INLINE_COMMAND_ATTEMPTS
            )
            self.coordinator.async_cancel_background_retry(
                f"{self._serial_number}:charge"
            )
            self._set_optimistic(False)
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
            if self._retry_in_background(err, False):
                return
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

    def _retry_in_background(
        self, err: ApiCommandRejectedError, turn_on: bool
    ) -> bool:
        """Queue a retry when the charger was unreachable.

        A refusal is final and should be shown. An unreachable RPC
        link is not: the same command usually lands a minute later,
        so it is retried without troubling the user.

        Returns:
            True if the command was handed to the background.

        """
        if err.code != COMMAND_ERROR_CODE_RPC_FAILURE:
            return False

        verb = "Starting" if turn_on else "Stopping"
        command = (
            self._api_client.async_start_charge
            if turn_on
            else self._api_client.async_stop_charge
        )

        self.coordinator.async_retry_in_background(
            key=f"{self._serial_number}:charge",
            action=lambda: command(
                self._serial_number, attempts=INLINE_COMMAND_ATTEMPTS
            ),
            description=f"{verb} the charge",
            on_failure=self._notify_error,
        )

        # Show the intent while the retries run.
        self._set_optimistic(turn_on, awaiting_retry=True)
        return True

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
