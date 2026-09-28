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

import math
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
    optimistic: Any | None,
    actual: Any | None,
    expired: bool,
) -> tuple[Any | None, bool]:
    """Decide what a switch should report, and whether to keep guessing.

    A command takes effect at the charger several seconds after it is
    accepted. Reporting the charger's reading during that window shows
    the old state and makes the toggle appear to flip back, so the
    commanded value is reported instead until reality catches up.

    The guess is dropped as soon as the charger agrees, and abandoned
    once it has been held too long, so a command that silently failed
    cannot leave the UI wrong indefinitely.

    Works for any value, not just a boolean: a charging current or an
    operation mode lags the same way a switch does.

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


# The charging current ceiling comes from the installation rating.
#
# sccLimit was tried first and is wrong: on a live charger it read
# 11739, identical to both maxExternalChargingCurrentInMilliAmps and
# lastMaxChargingCurrent, which are the current setting. Capping the
# slider at it would pin the slider to wherever it already sat.
#
# lastMaxInstallationCurrent is the rating of the installation, which
# is what bounds the hardware: a unit rated 1.5 to 7.4 kW single phase
# is 6.5 to 32 A, matching a reported 32000.
CURRENT_LIMIT_FIELDS = ("lastMaxInstallationCurrent",)

# The charger's floor is a power figure, not a current.
#
# Measured on a 1.5 to 7.4 kW single-phase unit at 232 V:
#
#     6000 mA = 1392 W  rejected
#     6400 mA = 1485 W  rejected
#     6521 mA = 1513 W  accepted
#    32000 mA = 7424 W  accepted
#
# The boundary sits at 1500 W, so the minimum current depends on the
# supply voltage and cannot be a constant. Offering the 6 A industry
# minimum made the bottom of the slider always fail with
# MaxExternalChargingCurrentOutOfRange.
MIN_CHARGING_POWER_W = 1500

# No EVSE charges below 6 A regardless of what the arithmetic says.
ABSOLUTE_MIN_CHARGING_CURRENT_MA = 6000

# The entity steps in 0.1 A, so the computed floor is rounded up to a
# step the user can actually select.
CURRENT_STEP_MA = 100

# Used when the charger reports no usable voltage reading.
NOMINAL_VOLTAGE = 230

# Fallback ceiling when the charger reports nothing usable.
FALLBACK_MAX_CHARGING_CURRENT_MA = 32000


def max_charging_current(data: dict[str, Any] | None) -> int:
    """Return the highest charging current the entity should offer.

    Uses the installation rating rather than a hardcoded 32 A, so a
    16 A installation is bounded correctly.

    This is not a promise the charger will accept the value. A grid
    power cap or dynamic power management can reject a current that is
    within the installation rating, which the API reports as
    MaxExternalChargingCurrentOutOfRange. That limit is not exposed as
    a field, so it cannot be applied here in advance.

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

    return max(int(min(candidates)), ABSOLUTE_MIN_CHARGING_CURRENT_MA)


def supply_voltage(data: dict[str, Any] | None) -> int:
    """Return the measured supply voltage, or the nominal value.

    Only L1 is consulted: on a single-phase charger the other two read
    near zero, which would drag an average down to nonsense.

    Args:
        data: The merged payload, or None.

    Returns:
        A voltage in volts.

    """
    if not data:
        return NOMINAL_VOLTAGE

    reading = data.get("lastACVoltageL1")
    if isinstance(reading, (int, float)) and reading > 100:
        return int(reading)

    return NOMINAL_VOLTAGE


def min_charging_current(data: dict[str, Any] | None) -> int:
    """Return the lowest charging current the charger will accept.

    The charger enforces a minimum power, not a minimum current, so
    the answer moves with the supply voltage. The result is rounded up
    to a selectable step, and never falls below the 6 A floor that
    applies to any EVSE.

    Args:
        data: The merged payload, or None before the first poll.

    Returns:
        A current in milliamps.

    """
    volts = supply_voltage(data)
    phases = 3 if (data or {}).get("evseIsThreePhase") else 1

    required_ma = MIN_CHARGING_POWER_W / (volts * phases) * 1000

    # Round up: rounding down would land back under the power floor.
    stepped = math.ceil(required_ma / CURRENT_STEP_MA) * CURRENT_STEP_MA

    return max(ABSOLUTE_MIN_CHARGING_CURRENT_MA, int(stepped))
