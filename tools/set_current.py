#!/usr/bin/env python3
"""Set the charging current limit and leave it set.

The other tools here restore whatever they found, because they exist to
measure. This one changes the limit and keeps it, which is what you
want when the aim is simply to charge faster or slower.

Accepts either a power figure or a current. Power is usually what you
actually mean: a wallbox is sold as 1.5 to 7.4 kW, and the charger
enforces a minimum power rather than a minimum current, so working in
watts avoids the arithmetic.

WARNING: this WRITES configuration to your wallbox and changes how fast
your car charges. The change is deliberate and is not undone.

Usage:

    python3 tools/set_current.py --watts 4000
    python3 tools/set_current.py --ma 17200
    python3 tools/set_current.py --watts 4000 --yes
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

TIMEOUT = 30

# The charger steps in 0.1 A and enforces a 1500 W floor.
STEP_MA = 100
MIN_POWER_W = 1500
ABSOLUTE_MIN_MA = 6000
NOMINAL_VOLTAGE = 230


def _send(
    request: urllib.request.Request, attempts: int = 3
) -> tuple[int, object]:
    """Send a request, tolerating HTTP errors and network stalls."""
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
            last = (0, {"_error": str(getattr(err, "reason", err))})
            if attempt < attempts:
                print(f"  network problem, retrying ({attempt}/{attempts})")
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


def measured_voltage(token: str, serial: str, record: dict) -> int:
    """Return the best available supply voltage.

    Prefers the live session, then the socket record, then nominal.
    Only L1 is used: on a single-phase charger L2 and L3 read near
    zero and would drag an average into nonsense.
    """
    quoted = urllib.parse.quote(serial, safe="")
    status, body = _get(
        token, f"/sockets/{quoted}/remoteInfo?includeEcoInfo=true"
    )

    if status == 200 and isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, dict):
            session = data.get("chargeSession")
            if isinstance(session, dict):
                reading = session.get("lastACVoltageL1")
                if isinstance(reading, (int, float)) and reading > 100:
                    return int(reading)

    sockets = record.get("sockets")
    if isinstance(sockets, list) and sockets and isinstance(sockets[0], dict):
        reading = sockets[0].get("lastACVoltageL1")
        if isinstance(reading, (int, float)) and reading > 100:
            return int(reading)

    return NOMINAL_VOLTAGE


def set_limit(token: str, serial: str, milliamps: int) -> tuple[int, object]:
    """Set the maximum charging current."""
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
    """Set the limit and confirm it stuck."""
    argv = sys.argv[1:]
    assume_yes = "--yes" in argv

    def flag(name: str) -> int | None:
        if name in argv:
            return int(argv[argv.index(name) + 1])
        return None

    watts = flag("--watts")
    milliamps = flag("--ma")

    if watts is None and milliamps is None:
        print(__doc__)
        return 3

    print("Daze charging current setter")
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

    volts = measured_voltage(token, serial, record)
    current = record.get("maxExternalChargingCurrentInMilliAmps")
    installation = record.get("lastMaxInstallationCurrent") or 32000

    if milliamps is None:
        assert watts is not None
        exact = watts / volts * 1000
        milliamps = int(round(exact / STEP_MA) * STEP_MA)

    floor = max(
        ABSOLUTE_MIN_MA,
        int(-(-MIN_POWER_W / volts * 1000 // STEP_MA) * STEP_MA),
    )
    ceiling = int(installation)

    print(f"Charger : {serial}")
    print(f"Voltage : {volts} V")
    if isinstance(current, (int, float)):
        print(f"Now     : {int(current)} mA "
              f"({round(int(current) * volts / 1000)} W)")
    print(f"Range   : {floor} to {ceiling} mA "
          f"({round(floor * volts / 1000)} to "
          f"{round(ceiling * volts / 1000)} W)")
    print(f"Target  : {milliamps} mA "
          f"({round(milliamps * volts / 1000)} W)")

    if milliamps < floor:
        print(f"\n{milliamps} mA is below the charger's {MIN_POWER_W} W "
              f"minimum and will be rejected.")
        return 1

    if milliamps > ceiling:
        print(f"\n{milliamps} mA is above the {ceiling} mA installation "
              "rating and will be rejected.")
        return 1

    if not assume_yes and (
        input("\nApply this and leave it set? [yes/no] ").strip().lower()
        != "yes"
    ):
        return 0

    print(f"\nSetting {milliamps} mA...")
    status, body = set_limit(token, serial, milliamps)
    print(f"  {describe(status, body)}")

    if not 200 <= status < 300:
        print("\nThe change was rejected. Nothing was altered.")
        return 1

    time.sleep(5)
    _, after = discover(token, email)
    readback = after.get("maxExternalChargingCurrentInMilliAmps")

    print(f"  read back: {readback} mA")

    if readback == milliamps:
        print(f"\nDone. The limit is now {milliamps} mA, about "
              f"{round(milliamps * volts / 1000)} W at {volts} V.")
        print("Whether the car draws that much is up to the car.")
        return 0

    print(f"\nThe charger accepted the request but reports {readback} mA.")
    print("It may still be applying it; check again shortly.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(3)
