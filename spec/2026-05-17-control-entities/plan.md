# Plan: Control Entities

## Group 1: Implement switch platform

- [x] Replace `switch.py` placeholder with `DazeWallboxSwitchEntity` class extending `CoordinatorEntity[DazeDataUpdateCoordinator]` and `SwitchEntity`
- [x] Implement `async_setup_entry` reading coordinator, api_client, serial_number, and device_info from `hass.data[DOMAIN][entry.entry_id]`
- [x] Add `_attr_is_on` property: read from `coordinator.data["evseStatus"]` — return `True` for `"charging"`, `False` for `"idle"`/`"paused"`/`"error"`/`"offline"`
- [x] Implement `async_turn_on`: call `self._api_client.async_start_charge(self._serial_number)`, invalidate coordinator, handle errors
- [x] Implement `async_turn_off`: call `self._api_client.async_stop_charge(self._serial_number)`, invalidate coordinator, handle errors
- [x] Set `_attr_has_entity_name = True` and pass `device_info` from constructor
- [x] Set `unique_id = f"{serial_number}_charge_switch"`
- [x] Add error handling: wrap API calls in try/except, log warnings, show HA persistent notification on command failure
- [x] Add `strings.json` entity name entry for the switch under the `switch` category

## Group 2: Implement number platform

- [x] Replace `number.py` placeholder with `DazeWallboxNumberEntity` class extending `CoordinatorEntity[DazeDataUpdateCoordinator]` and `NumberEntity`
- [x] Implement `async_setup_entry` reading coordinator, api_client, serial_number, and device_info from `hass.data`
- [x] Configure entity properties:
  - `native_min_value = 6000` (6 A minimum — common EVSE floor)
  - `native_max_value = 32000` (32 A maximum — typical wallbox hardware limit)
  - `native_step = 100` (0.1 A increments)
  - `native_unit_of_measurement = UnitOfElectricCurrent.MILLIAMPERE`
  - `entity_category = EntityCategory.CONFIG`
  - `_attr_has_entity_name = True`
- [x] Implement `native_value`: read from `coordinator.data["maxExternalChargingCurrentInMilliAmps"]` (or fallback `"lastMaxChargingCurrent"`)
- [x] Implement `async_set_native_value`: call `self._api_client.async_set_max_charging_current(self._serial_number, value)`, invalidate coordinator, handle errors
- [x] Set `unique_id = f"{serial_number}_max_charging_current"`
- [x] Add error handling: wrap API calls in try/except, log warnings, show HA persistent notification on command failure
- [x] Add `strings.json` entity name entry for the number entity under the `number` category

## Group 3: Implement select platform (operation mode)

- [x] Add `async_set_eco_mode(serial, eco_mode_enabled)` method to `DazeApiClient` — uses `POST /v3/evses/{serial}/configurations/ecoMode`
- [x] Replace `select.py` placeholder with `DazeWallboxSelectEntity` class extending `CoordinatorEntity[DazeDataUpdateCoordinator]` and `SelectEntity`
- [x] Implement `async_setup_entry` reading coordinator, api_client, serial_number, and device_info from `hass.data`
- [x] Define and surface operation mode options:
  - `"fast"` — maximum power, no eco restrictions
  - `"eco"` — eco mode (photovoltaic surplus or off-peak)
  - `"scheduled"` — follows configured schedule (not yet writable via API — shows notification)
- [x] Implement `current_option`: read from `coordinator.data` fields (`ecoModeEnabled`, `operationMode`)
- [x] Implement `async_select_option`: call API client method, invalidate coordinator, handle errors
- [x] Set `_attr_has_entity_name = True`, pass `device_info`
- [x] Set `unique_id = f"{serial_number}_operation_mode"`
- [x] Add error handling: wrap API calls in try/except, log warnings, show HA persistent notification on command failure
- [x] Add `strings.json` entity name entry for the select entity under the `select` category

## Group 4: Coordinator invalidation, error handling & tests

- [x] After each successful control command (switch, number, select), call `coordinator.async_request_refresh()` so sensor values reflect the new state on next poll
- [x] Handle edge case: switch `async_turn_on` when car is not connected — API returns an error; surface as a readable HA notification
- [x] Handle edge case: switch `async_turn_off` when already stopped — idempotent check (log at debug level)
- [x] Handle edge case: number set to same value as current — skip API call
- [x] Add unit tests for `switch.py`: mock coordinator with known state, toggle on/off, assert API client called with correct parameters
- [x] Add unit tests for `number.py`: mock coordinator, set value, assert API client called with correct mA value
- [x] Add unit tests for `select.py`: mock coordinator, select option, assert API client called correctly
- [x] Add unit tests for error handling: API raises ApiError/ApiAuthError → entity handles gracefully
- [x] Add unit tests for coordinator invalidation: after command, `async_request_refresh` is called
- [x] Verify existing sensor tests still pass (`pytest tests/`)
- [x] Run `ruff check .` and fix any lint errors
- [x] Run `pyright` and fix any type errors
