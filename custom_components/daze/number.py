"""Number platform for Daze Wallbox.

Exposes a number entity to set the maximum charging current limit on the
wallbox, in milliamps (mA). Appears as a config entity under the device.
"""

from __future__ import annotations

# Pyright (with homeassistant-stubs) models some entity accessors as
# cached_property fields; Home Assistant integrations override them with
# @property methods by design.
# pyright: reportIncompatibleVariableOverride=false
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.components.number import NumberEntity
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfPower,
)
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
    OPTIMISTIC_STATE_TIMEOUT,
    POST_COMMAND_REFRESH_DELAY,
)
from .coordinator import DazeDataUpdateCoordinator
from .payload import (
    POWER_STEP_W,
    max_charging_current,
    max_charging_power,
    milliamps_to_watts,
    min_charging_current,
    min_charging_power,
    resolve_optimistic,
    watts_to_milliamps,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)

# Neither bound is a constant. The minimum follows the charger's
# power floor and the supply voltage, and the maximum follows the
# installation rating. See min_charging_current and
# max_charging_current.
NATIVE_MAX_VALUE = 32000  # 32 A, used only until the charger reports
NATIVE_STEP = 100  # 0.1 A increments


class DazeWallboxNumberEntity(
    CoordinatorEntity[DazeDataUpdateCoordinator], NumberEntity
):
    """Number entity to set the max charging current on a Daze wallbox."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_step = NATIVE_STEP
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.MILLIAMPERE

    def __init__(
        self,
        coordinator: DazeDataUpdateCoordinator,
        api_client: Any,
        serial_number: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialise the number entity.

        Args:
            coordinator: The Daze data coordinator.
            api_client: The Daze API client.
            serial_number: The wallbox serial number.
            device_info: Device info for the wallbox device registry.

        """
        super().__init__(coordinator)
        self._api_client = api_client
        self._serial_number = serial_number
        self._attr_unique_id = f"{serial_number}_max_charging_current"
        self._attr_device_info = device_info
        self._optimistic_value: int | None = None
        self._optimistic_since: float = 0.0
        self._awaiting_retry: bool = False

    @property
    def native_min_value(self) -> float:
        """Return the lowest current the charger will accept.

        The charger enforces a minimum power rather than a minimum
        current, so this moves with the supply voltage. Offering the
        6 A industry minimum made the bottom of the slider fail with
        MaxExternalChargingCurrentOutOfRange on a 1.5 kW floor.
        """
        return float(min_charging_current(self.coordinator.data))

    @property
    def native_max_value(self) -> float:
        """Return the highest current the charger will accept.

        Advertising the installation rating offers values the charger
        rejects with MaxExternalChargingCurrentOutOfRange. A grid power
        cap can put the real ceiling well below it: a single-phase unit
        behind a 3000 W cap reported a 32 A installation limit but
        refused anything above 11.7 A.
        """
        return float(max_charging_current(self.coordinator.data))

    @property
    def native_value(self) -> int | None:
        """Return the charging current limit in mA.

        Shows the requested value while a change is in flight. The
        charger takes seconds to adopt it, and a background retry can
        take minutes, so reading the last poll would snap the slider
        back to its old position and look like nothing happened.
        """
        value, keep = resolve_optimistic(
            self._optimistic_value, self._reported_value, self._expired
        )

        if not keep:
            self._optimistic_value = None

        return value

    @property
    def _reported_value(self) -> int | None:
        """Return what the charger last reported."""
        if self.coordinator.data is None:
            return None

        for field in (
            "maxExternalChargingCurrentInMilliAmps",
            "lastMaxChargingCurrent",
        ):
            value = self.coordinator.data.get(field)
            if value is not None:
                return int(value)

        return None

    @property
    def _expired(self) -> bool:
        """Whether a pending change has been shown for too long."""
        if self._optimistic_value is None:
            return True

        # While a background retry is still running the request is
        # genuinely outstanding, so keep showing it.
        if self._awaiting_retry:
            return False

        held = time.monotonic() - self._optimistic_since
        return held > OPTIMISTIC_STATE_TIMEOUT

    def _show_requested(self, value: int, awaiting_retry: bool) -> None:
        """Display a requested value and re-read the charger later."""
        self._optimistic_value = value
        self._optimistic_since = time.monotonic()
        self._awaiting_retry = awaiting_retry
        self.async_write_ha_state()
        self.coordinator.async_schedule_refresh_in(POST_COMMAND_REFRESH_DELAY)

    def _clear_requested(self, message: str) -> None:
        """Drop a pending value and explain why."""
        self._optimistic_value = None
        self._awaiting_retry = False
        self.async_write_ha_state()
        self._notify_error(message)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Stop showing the request once the charger reports it."""
        if (
            self._optimistic_value is not None
            and self._reported_value == self._optimistic_value
        ):
            self._optimistic_value = None
            self._awaiting_retry = False

        super()._handle_coordinator_update()

    async def async_set_native_value(self, value: float) -> None:
        """Set the max charging current on the wallbox.

        Skips the API call if the value matches the current reading to
        avoid unnecessary writes.
        """
        int_value = int(value)

        # Skip API call if value hasn't changed
        current = self.native_value
        if current is not None and int_value == current:
            _LOGGER.debug(
                "Set current called with same value %d — skipping",
                int_value,
            )
            return

        try:
            _LOGGER.info(
                "Setting max charging current on %s to %d mA",
                self._serial_number,
                int_value,
            )
            await self._api_client.async_set_max_charging_current(
                self._serial_number,
                int_value,
                attempts=INLINE_COMMAND_ATTEMPTS,
            )
            self._show_requested(int_value, awaiting_retry=False)
        except ApiAuthError as err:
            _LOGGER.warning(
                "Auth error setting max current on %s: %s",
                self._serial_number,
                err,
            )
            self._notify_error(
                "Authentication failed when trying to set the charging "
                "current. Please re-authenticate the integration."
            )
        except ApiCommandRejectedError as err:
            if err.code == COMMAND_ERROR_CODE_RPC_FAILURE:
                # Unreachable rather than refused. Keep trying without
                # making the user wait or telling them it failed.
                self.coordinator.async_retry_in_background(
                    key=f"{self._serial_number}:current",
                    action=lambda: self._api_client.
                    async_set_max_charging_current(
                        self._serial_number,
                        int_value,
                        attempts=INLINE_COMMAND_ATTEMPTS,
                    ),
                    description=f"Setting the charging current to "
                    f"{int_value} mA",
                    on_failure=self._clear_requested,
                )
                self._show_requested(int_value, awaiting_retry=True)
                return

            _LOGGER.info(
                "Charger refused the current change on %s: %s",
                self._serial_number,
                err,
            )
            self._notify_error(
                f"{err} This charger currently accepts "
                f"{min_charging_current(self.coordinator.data)} to "
                f"{max_charging_current(self.coordinator.data)} mA."
            )
        except ApiError as err:
            _LOGGER.warning(
                "API error setting max current on %s: %s",
                self._serial_number,
                err,
            )
            self._notify_error(
                "Failed to set the maximum charging current. "
                f"Error: {err}"
            )

    def _notify_error(self, message: str) -> None:
        """Show a persistent notification in the HA frontend."""
        persistent_notification.async_create(
            self.hass,
            message,
            title="Daze Wallbox — Charging Current Error",
            notification_id=f"daze_number_error_{self._serial_number}",
        )



class DazeWallboxPowerEntity(
    CoordinatorEntity[DazeDataUpdateCoordinator], NumberEntity
):
    """Set the charging limit as a power figure rather than a current.

    The charger's API speaks milliamps, but a wallbox is sold in kW and
    the charger's own minimum is a wattage, so power is what a user
    thinks in. This is a second view of the same setting: changing
    either entity moves the other.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_step = POWER_STEP_W
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(
        self,
        coordinator: DazeDataUpdateCoordinator,
        api_client: Any,
        serial_number: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialise the power entity.

        Args:
            coordinator: The Daze data coordinator.
            api_client: The Daze API client.
            serial_number: The wallbox serial number.
            device_info: Device info for the wallbox device registry.

        """
        super().__init__(coordinator)
        self._api_client = api_client
        self._serial_number = serial_number
        self._attr_unique_id = f"{serial_number}_max_charging_power"
        self._attr_device_info = device_info
        self._optimistic_watts: int | None = None
        self._optimistic_since: float = 0.0
        self._awaiting_retry: bool = False

    @property
    def native_min_value(self) -> float:
        """Return the lowest selectable power."""
        return float(min_charging_power(self.coordinator.data))

    @property
    def native_max_value(self) -> float:
        """Return the highest selectable power."""
        return float(max_charging_power(self.coordinator.data))

    @property
    def native_value(self) -> int | None:
        """Return the configured limit expressed in watts."""
        value, keep = resolve_optimistic(
            self._optimistic_watts, self._reported_watts, self._expired
        )

        if not keep:
            self._optimistic_watts = None

        return value

    @property
    def _reported_watts(self) -> int | None:
        """Return the charger's limit converted to watts."""
        if self.coordinator.data is None:
            return None

        for field in (
            "maxExternalChargingCurrentInMilliAmps",
            "lastMaxChargingCurrent",
        ):
            value = self.coordinator.data.get(field)
            if value is not None:
                return milliamps_to_watts(int(value), self.coordinator.data)

        return None

    @property
    def _expired(self) -> bool:
        """Whether a pending change has been shown for too long."""
        if self._optimistic_watts is None:
            return True
        if self._awaiting_retry:
            return False
        return (
            time.monotonic() - self._optimistic_since
            > OPTIMISTIC_STATE_TIMEOUT
        )

    async def async_set_native_value(self, value: float) -> None:
        """Set the limit from a power figure.

        Converted to the nearest usable current at the charger's
        measured voltage, and clamped to the accepted range so a round
        figure near a boundary is corrected rather than refused.
        """
        milliamps = watts_to_milliamps(value, self.coordinator.data)
        watts = milliamps_to_watts(milliamps, self.coordinator.data)

        current = self.coordinator.data or {}
        if current.get("maxExternalChargingCurrentInMilliAmps") == milliamps:
            _LOGGER.debug(
                "Power set to %d W, already at %d mA, skipping",
                watts,
                milliamps,
            )
            return

        try:
            _LOGGER.info(
                "Setting charging power on %s to %d W (%d mA)",
                self._serial_number,
                watts,
                milliamps,
            )
            await self._api_client.async_set_max_charging_current(
                self._serial_number,
                milliamps,
                attempts=INLINE_COMMAND_ATTEMPTS,
            )
            self._show_requested(watts, awaiting_retry=False)
        except ApiAuthError as err:
            _LOGGER.warning(
                "Auth error setting power on %s: %s", self._serial_number, err
            )
            self._notify_error(
                "Authentication failed when trying to set the charging "
                "power. Please re-authenticate the integration."
            )
        except ApiCommandRejectedError as err:
            if err.code == COMMAND_ERROR_CODE_RPC_FAILURE:
                self.coordinator.async_retry_in_background(
                    key=f"{self._serial_number}:current",
                    action=lambda: self._api_client.
                    async_set_max_charging_current(
                        self._serial_number,
                        milliamps,
                        attempts=INLINE_COMMAND_ATTEMPTS,
                    ),
                    description=f"Setting the charging power to {watts} W",
                    on_failure=self._clear_requested,
                )
                self._show_requested(watts, awaiting_retry=True)
                return

            self._notify_error(
                f"{err} This charger accepts "
                f"{min_charging_power(self.coordinator.data)} to "
                f"{max_charging_power(self.coordinator.data)} W."
            )
        except ApiError as err:
            _LOGGER.warning(
                "API error setting power on %s: %s", self._serial_number, err
            )
            self._notify_error(f"Failed to set the charging power. {err}")

    def _show_requested(self, watts: int, awaiting_retry: bool) -> None:
        """Display a requested power and re-read the charger later."""
        self._optimistic_watts = watts
        self._optimistic_since = time.monotonic()
        self._awaiting_retry = awaiting_retry
        self.async_write_ha_state()
        self.coordinator.async_schedule_refresh_in(POST_COMMAND_REFRESH_DELAY)

    def _clear_requested(self, message: str) -> None:
        """Drop a pending power and explain why."""
        self._optimistic_watts = None
        self._awaiting_retry = False
        self.async_write_ha_state()
        self._notify_error(message)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Stop showing the request once the charger reports it."""
        if (
            self._optimistic_watts is not None
            and self._reported_watts == self._optimistic_watts
        ):
            self._optimistic_watts = None
            self._awaiting_retry = False

        super()._handle_coordinator_update()

    def _notify_error(self, message: str) -> None:
        """Show a persistent notification in the HA frontend."""
        persistent_notification.async_create(
            self.hass,
            message,
            title="Daze Wallbox — Charging Power Error",
            notification_id=f"daze_power_error_{self._serial_number}",
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Daze Wallbox number entity.

    Reads the coordinator, API client, serial number, and device info
    from ``hass.data`` and registers the number entity.
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
            DazeWallboxNumberEntity(
                coordinator=coordinator,
                api_client=api_client,
                serial_number=serial_number,
                device_info=device_info,
            ),
            DazeWallboxPowerEntity(
                coordinator=coordinator,
                api_client=api_client,
                serial_number=serial_number,
                device_info=device_info,
            ),
        ]
    )
