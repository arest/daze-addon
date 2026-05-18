# Plan: Polish & HACS Submission (Pi Test First)

## Group 1: Italian Translation
- [x] Create `custom_components/daze/translations/` directory
- [x] Write `translations/it.json` with complete Italian translations for:
  - Config flow strings (step titles, descriptions, errors, abort reasons)
  - Re-auth flow strings
  - All entity names (sensors, switch, number, select) in Italian
  - Service name and description translations
- [ ] Verify Italian UI displays correctly on HA instance with `language: it`

## Group 2: HA Services
- [x] Create `custom_components/daze/services.yaml` with service definitions:
  - `set_charging_current` (fields: `current` integer, 6000–32000 mA)
  - `start_charge`
  - `stop_charge`
- [x] Add `services:` section to `strings.json` for English service name translations
- [x] Add service constants to `const.py`
- [x] Register services in `__init__.py`:
  - `_async_register_services` function with domain-level services
  - Service handlers: start_charge, stop_charge, set_charging_current
  - Each calls the API client directly and invalidates coordinator cache
  - Auth errors trigger `ConfigEntryAuthFailed`, API errors raise `HomeAssistantError`
- [ ] Test services via HA Developer Tools → Services

## Group 3: State Restoration
- [x] Add `RestoreEntity` mixin to base sensor entity class
- [x] Implement `async_added_to_hass` with `async_get_last_state` restoration
- [x] Restorable sensors: delivered_energy, lifetime_energy, total_sessions, last_session_energy, last_session_cost, last_session_duration
- [x] Fall back to restored value when coordinator data is None (startup edge case)
- [x] Handle edge case: no previous state (first run), unknown/unavailable states
- [ ] Verify state survives HA restart (core restart, not config entry reload)

## Group 4: Device Info Polish
- [x] Verified device registry shows: manufacturer "Daze", model from `deviceProfile`
- [x] `sw_version` from `softwareVersion`, `hw_version` from `firmwareVersion`
- [x] `configuration_url` points to `https://webportal.dazeservice.com`
- [x] All constants (CONF_FIRMWARE_VERSION, CONF_SOFTWARE_VERSION, CONF_DEVICE_PROFILE) defined and used
- [x] Config flow correctly maps API fields to config entry data

## Group 5: README Overhaul
- [x] Rewrite README as proper HACS-friendly documentation:
  - Project description with feature list
  - Installation instructions (HACS → Custom Repositories → Add → Install + manual)
  - Configuration UI walkthrough (token entry → network selection → confirm)
  - Entity reference tables (all sensors with device class, state class, unit)
  - Diagnostic sensors table
  - Controls table (switch, number, select)
  - Service reference with YAML examples
  - Automation examples (peak hours, solar production)
  - Troubleshooting section (invalid tokens, unavailable, re-auth, stale data)
  - Data & privacy section
  - Development section with CI info and license
- [x] Add badges (HA version, build status)
- [x] Removed raw API documentation dumps

## Group 6: GitHub Actions CI
- [x] Create `.github/workflows/validate.yaml` with jobs:
  - **lint**: ruff check (Python 3.12+)
  - **typecheck**: pyright for type annotation validation
  - **hassfest**: HA integration validation via `home-assistant/actions/hassfest@master`
  - **hacs**: HACS validation via `hacs/action@main`
- [x] Trigger on push/PR to master and feature/* branches
- [ ] Verify all CI jobs pass

## Group 7: Raspberry Pi Testing
- [ ] Push `feature/polish-and-hacs-submission` to GitHub
- [ ] On Raspberry Pi: add repo as HACS custom repository
- [ ] Install the integration via HACS
- [ ] Verify all entities load correctly and appear under one Daze device
- [ ] Test sensor readings: power, energy, currents, voltages, temperatures, EVSE status
- [ ] Test controls: start/stop charge, set max current, change operation mode
- [ ] Verify Italian translations apply when HA language is Italian
- [ ] Test re-authentication flow (invalidate access token)
- [ ] Restart HA Core and verify state restoration
- [ ] Test service calls from HA Developer Tools
- [ ] Fix any issues found during testing
