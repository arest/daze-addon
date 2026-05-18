# Plan: Scaffold, Auth & Config Flow

## Group 1: Scaffold Package Structure

- [ ] Create `custom_components/daze/` directory tree
- [ ] Write `manifest.json` with domain `daze`, version, requirements, config flow flag, dependencies, `iot_class`, and `integration_type`
- [ ] Write `const.py` with all constants: DOMAIN, API base URLs, Cognito endpoints, client ID, redirect URI, config entry keys (`CONF_ACCESS_TOKEN`, `CONF_REFRESH_TOKEN`, `CONF_NETWORK_ID`, etc.), coordinator defaults
- [ ] Write `__init__.py` with minimal `DOMAIN`, `PLATFORMS`, `async_setup_entry`, and `async_unload_entry` stubs
- [ ] Write empty platform files: `sensor.py`, `switch.py`, `number.py`, `select.py` (placeholders — for now just `async_setup_entry` that returns `True` or adds no entities)

## Group 2: Implement DazeAuthClient

- [ ] Create `api/auth.py` with `DazeAuthClient` class
- [ ] Implement `__init__(access_token, refresh_token, token_expiry=None)` storing all tokens
- [ ] Implement `get_headers()` returning `{"authorization": "Bearer <access_token>"}`
- [ ] Implement `is_token_expired()` comparing current time against `token_expiry` with 60s buffer
- [ ] Implement `async refresh_access_token()` calling Cognito `/oauth2/token` with `grant_type=refresh_token`
- [ ] Handle success response: store new `access_token`, update `token_expiry`, optionally update `refresh_token` if rotated
- [ ] Handle failure: raise `AuthError` (401/400 from Cognito, network errors)
- [ ] Implement `async validate_tokens(session)` calling `GET /oauth2/userInfo` — returns `True` on 200, raises on failure
- [ ] Implement `async_get_tokens_for_store()` returning a dict safe for config entry storage

## Group 3: Build Async API Client

- [ ] Create `api/__init__.py` with `DazeApiClient` class
- [ ] Accept a `DazeAuthClient` instance and an `aiohttp.ClientSession`
- [ ] Implement `async _request(method, url, **kwargs)` as the base HTTP helper that:
  - Attaches auth headers via `DazeAuthClient.get_headers()`
  - Handles 401 by calling `DazeAuthClient.refresh_access_token()`, retrying once
  - Raises `ApiAuthError` if retry also 401s (triggers re-auth)
  - Raises `ApiError` for other HTTP errors
- [ ] Implement `async get_user_info()` → `GET /oauth2/userInfo`
- [ ] Implement `async get_networks()` → `GET /v3/users/{email}/networks?includeStats=true`
- [ ] Implement `async get_evses(network_uid)` → `GET /v3/networks/{uid}/evses?includeEcoInfo=false`
- [ ] Implement `async get_socket_remote_info(serial)` → `GET /v3/sockets/{serial}/remoteInfo?includeEcoInfo=true`
- [ ] Implement `async set_max_charging_current(serial, current_ma)` → `POST /v3/evses/{serial}/configurations/maxExternalChargingCurrent`
- [ ] Implement `async start_charge(serial)` → `POST /v3/sockets/{serial}/commands/playcharge`
- [ ] Implement `async stop_charge(serial)` → `POST /v3/sockets/{serial}/commands/stopcharge`
- [ ] Implement `async get_recharge_sessions(network_uid, limit=1000)` → `GET /v3/networks/{uid}/rechargeSessions`

## Group 4: Implement Config Flow

- [ ] Create `config_flow.py` with `DazeConfigFlow` class extending `ConfigFlow` (domain `daze`, version 1)
- [ ] Implement Step 1: user enters access token + refresh token
  - `async_step_user` renders a form with `CONF_ACCESS_TOKEN` and `CONF_REFRESH_TOKEN`
  - On submit, call `DazeApiClient.validate_tokens()` — if 200, proceed; if fails, show form errors
- [ ] Implement Step 2: network selection
  - Fetch networks via `DazeApiClient.get_networks()`
  - Show a `voluptuous` selector with network names as options, `network_uid` as value
- [ ] Implement Step 3: confirmation / setup
  - Fetch EVSEs for the selected network
  - Create config entry data with tokens + network_uid
  - Show confirmation with network name and charger serial
- [ ] Implement `async_step_reauth` for expired refresh tokens
  - Re-use token entry form with context hinting re-authentication
  - On valid tokens, update existing config entry via `async_update_entry`
- [ ] Wire `async_step_reauth` → `async_step_user` with `reauth` context

## Group 5: Device Registry Setup

- [ ] In `async_setup_entry`, instantiate `DazeApiClient` using tokens from `config_entry.data`
- [ ] Fetch EVSE info to get `serialNumber`, `deviceProfile`, `firmwareVersion`, `softwareVersion`
- [ ] Create/update device registry entry via `async_get dr.async_get_or_create`:
  - `identifiers` = `{(DOMAIN, serial_number)}`
  - `manufacturer` = "Daze"
  - `model` = `deviceProfile`
  - `name` = `evse_name`
  - `sw_version` = `softwareVersion`
  - `hw_version` = `firmwareVersion`
  - `configuration_url` = "https://webportal.dazeservice.com"
- [ ] Store the coordinator and API client in `hass.data[DOMAIN]` for entity platforms

## Group 6: DataUpdateCoordinator

- [ ] Create `coordinator.py` with `DazeDataUpdateCoordinator` extending `DataUpdateCoordinator`
- [ ] Accept `hass`, `api_client`, `serial_number`, `network_uid`, polling interval (default 30s from `const.py`)
- [ ] Implement `async _async_update_data()`:
  - Fetch `get_socket_remote_info(serial)` for live metrics
  - If 401 during refresh: raise `ConfigEntryAuthFailed` to trigger re-auth
  - If other errors: raise `UpdateFailed` (coordinator handles retry)
- [ ] Expose `data` as a typed dict or dataclass with all sensor-relevant fields
- [ ] Wire coordinator instantiation in `async_setup_entry` before device registry

## Group 7: strings.json

- [ ] Write `strings.json` with:
  - `config.title` = "Daze Wallbox"
  - `step.user.data` labels for access token + refresh token
  - `step.user.description` explaining where to get tokens
  - `step.network.data` label for network selection
  - `step.network.description`
  - `step.confirm.title` and `step.confirm.description`
  - `error.invalid_token` and `error.network_error` messages
  - `reauth` section with title + description
  - Entity translation base structure for future sensor/switch/number entities

## Group 8: Manual Validation & Testing

- [ ] Manually test token entry: valid tokens proceed, invalid show error
- [ ] Manually test network selection: available networks listed, correct one selected
- [ ] Manually test re-auth: expire/invalidate refresh token, verify re-auth flow triggers
- [ ] Manually test coordinator: verify polling runs, data returned, entities become available/unavailable
- [ ] Verify `manifest.json` passes `hassfest` validation
- [ ] Verify HACS validation passes
- [ ] Verify ruff lint passes with no errors
