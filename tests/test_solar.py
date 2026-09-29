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
