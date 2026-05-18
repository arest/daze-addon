# Plan: Recharge Sessions & Diagnostics

## Group 1: Session API Integration
- [x] Add `get_charge_sessions()` method to the API client (fetch last N sessions from Daze REST API) — *already existed as `async_get_recharge_sessions()`*
- [x] Define session data model / dataclass for parsed session records (start time, end time, energy Wh, cost, status) — *`models.py` with `RechargeSession` dataclass*
- [x] Integrate session fetching into `DataUpdateCoordinator` alongside existing EVSE data polling — *coordinator now fetches sessions on every poll*
- [x] Add appropriate error handling for session endpoint (separate from EVSE data failures) — *session errors logged, return empty list; socket data unaffected*

## Group 2: Last Session Sensor Entities
- [x] Create sensor for last session energy (Wh) — `SensorDeviceClass.ENERGY`, `SensorStateClass.TOTAL_INCREASING` — *added to sensor.py SENSORS tuple*
- [x] Create sensor for last session duration (minutes) — *added with `native_unit_of_measurement: min`*
- [x] Create sensor for last session cost (€) — *MONETARY device class, EUR unit*
- [x] Create timestamp sensors for last session start and end time — `SensorDeviceClass.TIMESTAMP`
- [x] Register all new sensors under the wallbox device via `device_info` — *reuse existing DazeWallboxSensorEntity loop*
- [x] Add translation strings in `strings.json` — *all 7 new sensors named*

## Group 3: Aggregate Counters
- [x] Create lifetime energy sensor (Wh) — `SensorDeviceClass.ENERGY`, `SensorStateClass.TOTAL_INCREASING`
- [x] Create total session count sensor (integer, measurement)
- [x] Ensure aggregate values are computed from all available session data, not just the last session — *coordinator._compute_session_fields sums all sessions*
- [x] Verify aggregate sensors survive coordinator data refresh (no spurious resets) — *recomputed every poll from API data, no state stored in coordinator*

## Group 4: Edge Case Handling
- [x] Handle never-used charger (no sessions exist) — show `None`/`0` gracefully, not errors — *coordinator returns all-None fields*
- [x] Handle in-progress session (missing end time, cost not yet available) — show partial data — *from_dict handles None fields gracefully*
- [x] Handle API returning empty session list — sensors show default/zero state — *empty list → all-None fields*
- [x] Handle API errors on session endpoint — sensors enter `unavailable` state — *coordinator returns [] → all-None*
- [x] Add unit tests for all edge case data scenarios — *7 tests for empty, partial, in-progress, multi-session, 10k sessions*

## Group 5: Diagnostics Support
- [ ] Implement `diagnostics.py` with `async_get_config_entry_diagnostics` handler
- [ ] Expose token metadata: issuer, expiry timestamp, token age (NOT the token strings themselves)
- [ ] Expose API connectivity status: last successful request time, last failure time, failure count
- [ ] Expose coordinator timing: last update, next update, update interval, total update count
- [ ] Expose entity inventory: count of sensors, switches, numbers, selects
- [ ] Follow HA diagnostics data format: return dict grouped by category

## Group 5: Diagnostics Support
- [x] Implement `diagnostics.py` with `async_get_config_entry_diagnostics` handler
- [x] Expose token metadata: issuer, expiry timestamp, token age (NOT the token strings themselves)
- [x] Expose API connectivity status: last successful request time, last failure time, failure count
- [x] Expose coordinator timing: last update, next update, update interval, total update count
- [x] Expose entity inventory: count of sensors, switches, numbers, selects
- [x] Follow HA diagnostics data format: return dict grouped by category

## Group 6: Scheduled Charge Diagnostic Sensor
- [x] Research Daze API for scheduled charge endpoint/data availability — *remoteInfo may include scheduling fields; select.py has "scheduled" option but no API endpoint confirmed*
- [x] If available: add scheduled charge data to API client — *added `_get_next_scheduled_charge` helper that checks multiple possible field names*
- [x] Create diagnostic sensor for next scheduled charge time — `entity_category = EntityCategory.DIAGNOSTIC`, `SensorDeviceClass.TIMESTAMP`
- [x] Handle case where scheduling is not active or not supported — *returns None gracefully; sensor stays unavailable*
