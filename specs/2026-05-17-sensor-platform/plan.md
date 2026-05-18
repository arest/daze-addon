# Plan: Sensor Platform

## Group 1: Scaffold sensor platform
- [x] Create `sensor.py` in `custom_components/daze/` with `async_setup_entry` that reads coordinator from `hass.data`
- [x] Define `DazeWallboxSensorBase` base class extending `CoordinatorEntity` and `SensorEntity` — common `device_info`, `_attr_has_entity_name`, and `available` property
- [x] Wire `sensor.py` into `__init__.py` platform forwarding (`PLATFORMS` list — already present)

## Group 2: Implement measurement sensors
- [x] Add instant power sensor: `SensorDeviceClass.POWER`, `SensorStateClass.MEASUREMENT`, native unit W, reading from `instantPower`
- [x] Add delivered energy sensor: `SensorDeviceClass.ENERGY`, `SensorStateClass.TOTAL_INCREASING`, native unit Wh, reading from `deliveredEnergy`
- [x] Add charging current sensors for L1, L2, L3: `SensorDeviceClass.CURRENT`, `SensorStateClass.MEASUREMENT`, native unit mA, reading from `phaseCurrentL1/L2/L3`
- [x] Add AC voltage sensors for L1, L2, L3: `SensorDeviceClass.VOLTAGE`, `SensorStateClass.MEASUREMENT`, native unit V, reading from `phaseVoltageL1/L2/L3`
- [x] Add board temperature sensor: `SensorDeviceClass.TEMPERATURE`, `SensorStateClass.MEASUREMENT`, native unit °C, reading from `boardTemperature`
- [x] Add case temperature sensor: `SensorDeviceClass.TEMPERATURE`, `SensorStateClass.MEASUREMENT`, native unit °C, reading from `caseTemperature`

## Group 3: Implement status and diagnostic sensors
- [x] Add EVSE status sensor: `SensorDeviceClass.ENUM`, with mapped options (`idle`, `charging`, `paused`, `error`, `offline`), reading from `evseStatus`
- [x] Add grid max power diagnostic sensor: `SensorDeviceClass.POWER`, `entity_category = DIAGNOSTIC`, reading from `gridMaxPower`
- [x] Add photovoltaic presence diagnostic sensor: `entity_category = DIAGNOSTIC`, `options=["on","off"]`, reading from `is_photovoltaic`
- [x] Add three-phase status diagnostic sensor: `entity_category = DIAGNOSTIC`, `options=["on","off"]`, reading from `is_three_phase`

## Group 4: Availability, registration & tests
- [x] Implement availability logic: `CoordinatorEntity` provides `available` for free based on `last_update_success`
- [x] Register all sensors under the wallbox device via `device_info` set in the constructor
- [x] Add `strings.json` entity name entries for all new sensors (already present from Phase 1)
- [x] Add basic validation tests: field extraction, EVSE status mapping, boolean presence mapping — 36 tests passing
