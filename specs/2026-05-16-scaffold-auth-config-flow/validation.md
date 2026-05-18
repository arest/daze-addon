# Validation: Scaffold, Auth & Config Flow

## Acceptance Criteria

### Package Structure
- [ ] `custom_components/daze/` exists with `__init__.py`, `const.py`, `manifest.json`, `config_flow.py`, `coordinator.py`, `api/__init__.py`, `api/auth.py`, and empty platform stubs
- [ ] `manifest.json` passes `hassfest` validation
- [ ] `manifest.json` passes HACS validation
- [ ] Ruff lint passes with 0 errors on all files

### DazeAuthClient
- [ ] `get_headers()` returns `{"authorization": "Bearer <token>"}`
- [ ] `is_token_expired()` returns `True` when token is past expiry (with 60s buffer)
- [ ] `async refresh_access_token()` successfully obtains a new token from Cognito
- [ ] `async refresh_access_token()` updates stored access_token and token_expiry
- [ ] `async refresh_access_token()` raises `AuthError` on 400/401 from Cognito
- [ ] `async refresh_access_token()` raises `AuthError` on network errors
- [ ] `async validate_tokens()` returns `True` for valid tokens (200 from /userInfo)
- [ ] `async validate_tokens()` raises on invalid/expired tokens

### DazeApiClient
- [ ] `async get_user_info()` returns parsed JSON from Cognito userInfo endpoint
- [ ] `async get_networks()` returns network list from `v3/users/{email}/networks`
- [ ] `async get_evses()` returns EVSE list for a given network
- [ ] `async get_socket_remote_info()` returns live socket data
- [ ] `async set_max_charging_current()` returns success response
- [ ] `async start_charge()` / `async stop_charge()` return success response
- [ ] `async get_recharge_sessions()` returns session list
- [ ] Auto-refresh on 401: a single 401 triggers token refresh and retries the request
- [ ] Double 401: second consecutive 401 raises `ApiAuthError` (triggers re-auth)

### Config Flow
- [ ] Step 1 (token entry): valid tokens proceed to network selection; invalid tokens show form error
- [ ] Step 2 (network selection): available networks are listed; user can select one
- [ ] Step 3 (confirmation): shows network name and charger serial; setup completes
- [ ] Re-auth flow: expired refresh token triggers re-auth; valid new tokens update existing config entry
- [ ] Config entry is created with all required data keys (access_token, refresh_token, network_uid, etc.)
- [ ] Config entry data does NOT include raw API responses or unnecessary metadata

### Device Registry
- [ ] Device created with identifiers = `{(DOMAIN, serial_number)}`
- [ ] Device has manufacturer "Daze", model from `deviceProfile`, sw_version, hw_version
- [ ] Device name matches the EVSE name from the API
- [ ] Device linked to the config entry

### DataUpdateCoordinator
- [ ] Coordinator polls at configured interval (default 30s)
- [ ] `_async_update_data()` returns structured data with all socket fields
- [ ] On 401 → auth failure → `ConfigEntryAuthFailed` raised → re-auth flow triggered
- [ ] On transient errors → `UpdateFailed` raised → coordinator retries
- [ ] Entities become unavailable when coordinator update fails repeatedly

### strings.json
- [ ] All config flow strings present for all 3 steps
- [ ] Re-auth strings present
- [ ] Error strings for invalid_token and network_error
- [ ] Entity translation stubs present for future platforms

## Testing

### Manual Test Flow

1. **Fresh HA instance with Daze integration**
   - Install custom component via HACS or manual `custom_components/daze/` copy
   - Restart HA
   - Verify integration appears in Settings → Devices & Services → Add Integration

2. **Token entry with valid tokens**
   - Enter valid access token and refresh token
   - Verify validation succeeds and proceeds to network selection

3. **Token entry with invalid tokens**
   - Enter garbage access token
   - Verify form shows error message (invalid token)

4. **Network selection**
   - Verify available network(s) listed
   - Select network and proceed

5. **Setup complete**
   - Verify device appears in HA with correct name, model, firmware
   - Verify config entry is created

6. **Re-auth test**
   - Manually corrupt refresh token in config entry (via HA's config entry YAML view or SQLite)
   - Trigger a coordinator update
   - Verify re-auth notification appears in HA
   - Enter new valid tokens
   - Verify integration resumes normally

7. **Coordinator polling**
   - Observe HA logs show periodic API calls
   - Verify device entities show correct availability

### Lint / Static Checks

```bash
# Run ruff lint
ruff check custom_components/daze/

# Run hassfest
python -m script.hassfest --integration-path custom_components/daze

# HACS validation (if HACS CLI is available)
hacs validate custom_components/daze/
```

## Merge Conditions

- [ ] All acceptance criteria met
- [ ] Ruff lint passes with 0 errors
- [ ] `hassfest` validation passes
- [ ] HACS validation passes
- [ ] Manual test flow completed successfully in a test HA instance
- [ ] No regressions on existing code
- [ ] Code reviewed (self-review or peer)
