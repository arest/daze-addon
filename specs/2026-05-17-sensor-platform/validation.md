# Validation: Sensor Platform

## Acceptance Criteria

- [ ] All sensor entities appear under the wallbox device in Home Assistant (developer tools → entities) with correct names, device classes, state classes, and units of measurement
- [ ] Each sensor reads the expected value from `coordinator.data`:
  - Power (W) — displays correct wattage
  - Delivered energy (Wh) — displays correct cumulative energy
  - Current L1/L2/L3 (mA) — displays per-phase current
  - Voltage L1/L2/L3 (V) — displays per-phase voltage
  - Board temperature (°C) — displays correct board temp
  - Case temperature (°C) — displays correct case temp
  - EVSE status — shows human-readable state (`idle`, `charging`, `paused`, `error`, `offline`)
  - Grid max power (W) — displays network grid power limit
  - Photovoltaic presence — boolean on/off reflecting PV installation
  - Three-phase status — boolean on/off reflecting three-phase setup
- [ ] All diagnostic sensors have `entity_category = "diagnostic"` in the entity registry
- [ ] When the API is unreachable (coordinator `last_update_success = False`), all sensors show `unavailable`
- [ ] When the API recovers, sensors regain their values automatically on next successful poll
- [ ] EVSE status enum displays valid options only (no raw API values leak through)
- [ ] `strings.json` includes entity names for all sensors under the `sensor` category
- [ ] `hassfest` validation passes with no errors

## Testing

### Unit / integration tests
- Mock the coordinator with known data → assert each sensor returns the correct `native_value` and `native_unit_of_measurement`
- Mock the coordinator with `last_update_success = False` → assert all sensors report `available = False`
- Run with: `pytest tests/` (once test infrastructure is set up)

### Manual testing
- Install the component on a development Home Assistant instance
- Configure the integration with valid Daze credentials (Phase 1 config flow)
- Navigate to Developer Tools → States and verify all sensors are listed under the wallbox device
- Trigger a network failure → observe sensors go `unavailable`
- Restore network connectivity → observe sensors return to normal values

## Merge Conditions

- [ ] All acceptance criteria met
- [ ] Unit/integration tests pass
- [ ] `ruff check .` passes with no errors
- [ ] `pyright` type checking passes (or configured to match project strictness)
- [ ] `hassfest` validation passes
- [ ] Phase 1 auth + config flow still works (no regressions)
- [ ] Code reviewed and approved
