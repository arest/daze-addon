#!/usr/bin/env python3
"""Find the working recharge-session endpoint on the Daze web API.

The integration requests:

    GET /v3/networks/{network_uid}/rechargeSessions?TotalLimit=1000

which returns HTTP 404 with an empty body. The same network UID works
for /v3/networks/{uid}/evses, so the UID and the base path are correct
and only this sub-resource is wrong.

This script probes plausible alternatives and reports which ones answer,
so the fix is based on a measurement rather than a guess. Every request
is a GET; nothing is created, modified, or deleted.

Usage:

    python3 tools/probe_sessions_endpoint.py

Tokens are read from hidden prompts, never passed as arguments, never
written to disk, and never printed. A fresh access token is obtained
from the refresh token first, so an hour-old access token is fine.
"""

from __future__ import annotations

import getpass
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# Mirrors custom_components/daze/const.py
API_BASE_URL = "https://webapi.dazeservice.com/v3"
COGNITO_BASE_URL = "https://daze.auth.eu-central-1.amazoncognito.com"
COGNITO_IDP_URL = "https://cognito-idp.eu-central-1.amazonaws.com/"
CLIENT_ID = "4m0rp7oqarbrc3hn67ivvonba8"
REDIRECT_URI = "https://webportal.dazeservice.com/authentication/callback"
GET_USER_TARGET = "AWSCognitoIdentityProviderService.GetUser"

TIMEOUT = 30


def _send(request: urllib.request.Request) -> tuple[int, object]:
    """Send a request, tolerating HTTP error statuses."""
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read().decode(errors="replace")
            return response.status, _parse(raw)
    except urllib.error.HTTPError as err:
        raw = err.read().decode(errors="replace")
        return err.code, _parse(raw)
    except urllib.error.URLError as err:
        return 0, {"_error": str(err.reason)}


def _parse(raw: str) -> object:
    """Parse a JSON body, falling back to a truncated raw string."""
    if not raw.strip():
        return {"_empty": True}
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw[:200]}


def refresh_access_token(refresh_token: str) -> str:
    """Exchange a refresh token for a fresh access token."""
    data = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    ).encode()

    request = urllib.request.Request(
        f"{COGNITO_BASE_URL}/oauth2/token",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"
        },
        method="POST",
    )

    status, body = _send(request)
    if status != 200 or not isinstance(body, dict):
        print(f"Could not refresh the access token (HTTP {status}).")
        raise SystemExit(2)

    token = body.get("access_token")
    if not isinstance(token, str):
        print("Refresh succeeded but returned no access token.")
        raise SystemExit(2)

    return token


def get_email(access_token: str) -> str:
    """Read the account email via Cognito GetUser."""
    request = urllib.request.Request(
        COGNITO_IDP_URL,
        data=json.dumps({"AccessToken": access_token}).encode(),
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": GET_USER_TARGET,
        },
        method="POST",
    )

    status, body = _send(request)
    if status != 200 or not isinstance(body, dict):
        return ""

    for attribute in body.get("UserAttributes", []):
        if isinstance(attribute, dict) and attribute.get("Name") == "email":
            return str(attribute.get("Value", ""))

    return ""


def probe(access_token: str, path: str) -> tuple[int, str]:
    """GET one API path and summarise the response shape."""
    request = urllib.request.Request(
        f"{API_BASE_URL}{path}",
        headers={"authorization": f"Bearer {access_token}"},
        method="GET",
    )

    status, body = _send(request)

    if status == 200 and isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, list):
            return status, f"list of {len(data)} item(s)"
        if isinstance(data, dict):
            return status, f"object with keys {sorted(data)[:6]}"
        return status, f"keys {sorted(body)[:6]}"

    if isinstance(body, dict):
        if body.get("_empty"):
            return status, "empty body"
        if body.get("_error"):
            return status, f"network error: {body['_error']}"
        message = body.get("message") or body.get("error")
        if message:
            return status, str(message)[:120]
        return status, f"keys {sorted(body)[:6]}"

    return status, str(body)[:120]


def build_candidates(network_uid: str, serial: str, email: str) -> list[str]:
    """Build the list of paths to probe, most likely first."""
    uid = urllib.parse.quote(network_uid, safe="")
    ser = urllib.parse.quote(serial, safe="")
    mail = urllib.parse.quote(email, safe="") if email else ""

    candidates = [
        # What the integration currently requests.
        f"/networks/{uid}/rechargeSessions?TotalLimit=1000",
        # Same path, no query, in case the parameter is the problem.
        f"/networks/{uid}/rechargeSessions",
        # Known-good sibling, to prove the UID and base path are fine.
        f"/networks/{uid}/evses?includeEcoInfo=false",
        # Casing and separator variants.
        f"/networks/{uid}/rechargesessions",
        f"/networks/{uid}/recharge-sessions",
        f"/networks/{uid}/sessions",
        # Session history hung off the charger rather than the network.
        f"/evses/{ser}/rechargeSessions",
        f"/evses/{ser}/sessions",
        f"/sockets/{ser}/rechargeSessions",
        f"/sockets/{ser}/sessions",
        # Alternative query parameter spellings.
        f"/networks/{uid}/rechargeSessions?limit=100",
        f"/networks/{uid}/rechargeSessions?totalLimit=1000",
    ]

    if mail:
        candidates.append(f"/users/{mail}/rechargeSessions")
        candidates.append(f"/users/{mail}/networks?includeStats=true")

    return candidates


def discover(access_token: str, email: str) -> tuple[str, str]:
    """Look up the network UID and wallbox serial from the API.

    Mirrors what the config flow does, so the probe needs nothing typed
    beyond the refresh token.

    Returns:
        A tuple of (network_uid, serial). Either may be empty if
        discovery failed.

    """
    if not email:
        return "", ""

    mail = urllib.parse.quote(email, safe="")
    status, body = probe_raw(
        access_token, f"/users/{mail}/networks?includeStats=true"
    )

    networks = body.get("data") if isinstance(body, dict) else None
    if status != 200 or not isinstance(networks, list) or not networks:
        print(f"  Could not list networks (HTTP {status}).")
        return "", ""

    print(f"  Networks found: {len(networks)}")
    for net in networks:
        if isinstance(net, dict):
            print(f"    - {net.get('name', '?')}  uid={net.get('uid', '?')}")

    first = networks[0] if isinstance(networks[0], dict) else {}
    network_uid = str(first.get("uid", ""))
    if not network_uid:
        return "", ""

    uid = urllib.parse.quote(network_uid, safe="")
    status, body = probe_raw(
        access_token, f"/networks/{uid}/evses?includeEcoInfo=false"
    )

    evses = body.get("data") if isinstance(body, dict) else None
    if status != 200 or not isinstance(evses, list) or not evses:
        print(f"  Could not list chargers (HTTP {status}).")
        return network_uid, ""

    print(f"  Chargers found: {len(evses)}")
    for evse in evses:
        if isinstance(evse, dict):
            print(
                f"    - {evse.get('evseName', '?')}  "
                f"serial={evse.get('serialNumber', '?')}"
            )

    if len(evses) > 1:
        print(
            "  NOTE: more than one charger. The integration only ever "
            "uses the first (config_flow.py:304)."
        )

    first_evse = evses[0] if isinstance(evses[0], dict) else {}
    return network_uid, str(first_evse.get("serialNumber", ""))


def probe_raw(access_token: str, path: str) -> tuple[int, object]:
    """GET one API path and return the raw status and parsed body."""
    request = urllib.request.Request(
        f"{API_BASE_URL}{path}",
        headers={"authorization": f"Bearer {access_token}"},
        method="GET",
    )
    return _send(request)


def main() -> int:
    """Probe every candidate endpoint and summarise the findings."""
    print("Daze recharge-session endpoint probe")
    print("All requests are GETs. Nothing is modified.")
    print()

    refresh_token = getpass.getpass("Refresh token (input hidden): ").strip()
    if not refresh_token:
        print("A refresh token is required.")
        return 3

    print("\nRefreshing the access token...")
    access_token = refresh_access_token(refresh_token)
    print("Got a fresh access token.")

    email = get_email(access_token)
    print(f"Account email resolved: {'yes' if email else 'no'}")

    print("\nDiscovering network and charger (same calls as the config flow):")
    network_uid, serial = discover(access_token, email)

    if not network_uid:
        network_uid = input("  Network UID (discovery failed): ").strip()
    if not serial:
        serial = input("  Wallbox serial (discovery failed): ").strip()

    if not network_uid or not serial:
        print("Need both a network UID and a serial to continue.")
        return 3

    candidates = build_candidates(network_uid, serial, email)

    print(f"\nProbing {len(candidates)} endpoints:\n")

    working: list[tuple[str, str]] = []
    for path in candidates:
        status, summary = probe(access_token, path)
        marker = "OK  " if status == 200 else "    "
        display = path.replace(network_uid, "{uid}").replace(serial, "{serial}")
        if email:
            display = display.replace(urllib.parse.quote(email, safe=""), "{email}")
        print(f"  {marker}{status:>3}  {display}")
        print(f"          {summary}")
        if status == 200:
            working.append((display, summary))

    print("\n" + "=" * 60)

    if not working:
        print("No candidate returned 200.")
        print()
        print("Capture the real request from the browser instead: open")
        print("https://webportal.dazeservice.com, view your charging")
        print("history, and look in DevTools > Network for the request")
        print("the page makes. The path it uses is the one to implement.")
        return 1

    print(f"{len(working)} endpoint(s) responded:")
    for path, summary in working:
        print(f"  {path}")
        print(f"      {summary}")

    print()
    print("If the evses path is the only one that worked, the session")
    print("endpoint has moved or never existed at that path; capture the")
    print("real one from the portal's DevTools Network tab.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(3)
