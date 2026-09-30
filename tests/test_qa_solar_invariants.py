"""Invariants that must hold for solar surplus control on any installation.

test_solar.py pins each branch of `decide()` against one hand-picked
state per branch, in table order. It is the right tool for "does this
branch exist and return the right action", but a single state per
branch cannot tell us whether the *bound* is right for every floor,
ceiling, reserve and surplus a real installation can produce - a
three-phase supply, a small consumer unit, a car parked overnight at
near-zero surplus. Those are exactly the shapes that made the payload
bounds wrong in ways one captured response never revealed, which is
why tests/test_qa_invariants.py exists for payload.py. This module is
the same discipline applied to solar.py: sweep the input space and
assert the properties that must hold everywhere, not more examples.

`solar.py` has no Home Assistant coupling, so `decide()`,
`target_watts()`, `available_watts()`, `compute_surplus()` and
`SurplusSmoother` are swept directly. One property - whether an
undeclared electrical supply blocks control - lives in
`solar_controller.py` instead (the phase guard reads the coordinator's
data, not anything `SolarState` carries), so that one test loads the
controller module too, the same way tests/test_solar_controller.py
does, stubbing only the Home Assistant imports needed to import it.

Run with pytest, or standalone:

    python3 tests/test_qa_solar_invariants.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import random
import sys
import types
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


solar = _load("daze_solar_qa", "solar.py")


def state(**overrides: Any) -> Any:
    """Build a healthy SolarState baseline; each test overrides what it
    is about and leaves the rest alone.

    Mirrors test_solar.py's own `state()`, deliberately: the two suites
    should read as the same fixture used two different ways.
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
# Sweep ranges, shared across the tests below.
# ------------------------------------------------------------------

# A slider's floor can never legitimately exceed its ceiling (that
# invariant is what tests/test_qa_invariants.py already proves for
# payload.py), so only pairs respecting that are swept here.
_FLOORS = (0, 800, 1400, 1600, 2500, 4000)
_CEILINGS = (1600, 3000, 4600, 7400, 11000, 22000)
FLOOR_CEILING_PAIRS: tuple[tuple[int, int], ...] = tuple(
    (floor, ceiling)
    for floor in _FLOORS
    for ceiling in _CEILINGS
    if floor <= ceiling
)

# From a near-empty overnight trickle to a heavily overcast import and
# a large three-phase installation's midday peak.
SURPLUS_SWEEP: tuple[int, ...] = tuple(range(-2000, 24001, 2000))
RESERVE_SWEEP: tuple[int, ...] = (0, 500, 1500, 3000)


# ------------------------------------------------------------------
# The target is always within bounds.
# ------------------------------------------------------------------


def test_target_within_bounds_whenever_a_command_is_issued() -> None:
    """A target below the floor or above the ceiling is a command the
    charger rejects. Sweeps every floor/ceiling pair against a wide
    surplus and reserve range, on both the charging (SET) and the
    not-charging (START) branch, and checks every SET or START
    decision's target against its own bounds.

    current_limit_w is pinned to 0 for the charging branch so the
    change from any positive target clears the deadband and a SET is
    not merely possible but likely across the sweep - this is what
    makes the "checked_set" counter non-trivial rather than a loop
    whose body never actually reaches a SET.
    """
    checked_set = 0
    checked_start = 0
    for floor, ceiling in FLOOR_CEILING_PAIRS:
        for reserve in RESERVE_SWEEP:
            for surplus in SURPLUS_SWEEP:
                base = {
                    "surplus_w": surplus,
                    "reserve_w": reserve,
                    "floor_w": floor,
                    "ceiling_w": ceiling,
                }

                charging_decision = solar.decide(
                    state(
                        charging=True,
                        current_limit_w=0,
                        seconds_since_start=solar.MIN_RUN_SECONDS + 1,
                        seconds_below_threshold=0,
                        **base,
                    )
                )
                if charging_decision.action in (
                    solar.SolarAction.SET,
                    solar.SolarAction.START,
                ):
                    checked_set += 1
                    target = charging_decision.target_watts
                    assert target is not None
                    assert floor <= target <= ceiling, (
                        floor, ceiling, surplus, reserve, charging_decision,
                    )

                start_decision = solar.decide(
                    state(
                        charging=False,
                        car_connected=True,
                        seconds_above_threshold=solar.START_DELAY_SECONDS,
                        **base,
                    )
                )
                if start_decision.action in (
                    solar.SolarAction.SET,
                    solar.SolarAction.START,
                ):
                    checked_start += 1
                    target = start_decision.target_watts
                    assert target is not None
                    assert floor <= target <= ceiling, (
                        floor, ceiling, surplus, reserve, start_decision,
                    )

    total_cases = len(FLOOR_CEILING_PAIRS) * len(RESERVE_SWEEP) * len(SURPLUS_SWEEP)
    assert total_cases > 500, "the sweep is not exercising enough of the space"
    assert checked_set > 0, "no SET/START was ever reached on the charging branch"
    assert checked_start > 0, "no SET/START was ever reached on the start branch"


# ------------------------------------------------------------------
# The reserve is always honoured; monotonicity in both directions.
# ------------------------------------------------------------------


def test_raising_the_reserve_never_raises_the_target() -> None:
    """The house's share can only ever grow the amount withheld from
    the car, never shrink it. Sweeps floor/ceiling pairs and surpluses,
    and for each one walks the reserve upward, checking the resulting
    target sequence is never increasing.
    """
    reserves = sorted(RESERVE_SWEEP)
    sequences_checked = 0
    for floor, ceiling in FLOOR_CEILING_PAIRS:
        for surplus in SURPLUS_SWEEP:
            targets = [
                solar.target_watts(
                    state(
                        surplus_w=surplus,
                        reserve_w=reserve,
                        floor_w=floor,
                        ceiling_w=ceiling,
                    )
                )
                for reserve in reserves
            ]
            sequences_checked += 1
            for earlier, later in itertools.pairwise(targets):
                assert later <= earlier, (
                    floor, ceiling, surplus, reserves, targets,
                )

    assert sequences_checked == len(FLOOR_CEILING_PAIRS) * len(SURPLUS_SWEEP)


def test_more_surplus_never_lowers_the_target() -> None:
    """More available power for the car cannot produce a lower
    request. Sweeps floor/ceiling pairs and reserves, and for each one
    walks the surplus upward, checking the resulting target sequence
    is never decreasing.
    """
    surpluses = sorted(SURPLUS_SWEEP)
    sequences_checked = 0
    for floor, ceiling in FLOOR_CEILING_PAIRS:
        for reserve in RESERVE_SWEEP:
            targets = [
                solar.target_watts(
                    state(
                        surplus_w=surplus,
                        reserve_w=reserve,
                        floor_w=floor,
                        ceiling_w=ceiling,
                    )
                )
                for surplus in surpluses
            ]
            sequences_checked += 1
            for earlier, later in itertools.pairwise(targets):
                assert later >= earlier, (
                    floor, ceiling, reserve, surpluses, targets,
                )

    assert sequences_checked == len(FLOOR_CEILING_PAIRS) * len(RESERVE_SWEEP)


# ------------------------------------------------------------------
# No command without information: every guard, every combination.
# ------------------------------------------------------------------

# The five guards decide() checks unconditionally, in this order, before
# anything else runs. commands_this_hour is represented by whether it
# has reached the cap, not its exact value.
_GUARD_NAMES = (
    "charger_reachable",
    "command_pending",
    "eco_mode_on",
    "schedule_set",
    "commands_this_hour",
)


def _guarded_state(active_guards: frozenset[str], **base: Any) -> Any:
    """Build a state where exactly `active_guards` are tripped."""
    overrides = dict(base)
    overrides["charger_reachable"] = "charger_reachable" not in active_guards
    overrides["command_pending"] = "command_pending" in active_guards
    overrides["eco_mode_on"] = "eco_mode_on" in active_guards
    overrides["schedule_set"] = "schedule_set" in active_guards
    overrides["commands_this_hour"] = (
        solar.MAX_COMMANDS_PER_HOUR if "commands_this_hour" in active_guards else 0
    )
    return state(**overrides)


def test_no_command_without_information_across_every_guard_combination() -> None:
    """decide() must return NOTHING whenever any one of these guards
    holds, no matter how good the surplus looks or whether the car is
    already charging. This is the spec's governing principle (rules.md
    S5), and the point of a QA suite is to prove it across the space
    rather than sample it.

    Sweeps the full powerset of the five unconditional guards (32
    combinations) against both charging states and three generous
    surplus levels: 192 cases, of which every one with at least one
    guard active must come back NOTHING.
    """
    surpluses = (2000, 6000, 50000)
    cases = 0
    guarded_cases = 0
    for size in range(len(_GUARD_NAMES) + 1):
        for combo in itertools.combinations(_GUARD_NAMES, size):
            active = frozenset(combo)
            for charging in (True, False):
                for surplus in surpluses:
                    cases += 1
                    decision = solar.decide(
                        _guarded_state(
                            active,
                            surplus_w=surplus,
                            charging=charging,
                            current_limit_w=0,
                            seconds_since_start=solar.MIN_RUN_SECONDS + 1,
                            seconds_above_threshold=solar.START_DELAY_SECONDS,
                        )
                    )
                    if active:
                        guarded_cases += 1
                        assert decision.action is solar.SolarAction.NOTHING, (
                            active, charging, surplus, decision,
                        )

    expected_cases = 2 ** len(_GUARD_NAMES) * 2 * len(surpluses)
    assert cases == expected_cases
    assert guarded_cases == (2 ** len(_GUARD_NAMES) - 1) * 2 * len(surpluses)


def test_backoff_blocks_a_restart_regardless_of_surplus() -> None:
    """The sixth guard, checked only while not charging: a car the
    controller started and that never drew must not be restarted, no
    matter how large the surplus grows while the back-off runs.

    Also confirms the guard's own conditionality is real and not an
    artefact of this sweep never trying the other branch: the same
    back-off value must not suppress a SET on a car that is already
    charging, since the guard exists to stop *restarting* an idle
    charger, not to stop an active one.
    """
    surpluses = (1700, 4000, 9000, 50000)
    backoffs = (1, 60, 1800, 3600)
    cases = 0
    for surplus in surpluses:
        for remaining in backoffs:
            cases += 1
            decision = solar.decide(
                state(
                    charging=False,
                    car_connected=True,
                    surplus_w=surplus,
                    backoff_remaining_s=remaining,
                    seconds_above_threshold=solar.START_DELAY_SECONDS,
                )
            )
            assert decision.action is solar.SolarAction.NOTHING
            assert "backing off" in decision.reason

    assert cases == len(surpluses) * len(backoffs)

    charging_decision = solar.decide(
        state(
            charging=True,
            surplus_w=9000,
            current_limit_w=0,
            backoff_remaining_s=3600,
            seconds_since_start=solar.MIN_RUN_SECONDS + 1,
        )
    )
    assert charging_decision.action is solar.SolarAction.SET


# ------------------------------------------------------------------
# Rule ordering is stable.
# ------------------------------------------------------------------


def test_guard_priority_is_a_strict_total_order() -> None:
    """decide() lists its guards in a specific order - reachability,
    pending command, eco mode, schedule, hourly rate limit - and that
    order is load-bearing: a guard that fires late is the same as a
    guard that does not exist.

    Builds a state with every later guard active and switches on
    earlier ones one at a time, checking at each step that the reason
    names the earliest active guard rather than a later one that could
    also apply. Repeated across both charging states and two surplus
    levels so the order is not merely a property of one fixture.
    """
    # (field, active value, inactive value, substring expected in the
    # reason when this is the earliest active guard)
    layers = (
        ("charger_reachable", False, True, "reachable"),
        ("command_pending", True, False, "pending"),
        ("eco_mode_on", True, False, "eco"),
        ("schedule_set", True, False, "schedule"),
        (
            "commands_this_hour",
            solar.MAX_COMMANDS_PER_HOUR,
            0,
            "rate limit",
        ),
    )

    checks = 0
    for charging in (True, False):
        for surplus in (2000, 9000):
            for earliest in range(len(layers)):
                overrides = {
                    name: (inactive if index < earliest else active)
                    for index, (name, active, inactive, _) in enumerate(layers)
                }
                decision = solar.decide(
                    state(
                        charging=charging,
                        surplus_w=surplus,
                        current_limit_w=0,
                        seconds_since_start=solar.MIN_RUN_SECONDS + 1,
                        seconds_above_threshold=solar.START_DELAY_SECONDS,
                        **overrides,
                    )
                )
                checks += 1
                assert decision.action is solar.SolarAction.NOTHING
                expected = layers[earliest][3]
                assert expected in decision.reason.lower(), (
                    charging, surplus, earliest, decision.reason,
                )

    assert checks == 2 * 2 * len(layers)


# ------------------------------------------------------------------
# The deadband is respected in both directions.
# ------------------------------------------------------------------


def test_deadband_blocks_small_changes_in_either_direction() -> None:
    """No SET for a change smaller than DEADBAND_W, whichever way the
    surplus moved relative to the current limit; a SET must fire once
    the gap reaches the deadband, in either direction.
    """
    floor, ceiling = 1600, 20000
    deadband = solar.DEADBAND_W

    inside_band = 0
    for target in range(floor, ceiling + 1, 200):
        for delta in range(-(deadband - 1), deadband, 137):
            current = target - delta
            if not (floor <= current <= ceiling):
                continue
            decision = solar.decide(
                state(
                    charging=True,
                    surplus_w=target,
                    current_limit_w=current,
                    floor_w=floor,
                    ceiling_w=ceiling,
                    seconds_below_threshold=0,
                    seconds_since_start=solar.MIN_RUN_SECONDS + 1,
                )
            )
            inside_band += 1
            assert decision.action is not solar.SolarAction.SET, (
                target, current, delta, decision,
            )

    at_or_past_boundary = 0
    fixed_target = floor + 3000
    for delta in (deadband, deadband + 500, -deadband, -(deadband + 500)):
        current = fixed_target - delta
        if not (floor <= current <= ceiling):
            continue
        decision = solar.decide(
            state(
                charging=True,
                surplus_w=fixed_target,
                current_limit_w=current,
                floor_w=floor,
                ceiling_w=ceiling,
                seconds_below_threshold=0,
                seconds_since_start=solar.MIN_RUN_SECONDS + 1,
            )
        )
        at_or_past_boundary += 1
        assert decision.action is solar.SolarAction.SET, (
            fixed_target, current, delta, decision,
        )

    assert inside_band > 50
    assert at_or_past_boundary == 4


# ------------------------------------------------------------------
# The smoother.
# ------------------------------------------------------------------


def test_smoother_output_is_always_within_its_window_range() -> None:
    """A moving average can never report a value outside the range of
    what it was actually fed. Sweeps several window sizes and a long,
    seeded-random sequence of readings, checking after every add() that
    the smoother's value lies within [min, max] of whatever is
    currently inside its own window.
    """
    rng = random.Random(20260929)
    windows = (60.0, 300.0, 900.0)
    checks = 0
    for window in windows:
        smoother = solar.SurplusSmoother(window_seconds=window)
        now = 0.0
        history: list[tuple[float, float]] = []
        for _ in range(200):
            now += rng.uniform(1, 50)
            value = rng.uniform(-500.0, 12000.0)
            smoother.add(value, now=now)
            history.append((now, value))

            cutoff = now - window
            in_window = [v for t, v in history if t >= cutoff]
            result = smoother.value()
            checks += 1
            assert result is not None
            assert min(in_window) - 1e-9 <= result <= max(in_window) + 1e-9, (
                window, now, in_window, result,
            )

    assert checks == len(windows) * 200


def test_smoother_evicts_exactly_at_the_window_boundary() -> None:
    """A sample exactly `window_seconds` old is still inside the
    window; one a moment older is not. The cutoff comparison has to be
    `>=`, not `>`, right at the instant that changes - the one place a
    boundary bug hides from a sweep that never lands exactly on it.
    """
    window = 300.0

    still_included = solar.SurplusSmoother(window_seconds=window)
    still_included.add(1000.0, now=0.0)
    still_included.add(0.0, now=window)
    assert still_included.value() == 500.0, "the sample at exactly the window's age was evicted early"

    just_evicted = solar.SurplusSmoother(window_seconds=window)
    just_evicted.add(1000.0, now=0.0)
    just_evicted.add(0.0, now=window + 0.001)
    assert just_evicted.value() == 0.0, "a sample older than the window survived"


# ------------------------------------------------------------------
# compute_surplus.
# ------------------------------------------------------------------


def test_compute_surplus_never_negative() -> None:
    """max(0, ...) must hold everywhere, including a heavy import with
    no car drawing at all."""
    draws = range(0, 12001, 1000)
    exports = range(0, 12001, 1000)
    imports = range(0, 20001, 1000)
    cases = 0
    for draw in draws:
        for export in exports:
            for imp in imports:
                cases += 1
                assert solar.compute_surplus(draw, export, imp) >= 0

    assert cases > 1000


def test_car_draw_makes_self_consumption_visible() -> None:
    """A charge entirely consumed by the house itself - the car
    drawing exactly what the house imports, nothing exported - is not
    zero surplus. It is surplus already spent, and adding the car's own
    draw back is what makes that visible. Sweeps the draw rather than
    asserting one figure, and compares each case against what the
    naive (no-add-back) computation would have said instead.
    """
    cases = 0
    for draw in range(200, 10001, 200):
        # Production exactly meets the load: nothing exported, nothing
        # imported. Perfect self-consumption, invisible to anything
        # that only looks at the grid meters.
        visible = solar.compute_surplus(car_draw_w=draw, export_w=0, import_w=0)
        naive = max(0.0, 0.0 - 0.0)  # the same formula with the draw term dropped
        cases += 1
        assert visible == draw, (draw, visible)
        assert naive == 0
        assert visible > naive

    assert cases == 50


# ------------------------------------------------------------------
# No command without information: the undeclared-supply guard.
#
# This one lives in solar_controller.py, not solar.py - the phase
# guard reads the coordinator's raw payload, not anything SolarState
# carries - so it needs the controller module loaded, stubbing only
# the Home Assistant imports required to import it cleanly. Everything
# above this point never needed that.
# ------------------------------------------------------------------


def _install_ha_stubs() -> None:
    """Register just enough of Home Assistant for solar_controller.py
    to import. Nothing here is ever called: unsupported_reason and the
    early return in _async_evaluate never touch hass itself."""

    def _module(name: str, **attributes: Any) -> None:
        module = types.ModuleType(name)
        for key, value in attributes.items():
            setattr(module, key, value)
        sys.modules[name] = module

    def _unused(*_args: Any, **_kwargs: Any) -> Any:
        return lambda: None

    class _StubHomeAssistant:
        """Stands in for the type hint only; never instantiated here."""

    _module(
        "homeassistant.core", HomeAssistant=_StubHomeAssistant, callback=lambda fn: fn
    )
    _module("homeassistant.helpers")
    _module(
        "homeassistant.helpers.event",
        async_call_later=_unused,
        async_track_state_change_event=_unused,
    )
    _module("homeassistant")


def _load_controller_module() -> Any:
    """Load solar_controller.py as part of the daze package, the same
    way tests/test_solar_controller.py does, under a distinct package
    name so this module can be imported standalone or via run_all.py
    without colliding with that file's own sys.modules entries."""
    _install_ha_stubs()

    package = types.ModuleType("daze_solar_qa_ctl")
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules["daze_solar_qa_ctl"] = package

    for name in ("const", "payload", "optimistic", "solar"):
        spec = importlib.util.spec_from_file_location(
            f"daze_solar_qa_ctl.{name}", PACKAGE_DIR / f"{name}.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"daze_solar_qa_ctl.{name}"] = module
        spec.loader.exec_module(module)

    spec = importlib.util.spec_from_file_location(
        "daze_solar_qa_ctl.solar_controller",
        PACKAGE_DIR / "solar_controller.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["daze_solar_qa_ctl.solar_controller"] = module
    spec.loader.exec_module(module)
    return module


controller_module = _load_controller_module()


class _FakeCoordinator:
    """Just the `.data` attribute unsupported_reason reads.

    Deliberately has no `api_client`: if the guard this test protects
    is ever skipped, the tick reaching for one raises AttributeError
    instead of silently passing.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


def test_unsupported_reason_fires_whenever_the_supply_is_undeclared() -> None:
    """A missing declaration of how many phases feed the house is not
    evidence it is safe to guess. Sweeps supply_phases against every
    way the charger can (or cannot) confirm its own phase count, and
    checks that control refuses to run in every case where the number
    of phases is not affirmatively known - and, as a sanity check that
    this is not vacuously "always refuses", that it does not refuse
    once both sides agree.
    """
    declared_undeclared = 0
    declared_known = 0
    for supply_phases in (None, "single", "three", "SINGLE", "", "quad"):
        for three_phase_charger in (None, True, False):
            controller = controller_module.SolarController(
                hass=object(),
                coordinator=_FakeCoordinator({"evseIsThreePhase": three_phase_charger}),
                import_entity="sensor.grid_import",
                export_entity="sensor.grid_export",
                supply_phases=supply_phases,
            )
            reason = controller.unsupported_reason
            phases_affirmatively_known = supply_phases in ("single", "three")
            three_phase_unconfirmed = (
                supply_phases == "three" and three_phase_charger is not True
            )

            if not phases_affirmatively_known or three_phase_unconfirmed:
                declared_undeclared += 1
                assert reason is not None, (supply_phases, three_phase_charger)
            else:
                declared_known += 1
                assert reason is None, (supply_phases, three_phase_charger, reason)

    assert declared_undeclared > 0
    assert declared_known > 0


def test_undeclared_supply_blocks_every_tick_from_issuing_a_command() -> None:
    """The guard is not just a message a UI could ignore: an active
    controller with an undeclared supply must send nothing on any
    tick, regardless of surplus. coordinator here carries no
    api_client, so a guard that failed to hold would surface as an
    AttributeError from inside the tick rather than a silent pass.
    """
    for supply_phases in (None, "quad", "", "three"):
        coordinator = _FakeCoordinator({"evseIsThreePhase": None, "active": True})
        controller = controller_module.SolarController(
            hass=object(),
            coordinator=coordinator,
            import_entity="sensor.grid_import",
            export_entity="sensor.grid_export",
            supply_phases=supply_phases,
        )
        controller.mode = controller_module.SolarMode.ACTIVE

        assert controller.unsupported_reason is not None
        asyncio.run(controller.async_tick())


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
