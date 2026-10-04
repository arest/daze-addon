"""Sensor platform for Daze Wallbox."""

from __future__ import annotations

# Pyright (with homeassistant-stubs) models some entity accessors as
# cached_property fields; Home Assistant integrations override them with
# @property methods by design.
# pyright: reportIncompatibleVariableOverride=false
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
from .coordinator import DazeCoordinatorData, DazeDataUpdateCoordinator
from .sensor_catalog import EVSE_SENSOR_CATALOG, RESTORE_STATE_KEYS, EVSESensorSpec
from .solar import solar_attributes

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


@dataclass(frozen=True, kw_only=True)
class DazeSensorEntityDescription(SensorEntityDescription):
    """Description for a Daze wallbox sensor."""

    value_fn: Callable[[dict[str, Any]], Any | None] = lambda data: None


_DEVICE_CLASS_MAP: dict[str, SensorDeviceClass] = {
    "power": SensorDeviceClass.POWER,
    "energy": SensorDeviceClass.ENERGY,
    "current": SensorDeviceClass.CURRENT,
    "voltage": SensorDeviceClass.VOLTAGE,
    "temperature": SensorDeviceClass.TEMPERATURE,
    "enum": SensorDeviceClass.ENUM,
    "monetary": SensorDeviceClass.MONETARY,
    "timestamp": SensorDeviceClass.TIMESTAMP,
}

_STATE_CLASS_MAP: dict[str, SensorStateClass] = {
    "measurement": SensorStateClass.MEASUREMENT,
    "total_increasing": SensorStateClass.TOTAL_INCREASING,
}

_ENTITY_CATEGORY_MAP: dict[str, EntityCategory] = {
    "diagnostic": EntityCategory.DIAGNOSTIC,
}

_UNIT_MAP: dict[str, str] = {
    "W": UnitOfPower.WATT,
    "Wh": UnitOfEnergy.WATT_HOUR,
    "mA": UnitOfElectricCurrent.MILLIAMPERE,
    "V": UnitOfElectricPotential.VOLT,
    "°C": UnitOfTemperature.CELSIUS,
    "min": UnitOfTime.MINUTES,
    "EUR": "EUR",
}


def _to_description(spec: EVSESensorSpec) -> DazeSensorEntityDescription:
    """Convert one canonical EVSE sensor spec into a HA description."""
    return DazeSensorEntityDescription(
        key=spec.key,
        device_class=(
            _DEVICE_CLASS_MAP[spec.device_class]
            if spec.device_class is not None
            else None
        ),
        state_class=(
            _STATE_CLASS_MAP[spec.state_class]
            if spec.state_class is not None
            else None
        ),
        native_unit_of_measurement=(
            _UNIT_MAP[spec.native_unit_of_measurement]
            if spec.native_unit_of_measurement is not None
            else None
        ),
        entity_category=(
            _ENTITY_CATEGORY_MAP[spec.entity_category]
            if spec.entity_category is not None
            else None
        ),
        options=list(spec.options) if spec.options is not None else None,
        value_fn=spec.value_fn,
    )


def _build_sensor_descriptions(
    catalog: tuple[EVSESensorSpec, ...] = EVSE_SENSOR_CATALOG,
) -> tuple[DazeSensorEntityDescription, ...]:
    """Build sensor descriptions and fail fast on duplicate keys."""
    seen: set[str] = set()
    duplicates: set[str] = set()

    descriptions: list[DazeSensorEntityDescription] = []
    for spec in catalog:
        if spec.key in seen:
            duplicates.add(spec.key)
        seen.add(spec.key)
        descriptions.append(_to_description(spec))

    if duplicates:
        dupes = ", ".join(sorted(duplicates))
        raise ValueError(f"Duplicate Daze sensor description keys: {dupes}")

    return tuple(descriptions)


SENSORS: tuple[DazeSensorEntityDescription, ...] = _build_sensor_descriptions()


class DazeWallboxSensorEntity(
    CoordinatorEntity[DazeDataUpdateCoordinator], RestoreEntity, SensorEntity
):
    """Base sensor entity for Daze Wallbox metrics."""

    entity_description: DazeSensorEntityDescription
    _attr_has_entity_name = True
    _restored_value: Any | None = None

    def __init__(
        self,
        coordinator: DazeDataUpdateCoordinator,
        description: DazeSensorEntityDescription,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.serial_number}_{description.key}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Restore last known state for cumulative sensors."""
        await super().async_added_to_hass()

        if self.entity_description.key not in RESTORE_STATE_KEYS:
            return

        if self.coordinator.data is not None:
            return

        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return

        try:
            self._restored_value = float(last_state.state)
        except (ValueError, TypeError):
            self._restored_value = last_state.state

    @property
    def native_value(self) -> Any | None:
        """Return current coordinator value or restored fallback."""
        data: DazeCoordinatorData | None = self.coordinator.data
        if data is not None:
            value = self.entity_description.value_fn(data)
            # Return None so HA shows "unavailable" rather than
            # displaying the raw null / zero / junk that the API
            # returns for sensors the charger does not support.
            if value is None:
                return None

            # Single-phase chargers return junk in L2/L3 fields
            # (e.g. 1 V, 7 V). Hide them unless the charger is
            # declared three-phase.
            if (
                self.entity_description.key
                in (
                    "charging_current_l2",
                    "charging_current_l3",
                    "ac_voltage_l2",
                    "ac_voltage_l3",
                )
                and not data.get("evseIsThreePhase")
            ):
                return None

            return value

        if self._restored_value is not None:
            return self._restored_value

        return None


class DazeSolarSurplusSensor(
    CoordinatorEntity[DazeDataUpdateCoordinator], SensorEntity
):
    """The smoothed surplus the controller is working from.

    Exposed so the figure everything else depends on can be seen and
    graphed, rather than inferred from behaviour.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(
        self,
        coordinator: DazeDataUpdateCoordinator,
        controller: Any,
        serial_number: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialise the surplus sensor."""
        super().__init__(coordinator)
        self._controller = controller
        self._serial_number = serial_number
        self._attr_unique_id = f"{serial_number}_solar_surplus"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Redraw when the controller updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._controller.add_listener(self.async_write_ha_state)
        )

    @property
    def native_value(self) -> float | None:
        """Return the smoothed surplus."""
        return self._controller.surplus_w

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return solar control simulation state."""
        return solar_attributes(
            mode=self._controller.mode,
            decision=self._controller.last_decision,
            surplus_w=self._controller.surplus_w,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Daze Wallbox sensor entities."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DazeDataUpdateCoordinator = entry_data["coordinator"]
    serial_number: str = entry_data["serial_number"]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, serial_number)},
    )

    entities = [
        DazeWallboxSensorEntity(
            coordinator=coordinator,
            description=description,
            device_info=device_info,
        )
        for description in SENSORS
    ]

    solar_controller = entry_data.get("solar_controller")
    if solar_controller is not None:
        entities.append(
            DazeSolarSurplusSensor(
                coordinator=coordinator,
                controller=solar_controller,
                serial_number=serial_number,
                device_info=device_info,
            )
        )

    async_add_entities(entities)
