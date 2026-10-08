# Solar Surplus Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Charge the car from solar surplus by adjusting the charger's limit and starting or stopping it, driven from inside the integration rather than from user-written automations.

**Architecture:** A pure decision function with no Home Assistant imports decides what to do; a controller object owns a timer, reads the user's grid sensors, and carries the decision out through the existing API client. Three entities expose and control it. This mirrors `payload.py` and `optimistic.py`, which are pure and have produced no review defects, while the Home-Assistant-coupled code has produced most of them.

**Tech Stack:** Python 3.12+, Home Assistant custom integration, aiohttp (already a dependency). No new third-party packages. Tests run standalone via `python3 tests/run_all.py` — pytest is not installed in this environment.

**Spec:** `docs/superpowers/specs/2026-09-29-solar-surplus-control-design.md`

## Global Constraints

- **Version:** set `manifest.json` to `"version": "0.2.0"` in the final
  documentation task, and nowhere else. No other task touches it. The
  maintainer chose this number; do not invent a different one.
- **No task publishes.** This work lands on the `solar-control` branch.
  Merging it and moving the release tag is the operator's step, after
  review, and `main` is several tasks behind the branch while the plan
  runs: a push from inside a task would publish a release without the
  feature in it. Commit; do not push, tag or force-push.
- **Every commit message ends with the attribution line your own session
  specifies.** Do not copy a model name from this plan: a subagent running
  a different model attributes to that model, which is accurate.
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
Expected: PASS, `28 passed, 0 failed`

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


def test_the_reserve_lowers_the_target() -> None:
    """The house gets its share before the car does.

    Compared against an identical controller with no reserve, rather
    than asserting an exact figure: the target is also clamped to the
    charger's ceiling, so the difference is not simply the reserve.
    """
    plain, _, _ = build()
    plain.mode = controller_module.SolarMode.ACTIVE

    withheld, _, _ = build()
    withheld.mode = controller_module.SolarMode.ACTIVE
    withheld.reserve_w = 2000

    for _ in range(2):
        asyncio.run(plain.async_tick())
        asyncio.run(withheld.async_tick())

    assert plain.last_decision is not None
    assert withheld.last_decision is not None

    # The reserve must not change what surplus is, only what the car
    # is allowed to take of it.
    assert plain.surplus_w == withheld.surplus_w == 8000

    plain_target = plain.last_decision.target_watts
    withheld_target = withheld.last_decision.target_watts
    assert plain_target is not None
    assert withheld_target is not None
    assert withheld_target < plain_target


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
Expected: PASS, `0 failed`, with one more test than the suite had before
this task. The absolute count is deliberately not stated: Task 4's review
added twelve tests to this file, so any figure written here ages badly.

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
- Modify: `custom_components/daze/switch.py` (the charge control switch)
- Modify: `custom_components/daze/coordinator.py` (declare the attribute)
- Modify: `custom_components/daze/solar_controller.py` (add `disarm`, accept a reserve)
- Test: `tests/test_entities.py` (append before `_main`)
- Test: `tests/test_solar_controller.py` (append before `_main`)

**Interfaces:**
- Consumes: `SolarController`, `SolarMode` from Task 4, and
  `CONF_GRID_IMPORT_SENSOR`, `CONF_GRID_EXPORT_SENSOR`,
  `CONF_SOLAR_RESERVE`, `DEFAULT_SOLAR_RESERVE` from Task 3.
- Produces:
  - `hass.data[DOMAIN][entry.entry_id]["solar_controller"]`
  - `SolarController.disarm(reason)`
  - `SolarController(..., reserve_w=...)`: the reserve arrives from the
    entry's options rather than starting at zero every time.
  - `DazeDataUpdateCoordinator.solar_controller`, declared on the class
    so every entity and service can read it without `getattr`.
  - `_reload_signature(entry)` in `__init__.py`, so that writing the
    reserve back to the options does not reload the entry.

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


def test_a_manual_charge_toggle_disarms_solar_control() -> None:
    """The switch is a control too.

    Without this the user presses the toggle, and the next tick — at
    most two minutes later — sees a connected car and sustained surplus
    and commands the opposite. Solar control would be fighting the
    person holding the button.
    """
    class Ctl:
        disarmed = False

        def disarm(self, reason: str) -> None:
            self.disarmed = True

    coordinator = FakeCoordinator(dict(BASE_DATA))
    coordinator.solar_controller = Ctl()
    client = FakeApi()

    switch_module = sys.modules["daze_entities_under_test.switch"]
    entity = switch_module.DazeWallboxSwitchEntity(
        coordinator=coordinator, api_client=client,
        serial_number="SER1", device_info={},
    )

    asyncio.run(entity.async_turn_on())

    assert coordinator.solar_controller.disarmed is True
```

And append to `tests/test_solar_controller.py`, before `_main`:

```python
def test_disarming_clears_the_clocks_a_rearm_would_misread() -> None:
    """Disarming ends the episode, not just the mode.

    A start this controller issued, and the back-off that start could
    still arm, must not survive into the next time solar control is
    switched on. Left behind, a start issued at noon and abandoned at
    12:01 is judged at 14:00 against a car that has long since
    finished, arming a 60-minute back-off for a start nobody is
    waiting on.
    """
    controller, _, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE
    controller._start_issued_at = 100.0
    controller._backoff_until = 1e9
    controller._started_at = 100.0

    controller.disarm("the charging limit was set manually")

    assert controller.mode is controller_module.SolarMode.OFF
    assert controller._start_issued_at is None
    assert controller._backoff_until == 0.0
    assert controller._started_at is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
python3 tests/test_entities.py
python3 tests/test_solar_controller.py
```
Expected: FAIL on `assert ... .disarmed is True`, and
`AttributeError: 'SolarController' object has no attribute 'disarm'`.

Step 5 declares `solar_controller` on the real coordinator, so
`FakeCoordinator` in `tests/test_entities.py` has to mirror it or every
existing test that sets a value raises `AttributeError` from the new
helper. Add one line to its `__init__`:

```python
        # Mirrors the real coordinator, which declares this so entities
        # and services can read it without getattr.
        self.solar_controller: Any = None
```

The two tests above then overwrite it with their own double.

- [ ] **Step 3: Add `disarm` to the controller**

In `custom_components/daze/solar_controller.py`, add to `SolarController`:

```python
    def disarm(self, reason: str) -> None:
        """Turn solar control off because something else took over.

        Called when a limit change arrives through an entity or a
        service, which by construction means it did not come from here.

        Every clock of the episode goes with the mode, not just the two
        threshold timers. A start this controller issued is no longer
        ours to judge the car against: left set, _start_issued_at is
        read hours later, against a car that has long since finished,
        and arms a 60-minute back-off for a start nobody is waiting on.
        A back-off already armed goes too — it was armed to stop this
        controller retrying, and the user has just taken over anyway.
        """
        if self._mode is SolarMode.OFF:
            return

        _LOGGER.info("Solar control disarmed: %s", reason)
        self._mode = SolarMode.OFF
        self._above_since = None
        self._below_since = None
        self._collapsed_since = None
        self._started_at = None
        self._start_issued_at = None
        self._backoff_until = 0.0
        self._notify()
```

`_collapsed_since` is Task 8's; if Task 8 has not run yet, leave that
line out and Task 8 will add it with the attribute.

Clearing `_started_at` here is safe because Task 10 seeds it again from
an observed charge, once per charging episode. If Task 10's seeding is
ever removed, this line must go with it, or a charge still running when
solar control is re-armed can never be stopped: `_elapsed(None)` is
`0.0`, which reads as "just started" for ever.

- [ ] **Step 4: Call it from the controls**

In `custom_components/daze/number.py`, add this helper to both `DazeWallboxNumberEntity` and `DazeWallboxPowerEntity`:

```python
    def _disarm_solar(self) -> None:
        """Hand control back to the user.

        Solar control writes through the API client, so anything
        arriving here came from a person or their automation.
        """
        controller = self.coordinator.solar_controller
        if controller is not None:
            controller.disarm("the charging limit was set manually")
```

Call `self._disarm_solar()` in both `async_set_native_value` methods, immediately after the no-op guard returns and before the validation check. Before validation rather than after, so that a value the charger would reject still counts as the user taking over: they have expressed the intent either way, and solar control writing the limit a second later is exactly what the rule exists to prevent.

In `custom_components/daze/switch.py`, add the same helper to `DazeWallboxSwitchEntity`, worded for what it controls:

```python
    def _disarm_solar(self) -> None:
        """Hand control back to the user.

        Solar control starts and stops the charge through the API
        client, so a toggle arriving here came from a person or their
        automation. Without this the next tick reverses them: the car
        is connected and the surplus is unchanged, so decide() returns
        the opposite command within two minutes.
        """
        controller = self.coordinator.solar_controller
        if controller is not None:
            controller.disarm("charging was started or stopped manually")
```

Call `self._disarm_solar()` in both `async_turn_on` and `async_turn_off`, immediately after the idempotent no-op guard returns and before the offline check.

- [ ] **Step 5: Let the controller be told its reserve**

In `custom_components/daze/solar_controller.py`, add a keyword to
`SolarController.__init__` and use it instead of the hardcoded zero:

```python
        import_entity: str | None,
        export_entity: str | None,
        reserve_w: float = 0.0,
```

and, in the body, replace `self._reserve_w = 0.0` with:

```python
        self._reserve_w = max(0.0, float(reserve_w))
```

Document the keyword in the docstring's `Args:` block:

```python
            reserve_w: Watts to leave for the house, restored from the
                config entry's options. Held there rather than only in
                memory: a reserve that returns to zero on every restart
                gives the car everything the house was keeping, and
                does it silently.
```

- [ ] **Step 6: Declare the attribute on the coordinator**

In `custom_components/daze/coordinator.py`, add to
`DazeDataUpdateCoordinator.__init__`, beside the other state:

```python
        # Set by async_setup_entry. Declared here so every entity and
        # service can read it directly: a getattr default would turn a
        # wiring mistake into silent no-disarm, which is the failure
        # this whole mechanism exists to prevent.
        self.solar_controller: Any = None
```

`Any` is already imported in `coordinator.py`.

- [ ] **Step 7: Create and tear down the controller**

In `custom_components/daze/__init__.py`, inside `async_setup_entry`, after the coordinator is created and before `hass.data[DOMAIN][entry.entry_id] = {...}`:

```python
    solar_controller = SolarController(
        hass=hass,
        coordinator=coordinator,
        import_entity=entry.options.get(CONF_GRID_IMPORT_SENSOR),
        export_entity=entry.options.get(CONF_GRID_EXPORT_SENSOR),
        reserve_w=entry.options.get(
            CONF_SOLAR_RESERVE, DEFAULT_SOLAR_RESERVE
        ),
    )
    # The entities reach the controller through the coordinator, which
    # every one of them already holds.
    coordinator.solar_controller = solar_controller
    await solar_controller.async_start()
```

Add `"solar_controller": solar_controller,` to the `hass.data[DOMAIN][entry.entry_id]` dict.

In `async_unload_entry`, **inside the existing `if entry_data is not None:` block**, before `coordinator.async_shutdown_timers()`:

```python
            controller = entry_data.get("solar_controller")
            if controller is not None:
                await controller.async_stop()
```

The indentation is load-bearing. `entry_data` is `None` whenever the
entry was already cleaned up — a second unload, or an unload after a
failed setup — and that guard is why the existing code checks it. One
level out, `None.get(...)` raises `AttributeError` and the rest of the
teardown never runs, leaving the coordinator's timers firing against a
closed client: the exact fault the comment above that block describes.

Add the imports:

```python
from .const import (
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_SOLAR_RESERVE,
    DEFAULT_SOLAR_RESERVE,
)
from .solar_controller import SolarController
```

merging the `const` names into the existing import block.

- [ ] **Step 8: Stop reloading the entry for a reserve change**

The reserve is stored in the entry's options (Task 7 writes it there),
and `_async_update_listener` currently reloads the entry on any options
change. Without this, every step of the reserve slider tears the
integration down and rebuilds it: timers cancelled, entities recreated,
the controller's smoothing window emptied, and the mode reset until the
select restores it.

In `custom_components/daze/__init__.py`, add above `_async_update_listener`:

```python
def _reload_signature(entry: ConfigEntry) -> tuple[Any, Any]:
    """Return the parts of an entry whose change needs a reload.

    The solar reserve is deliberately absent. It is applied live by the
    controller, so rewriting it is not a reason to rebuild the entry;
    everything else — credentials, the poll interval, the grid sensors
    the controller is constructed with — is.
    """
    options = {
        key: value
        for key, value in entry.options.items()
        if key != CONF_SOLAR_RESERVE
    }
    return (dict(entry.data), options)
```

and replace the body of `_async_update_listener` with:

```python
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    signature = _reload_signature(entry)

    if entry_data is not None and entry_data.get("reload_signature") == (
        signature
    ):
        _LOGGER.debug(
            "Config entry %s changed in a way that needs no reload",
            entry.entry_id,
        )
        return

    _LOGGER.debug("Config entry updated for %s — reloading", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)
```

Add `"reload_signature": _reload_signature(entry),` to the
`hass.data[DOMAIN][entry.entry_id]` dict in `async_setup_entry`, and
import `Any` from `typing` if it is not already imported there.

- [ ] **Step 9: Disarm from the services too**

In `custom_components/daze/__init__.py`, inside `_handle_set_charging_current`, `_handle_start_charge` and `_handle_stop_charge`, **immediately before** `_refuse_if_offline()`:

```python
        if coordinator.solar_controller is not None:
            coordinator.solar_controller.disarm(
                "the charge was commanded by a service call"
            )
```

Before the offline check rather than after it, for the same reason as
the entities: `_refuse_if_offline()` raises, and a user whose charger
is briefly unreachable has still expressed the intent to take over.
Word the reason for each handler — "the charging current was set by a
service call" in `_handle_set_charging_current`.

- [ ] **Step 10: Run the tests and lint**

Run:
```bash
python3 tests/run_all.py
ruff check custom_components/daze/ tests/
```
Expected: 0 failures, `All checks passed!`, with three more tests than
the suite had before this task.

- [ ] **Step 11: Commit**

```bash
git add custom_components/daze/__init__.py custom_components/daze/number.py custom_components/daze/switch.py custom_components/daze/coordinator.py custom_components/daze/solar_controller.py tests/test_entities.py tests/test_solar_controller.py
git commit -m "feat: wire solar control into the entry and disarm on override

The controller is created with the entry and stopped when it unloads,
alongside the coordinator's own timers. It is told the reserve from the
entry's options rather than starting at zero, and a reserve-only
options change no longer reloads the entry.

Any limit change or charge toggle arriving through an entity or a
service disarms solar control, because the controller writes through
the API client and never through an entity. That makes the rule
mechanical rather than a flag that has to be set and cleared
correctly. Disarming clears the episode's clocks too, so re-arming
hours later is not judged against a start nobody is waiting on.

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
- Consumes: `SolarController` and `SolarMode` from Task 4,
  `hass.data[DOMAIN][entry.entry_id]["solar_controller"]` from Task 6,
  and `CONF_SOLAR_RESERVE` / `MAX_SOLAR_RESERVE` from Task 3.
- Produces:
  - `DazeSolarControlSelect` in `select.py`, which refuses to leave
    `off` until both grid sensors are chosen.
  - `DazeSolarReserveEntity` in `number.py`, which persists the reserve
    to the config entry's options.
  - `DazeSolarSurplusSensor` in `sensor.py`

Note on coverage: `tests/test_entities.py` loads `const`, `payload`,
`models`, `api`, `coordinator`, `number`, `select` and `switch` — not
`sensor.py`, which pulls in the sensor catalogue and would need more
stubs than it is worth here. The surplus sensor is therefore not
covered by a test in this task. That is accepted: it is a read-only
projection of `controller.surplus_w`, which Task 2 and Task 4 already
test directly.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_entities.py`, before `_main`:

```python
def test_solar_select_offers_three_modes() -> None:
    """One control with three states, so 'dry run on, solar off'
    cannot be expressed."""
    select_mod = sys.modules["daze_entities_under_test.select"]
    assert select_mod.SOLAR_MODE_OPTIONS == ["off", "simulate", "active"]


def _solar_select(configured: bool = False) -> tuple[Any, Any]:
    """Build the solar select over a controller double."""
    select_mod = sys.modules["daze_entities_under_test.select"]

    class Ctl:
        def __init__(self) -> None:
            self.configured = configured
            self.mode = None

        def add_listener(self, cb):
            return lambda: None

    controller = Ctl()
    entity = select_mod.DazeSolarControlSelect(
        coordinator=FakeCoordinator(dict(BASE_DATA)),
        controller=controller,
        serial_number="SER1",
        device_info={},
    )
    return entity, controller


def test_solar_select_is_unavailable_without_sensors() -> None:
    """Both grid sensors are required before it can do anything."""
    entity, _ = _solar_select(configured=False)

    assert entity.available is False


def test_solar_select_refuses_to_arm_without_sensors() -> None:
    """Availability is a hint to the dashboard, not a gate.

    A service call or an automation reaches async_select_option
    whatever the entity reports, so the refusal the spec requires —
    "both are required before solar control can leave off" — has to be
    enforced in the method that acts, and explained where the caller
    can see it. Asserting `available is False` instead would pass
    against a select that happily arms itself with no sensors at all.
    """
    entity, controller = _solar_select(configured=False)

    raised = False
    try:
        asyncio.run(entity.async_select_option("active"))
    except HomeAssistantError:
        raised = True

    assert raised, "arming without sensors was not refused"
    assert controller.mode is None, "the mode was changed anyway"


def test_solar_select_arms_once_the_sensors_are_there() -> None:
    """The refusal must not be a blanket one."""
    entity, controller = _solar_select(configured=True)

    asyncio.run(entity.async_select_option("simulate"))

    assert controller.mode is not None
    assert controller.mode.value == "simulate"


def test_the_reserve_survives_a_restart() -> None:
    """An in-memory reserve returns to 0 W on every restart, and 0 W
    means the house gets nothing before the car does. A setting that
    exists to hold power back must not quietly stop holding it.
    """
    number_mod = sys.modules["daze_entities_under_test.number"]
    const_mod = sys.modules["daze_entities_under_test.const"]

    class Ctl:
        reserve_w = 0.0

    class FakeEntry:
        options: dict[str, Any] = {"poll_interval": 30}

    class FakeEntries:
        def __init__(self) -> None:
            self.updated: list[dict[str, Any]] = []

        def async_update_entry(self, entry, options=None, **kwargs):
            entry.options = options
            self.updated.append(options)

    class FakeHass:
        def __init__(self) -> None:
            self.config_entries = FakeEntries()

    entry = FakeEntry()
    entity = number_mod.DazeSolarReserveEntity(
        coordinator=FakeCoordinator(dict(BASE_DATA)),
        controller=Ctl(),
        entry=entry,
        serial_number="SER1",
        device_info={},
    )
    entity.hass = FakeHass()

    asyncio.run(entity.async_set_native_value(1500))

    assert entity.native_value == 1500
    assert entry.options[const_mod.CONF_SOLAR_RESERVE] == 1500
    # The rest of the options must survive the write, or saving a
    # reserve would silently drop the user's grid sensors.
    assert entry.options["poll_interval"] == 30
```

Add `from homeassistant.exceptions import HomeAssistantError` to the
test module's imports if it is not already there; the stub harness
already provides it.

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
        """Set the mode, refusing to arm before it can work.

        `available` is a hint for the dashboard. A service call or an
        automation arrives here whatever the entity reports, so the
        rule that both grid sensors are required before solar control
        leaves "off" has to be enforced in the method that acts — and
        raised, not logged, because the caller asked for something and
        is entitled to know it did not happen.
        """
        from .solar_controller import SolarMode

        if option != "off" and not self._controller.configured:
            raise HomeAssistantError(
                "Solar control needs both a grid import and a grid "
                "export sensor before it can be armed. Set them in the "
                "integration's options."
            )

        self._controller.mode = SolarMode(option)
        self.async_write_ha_state()
```

Add `from typing import Any` and
`from homeassistant.exceptions import HomeAssistantError` to the
imports if not already present, and register the entity in `select.py`'s `async_setup_entry` by appending it to the `async_add_entities([...])` list:

```python
            DazeSolarControlSelect(
                coordinator=coordinator,
                controller=entry_data["solar_controller"],
                serial_number=serial_number,
                device_info=device_info,
            ),
```

Read the controller with `entry_data["solar_controller"]` only if you
are confident the key is always present — it is, since Task 6 writes it
before the platforms are forwarded. If that ordering ever changes, a
`KeyError` here fails the whole select platform and takes
`select.daze_operation_mode` down with it, so the safer form is:

```python
    entities: list[SelectEntity] = [
        DazeWallboxSelectEntity(...),  # the existing entity, unchanged
    ]

    solar_controller = entry_data.get("solar_controller")
    if solar_controller is not None:
        entities.append(
            DazeSolarControlSelect(
                coordinator=coordinator,
                controller=solar_controller,
                serial_number=serial_number,
                device_info=device_info,
            )
        )

    async_add_entities(entities)
```

Use the second form. The same applies to `number.py` and `sensor.py`
below.

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
        entry: ConfigEntry,
        serial_number: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialise the reserve control.

        Args:
            coordinator: The Daze data coordinator.
            controller: The solar controller whose reserve this is.
            entry: The config entry the reserve is persisted in.
            serial_number: The wallbox serial number.
            device_info: Device info for the device registry.

        """
        super().__init__(coordinator)
        self._controller = controller
        self._entry = entry
        self._serial_number = serial_number
        self._attr_unique_id = f"{serial_number}_solar_reserve"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> float:
        """Return the configured reserve."""
        return float(self._controller.reserve_w)

    async def async_set_native_value(self, value: float) -> None:
        """Set the reserve, and remember it across a restart.

        Written to the config entry's options, not just to the
        controller. An in-memory reserve returns to 0 W every time
        Home Assistant restarts, and 0 W means the house gets nothing
        before the car does — a setting whose whole job is holding
        power back, quietly stopping. Task 6's _reload_signature is
        what keeps this write from reloading the entry on every step
        of the slider.
        """
        self._controller.reserve_w = value
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_SOLAR_RESERVE: int(value)},
        )
        self.async_write_ha_state()
```

Add `CONF_SOLAR_RESERVE` and `MAX_SOLAR_RESERVE` to the `from .const import (...)` block. `ConfigEntry` is already imported in `number.py` under `TYPE_CHECKING`, which is enough for the annotation.

Register the entity in `number.py`'s `async_setup_entry` list, guarded
the same way as the select, and passing the entry that function already
receives:

```python
    solar_controller = entry_data.get("solar_controller")
    if solar_controller is not None:
        entities.append(
            DazeSolarReserveEntity(
                coordinator=coordinator,
                controller=solar_controller,
                entry=entry,
                serial_number=serial_number,
                device_info=device_info,
            )
        )
```

`number.py`'s `async_setup_entry` currently passes a list literal
straight to `async_add_entities`; build it as `entities = [...]` first.

The reserve entity must **not** call `_disarm_solar`. It is solar
control's own setting, not a manual override of the charging limit.

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
    _attr_native_unit_of_measurement = UnitOfPower.WATT

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

`UnitOfPower` is already imported in `sensor.py`.

`sensor.py` builds `entities` as a list comprehension over `SENSORS`,
so there is no literal to extend. Append after it, guarded like the
other two:

```python
    solar_controller = entry_data.get("solar_controller")
    if solar_controller is not None:
        entities.append(
            DazeSolarSurplusSensor(
                coordinator=coordinator,
                controller=solar_controller,
                serial_number=serial_number,
                device_info=device_info,
            )
        )
```

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
fact, and it refuses to leave 'off' until both grid sensors are set:
availability is a hint to the dashboard, not a gate on a service call.

The reserve is persisted to the entry's options. Held only in memory
it returned to 0 W on every restart, which hands the house's share to
the car without saying so.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Start the stop clock when surplus collapses

**Files:**
- Modify: `custom_components/daze/solar_controller.py`
- Test: `tests/test_solar_controller.py` (append before `_main`)

**Interfaces:**
- Consumes: `SolarController` from Task 4.
- Produces: no new public surface. The controller subscribes to state
  changes on its two grid sensors, and gains three private attributes:
  `_collapsed_since` (when the raw reading fell below the floor),
  `_last_evaluation` and `_evaluating`.

**What this task is actually for.** Not what it looks like. The obvious
reading — "a fixed tick keeps the car importing for up to two minutes"
— is wrong, and building to it would produce a task that cannot deliver
what it promises.

Trace it with the real constants. A stop needs
`seconds_below_threshold >= STOP_DELAY_SECONDS`, which is 600 s. That
clock is kept by `_track_thresholds` from the **smoothed** figure, and
the smoother is a five-minute average: after a collapse from 4000 W to
0 W it takes several samples before the average itself drops below the
~1500 W floor. Nothing about evaluating sooner changes the 600 s, so
evaluating sooner saves one tick at most — 120 s out of 700 s or more.

What is worth fixing is the *start* of that clock. Surplus fell at
12:00; the average admits it at 12:04; the stop then fires at 12:14
instead of 12:10, and the car imports at up to the charger's ceiling
for the extra four minutes. So: the collapse itself starts the stop
clock, and the smoothed figure decides what to do — not when the drop
began. That is what the spec means by evaluating a drop immediately.

The second half of the task is making sure this costs nothing. The
120-second tick was the only thing bounding how often the controller
writes to the charger; a sensor-driven path removes that bound in
exactly the direction that writes most, so it needs a latch and a
minimum spacing of its own.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_solar_controller.py`, before `_main`:

```python
def test_a_collapse_starts_the_stop_clock_when_it_happens() -> None:
    """The ten-minute stop delay must run from the collapse, not from
    the moment the five-minute average catches up with it.

    Asserting the mark itself rather than "a stop was sent": no stop
    can be sent at the moment of a collapse — the smoothed figure is
    still healthy, which is the whole reason this path exists — so a
    test that looked for a command would pass against an
    implementation that did nothing at all.
    """
    controller, _, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [20_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        controller._started_at = clock[0] - solar.MIN_RUN_SECONDS - 1

        # A healthy history: 3000 W drawn plus 5000 W exported.
        for _ in range(2):
            asyncio.run(controller.async_tick())
            clock[0] += solar.TICK_SECONDS

        clock[0] += 1
        collapse_at = clock[0]
        hass.states.set(
            "sensor.grid_import", "4000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )

        asyncio.run(controller.async_sensor_changed())

        assert controller._below_since == collapse_at, (
            "the stop clock did not start at the collapse: "
            f"{controller._below_since} instead of {collapse_at}"
        )
    finally:
        controller_module.time.monotonic = original_monotonic


def test_a_collapse_is_evaluated_once_not_on_every_sensor_update() -> None:
    """A grid sensor reporting every ten seconds updates six times a
    minute, and the raw reading stays below the floor for as long as
    the average takes to catch up. Without a latch each of those
    updates runs a full evaluation, and each can rewrite the limit:
    the twenty-command hourly backstop is spent in minutes, and it is
    then not there for the stop when the stop finally comes.
    """
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [21_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        controller._started_at = clock[0] - solar.MIN_RUN_SECONDS - 1
        asyncio.run(controller.async_tick())

        clock[0] += solar.TICK_SECONDS + 1
        hass.states.set(
            "sensor.grid_import", "4000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_sensor_changed())

        after_first = len(coordinator.api_client.calls)

        # The sensor keeps reporting the same collapsed figures.
        for _ in range(6):
            clock[0] += 10
            asyncio.run(controller.async_sensor_changed())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert len(coordinator.api_client.calls) == after_first, (
        "the fast path fired again while the same collapse was still "
        "being counted"
    )


def test_a_recovery_re_arms_the_fast_path() -> None:
    """A kettle is not a collapse.

    When the raw reading comes back above the floor the stop clock must
    let go of it. Otherwise a dozen three-kilowatt kitchen dips over an
    afternoon add up to ten minutes "below the floor" and stop a charge
    that never wanted for surplus.
    """
    controller, _, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [22_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        controller._started_at = clock[0] - solar.MIN_RUN_SECONDS - 1
        asyncio.run(controller.async_tick())

        clock[0] += solar.TICK_SECONDS + 1
        hass.states.set(
            "sensor.grid_import", "4000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_sensor_changed())
        assert controller._below_since is not None

        # The kettle switches off.
        clock[0] += 30
        hass.states.set(
            "sensor.grid_import", "0", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "5000", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_sensor_changed())
        assert controller._collapsed_since is None

        clock[0] += solar.TICK_SECONDS
        asyncio.run(controller.async_tick())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert controller._below_since is None, (
        "the stop clock is still anchored to a collapse that recovered"
    )


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


def test_a_sensor_event_during_a_tick_does_not_start_a_second_one() -> None:
    """async_tick has two callers now, and an API call is an await.

    A sensor event arriving while a tick waits on the charger would
    otherwise run a second evaluation against the same coordinator
    data: both append to _command_times, both reach the same branch,
    and both send the same command. The clock is advanced past the
    minimum spacing inside the call on purpose, so that only the
    re-entrancy guard can be what stops it.
    """
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [23_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    reentered: list[int] = []

    async def _set_current_then_collapse(
        serial: str, current_ma: int, attempts: int = 8
    ) -> dict:
        coordinator.api_client.calls.append(("current", current_ma))
        hass.states.set(
            "sensor.grid_import", "4000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        clock[0] += solar.TICK_SECONDS + 1
        await controller.async_sensor_changed()
        reentered.append(1)
        return {}

    coordinator.api_client.async_set_max_charging_current = (
        _set_current_then_collapse
    )

    try:
        asyncio.run(controller.async_tick())
    finally:
        controller_module.time.monotonic = original_monotonic

    assert reentered == [1], "the sensor event never arrived mid-tick"
    assert len(coordinator.api_client.calls) == 1, (
        "a second evaluation ran inside the first and commanded again"
    )


def test_async_start_still_clears_the_stopped_flag() -> None:
    """async_start is rewritten in this task, and the flag it sets is
    easy to drop on the way past: no other test builds a controller,
    stops it and starts it again, so nothing else would notice.
    """
    controller, _, _ = build()
    SCHEDULED.clear()

    asyncio.run(controller.async_stop())
    assert controller._stopped is True

    asyncio.run(controller.async_start())

    assert controller._stopped is False
    assert len(SCHEDULED) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 tests/test_solar_controller.py`
Expected: FAIL with `AttributeError: 'SolarController' object has no attribute 'async_sensor_changed'`

- [ ] **Step 3: Make the tick non-re-entrant**

`async_tick` has had exactly one caller, the timer. This task adds a
second, driven by sensor events that can arrive while a tick is waiting
on an API call.

In `custom_components/daze/solar_controller.py`, rename the existing
`async_tick` to `_async_evaluate`, leaving its body as it is apart from
the edits in the steps below, and add this in its place:

```python
    async def async_tick(self) -> None:
        """Evaluate once, unless an evaluation is already running.

        Skipping rather than queueing: a queued evaluation would run
        against coordinator data that is by then one command out of
        date, and would decide the same thing twice — two entries in
        _command_times, two commands on the wire.

        A plain flag rather than an asyncio.Lock. The lock would be
        correct in production and wrong in the test suite, which drives
        the controller through asyncio.run() one call at a time: a Lock
        binds itself to the first event loop that acquires it and
        raises RuntimeError on the next one. The event loop is
        single-threaded, so nothing can interleave between the check
        and the assignment below, and a flag is enough.
        """
        if self._evaluating:
            _LOGGER.debug("An evaluation is already running; skipping")
            return

        self._evaluating = True
        try:
            await self._async_evaluate()
        finally:
            self._evaluating = False
```

- [ ] **Step 4: Start the stop clock at the collapse**

In `_async_evaluate`, record when each evaluation ran, immediately
after `now = time.monotonic()`:

```python
        self._last_evaluation = now
```

and pass the raw reading to `_track_thresholds`, which currently
receives only the smoothed state:

```python
        self._track_thresholds(state, now, surplus)
```

Then replace `_track_thresholds` with:

```python
    def _track_thresholds(
        self, state: SolarState, now: float, raw_surplus: float
    ) -> None:
        """Maintain how long surplus has been above or below the floor.

        Two figures, deliberately. What to do is decided from the
        smoothed surplus, because raw grid readings move with every
        kettle. When the below-floor period *started* is taken from the
        raw reading, because the five-minute average is minutes behind
        a real collapse, and the stop delay is counted from this mark:
        anchoring it to the average adds those minutes to the ten, and
        the car imports at up to the charger's ceiling throughout.

        _collapsed_since holds that anchor and doubles as the fast
        path's latch. It is cleared the moment the raw reading comes
        back above the floor, so a kettle that dips the supply for a
        minute leaves nothing behind.
        """
        available = state.surplus_w - state.reserve_w

        if raw_surplus - state.reserve_w >= state.floor_w:
            self._collapsed_since = None
        elif self._collapsed_since is None:
            self._collapsed_since = now

        if available >= state.floor_w and self._collapsed_since is None:
            self._below_since = None
            if self._above_since is None:
                self._above_since = now
        else:
            self._above_since = None
            if self._below_since is None:
                self._below_since = self._collapsed_since or now
```

- [ ] **Step 5: Add the fast path**

Add to `SolarController`:

```python
    async def async_sensor_changed(self) -> None:
        """Note a collapse as soon as it happens.

        What this brings forward is the start of the stop clock, not
        the stop. The stop needs ten minutes below the floor and is
        decided from the smoothed figure, which is minutes behind the
        drop; starting its clock from the drop itself is worth about
        four minutes of avoided import, and is the whole benefit. An
        evaluation is run as well when it is cheap to do so, because
        the collapse may also be the moment a limit becomes too high.

        Rising surplus is not urgent, and is left to the tick: acting
        on every increase would rewrite the limit constantly against a
        charger that takes seconds to apply a change.
        """
        if self._mode is SolarMode.OFF:
            return

        surplus = self._read_surplus()
        if surplus is None:
            return

        now = time.monotonic()
        data = self._coordinator.data or {}
        floor = milliamps_to_watts(min_charging_current(data), data)

        if surplus - self._reserve_w >= floor:
            # Healthy again. Let go of the anchor and re-arm, so the
            # next collapse is counted from itself.
            self._collapsed_since = None
            return

        if self._collapsed_since is not None:
            # This collapse is already being counted. Without this the
            # condition below the floor holds on every sensor update
            # until the average catches up, and a sensor reporting
            # every ten seconds would run six evaluations a minute and
            # spend the hourly command backstop in about three.
            return

        self._collapsed_since = now

        smoothed = self._smoother.value()
        if smoothed is not None and smoothed - self._reserve_w < floor:
            # The average is already below the floor, so the ordinary
            # tick is already treating this as a deficit and the clock
            # is already running. Nothing to bring forward.
            return

        if (
            self._last_evaluation is not None
            and now - self._last_evaluation < TICK_SECONDS
        ):
            _LOGGER.debug(
                "Surplus collapsed to %.0f W; the stop clock starts now, "
                "the evaluation waits for the tick",
                surplus,
            )
            return

        _LOGGER.debug(
            "Surplus collapsed to %.0f W; evaluating without waiting", surplus
        )
        await self.async_tick()
```

The spacing check costs nothing that matters: the collapse is recorded
either way, and that is the part with a deadline. Only the evaluation
waits, by at most one tick.

Register the subscription in `async_start`:

```python
    async def async_start(self) -> None:
        """Begin ticking, and watch the grid sensors for a collapse."""
        # Keep this. async_stop sets the flag to prevent a tick already
        # in flight from re-arming itself, and a controller started
        # again after a stop would otherwise never tick at all.
        self._stopped = False
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
        # When the raw reading fell below the floor and has stayed
        # there. Anchors the stop clock and latches the fast path.
        self._collapsed_since: float | None = None
        self._last_evaluation: float | None = None
        self._evaluating = False
```

Add to `async_stop`, before clearing listeners:

```python
        if self._cancel_listener is not None:
            self._cancel_listener()
            self._cancel_listener = None
```

And add `self._collapsed_since = None` to `disarm` (Task 6), beside the
other clocks it clears. A latch left armed from before the user took
over would anchor the next collapse's stop clock to the old one.

And to the imports:

```python
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
```

Add `async_track_state_change_event=lambda hass, entities, cb: (lambda: None)`
to the `homeassistant.helpers.event` stub in **both**
`tests/test_solar_controller.py` and `tests/test_entities.py`. The
second is not optional: Task 7's select imports `SolarMode` from
`.solar_controller` inside `async_select_option`, so the entity tests
import this module at runtime, and an import line that names a symbol
the stub does not have fails the whole file.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 tests/test_solar_controller.py`
Expected: PASS, `0 failed`, with six more tests than the suite had before
this task. The absolute count is deliberately not stated; see Task 5.

- [ ] **Step 7: Lint and full suite**

Run:
```bash
ruff check custom_components/daze/ tests/
python3 tests/run_all.py
```
Expected: `All checks passed!`, 0 failures.

- [ ] **Step 8: Commit**

```bash
git add custom_components/daze/solar_controller.py tests/test_solar_controller.py
git commit -m "feat: start the stop clock when surplus actually collapses

The ten-minute stop delay was counted from the moment the five-minute
average admitted the drop, several minutes after the drop itself, and
the car imported at up to the charger's ceiling in between. The raw
reading now anchors that clock; the smoothed figure still decides what
to do.

Rising surplus still waits for the tick, and so does most of the work
on a collapse: the fast path fires once per collapse and never more
often than the tick would, because the twenty-command hourly backstop
is a backstop against bugs and has to still be there for the stop.

The tick is no longer re-entrant, now that a sensor event can reach it
while an API call is in flight.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: The supply declaration, and the refusals

**Files:**
- Modify: `custom_components/daze/const.py`
- Modify: `custom_components/daze/config_flow.py` (the `DazeOptionsFlowHandler` class)
- Modify: `custom_components/daze/strings.json`
- Modify: `custom_components/daze/translations/it.json`
- Modify: `custom_components/daze/__init__.py` (one more keyword on the controller)
- Modify: `custom_components/daze/solar_controller.py`
- Modify: `custom_components/daze/select.py` (the solar select)
- Test: `tests/test_solar_controller.py` (append before `_main`, and change `build`)
- Test: `tests/test_entities.py` (the `_solar_select` double from Task 7)

**Interfaces:**
- Consumes: `SolarController` from Task 4, `DazeSolarControlSelect` from Task 7.
- Produces:
  - `CONF_SUPPLY_PHASES = "supply_phases"`, `SUPPLY_PHASES_SINGLE = "single"`,
    `SUPPLY_PHASES_THREE = "three"` in `const.py`, and a third question in
    the options flow. **No default**: an unanswered question is not an
    answer.
  - `SolarController(..., supply_phases=...)`
  - `SolarController.unsupported_reason` property, returning `str | None`
    — the single answer to "can solar control run here, and if not, why
    not". The select uses it for availability and for refusing to arm,
    the tick uses it to stand down, and the README quotes it.

Surviving a restart is **Task 10**. This task is the refusals: three
things the spec requires that nothing yet implements — refusing a supply
the charger cannot follow, refusing one that has not been described at
all, and standing down for the charger's own eco mode and schedules.

**Why the supply is declared rather than detected.** The Daze payload
has exactly one phase field, `evseIsThreePhase`, and `payload.py:308`
already reads it as the *charger's* phase count when deriving the
current floor. Nothing in the payload describes the supply feeding it.
A guard keyed on a field that does not exist would return "supported"
for every installation on earth and pass its own tests, so the question
is asked in the options flow instead. Until it is answered, solar
control refuses to arm: guessing single-phase would let a three-phase
house follow a meter that nets across phases and load the one phase the
charger is on.

(`sensor_catalog.py:243` surfaces `evseIsThreePhase` to users as
"Three-Phase Supply", which contradicts `payload.py`'s reading of the
same field. Not this task's job — renaming an existing entity breaks
dashboards — but it is worth a follow-up, and it is why the option is
named for the *supply* explicitly.)

- [ ] **Step 1: Write the failing tests**

First, `build()` in `tests/test_solar_controller.py` must declare a
supply, or every test in the file stops at the new guard. Change it to:

```python
def build(
    data: dict[str, Any] | None = None,
    supply_phases: str | None = "single",
) -> tuple[Any, Any, Any]:
    """Build a controller wired to stubs.

    Declares a single-phase supply unless a test says otherwise: that
    is the ordinary installation, and the alternatives each have a test
    of their own below.
    """
```

passing `supply_phases=supply_phases` to the constructor.

Then append to `tests/test_solar_controller.py`, before `_main`:

```python
def test_an_undeclared_supply_refuses_to_run() -> None:
    """The Daze payload cannot tell us how many phases feed the house,
    so the user is asked. Until they answer, an unanswered question is
    not evidence of a single-phase supply: guessing wrong loads one
    phase with the whole of a netted three-phase surplus.
    """
    controller, _, _ = build(supply_phases=None)

    assert controller.unsupported_reason is not None
    assert "phase" in controller.unsupported_reason


def test_three_phase_supply_with_a_single_phase_charger_is_refused() -> None:
    """Grid meters usually report net across phases, so the surplus
    can exist mostly on phases the charger cannot reach."""
    data = dict(CHARGING_DATA)
    data["evseIsThreePhase"] = False
    controller, _, _ = build(data, supply_phases="three")

    assert controller.unsupported_reason is not None
    assert "phase" in controller.unsupported_reason


def test_a_matched_single_phase_pair_is_supported() -> None:
    data = dict(CHARGING_DATA)
    data["evseIsThreePhase"] = False
    controller, _, _ = build(data, supply_phases="single")

    assert controller.unsupported_reason is None


def test_a_three_phase_charger_on_a_three_phase_supply_is_supported() -> None:
    """The refusal is about the mismatch, not about three phases."""
    data = dict(CHARGING_DATA)
    data["evseIsThreePhase"] = True
    controller, _, _ = build(data, supply_phases="three")

    assert controller.unsupported_reason is None


def test_eco_mode_refuses_to_arm() -> None:
    """The spec asks for this three times: the charger's own eco mode
    is controlling it, so solar control stands down and says so rather
    than quietly deciding nothing every two minutes for ever.
    """
    data = dict(CHARGING_DATA)
    data["ecoModeEnabled"] = True
    controller, _, _ = build(data)

    assert controller.unsupported_reason is not None
    assert "eco" in controller.unsupported_reason


def test_a_charger_schedule_refuses_to_arm() -> None:
    data = dict(CHARGING_DATA)
    data["schedules"] = [{"id": 1}]
    controller, _, _ = build(data)

    assert controller.unsupported_reason is not None
    assert "schedule" in controller.unsupported_reason
```

`_solar_select` is Task 7's helper in `tests/test_entities.py`. Give its
double an `unsupported_reason` now that the select reads one:

```python
        @property
        def unsupported_reason(self):
            if not self.configured:
                return "no grid sensors have been chosen"
            return None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
python3 tests/test_solar_controller.py
python3 tests/test_entities.py
```
Expected: FAIL with `AttributeError: ... 'unsupported_reason'`, and
`TypeError: build() got an unexpected keyword argument 'supply_phases'`
until Step 1's change to `build` is in place.

- [ ] **Step 3: Add the constants**

Append to `custom_components/daze/const.py`, after the solar block Task 3 added:

```python
# How many phases feed the house. Declared by the user, because the
# Daze payload does not say: its only phase field, evseIsThreePhase,
# describes the charger, and payload.min_charging_current already reads
# it that way. There is deliberately no default — a three-phase meter
# reports surplus netted across phases, and following it with a
# single-phase charger loads the one phase the charger is on.
CONF_SUPPLY_PHASES = "supply_phases"
SUPPLY_PHASES_SINGLE = "single"
SUPPLY_PHASES_THREE = "three"
```

- [ ] **Step 4: Ask the question in the options flow**

In `custom_components/daze/config_flow.py`, add a third field to the
schema Task 3 built in `DazeOptionsFlowHandler.async_step_init`, after
the two sensor pickers:

```python
                # Optional so the form can still be saved without it,
                # not because it has a default: solar control refuses
                # to arm until it is answered.
                vol.Optional(
                    CONF_SUPPLY_PHASES,
                    description={
                        "suggested_value": options.get(CONF_SUPPLY_PHASES)
                    },
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[SUPPLY_PHASES_SINGLE, SUPPLY_PHASES_THREE],
                        translation_key="supply_phases",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
```

Add `CONF_SUPPLY_PHASES`, `SUPPLY_PHASES_SINGLE` and
`SUPPLY_PHASES_THREE` to the existing `from .const import (...)` block.

In `custom_components/daze/strings.json`, add to
`options.step.init.data`:

```json
        "supply_phases": "Grid supply"
```

add a `data_description` beside `data` in the same step:

```json
      "data_description": {
        "supply_phases": "How many phases feed the house, not the charger. A three-phase meter reports surplus added up across all three, and a single-phase charger can only use one of them, so solar control will not arm until this is set."
      }
```

and add a top-level `selector` block beside `options`:

```json
  "selector": {
    "supply_phases": {
      "options": {
        "single": "Single-phase",
        "three": "Three-phase"
      }
    }
  }
```

Do the same in `custom_components/daze/translations/it.json`:
`"supply_phases": "Alimentazione di rete"`, the options
`"single": "Monofase"` and `"three": "Trifase"`, and the description:

```json
      "data_description": {
        "supply_phases": "Quante fasi alimentano la casa, non il caricatore. Un contatore trifase riporta il surplus sommato sulle tre fasi e un caricatore monofase può usarne solo una, quindi il controllo solare non si attiva finché non è impostato."
      }
```

- [ ] **Step 5: Tell the controller what was answered**

In `custom_components/daze/solar_controller.py`, add a keyword to
`SolarController.__init__`, beside the reserve Task 6 added:

```python
        supply_phases: str | None = None,
```

stored as `self._supply_phases = supply_phases`, and documented:

```python
            supply_phases: "single", "three", or None if the user has
                not said. None refuses to arm rather than assuming:
                the payload cannot tell us, and the wrong guess loads
                one phase with a surplus measured across three.
```

In `custom_components/daze/__init__.py`, pass it when the controller is
constructed:

```python
        supply_phases=entry.options.get(CONF_SUPPLY_PHASES),
```

and add `CONF_SUPPLY_PHASES` to the `from .const import (...)` block
there.

- [ ] **Step 6: Implement the one refusal**

Add to `SolarController`:

```python
    @property
    def unsupported_reason(self) -> str | None:
        """Explain why solar control cannot run here, if it cannot.

        One property with one answer, because every caller needs the
        same one: the select for its availability and for refusing to
        arm, the tick to stand down, the log line, and the README. The
        spec asks three separate times for a refusal that explains
        itself, and a boolean cannot.

        Ordered cheapest and most fundamental first, so the message a
        user sees names the thing they have to fix.
        """
        if not self.configured:
            return (
                "both a grid import and a grid export sensor have to be "
                "chosen in the integration's options"
            )

        if self._supply_phases not in (
            SUPPLY_PHASES_SINGLE,
            SUPPLY_PHASES_THREE,
        ):
            return (
                "the number of phases feeding the house has not been set "
                "in the integration's options, and it cannot be read from "
                "the charger"
            )

        data = self._coordinator.data
        if not data:
            return "the charger has not reported yet"

        if self._supply_phases == SUPPLY_PHASES_THREE and not bool(
            data.get("evseIsThreePhase")
        ):
            return (
                "the supply is three-phase and the charger is single-phase, "
                "so exported power may be on a phase it cannot use"
            )

        if data.get("ecoModeEnabled"):
            return "the charger's own eco mode is controlling it"

        if data.get("schedules"):
            return "the charger has a schedule set"

        return None
```

Add `SUPPLY_PHASES_SINGLE` and `SUPPLY_PHASES_THREE` to the
`from .const import (...)` block in `solar_controller.py`, creating it
if the module does not import from `const` yet.

An absent payload is a refusal, not a pass. `self._coordinator.data`
is empty before the first successful poll, and reading that as "no
phase mismatch, no eco mode, no schedule" would arm solar control on
the strength of knowing nothing — the same mistake `_build_state`
already avoids for `charger_reachable`.

In `_async_evaluate`, immediately after the `SolarMode.OFF` check:

```python
        unsupported = self.unsupported_reason
        if unsupported is not None:
            if not self._unsupported_warning_logged:
                self._unsupported_warning_logged = True
                _LOGGER.warning("Solar control cannot run: %s", unsupported)
            return

        self._unsupported_warning_logged = False
```

with `self._unsupported_warning_logged = False` added to `__init__`.

Its own flag, not `_sensor_warning_logged`. The two conditions are
unrelated, and the reset that clears the sensor flag sits below this
guard, where an unsupported setup never reaches it: sharing one flag
means whichever warned first silences the other for the lifetime of
the entry.

- [ ] **Step 7: Log the rate limit at the level the spec asks for**

The spec's error table says the rate limit is logged at **warning** and
everything else at debug; every `nothing` decision currently goes to
debug, the rate limit included, so the one condition a user needs to
know about is the one they cannot see. In `_async_evaluate`, replace
the `NOTHING` branch's log line:

```python
        if decision.action is SolarAction.NOTHING:
            if state.commands_this_hour >= MAX_COMMANDS_PER_HOUR:
                # The backstop is against bugs. If it is what is
                # holding the charger back, something upstream is
                # wrong and the log has to say so out loud.
                _LOGGER.warning("Solar control: %s", decision.reason)
            else:
                _LOGGER.debug("Solar control: %s", decision.reason)
            self._notify()
            return
```

Add `MAX_COMMANDS_PER_HOUR` to the `from .solar import (...)` block.

- [ ] **Step 8: Make the select read the one property**

In `custom_components/daze/select.py`, replace `available`, and the
refusal Task 7 put in `async_select_option`, with the one property.
Both asked a narrower question — "are the sensors set?" — and the
answer is now "is there any reason this cannot run?":

```python
    @property
    def available(self) -> bool:
        """Usable only where solar control could actually run."""
        return self._controller.unsupported_reason is None
```

```python
    async def async_select_option(self, option: str) -> None:
        """Set the mode, refusing to arm where it cannot work.

        `available` is a hint for the dashboard. A service call or an
        automation arrives here whatever the entity reports, so the
        refusal has to be enforced in the method that acts — and
        raised, not logged, because the caller asked for something and
        is entitled to know it did not happen, and why.
        """
        from .solar_controller import SolarMode

        if option != "off":
            reason = self._controller.unsupported_reason
            if reason is not None:
                raise HomeAssistantError(
                    f"Solar control cannot be armed: {reason}."
                )

        self._controller.mode = SolarMode(option)
        self.async_write_ha_state()
```

Turning it **off** is never refused. A control that cannot be switched
off because the charger is in eco mode would be worse than the problem.

- [ ] **Step 9: Run the tests to verify they pass**

Run:
```bash
python3 tests/test_solar_controller.py
python3 tests/test_entities.py
```
Expected: PASS, `0 failed`, with six more tests than the two files had
before this task. The absolute count is deliberately not stated; see
Task 5.

One existing test now passes for a reason other than the one it is
named after: `test_unknown_charging_status_does_not_assume_zero_draw`
runs a full tick with `coordinator.data = {}`, which from this task on
stops at "the charger has not reported yet" before it ever reads a
sensor. Its assertion still holds, so the suite stays green. **Task 10
moves it down a layer**; leave it alone here rather than half-fixing it
in two places.

- [ ] **Step 10: Lint and full suite**

Run:
```bash
ruff check custom_components/daze/ tests/
python3 tests/run_all.py
```
Expected: `All checks passed!`, 0 failures.

- [ ] **Step 11: Commit**

```bash
git add custom_components/daze/const.py custom_components/daze/config_flow.py custom_components/daze/strings.json custom_components/daze/translations/it.json custom_components/daze/__init__.py custom_components/daze/solar_controller.py custom_components/daze/select.py tests/test_solar_controller.py tests/test_entities.py
git commit -m "feat: refuse setups solar control cannot follow

A three-phase supply feeding a single-phase charger reports surplus
netted across phases, most of which the charger cannot reach. The Daze
payload does not say how many phases feed the house — its one phase
field describes the charger — so the options flow asks, with no
default, and solar control will not arm until it is answered.

One property now answers 'can this run, and if not, why not', for the
select's availability, its refusal to arm, and the log line. Eco mode
and a configured charger schedule are part of that answer, as the spec
asks; previously they produced a decision of 'nothing' logged at debug
and no other sign. Availability alone was never enough: a service call
reaches async_select_option whatever the entity reports.

The rate limit is logged at warning rather than debug. It is a backstop
against bugs, so if it is what is holding the charger back, that is not
a debug-level fact.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: Surviving a restart

**Files:**
- Modify: `custom_components/daze/solar_controller.py`
- Modify: `custom_components/daze/select.py` (the solar select)
- Test: `tests/test_solar_controller.py` (append before `_main`)
- Test: `tests/test_entities.py` (append before `_main`)

**Interfaces:**
- Consumes: `SolarController` from Task 4, `DazeSolarControlSelect` from
  Task 7, `unsupported_reason` from Task 9.
- Produces: no new public surface. `SolarController` gains one private
  attribute, `_charge_seeded`, and the select inherits `RestoreEntity`.

On a Home Assistant restart the controller's timers begin at zero. If
the car was already charging, an unelapsed minimum run time reads as a
charge that has only just begun, and the select comes back `off`
whatever the user had chosen. Both are silent: the car simply stops
following the sun, and nothing says why.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_solar_controller.py`, before `_main`. Add
`import time` to the file's imports if it is not already there — the
first test reads `time.monotonic()`:

```python
def test_a_charge_already_running_counts_as_having_run() -> None:
    """Timers start at zero after a restart. Without seeding, an
    unelapsed minimum run time could stop a healthy charge moments
    after boot."""
    controller, _, _ = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    asyncio.run(controller.async_tick())

    # Assert how far back the mark was seeded, not merely that one exists.
    # Seeding it to the present moment would satisfy "is not None" while
    # leaving the charge unstoppable for the next ten minutes, which is the
    # bug this seeding exists to prevent.
    assert controller._started_at is not None
    elapsed = time.monotonic() - controller._started_at
    assert elapsed >= solar.MIN_RUN_SECONDS, (
        "a charge already running must count as having served its minimum "
        f"run time, but the mark was seeded only {elapsed:.0f}s back"
    )


def test_a_stop_that_could_not_be_sent_is_not_re_issued_every_tick() -> None:
    """_carry_out clears the minimum-run clock after a stop it sent,
    and "sent" includes one only queued for the background retry —
    where the charger is still charging. Seeding that clock again on
    the next tick makes decide() return STOP again, and again every
    two minutes, until the hourly backstop trips forty minutes later.
    Handing a stuck link to the background retry and leaving it there
    is the spec's own rule; this is why the seeding is once per charge
    and not once per tick.
    """
    controller, coordinator, hass = build()
    controller.mode = controller_module.SolarMode.ACTIVE

    clock = [24_000_000.0]
    original_monotonic = controller_module.time.monotonic
    controller_module.time.monotonic = lambda: clock[0]
    try:
        controller._started_at = clock[0] - solar.MIN_RUN_SECONDS - 1
        hass.states.set(
            "sensor.grid_import", "3000", {"unit_of_measurement": "W"}
        )
        hass.states.set(
            "sensor.grid_export", "0", {"unit_of_measurement": "W"}
        )
        asyncio.run(controller.async_tick())  # starts the below-floor timer

        clock[0] += solar.STOP_DELAY_SECONDS + 1

        async def _rpc_failure(serial: str, attempts: int = 8) -> dict:
            coordinator.api_client.calls.append(("stop", serial))
            raise api_module.ApiCommandRejectedError(
                "unreachable", code=api_module.COMMAND_ERROR_CODE_RPC_FAILURE
            )

        coordinator.api_client.async_stop_charge = _rpc_failure
        asyncio.run(controller.async_tick())

        for _ in range(3):
            clock[0] += solar.TICK_SECONDS
            asyncio.run(controller.async_tick())
    finally:
        controller_module.time.monotonic = original_monotonic

    stops = len(
        [call for call in coordinator.api_client.calls if call[0] == "stop"]
    )
    assert stops == 1, f"the queued stop was re-issued: {stops} attempts"


def test_a_charge_that_starts_later_is_seeded_in_its_turn() -> None:
    """Once per charging episode, not once per lifetime.

    A charge the user starts by hand an hour from now has also been
    running longer than we have been watching it. If the flag never
    reset, that charge's minimum-run clock would read as zero for ever
    and solar control could never stop it — the mirror image of the
    bug the seeding exists to fix.
    """
    controller, coordinator, _ = build(NOT_CHARGING_DATA)
    controller.mode = controller_module.SolarMode.SIMULATE

    asyncio.run(controller.async_tick())
    assert controller._started_at is None

    coordinator.data = dict(CHARGING_DATA)
    asyncio.run(controller.async_tick())

    assert controller._started_at is not None
```

And append to `tests/test_entities.py`, before `_main`:

```python
def test_the_solar_select_restores_its_mode() -> None:
    """The spec asks for restoration across a restart by name.

    Without it every Home Assistant restart silently disarms solar
    control: the select comes back "off", the car stops following the
    sun, and nothing says so.
    """
    entity, controller = _solar_select(configured=True)

    class LastState:
        state = "active"

    async def _last_state() -> Any:
        return LastState()

    entity.async_get_last_state = _last_state

    asyncio.run(entity.async_added_to_hass())

    assert controller.mode is not None
    assert controller.mode.value == "active"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
python3 tests/test_solar_controller.py
python3 tests/test_entities.py
```
Expected: the three controller tests fail on `_started_at` being `None`
or the stop being re-issued; the select test fails with
`AttributeError: ... 'async_get_last_state'` until `RestoreEntity` is
inherited.

- [ ] **Step 3: Seed the minimum-run clock, once per charge**

Put this immediately after `self._track_thresholds(state, now, surplus)`
and **outside** the `if self._mode is SolarMode.ACTIVE:` block that
wraps the `self._check_ignored_start(...)` call. Seeding must happen in
every mode: a `simulate` dry run of a charge that is already running has
to preview the stop, and under the `ACTIVE` gate it would instead report
"the minimum run time has not elapsed" forever:

```python
        # Timers begin at zero after a restart. A charge that is
        # already running has, by definition, been running: without
        # this the minimum run time reads as unelapsed and a healthy
        # charge could be stopped moments after boot.
        #
        # Once per charging episode, not once per tick. _carry_out
        # clears the minimum-run clock after a stop it *sent*, and
        # "sent" includes one only queued for the background retry —
        # where the charger is still charging. Re-seeding on the next
        # tick would put the clock back, decide() would return STOP
        # again, and it would do so every two minutes until the hourly
        # backstop tripped forty minutes later. A stop that will not
        # land is the background retry's business, and the spec says
        # so: "hand to the existing background retry; do not retry
        # here."
        #
        # The flag resets when the charge is observed to end, so the
        # next one — including a charge the user starts by hand — is
        # seeded in its turn. A flag that only ever set once would
        # leave that later charge with a zero minimum-run clock for
        # ever, and solar control could never stop it.
        #
        # This seeds the minimum-run clock only. The draw-grace clock
        # is a separate attribute, set solely when this controller
        # issues a start of its own, and it must stay unset here: a
        # charge that was already running was never ours to judge, and
        # a charger sitting in waiting_for_ev at 0 W at boot would
        # otherwise arm an hour-long back-off on a healthy charge.
        if not state.charging:
            self._charge_seeded = False
        elif not self._charge_seeded:
            self._charge_seeded = True
            if self._started_at is None:
                self._started_at = now - MIN_RUN_SECONDS
```

Add `self._charge_seeded = False` to `__init__`, and `MIN_RUN_SECONDS`
to the `from .solar import (...)` block.

The inner `if self._started_at is None` is what protects a start this
controller issued: `_carry_out` has already set the real time, and this
must not overwrite it with one ten minutes in the past.

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

Restoring writes straight to the controller rather than through
`async_select_option`, so it cannot raise at startup: a setup that is
temporarily unsupported — the charger has not polled yet, say — must
come back as the user left it and be refused later by the guard in the
tick, not lose the setting because of a race with the first refresh.

- [ ] **Step 5: Move one test back to the layer it tests**

`test_unknown_charging_status_does_not_assume_zero_draw` runs a full
tick with `coordinator.data = {}`. Since Task 9 that tick stops at "the
charger has not reported yet", before it ever reads a sensor, so the
test passes for a reason that has nothing to do with the unknown car
draw it is named after. Call `controller._read_surplus()` directly and
assert that it returns `None`, the same way
`test_no_coordinator_data_reads_as_not_reachable` already tests its own
layer.

- [ ] **Step 6: Run the tests to verify they pass**

Run:
```bash
python3 tests/test_solar_controller.py
python3 tests/test_entities.py
```
Expected: PASS, `0 failed`, with four more tests than the two files had
before this task. The absolute count is deliberately not stated; see
Task 5.

- [ ] **Step 7: Lint and full suite**

Run:
```bash
ruff check custom_components/daze/ tests/
python3 tests/run_all.py
```
Expected: `All checks passed!`, 0 failures.

- [ ] **Step 8: Commit**

```bash
git add custom_components/daze/solar_controller.py custom_components/daze/select.py tests/test_solar_controller.py tests/test_entities.py
git commit -m "feat: survive a Home Assistant restart

Timers begin at zero after a restart, so a charge that was already
running would read as having no elapsed run time and could be stopped
moments after boot. A running charge now seeds its own start time.

Once per charge rather than once per tick, and released when the charge
is observed to end. Re-seeding on every tick would re-issue a stop that
was queued but never landed, every two minutes until the hourly
backstop tripped; seeding only once per lifetime would strand the next
charge instead, with a clock that reads as zero for ever and can never
be stopped. This clock has now been wrong in both directions, which is
why it is tested in both.

The control also remembers its mode across a restart.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---
### Task 11: Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/solar-surplus-charging.md`
- Modify: `custom_components/daze/manifest.json`

**Interfaces:**
- Consumes: the entity names from Task 7, the refusals from Task 9, and
  the restored mode from Task 10.
- Produces: nothing code depends on. This is the last task.

- [ ] **Step 1: Document the entities in the README**

In `README.md`, add to the Controls table:

```markdown
| Select | `select.daze_solar_control` | Solar control | `off` / `simulate` / `active` |
| Number | `number.daze_solar_reserve` | Solar reserve | Watts to leave for the house before the car gets any |
```

And to the Sensors table:

```markdown
| `sensor.daze_solar_surplus` | Solar surplus | `power` | `measurement` | W |
```

Every other row in those tables uses the bare `daze_` prefix —
`number.daze_max_charging_current`, `select.daze_operation_mode` — and
these must match, or an automation copied out of the README addresses
an entity that does not exist. Confirm the real object IDs on a live
install before publishing: the prefix follows the device name, and a
renamed device changes it.

- [ ] **Step 2: Add a Solar control section to the README**

Insert before `## Automation Examples`:

```markdown
## Solar control

Charges the car from what the house would otherwise export, adjusting
the limit as production and load change, and stopping when there is not
enough surplus to charge at all.

1. In the integration's options, pick your **grid import** and **grid
   export** power sensors, and say whether your **grid supply** is
   single-phase or three-phase.
2. Set **Solar control** to `simulate`. It decides and logs but sends
   nothing.
3. Leave it for a day. The select's attributes show the surplus it sees
   and what it would have done.
4. If the decisions look right, set it to `active`.

It never imports to charge: the charger cannot run below 1500 W, so
when surplus falls below that it stops rather than topping up from the
grid.

Changing the charging limit yourself — from the dashboard, or from your
own automation — turns solar control off. Starting or stopping the
charge by hand does the same. It does not fight you.

### When the control is unavailable

Solar control refuses to arm rather than guess, and says why in the
log (`Solar control cannot run: …`). It is unavailable when:

- **Both grid sensors are not set.** It has nothing to measure.
- **The grid supply has not been declared.** The charger cannot tell
  the integration how many phases feed the house, so you have to. A
  three-phase meter reports surplus added up across all three phases;
  a single-phase charger can only use one of them, so following that
  figure would load one phase with all three phases' surplus. For the
  same reason, a **three-phase supply with a single-phase charger is
  refused outright** — see the YAML guide below if that is your setup.
- **The charger's own eco mode is on, or it has a schedule set.**
  Something else is already deciding when the car charges, and two
  controllers fighting over one charger is worse than either alone.

### The reserve

**Solar reserve** is watts to leave for the house before the car gets
any: set it to 500 and the car is only offered surplus above 500 W. It
is saved with the integration's settings and survives a restart.

For a version you build and tune yourself, see
[docs/solar-surplus-charging.md](docs/solar-surplus-charging.md).
```

- [ ] **Step 3: Bump the version**

Modify `custom_components/daze/manifest.json`, setting `"version"` to
`"0.2.0"`. This is the only task that touches it, and Step 5 commits it
— an edit left in the working tree would ship a release announcing a
version the manifest does not carry.

Run: `python3 -c "import json; print(json.load(open('custom_components/daze/manifest.json'))['version'])"`
Expected: `0.2.0`

- [ ] **Step 4: Cross-reference from the YAML guide**

At the top of `docs/solar-surplus-charging.md`, after the first paragraph, add:

```markdown
> The integration can now do this itself — see **Solar control** in the
> README. This guide remains for setups the built-in version does not
> fit: a house battery to arbitrate with, tariff windows, or anything
> needing logic of your own.
```

- [ ] **Step 5: Verify and commit**

Run: `python3 tests/run_all.py`
Expected: 0 failures.

```bash
git add README.md docs/solar-surplus-charging.md custom_components/daze/manifest.json
git commit -m "docs: document solar control

Includes the simulate-first procedure, because a feature that starts
and stops the car should be watched for a day before it is trusted,
and what makes the control unavailable, because a feature that refuses
to arm has to say why somewhere a user will look.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

This is the last step of the plan. Do not push, tag or force-push:
this work is on the `solar-control` branch, `main` is several tasks
behind it, and publishing is the operator's call once the branch has
been reviewed and merged. A push from here would move the release tag
onto a `main` that does not contain the feature.

---

## After the plan

Solar control ships **off**. Nothing changes for an existing install
until the user picks two sensors, declares their supply, and moves the
select.

The first real validation is a day in `simulate` against actual
production. That is the step this plan cannot do, and the one that
decides whether the constants in `solar.py` are right for the site.

Then the operator reviews the branch, merges it, and moves the release
tag. No task does that.

Two things are deliberately left undone and are worth a follow-up:

- `sensor_catalog.py:243` labels `evseIsThreePhase` "Three-Phase
  Supply", while `payload.py:308` reads the same field as the
  *charger's* phase count. One of the two is wrong. Renaming the
  entity breaks existing dashboards, so it is not folded into this
  work; Task 9's option is named for the supply explicitly to avoid
  inheriting the confusion.
- A stop that is queued for the background retry and never lands
  leaves the charge running with solar control unable to re-issue it
  until the charge ends by other means. That is the spec's rule ("hand
  to the existing background retry; do not retry here") working as
  written, and re-issuing every tick is worse, but neither is
  obviously right and the case deserves its own decision.
