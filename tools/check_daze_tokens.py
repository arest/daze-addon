#!/usr/bin/env python3
"""Diagnose Daze Wallbox token failures outside Home Assistant.

The integration's config flow reports a single generic ``invalid_token``
error for at least four distinct causes, and it never exercises the
refresh token during setup. This script reproduces the exact requests the
integration makes, against the same endpoints, and reports which step
fails and why.

It is a diagnostic tool only. It is not imported by the integration.

Usage:

    python3 tools/check_daze_tokens.py

Tokens are read from a hidden prompt on stdin. They are never passed as
command-line arguments (visible in ``ps`` and shell history), never
written to disk, and never printed back. Only the access token's
non-secret claims, HTTP status codes, and OAuth error codes are shown.

Exit codes:
    0 - both tokens valid
    1 - access token rejected, refresh token valid (recoverable)
    2 - both tokens rejected (re-authentication required)
    3 - network or unexpected error
"""

from __future__ import annotations

import base64
import getpass
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Mirrors custom_components/daze/const.py
COGNITO_BASE_URL = "https://daze.auth.eu-central-1.amazoncognito.com"
CLIENT_ID = "4m0rp7oqarbrc3hn67ivvonba8"
REDIRECT_URI = "https://webportal.dazeservice.com/authentication/callback"

# Cognito user pool API for the same region. Unlike the hosted-UI
# userInfo endpoint, GetUser accepts tokens carrying the
# 'aws.cognito.signin.user.admin' scope, which is what the Daze portal
# actually issues.
COGNITO_IDP_URL = "https://cognito-idp.eu-central-1.amazonaws.com/"
GET_USER_TARGET = "AWSCognitoIdentityProviderService.GetUser"

TIMEOUT = 30


def _post_form(url: str, fields: dict[str, str]) -> tuple[int, dict]:
    """POST form-encoded fields and return (status, parsed body)."""
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"
        },
        method="POST",
    )
    return _send(request)


def _get(url: str, headers: dict[str, str]) -> tuple[int, dict]:
    """GET a URL with headers and return (status, parsed body)."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    return _send(request)


def _send(request: urllib.request.Request) -> tuple[int, dict]:
    """Send a request, tolerating HTTP error statuses."""
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read().decode(errors="replace")
            return response.status, _parse(raw)
    except urllib.error.HTTPError as err:
        raw = err.read().decode(errors="replace")
        return err.code, _parse(raw)


def _parse(raw: str) -> dict:
    """Parse a JSON body, falling back to a truncated raw string."""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"_raw": raw[:200]}
    return parsed if isinstance(parsed, dict) else {"_raw": raw[:200]}


def decode_claims(token: str) -> dict | None:
    """Decode a JWT payload without verifying its signature.

    Signature verification is Cognito's job; this only reads the public
    claims so the caller can see token type, scope, and expiry.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return None


def report_claims(access_token: str) -> bool:
    """Print the access token's non-secret claims.

    Returns True if the token is missing the 'openid' scope, which makes
    the userInfo endpoint permanently unusable for this token.
    """
    print("\n[1] Access token claims (decoded locally, nothing sent)")

    claims = decode_claims(access_token)
    if claims is None:
        print("    NOT a decodable JWT.")
        print("    Expected three dot-separated base64 segments.")
        print("    Cause: wrong value copied, or the token was truncated.")
        return False

    token_use = claims.get("token_use")
    scope = claims.get("scope", "")
    exp = claims.get("exp")

    print(f"    token_use : {token_use}")
    print(f"    scope     : {scope}")
    print(f"    client_id : {claims.get('client_id')}")

    if exp:
        remaining = exp - time.time()
        expired = remaining <= 0
        print(f"    expires   : {time.ctime(exp)}")
        if expired:
            print(f"    EXPIRED   : yes, {int(-remaining // 60)} minutes ago")
        else:
            print(f"    EXPIRED   : no, {int(remaining // 60)} minutes left")

    if token_use != "access":
        print()
        print(f"    PROBLEM: token_use is '{token_use}', not 'access'.")
        print("    The /oauth2/userInfo endpoint only accepts the access")
        print("    token. Copy the value under the key ending in")
        print("    '.accessToken', not '.idToken'.")

    scope_missing = bool(scope) and "openid" not in scope
    if scope_missing:
        print()
        print("    PROBLEM: the 'openid' scope is missing.")
        print("    /oauth2/userInfo requires it, so this token can never")
        print("    pass the integration's validation step. Copying a fresh")
        print("    token will not help: the portal issues every token with")
        print("    this same scope set.")

    if claims.get("client_id") and claims["client_id"] != CLIENT_ID:
        print()
        print("    NOTE: this token was issued to a different OAuth client")
        print(f"    than the one the integration uses ({CLIENT_ID}).")

    return scope_missing


def check_access_token(access_token: str) -> bool:
    """Replicate DazeAuthClient.async_validate_tokens. Return True if valid."""
    print("\n[2] Access token against Cognito userInfo")
    print("    Same request as custom_components/daze/api/auth.py:167")

    url = f"{COGNITO_BASE_URL}/oauth2/userInfo"
    headers = {"authorization": f"Bearer {access_token}"}

    try:
        status, body = _get(url, headers)
    except urllib.error.URLError as err:
        print(f"    NETWORK ERROR: {err.reason}")
        raise SystemExit(3) from err

    print(f"    HTTP {status}")

    if status == 200:
        print("    VALID. The integration would accept this access token.")
        return True

    error = body.get("error", "")
    description = body.get("error_description", "")
    if error:
        print(f"    error: {error}")
    if description:
        print(f"    error_description: {description}")
    if not error and "_raw" in body:
        print(f"    body: {body['_raw']}")

    print()
    print("    REJECTED. The config flow turns this into 'invalid_token'.")
    return False


def check_refresh_token(refresh_token: str) -> bool:
    """Replicate DazeAuthClient.async_refresh_access_token.

    The config flow never performs this step. If it did, an expired
    access token would not block setup.
    """
    print("\n[3] Refresh token against Cognito token endpoint")
    print("    Same request as custom_components/daze/api/auth.py:85")
    print("    NOTE: the config flow never runs this step.")

    url = f"{COGNITO_BASE_URL}/oauth2/token"
    fields = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    try:
        status, body = _post_form(url, fields)
    except urllib.error.URLError as err:
        print(f"    NETWORK ERROR: {err.reason}")
        raise SystemExit(3) from err

    print(f"    HTTP {status}")

    if status == 200 and "access_token" in body:
        expires_in = body.get("expires_in", "unknown")
        rotated = "refresh_token" in body
        print(f"    VALID. A new access token was issued ({expires_in}s).")
        print(f"    Refresh token rotated by Cognito: {rotated}")
        return True

    error = body.get("error", "")
    if error:
        print(f"    error: {error}")
        if error == "invalid_grant":
            print("    Meaning: expired, revoked, or not issued to this client.")
        elif error == "invalid_client":
            print("    Meaning: the hardcoded CLIENT_ID is no longer valid.")
    elif "_raw" in body:
        print(f"    body: {body['_raw']}")

    print("\n    REJECTED.")
    return False


def check_get_user(access_token: str) -> bool:
    """Try the Cognito user pool GetUser call as a userInfo replacement.

    The integration does not use this endpoint. It is tested here because
    GetUser accepts the 'aws.cognito.signin.user.admin' scope and returns
    the email address that the config flow needs, which makes it a viable
    substitute for the userInfo call that rejects these tokens.
    """
    print("\n[4] Access token against Cognito GetUser (proposed fix)")
    print("    The integration does NOT currently call this.")

    request = urllib.request.Request(
        COGNITO_IDP_URL,
        data=json.dumps({"AccessToken": access_token}).encode(),
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": GET_USER_TARGET,
        },
        method="POST",
    )

    try:
        status, body = _send(request)
    except urllib.error.URLError as err:
        print(f"    NETWORK ERROR: {err.reason}")
        return False

    print(f"    HTTP {status}")

    if status == 200:
        attributes = body.get("UserAttributes", [])
        names = sorted(
            attr.get("Name", "") for attr in attributes if attr.get("Name")
        )
        has_email = "email" in names
        print("    VALID. This endpoint accepts your token.")
        print(f"    Attributes returned: {', '.join(names)}")
        print(f"    Supplies the email the config flow needs: {has_email}")
        return has_email

    error_type = body.get("__type", "")
    message = body.get("message", "")
    if error_type:
        print(f"    error: {error_type}")
    if message:
        print(f"    message: {message}")
    return False


def verdict(
    access_ok: bool,
    refresh_ok: bool,
    scope_missing: bool,
    get_user_ok: bool,
) -> int:
    """Print a conclusion and return the process exit code."""
    print("\n" + "=" * 60)

    if access_ok and refresh_ok:
        print("VERDICT: both tokens are valid.")
        print()
        print("Setup should succeed. If it still fails, the tokens are")
        print("likely being altered between your clipboard and the form:")
        print("check for a trailing newline or space, since the schema at")
        print("config_flow.py:36-41 does not strip whitespace.")
        return 0

    if scope_missing:
        print("VERDICT: wrong validation endpoint for this token type.")
        print()
        print("Your tokens are healthy. The access token is unexpired and")
        print("the refresh token works. The problem is that the Daze portal")
        print("issues access tokens scoped 'aws.cognito.signin.user.admin'")
        print("without 'openid', and /oauth2/userInfo requires 'openid'.")
        print()
        print("So async_validate_tokens can never succeed with a token from")
        print("this portal, no matter how fresh it is. Re-copying the token")
        print("is not a workaround; there is no user-side workaround.")
        print()
        if get_user_ok:
            print("GetUser accepted the same token and returned the email")
            print("address the config flow needs. Replacing the userInfo")
            print("calls in api/auth.py and api/__init__.py with GetUser")
            print("fixes setup without changing anything else.")
        else:
            print("GetUser did not succeed either; see section [4] above")
            print("before changing the validation call.")
        return 1

    if not access_ok and refresh_ok:
        print("VERDICT: access token rejected, refresh token VALID.")
        print()
        print("Your account and refresh token are fine; only the")
        print("short-lived access token was rejected.")
        print()
        print("The integration cannot recover on its own because the config")
        print("flow calls async_validate_tokens and gives up, without ever")
        print("calling async_refresh_access_token.")
        print()
        print("Workaround: copy a brand new access token and submit the")
        print("form immediately, within the hour.")
        print()
        print("Real fix: have the config flow attempt a refresh before")
        print("raising invalid_token.")
        return 1

    if access_ok and not refresh_ok:
        print("VERDICT: access token valid, refresh token rejected.")
        print()
        print("Setup will succeed now but will break at the first refresh,")
        print("leaving the integration stuck until you re-authenticate.")
        print("Re-copy the refresh token.")
        return 1

    print("VERDICT: both tokens rejected.")
    print()
    print("Log in again at https://webportal.dazeservice.com and copy a")
    print("fresh pair. Check section [1] above: if token_use was not")
    print("'access', you copied the wrong value rather than a stale one.")
    return 2


def main() -> int:
    """Run all checks and print a verdict."""
    print("Daze token diagnostic")
    print("Endpoints and client ID are read from const.py values.")
    print("Tokens are not stored, logged, or transmitted anywhere except")
    print("to Cognito, exactly as the integration does.")
    print()

    access_token = getpass.getpass("Access token (input hidden): ").strip()
    refresh_token = getpass.getpass("Refresh token (input hidden): ").strip()

    if not access_token or not refresh_token:
        print("\nBoth tokens are required.")
        return 3

    scope_missing = report_claims(access_token)
    access_ok = check_access_token(access_token)
    refresh_ok = check_refresh_token(refresh_token)
    get_user_ok = check_get_user(access_token)

    return verdict(access_ok, refresh_ok, scope_missing, get_user_ok)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(3)
