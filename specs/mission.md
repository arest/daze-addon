# Mission

## Purpose

Integrate Daze WallBox EV chargers into Home Assistant, enabling real-time monitoring of charging metrics and direct control of charging behaviour — all from within the user's smart home dashboard, following Home Assistant ecosystem conventions.

Daze wallboxes are managed through a mobile app and web portal at dazeservice.com. This integration bridges the gap, bringing the wallbox into Home Assistant alongside other smart home devices without requiring a separate app.

## Goals

- Expose real-time charging metrics as Home Assistant sensor entities with correct device classes, state classes, and units of measurement: power (W), delivered energy (Wh), charging current per phase (mA), AC voltage per phase (V), board and case temperatures (°C), and EVSE operational status.
- Provide controls to start and stop charging via Home Assistant switch entities.
- Expose the maximum charging current limit as a Number entity, settable from HA automations and dashboards.
- Automatically manage OAuth token lifecycle (access + refresh tokens from Amazon Cognito) so the integration runs without manual re-authentication.
- Track recharge session history, including total energy and cost per session.
- Surface network-level configuration (grid max power, three-phase info, photovoltaic presence) as diagnostic sensor entities with `entity_category: diagnostic`.
- Support setup entirely through the Home Assistant UI config flow — no YAML configuration required.
- Ship with full translations (`strings.json`), proper entity naming, and device registry integration so the wallbox appears as a single device with all sensors and controls grouped under it.
- Follow Home Assistant custom component best practices: DataUpdateCoordinator for polling, proper async patterns, availability logic, and state restoration.

## Non-Goals

- Replacing the official Daze mobile app or web portal.
- Implementing OCPP protocol — the integration uses the Daze REST API exclusively.
- Controlling non-Daze chargers or wallboxes.
- Providing a billing or payment interface beyond showing energy cost per session.
- Managing firmware updates or wallbox configuration (scheduling, WiFi settings, etc.).
- Supporting multiple wallbox brands or generic EVSEs.
- Exposing raw API data without going through HA entity abstractions.
