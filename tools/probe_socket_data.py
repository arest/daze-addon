#!/usr/bin/env python3
"""Compare the live socket payload against the keys the sensors expect.

Entities appear in Home Assistant but read Unknown when the API returns
200 with a payload whose field names do not match what the sensor
catalog looks up. Every value_fn returns None, so there is no error to
log and nothing to see.

This script fetches the same endpoint the coordinator polls, extracts
the key names the catalog expects directly from the source, and reports
which are present, which are missing, and which fields the API returned
that nothing reads. For each missing key it suggests the closest
available name.

Usage:

    python3 tools/probe_socket_data.py

Only the refresh token is typed; the network and charger are discovered
the same way the config flow discovers them. Every request is a GET.
Nothing is created, modified, or deleted.
"""

from __future__ import annotations

import ast
import difflib
import getpass
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Mirrors custom_components/daze/const.py
API_BASE_URL = "https://webapi.dazeservice.com/v3"
COGNITO_BASE_URL = "https://daze.auth.eu-central-1.amazoncognito.com"
COGNITO_IDP_URL = "https://cognito-idp.eu-central-1.amazonaws.com/"
CLIENT_ID = "4m0rp7oqarbrc3hn67ivvonba8"
REDIRECT_URI = "https://webportal.dazeservice.com/authentication/callback"
GET_USER_TARGET = "AWSCognitoIdentityProviderService.GetUser"

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "custom_components" / "daze" / "sensor_catalog.py"

# Keys the coordinator computes locally rather than reading from the
# API, so their absence from the payload is expected.
LOCALLY_COMPUTED = {
    "last_session_cost",
    "last_session_duration",
    "last_session_end",
    "last_session_energy",
    "last_session_start",
    "lifetime_energy",
    "total_sessions",
}

TIMEOUT = 30


def _send(request: urllib.request.Request) -> tuple[int, object]:
    """Send a request, tolerating HTTP error statuses."""
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read().decode(errors="replace")
            return response.status, _parse(raw)
    except urllib.error.HTTPError as err:
        return err.code, _parse(err.read().decode(errors="replace"))
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


def _api_get(access_token: str, path: str) -> tuple[int, object]:
    """GET an API path with the bearer token."""
    request = urllib.request.Request(
        f"{API_BASE_URL}{path}",
        headers={"authorization": f"Bearer {access_token}"},
        method="GET",
    )
    return _send(request)


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


def discover_serial(access_token: str, email: str) -> str:
    """Find the first wallbox serial, as the config flow does."""
    if not email:
        return ""

    mail = urllib.parse.quote(email, safe="")
    status, body = _api_get(
        access_token, f"/users/{mail}/networks?includeStats=true"
    )
    networks = body.get("data") if isinstance(body, dict) else None
    if status != 200 or not isinstance(networks, list) or not networks:
        return ""

    first = networks[0] if isinstance(networks[0], dict) else {}
    uid = urllib.parse.quote(str(first.get("uid", "")), safe="")
    if not uid:
        return ""

    status, body = _api_get(
        access_token, f"/networks/{uid}/evses?includeEcoInfo=false"
    )
    evses = body.get("data") if isinstance(body, dict) else None
    if status != 200 or not isinstance(evses, list) or not evses:
        return ""

    first_evse = evses[0] if isinstance(evses[0], dict) else {}
    return str(first_evse.get("serialNumber", ""))


def expected_keys() -> set[str]:
    """Extract the API field names the sensor catalog looks up."""
    tree = ast.parse(CATALOG.read_text(encoding="utf-8"))
    keys: set[str] = set()

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
        ):
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and isinstance(
                argument.value, str
            ):
                keys.add(argument.value)

        # Schedule keys live in a module-level tuple, not a .get() call.
        # The assignment may or may not carry a type annotation.
        targets: list[ast.expr] = []
        value: ast.expr | None = None

        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value

        if value is not None and isinstance(value, ast.Tuple):
            for target in targets:
                if getattr(target, "id", "").endswith("_KEYS"):
                    for element in value.elts:
                        if isinstance(element, ast.Constant) and isinstance(
                            element.value, str
                        ):
                            keys.add(element.value)

    return keys


def describe(value: object) -> str:
    """Summarise a value's type and content compactly."""
    if isinstance(value, bool):
        return f"bool {value}"
    if isinstance(value, (int, float)):
        return f"number {value}"
    if isinstance(value, str):
        return f'string "{value[:40]}"'
    if isinstance(value, list):
        return f"list of {len(value)}"
    if isinstance(value, dict):
        return f"object with keys {sorted(value)[:5]}"
    if value is None:
        return "null"
    return type(value).__name__


def dump(value: object, indent: int = 2, path: str = "") -> None:
    """Print a nested structure in full, one leaf per line."""
    pad = " " * indent

    if isinstance(value, dict):
        for key in sorted(value):
            child = value[key]
            child_path = f"{path}.{key}" if path else key
            if isinstance(child, (dict, list)) and child:
                print(f"{pad}{key}:")
                dump(child, indent + 2, child_path)
            else:
                print(f"{pad}{key} = {describe(child)}")
        return

    if isinstance(value, list):
        for index, child in enumerate(value[:3]):
            print(f"{pad}[{index}]:")
            dump(child, indent + 2, f"{path}[{index}]")
        if len(value) > 3:
            print(f"{pad}... {len(value) - 3} more item(s)")
        return

    print(f"{pad}{describe(value)}")


def find_paths(
    tree: object, wanted: set[str], path: str = ""
) -> dict[str, list[str]]:
    """Locate every wanted key anywhere in a nested structure."""
    found: dict[str, list[str]] = {}

    if isinstance(tree, dict):
        for key, child in tree.items():
            child_path = f"{path}.{key}" if path else key
            if key in wanted:
                found.setdefault(key, []).append(child_path)
            for name, paths in find_paths(child, wanted, child_path).items():
                found.setdefault(name, []).extend(paths)

    elif isinstance(tree, list):
        for index, child in enumerate(tree):
            child_path = f"{path}[{index}]"
            for name, paths in find_paths(child, wanted, child_path).items():
                found.setdefault(name, []).extend(paths)

    return found


def also_probe_evse(access_token: str, email: str) -> None:
    """Dump the EVSE record, where control fields may live.

    The number, select and switch entities read
    maxExternalChargingCurrentInMilliAmps, ecoModeEnabled and
    evseStatus. None appear in the socket payload, so check whether the
    EVSE listing carries them instead.
    """
    if not email:
        return

    mail = urllib.parse.quote(email, safe="")
    status, body = _api_get(
        access_token, f"/users/{mail}/networks?includeStats=true"
    )
    networks = body.get("data") if isinstance(body, dict) else None
    if status != 200 or not isinstance(networks, list) or not networks:
        return

    uid = urllib.parse.quote(str(networks[0].get("uid", "")), safe="")
    status, body = _api_get(
        access_token, f"/networks/{uid}/evses?includeEcoInfo=true"
    )
    evses = body.get("data") if isinstance(body, dict) else None
    if status != 200 or not isinstance(evses, list) or not evses:
        print(f"\nCould not read the EVSE record (HTTP {status}).")
        return

    print("\n" + "=" * 60)
    print("EVSE record (/networks/{uid}/evses?includeEcoInfo=true):\n")
    dump(evses[0])

    control_fields = {
        "maxExternalChargingCurrentInMilliAmps",
        "lastMaxChargingCurrent",
        "ecoModeEnabled",
        "operationMode",
        "evseStatus",
    }
    hits = find_paths(evses[0], control_fields)

    print("\nControl fields the number/select/switch entities read:")
    for field in sorted(control_fields):
        paths = hits.get(field)
        print(f"  {'FOUND ' if paths else 'ABSENT'} {field}"
              + (f"   at {paths[0]}" if paths else ""))


def main() -> int:
    """Fetch the live payload and diff it against the catalog."""
    print("Daze socket payload probe")
    print("All requests are GETs. Nothing is modified.")
    print()

    refresh_token = getpass.getpass("Refresh token (input hidden): ").strip()
    if not refresh_token:
        print("A refresh token is required.")
        return 3

    access_token = refresh_access_token(refresh_token)
    email = get_email(access_token)
    serial = discover_serial(access_token, email)

    if not serial:
        serial = input("Wallbox serial (discovery failed): ").strip()
    if not serial:
        print("A serial number is required.")
        return 3

    print(f"Charger: {serial}")

    path = (
        f"/sockets/{urllib.parse.quote(serial, safe='')}/remoteInfo"
        "?includeEcoInfo=true&includeNextSchedule=true"
    )
    print(f"\nGET {path}")

    status, body = _api_get(access_token, path)
    print(f"HTTP {status}")

    if status != 200 or not isinstance(body, dict):
        print("\nThe endpoint the coordinator polls did not return data.")
        print(f"Response: {describe(body)}")
        return 1

    data = body.get("data")
    if not isinstance(data, dict):
        print("\nResponse had no 'data' object. Top-level keys:")
        for key in sorted(body):
            print(f"  {key}: {describe(body[key])}")
        print("\nThe coordinator reads response['data'], so nothing is read.")
        return 1

    print(f"\nFull payload tree ({len(data)} top-level field(s)):\n")
    dump(data)

    # The metrics may be nested, so locate every expected key anywhere
    # in the tree rather than only at the top level.
    wanted = expected_keys() - LOCALLY_COMPUTED
    found = find_paths(data, wanted)

    print("\n" + "=" * 60)
    print("Where each expected field actually lives:\n")
    for key in sorted(wanted):
        paths = found.get(key)
        if paths:
            for path in paths:
                location = "top level" if path == key else f"at {path}"
                print(f"  FOUND   {key:<32} {location}")
        else:
            print(f"  ABSENT  {key}")

    also_probe_evse(access_token, email)

    present = sorted(k for k in wanted if k in data)
    missing = sorted(k for k in wanted if k not in data)
    unused = sorted(k for k in data if k not in wanted)

    print("\n" + "=" * 60)
    print(f"Catalog expects {len(wanted)} field(s) from this payload.")
    print(f"  present: {len(present)}")
    print(f"  missing: {len(missing)}")

    if present:
        print("\nWorking (sensors for these should show values):")
        for key in present:
            print(f"  {key} = {describe(data[key])}")

    if missing:
        print("\nMissing (these sensors will read Unknown):")
        for key in missing:
            close = difflib.get_close_matches(key, list(data), n=2, cutoff=0.5)
            hint = f"   closest in payload: {', '.join(close)}" if close else ""
            print(f"  {key}{hint}")

    if unused:
        print("\nReturned by the API but read by nothing:")
        for key in unused:
            print(f"  {key} = {describe(data[key])}")

    print("\n" + "=" * 60)
    if not missing:
        print("Every expected field is present. The mismatch is elsewhere.")
        return 0

    print(f"{len(missing)} of {len(wanted)} expected fields are absent.")
    print("The sensor catalog's field names do not match this API.")
    print("Map each missing name to the matching field listed above.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(3)
