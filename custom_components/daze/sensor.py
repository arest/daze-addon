"""Sensor platform for Daze Wallbox.

Exposes wallbox metrics as Home Assistant sensor entities with correct
device classes, state classes, and units of measurement.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DazeDataUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


# ------------------------------------------------------------------
# EVSE status mapping
# ------------------------------------------------------------------

EVSE_STATUS_MAP: dict[str, str] = {
    "idle": "idle",
    "charging": "charging",
    "paused": "paused",
    "error": "error",
    "offline": "offline",
    # Potential API values that map to the same HA states
    "waiting_for_car": "idle",
    "waiting_for_charge": "idle",
    "play_charge": "charging",
    "pause_charge": "paused",
    "stop_charge": "idle",
}


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------


def _get_evse_status(data: dict[str, Any]) -> str | None:
    """Map raw EVSE status to a human-readable HA state."""
    raw = data.get("evseStatus")
    if raw is None:
        return None
    return EVSE_STATUS_MAP.get(str(raw).lower(), str(raw).lower())





# ------------------------------------------------------------------
# Sensor description
# ------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class DazeSensorEntityDescription(SensorEntityDescription):
    """Description for a Daze wallbox sensor.

    Extends SensorEntityDescription with a ``value_fn`` callback that
    extracts the sensor value from the coordinator data dict.
    """

    value_fn: Callable[[dict[str, Any]], Any | None] = lambda data: None


# ------------------------------------------------------------------
# Helper functions used by sensor definitions
# ------------------------------------------------------------------


_SCHEDULED_CHARGE_KEYS = (
    "nextScheduledCharge",
    "scheduledChargeTime",
    "scheduledStart",
    "scheduleTime",
)
"""Possible API field names for scheduled charge time.

Checked in order — the first non-None value wins.
"""


def _get_next_scheduled_charge(data: dict[str, Any]) -> Any | None:
    """Extract the next scheduled charge time from coordinator data.

    Tries multiple possible API field names to accommodate variations
    in the Daze API response. Returns None if no scheduling data is
    available (scheduling not active or not supported).
    """
    for key in _SCHEDULED_CHARGE_KEYS:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _presence_on_off(data: dict[str, Any], key: str) -> str | None:
    """Return 'on' or 'off' for a boolean diagnostic field."""
    val = data.get(key)
    if val is None:
        return None
    return "on" if bool(val) else "off"


# ------------------------------------------------------------------
# Sensor definitions
# ------------------------------------------------------------------

SENSORS: tuple[DazeSensorEntityDescription, ...] = (
    # --- Measurement sensors ---
    # NOTE: Field names match the raw API response keys since the
    # coordinator flattens chargeSession into the top-level data dict.
    DazeSensorEntityDescription(
        key="instant_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda data: data.get("instantPowerAsWatt"),
    ),
    DazeSensorEntityDescription(
        key="delivered_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        value_fn=lambda data: data.get("deliveredEnergyAsWattHour"),
    ),
    DazeSensorEntityDescription(
        key="charging_current_l1",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        value_fn=lambda data: data.get("lastChargingCurrentInstantL1"),
    ),
    DazeSensorEntityDescription(
        key="charging_current_l2",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        value_fn=lambda data: data.get("lastChargingCurrentInstantL2"),
    ),
    DazeSensorEntityDescription(
        key="charging_current_l3",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        value_fn=lambda data: data.get("lastChargingCurrentInstantL3"),
    ),
    DazeSensorEntityDescription(
        key="ac_voltage_l1",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=lambda data: data.get("lastACVoltageL1"),
    ),
    DazeSensorEntityDescription(
        key="ac_voltage_l2",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=lambda data: data.get("lastACVoltageL2"),
    ),
    DazeSensorEntityDescription(
        key="ac_voltage_l3",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=lambda data: data.get("lastACVoltageL3"),
    ),
    DazeSensorEntityDescription(
        key="board_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda data: data.get("boardTemperature"),
    ),
    DazeSensorEntityDescription(
        key="case_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda data: data.get("caseTemperature"),
    ),
    # --- EVSE status sensor ---
    DazeSensorEntityDescription(
        key="evse_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(EVSE_STATUS_MAP.values()),
        value_fn=_get_evse_status,
    ),
    # --- Diagnostic sensors (network-level) ---
    DazeSensorEntityDescription(
        key="grid_max_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("gridMaxPower"),
    ),
    DazeSensorEntityDescription(
        key="is_photovoltaic",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=["on", "off"],
        value_fn=lambda data: _presence_on_off(data, "is_photovoltaic"),
    ),
    DazeSensorEntityDescription(
        key="is_three_phase",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=["on", "off"],
        value_fn=lambda data: _presence_on_off(data, "evseIsThreePhase"),
    ),
    # --- Session sensors ---
    DazeSensorEntityDescription(
        key="last_session_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        value_fn=lambda data: data.get("last_session_energy"),
    ),
    DazeSensorEntityDescription(
        key="last_session_duration",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda data: data.get("last_session_duration"),
    ),
    DazeSensorEntityDescription(
        key="last_session_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        value_fn=lambda data: data.get("last_session_cost"),
    ),
    DazeSensorEntityDescription(
        key="last_session_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.get("last_session_start"),
    ),
    DazeSensorEntityDescription(
        key="last_session_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.get("last_session_end"),
    ),
    # --- Aggregate counters ---
    DazeSensorEntityDescription(
        key="lifetime_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        value_fn=lambda data: data.get("lifetime_energy"),
    ),
    DazeSensorEntityDescription(
        key="total_sessions",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.get("total_sessions"),
    ),
    # --- Diagnostic sensors ---
    DazeSensorEntityDescription(
        key="next_scheduled_charge",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_get_next_scheduled_charge,
    ),
)


# ------------------------------------------------------------------
# Sensors that restore state on HA restart
# ------------------------------------------------------------------

_RESTORE_STATE_KEYS: frozenset[str] = frozenset({
    "delivered_energy",
    "lifetime_energy",
    "total_sessions",
    "last_session_energy",
    "last_session_cost",
    "last_session_duration",
})
"""Sensor keys whose native_value should survive HA restarts.

These are cumulative or infrequently-changing values where losing the
last known state would cause visible gaps in history or dashboards.
"""


# ------------------------------------------------------------------
# Base sensor entity
# ------------------------------------------------------------------


class DazeWallboxSensorEntity(
    CoordinatorEntity[DazeDataUpdateCoordinator], RestoreEntity, SensorEntity
):
    """Base sensor entity for Daze Wallbox metrics.

    All wallbox sensor entities inherit from this class. It provides:
    - ``device_info`` from the registered wallbox device
    - ``_attr_has_entity_name`` so HA prefixes the device name
    - Automatic ``available`` propagation via ``CoordinatorEntity``
    - State restoration for cumulative sensors via ``RestoreEntity``
    """

    entity_description: DazeSensorEntityDescription
    _attr_has_entity_name = True
    _restored_value: Any | None = None

    def __init__(
        self,
        coordinator: DazeDataUpdateCoordinator,
        description: DazeSensorEntityDescription,
        device_info: DeviceInfo,
    ) -> None:
        """Initialise the sensor entity.

        Args:
            coordinator: The Daze data coordinator.
            description: The sensor entity description.
            device_info: Device info for the wallbox device registry.

        """
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.serial_number}_{description.key}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Restore last known state on HA restart.

        Only meaningful for cumulative sensors listed in
        ``_RESTORE_STATE_KEYS``. If the coordinator already returned
        data before this entity is added, restoration is skipped.
        """
        await super().async_added_to_hass()

        # Only restore for sensors that need it
        if self.entity_description.key not in _RESTORE_STATE_KEYS:
            return

        # Coordinator already has data — no need to restore
        if self.coordinator.data is not None:
            return

        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return

        try:
            # Preserve the state as a float for numeric sensors
            self._restored_value = float(last_state.state)
        except (ValueError, TypeError):
            self._restored_value = last_state.state

    @property
    def native_value(self) -> Any | None:
        """Return the sensor value from the latest coordinator data.

        Falls back to the restored value (from before HA restart) when
        coordinator data is temporarily unavailable, so cumulative
        sensors don't show None during startup delays.
        """
        if self.coordinator.data is not None:
            return self.entity_description.value_fn(self.coordinator.data)
        # Fall back to restored state if available
        if self._restored_value is not None:
            return self._restored_value
        return None


# ------------------------------------------------------------------
# Platform setup
# ------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Daze Wallbox sensor entities.

    Reads the coordinator and device info from ``hass.data``, then
    creates and registers all sensor entities.
    """
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DazeDataUpdateCoordinator = entry_data["coordinator"]
    serial_number: str = entry_data["serial_number"]

    # Build device info matching the device registered in __init__.py
    device_info = DeviceInfo(
        identifiers={(DOMAIN, serial_number)},
    )

    entities: list[DazeWallboxSensorEntity] = []

    for description in SENSORS:
        entities.append(
            DazeWallboxSensorEntity(
                coordinator=coordinator,
                description=description,
                device_info=device_info,
            )
        )

    async_add_entities(entities)
