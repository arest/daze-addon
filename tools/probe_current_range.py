#!/usr/bin/env python3
"""Find which charging currents the wallbox actually accepts.

Setting the maximum charging current can fail with:

    422 code 369, MaxExternalChargingCurrentOutOfRange

The installation rating does not explain it. A charger rated 1.5 to
7.4 kW single phase is 6.5 to 32 A and reports
lastMaxInstallationCurrent 32000, yet still rejects some values in that
span. A grid power cap or dynamic power management is the likely
reason, and neither is exposed as a field, so the accepted range has to
be measured.

This script walks a ladder of currents, reports which are accepted, and
restores the original setting when it finishes.

WARNING: this WRITES configuration to your wallbox. Each step changes
the charging current limit, which will affect an active charge while
the script runs. The original value is read first and restored at the
end, including on Ctrl-C.

Usage:

    python3 tools/probe_current_range.py            # prompts
    python3 tools/probe_current_range.py --yes      # no confirmation
"""

from __future__ import annotations

import getpass
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE_URL = "https://webapi.dazeservice.com/v3"
COGNITO_BASE_URL = "https://daze.auth.eu-central-1.amazoncognito.com"
COGNITO_IDP_URL = "https://cognito-idp.eu-central-1.amazonaws.com/"
CLIENT_ID = "4m0rp7oqarbrc3hn67ivvonba8"
REDIRECT_URI = "https://webportal.dazeservice.com/authentication/callback"
GET_USER_TARGET = "AWSCognitoIdentityProviderService.GetUser"

# The floor is not 6 A. A charger rated 1.5 kW minimum at 230 V will
# not accept less than about 6520 mA, and 6000 was rejected as out of
# range. The ladder therefore starts just below the observed floor to
# confirm where it sits, and reaches the 7.4 kW rating at the top.
LADDER = (
    6000,   # expected to fail: below a 1.5 kW floor
    6400,
    6521,   # 1500 W at 230 V
    7000,
    8000,
    10000,
    13000,
    16000,
    20000,
    26000,
    32000,  # 7360 W at 230 V, near the 7.4 kW rating
)

TIMEOUT = 30


def _send(
    request: urllib.request.Request, attempts: int = 3
) -> tuple[int, object]:
    """Send a request, tolerating HTTP errors and network timeouts.

    The Daze API stalls occasionally. A timeout crashed the first
    version of this script mid-ladder, which is the one place a crash
    is expensive: the original setting may not have been restored yet.
    """
    last: tuple[int, object] = (0, {"_error": "not attempted"})

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.status, _parse(
                    response.read().decode(errors="replace")
                )
        except urllib.error.HTTPError as err:
            return err.code, _parse(err.read().decode(errors="replace"))
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            reason = getattr(err, "reason", err)
            last = (0, {"_error": str(reason)})
            if attempt < attempts:
                print(f"      network problem ({reason}), retrying")
                time.sleep(3)

    return last


def _parse(raw: str) -> object:
    """Parse a JSON body, falling back to a truncated raw string."""
    if not raw.strip():
        return {"_empty": True}
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw[:300]}


def _get(token: str, path: str) -> tuple[int, object]:
    """GET an API path with the bearer token."""
    return _send(
        urllib.request.Request(
            f"{API_BASE_URL}{path}",
            headers={"authorization": f"Bearer {token}"},
            method="GET",
        )
    )


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

    status, body = _send(
        urllib.request.Request(
            f"{COGNITO_BASE_URL}/oauth2/token",
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"
            },
            method="POST",
        )
    )

    if status != 200 or not isinstance(body, dict):
        print(f"Could not refresh the access token (HTTP {status}).")
        raise SystemExit(2)

    token = body.get("access_token")
    if not isinstance(token, str):
        print("Refresh succeeded but returned no access token.")
        raise SystemExit(2)

    return token


def get_email(token: str) -> str:
    """Read the account email via Cognito GetUser."""
    status, body = _send(
        urllib.request.Request(
            COGNITO_IDP_URL,
            data=json.dumps({"AccessToken": token}).encode(),
            headers={
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": GET_USER_TARGET,
            },
            method="POST",
        )
    )

    if status != 200 or not isinstance(body, dict):
        return ""

    for attribute in body.get("UserAttributes", []):
        if isinstance(attribute, dict) and attribute.get("Name") == "email":
            return str(attribute.get("Value", ""))

    return ""


def discover(token: str, email: str) -> tuple[str, dict]:
    """Return the first charger's serial and its EVSE record."""
    if not email:
        return "", {}

    mail = urllib.parse.quote(email, safe="")
    status, body = _get(token, f"/users/{mail}/networks?includeStats=true")
    networks = body.get("data") if isinstance(body, dict) else None
    if status != 200 or not isinstance(networks, list) or not networks:
        return "", {}

    uid = urllib.parse.quote(str(networks[0].get("uid", "")), safe="")
    status, body = _get(token, f"/networks/{uid}/evses?includeEcoInfo=true")
    evses = body.get("data") if isinstance(body, dict) else None
    if status != 200 or not isinstance(evses, list) or not evses:
        return "", {}

    record = evses[0] if isinstance(evses[0], dict) else {}
    return str(record.get("serialNumber", "")), record


def set_current(token: str, serial: str, milliamps: int) -> tuple[int, object]:
    """Attempt to set the maximum charging current."""
    quoted = urllib.parse.quote(serial, safe="")
    payload = {
        "evseSerialNumber": serial,
        "maxExternalChargingCurrentInMilliAmps": milliamps,
    }

    return _send(
        urllib.request.Request(
            f"{API_BASE_URL}/evses/{quoted}"
            "/configurations/maxExternalChargingCurrent",
            data=json.dumps(payload).encode(),
            headers={
                "authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
    )


def describe(status: int, body: object) -> str:
    """Summarise a response in one line."""
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return (
                    f"HTTP {status} code {first.get('code')}: "
                    f"{str(first.get('message', ''))[:90]}"
                )
        if body.get("_error"):
            return f"network error: {body['_error']}"
    return f"HTTP {status}"


def main() -> int:
    """Walk the ladder and report the accepted range."""
    argv = sys.argv[1:]
    assume_yes = "--yes" in argv

    ladder = LADDER
    if "--values" in argv:
        raw = argv[argv.index("--values") + 1]
        ladder = tuple(int(v) for v in raw.split(",") if v.strip())

    print("Daze charging current range probe")
    print()
    print("WARNING: this WRITES configuration to your wallbox. Each step")
    print("changes the charging current limit and will affect an active")
    print("charge. The original value is restored at the end.")
    print()

    refresh_token = getpass.getpass("Refresh token (input hidden): ").strip()
    if not refresh_token:
        print("A refresh token is required.")
        return 3

    token = refresh_access_token(refresh_token)
    email = get_email(token)
    serial, record = discover(token, email)

    if not serial:
        serial = input("Wallbox serial (discovery failed): ").strip()
    if not serial:
        print("A serial number is required.")
        return 3

    original = record.get("maxExternalChargingCurrentInMilliAmps")
    print(f"\nCharger  : {serial}")
    print(f"Current  : {original} mA")
    for field in (
        "lastMaxInstallationCurrent",
        "sccLimit",
        "supplyGridMaxPower",
        "evseIsThreePhase",
        "dpm",
        "isDynamicLoadManagementOn",
    ):
        print(f"  {field:28s} = {record.get(field)}")

    if not isinstance(original, int):
        print("\nCould not read the current setting, so it cannot be")
        print("restored afterwards. Refusing to change anything.")
        return 1

    print(f"\nWill try {len(ladder)} values: "
          f"{', '.join(str(v) for v in ladder)} mA")
    print(f"Then restore {original} mA.")

    if not assume_yes and (
        input("\nProceed? [yes/no] ").strip().lower() != "yes"
    ):
        return 0

    # Used only to annotate the output with the implied power.
    voltage = 230
    session = record.get("sockets")
    if isinstance(session, list) and session and isinstance(session[0], dict):
        reading = session[0].get("lastACVoltageL1")
        if isinstance(reading, (int, float)) and reading > 100:
            voltage = int(reading)
    print(f"\nUsing {voltage} V to show implied power.")

    accepted: list[int] = []
    rejected: list[tuple[int, str]] = []

    try:
        for value in ladder:
            status, body = set_current(token, serial, value)
            line = describe(status, body)
            verdict = "OK  " if 200 <= status < 300 else "    "
            watts = round(value * voltage / 1000)
            print(f"  {verdict}{value:>6} mA  ({watts:>5} W)  {line}")

            if 200 <= status < 300:
                accepted.append(value)
            else:
                rejected.append((value, line))

            time.sleep(2)
    finally:
        print(f"\nRestoring {original} mA...")
        status, body = set_current(token, serial, original)
        print(f"  {describe(status, body)}")

    print("\n" + "=" * 60)
    if accepted:
        print(f"Accepted: {min(accepted)} to {max(accepted)} mA")
        print(f"  values: {', '.join(str(v) for v in accepted)}")
    else:
        print("Nothing was accepted.")

    if rejected:
        print("\nRejected:")
        for value, line in rejected:
            print(f"  {value:>6} mA  {line}")

    if accepted:
        low, high = min(accepted), max(accepted)
        print(f"\nImplied power range: {round(low * voltage / 1000)} W "
              f"to {round(high * voltage / 1000)} W at {voltage} V.")
        print("If those land near the charger's kW rating, the limits")
        print("are power based and the entity should bound itself the")
        print("same way rather than by current.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted. The original value may not have been restored;")
        print("check the charger and set it back if needed.")
        sys.exit(3)
