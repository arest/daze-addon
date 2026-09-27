#!/usr/bin/env python3
"""Find the request that resumes a paused Daze charging session.

The integration posts an empty body to
``/sockets/{serial}/commands/playcharge`` and the API answers:

    422 ErrorWrongSessionID: Failed to suspend session: Wrong Session ID

A paused session keeps its ``sessionId``, so the ID exists and is
valid. The likely cause is that the command must name the session it
acts on. This script tries the plausible variants one at a time and
stops at the first that succeeds.

WARNING: unlike the other tools here, this one SENDS COMMANDS to real
hardware. A successful attempt resumes charging on your wallbox. Each
attempt is shown in full and requires typing "yes" before it is sent;
nothing is sent without that confirmation.

Run it while the charger is paused with a car connected.

Usage:

    python3 tools/try_resume.py
"""

from __future__ import annotations

import getpass
import json
import time
import sys
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


def _send(request: urllib.request.Request) -> tuple[int, object]:
    """Send a request, tolerating HTTP error statuses."""
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, _parse(
                response.read().decode(errors="replace")
            )
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


def discover_serial(token: str, email: str) -> str:
    """Find the first wallbox serial, as the config flow does."""
    if not email:
        return ""

    mail = urllib.parse.quote(email, safe="")
    status, body = _get(token, f"/users/{mail}/networks?includeStats=true")
    networks = body.get("data") if isinstance(body, dict) else None
    if status != 200 or not isinstance(networks, list) or not networks:
        return ""

    uid = urllib.parse.quote(str(networks[0].get("uid", "")), safe="")
    status, body = _get(token, f"/networks/{uid}/evses?includeEcoInfo=false")
    evses = body.get("data") if isinstance(body, dict) else None
    if status != 200 or not isinstance(evses, list) or not evses:
        return ""

    return str(evses[0].get("serialNumber", ""))


def read_state(token: str, serial: str) -> dict:
    """Read the current socket state."""
    path = (
        f"/sockets/{urllib.parse.quote(serial, safe='')}/remoteInfo"
        "?includeEcoInfo=true&includeNextSchedule=true"
    )
    status, body = _get(token, path)
    if status != 200 or not isinstance(body, dict):
        return {}

    data = body.get("data")
    return data if isinstance(data, dict) else {}


def start_candidates(serial: str, last_session_id: object) -> list[tuple[str, dict]]:
    """Variants for starting when no session is paused.

    Resuming is solved: playcharge with the serial and the live session
    ID works. Starting from a connected-but-idle charger is a different
    problem, because there is no session to name and playcharge answers
    HTTP 500 code 101.
    """
    quoted = urllib.parse.quote(serial, safe="")

    variants: list[tuple[str, dict]] = [
        # Play with no session at all, only the serial.
        (f"/sockets/{quoted}/commands/playcharge", {"evseSerialNumber": serial}),
        # A dedicated start rather than a resume.
        (f"/sockets/{quoted}/commands/startcharge", {"evseSerialNumber": serial}),
        (f"/sockets/{quoted}/commands/startcharge", {}),
        # Zero as an explicit "no current session" marker.
        (
            f"/sockets/{quoted}/commands/playcharge",
            {"evseSerialNumber": serial, "sessionId": 0},
        ),
        # The command scoped to the EVSE rather than the socket.
        (f"/evses/{quoted}/commands/playcharge", {"evseSerialNumber": serial}),
    ]

    if last_session_id:
        # The previous session ID, in case the charger expects the most
        # recent one even after it closed.
        variants.append(
            (
                f"/sockets/{quoted}/commands/playcharge",
                {"evseSerialNumber": serial, "sessionId": last_session_id},
            )
        )

    return variants


def candidates(
    serial: str, session_id: object, restore_current: int
) -> list[tuple[str, dict]]:
    """Build the resume variants, most likely first.

    The first entry is the confirmed winner: it moved the charger from
    evseState 6 to 5 with isPaused clearing.
    """
    quoted = urllib.parse.quote(serial, safe="")

    return [
        (
            f"/sockets/{quoted}/commands/playcharge",
            {"evseSerialNumber": serial, "sessionId": session_id},
        ),
        # Restoring the current limit. While paused the session reports
        # lastMaxChargingCurrent 0, so the pause may simply be a zero
        # current limit rather than a session state.
        (
            f"/evses/{quoted}/configurations/maxExternalChargingCurrent",
            {
                "evseSerialNumber": serial,
                "maxExternalChargingCurrentInMilliAmps": restore_current,
            },
        ),
        # Play with the serial echoed alongside the session.
        (
            f"/sockets/{quoted}/commands/playcharge",
            {"evseSerialNumber": serial, "sessionId": session_id},
        ),
        (
            f"/sockets/{quoted}/commands/playcharge",
            {"socketSerialNumber": serial, "sessionId": session_id},
        ),
        # A dedicated resume command rather than play.
        (f"/sockets/{quoted}/commands/resumecharge", {"sessionId": session_id}),
        (f"/sockets/{quoted}/commands/resumecharge", {}),
        # The command hung off the EVSE rather than the socket.
        (f"/evses/{quoted}/commands/playcharge", {"sessionId": session_id}),
        # Control: known to answer 200 and change nothing.
        (f"/sockets/{quoted}/commands/playcharge", {"sessionId": session_id}),
    ]


def verify_resumed(
    token: str, serial: str, attempts: int = 6, delay: int = 5
) -> tuple[bool, dict]:
    """Poll the socket state to see whether charging actually resumed.

    HTTP 200 only means the command was accepted. The first version of
    this script treated that as success and reported a false positive,
    so success is now defined as an observed state change.

    Returns:
        A tuple of (resumed, last observed state).

    """
    state: dict = {}

    for index in range(attempts):
        time.sleep(delay)
        state = read_state(token, serial)
        session = state.get("chargeSession")
        power = (
            session.get("instantPowerAsWatt")
            if isinstance(session, dict)
            else None
        )

        print(
            f"    +{(index + 1) * delay:>2}s  evseState={state.get('evseState')}"
            f"  isPaused={state.get('isPaused')}"
            f"  suspension={state.get('evseSuspensionReason')}"
            f"  power={power} W"
        )

        if state.get("isPaused") is False or (
            isinstance(power, (int, float)) and power > 0
        ):
            return True, state

    return False, state


def attempt(token: str, path: str, body: dict) -> tuple[int, object]:
    """POST one candidate command."""
    return _send(
        urllib.request.Request(
            f"{API_BASE_URL}{path}",
            data=json.dumps(body).encode(),
            headers={
                "authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
    )


def summarise(status: int, body: object) -> str:
    """Describe a command response in one line."""
    if isinstance(body, dict):
        if body.get("_empty"):
            return f"HTTP {status}, empty body"
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return (
                    f"HTTP {status}, code {first.get('code')}: "
                    f"{str(first.get('message', ''))[:120]}"
                )
        message = body.get("message")
        if message:
            return f"HTTP {status}: {message}"
    return f"HTTP {status}: {str(body)[:120]}"



def verify_changed(
    token: str,
    serial: str,
    baseline: dict,
    attempts: int = 4,
    delay: int = 3,
) -> tuple[bool, dict]:
    """Poll until the charger state differs from the baseline.

    Direction agnostic: a start and a stop both show up as a change in
    evseState or the pause flag, so the same check works for either.

    Returns:
        A tuple of (changed, last observed state).

    """
    base_state = baseline.get("evseState")
    base_paused = baseline.get("isPaused")
    state: dict = {}

    for index in range(attempts):
        time.sleep(delay)
        state = read_state(token, serial)
        session = state.get("chargeSession")
        power = (
            session.get("instantPowerAsWatt")
            if isinstance(session, dict)
            else None
        )

        print(
            f"      +{(index + 1) * delay:>2}s  "
            f"evseState={state.get('evseState')}  "
            f"isPaused={state.get('isPaused')}  power={power} W"
        )

        if (
            state.get("evseState") != base_state
            or state.get("isPaused") != base_paused
        ):
            return True, state

    return False, state


def retry_mode(token: str, serial: str, state: dict) -> int:
    """Send one command repeatedly until the charger state changes.

    The command shape is already known to be correct. What is not known
    is how many attempts the Daze RPC link needs before it takes. This
    measures exactly that.
    """
    session = state.get("chargeSession")
    session_id = session.get("sessionId") if isinstance(session, dict) else None
    quoted = urllib.parse.quote(serial, safe="")

    print("\nWhich direction do you want to test?")
    print("  1  start / resume  (playcharge)")
    print("  2  stop            (stopcharge)")
    choice = input("Choice [1/2]: ").strip()

    if choice == "2":
        path = f"/sockets/{quoted}/commands/stopcharge"
        label = "stop"
    else:
        path = f"/sockets/{quoted}/commands/playcharge"
        label = "start"

    body: dict = {"evseSerialNumber": serial}
    if session_id is not None:
        body["sessionId"] = session_id

    max_attempts = input("How many attempts at most? [8]: ").strip()
    attempts = int(max_attempts) if max_attempts.isdigit() else 8

    gap = input("Seconds between attempts? [2]: ").strip()
    gap_seconds = int(gap) if gap.isdigit() else 2

    print(f"\nWill send this up to {attempts} time(s), {gap_seconds}s apart:")
    print(f"  POST {API_BASE_URL}{path}")
    print(f"  body {json.dumps(body)}")
    print(f"\nBaseline: evseState={state.get('evseState')} "
          f"isPaused={state.get('isPaused')}")

    if input("\nProceed? [yes/no] ").strip().lower() != "yes":
        return 0

    statuses: list[str] = []

    for number in range(1, attempts + 1):
        print(f"\n  Attempt {number} of {attempts}")
        status, response = attempt(token, path, body)
        print(f"    -> {summarise(status, response)}")
        statuses.append(str(status))

        if 200 <= status < 300:
            print("    accepted; watching for a state change:")
            changed, after = verify_changed(token, serial, state)
            if changed:
                print("\n" + "=" * 60)
                print(f"WORKED on attempt {number} of {attempts}.")
                print(f"  status sequence: {', '.join(statuses)}")
                print(f"  final: evseState={after.get('evseState')} "
                      f"isPaused={after.get('isPaused')}")
                print(f"\nThe {label} command is correct. It needed "
                      f"{number} attempt(s), which is what the retry in "
                      "the integration is sized for.")
                return 0
            print("    accepted but nothing changed; treating as a miss")

        if number < attempts:
            time.sleep(gap_seconds)

    print("\n" + "=" * 60)
    print(f"No attempt produced a state change after {attempts} tries.")
    print(f"  status sequence: {', '.join(statuses)}")
    print("\nIf these were all 500s, the RPC link is down rather than")
    print("flaky. If they were 200s with no change, the command is")
    print("accepted but not applicable from this state.")
    return 1


def main() -> int:
    """Try each resume variant with per-attempt confirmation."""
    print("Daze resume-command finder")
    print()
    print("WARNING: this sends COMMANDS to your wallbox. A successful")
    print("attempt will resume charging. Every attempt is shown first")
    print("and requires typing 'yes'.")
    print()

    refresh_token = getpass.getpass("Refresh token (input hidden): ").strip()
    if not refresh_token:
        print("A refresh token is required.")
        return 3

    token = refresh_access_token(refresh_token)
    email = get_email(token)
    serial = discover_serial(token, email)

    if not serial:
        serial = input("Wallbox serial (discovery failed): ").strip()
    if not serial:
        print("A serial number is required.")
        return 3

    state = read_state(token, serial)
    session = state.get("chargeSession")
    session_id = session.get("sessionId") if isinstance(session, dict) else None

    print(f"\nCharger   : {serial}")
    print(f"evseState : {state.get('evseState')}")
    print(f"isPaused  : {state.get('isPaused')}")
    print(f"suspension: {state.get('evseSuspensionReason')}")
    print(f"sessionId : {session_id}")

    print("\nWhat do you want to do?")
    print("  1  retry one known command until it works (measures flakiness)")
    print("  2  search for a working command variant")
    if input("Choice [1/2]: ").strip() != "2":
        return retry_mode(token, serial, state)

    paused = bool(state.get("isPaused"))

    if paused and session_id is not None:
        print("\nMode: RESUME (charger is paused with an open session).")
        # While paused the session reports a zero current limit, so
        # restoring a sane value is one of the things worth trying.
        restore = 0
        if isinstance(session, dict):
            restore = session.get("lastMaxChargingCurrent") or 0
        if not restore:
            entered = input("Current limit to restore in mA [11739]: ").strip()
            restore = int(entered) if entered.isdigit() else 11739
        variants = candidates(serial, session_id, restore)
    else:
        print("\nMode: START (charger is not paused).")
        print("Resuming is already solved; this searches for the call")
        print("that starts charging from a connected but idle charger.")
        last_id = session_id
        if last_id is None:
            entered = input("Last known session ID, blank to skip: ").strip()
            last_id = int(entered) if entered.isdigit() else None
        variants = start_candidates(serial, last_id)

    print(f"\n{len(variants)} variant(s) to try. Ctrl-C stops at any point.")

    for index, (path, body) in enumerate(variants, start=1):
        print("\n" + "-" * 60)
        print(f"Attempt {index} of {len(variants)}")
        print(f"  POST {API_BASE_URL}{path}")
        print(f"  body {json.dumps(body)}")

        answer = input("  Send this? [yes/skip/quit] ").strip().lower()
        if answer == "quit":
            print("Stopped.")
            return 0
        if answer != "yes":
            print("  skipped")
            continue

        status, response = attempt(token, path, body)
        print(f"  -> {summarise(status, response)}")

        if not 200 <= status < 300:
            print("  rejected, moving on")
            continue

        # Accepted is not resumed. Watch the state before believing it.
        print("  accepted; watching the charger state for 30s:")
        resumed, after = verify_resumed(token, serial)

        if resumed:
            print("\n" + "=" * 60)
            print("CONFIRMED. The charger actually resumed.")
            print("This is the request the integration should make:")
            print(f"  POST {path}")
            print(f"  body {json.dumps(body)}")
            print(f"\nFinal state: evseState={after.get('evseState')} "
                  f"isPaused={after.get('isPaused')}")
            return 0

        print("  no state change: accepted but did not resume. Next variant.")

    print("\n" + "=" * 60)
    print("No variant succeeded.")
    print()
    print("Capture the real request instead: open the web portal in a")
    print("browser, open DevTools on the Network tab, and press resume.")
    print("The request it sends is the one to implement.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(3)
