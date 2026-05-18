# Tech Stack

## Languages & Runtimes

- **Python 3.12+** — Home Assistant custom components run on the HA Python runtime. Minimum version aligns with the oldest supported HA release.
- **Home Assistant 2025.x+** — Targets the currently supported HA core. Uses the `homeassistant` package for component registration, entity platforms, config flows, and translations.

## Frameworks & Libraries

- **aiohttp** — Async HTTP client built into Python. Used for all Daze REST API calls (GET for sensor data, POST for commands and token refresh).
- **`homeassistant.helpers.update_coordinator` (DataUpdateCoordinator)** — Standard HA pattern for polling an external API and notifying entity platforms of updated data. Handles throttling, error classification (via `UpdateFailed`), refresh scheduling, and availability propagation to entities.
- **`homeassistant.config_flow` (ConfigFlow)** — HA's config flow framework for setting up the integration entirely through the UI. Handles credential entry, token validation, network selection, and re-authentication flows. No YAML configuration path.
- **`homeassistant.helpers.entity` / entity platforms** — Standard HA entity base classes: `SensorEntity` (with `SensorDeviceClass`, `SensorStateClass`), `SwitchEntity`, `NumberEntity`, `SelectEntity`. All entities use `EntityCategory` for proper UX grouping (diagnostic, config).
- **`homeassistant.helpers.device_registry` / `entity_registry`** — Device registry links all entities to a single device. Entity registry provides stable unique IDs, entity names, and `entity_category` assignment.
- **`homeassistant.helpers.translation` (strings.json)** — HA's translation system for entity names, config flow strings, and services. Translations are defined in `strings.json` (English base) and `translations/` (localised files).

## Infrastructure

- **Installation**: Custom component via HACS (Home Assistant Community Store) or manual `custom_components/daze/` directory.
- **API**: Daze REST API at `webapi.dazeservice.com/v3/` — authenticated via Amazon Cognito Bearer access token.
- **Auth**: Cognito token endpoint at `daze.auth.eu-central-1.amazoncognito.com/oauth2/` — integration accepts a pre-obtained access token + refresh token (no email/password). The refresh token is used to obtain new access tokens. Tokens are stored in HA config entry data (never in `configuration.yaml` or `.storage/` files directly).
- **Storage**: HA config entry data for credentials and tokens. No external database required.
- **CI/CD**: GitHub Actions for linting (ruff), type checking (pyright/mypy), HACS validation, and `hassfest` on pull requests.

## Rationale

- **DataUpdateCoordinator** is the HA-recommended pattern for polling APIs — it standardises error handling, retry logic, and entity state consistency. All sensor entities read from the same coordinator data, ensuring a single source of truth.
- **aiohttp** avoids blocking the HA event loop. The Daze API is entirely REST-based so no WebSocket or MQTT is needed.
- **Cognito token refresh flow** matches the Daze auth scheme exactly — the integration stores the refresh token in the config entry and transparently refreshes the access token, keeping the setup self-maintaining.
- **Config flow UI** is the HA standard for integration setup — no YAML editing required, full validation at input time, and support for re-authentication when tokens expire.
- **Entity platforms with device classes** follow HA core conventions: sensors get proper `device_class` and `state_class` so HA can compute statistics, energy dashboards, and history correctly. Switch/Number/Select entities use typed platform helpers for consistent behaviour.

## Constraints

- **Single-user token**: The integration uses the Daze platform's personal access token model — not a service account. Only one person's wallbox access is configured per HA instance.
- **Polling latency**: The Daze API does not push state changes. The integration polls at a configurable interval (default 30–60s). Sub-second state changes are not expected.
- **Cloud dependency**: All data flows through the Daze cloud API. No local/offline control if the wallbox or internet is unreachable. Entities enter `unavailable` state when the API cannot be reached.
- **No write WebSocket**: All controls are HTTP POST commands. State must be polled after a command to confirm the change took effect.
- **HA ecosystem constraints**: Must not edit `.storage/` files, must not write raw YAML to `configuration.yaml`, must follow HA's async patterns, and must pass `hassfest` validation for HACS default repository inclusion.
