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

# The only EVSE state value confirmed against live hardware: observed
# as 3 while the charger was delivering 2688 W with a session running.
# Other values are inferred conservatively rather than guessed, so an
# unrecognised state reports "idle" rather than inventing a meaning.
EVSE_STATE_CHARGING = 3

STATUS_CHARGING = "charging"
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
