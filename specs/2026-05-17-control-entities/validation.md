# Validation: Control Entities

## Acceptance Criteria

### Switch entity
- [x] A switch entity appears under the wallbox device in Home Assistant with name "Charge Control" (or configured name from `strings.json`)
- [x] Switch shows `on` when `evseStatus` is `"charging"` and `off` for all other states (`"idle"`, `"paused"`, `"error"`, `"offline"`)
- [x] Toggling switch `on` calls `async_start_charge` API method and triggers coordinator refresh
- [x] Toggling switch `off` calls `async_stop_charge` API method and triggers coordinator refresh
- [x] Switch state updates automatically after next coordinator poll when charging state changes externally (e.g., via the Daze mobile app)
- [x] Switch shows `unavailable` when coordinator `last_update_success` is `False`

### Number entity
- [x] A number entity appears under the wallbox device with entity category `config` and name reflecting "Max Charging Current"
- [x] Number entity shows native unit "mA" and proper device class
- [x] `native_min_value` is 6000, `native_max_value` is 32000, `native_step` is 100
- [x] Number reads current value from `coordinator.data["maxExternalChargingCurrentInMilliAmps"]` (or fallback `"lastMaxChargingCurrent"`)
- [x] Setting a new value calls `async_set_max_charging_current` with correct serial and mA value
- [x] Number value updates automatically after coordinator refresh
- [x] Number entity shows `unavailable` when coordinator `last_update_success` is `False`
- [x] Setting the same value as the current value skips the API call

### Select entity
- [x] A select entity appears under the wallbox device with available operation mode options
- [x] Options match the Daze API's inferred operation modes (fast, eco, scheduled)
- [x] Selecting an option calls the corresponding API method and triggers coordinator refresh
- [x] Current option updates automatically after coordinator poll
- [x] Entity shows `unavailable` when coordinator `last_update_success` is `False`

### Error handling
- [x] API errors during `async_turn_on`/`async_turn_off`/`async_set_native_value`/`async_select_option` are caught, logged, and surfaced as HA persistent notifications
- [x] Auth errors (`ApiAuthError`) trigger the re-authentication flow rather than crashing
- [x] Control commands are idempotent — calling `turn_on` when already charging is a no-op
- [ ] Integration works end-to-end with real Daze credentials on a development HA instance *(manual test)*

### No regressions
- [x] All sensor entities from Phase 2 still work correctly (36 tests pass)
- [x] Config flow setup and re-authentication still work (no code changes)
- [x] `strings.json` includes entries for switch, number, and select entity names
- [ ] `hassfest` validation passes with no errors *(requires HA dev environment)*

## Testing

### Unit / integration tests
- Mock the coordinator with known data → assert switch returns correct `is_on`, number returns correct `native_value`, select returns correct `current_option`
- Mock the API client → call `async_turn_on` and assert `async_start_charge` was called with correct serial; same for off, set_current, select_option
- Mock the coordinator with `last_update_success = False` → assert all control entities report `available = False`
- Mock API error during command → assert error is handled gracefully (no crash, notification created)
- Run with: `pytest tests/`

### Manual testing
- Install the component on a development Home Assistant instance with a real Daze wallbox
- Navigate to Developer Tools → States and verify the switch, number, and select entities appear under the wallbox device
- Toggle the switch on/off and verify the wallbox starts/stops charging (confirm via wallbox LED or Daze app)
- Set the max charging current and verify the change takes effect (check sensor values update)
- Change the operation mode and verify the change takes effect
- Disconnect network → observe entities go `unavailable`; restore network → observe entities return to normal
- Verify the Daze mobile app shows the same state as HA (bidirectional consistency)

## Merge Conditions

- [ ] All acceptance criteria met
- [ ] Unit/integration tests pass with `pytest tests/`
- [ ] `ruff check .` passes with no errors
- [ ] `pyright` type checking passes (or matches project strictness)
- [ ] `hassfest` validation passes
- [ ] Phase 1 auth + config flow still works (no regressions)
- [ ] Phase 2 sensor platform still works (no regressions)
- [ ] Control entities work in both directions: HA → wallbox and wallbox → HA (via poll)
- [ ] Code reviewed and approved
