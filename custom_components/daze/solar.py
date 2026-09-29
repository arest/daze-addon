"""Decide what solar control should do, with no Home Assistant coupling.

The controller reads sensors and issues commands; this module decides.
Keeping the decision pure means the part that can strand a car or
hammer an API is exhaustively testable without a Home Assistant
instance, which is the split that has worked for payload.py and
optimistic.py.

Ordering in `decide` is load-bearing. The guards come first because a
charger that cannot answer must never be read as an absence of
surplus: that would produce a stop, and it is exactly what happened
when the wallbox lost power at the wall.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# How often the controller re-evaluates. The charger takes seconds to
# apply a change and may need retries, so a faster cadence fights
# itself.
TICK_SECONDS = 120

# Raw grid readings move with every kettle and oven cycle.
SMOOTHING_SECONDS = 300

# Confirm surplus is real before starting; be slower to give up than to
# begin, because interrupting a car is worse than riding out a cloud.
START_DELAY_SECONDS = 300
STOP_DELAY_SECONDS = 600

# Once started, stay started, or surplus hovering at the threshold
# cycles the car.
MIN_RUN_SECONDS = 600

# Do not rewrite the limit for trivial changes.
DEADBAND_W = 300

# A car that has finished stops drawing while surplus is still high.
# Without a back-off the controller restarts it until sunset.
DRAW_GRACE_SECONDS = 300
IGNORED_START_BACKOFF_SECONDS = 3600
MIN_MEANINGFUL_DRAW_W = 200

# A hard ceiling regardless of what the logic decides, so a bug hits a
# wall rather than an API that has already proven fragile.
MAX_COMMANDS_PER_HOUR = 20


class SolarAction(Enum):
    """What the controller should do this cycle."""

    NOTHING = "nothing"
    START = "start"
    STOP = "stop"
    SET = "set"


@dataclass(frozen=True, kw_only=True)
class SolarState:
    """Everything the decision depends on.

    Assembled by the controller from the grid sensors, the coordinator
    and its own timers.
    """

    surplus_w: float
    reserve_w: float
    floor_w: int
    ceiling_w: int
    charging: bool
    current_limit_w: int
    command_pending: bool
    charger_reachable: bool
    eco_mode_on: bool
    schedule_set: bool
    car_connected: bool
    seconds_above_threshold: float
    seconds_below_threshold: float
    seconds_since_start: float
    seconds_since_last_command: float
    commands_this_hour: int
    backoff_remaining_s: float


@dataclass(frozen=True, kw_only=True)
class SolarDecision:
    """What to do, and why.

    The reason is not decoration: it becomes the log line and an
    attribute on the control entity, which is the only way an
    autonomous feature can be understood after the fact.
    """

    action: SolarAction
    target_watts: int | None
    reason: str


def _nothing(reason: str) -> SolarDecision:
    """Return a do-nothing decision with an explanation."""
    return SolarDecision(
        action=SolarAction.NOTHING, target_watts=None, reason=reason
    )


def available_watts(state: SolarState) -> float:
    """Return the surplus left for the car once the house has its share."""
    return state.surplus_w - state.reserve_w


def target_watts(state: SolarState) -> int:
    """Return the limit to request, clamped to what the charger accepts."""
    available = available_watts(state)
    bounded = max(float(state.floor_w), min(float(state.ceiling_w), available))
    return round(bounded)


def decide(state: SolarState) -> SolarDecision:
    """Decide what to do this cycle.

    Args:
        state: Everything the decision depends on.

    Returns:
        The action to take and the reason for it.

    """
    # --- Guards. Nothing below these runs on bad information. ---

    if not state.charger_reachable:
        return _nothing("charger is not reachable")

    if state.command_pending:
        return _nothing("a command is still pending")

    if state.eco_mode_on:
        return _nothing("the charger's own eco mode is controlling it")

    if state.schedule_set:
        return _nothing("the charger has a schedule set")

    if state.commands_this_hour >= MAX_COMMANDS_PER_HOUR:
        return _nothing("rate limit reached for this hour")

    # I3: this guard exists solely to stop the controller re-starting
    # an idle charger the car ignored — not to stop it stopping. Gated
    # on "not charging" so a car that wakes late and starts drawing on
    # its own is still subject to the ordinary stop path below rather
    # than importing from the grid, suppressed, for the rest of the
    # hour-long back-off.
    if not state.charging and state.backoff_remaining_s > 0:
        return _nothing(
            f"backing off for {int(state.backoff_remaining_s)}s after a "
            "start the car ignored"
        )

    available = available_watts(state)
    target = target_watts(state)

    # --- Stopping. Checked before starting so a charging car is
    # --- considered on its own terms.

    if state.charging:
        if available < state.floor_w:
            if state.seconds_since_start < MIN_RUN_SECONDS:
                return _nothing(
                    f"surplus {available:.0f} W is below the "
                    f"{state.floor_w} W floor, but the minimum run time "
                    "has not elapsed"
                )

            if state.seconds_below_threshold >= STOP_DELAY_SECONDS:
                return SolarDecision(
                    action=SolarAction.STOP,
                    target_watts=None,
                    reason=(
                        f"surplus {available:.0f} W below the "
                        f"{state.floor_w} W floor for "
                        f"{int(state.seconds_below_threshold)}s"
                    ),
                )

            return _nothing(
                f"surplus {available:.0f} W is below the floor, waiting "
                f"{STOP_DELAY_SECONDS - int(state.seconds_below_threshold)}s "
                "before stopping"
            )

        if abs(target - state.current_limit_w) >= DEADBAND_W:
            return SolarDecision(
                action=SolarAction.SET,
                target_watts=target,
                reason=(
                    f"following surplus {available:.0f} W: "
                    f"{state.current_limit_w} W to {target} W"
                ),
            )

        return _nothing(
            f"holding at {state.current_limit_w} W, surplus "
            f"{available:.0f} W is within the deadband"
        )

    # --- Starting. ---

    if not state.car_connected:
        return _nothing("no car is connected")

    if available < state.floor_w:
        return _nothing(
            f"surplus {available:.0f} W is below the {state.floor_w} W floor"
        )

    if state.seconds_above_threshold < START_DELAY_SECONDS:
        return _nothing(
            f"surplus {available:.0f} W is sufficient, waiting "
            f"{START_DELAY_SECONDS - int(state.seconds_above_threshold)}s "
            "to confirm"
        )

    return SolarDecision(
        action=SolarAction.START,
        target_watts=target,
        reason=f"surplus {available:.0f} W sustained, starting at {target} W",
    )


def compute_surplus(
    car_draw_w: float, export_w: float, import_w: float
) -> float:
    """Return the power available to the car, in watts.

    The car's own draw is added back because it is not surplus that has
    disappeared: it is surplus already being used. Omitting that term
    makes the controller read its own consumption as a deficit and wind
    itself down to zero.

    Args:
        car_draw_w: What the charger is currently delivering.
        export_w: Grid export, positive.
        import_w: Grid import, positive.

    Returns:
        Available watts, never negative.

    """
    return max(0.0, car_draw_w + export_w - import_w)


class SurplusSmoother:
    """A moving average over a fixed time window.

    Raw grid readings move with every kettle and oven cycle. Acting on
    them would rewrite the charger's limit constantly, against a device
    that takes seconds to apply a change.
    """

    def __init__(self, window_seconds: float = SMOOTHING_SECONDS) -> None:
        """Initialise an empty window.

        Args:
            window_seconds: How much history to average over.

        """
        self.window_seconds = window_seconds
        self._samples: list[tuple[float, float]] = []

    def add(self, value: float, now: float) -> None:
        """Record a reading and drop anything that has aged out.

        Args:
            value: The reading, in watts.
            now: A monotonic timestamp in seconds.

        """
        # A monotonic clock should never go backwards, but if a caller
        # passes wall-clock time instead, drop only the future-dated
        # samples rather than discarding the entire history and losing
        # the smoothing this class exists to provide.
        if self._samples and now < self._samples[-1][0]:
            self._samples = [
                sample for sample in self._samples if sample[0] <= now
            ]

        self._samples.append((now, value))

        cutoff = now - self.window_seconds
        self._samples = [
            sample for sample in self._samples if sample[0] >= cutoff
        ]

    def value(self) -> float | None:
        """Return the average of the window, or None if it is empty."""
        if not self._samples:
            return None

        return sum(value for _, value in self._samples) / len(self._samples)
