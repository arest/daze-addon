# Requirements: Recharge Sessions & Diagnostics

## Scope

### Includes
- Polling session history from the Daze REST API (last N recharge sessions per wallbox)
- Sensor entities for the last completed session: energy (Wh), duration, cost (€), start time, end time
- Lifetime aggregate counters: total energy delivered across all sessions (Wh), total session count
- Graceful handling of edge cases: never-used charger, in-progress session, empty API response, API errors
- HA diagnostics endpoint (`diagnostics.py`) exposing operational metadata (token validity, API connectivity, coordinator timing, entity inventory) — without exposing secrets
- Diagnostic sensor for next scheduled charge time (when scheduling is active/available)

### Excludes
- Session modification or deletion (read-only data)
- Billing or payment processing beyond cost display
- Per-session chart or graph rendering (HA's energy dashboard handles this)
- Exporting session data to CSV or external systems
- Session notification automations (users create these in HA themselves)

## Context

This phase builds on the existing integration which already has:
- A working `DataUpdateCoordinator` polling EVSE live data every 30-60s
- Sensor, switch, number, and select entity platforms
- An async API client with Cognito auth
- Device registry setup linking all entities to a single wallbox device

Session history is a natural extension — the Daze API provides historical charging data per wallbox. By adding session sensors alongside the existing live sensors, users get a complete picture of their charging behaviour directly in Home Assistant dashboards and energy tracking.

The diagnostics endpoint is an HA standard for custom components, enabling users and support to debug issues without exposing tokens or secrets.

Referenced documents: `specs/mission.md` (goals include tracking session history), `specs/tech-stack.md` (DataUpdateCoordinator, aiohttp, sensor entity patterns).

## Decisions

### Single coordinator, extended poll cycle
Session data is fetched alongside EVSE live data in the **existing** `DataUpdateCoordinator`, not a separate coordinator. This keeps the architecture simple — one data flow, one set of error handling rules, one refresh trigger. Sessions are a lightweight API call and don't warrant a separate refresh schedule.

### Lifetime energy as TOTAL_INCREASING
The lifetime energy sensor uses `SensorStateClass.TOTAL_INCREASING` so Home Assistant's energy dashboard and statistics system handle daily/weekly/monthly resets automatically — the integration only reports the monotonic total.

### Diagnostics follows HA convention
`diagnostics.py` exports a nested dict grouped by category (auth, API, coordinator, entities). Token strings are NEVER included — only metadata (issuer, expiry timestamp). This follows HA's security guidelines for diagnostics data.

### Scheduled charge as diagnostic entity
The scheduled charge sensor uses `entity_category = EntityCategory.DIAGNOSTIC` to keep it out of the main dashboard view while still being available for automations and debugging.
