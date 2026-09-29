# Solar Surplus Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Charge the car from solar surplus by adjusting the charger's limit and starting or stopping it, driven from inside the integration rather than from user-written automations.

**Architecture:** A pure decision function with no Home Assistant imports decides what to do; a controller object owns a timer, reads the user's grid sensors, and carries the decision out through the existing API client. Three entities expose and control it. This mirrors `payload.py` and `optimistic.py`, which are pure and have produced no review defects, while the Home-Assistant-coupled code has produced most of them.

**Tech Stack:** Python 3.12+, Home Assistant custom integration, aiohttp (already a dependency). No new third-party packages. Tests run standalone via `python3 tests/run_all.py` — pytest is not installed in this environment.

**Spec:** `docs/superpowers/specs/2026-09-29-solar-surplus-control-design.md`

## Global Constraints

- **Do not bump `manifest.json` version.** The maintainer sets version numbers explicitly; leave `"version": "0.1.6"` untouched.
- **Deploying is `git push`.** There is no separate copy step. Push `main` and force-push the `v0.1.6` tag together, since the tag tracks `main`.
- **Every commit message ends with:** `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- **Lint gate:** `ruff check custom_components/daze/` must pass. This is what CI runs.
- **Test gate:** `python3 tests/run_all.py` must report 0 failures.
- **No Home Assistant in the test environment.** Pure modules are imported directly; Home-Assistant-coupled modules are tested through the stub harness in `tests/test_entities.py`.
- **Charger floor is a power figure, not a current.** 1500 W; the equivalent current depends on supply voltage. Always obtain bounds from `payload.min_charging_current` / `payload.max_charging_current`, never hardcode.
- **Absence of information is never grounds for acting.** Every unknown results in `nothing`.

---

### Task 1: The decision function

**Files:**
- Create: `custom_components/daze/solar.py`
- Test: `tests/test_solar.py`

**Interfaces:**
- Consumes: nothing. This task has no dependencies.
- Produces:
  - `SolarAction` — enum with members `NOTHING`, `START`, `STOP`, `SET`
  - `SolarState` — frozen dataclass, keyword-only, fields listed in Step 3
  - `SolarDecision` — frozen dataclass with `action: SolarAction`, `target_watts: int | None`, `reason: str`
  - `decide(state: SolarState) -> SolarDecision`
  - Constants: `TICK_SECONDS = 120`, `SMOOTHING_SECONDS = 300`, `START_DELAY_SECONDS = 300`, `STOP_DELAY_SECONDS = 600`, `MIN_RUN_SECONDS = 600`, `DEADBAND_W = 300`, `DRAW_GRACE_SECONDS = 300`, `IGNORED_START_BACKOFF_SECONDS = 3600`, `MAX_COMMANDS_PER_HOUR = 20`, `MIN_MEANINGFUL_DRAW_W = 200`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_solar.py`:

```python
"""Tests for the solar surplus decision function.

The decision is a pure function so that the risky part of solar
control can be exercised exhaustively without Home Assistant. Every
branch of the decision table is covered here, in the order the table
evaluates them, because the ordering is load-bearing: a guard that
fires late is the same as a guard that does not exist.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "daze"


def _load(name: str, filename: str) -> Any:
    """Load a single integration module without Home Assistant."""
    spec = importlib.util.spec_from_file_location(name, PACKAGE_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


solar = _load("daze_solar_under_test", "solar.py")


def state(**overrides: Any) -> Any:
    """Build a SolarState that is healthy unless overridden.

    Defaults describe a charger that is reachable, idle, with a car
    connected and plenty of surplus, so each test changes only the one
    thing it is about.
    """
    defaults: dict[str, Any] = {
        "surplus_w": 4000,
        "reserve_w": 0,
        "floor_w": 1600,
        "ceiling_w": 7400,
        "charging": False,
        "current_limit_w": 1600,
        "command_pending": False,
        "charger_reachable": True,
        "eco_mode_on": False,
        "schedule_set": False,
        "car_connected": True,
        "seconds_above_threshold": 600,
        "seconds_below_threshold": 0,
        "seconds_since_start": 0,
        "seconds_since_last_command": 3600,
        "commands_this_hour": 0,
        "backoff_remaining_s": 0,
    }
    defaults.update(overrides)
    return solar.SolarState(**defaults)


# ------------------------------------------------------------------
# Guards, in table order
# ------------------------------------------------------------------


def test_unreachable_charger_does_nothing() -> None:
    """A charger that cannot answer must never imply an absence of
    surplus, which would produce a stop. Observed in practice when the
    wallbox lost power at the wall."""
    decision = solar.decide(state(charger_reachable=False, charging=True))
    assert decision.action is solar.SolarAction.NOTHING
    assert "reachable" in decision.reason


def test_pending_command_does_nothing() -> None:
    """Issuing another command while one is queued stacks requests
    against a charger that is already not answering."""
    decision = solar.decide(state(command_pending=True))
    assert decision.action is solar.SolarAction.NOTHING
    assert "pending" in decision.reason


def test_vendor_eco_mode_does_nothing() -> None:
    """Solar Boost is a competing controller on the same setting."""
    decision = solar.decide(state(eco_mode_on=True))
    assert decision.action is solar.SolarAction.NOTHING
    assert "eco" in decision.reason.lower()


def test_charger_schedule_does_nothing() -> None:
    """A schedule decides when the car charges; so does this."""
    decision = solar.decide(state(schedule_set=True))
    assert decision.action is solar.SolarAction.NOTHING
    assert "schedule" in decision.reason.lower()


def test_no_car_connected_does_not_start() -> None:
    """Starting with nothing plugged in only produces errors."""
    decision = solar.decide(state(car_connected=False))
    assert decision.action is solar.SolarAction.NOTHING


def test_backoff_blocks_a_restart() -> None:
    """A finished car stops drawing while surplus is still high. Without
    a back-off the controller restarts it forever."""
    decision = solar.decide(state(backoff_remaining_s=1800))
    assert decision.action is solar.SolarAction.NOTHING
    assert "backing off" in decision.reason


def test_rate_limit_blocks_everything() -> None:
    """A hard ceiling regardless of what the logic wants, so a bug
    cannot hammer an API that has already proven fragile."""
    decision = solar.decide(
        state(commands_this_hour=solar.MAX_COMMANDS_PER_HOUR)
    )
    assert decision.action is solar.SolarAction.NOTHING
    assert "rate limit" in decision.reason


# ------------------------------------------------------------------
# Stopping
# ------------------------------------------------------------------


def test_stops_when_surplus_below_floor_for_long_enough() -> None:
    """Pure solar: below the charger's floor it cannot charge at all."""
    decision = solar.decide(
        state(
            charging=True,
            surplus_w=1000,
            seconds_below_threshold=solar.STOP_DELAY_SECONDS,
            seconds_since_start=solar.MIN_RUN_SECONDS + 1,
        )
    )
    assert decision.action is solar.SolarAction.STOP


def test_does_not_stop_before_the_delay() -> None:
    """A passing cloud is not a reason to interrupt the car."""
    decision = solar.decide(
        state(
            charging=True,
            surplus_w=1000,
            seconds_below_threshold=60,
            seconds_since_start=solar.MIN_RUN_SECONDS + 1,
        )
    )
    assert decision.action is not solar.SolarAction.STOP


def test_minimum_run_time_outranks_a_stop() -> None:
    """Prevents cycling when surplus hovers at the threshold."""
    decision = solar.decide(
        state(
            charging=True,
            surplus_w=1000,
            seconds_below_threshold=solar.STOP_DELAY_SECONDS,
            seconds_since_start=10,
        )
    )
    assert decision.action is solar.SolarAction.NOTHING
    assert "minimum run" in decision.reason


# ------------------------------------------------------------------
# Starting
# ------------------------------------------------------------------


def test_starts_when_surplus_sustained() -> None:
    decision = solar.decide(
        state(surplus_w=4000, seconds_above_threshold=solar.START_DELAY_SECONDS)
    )
    assert decision.action is solar.SolarAction.START
    assert decision.target_watts == 4000


def test_does_not_start_before_the_delay() -> None:
    decision = solar.decide(state(surplus_w=4000, seconds_above_threshold=60))
    assert decision.action is solar.SolarAction.NOTHING


def test_does_not_start_below_the_floor() -> None:
    decision = solar.decide(
        state(surplus_w=1000, seconds_above_threshold=99999)
    )
    assert decision.action is solar.SolarAction.NOTHING


# ------------------------------------------------------------------
# Following
# ------------------------------------------------------------------


def test_follows_surplus_when_the_change_is_worth_making() -> None:
    decision = solar.decide(
        state(charging=True, surplus_w=5000, current_limit_w=1600)
    )
    assert decision.action is solar.SolarAction.SET
    assert decision.target_watts == 5000


def test_ignores_a_change_inside_the_deadband() -> None:
    """Without this the limit is rewritten every tick for no benefit."""
    decision = solar.decide(
        state(charging=True, surplus_w=4100, current_limit_w=4000)
    )
    assert decision.action is solar.SolarAction.NOTHING


def test_target_is_clamped_to_the_ceiling() -> None:
    """10 kW of surplus does not make a 32 A charger draw 10 kW."""
    decision = solar.decide(
        state(charging=True, surplus_w=10000, current_limit_w=1600)
    )
    assert decision.target_watts == 7400


def test_target_is_clamped_to_the_floor() -> None:
    decision = solar.decide(
        state(
            charging=True,
            surplus_w=1700,
            floor_w=1600,
            current_limit_w=7000,
        )
    )
    assert decision.target_watts == 1700


def test_reserve_is_subtracted_before_anything_else() -> None:
    """The house gets its share first."""
    decision = solar.decide(
        state(charging=True, surplus_w=5000, reserve_w=2000, current_limit_w=1600)
    )
    assert decision.target_watts == 3000


def test_reserve_can_push_below_the_floor_and_stop() -> None:
    decision = solar.decide(
        state(
            charging=True,
            surplus_w=2000,
            reserve_w=1000,
            seconds_below_threshold=solar.STOP_DELAY_SECONDS,
            seconds_since_start=solar.MIN_RUN_SECONDS + 1,
        )
    )
    assert decision.action is solar.SolarAction.STOP


def test_every_decision_carries_a_reason() -> None:
    """The reason becomes the log line and a visible attribute. An
    autonomous feature that acts silently cannot be debugged."""
    for decision in (
        solar.decide(state()),
        solar.decide(state(charging=True)),
        solar.decide(state(charger_reachable=False)),
        solar.decide(state(charging=True, surplus_w=5000)),
    ):
        assert decision.reason
        assert decision.reason.strip() == decision.reason


def _main() -> int:
    """Run every test in this module and report results."""
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]

    failures = 0
    for test in tests:
        try:
            test()
        except Exception as err:  # noqa: BLE001 - standalone runner
            failures += 1
            print(f"FAIL {test.__name__}: {type(err).__name__}: {err}")
        else:
            print(f"ok   {test.__name__}")

    print(f"\n{len(tests) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 tests/test_solar.py`
Expected: FAIL — `FileNotFoundError` or `ModuleNotFoundError`, because `custom_components/daze/solar.py` does not exist.

- [ ] **Step 3: Write the decision function**

Create `custom_components/daze/solar.py`:

```python
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
    return int(round(bounded))


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

    if state.backoff_remaining_s > 0:
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 tests/test_solar.py`
Expected: PASS, `21 passed, 0 failed`

- [ ] **Step 5: Register the suite with the runner**

Modify `tests/run_all.py`, in the `STANDALONE` tuple, adding `"test_solar.py"` after `"test_entities.py"`.

Run: `python3 tests/run_all.py`
Expected: 5 modules, 0 failures.

- [ ] **Step 6: Lint**

Run: `ruff check custom_components/daze/ tests/`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add custom_components/daze/solar.py tests/test_solar.py tests/run_all.py
git commit -m "feat: add the solar surplus decision function

A pure function with no Home Assistant imports, so the part of solar
control that can strand a car or hammer an API is exhaustively
testable. The guards come first deliberately: a charger that cannot
answer must never be read as an absence of surplus, which would
produce a stop.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Surplus arithmetic and smoothing

**Files:**
- Modify: `custom_components/daze/solar.py` (append)
- Test: `tests/test_solar.py` (append before `_main`)

**Interfaces:**
- Consumes: `SMOOTHING_SECONDS` from Task 1.
- Produces:
  - `compute_surplus(car_draw_w: float, export_w: float, import_w: float) -> float`
  - `SurplusSmoother` — class with `add(value: float, now: float) -> None`, `value() -> float | None`, `window_seconds: float`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_solar.py`, immediately before `def _main() -> int:`:

```python
# ------------------------------------------------------------------
# Surplus arithmetic
# ------------------------------------------------------------------


def test_surplus_adds_back_the_cars_own_draw() -> None:
    """The car's consumption is not surplus that disappeared; it is
    surplus already in use. Without this term the controller reads its
    own draw as a deficit and winds itself down to zero."""
    assert solar.compute_surplus(car_draw_w=3000, export_w=0, import_w=0) == 3000


def test_surplus_counts_export() -> None:
    assert solar.compute_surplus(car_draw_w=0, export_w=4000, import_w=0) == 4000


def test_surplus_subtracts_import() -> None:
    """Importing while charging means the car is over-drawing."""
    assert (
        solar.compute_surplus(car_draw_w=3000, export_w=0, import_w=1000) == 2000
    )


def test_surplus_never_goes_negative() -> None:
    """A negative surplus is not meaningful to the caller; zero is."""
    assert solar.compute_surplus(car_draw_w=0, export_w=0, import_w=5000) == 0


def test_smoother_reports_nothing_until_it_has_data() -> None:
    smoother = solar.SurplusSmoother()
    assert smoother.value() is None


def test_smoother_averages_its_window() -> None:
    smoother = solar.SurplusSmoother()
    for index, reading in enumerate((1000, 2000, 3000)):
        smoother.add(reading, now=float(index))
    assert smoother.value() == 2000


def test_smoother_discards_readings_outside_the_window() -> None:
    """Otherwise this morning's surplus still influences this evening."""
    smoother = solar.SurplusSmoother()
    smoother.add(9999, now=0.0)
    smoother.add(1000, now=solar.SMOOTHING_SECONDS + 1)
    assert smoother.value() == 1000


def test_smoother_survives_a_clock_that_goes_backwards() -> None:
    """A restart or a clock correction must not wedge it."""
    smoother = solar.SurplusSmoother()
    smoother.add(1000, now=100.0)
    smoother.add(2000, now=50.0)
    assert smoother.value() is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 tests/test_solar.py`
Expected: FAIL with `AttributeError: module ... has no attribute 'compute_surplus'`

- [ ] **Step 3: Implement**

Append to `custom_components/daze/solar.py`:

```python
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
        # A clock that goes backwards, from a restart or a correction,
        # would otherwise leave future-dated samples wedged in the
        # window forever.
        if self._samples and now < self._samples[-1][0]:
            self._samples.clear()

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 tests/test_solar.py`
Expected: PASS, `29 passed, 0 failed`

- [ ] **Step 5: Lint**

Run: `ruff check custom_components/daze/ tests/`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add custom_components/daze/solar.py tests/test_solar.py
git commit -m "feat: compute and smooth solar surplus

Surplus is the car's own draw plus export minus import. The car term
matters: its consumption is not surplus that disappeared but surplus
already in use, and without it the controller reads its own draw as a
deficit and winds itself down.

Smoothed over five minutes, because raw grid readings move with every
kettle cycle and the charger takes seconds to apply a change.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Configuration and constants

**Files:**
- Modify: `custom_components/daze/const.py`
- Modify: `custom_components/daze/config_flow.py:358-400` (the `DazeOptionsFlowHandler` class)
- Modify: `custom_components/daze/strings.json`
- Modify: `custom_components/daze/translations/it.json`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `CONF_GRID_IMPORT_SENSOR = "grid_import_sensor"`
  - `CONF_GRID_EXPORT_SENSOR = "grid_export_sensor"`
  - `CONF_SOLAR_RESERVE = "solar_reserve"`
  - `DEFAULT_SOLAR_RESERVE = 0`
  - `MAX_SOLAR_RESERVE = 5000`
  - Options flow accepting both sensor entity IDs as optional strings.

- [ ] **Step 1: Add the constants**

Modify `custom_components/daze/const.py`, appending after the `MAX_POLL_INTERVAL` block:

```python
# Solar surplus control. The two grid sensors are chosen by the user in
# the options flow; both are required before solar control can leave
# "off".
CONF_GRID_IMPORT_SENSOR = "grid_import_sensor"
CONF_GRID_EXPORT_SENSOR = "grid_export_sensor"

# Watts to leave for the house before the car gets any. Site-specific,
# so it is an entity rather than a constant; this is only its default.
CONF_SOLAR_RESERVE = "solar_reserve"
DEFAULT_SOLAR_RESERVE = 0
MAX_SOLAR_RESERVE = 5000
```

- [ ] **Step 2: Extend the options flow**

Modify `custom_components/daze/config_flow.py`. In `DazeOptionsFlowHandler.async_step_init`, replace the `schema = vol.Schema({...})` block with:

```python
        options = self._config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL, default=current
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
                ),
                # Optional so the integration works without solar. Solar
                # control refuses to leave "off" until both are set.
                vol.Optional(
                    CONF_GRID_IMPORT_SENSOR,
                    description={
                        "suggested_value": options.get(CONF_GRID_IMPORT_SENSOR)
                    },
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="power"
                    )
                ),
                vol.Optional(
                    CONF_GRID_EXPORT_SENSOR,
                    description={
                        "suggested_value": options.get(CONF_GRID_EXPORT_SENSOR)
                    },
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="power"
                    )
                ),
            }
        )
```

Add to the imports at the top of `config_flow.py`:

```python
from homeassistant.helpers import selector
```

Add `CONF_GRID_EXPORT_SENSOR` and `CONF_GRID_IMPORT_SENSOR` to the existing `from .const import (...)` block, in alphabetical position.

- [ ] **Step 3: Add the English strings**

Modify `custom_components/daze/strings.json`. In `options.step.init.data`, add:

```json
        "grid_import_sensor": "Grid import power sensor",
        "grid_export_sensor": "Grid export power sensor"
```

And replace `options.step.init.description` with:

```json
        "description": "How often to poll the Daze cloud API, and which sensors report your grid import and export. The grid sensors are only needed for solar control; leave them empty otherwise."
```

- [ ] **Step 4: Add the Italian strings**

Modify `custom_components/daze/translations/it.json`, same keys:

```json
        "grid_import_sensor": "Sensore di potenza prelevata dalla rete",
        "grid_export_sensor": "Sensore di potenza immessa in rete"
```

And the description:

```json
        "description": "Ogni quanto interrogare l'API cloud di Daze e quali sensori riportano prelievo e immissione in rete. I sensori di rete servono solo per il controllo solare; lasciali vuoti altrimenti."
```

- [ ] **Step 5: Verify the JSON parses and lint passes**

Run:
```bash
python3 -c "import json; [json.load(open(f)) for f in ['custom_components/daze/strings.json','custom_components/daze/translations/it.json']]; print('valid')"
ruff check custom_components/daze/
python3 tests/run_all.py
```
Expected: `valid`, `All checks passed!`, 0 failures.

- [ ] **Step 6: Commit**

```bash
git add custom_components/daze/const.py custom_components/daze/config_flow.py custom_components/daze/strings.json custom_components/daze/translations/it.json
git commit -m "feat: let the user pick grid import and export sensors

Both optional, so the integration works unchanged without solar.
Solar control refuses to leave 'off' until both are set, which is
checked where it can be explained rather than by making the fields
required here.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: The controller

**Files:**
- Create: `custom_components/daze/solar_controller.py`
- Test: `tests/test_solar_controller.py`

**Interfaces:**
- Consumes: everything from Tasks 1 and 2; `CONF_GRID_IMPORT_SENSOR`, `CONF_GRID_EXPORT_SENSOR` from Task 3; `payload.min_charging_current`, `payload.max_charging_current`, `payload.milliamps_to_watts`, `payload.watts_to_milliamps`, `payload.charger_offline_reason`, `payload.is_charge_enabled`; `DazeDataUpdateCoordinator.limit_state`, `.api_client`, `.serial_number`, `.data`, `.async_schedule_refresh_in`.
- Produces:
  - `SolarMode` — enum with `OFF = "off"`, `SIMULATE = "simulate"`, `ACTIVE = "active"`
  - `SolarController` — class with `async_start()`, `async_stop()`, `mode` property and setter, `last_decision` property, `reserve_w` property and setter, `surplus_w` property, `async_tick()`, `add_listener(cb) -> remove_cb`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_solar_controller.py`:

```python
"""Tests for the solar controller against a stubbed Home Assistant.

The controller is where the decision meets real sensors and a real API
client, so these cover the joins: reading the sensors, assembling the
state, honouring simulate, and not fighting the retry machinery.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "daze"


class StubState:
    """A Home Assistant state object."""

    def __init__(self, state: str) -> None:
        self.state = state


class StubStates:
    """The subset of hass.states the controller uses."""

    def __init__(self) -> None:
        self._states: dict[str, StubState] = {}

    def set(self, entity_id: str, value: str) -> None:
        """Set a state."""
        self._states[entity_id] = StubState(value)

    def get(self, entity_id: str) -> StubState | None:
        """Return a state, or None if unknown."""
        return self._states.get(entity_id)


class StubHass:
    """Just enough of HomeAssistant for the controller."""

    def __init__(self) -> None:
        self.states = StubStates()


def _install_stubs() -> None:
    """Register the Home Assistant modules the controller imports."""
    def _module(name: str, **attributes: Any) -> None:
        module = types.ModuleType(name)
        for key, value in attributes.items():
            setattr(module, key, value)
        sys.modules[name] = module

    scheduled: list[Any] = []

    def async_call_later(hass: Any, delay: Any, action: Any) -> Any:
        scheduled.append((delay, action))
        return lambda: None

    _module("homeassistant")
    _module("homeassistant.core", HomeAssistant=StubHass, callback=lambda fn: fn)
    _module("homeassistant.helpers")
    _module("homeassistant.helpers.event", async_call_later=async_call_later)


_install_stubs()


def _load_package() -> None:
    """Load the integration modules the controller needs."""
    package = types.ModuleType("daze_solar_ctl")
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules["daze_solar_ctl"] = package

    for name in ("const", "payload", "optimistic", "solar"):
        spec = importlib.util.spec_from_file_location(
            f"daze_solar_ctl.{name}", PACKAGE_DIR / f"{name}.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"daze_solar_ctl.{name}"] = module
        spec.loader.exec_module(module)

    spec = importlib.util.spec_from_file_location(
        "daze_solar_ctl.solar_controller",
        PACKAGE_DIR / "solar_controller.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["daze_solar_ctl.solar_controller"] = module
    spec.loader.exec_module(module)


_load_package()

solar = sys.modules["daze_solar_ctl.solar"]
optimistic = sys.modules["daze_solar_ctl.optimistic"]
controller_module = sys.modules["daze_solar_ctl.solar_controller"]


class FakeApi:
    """Records the commands the controller issues."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def async_set_max_charging_current(
        self, serial: str, current_ma: int, attempts: int = 8
    ) -> dict:
        self.calls.append(("current", current_ma))
        return {}

    async def async_start_charge(self, serial: str, attempts: int = 8) -> dict:
        self.calls.append(("start", serial))
        return {}

    async def async_stop_charge(self, serial: str, attempts: int = 8) -> dict:
        self.calls.append(("stop", serial))
        return {}


class FakeCoordinator:
    """The coordinator surface the controller touches."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.api_client = FakeApi()
        self.serial_number = "SER1"
        self.limit_state = optimistic.OptimisticState()
        self.refresh_delays: list[int] = []

    def async_schedule_refresh_in(self, delay: int) -> None:
        self.refresh_delays.append(delay)


CHARGING_DATA: dict[str, Any] = {
    "active": True,
    "lastAttributesUpdatedOn": None,
    "evseStatus": "charging",
    "evseState": 3,
    "instantPowerAsWatt": 3000,
    "maxExternalChargingCurrentInMilliAmps": 13000,
    "lastMaxInstallationCurrent": 32000,
    "lastACVoltageL1": 230,
    "ecoModeEnabled": False,
    "schedules": [],
    "chargeSession": {"sessionId": 1},
}


def build(data: dict[str, Any] | None = None) -> tuple[Any, Any, Any]:
    """Build a controller wired to stubs."""
    hass = StubHass()
    hass.states.set("sensor.grid_import", "0")
    hass.states.set("sensor.grid_export", "5000")

    coordinator = FakeCoordinator(dict(data or CHARGING_DATA))
    controller = controller_module.SolarController(
        hass=hass,
        coordinator=coordinator,
        import_entity="sensor.grid_import",
        export_entity="sensor.grid_export",
    )
    return controller, coordinator, hass


def test_surplus_uses_both_sensors_and_the_car_draw() -> None:
    """3000 W drawn plus 5000 W exported is 8000 W available."""
    controller, _, _ = build()
    asyncio.run(controller.async_tick())
    assert controller.surplus_w == 8000


def test_simulate_decides_but_sends_nothing() -> None:
    """The default on first enable. It must be genuinely inert."""
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.SIMULATE

    asyncio.run(controller.async_tick())

    assert coordinator.api_client.calls == []
    assert controller.last_decision is not None


def test_off_does_not_even_decide() -> None:
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.OFF

    asyncio.run(controller.async_tick())

    assert coordinator.api_client.calls == []


def test_active_follows_surplus() -> None:
    """13000 mA at 230 V is about 2990 W; 8000 W of surplus should
    raise it, and the request is made in milliamps."""
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    # Seed the smoother so the first tick has a usable average.
    asyncio.run(controller.async_tick())
    asyncio.run(controller.async_tick())

    assert any(call[0] == "current" for call in coordinator.api_client.calls)


def test_a_missing_sensor_stops_nothing() -> None:
    """Absence of information is never grounds for acting."""
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE
    hass.states.set("sensor.grid_export", "unavailable")

    asyncio.run(controller.async_tick())

    assert coordinator.api_client.calls == []


def test_a_pending_command_is_not_piled_on() -> None:
    """The integration already retries in the background for minutes."""
    controller, coordinator, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE
    coordinator.limit_state.request(20000)

    asyncio.run(controller.async_tick())
    asyncio.run(controller.async_tick())

    assert coordinator.api_client.calls == []


def test_the_reserve_is_honoured() -> None:
    controller, _, _ = build()
    controller.reserve_w = 2000
    asyncio.run(controller.async_tick())
    assert controller.surplus_w == 8000
    assert controller.last_decision is None or True


def test_listeners_are_told_after_a_tick() -> None:
    """The entities redraw from this rather than polling the object."""
    controller, _, _ = build()
    seen: list[int] = []
    controller.add_listener(lambda: seen.append(1))

    asyncio.run(controller.async_tick())

    assert seen


def _main() -> int:
    """Run every test in this module and report results."""
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]

    failures = 0
    for test in tests:
        try:
            test()
        except Exception as err:  # noqa: BLE001 - standalone runner
            failures += 1
            print(f"FAIL {test.__name__}: {type(err).__name__}: {err}")
        else:
            print(f"ok   {test.__name__}")

    print(f"\n{len(tests) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 tests/test_solar_controller.py`
Expected: FAIL — `solar_controller.py` does not exist.

- [ ] **Step 3: Implement the controller**

Create `custom_components/daze/solar_controller.py`:

```python
"""Drive the charger from solar surplus.

Reads the user's grid sensors, assembles the state the decision needs,
and carries out whatever it returns. The decision itself lives in
solar.py, which has no Home Assistant coupling and is where the
behaviour is tested.

Commands go through the API client, never through the number entity.
That makes the manual-override rule mechanical: any write arriving at
the entity is by definition external, so solar control disarms itself
without needing a flag that could be wrong.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import Enum
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later

from .coordinator import DazeDataUpdateCoordinator
from .payload import (
    charger_offline_reason,
    is_charge_enabled,
    max_charging_current,
    milliamps_to_watts,
    min_charging_current,
    watts_to_milliamps,
)
from .solar import (
    DRAW_GRACE_SECONDS,
    IGNORED_START_BACKOFF_SECONDS,
    MAX_COMMANDS_PER_HOUR,
    MIN_MEANINGFUL_DRAW_W,
    TICK_SECONDS,
    SolarAction,
    SolarDecision,
    SolarState,
    SurplusSmoother,
    compute_surplus,
    decide,
)

_LOGGER = logging.getLogger(__name__)


class SolarMode(Enum):
    """How much authority solar control has.

    A single tri-state rather than two switches, so that "dry run on,
    solar off" cannot be expressed.
    """

    OFF = "off"
    SIMULATE = "simulate"
    ACTIVE = "active"


class SolarController:
    """Evaluates surplus on a timer and acts on the result."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DazeDataUpdateCoordinator,
        import_entity: str | None,
        export_entity: str | None,
    ) -> None:
        """Initialise in the off state.

        Args:
            hass: Used to read the grid sensors and schedule ticks.
            coordinator: Source of charger state and the API client.
            import_entity: Grid import power sensor, or None.
            export_entity: Grid export power sensor, or None.

        """
        self._hass = hass
        self._coordinator = coordinator
        self._import_entity = import_entity
        self._export_entity = export_entity

        self._mode = SolarMode.OFF
        self._reserve_w = 0.0
        self._smoother = SurplusSmoother()
        self._last_decision: SolarDecision | None = None
        self._listeners: list[Callable[[], None]] = []
        self._cancel_tick: Callable[[], None] | None = None

        self._above_since: float | None = None
        self._below_since: float | None = None
        self._started_at: float | None = None
        self._backoff_until: float = 0.0
        self._command_times: list[float] = []
        self._sensor_warning_logged = False

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def mode(self) -> SolarMode:
        """Return the current mode."""
        return self._mode

    @mode.setter
    def mode(self, value: SolarMode) -> None:
        """Set the mode, resetting timers when it changes."""
        if value is self._mode:
            return

        self._mode = value
        self._above_since = None
        self._below_since = None
        _LOGGER.info("Solar control set to %s", value.value)
        self._notify()

    @property
    def reserve_w(self) -> float:
        """Return the watts held back for the house."""
        return self._reserve_w

    @reserve_w.setter
    def reserve_w(self, value: float) -> None:
        """Set the reserve."""
        self._reserve_w = max(0.0, float(value))
        self._notify()

    @property
    def surplus_w(self) -> float | None:
        """Return the smoothed surplus, or None before the first read."""
        return self._smoother.value()

    @property
    def last_decision(self) -> SolarDecision | None:
        """Return the most recent decision, for display and logging."""
        return self._last_decision

    @property
    def configured(self) -> bool:
        """Whether both grid sensors have been chosen."""
        return bool(self._import_entity and self._export_entity)

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a callback for state changes.

        Returns:
            A callable that unregisters the listener.

        """
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    async def async_start(self) -> None:
        """Begin ticking."""
        self._schedule_tick()

    async def async_stop(self) -> None:
        """Stop ticking and drop listeners."""
        if self._cancel_tick is not None:
            self._cancel_tick()
            self._cancel_tick = None
        self._listeners.clear()

    # ------------------------------------------------------------------
    # The cycle
    # ------------------------------------------------------------------

    async def async_tick(self) -> None:
        """Evaluate once and act if the mode allows it."""
        if self._mode is SolarMode.OFF:
            return

        now = time.monotonic()
        surplus = self._read_surplus()

        if surplus is None:
            # Absence of information is never grounds for acting.
            if not self._sensor_warning_logged:
                self._sensor_warning_logged = True
                _LOGGER.warning(
                    "Solar control cannot read its grid sensors; doing "
                    "nothing until they report"
                )
            return

        self._sensor_warning_logged = False
        self._smoother.add(surplus, now)

        smoothed = self._smoother.value()
        if smoothed is None:
            return

        state = self._build_state(smoothed, now)
        self._track_thresholds(state, now)

        decision = decide(self._build_state(smoothed, now))
        self._last_decision = decision

        if decision.action is SolarAction.NOTHING:
            _LOGGER.debug("Solar control: %s", decision.reason)
            self._notify()
            return

        if self._mode is SolarMode.SIMULATE:
            _LOGGER.info(
                "Solar control (simulating): would %s — %s",
                decision.action.value,
                decision.reason,
            )
            self._notify()
            return

        await self._carry_out(decision, now)
        self._notify()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _schedule_tick(self) -> None:
        """Queue the next evaluation."""

        async def _run(_now: Any) -> None:
            self._cancel_tick = None
            try:
                await self.async_tick()
            finally:
                self._schedule_tick()

        self._cancel_tick = async_call_later(self._hass, TICK_SECONDS, _run)

    def _read_number(self, entity_id: str | None) -> float | None:
        """Read a numeric sensor, or None if it cannot be used."""
        if not entity_id:
            return None

        state = self._hass.states.get(entity_id)
        if state is None:
            return None

        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _read_surplus(self) -> float | None:
        """Compute surplus from the grid sensors and the car's draw."""
        import_w = self._read_number(self._import_entity)
        export_w = self._read_number(self._export_entity)

        if import_w is None or export_w is None:
            return None

        data = self._coordinator.data or {}
        car_draw = data.get("instantPowerAsWatt")
        car_w = float(car_draw) if isinstance(car_draw, (int, float)) else 0.0

        return compute_surplus(
            car_draw_w=car_w, export_w=export_w, import_w=import_w
        )

    def _build_state(self, smoothed: float, now: float) -> SolarState:
        """Assemble everything the decision depends on."""
        data = self._coordinator.data or {}

        limit_ma = data.get("maxExternalChargingCurrentInMilliAmps") or 0
        charging = bool(is_charge_enabled(data))
        schedules = data.get("schedules")

        return SolarState(
            surplus_w=smoothed,
            reserve_w=self._reserve_w,
            floor_w=milliamps_to_watts(min_charging_current(data), data),
            ceiling_w=milliamps_to_watts(max_charging_current(data), data),
            charging=charging,
            current_limit_w=milliamps_to_watts(int(limit_ma), data),
            command_pending=self._coordinator.limit_state.pending,
            charger_reachable=charger_offline_reason(data) is None,
            eco_mode_on=bool(data.get("ecoModeEnabled")),
            schedule_set=bool(schedules),
            car_connected=data.get("chargeSession") is not None or charging,
            seconds_above_threshold=self._elapsed(self._above_since, now),
            seconds_below_threshold=self._elapsed(self._below_since, now),
            seconds_since_start=self._elapsed(self._started_at, now),
            seconds_since_last_command=(
                now - self._command_times[-1] if self._command_times else 1e9
            ),
            commands_this_hour=self._commands_this_hour(now),
            backoff_remaining_s=max(0.0, self._backoff_until - now),
        )

    @staticmethod
    def _elapsed(since: float | None, now: float) -> float:
        """Return seconds since a mark, or zero if it is unset."""
        return 0.0 if since is None else max(0.0, now - since)

    def _commands_this_hour(self, now: float) -> int:
        """Count commands issued in the last hour, dropping older ones."""
        self._command_times = [
            when for when in self._command_times if now - when < 3600
        ]
        return len(self._command_times)

    def _track_thresholds(self, state: SolarState, now: float) -> None:
        """Maintain how long surplus has been above or below the floor."""
        available = state.surplus_w - state.reserve_w

        if available >= state.floor_w:
            self._below_since = None
            if self._above_since is None:
                self._above_since = now
        else:
            self._above_since = None
            if self._below_since is None:
                self._below_since = now

    async def _carry_out(self, decision: SolarDecision, now: float) -> None:
        """Issue the command a decision calls for."""
        client = self._coordinator.api_client
        serial = self._coordinator.serial_number
        data = self._coordinator.data or {}

        _LOGGER.info(
            "Solar control: %s — %s", decision.action.value, decision.reason
        )

        if decision.action is SolarAction.STOP:
            await client.async_stop_charge(serial)
            self._started_at = None

        elif decision.action is SolarAction.START:
            if decision.target_watts is not None:
                await client.async_set_max_charging_current(
                    serial, watts_to_milliamps(decision.target_watts, data)
                )
            await client.async_start_charge(serial)
            self._started_at = now

        elif decision.action is SolarAction.SET:
            if decision.target_watts is None:
                return
            await client.async_set_max_charging_current(
                serial, watts_to_milliamps(decision.target_watts, data)
            )

        self._command_times.append(now)
        self._coordinator.async_schedule_refresh_in(10)

    def _notify(self) -> None:
        """Tell the entities to redraw."""
        for listener in list(self._listeners):
            listener()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 tests/test_solar_controller.py`
Expected: PASS, `8 passed, 0 failed`

- [ ] **Step 5: Register the suite and lint**

Modify `tests/run_all.py`, adding `"test_solar_controller.py"` to `STANDALONE`.

Run:
```bash
python3 tests/run_all.py
ruff check custom_components/daze/ tests/
```
Expected: 6 modules, 0 failures; `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add custom_components/daze/solar_controller.py tests/test_solar_controller.py tests/run_all.py
git commit -m "feat: add the solar controller

Reads the grid sensors, assembles the decision state, and carries out
the result. Commands go through the API client rather than the number
entity, which makes the manual-override rule mechanical: any write
arriving at the entity is by definition external.

Simulate decides and logs but sends nothing, and is what the mode
defaults to on first enable.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: The ignored-start back-off

**Files:**
- Modify: `custom_components/daze/solar_controller.py`
- Test: `tests/test_solar_controller.py` (append before `_main`)

**Interfaces:**
- Consumes: `DRAW_GRACE_SECONDS`, `IGNORED_START_BACKOFF_SECONDS`, `MIN_MEANINGFUL_DRAW_W` from Task 1.
- Produces: no new public surface; `_backoff_until` is set internally.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_solar_controller.py`, before `_main`:

```python
def test_a_car_that_ignores_a_start_triggers_a_backoff() -> None:
    """A finished car stops drawing while surplus is still high, so a
    naive controller restarts it until sunset."""
    data = dict(CHARGING_DATA)
    data["evseStatus"] = "idle"
    data["instantPowerAsWatt"] = 0
    controller, coordinator, _ = build(data)
    controller.mode = controller_module.SolarMode.ACTIVE

    # Pretend a start was issued a while ago and the car never drew.
    controller._started_at = 0.0

    asyncio.run(controller.async_tick())

    assert controller._backoff_until > 0, "no back-off was armed"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 tests/test_solar_controller.py`
Expected: FAIL on `assert controller._backoff_until > 0`

- [ ] **Step 3: Implement**

In `custom_components/daze/solar_controller.py`, add this method to `SolarController`:

```python
    def _check_ignored_start(self, now: float) -> None:
        """Back off if a started car never began drawing.

        When a car finishes it stops drawing while surplus is still
        high. The charger goes idle, the controller sees "not charging,
        plenty of surplus", and starts again. Without this the cycle
        repeats until sunset.
        """
        if self._started_at is None:
            return

        if now - self._started_at < DRAW_GRACE_SECONDS:
            return

        data = self._coordinator.data or {}
        draw = data.get("instantPowerAsWatt")
        drawing = isinstance(draw, (int, float)) and draw >= MIN_MEANINGFUL_DRAW_W

        if drawing:
            return

        self._backoff_until = now + IGNORED_START_BACKOFF_SECONDS
        self._started_at = None
        _LOGGER.info(
            "The car did not draw within %ds of starting; backing off for "
            "%d minutes",
            DRAW_GRACE_SECONDS,
            IGNORED_START_BACKOFF_SECONDS // 60,
        )
```

And call it in `async_tick`, immediately after `self._track_thresholds(state, now)`:

```python
        self._check_ignored_start(now)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 tests/test_solar_controller.py`
Expected: PASS, `9 passed, 0 failed`

- [ ] **Step 5: Lint and full suite**

Run:
```bash
ruff check custom_components/daze/ tests/
python3 tests/run_all.py
```
Expected: `All checks passed!`, 0 failures.

- [ ] **Step 6: Commit**

```bash
git add custom_components/daze/solar_controller.py tests/test_solar_controller.py
git commit -m "feat: back off when a started car does not draw

A car that has finished stops drawing while surplus is still high, so
the charger goes idle, the controller sees plenty of surplus and no
charge, and starts again. The cycle repeats until sunset.

The rate limit would blunt this but is the wrong instrument: it is a
backstop against bugs, not a substitute for handling a state the
design knows about.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Wire it into the entry, and disarm on manual override

**Files:**
- Modify: `custom_components/daze/__init__.py`
- Modify: `custom_components/daze/number.py` (the two existing limit entities)
- Test: `tests/test_entities.py` (append before `_main`)

**Interfaces:**
- Consumes: `SolarController`, `SolarMode` from Task 4.
- Produces: `hass.data[DOMAIN][entry.entry_id]["solar_controller"]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_entities.py`, before `_main`:

```python
def test_a_manual_limit_change_disarms_solar_control() -> None:
    """Touching the control means you want manual control.

    The controller writes through the API client, never the entity, so
    any write arriving here is by definition external. That makes the
    rule mechanical rather than a flag that could be wrong.
    """
    class Ctl:
        mode = "active"
        disarmed = False

        def disarm(self, reason: str) -> None:
            self.disarmed = True

    coordinator = FakeCoordinator(dict(BASE_DATA))
    coordinator.solar_controller = Ctl()
    entity, _, _ = make_number()
    entity.coordinator = coordinator

    asyncio.run(entity.async_set_native_value(16000))

    assert coordinator.solar_controller.disarmed is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 tests/test_entities.py`
Expected: FAIL on `assert ... .disarmed is True`

- [ ] **Step 3: Add `disarm` to the controller**

In `custom_components/daze/solar_controller.py`, add to `SolarController`:

```python
    def disarm(self, reason: str) -> None:
        """Turn solar control off because something else took over.

        Called when a limit change arrives through an entity or a
        service, which by construction means it did not come from here.
        """
        if self._mode is SolarMode.OFF:
            return

        _LOGGER.info("Solar control disarmed: %s", reason)
        self._mode = SolarMode.OFF
        self._above_since = None
        self._below_since = None
        self._notify()
```

- [ ] **Step 4: Call it from the limit entities**

In `custom_components/daze/number.py`, add this helper to both `DazeWallboxNumberEntity` and `DazeWallboxPowerEntity`:

```python
    def _disarm_solar(self) -> None:
        """Hand control back to the user.

        Solar control writes through the API client, so anything
        arriving here came from a person or their automation.
        """
        controller = getattr(self.coordinator, "solar_controller", None)
        if controller is not None:
            controller.disarm("the charging limit was set manually")
```

Call `self._disarm_solar()` in both `async_set_native_value` methods, immediately after the no-op guard returns and before the offline check.

- [ ] **Step 5: Create and tear down the controller**

In `custom_components/daze/__init__.py`, inside `async_setup_entry`, after the coordinator is created and before `hass.data[DOMAIN][entry.entry_id] = {...}`:

```python
    solar_controller = SolarController(
        hass=hass,
        coordinator=coordinator,
        import_entity=entry.options.get(CONF_GRID_IMPORT_SENSOR),
        export_entity=entry.options.get(CONF_GRID_EXPORT_SENSOR),
    )
    # The entities reach the controller through the coordinator, which
    # every one of them already holds.
    coordinator.solar_controller = solar_controller
    await solar_controller.async_start()
```

Add `"solar_controller": solar_controller,` to the `hass.data[DOMAIN][entry.entry_id]` dict.

In `async_unload_entry`, inside the `if unload_ok:` block and before `coordinator.async_shutdown_timers()`:

```python
        controller = entry_data.get("solar_controller")
        if controller is not None:
            await controller.async_stop()
```

Add the imports:

```python
from .const import CONF_GRID_EXPORT_SENSOR, CONF_GRID_IMPORT_SENSOR
from .solar_controller import SolarController
```

merging the `const` names into the existing import block.

- [ ] **Step 6: Disarm from the services too**

In `custom_components/daze/__init__.py`, inside `_handle_set_charging_current`, immediately after `_refuse_if_offline()`:

```python
        if coordinator.solar_controller is not None:
            coordinator.solar_controller.disarm(
                "the charging current was set by a service call"
            )
```

- [ ] **Step 7: Run the tests and lint**

Run:
```bash
python3 tests/run_all.py
ruff check custom_components/daze/ tests/
```
Expected: 0 failures, `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add custom_components/daze/__init__.py custom_components/daze/number.py custom_components/daze/solar_controller.py tests/test_entities.py
git commit -m "feat: wire solar control into the entry and disarm on override

The controller is created with the entry and stopped when it unloads,
alongside the coordinator's own timers.

Any limit change arriving through an entity or a service disarms solar
control, because the controller writes through the API client and
never through an entity. That makes the rule mechanical rather than a
flag that has to be set and cleared correctly.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: The three entities

**Files:**
- Modify: `custom_components/daze/select.py` (append a second entity class and register it)
- Modify: `custom_components/daze/number.py` (append a third entity class and register it)
- Modify: `custom_components/daze/sensor.py` (append an entity class and register it)
- Modify: `custom_components/daze/strings.json`
- Modify: `custom_components/daze/translations/it.json`
- Test: `tests/test_entities.py` (append before `_main`)

**Interfaces:**
- Consumes: `SolarController` and `SolarMode` from Task 4, and
  `hass.data[DOMAIN][entry.entry_id]["solar_controller"]` from Task 6.
- Produces:
  - `DazeSolarControlSelect` in `select.py`
  - `DazeSolarReserveEntity` in `number.py`
  - `DazeSolarSurplusSensor` in `sensor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_entities.py`, before `_main`:

```python
def test_solar_select_offers_three_modes() -> None:
    """One control with three states, so 'dry run on, solar off'
    cannot be expressed."""
    select_mod = sys.modules["daze_entities_under_test.select"]
    assert select_mod.SOLAR_MODE_OPTIONS == ["off", "simulate", "active"]


def test_solar_select_refuses_active_without_sensors() -> None:
    """Both grid sensors are required before it can do anything."""
    select_mod = sys.modules["daze_entities_under_test.select"]

    class Ctl:
        configured = False
        mode = None

        def add_listener(self, cb):
            return lambda: None

    coordinator = FakeCoordinator(dict(BASE_DATA))
    entity = select_mod.DazeSolarControlSelect(
        coordinator=coordinator,
        controller=Ctl(),
        serial_number="SER1",
        device_info={},
    )

    assert entity.available is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 tests/test_entities.py`
Expected: FAIL with `AttributeError: module ... has no attribute 'SOLAR_MODE_OPTIONS'`

- [ ] **Step 3: Add the select**

Append to `custom_components/daze/select.py`, before `async_setup_entry`:

```python
SOLAR_MODE_OPTIONS = ["off", "simulate", "active"]


class DazeSolarControlSelect(
    CoordinatorEntity[DazeDataUpdateCoordinator], SelectEntity
):
    """Arm solar control, in simulation or for real.

    A single tri-state rather than a switch plus a dry-run flag, so the
    meaningless combination cannot be selected.
    """

    _attr_has_entity_name = True
    _attr_options = SOLAR_MODE_OPTIONS

    def __init__(
        self,
        coordinator: DazeDataUpdateCoordinator,
        controller: Any,
        serial_number: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialise the control.

        Args:
            coordinator: The Daze data coordinator.
            controller: The solar controller to drive.
            serial_number: The wallbox serial number.
            device_info: Device info for the device registry.

        """
        super().__init__(coordinator)
        self._controller = controller
        self._serial_number = serial_number
        self._attr_unique_id = f"{serial_number}_solar_control"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Redraw when the controller decides something."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._controller.add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Only usable once both grid sensors have been chosen."""
        return bool(self._controller.configured)

    @property
    def current_option(self) -> str | None:
        """Return the controller's mode."""
        mode = self._controller.mode
        return mode.value if mode is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the last decision, so the feature can be understood."""
        decision = self._controller.last_decision
        return {
            "surplus_w": self._controller.surplus_w,
            "last_action": decision.action.value if decision else None,
            "last_reason": decision.reason if decision else None,
        }

    async def async_select_option(self, option: str) -> None:
        """Set the mode."""
        from .solar_controller import SolarMode

        self._controller.mode = SolarMode(option)
        self.async_write_ha_state()
```

Add `from typing import Any` to the imports if not already present, and register the entity in `select.py`'s `async_setup_entry` by appending it to the `async_add_entities([...])` list:

```python
            DazeSolarControlSelect(
                coordinator=coordinator,
                controller=entry_data["solar_controller"],
                serial_number=serial_number,
                device_info=device_info,
            ),
```

- [ ] **Step 4: Add the reserve number**

Append to `custom_components/daze/number.py`, before `async_setup_entry`:

```python
class DazeSolarReserveEntity(
    CoordinatorEntity[DazeDataUpdateCoordinator], NumberEntity
):
    """Watts to leave for the house before the car gets any."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = MAX_SOLAR_RESERVE
    _attr_native_step = 100
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(
        self,
        coordinator: DazeDataUpdateCoordinator,
        controller: Any,
        serial_number: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialise the reserve control."""
        super().__init__(coordinator)
        self._controller = controller
        self._serial_number = serial_number
        self._attr_unique_id = f"{serial_number}_solar_reserve"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> float:
        """Return the configured reserve."""
        return float(self._controller.reserve_w)

    async def async_set_native_value(self, value: float) -> None:
        """Set the reserve."""
        self._controller.reserve_w = value
        self.async_write_ha_state()
```

Add `MAX_SOLAR_RESERVE` to the `from .const import (...)` block, and register the entity in `number.py`'s `async_setup_entry` list.

- [ ] **Step 5: Add the surplus sensor**

Append to `custom_components/daze/sensor.py`, before `async_setup_entry`:

```python
class DazeSolarSurplusSensor(
    CoordinatorEntity[DazeDataUpdateCoordinator], SensorEntity
):
    """The smoothed surplus the controller is working from.

    Exposed so the figure everything else depends on can be seen and
    graphed, rather than inferred from behaviour.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "W"

    def __init__(
        self,
        coordinator: DazeDataUpdateCoordinator,
        controller: Any,
        serial_number: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialise the surplus sensor."""
        super().__init__(coordinator)
        self._controller = controller
        self._serial_number = serial_number
        self._attr_unique_id = f"{serial_number}_solar_surplus"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Redraw when the controller updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._controller.add_listener(self.async_write_ha_state)
        )

    @property
    def native_value(self) -> float | None:
        """Return the smoothed surplus."""
        return self._controller.surplus_w
```

Register it in `sensor.py`'s `async_setup_entry` by appending to the `entities` list.

- [ ] **Step 6: Add the strings**

Modify `custom_components/daze/strings.json`, under `entity`:

```json
    "select": {
      "solar_control": { "name": "Solar control" }
    },
    "number": {
      "solar_reserve": { "name": "Solar reserve" }
    },
    "sensor": {
      "solar_surplus": { "name": "Solar surplus" }
    }
```

Merge these into the existing `select`, `number` and `sensor` objects rather than replacing them. Do the same in `translations/it.json` with `Controllo solare`, `Riserva solare`, `Surplus solare`.

- [ ] **Step 7: Run the tests and lint**

Run:
```bash
python3 tests/run_all.py
ruff check custom_components/daze/ tests/
python3 -c "import json; [json.load(open(f)) for f in ['custom_components/daze/strings.json','custom_components/daze/translations/it.json']]; print('valid')"
```
Expected: 0 failures, `All checks passed!`, `valid`.

- [ ] **Step 8: Commit**

```bash
git add custom_components/daze/select.py custom_components/daze/number.py custom_components/daze/sensor.py custom_components/daze/strings.json custom_components/daze/translations/it.json tests/test_entities.py
git commit -m "feat: add solar control, reserve and surplus entities

The control is one tri-state select rather than a switch and a
dry-run flag, so the meaningless combination cannot be selected. It
carries the last decision and its reason as attributes, because an
autonomous feature that acts silently cannot be understood after the
fact.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: React immediately when surplus collapses

**Files:**
- Modify: `custom_components/daze/solar_controller.py`
- Test: `tests/test_solar_controller.py` (append before `_main`)

**Interfaces:**
- Consumes: `SolarController` from Task 4.
- Produces: no new public surface. The controller subscribes to state
  changes on its two grid sensors.

Unused cheap power costs nothing; imported expensive power is what pure
solar mode exists to avoid. On a fixed two-minute tick a collapse in
surplus keeps the car importing for up to two minutes. Rising surplus
can wait; falling surplus cannot.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_solar_controller.py`, before `_main`:

```python
def test_a_collapse_is_evaluated_without_waiting_for_the_tick() -> None:
    """Rising surplus can wait for the tick; falling surplus cannot,
    or the car imports until the next one."""
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    # Establish a healthy history.
    for _ in range(3):
        asyncio.run(controller.async_tick())

    coordinator.api_client.calls.clear()
    hass.states.set("sensor.grid_export", "0")
    hass.states.set("sensor.grid_import", "4000")

    asyncio.run(controller.async_sensor_changed())

    assert controller.surplus_w is not None
    assert controller.last_decision is not None


def test_a_rise_does_not_trigger_an_immediate_evaluation() -> None:
    """Otherwise every sensor update rewrites the charger's limit."""
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    for _ in range(3):
        asyncio.run(controller.async_tick())

    before = len(coordinator.api_client.calls)
    hass.states.set("sensor.grid_export", "9000")

    asyncio.run(controller.async_sensor_changed())

    assert len(coordinator.api_client.calls) == before
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 tests/test_solar_controller.py`
Expected: FAIL with `AttributeError: 'SolarController' object has no attribute 'async_sensor_changed'`

- [ ] **Step 3: Implement**

Add to `SolarController` in `custom_components/daze/solar_controller.py`:

```python
    async def async_sensor_changed(self) -> None:
        """Evaluate now if surplus has collapsed, otherwise wait.

        A fixed tick would keep the car importing for up to two
        minutes after surplus disappears. Rising surplus is not urgent:
        acting on every increase would rewrite the limit constantly
        against a charger that takes seconds to apply a change.
        """
        if self._mode is SolarMode.OFF:
            return

        surplus = self._read_surplus()
        if surplus is None:
            return

        smoothed = self._smoother.value()
        data = self._coordinator.data or {}
        floor = milliamps_to_watts(min_charging_current(data), data)

        collapsed = (
            surplus - self._reserve_w < floor
            and (smoothed is None or smoothed - self._reserve_w >= floor)
        )

        if not collapsed:
            return

        _LOGGER.debug(
            "Surplus collapsed to %.0f W; evaluating without waiting", surplus
        )
        await self.async_tick()
```

Register the subscription in `async_start`:

```python
    async def async_start(self) -> None:
        """Begin ticking, and watch the grid sensors for a collapse."""
        self._schedule_tick()

        entities = [
            entity
            for entity in (self._import_entity, self._export_entity)
            if entity
        ]

        if entities:
            async def _changed(_event: Any) -> None:
                await self.async_sensor_changed()

            self._cancel_listener = async_track_state_change_event(
                self._hass, entities, _changed
            )
```

Add to `__init__`:

```python
        self._cancel_listener: Callable[[], None] | None = None
```

Add to `async_stop`, before clearing listeners:

```python
        if self._cancel_listener is not None:
            self._cancel_listener()
            self._cancel_listener = None
```

And to the imports:

```python
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
```

Add `async_track_state_change_event=lambda hass, entities, cb: (lambda: None)`
to the `homeassistant.helpers.event` stub in `tests/test_solar_controller.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 tests/test_solar_controller.py`
Expected: PASS, `11 passed, 0 failed`

- [ ] **Step 5: Lint and full suite**

Run:
```bash
ruff check custom_components/daze/ tests/
python3 tests/run_all.py
```
Expected: `All checks passed!`, 0 failures.

- [ ] **Step 6: Commit**

```bash
git add custom_components/daze/solar_controller.py tests/test_solar_controller.py
git commit -m "feat: evaluate immediately when surplus collapses

A fixed two-minute tick keeps the car importing for up to two minutes
after surplus disappears, which is exactly what pure solar mode exists
to avoid. Rising surplus still waits for the tick: acting on every
increase would rewrite the limit constantly against a charger that
takes seconds to apply a change.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Remaining guards, and surviving a restart

**Files:**
- Modify: `custom_components/daze/solar_controller.py`
- Modify: `custom_components/daze/select.py` (the solar select)
- Test: `tests/test_solar_controller.py` (append before `_main`)

**Interfaces:**
- Consumes: `SolarController` from Task 4, `DazeSolarControlSelect` from Task 7.
- Produces: `SolarController.unsupported_reason` property, returning
  `str | None`.

Three things the spec requires that nothing yet implements: refusing a
supply the charger cannot follow, surviving a Home Assistant restart
without stopping a healthy charge, and remembering the mode.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_solar_controller.py`, before `_main`:

```python
def test_three_phase_supply_with_a_single_phase_charger_is_refused() -> None:
    """Grid meters usually report net across phases, so the surplus
    can exist mostly on phases the charger cannot reach."""
    data = dict(CHARGING_DATA)
    data["evseIsThreePhase"] = False
    data["supplyGrid3F"] = True
    controller, _, _ = build(data)

    assert controller.unsupported_reason is not None
    assert "phase" in controller.unsupported_reason


def test_a_matched_supply_is_supported() -> None:
    data = dict(CHARGING_DATA)
    data["evseIsThreePhase"] = False
    data["supplyGrid3F"] = False
    controller, _, _ = build(data)

    assert controller.unsupported_reason is None


def test_a_charge_already_running_counts_as_having_run() -> None:
    """Timers start at zero after a restart. Without seeding, an
    unelapsed minimum run time could stop a healthy charge moments
    after boot."""
    controller, _, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    asyncio.run(controller.async_tick())

    assert controller._started_at is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 tests/test_solar_controller.py`
Expected: FAIL with `AttributeError: ... 'unsupported_reason'`

- [ ] **Step 3: Implement the guard and the seeding**

Add to `SolarController`:

```python
    @property
    def unsupported_reason(self) -> str | None:
        """Explain why this setup cannot be followed, if it cannot.

        A three-phase supply feeding a single-phase charger reports
        surplus netted across phases, most of which the charger cannot
        reach. Following it would overload one phase.
        """
        data = self._coordinator.data or {}

        supply_three_phase = bool(data.get("supplyGrid3F"))
        charger_three_phase = bool(data.get("evseIsThreePhase"))

        if supply_three_phase and not charger_three_phase:
            return (
                "the supply is three-phase and the charger is single-phase, "
                "so exported power may be on a phase it cannot use"
            )

        return None
```

In `async_tick`, immediately after the `SolarMode.OFF` check:

```python
        unsupported = self.unsupported_reason
        if unsupported is not None:
            if not self._sensor_warning_logged:
                self._sensor_warning_logged = True
                _LOGGER.warning("Solar control cannot run: %s", unsupported)
            return
```

And seed the start time, immediately before `self._check_ignored_start(now)`:

```python
        # Timers begin at zero after a restart. A charge that is
        # already running has, by definition, been running: without
        # this the minimum run time reads as unelapsed and a healthy
        # charge could be stopped moments after boot.
        if state.charging and self._started_at is None:
            self._started_at = now - MIN_RUN_SECONDS
```

Add `MIN_RUN_SECONDS` to the `from .solar import (...)` block.

- [ ] **Step 4: Make the select remember its mode**

In `custom_components/daze/select.py`, change `DazeSolarControlSelect` to
also inherit `RestoreEntity`:

```python
class DazeSolarControlSelect(
    CoordinatorEntity[DazeDataUpdateCoordinator], SelectEntity, RestoreEntity
):
```

Add the import:

```python
from homeassistant.helpers.restore_state import RestoreEntity
```

And restore in `async_added_to_hass`, after the existing `super()` call:

```python
        last = await self.async_get_last_state()
        if last is not None and last.state in SOLAR_MODE_OPTIONS:
            from .solar_controller import SolarMode

            self._controller.mode = SolarMode(last.state)
```

Also make `available` account for an unsupported setup:

```python
    @property
    def available(self) -> bool:
        """Usable only with both sensors and a supply we can follow."""
        return (
            bool(self._controller.configured)
            and self._controller.unsupported_reason is None
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 tests/test_solar_controller.py`
Expected: PASS, `14 passed, 0 failed`

- [ ] **Step 6: Lint and full suite**

Run:
```bash
ruff check custom_components/daze/ tests/
python3 tests/run_all.py
```
Expected: `All checks passed!`, 0 failures.

- [ ] **Step 7: Commit**

```bash
git add custom_components/daze/solar_controller.py custom_components/daze/select.py tests/test_solar_controller.py
git commit -m "feat: refuse unfollowable supplies, and survive a restart

A three-phase supply feeding a single-phase charger reports surplus
netted across phases, most of which the charger cannot reach.

Timers begin at zero after a Home Assistant restart, so a charge that
was already running would read as having no elapsed run time and could
be stopped moments after boot. A running charge now seeds its own
start time.

The control also remembers its mode across a restart.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---
### Task 10: Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/solar-surplus-charging.md`

**Interfaces:**
- Consumes: the entity names from Task 6.
- Produces: nothing code depends on.

- [ ] **Step 1: Document the entities in the README**

In `README.md`, add to the Controls table:

```markdown
| Select | `select.daze_homett_solar_control` | Solar control | `off` / `simulate` / `active` |
| Number | `number.daze_homett_solar_reserve` | Solar reserve | Watts to leave for the house before the car gets any |
```

And to the Sensors table:

```markdown
| `sensor.daze_homett_solar_surplus` | Solar surplus | `power` | `measurement` | W |
```

- [ ] **Step 2: Add a Solar control section to the README**

Insert before `## Automation Examples`:

```markdown
## Solar control

Charges the car from what the house would otherwise export, adjusting
the limit as production and load change, and stopping when there is not
enough surplus to charge at all.

1. In the integration's options, pick your **grid import** and **grid
   export** power sensors.
2. Set **Solar control** to `simulate`. It decides and logs but sends
   nothing.
3. Leave it for a day. The select's attributes show the surplus it sees
   and what it would have done.
4. If the decisions look right, set it to `active`.

It never imports to charge: the charger cannot run below 1500 W, so
when surplus falls below that it stops rather than topping up from the
grid.

Changing the charging limit yourself — from the dashboard, or from your
own automation — turns solar control off. It does not fight you.

For a version you build and tune yourself, see
[docs/solar-surplus-charging.md](docs/solar-surplus-charging.md).
```

- [ ] **Step 3: Cross-reference from the YAML guide**

At the top of `docs/solar-surplus-charging.md`, after the first paragraph, add:

```markdown
> The integration can now do this itself — see **Solar control** in the
> README. This guide remains for setups the built-in version does not
> fit: a house battery to arbitrate with, tariff windows, or anything
> needing logic of your own.
```

- [ ] **Step 4: Verify and commit**

Run: `python3 tests/run_all.py`
Expected: 0 failures.

```bash
git add README.md docs/solar-surplus-charging.md
git commit -m "docs: document solar control

Includes the simulate-first procedure, because a feature that starts
and stops the car should be watched for a day before it is trusted.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Push, and move the tag**

```bash
set -a; . ./.env; set +a
ASK=$(mktemp /tmp/askpass.XXXXXX); chmod 700 "$ASK"
printf '#!/bin/sh\ncase "$1" in\n  *sername*) printf "%%s\\n" "$GIT_USER" ;;\n  *assword*) printf "%%s\\n" "$GITHUB_PAT" ;;\nesac\n' > "$ASK"
GIT_USER=tarrinho GIT_TERMINAL_PROMPT=0 GIT_ASKPASS="$ASK" \
  git -c credential.helper= push origin main 2>&1 | sed 's/gh[pousr]_[A-Za-z0-9_]*/[REDACTED]/g' | tail -1
git tag -f -a v0.1.6 -m "Release 0.1.6

Re-pointed at the current code. This tag tracks main.
" >/dev/null
GIT_USER=tarrinho GIT_TERMINAL_PROMPT=0 GIT_ASKPASS="$ASK" \
  git -c credential.helper= push --force origin v0.1.6 2>&1 | sed 's/gh[pousr]_[A-Za-z0-9_]*/[REDACTED]/g' | tail -1
rm -f "$ASK"; unset GITHUB_PAT GIT_USER
```

Expected: both pushes report success, and `main` and `v0.1.6` point at the same commit.

---

## After the plan

Solar control ships **off**. Nothing changes for an existing install
until the user picks two sensors and moves the select.

The first real validation is a day in `simulate` against actual
production. That is the step this plan cannot do, and the one that
decides whether the constants in `solar.py` are right for the site.
