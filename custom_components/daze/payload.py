"""Normalise Daze API responses into one flat mapping.

The live data an entity needs is spread across two endpoints and three
nesting levels:

- ``/sockets/{serial}/remoteInfo`` returns the current session under a
  ``chargeSession`` object, plus top-level state flags.
- ``/networks/{uid}/evses`` returns the charger record, with per-socket
  readings under ``sockets[0]`` and configuration at the top level.

Neither endpoint alone covers the sensors and controls. Temperatures,
grid limits, eco mode and the configured current live only in the EVSE
record; live session power, energy and elapsed time live only in
``chargeSession``.

This module flattens both into a single mapping so every value function
can read the field it wants by name, regardless of where the API chose
to put it. It imports nothing from Home Assistant so it can be tested
directly.
"""

from __future__ import annotations

from typing import Any

# EVSE state values confirmed against live hardware:
#
#   3  charging  observed while delivering 2688 W with a session running
#   5  waiting   observed immediately after a start or resume takes
#                effect: isPaused cleared and evseSuspensionReason
#                zero, but still drawing 0 W. The session is live and
#                authorised; the car has not begun drawing yet. The
#                charger passes through this on its way to 3.
#   6  paused    observed with isPaused true, evseSuspensionReason 3,
#                zero instant power, and the session still open
#
# Other values remain unknown, so an unrecognised state reports "idle"
# rather than inventing a meaning.
EVSE_STATE_CHARGING = 3
EVSE_STATE_WAITING_FOR_EV = 5
EVSE_STATE_PAUSED = 6

STATUS_CHARGING = "charging"
STATUS_WAITING_FOR_EV = "waiting_for_ev"
STATUS_IDLE = "idle"
STATUS_PAUSED = "paused"
STATUS_ERROR = "error"
STATUS_OFFLINE = "offline"

PAUSE_FLAGS = (
    "isPaused",
    "isScheduledPaused",
    "isSmartTariffPaused",
)


def _scalars(source: Any) -> dict[str, Any]:
    """Return only the non-container entries of a mapping."""
    if not isinstance(source, dict):
        return {}

    return {
        key: value
        for key, value in source.items()
        if not isinstance(value, (dict, list))
    }


def derive_status(data: dict[str, Any]) -> str | None:
    """Derive a canonical status string from the merged payload.

    The API reports state as an integer plus several independent
    boolean flags, rather than as the status string the sensor catalog
    and the charge switch expect.

    Args:
        data: The merged payload.

    Returns:
        One of the canonical status strings, or None if the payload
        carries no state information at all.

    """
    if data.get("active") is False:
        return STATUS_OFFLINE

    if data.get("evseSystemError"):
        return STATUS_ERROR

    if any(data.get(flag) for flag in PAUSE_FLAGS):
        return STATUS_PAUSED

    state = data.get("evseState")
    if state is None:
        state = data.get("lastStatus")

    if state is None:
        return None

    if state == EVSE_STATE_CHARGING:
        return STATUS_CHARGING

    if state == EVSE_STATE_PAUSED:
        return STATUS_PAUSED

    if state == EVSE_STATE_WAITING_FOR_EV:
        return STATUS_WAITING_FOR_EV

    return STATUS_IDLE


def merge_payload(
    remote_info: dict[str, Any] | None,
    evse_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Flatten the socket and EVSE responses into one mapping.

    Later sources overwrite earlier ones, so the ordering encodes
    precedence: charger configuration first, then the per-socket
    readings, then the socket's own live state, and finally the active
    charge session, which is the freshest view of what is happening
    right now.

    Args:
        remote_info: The ``data`` object from the remoteInfo response.
        evse_record: The charger record from the evses response.

    Returns:
        A flat mapping, plus the original ``chargeSession`` and
        ``nextScheduleInfo`` objects and a derived ``evseStatus``.

    """
    remote_info = remote_info or {}
    evse_record = evse_record or {}

    merged: dict[str, Any] = {}

    # Charger configuration: eco mode, grid limits, photovoltaic flag.
    merged.update(_scalars(evse_record))

    # Per-socket readings: temperatures, voltages, currents, status.
    sockets = evse_record.get("sockets")
    if isinstance(sockets, list) and sockets:
        merged.update(_scalars(sockets[0]))

    # Socket state flags: pause reasons, system error, evseState.
    merged.update(_scalars(remote_info))

    # Live session: power, energy, elapsed time. Freshest, so last.
    session = remote_info.get("chargeSession")
    merged.update(_scalars(session))

    # Preserve the nested objects that value functions still inspect.
    merged["chargeSession"] = session if isinstance(session, dict) else None
    merged["nextScheduleInfo"] = remote_info.get("nextScheduleInfo")

    status = derive_status(merged)
    if status is not None:
        merged["evseStatus"] = status

    return merged


# States in which charging is enabled, whether or not energy is
# currently flowing. The charge switch reads this so that it does not
# snap back to off while the charger waits for the car to draw.
ACTIVE_STATUSES = frozenset({STATUS_CHARGING, STATUS_WAITING_FOR_EV})


def is_charge_enabled(data: dict[str, Any]) -> bool | None:
    """Return whether a charge is authorised and under way.

    True while charging and while waiting for the EV to start drawing,
    because the user's intent has been carried out in both cases.

    Args:
        data: The merged payload.

    Returns:
        True, False, or None when the status is unknown.

    """
    status = data.get("evseStatus")
    if status is None:
        return None
    return str(status).lower() in ACTIVE_STATUSES


def resolve_optimistic(
    optimistic: bool | None,
    actual: bool | None,
    expired: bool,
) -> tuple[bool | None, bool]:
    """Decide what a switch should report, and whether to keep guessing.

    A command takes effect at the charger several seconds after it is
    accepted. Reporting the charger's reading during that window shows
    the old state and makes the toggle appear to flip back, so the
    commanded value is reported instead until reality catches up.

    The guess is dropped as soon as the charger agrees, and abandoned
    once it has been held too long, so a command that silently failed
    cannot leave the UI wrong indefinitely.

    Args:
        optimistic: The value the last command asked for, or None.
        actual: What the charger currently reports, or None.
        expired: Whether the optimistic value has been held too long.

    Returns:
        A tuple of the value to report and whether to keep holding the
        optimistic value.

    """
    if optimistic is None:
        return actual, False

    if expired:
        return actual, False

    if actual == optimistic:
        # Reality caught up; stop guessing.
        return actual, False

    return optimistic, True


# The charging current the charger will accept is not the installation
# rating. A single-phase unit behind a 3000 W grid cap reported a
# 32000 mA installation limit but rejected anything above 11739 mA with
# MaxExternalChargingCurrentOutOfRange.
#
# These fields have all been observed carrying a usable ceiling, in
# decreasing order of specificity.
CURRENT_LIMIT_FIELDS = (
    "sccLimit",
    "lastMaxInstallationCurrent",
)

# Industry minimum for EVSE charging current.
MIN_CHARGING_CURRENT_MA = 6000

# Fallback ceiling when the charger reports nothing usable: 32 A, the
# maximum the hardware line supports.
FALLBACK_MAX_CHARGING_CURRENT_MA = 32000


def max_charging_current(data: dict[str, Any] | None) -> int:
    """Return the highest charging current the charger will accept.

    Advertising the installation rating makes the slider offer values
    the charger rejects, which surfaces as an opaque 422. Prefer the
    limit the charger itself reports.

    Args:
        data: The merged payload, or None before the first poll.

    Returns:
        A ceiling in milliamps, never below the industry minimum.

    """
    if not data:
        return FALLBACK_MAX_CHARGING_CURRENT_MA

    candidates = [
        value
        for field in CURRENT_LIMIT_FIELDS
        if isinstance(value := data.get(field), (int, float)) and value > 0
    ]

    if not candidates:
        return FALLBACK_MAX_CHARGING_CURRENT_MA

    return max(int(min(candidates)), MIN_CHARGING_CURRENT_MA)
