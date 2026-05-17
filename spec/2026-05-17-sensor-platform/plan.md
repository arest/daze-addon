# Plan: Sensor Platform

## Group 1: Scaffold sensor platform
- [ ] Create `sensor.py` in `custom_components/daze/` with `async_setup_entry` that reads coordinator from `hass.data`
- [ ] Define `DazeWallboxSensorBase` base class extending `CoordinatorEntity` and `SensorEntity` — common `device_info`, `_attr_has_entity_name`, and `available` property
- [ ] Wire `sensor.py` into `__init__.py` platform forwarding (`PLATFORMS` list)

## Group 2: Implement measurement sensors
- [ ] Add instant power sensor: `SensorDeviceClass.POWER`, `SensorStateClass.MEASUREMENT`, native unit W, reading from `data.socket.instantPower`
- [ ] Add delivered energy sensor: `SensorDeviceClass.ENERGY`, `SensorStateClass.TOTAL_INCREASING`, native unit Wh, reading from `data.socket.deliveredEnergy`
- [ ] Add charging current sensors for L1, L2, L3: `SensorDeviceClass.CURRENT`, `SensorStateClass.MEASUREMENT`, native unit mA, reading from `data.socket.phaseCurrentL1/L2/L3`
- [ ] Add AC voltage sensors for L1, L2, L3: `SensorDeviceClass.VOLTAGE`, `SensorStateClass.MEASUREMENT`, native unit V, reading from `data.socket.phaseVoltageL1/L2/L3`
- [ ] Add board temperature sensor: `SensorDeviceClass.TEMPERATURE`, `SensorStateClass.MEASUREMENT`, native unit °C, reading from `data.socket.boardTemperature`
- [ ] Add case temperature sensor: `SensorDeviceClass.TEMPERATURE`, `SensorStateClass.MEASUREMENT`, native unit °C, reading from `data.socket.caseTemperature`

## Group 3: Implement status and diagnostic sensors
- [ ] Add EVSE status sensor: `SensorDeviceClass.ENUM`, with mapped options (`idle`, `charging`, `paused`, `error`, `offline`), reading from `data.socket.evseStatus` — use a `DazeEvseStatus` enum or mapping dict
- [ ] Add grid max power diagnostic sensor: `SensorDeviceClass.POWER`, `entity_category = DIAGNOSTIC`, reading from `data.network.gridMaxPower`
- [ ] Add photovoltaic presence diagnostic sensor: `entity_category = DIAGNOSTIC`, boolean icon-based sensor, reading from `data.network.hasPhotovoltaic`
- [ ] Add three-phase status diagnostic sensor: `entity_category = DIAGNOSTIC`, boolean icon-based sensor, reading from `data.network.isThreePhase`

## Group 4: Availability, registration & tests
- [ ] Implement availability logic: `available` returns `True` only if the coordinator last update succeeded and `data` is not `None`
- [ ] Register all sensors under the wallbox device via `device_info` — use the device info dict returned by `async_setup_entry`
- [ ] Add `strings.json` entity name entries for all new sensors (sensor category)
- [ ] Add basic validation tests: coordinator returns mock data → sensors have correct values; coordinator fails → sensors are unavailable
