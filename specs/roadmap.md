# Roadmap

## Phase 1: Scaffold, Auth & Config Flow

- [x] Scaffold the `custom_components/daze/` package structure (`__init__.py`, `const.py`, `manifest.json`)
- [x] Implement the Cognito OAuth client: validate provided tokens, refresh access token using refresh token, handle token expiry and re-authentication
- [x] Build the minimal async API client class wrapping core Daze REST endpoints (user info, networks, EVSEs, sockets)
- [x] Implement the config flow (`config_flow.py`): step 1 (access token + refresh token entry), step 2 (network selection from available networks), step 3 (validation and setup)
- [x] Add re-authentication flow for expired refresh tokens (triggers when the client gets 401 on token refresh)
- [x] Define device registry entry (`async_setup_entry` creates/updates the device)
- [x] Add `strings.json` with config flow strings and entity translation base
- [x] Implement `DataUpdateCoordinator` with configurable polling interval and error handling (auth failure triggers re-auth flow, transient errors retry)

## Phase 2: Sensor Platform

- [x] Create `sensor.py` platform with coordinator integration
- [x] Add sensors with correct device classes and state classes:
  - Instant power (W) → `SensorDeviceClass.POWER`, `SensorStateClass.MEASUREMENT`
  - Delivered energy (Wh) → `SensorDeviceClass.ENERGY`, `SensorStateClass.TOTAL_INCREASING`
  - Charging current L1/L2/L3 (mA) → `SensorDeviceClass.CURRENT`, `SensorStateClass.MEASUREMENT`
  - AC voltage L1/L2/L3 (V) → `SensorDeviceClass.VOLTAGE`, `SensorStateClass.MEASUREMENT`
  - Board temperature (°C) → `SensorDeviceClass.TEMPERATURE`, `SensorStateClass.MEASUREMENT`
  - Case temperature (°C) → `SensorDeviceClass.TEMPERATURE`, `SensorStateClass.MEASUREMENT`
- [x] Add EVSE status sensor with `device_class = SensorDeviceClass.ENUM` and mapped state values (idle, charging, paused, error, etc.)
- [x] Add network-level diagnostic sensors with `entity_category = EntityCategory.DIAGNOSTIC`: grid max power, photovoltaic presence, three-phase status
- [x] Register all sensors under the wallbox device via `device_info`
- [x] Implement availability logic: entities become `unavailable` when coordinator update fails repeatedly

## Phase 3: Control Entities

- [x] Create `switch.py` — start/stop charge toggle entity using `SwitchEntity`
- [x] Create `number.py` — max charging current setter using `NumberEntity` with `native_min_value`/`native_max_value`/`native_step`
- [x] Create `select.py` — operation mode selector (eco, fast, scheduled, etc.) using `SelectEntity`
- [x] Wire controls through the API client with proper error handling
- [x] Invalidate coordinator data after each control command so sensors reflect the new state on next poll
- [x] Add `entity_category = EntityCategory.CONFIG` to the number entity (charging current limit is a configuration parameter)

## Phase 4: Recharge Sessions & Diagnostics

- [x] Add session history polling to the coordinator (last N sessions)
- [x] Create sensor entities for: last session energy (Wh), last session duration, last session cost (€), last session start/end time
- [x] Add aggregate counters: lifetime energy (Wh), total session count
- [x] Handle edge cases: never-used charger (no sessions), in-progress session (only partial data available)
- [x] Implement `diagnostics.py` for HA diagnostics endpoint — exposes token metadata (not the tokens themselves), API connectivity status, coordinator timing, and entity counts
- [x] Add diagnostic sensor for next scheduled charge (if scheduling is active)

## Phase 5: Polish & HACS Submission

- [ ] Complete `strings.json` and add Italian translation (`translations/it.json`)
- [ ] Add optional HA services: `set_charging_current`, `start_charge`, `stop_charge` as callable services with proper service schema
- [ ] Implement proper state restoration on HA restart (sensor history preserved via `restore_state`)
- [ ] Add device info: manufacturer "Daze", model from `deviceProfile`, firmware version from `softwareVersion`/`firmwareVersion`
- [ ] Write README with installation instructions (HACS), configuration UI walkthrough, entity reference table, and troubleshooting
- [ ] Add GitHub Actions CI: ruff lint, pyright type check, `hassfest` validation, HACS validation
- [ ] Submit to HACS default repository
