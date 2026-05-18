# Requirements: Scaffold, Auth & Config Flow

## Scope

### Covers

- Custom component scaffold: `manifest.json`, `const.py`, `__init__.py`, empty platform stubs.
- `DazeAuthClient`: token validation, refresh, expiry checking, and re-authentication signalling.
- `DazeApiClient`: async REST client wrapping all Daze API endpoints needed for Phase 1 (user info, networks, EVSEs, sockets, basic commands).
- Config flow: 3-step UI setup (tokens → network → confirm) plus re-authentication flow for expired refresh tokens.
- Device registry: single device entry linking to the wallbox.
- `DataUpdateCoordinator`: polling loop that fetches live socket data and propagates to entity platforms.
- `strings.json`: config flow strings and translation base.

### Explicitly Does Not Cover

- Sensor entities (`sensor.py`), switch entities (`switch.py`), number entities (`number.py`), or select entities (`select.py`) — these are Phase 2 and Phase 3.
- Recharge session history endpoints — Phase 4.
- HA diagnostics endpoint (`diagnostics.py`) — Phase 4.
- HA services — Phase 5.
- Italian translations — Phase 5.
- HACS submission — Phase 5.

## Context

This phase establishes the foundational architecture of the Daze Home Assistant integration. Per `spec/mission.md`, the integration must:

- Support setup entirely through the HA UI config flow (no YAML).
- Automatically manage OAuth token lifecycle (Cognito access + refresh tokens).
- Use `DataUpdateCoordinator` for standardised polling and error handling.

Per `spec/tech-stack.md`, the integration uses:

- **aiohttp** for all async HTTP calls.
- **ConfigFlow** for UI-based setup.
- **DataUpdateCoordinator** for polling and entity state consistency.
- **Cognito OAuth** for token refresh (`grant_type=refresh_token`).

The `spec/AUTH.md` document provides the detailed auth specification that this phase implements. It describes the token lifecycle, the Cognito refresh flow, re-authentication mechanism, and the config flow steps in detail.

## Decisions

| Decision | Chosen Approach | Rationale |
|----------|----------------|-----------|
| Auth model | Token-only (access + refresh token from user) | Daze uses Cognito — no email/password. The user obtains tokens from the webapp/developer console. |
| Token storage | Config entry `data` dict | HA standard for integration credentials. Never written to `configuration.yaml` or `.storage/` files. |
| Token refresh | Proactive check via `is_token_expired()`, reactive on 401 | Both covered: proactive check before API calls when possible, 401 handling as fallback. |
| Re-auth trigger | `ConfigEntryAuthFailed` raised on 401 from token refresh | HA standard — triggers the re-authentication config flow automatically. |
| API client pattern | Separate `DazeApiClient` wrapping `DazeAuthClient` | Clean separation: auth logic is independent of API endpoint logic. Easy to extend with new endpoints. |
| Polling | `DataUpdateCoordinator` with configurable interval (default 30s) | HA-recommended pattern. Standardises error classification, retry, and entity availability. |
| HTTP error handling | 401 → refresh → retry once | If refresh succeeds, the retry uses the new token. If refresh or retry fails, raise appropriate error. |
| Device registry info | From EVSE `remoteInfo` endpoint | Provides serial number, model, firmware versions — the canonical device identity. |
| `config_flow.py` version | Start at version 1 | Initial release. Incremented on breaking changes to config entry data schema. |
