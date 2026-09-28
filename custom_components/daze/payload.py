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
from datetime import datetime, timezone
from typing import Any

# EVSE state values confirmed against live hardware:
#
#   1  idle      observed with no chargeSession at all: the car is
#                not connected or the session has ended
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
EVSE_STATE_IDLE = 1
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
    floor = max(ABSOLUTE_MIN_CHARGING_CURRENT_MA, int(stepped))

    # Never exclude the value the charger is already using. With no
    # session there is no voltage reading, so the nominal 230 V is
    # assumed and the computed floor can land above a setting the
    # charger demonstrably accepted at its real voltage. Offering a
    # range that omits the current value is worse than offering one
    # value that might be refused.
    configured = (data or {}).get("maxExternalChargingCurrentInMilliAmps")
    if isinstance(configured, (int, float)) and configured > 0:
        floor = min(floor, max(int(configured), ABSOLUTE_MIN_CHARGING_CURRENT_MA))

    return floor


# Charging power is the figure users actually think in: a wallbox is
# sold as 1.5 to 7.4 kW, and the charger's own floor is a wattage. The
# API only accepts milliamps, so the conversion lives here.
POWER_STEP_W = 100


def milliamps_to_watts(milliamps: float, data: dict[str, Any] | None) -> int:
    """Convert a charging current to power at the measured voltage."""
    return round(milliamps * supply_voltage(data) / 1000)


def watts_to_milliamps(watts: float, data: dict[str, Any] | None) -> int:
    """Convert a charging power to current, rounded to a usable step.

    The result is clamped to the range the charger accepts, so a power
    figure that rounds just outside it is corrected rather than
    rejected.
    """
    volts = supply_voltage(data)
    raw = watts / volts * 1000
    stepped = int(round(raw / CURRENT_STEP_MA) * CURRENT_STEP_MA)

    return max(
        min_charging_current(data), min(max_charging_current(data), stepped)
    )


def min_charging_power(data: dict[str, Any] | None) -> int:
    """Return the lowest selectable charging power, in watts.

    Rounded up: rounding down would offer a figure that converts back
    to a current under the charger's floor.
    """
    exact = milliamps_to_watts(min_charging_current(data), data)
    return int(-(-exact // POWER_STEP_W) * POWER_STEP_W)


def max_charging_power(data: dict[str, Any] | None) -> int:
    """Return the highest selectable charging power, in watts.

    Rounded down, for the mirror of the reason above.
    """
    exact = milliamps_to_watts(max_charging_current(data), data)
    return int(exact // POWER_STEP_W * POWER_STEP_W)


def grid_power_limit(data: dict[str, Any] | None) -> int | None:
    """Return the grid supply cap in watts, if the charger reports one.

    supplyGridMaxPower is the household supply the charger balances
    against when dynamic power management is on. It does not make the
    API reject a higher setting: a charger reporting 3000 W here
    accepted a 7552 W limit without complaint. What it does mean is
    that the charger will throttle the actual draw, so asking for more
    achieves nothing.

    Treated as advisory for that reason, not as a hard bound.

    Args:
        data: The merged payload, or None.

    Returns:
        The cap in watts, or None if none is reported or it is not in
        force.

    """
    if not data or not data.get("dpm"):
        return None

    value = data.get("supplyGridMaxPower")
    if isinstance(value, (int, float)) and value > 0:
        return int(value)

    return None


def validate_charging_current(
    milliamps: int, data: dict[str, Any] | None
) -> str | None:
    """Check a current against the bounds before it is sent.

    The API answers a value outside its range with HTTP 422 and
    MaxExternalChargingCurrentOutOfRange after a round trip. The bounds
    are already known locally, so the round trip is avoidable and the
    user gets an immediate, specific answer instead.

    Args:
        milliamps: The requested current.
        data: The merged payload, or None.

    Returns:
        None if the value is acceptable, otherwise an explanation.

    """
    floor = min_charging_current(data)
    ceiling = max_charging_current(data)
    volts = supply_voltage(data)

    if milliamps < floor:
        return (
            f"{milliamps} mA is below the {floor} mA minimum this charger "
            f"accepts. It enforces a {MIN_CHARGING_POWER_W} W floor, which "
            f"is {floor} mA at {volts} V."
        )

    if milliamps > ceiling:
        return (
            f"{milliamps} mA is above the {ceiling} mA the installation is "
            f"rated for."
        )

    return None


def grid_cap_advice(milliamps: int, data: dict[str, Any] | None) -> str | None:
    """Warn when a value exceeds the grid supply the charger balances to.

    Not a rejection: the charger accepts the setting and then limits
    what it actually draws.

    Args:
        milliamps: The requested current.
        data: The merged payload, or None.

    Returns:
        A note if the request exceeds the grid cap, otherwise None.

    """
    cap = grid_power_limit(data)
    if cap is None:
        return None

    requested = milliamps_to_watts(milliamps, data)
    if requested <= cap:
        return None

    return (
        f"Requested {requested} W, but this charger balances against a "
        f"{cap} W supply limit, so it will not draw more than that."
    )


# Used when the charger reports no name of its own.
DEFAULT_DEVICE_NAME = "Daze Wallbox"


def device_name(evse_record: dict[str, Any] | None) -> str:
    """Return the name to give the charger in Home Assistant.

    The charger's own name is used unchanged. Appending a vendor
    suffix produced "Daze HomeTT Daze" on a charger that already
    named itself "Daze HomeTT", and every entity inherits the device
    name, so the duplication showed up throughout the interface.

    Args:
        evse_record: The charger record from the evses response.

    Returns:
        A display name, never empty.

    """
    name = (evse_record or {}).get("evseName")

    if isinstance(name, str) and name.strip():
        return name.strip()

    return DEFAULT_DEVICE_NAME


# How long the charger's own attributes may go unrefreshed before it is
# treated as not reporting. It updated every few seconds in every
# capture taken, including while idle, so a gap this long means it is
# not talking to the service.
#
# Inferred rather than measured: no capture exists of a charger that
# was switched off at the wall, because the API kept serving the last
# known record. If this turns out to be wrong the symptom is a command
# refused when it would have worked, which the message names explicitly
# so it can be recognised.
STALE_REPORT_SECONDS = 900


def last_reported_at(data: dict[str, Any] | None) -> datetime | None:
    """Return when the charger last refreshed its own attributes."""
    raw = (data or {}).get("lastAttributesUpdatedOn")

    if not isinstance(raw, str) or not raw:
        return None

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def charger_offline_reason(data: dict[str, Any] | None) -> str | None:
    """Explain why a command cannot reach the charger, if it cannot.

    Cutting power to the wallbox leaves the cloud API serving its last
    known record, so a command is accepted by the service and then
    fails against a device that is not there. That surfaces as HTTP 500
    with error 101 after a long retry, which reads like a service
    outage rather than a charger that is switched off.

    Args:
        data: The merged payload, or None before the first poll.

    Returns:
        None if the charger appears reachable, otherwise a reason.

    """
    if not data:
        return None

    if data.get("active") is False:
        return "the charger reports itself as not active"

    reported = last_reported_at(data)
    if reported is not None:
        age = (datetime.now(timezone.utc) - reported).total_seconds()
        if age > STALE_REPORT_SECONDS:
            return (
                f"the charger last reported {int(age // 60)} minutes ago, "
                "so it appears to be switched off or offline"
            )

    return None
