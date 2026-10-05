#!/usr/bin/env python3
"""Read the two things only a real charger can settle.

READ-ONLY. This tool sends no commands and changes nothing on the
charger. It fetches the EVSE record and prints the fields behind the
two open questions in rules.md section 10.

1. What ``evseIsThreePhase`` means, and whether the L2/L3 filter is
   right for this install. ``sensor.py:is_unreportable_phase_sensor``
   withholds the second and third phase readings unless that field is
   exactly ``True``. On the charger this fork was developed against it
   reports ``False`` with L2 at 1 V and L3 at 7 V — an unconnected
   input rather than a measurement. What nobody here can test is a
   three-phase-capable unit wired single-phase: if the field means
   "capable" rather than "currently wired", such an install reports
   True and publishes exactly the junk the filter exists to suppress.

2. The positive direction of the schedule guard. ``nextScheduleInfo``
   has only ever been observed null, on a charger with no schedule set,
   so the refusal has never been seen to fire for the right reason.

Usage:

    python3 tools/probe_phase_and_schedule.py

Tokens are read from a hidden prompt on stdin, following
``check_daze_tokens.py``. They are never passed as command-line
arguments (visible in ``ps`` and in shell history), never read from or
written to disk, and never printed back. The serial number is shown
masked; nothing printed includes a token.

To settle question 2, run it three times: with no schedule set, with a
schedule set in the vendor app, and after clearing it again. The field
should be null, populated, and null.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "daze"

PHASE_FIELDS = (
    "evseIsThreePhase",
    "lastChargingCurrentInstantL1",
    "lastChargingCurrentInstantL2",
    "lastChargingCurrentInstantL3",
    "lastACVoltageL1",
    "lastACVoltageL2",
    "lastACVoltageL3",
)

SCHEDULE_FIELDS = (
    "nextScheduleInfo",
    "nextScheduledCharge",
    "scheduledChargeTime",
    "scheduledStart",
    "scheduleTime",
    "ecoModeEnabled",
)


def _load_api() -> tuple[Any, Any, Any]:
    """Load the API client without executing the integration's __init__.

    ``import custom_components.daze.api`` runs
    ``custom_components/daze/__init__.py`` first, which imports
    voluptuous and Home Assistant — neither installed on a machine
    being used to query a charger. The api package itself needs only
    aiohttp and ``..const``, so it loads cleanly under a synthetic
    package name, the same approach the test suites use.

    Found by running this tool with no tokens: it failed on
    ``ModuleNotFoundError: No module named 'voluptuous'`` before
    reaching the prompt.
    """
    import importlib.util
    import types

    pkg_name = "daze_probe"
    package = types.ModuleType(pkg_name)
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules[pkg_name] = package

    def _load(name: str, path: pathlib.Path, is_pkg: bool = False) -> Any:
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{name}",
            path,
            submodule_search_locations=[str(path.parent)] if is_pkg else None,
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{name}"] = module
        spec.loader.exec_module(module)
        return module

    _load("const", PACKAGE_DIR / "const.py")
    _load("models", PACKAGE_DIR / "models.py")
    payload = _load("payload", PACKAGE_DIR / "payload.py")
    api = _load("api", PACKAGE_DIR / "api" / "__init__.py", is_pkg=True)
    auth = _load("api.auth", PACKAGE_DIR / "api" / "auth.py")
    return api, auth, payload


def _prompt_tokens() -> tuple[str, str]:
    """Read both tokens from a hidden stdin prompt.

    Deliberately not from .env, the environment, or argv. An earlier
    draft of this tool read .env, which would have encouraged writing
    charger credentials to disk in a repository whose §0 incident was a
    device identifier reaching a commit. check_daze_tokens.py set the
    convention for a reason; this follows it.
    """
    access = getpass.getpass("Daze access token (hidden): ").strip()
    refresh = getpass.getpass("Daze refresh token (hidden): ").strip()
    return access.strip('"'), refresh.strip('"')


def _mask(serial: str | None) -> str:
    """Show enough of a serial to tell chargers apart, not to identify one."""
    if not serial:
        return "(none)"
    return f"{serial[:3]}…{serial[-2:]}" if len(serial) > 5 else "…"


def _report(label: str, data: dict[str, Any], fields: tuple[str, ...]) -> None:
    """Print the named fields, distinguishing absent from null."""
    print(f"\n{label}")
    for field in fields:
        if field not in data:
            print(f"  {field:34s} <absent from payload>")
        else:
            print(f"  {field:34s} {json.dumps(data[field])}")


async def main() -> int:
    """Fetch the EVSE record and report the two field groups."""
    import aiohttp

    api_module, auth_module, payload_module = _load_api()
    DazeApiClient = api_module.DazeApiClient
    DazeAuthClient = auth_module.DazeAuthClient
    merge_payload = payload_module.merge_payload

    access, refresh = _prompt_tokens()
    if not access or not refresh:
        print(
            "Both tokens are required. Obtain them from the Daze web "
            "portal (webportal.dazeservice.com)."
        )
        return 2

    async with aiohttp.ClientSession() as session:
        auth = DazeAuthClient(access, refresh)
        api = DazeApiClient(auth, session)

        user = await api.async_get_user_info()
        networks = await api.async_get_networks(user.get("email", ""))
        if not networks:
            print("No networks on this account.")
            return 1

        network_uid = networks[0].get("uid")
        evses = await api.async_get_evses(network_uid)
        if not evses:
            print("No wallbox in this network.")
            return 1

        listing = evses[0]
        serial = listing.get("serialNumber") or listing.get("serial_number")
        print(
            f"charger {_mask(serial)}  "
            f"profile {listing.get('deviceProfile')}"
        )

        # Mirror the coordinator exactly: the live metrics are in the
        # socket's remoteInfo, nested under chargeSession, and the
        # charger configuration is in the EVSE record. Reading either
        # alone reports fields as absent that the integration does see.
        remote_info = await api.async_get_socket_remote_info(serial)
        evse_record = await api.async_get_evse_record(network_uid, serial)
        merged = merge_payload(remote_info, evse_record)

        _report("phase fields (merged payload)", merged, PHASE_FIELDS)
        _report("schedule fields (merged payload)", merged, SCHEDULE_FIELDS)

        missing = [f for f in PHASE_FIELDS + SCHEDULE_FIELDS
                   if f not in merged]
        if missing:
            print(
                "\nStill absent after the merge — check these against the "
                "raw sources below before concluding the charger does not "
                "report them:"
            )
            for field in missing:
                in_remote = field in remote_info
                in_evse = field in evse_record
                print(
                    f"  {field:34s} remoteInfo={in_remote}  "
                    f"evseRecord={in_evse}"
                )

        print(
            "\nQuestion 1. If evseIsThreePhase is False and L2/L3 read a "
            "volt or two with nothing connected, the filter is correct "
            "for this install. If it is True on a unit you know is wired "
            "single-phase, the field means capability and the filter is "
            "mis-keyed."
        )
        print(
            "Question 2. nextScheduleInfo should be null with no schedule "
            "set, populated with one set in the vendor app, and null "
            "again after clearing it. A smart-tariff pause must NOT "
            "populate it."
        )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
