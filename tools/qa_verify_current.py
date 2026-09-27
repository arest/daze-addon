#!/usr/bin/env python3
"""Check whether the charger's draw actually follows the current limit.

Setting the maximum charging current returns HTTP 200 for every value
in the accepted range. That only means the request was accepted. This
script measures whether the charger then changes what it draws.

Method: lower the limit well below the present draw, watch the measured
current, then raise it again. Lowering is the reliable direction. A car
draws up to the limit but no more than it wants, so raising the limit
proves nothing if the car is already at its own ceiling, whereas
lowering it must reduce the draw if the limit is being honoured.

WARNING: this WRITES configuration to your wallbox and will change how
fast your car charges while it runs. The original limit is read first
and restored at the end, including on Ctrl-C.

Run it while the car is actually charging, or there is nothing to
measure.

Usage:

    python3 tools/qa_verify_current.py
    python3 tools/qa_verify_current.py --yes --low 6500 --high 16000
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

# How long to watch after a change, and how often to sample. Charge
# current ramps rather than stepping, so this needs to be generous.
WATCH_SECONDS = 90
SAMPLE_SECONDS = 5

# A change counts as honoured if the measured current ends up within
# this tolerance of the requested limit, or below it.
TOLERANCE_MA = 1500


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


def read_live(token: str, serial: str) -> dict:
    """Return the live session figures."""
    quoted = urllib.parse.quote(serial, safe="")
    status, body = _get(
        token,
        f"/sockets/{quoted}/remoteInfo"
        "?includeEcoInfo=true&includeNextSchedule=true",
    )
    if status != 200 or not isinstance(body, dict):
        return {}

    data = body.get("data")
    if not isinstance(data, dict):
        return {}

    session = data.get("chargeSession")
    session = session if isinstance(session, dict) else {}

    return {
        "evseState": data.get("evseState"),
        "isPaused": data.get("isPaused"),
        "power": session.get("instantPowerAsWatt"),
        "current": session.get("lastChargingCurrentInstantL1"),
        "limit": session.get("lastMaxChargingCurrent"),
        "voltage": session.get("lastACVoltageL1"),
    }


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


def watch(token: str, serial: str, target: int) -> dict:
    """Sample the charger while it reacts to a new limit."""
    samples: list[dict] = []
    deadline = time.monotonic() + WATCH_SECONDS

    while time.monotonic() < deadline:
        time.sleep(SAMPLE_SECONDS)
        live = read_live(token, serial)
        samples.append(live)

        elapsed = int(WATCH_SECONDS - (deadline - time.monotonic()))
        print(
            f"      +{elapsed:>3}s  limit={live.get('limit')}"
            f"  current={live.get('current')}"
            f"  power={live.get('power')} W"
        )

        # Stop early once the reported limit matches and the draw has
        # settled at or under it.
        current = live.get("current")
        limit = live.get("limit")
        settled = (
            limit == target
            and isinstance(current, (int, float))
            and current <= target + TOLERANCE_MA
        )
        if settled:
            print("      settled")
            break

    return samples[-1] if samples else {}


def main() -> int:
    """Lower the limit, verify the draw follows, then restore."""
    argv = sys.argv[1:]
    assume_yes = "--yes" in argv

    def flag(name: str, default: int) -> int:
        if name in argv:
            return int(argv[argv.index(name) + 1])
        return default

    low = flag("--low", 6500)
    high = flag("--high", 16000)

    print("Daze charging current QA check")
    print()
    print("WARNING: this WRITES configuration to your wallbox and will")
    print("change how fast your car charges while it runs. The original")
    print("limit is restored at the end.")
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
    baseline = read_live(token, serial)

    print(f"\nCharger : {serial}")
    print(f"Limit   : {original} mA")
    print(f"State   : evseState={baseline.get('evseState')} "
          f"isPaused={baseline.get('isPaused')}")
    print(f"Draw    : {baseline.get('current')} mA, "
          f"{baseline.get('power')} W at {baseline.get('voltage')} V")

    if not isinstance(original, int):
        print("\nCould not read the current limit, so it cannot be")
        print("restored. Refusing to change anything.")
        return 1

    power = baseline.get("power")
    if not isinstance(power, (int, float)) or power <= 0:
        print("\nThe car is not drawing any power right now, so there is")
        print("nothing to measure: a limit change cannot be seen when the")
        print("draw is already zero. Start a charge and run this again.")
        return 1

    print(f"\nPlan: set {low} mA, watch, set {high} mA, watch, "
          f"restore {original} mA.")
    print(f"Watching up to {WATCH_SECONDS}s after each change.")

    if not assume_yes and (
        input("\nProceed? [yes/no] ").strip().lower() != "yes"
    ):
        return 0

    results: dict[int, dict] = {}

    try:
        for target in (low, high):
            print(f"\n  Setting {target} mA")
            status, body = set_limit(token, serial, target)
            print(f"    -> HTTP {status}")

            if not 200 <= status < 300:
                print(f"    rejected: {body}")
                continue

            results[target] = watch(token, serial, target)
    finally:
        print(f"\nRestoring {original} mA...")
        status, _ = set_limit(token, serial, original)
        print(f"  HTTP {status}")

    print("\n" + "=" * 60)

    if not results:
        print("No limit change was accepted, so nothing was measured.")
        return 1

    honoured = True
    for target, final in results.items():
        current = final.get("current")
        limit = final.get("limit")
        print(f"\nRequested {target} mA")
        print(f"  charger reported limit : {limit}")
        print(f"  measured draw          : {current} mA, "
              f"{final.get('power')} W")

        if limit != target:
            print("  PROBLEM: the charger did not adopt the limit.")
            honoured = False
        elif isinstance(current, (int, float)) and current > target + TOLERANCE_MA:
            print("  PROBLEM: the draw exceeds the limit it accepted.")
            honoured = False
        else:
            print("  OK: limit adopted and draw is within it.")

    print()
    if honoured:
        print("The charger honours the limit. If the power still looks")
        print("wrong in Home Assistant, the problem is the integration")
        print("sending or displaying it, not the charger.")
    else:
        print("The charger accepted the request but did not act on it.")
        print("That is a charger or service problem, not an integration")
        print("one: the same calls are being made here directly.")

    print()
    print("Note: raising a limit only increases the draw if the car asks")
    print("for more. A car at its own ceiling will ignore the headroom,")
    print("which is why the lowering step is the meaningful one.")

    return 0 if honoured else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted. The limit may not have been restored; check the")
        print("charger and set it back if needed.")
        sys.exit(3)
