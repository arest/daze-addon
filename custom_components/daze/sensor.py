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
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import DazeDataUpdateCoordinator


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


def _get_presence_value(data: dict[str, Any]) -> str | None:
    """Return 'on' or 'off' for a boolean diagnostic field.

    Field keys are mapped from the sensor name: ``is_photovoltaic`` and
    ``is_three_phase``.
    """
    return None  # overridden per sensor via lambdas in SENSORS


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
# Sensor definitions
# ------------------------------------------------------------------

SENSORS: tuple[DazeSensorEntityDescription, ...] = (
    # --- Measurement sensors ---
    DazeSensorEntityDescription(
        key="instant_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda data: data.get("instantPower"),
    ),
    DazeSensorEntityDescription(
        key="delivered_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        value_fn=lambda data: data.get("deliveredEnergy"),
    ),
    DazeSensorEntityDescription(
        key="charging_current_l1",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        value_fn=lambda data: data.get("phaseCurrentL1"),
    ),
    DazeSensorEntityDescription(
        key="charging_current_l2",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        value_fn=lambda data: data.get("phaseCurrentL2"),
    ),
    DazeSensorEntityDescription(
        key="charging_current_l3",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        value_fn=lambda data: data.get("phaseCurrentL3"),
    ),
    DazeSensorEntityDescription(
        key="ac_voltage_l1",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=lambda data: data.get("phaseVoltageL1"),
    ),
    DazeSensorEntityDescription(
        key="ac_voltage_l2",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=lambda data: data.get("phaseVoltageL2"),
    ),
    DazeSensorEntityDescription(
        key="ac_voltage_l3",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=lambda data: data.get("phaseVoltageL3"),
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
        value_fn=lambda data: _presence_on_off(data, "is_three_phase"),
    ),
)


def _presence_on_off(data: dict[str, Any], key: str) -> str | None:
    """Return 'on' or 'off' for a boolean diagnostic field."""
    val = data.get(key)
    if val is None:
        return None
    return "on" if bool(val) else "off"


# ------------------------------------------------------------------
# Base sensor entity
# ------------------------------------------------------------------


class DazeWallboxSensorEntity(
    CoordinatorEntity[DazeDataUpdateCoordinator], SensorEntity
):
    """Base sensor entity for Daze Wallbox metrics.

    All wallbox sensor entities inherit from this class. It provides:
    - ``device_info`` from the registered wallbox device
    - ``_attr_has_entity_name`` so HA prefixes the device name
    - Automatic ``available`` propagation via ``CoordinatorEntity``
    """

    entity_description: DazeSensorEntityDescription
    _attr_has_entity_name = True

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

    @property
    def native_value(self) -> Any | None:
        """Return the sensor value from the latest coordinator data."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


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
