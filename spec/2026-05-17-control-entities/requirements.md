# Requirements: Control Entities

## Scope

### What this covers

- **Switch entity** (`switch.py`): A binary `SwitchEntity` to start and stop charging on the Daze wallbox. Toggle state reflects the live charging status from the coordinator data (`evseStatus`).
- **Number entity** (`number.py`): A `NumberEntity` to set the maximum charging current limit in milliamps (mA). Exposed as `entity_category: config` so it appears in device configuration rather than primary controls.
- **Select entity** (`select.py`): A `SelectEntity` to choose the wallbox operation mode — eco, fast, scheduled (or equivalent mode options exposed by the Daze API).
- **API client additions**: New API method `async_set_operation_mode` on `DazeApiClient` for the select entity (if the corresponding REST endpoint exists).
- **Coordinator invalidation**: After each control command, `coordinator.async_request_refresh()` is called so sensor entities reflect the new state on the next poll cycle.
- **Error handling**: All control commands surface errors via Home Assistant notifications, log warnings, and gracefully degrade without crashing the integration.

### What this does **not** cover

- Recharge session history or aggregate counters (Phase 4).
- State restoration on HA restart (Phase 5).
- Diagnostic endpoint (`diagnostics.py` — Phase 4).
- HA service definitions (`set_charging_current`, `start_charge`, `stop_charge` — Phase 5).
- Italian translations or translation files beyond `strings.json` (Phase 5).
- Scheduling or timed charge control — the select entity selects the mode, but schedule configuration is outside scope.

## Context

This phase builds directly on Phase 1 (API client, coordinator, auth) and Phase 2 (sensor platform). The API client in `api/__init__.py` already implements the three command endpoints needed:

| Endpoint | Method | API Client Method |
|----------|--------|-------------------|
| `POST /v3/sockets/{serial}/commands/playcharge` | Start charge | `async_start_charge(serial)` |
| `POST /v3/sockets/{serial}/commands/stopcharge` | Stop charge | `async_stop_charge(serial)` |
| `POST /v3/evses/{serial}/configurations/maxExternalChargingCurrent` | Set max current | `async_set_max_charging_current(serial, current_ma)` |

Placeholder files already exist for all three entity platforms (`switch.py`, `number.py`, `select.py`) with empty `async_setup_entry` returning `True`. The `PLATFORMS` list in `const.py` already includes `"switch"`, `"number"`, `"select"`.

The operation mode select entity will likely require adding a new API method for a Daze REST endpoint — the exact endpoint needs to be identified during implementation (candidates: `POST /v3/evses/{serial}/configurations/ecoMode` or network-level eco mode endpoints). If no write endpoint exists, the select entity may operate as read-only or be deferred.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Entity base class | `CoordinatorEntity` + platform class | Consistent with Phase 2 sensor entities. `CoordinatorEntity` provides automatic availability propagation and coordinator integration. |
| Switch state source | `coordinator.data["evseStatus"]` | The switch's on/off state is derived from the existing coordinator data polled every 30s — no separate API call needed. State classification: `"charging"` → on, all others (`"idle"`, `"paused"`, `"error"`, `"offline"`) → off. |
| Number unit | Milliamps (mA) | The Daze API uses mA for all current-related values (charging current sensors in Phase 2 use `UnitOfElectricCurrent.MILLIAMPERE`). Using mA keeps consistency and avoids floating-point rounding. |
| Number entity category | `EntityCategory.CONFIG` | The max charging current is a configuration parameter, not a primary control. HA will show it under the device's configuration UI rather than the main controls surface. |
| Number min/max | 6000–32000 mA (6–32 A) | Industry standard range for EVSE charging current. Can be adjusted if the specific wallbox model has different hardware limits. |
| Coordinator invalidation | `coordinator.async_request_refresh()` after each command | Ensures sensor entities (power, current, energy, status) reflect the new state on the next poll cycle without waiting for the full interval. Uses HA's debounced refresh to avoid rapid polling. |
| Error handling | HA persistent notification + log warning | Control commands are user-initiated, so errors should be visible in the HA UI. Failed commands do not crash the integration or affect sensor polling. |
| Operation mode API | New method on `DazeApiClient` | The select entity needs a write endpoint. If the Daze API doesn't expose one, the select entity will be read-only (display mode only) or deferred to a later phase. |

## Open Questions

1. **Operation mode API endpoint**: Does the Daze REST API expose a write endpoint for changing operation mode? The network EVSE data includes `operationMode` (numeric) and `ecoModeEnabled` fields — but the corresponding write endpoint (if any) needs discovery during implementation.
2. **Switch state when car disconnected**: What happens when `async_turn_on` is called but no car is plugged in? The Daze API likely returns an error — the entity should surface this clearly without confusing the user.
3. **Number range limits**: Are the 6000–32000 mA bounds correct for the DT01 wallbox model? These match typical EVSE limits but should be confirmed against hardware specs.
