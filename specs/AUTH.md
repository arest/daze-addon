# Daze Auth Specification

## Overview

Daze uses Amazon Cognito (hosted at `daze.auth.eu-central-1.amazoncognito.com`) for OAuth 2.0 authentication. The integration **does not** collect email/password credentials from the user. Instead, the user manually provides:

- An **initial `access_token`** — short-lived Bearer token used for all API requests.
- A **`refresh_token`** — long-lived token used to obtain new access tokens before expiry.

This is a one-time setup. Once stored, the integration transparently refreshes the access token in the background.

---

## Token Lifecycle

```
User provides ──► Config entry stores ──► Integration uses
access_token       both tokens              access_token for
refresh_token                               API calls
                                            │
                              ┌─────────────┘
                              ▼
                      Token near expiry?
                     (401 on API call,
                      or proactive refresh)
                              │
                         Yes  │   No
                              │
                              ▼
                    POST /oauth2/token
                    grant_type=refresh_token
                              │
                     ┌────────┴────────┐
                     ▼                 ▼
                  200 OK             401/400
                     │                 │
                     ▼                 ▼
              Store new              Refresh token
              access_token           expired/invalid
              Retry API              ──────────────►
                                     Trigger
                                     re-authentication
                                     flow (user must
                                     provide new tokens)
```

### 1. Initial Provisioning (Config Flow Step)

The config flow asks the user for two fields:
- **Access Token** — the Bearer token obtained from the Daze webapp.
- **Refresh Token** — the Cognito refresh token obtained alongside the access token.

The integration validates these by calling `GET /oauth2/userInfo` with the access token. If the call succeeds, the tokens are stored in the config entry and the flow proceeds to network selection.

### 2. Token Refresh

When the access token expires (detected by a 401 response from any API call, or proactively before expiry), the integration calls:

```
POST https://daze.auth.eu-central-1.amazoncognito.com/oauth2/token
Content-Type: application/x-www-form-urlencoded

client_id=4m0rp7oqarbrc3hn67ivvonba8
&redirect_uri=https%3A%2F%2Fwebportal.dazeservice.com%2Fauthentication%2Fcallback
&grant_type=refresh_token
&refresh_token=<stored-refresh-token>
```

**Parameters:**

| Parameter      | Value                                                              |
|----------------|--------------------------------------------------------------------|
| `client_id`    | `4m0rp7oqarbrc3hn67ivvonba8` (Daze Cognito app client)            |
| `redirect_uri` | `https://webportal.dazeservice.com/authentication/callback`        |
| `grant_type`   | `refresh_token`                                                    |
| `refresh_token`| The user's refresh token (stored in config entry)                  |

**Success response (200):**

```json
{
    "access_token": "<new-access-token>",
    "expires_in": 3600,
    "token_type": "Bearer"
}
```

The new `access_token` is stored in the config entry (overwriting the old one). The `refresh_token` stays the same unless Cognito rotates it (rare — but if returned, update it too).

### 3. Re-authentication (Expired / Invalid Refresh Token)

If the refresh call returns 400 or 401 (refresh token is expired, revoked, or invalid), the integration:

1. Sets the config entry state to `ConfigEntryState.SETUP_ERROR`.
2. Raises `ConfigEntryAuthFailed` — Home Assistant automatically triggers the **re-authentication flow**.
3. The user is prompted to re-enter their access token and refresh token.
4. Once provided, the tokens are re-validated and the integration resumes normally.

---

## Token Storage

Tokens are stored exclusively in the `config_entry.data` dictionary:

```python
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_TOKEN_EXPIRY = "token_expiry"  # Optional: UNIX timestamp of access_token expiry
```

- Stored via `async_update_entry` when refreshed.
- Never written to `configuration.yaml`, `.storage/`, or any persistent file outside the HA config entry system.
- Tokens are **not** exposed through diagnostics (`diagnostics.py` must strip them before returning data).

---

## Auth Client Implementation

A dedicated `DazeAuthClient` class handles all token logic:

```
DazeAuthClient
├── access_token (str)
├── refresh_token (str)
├── token_expiry (float | None)
│
├── get_headers() → dict
│   Returns {"authorization": "Bearer <access_token>"}
│
├── is_token_expired() → bool
│   Compares current time against token_expiry (with 60s buffer)
│
├── refresh_access_token() → str
│   Calls Cognito /oauth2/token with refresh_token
│   Updates access_token and token_expiry
│   Returns new access_token
│   Raises AuthError on failure
│
├── validate_tokens() → bool
│   Calls GET /oauth2/userInfo to verify tokens are valid
│
└── async_get_tokens_for_store() → dict
    Returns sanitised dict for config entry storage
```

### Error handling

| Scenario | Action |
|----------|--------|
| API call returns 401 | Call `refresh_access_token()`, then retry the API call once. If retry also 401s, raise `ConfigEntryAuthFailed`. |
| Token refresh returns 400/401 | Raise `ConfigEntryAuthFailed` — triggers HA re-auth flow. |
| Network error during refresh | Log warning, keep old token, await next poll cycle. Coordinator handles retry. |

---

## API Endpoints Summary (Auth-related)

| Endpoint | Purpose |
|----------|---------|
| `GET https://daze.auth.eu-central-1.amazoncognito.com/oauth2/userInfo` | Validate access token, fetch user info |
| `POST https://daze.auth.eu-central-1.amazoncognito.com/oauth2/token` | Refresh access token via `grant_type=refresh_token` |

---

## Config Flow Steps

### Step 1: Token Entry

```
┌─────────────────────────────────────┐
│  Daze Wallbox                       │
│                                     │
│  Enter your Daze access token       │
│  and refresh token.                 │
│                                     │
│  Access Token  [________________]   │
│  Refresh Token [________________]   │
│                                     │
│  [Submit]                           │
└─────────────────────────────────────┘
```

- No email/password fields.
- No Cognito SRP or login UI — tokens are obtained by the user from the Daze webapp/developer console.
- On submit: `GET /oauth2/userInfo` is called to validate tokens.

### Step 2: Network Selection

After validation, the user selects which network (installation) to monitor:

```
┌─────────────────────────────────────┐
│  Select Network                     │
│                                     │
│  ○ casa (Italy)                     │
│  ○ ...                              │
│                                     │
│  [Submit]                           │
└─────────────────────────────────────┘
```

### Step 3: Confirmation

```
┌─────────────────────────────────────┐
│  ✓ Success!                         │
│                                     │
│  Network: casa                      │
│  Charger: ABCDE12345               │
│                                     │
│  [Finish]                           │
└─────────────────────────────────────┘
```
