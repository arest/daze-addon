# Validation: Polish & HACS Submission (Pi Test First)

## Acceptance Criteria

### Italian Translation
- [ ] `custom_components/daze/translations/it.json` exists and is valid JSON
- [ ] All config flow strings (titles, descriptions, errors, abort reasons) have Italian translations
- [ ] All entity names are translated to Italian
- [ ] Switching HA to `language: it` shows Italian UI strings for the Daze integration

### HA Services
- [ ] `services.yaml` defines all 3 services with proper field schemas
- [ ] Services appear in HA Developer Tools → Services with correct descriptions
- [ ] `set_charging_current` accepts a valid current value and updates the wallbox
- [ ] `start_charge` triggers charging and sensor reflects new state
- [ ] `stop_charge` stops charging and sensor reflects new state
- [ ] Invalid values (current out of range) return meaningful error

### State Restoration
- [ ] `delivered_energy` value survives HA core restart
- [ ] `lifetime_energy` value survives HA core restart
- [ ] `total_sessions` value survives HA core restart
- [ ] Last session values restore if available, gracefully handle missing data on first run
- [ ] Instantaneous sensors (power, current, voltage) do NOT restore (expected — they poll on first update)

### Device Info
- [ ] Device registry shows manufacturer: "Daze"
- [ ] Model matches the `deviceProfile` from the API (e.g., "Daze7000")
- [ ] Firmware version populated from API data
- [ ] `configuration_url` links to Daze web portal

### README
- [ ] README has clear HACS installation instructions
- [ ] README has a configuration walkthrough with steps
- [ ] README has an entity reference table
- [ ] README has a troubleshooting section
- [ ] No raw API documentation dumps remain
- [ ] README renders correctly on GitHub

### CI Pipeline
- [ ] `.github/workflows/validate.yaml` triggers on push and PR
- [ ] ruff lint passes with 0 errors
- [ ] pyright/mypy type check passes
- [ ] `hassfest` validation passes
- [ ] HACS validation passes

### Raspberry Pi Testing
- [ ] Installs successfully via HACS custom repository
- [ ] All entity types (sensor, switch, number, select) appear correctly
- [ ] All entities are grouped under a single Daze device
- [ ] Sensor values update at the configured poll interval
- [ ] Charge start/stop control works
- [ ] Charging current limit can be set
- [ ] Operation mode selector works
- [ ] Italian language strings display correctly
- [ ] Re-authentication flow works when tokens expire
- [ ] State restoration works after HA restart
- [ ] No errors in HA logs related to Daze integration

## Merge Conditions

- [ ] All acceptance criteria met (both automated and manual)
- [ ] CI pipeline passes on the feature branch
- [ ] Raspberry Pi testing completed with no blocking issues
- [ ] Code reviewed
- [ ] No regressions in existing functionality

## Testing

### Automated checks
```bash
# Lint
ruff check custom_components/daze/

# Type checking
pyright custom_components/daze/

# HA validation (requires hassfest CLI or GitHub action)
# hassfest --action validate --path custom_components/daze/

# HACS validation (requires hacs CLI or GitHub action)
# hacs validation
```

### Manual tests (on Raspberry Pi)
1. Install via HACS custom repository
2. Configure with valid Daze tokens
3. Verify all entities on the dashboard
4. Test each control
5. Switch HA language to Italian, verify strings
6. Force token expiry, verify re-auth flow
7. Restart HA Core, verify state restoration
8. Check HA logs for errors

### Regression check
- Config flow still works (fresh install and re-auth)
- All Phase 1–4 entities still report correct values
- Controls still work through the UI
