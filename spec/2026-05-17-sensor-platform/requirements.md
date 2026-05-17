# Requirements: Sensor Platform

## Scope

### What this covers
- A `sensor.py` platform registered via `PLATFORMS` in `__init__.py`, reading data from the existing `DataUpdateCoordinator` (Phase 1).
- 10+ sensor entities representing real-time wallbox metrics: power, energy, per-phase current and voltage, temperatures, and EVSE operational status.
- Network-level diagnostic sensor entities for grid max power, photovoltaic presence, and three-phase configuration.
- Correct Home Assistant `device_class`, `state_class`, `entity_category`, and `native_unit_of_measurement` on every sensor.
- All sensors grouped under the single wallbox device via `device_info`.
- Availability logic: sensors become `unavailable` when the coordinator fails to fetch data.
- Entity name entries in `strings.json` for all sensors.

### What this does **not** cover
- Recharge session history or aggregate counters (Phase 4).
- State restoration on HA restart (Phase 5).
- Translation files beyond `strings.json` (Phase 5 — Italian translation).
- Control entities — switch, number, select (Phase 3).
- Services — `set_charging_current`, `start_charge`, `stop_charge` (Phase 5).
- Diagnostics endpoint (Phase 4).

## Context

This phase builds on Phase 1's `DataUpdateCoordinator`, which already fetches all the data needed from the Daze REST API (socket metrics, network info). No new API calls are required.

The sensor entities are the primary user-facing interface of the integration — they populate dashboards, feed energy calculations, and trigger automations. Getting the device classes and state classes right is critical so HA handles statistics, energy dashboards, and history correctly (per `spec/tech-stack.md`).

Network-level diagnostic sensors (`entity_category = "diagnostic"`) are surfaced only for advanced users configuring grid constraints and can be hidden from default dashboard views.

The EVSE status enum aligns with the Daze API's status field values and maps them to human-readable string states (`idle`, `charging`, `paused`, `error`, `offline`) that HA displays natively.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Data source | Single `DataUpdateCoordinator` | All sensor entities read from `coordinator.data`. Guarantees consistency across entities on each poll cycle. Established pattern in HA custom components. |
| Base class | `CoordinatorEntity` + `SensorEntity` | `CoordinatorEntity` provides automatic `available` propagation and `async_update` integration. `SensorEntity` provides the entity platform contract. |
| EVSE status | `SensorDeviceClass.ENUM` with `options` list | HA renders ENUM-class sensors as native badges with human-readable text. A mapping dict translates Daze API values → HA state strings. |
| Network diagnostics | `entity_category = "diagnostic"` | These are configuration-state readouts, not actionable metrics. Diagnostic category hides them from default dashboards. |
| Temperature unit | °C | The Daze API returns temperatures in Celsius. HA handles unit conversion for Fahrenheit locales automatically when `device_class = TEMPERATURE`. |
| Availability | `CoordinatorEntity.available` | Phase 1 coordinator already tracks `last_update_success`. Inheriting from `CoordinatorEntity` gives us this for free — no per-sensor logic needed. |
