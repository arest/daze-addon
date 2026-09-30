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

import asyncio
import logging
import time
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)

from .api import (
    COMMAND_ERROR_CODE_RPC_FAILURE,
    ApiAuthError,
    ApiCommandRejectedError,
    ApiError,
)
from .const import SUPPLY_PHASES_SINGLE, SUPPLY_PHASES_THREE
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
    MIN_RUN_SECONDS,
    TICK_SECONDS,
    SolarAction,
    SolarDecision,
    SolarState,
    SurplusSmoother,
    compute_surplus,
    decide,
)

if TYPE_CHECKING:
    from .coordinator import DazeDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Recognised power units for the user's grid sensors, keyed by the
# lower-cased unit_of_measurement attribute. A kW inverter sensor read
# as watts would understate surplus by a factor of a thousand and
# still look like a plausible number, so anything else is treated the
# same as an unavailable reading rather than assumed to be watts.
_POWER_UNIT_FACTORS: dict[str, float] = {"w": 1.0, "kw": 1000.0}


def _coerce_float(value: Any) -> float | None:
    """Parse a number that may have arrived as a numeric string.

    The Daze API and Home Assistant sensors both sometimes carry a
    number as text, and a naive ``isinstance(value, (int, float))``
    check reads a string like ``"3000"`` as unusable rather than 3000.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


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
        reserve_w: float = 0.0,
        supply_phases: str | None = None,
    ) -> None:
        """Initialise in the off state.

        Ships off: nothing reads a sensor or notifies a listener until
        the user has opted in, since the select entity that gates that
        opt-in defaults to simulate the first time it does.

        Args:
            hass: Used to read the grid sensors and schedule ticks.
            coordinator: Source of charger state and the API client.
            import_entity: Grid import power sensor, or None.
            export_entity: Grid export power sensor, or None.
            reserve_w: Watts to leave for the house, restored from the
                config entry's options. Held there rather than only in
                memory: a reserve that returns to zero on every restart
                gives the car everything the house was keeping, and
                does it silently.
            supply_phases: "single", "three", or None if the user has
                not said. None refuses to arm rather than assuming:
                the payload cannot tell us, and the wrong guess loads
                one phase with a surplus measured across three.

        """
        self._hass = hass
        self._coordinator = coordinator
        self._import_entity = import_entity
        self._export_entity = export_entity
        self._supply_phases = supply_phases

        self._mode = SolarMode.OFF
        self._reserve_w = max(0.0, float(reserve_w))
        self._smoother = SurplusSmoother()
        self._last_decision: SolarDecision | None = None
        self._listeners: list[Callable[[], None]] = []
        self._cancel_tick: Callable[[], None] | None = None
        self._cancel_listener: Callable[[], None] | None = None
        self._stopped = False

        self._above_since: float | None = None
        self._below_since: float | None = None
        # When the raw reading fell below the floor and has stayed
        # there. Anchors the stop clock and latches the fast path.
        self._collapsed_since: float | None = None
        self._last_evaluation: float | None = None
        self._evaluating = False
        # The minimum-run clock: how long ago the current charge
        # began, consumed by decide() via seconds_since_start. Not
        # necessarily a start this controller issued — Task 10 seeds
        # this from a charge already running when the controller
        # starts, so a healthy charge is not stopped moments after
        # boot. Because of that, this must stay agnostic to who or
        # what started the charge; _check_ignored_start reads
        # _start_issued_at instead, never this one.
        self._started_at: float | None = None
        # Whether the minimum-run clock has already been seeded for the
        # charging episode under way. Once per episode, not once per
        # tick — see the seeding itself in _async_evaluate for why.
        self._charge_seeded = False
        # "We issued a start and are waiting to see whether the car
        # draws." Set only in _carry_out, only when a start genuinely
        # reached the charger, so a charge this controller did not
        # itself start (seeded at boot, or started manually) is never
        # judged against a start that never happened.
        self._start_issued_at: float | None = None
        self._backoff_until: float = 0.0
        self._command_times: list[float] = []
        self._sensor_warning_logged = False
        self._unsupported_warning_logged = False
        self._limit_warning_logged = False

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def mode(self) -> SolarMode:
        """Return the current mode."""
        return self._mode

    @mode.setter
    def mode(self, value: SolarMode) -> None:
        """Set the mode, resetting every episode clock when it changes.

        Shares _reset_episode with disarm(): both take solar control
        out of ACTIVE, and a select round-trip (off, then back to
        active) must end the episode exactly as thoroughly as disarm
        does, or four of the eight clocks survive it. Left set,
        _collapsed_since re-anchors the stop clock to a mark measured
        during a period nobody was watching and can issue an immediate
        STOP on arming; left set the other way, _charge_seeded stays
        True over a queued STOP's None _started_at and the minimum-run
        clock never re-seeds, so the charge can never be stopped.
        """
        if value is self._mode:
            return

        self._mode = value
        self._reset_episode()
        _LOGGER.info("Solar control set to %s", value.value)
        self._notify()

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

        The seeding flag goes with it too: left set across a disarm,
        a re-arm onto the same still-running charge would see
        _charge_seeded already True and never reseed _started_at,
        which this same method has just cleared to None — the minimum
        run time would then read as unelapsed for ever, on a charge
        already minutes or hours old, and solar control could never
        stop it. The mirror image of the restart bug, reached through
        disarm/re-arm instead of a reboot.
        """
        if self._mode is SolarMode.OFF:
            return

        _LOGGER.info("Solar control disarmed: %s", reason)
        self._mode = SolarMode.OFF
        self._reset_episode()
        self._notify()

    def _reset_episode(self) -> None:
        """Clear every clock that describes the current charging episode.

        One definition, called from both disarm() and the mode setter,
        so "ending an episode" means the same thing regardless of
        which door was used to end it — disarm, the number/switch
        entities, a service call, or the select flipping to off and
        back. Before this existed the setter only cleared three of the
        eight clocks and disarm cleared seven, so a select round-trip
        left _collapsed_since, _started_at, _charge_seeded and
        _backoff_until stranded across it.

        Clearing _started_at and _charge_seeded together here is safe
        even mid-charge: the seeding block in _async_evaluate re-seeds
        _started_at from a charge already running on the very next
        tick that observes it charging, which is the same outcome
        disarm's own docstring above already relies on.
        """
        self._above_since = None
        self._below_since = None
        self._start_issued_at = None
        self._collapsed_since = None
        self._started_at = None
        self._charge_seeded = False
        self._backoff_until = 0.0

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

        if self._supply_phases == SUPPLY_PHASES_THREE:
            # is False, not "not bool(...)": a captured real payload
            # carries this field (tests/test_payload.py's EVSE_RECORD),
            # so an absent reading is not the same fact as a charger
            # that has confirmed it is single-phase, and the two need
            # different messages — the same distinction _car_draw_w,
            # _read_power and charger_reachable already make on this
            # branch, between "confirmed no" and "do not know".
            three_phase_charger = data.get("evseIsThreePhase")
            if three_phase_charger is None:
                return "the charger has not said how many phases it uses"

            if three_phase_charger is False:
                return (
                    "the supply is three-phase and the charger is "
                    "single-phase, so exported power may be on a phase "
                    "it cannot use"
                )

        if data.get("ecoModeEnabled"):
            return "the charger's own eco mode is controlling it"

        # Not "schedules": see the comment on schedule_info in
        # _build_state — that key never survives merge_payload, and
        # nextScheduleInfo is the one that does.
        if data.get("nextScheduleInfo"):
            return "the charger has a schedule set"

        return None

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

    async def async_stop(self) -> None:
        """Stop ticking and drop listeners.

        Sets a flag rather than only cancelling the pending timer,
        because a tick already in flight has cleared its own handle
        before this can run: there is nothing left to cancel, but the
        cycle must still not re-arm itself once it finishes.
        """
        self._stopped = True
        if self._cancel_tick is not None:
            self._cancel_tick()
            self._cancel_tick = None
        if self._cancel_listener is not None:
            self._cancel_listener()
            self._cancel_listener = None
        self._listeners.clear()

    # ------------------------------------------------------------------
    # The cycle
    # ------------------------------------------------------------------

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

    async def _async_evaluate(self) -> None:
        """Evaluate once and act if the mode allows it."""
        if self._mode is SolarMode.OFF:
            return

        unsupported = self.unsupported_reason
        if unsupported is not None:
            if not self._unsupported_warning_logged:
                self._unsupported_warning_logged = True
                _LOGGER.warning("Solar control cannot run: %s", unsupported)
            return

        self._unsupported_warning_logged = False

        now = time.monotonic()
        surplus = self._read_surplus()

        if surplus is None:
            # Absence of information is never grounds for acting, and
            # must not silently continue a confirmation or stop delay
            # that was timed against a period nobody actually observed.
            # _collapsed_since anchors that same stop delay to a raw
            # reading, so it goes with the other two clocks: left set,
            # a blind period would let the stop clock resume counting
            # from a collapse observed before the sensors went dark,
            # against time nobody actually watched.
            self._above_since = None
            self._below_since = None
            self._collapsed_since = None

            if not self._sensor_warning_logged:
                self._sensor_warning_logged = True
                _LOGGER.warning(
                    "Solar control cannot read its grid sensors; doing "
                    "nothing until they report"
                )
            # _last_evaluation is the fast path's spacing clock, and
            # this evaluation observed nothing: leaving it unset here
            # means a genuine collapse in the following TICK_SECONDS is
            # not deferred a full tick on the strength of a cycle that
            # never actually looked.
            return

        self._last_evaluation = now
        self._sensor_warning_logged = False
        self._smoother.add(surplus, now)

        smoothed = self._smoother.value()
        if smoothed is None:
            self._above_since = None
            self._below_since = None
            self._collapsed_since = None
            return

        if self._current_limit_ma() is None:
            # The surplus reading was real, so the threshold clocks keep
            # what they have earned — unlike the blind-sensor path
            # above, this cycle did observe the surplus. What it cannot
            # observe is the charger's own limit, and every decision
            # from here compares a target against it.
            if not self._limit_warning_logged:
                self._limit_warning_logged = True
                _LOGGER.warning(
                    "Solar control cannot read the charger's own "
                    "charging limit; doing nothing until it reports"
                )
            return

        self._limit_warning_logged = False

        state = self._build_state(smoothed, now)
        self._track_thresholds(state, now, surplus)

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

        # A start only simulated never reached the charger, so the
        # car was never given the chance to draw. Checking anyway
        # would let a dry run arm a real hour-long back-off from a
        # start that never happened.
        if self._mode is SolarMode.ACTIVE:
            self._check_ignored_start(now, state.car_connected)

        decision = decide(self._build_state(smoothed, now))
        self._last_decision = decision

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
        if self._mode is SolarMode.OFF or self._stopped:
            # Mirrors _schedule_tick's own re-arm check: async_stop
            # cancels this subscription, but does not do so atomically
            # with setting the flag, so an event already dispatched can
            # still arrive here in the gap. Without this, that race
            # runs a full evaluation — and can issue a command — on a
            # controller that believes it has been torn down.
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
                if not self._stopped:
                    self._schedule_tick()

        self._cancel_tick = async_call_later(self._hass, TICK_SECONDS, _run)

    def _read_power(self, entity_id: str | None) -> float | None:
        """Read a grid power sensor, in watts, or None if unusable.

        Only W and kW are recognised, whatever unit the entity itself
        displays. An unrecognised or missing unit is treated the same
        as an unavailable reading: acting on a number whose scale is
        unknown risks a surplus over- or under-stated by a factor of a
        thousand, which is worse than waiting a cycle.
        """
        if not entity_id:
            return None

        state = self._hass.states.get(entity_id)
        if state is None:
            return None

        value = _coerce_float(state.state)
        if value is None:
            return None

        attributes = getattr(state, "attributes", None) or {}
        unit = str(attributes.get("unit_of_measurement", "")).strip().lower()
        factor = _POWER_UNIT_FACTORS.get(unit)
        if factor is None:
            return None

        return value * factor

    @staticmethod
    def _car_draw_w(data: dict[str, Any]) -> float | None:
        """Return the charger's own draw, in watts, or None if unknown.

        Zero is only a safe default while the charger reports that it
        is not delivering power. ``instantPowerAsWatt`` comes from the
        active charge session (see payload.merge_payload), so it goes
        missing whenever that session drops out of a single poll.
        Reading that as zero while the charger is actually charging
        misreads the car's own draw as surplus that vanished, which is
        the exact failure this project has already hit once.

        Checked against ``is False`` rather than truthiness: an
        *unknown* status (an absent or partial payload, where
        is_charge_enabled returns None) is not evidence the car draws
        nothing either, and reading it as zero pollutes the five-minute
        average with an assumed reading that survives long after the
        payload that produced it is gone.
        """
        value = _coerce_float(data.get("instantPowerAsWatt"))
        if value is not None:
            return value
        return 0.0 if is_charge_enabled(data) is False else None

    def _read_surplus(self) -> float | None:
        """Compute surplus from the grid sensors and the car's draw."""
        import_w = self._read_power(self._import_entity)
        export_w = self._read_power(self._export_entity)

        if import_w is None or export_w is None:
            return None

        data = self._coordinator.data or {}
        car_w = self._car_draw_w(data)
        if car_w is None:
            return None

        return compute_surplus(
            car_draw_w=car_w, export_w=export_w, import_w=import_w
        )

    def _current_limit_ma(self) -> float | None:
        """The charger's own limit in milliamps, or None if unknown.

        Absent and zero are different facts. The limit feeds
        `current_limit_w`, which `decide()` compares against the target
        across the 300 W deadband: read as 0 mA, an unknown limit makes
        every target look like a large change and produces a SET on the
        first tick, spending one of the twenty hourly commands to
        re-assert a limit that was probably already correct.
        """
        return _coerce_float(
            (self._coordinator.data or {}).get(
                "maxExternalChargingCurrentInMilliAmps"
            )
        )

    def _build_state(self, smoothed: float, now: float) -> SolarState:
        """Assemble everything the decision depends on."""
        data = self._coordinator.data or {}

        # 0.0 here is a structural placeholder, not a reading. The live
        # path cannot reach it: _async_evaluate calls
        # _current_limit_ma() and skips the cycle when the charger has
        # not reported a limit, precisely so an unknown never enters
        # the deadband comparison as zero. Tests that call _build_state
        # directly supply their own payload.
        limit_ma = self._current_limit_ma() or 0.0
        charging = bool(is_charge_enabled(data))
        # Not "schedules": that key is a list, which payload._scalars
        # drops from every source merge_payload flattens, so it never
        # survives to the merged payload. merge_payload re-attaches
        # exactly two nested objects by name, and nextScheduleInfo is
        # the one the charger uses to report a configured schedule —
        # an object when one is set, None when it is not (see
        # sensor_catalog.get_next_scheduled_charge and
        # tests/test_payload.py's schedule-object tests).
        schedule_info = data.get("nextScheduleInfo")

        return SolarState(
            surplus_w=smoothed,
            reserve_w=self._reserve_w,
            floor_w=milliamps_to_watts(min_charging_current(data), data),
            ceiling_w=milliamps_to_watts(max_charging_current(data), data),
            charging=charging,
            current_limit_w=milliamps_to_watts(limit_ma, data),
            command_pending=self._coordinator.limit_state.pending,
            # An absent payload — before the first successful poll —
            # must read as unreachable rather than as "no known reason
            # to think otherwise". charger_offline_reason returns None
            # for that case too, which would otherwise make an unpolled
            # charger look reachable by luck rather than by design.
            charger_reachable=bool(data)
            and charger_offline_reason(data) is None,
            eco_mode_on=bool(data.get("ecoModeEnabled")),
            schedule_set=bool(schedule_info),
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

        # I1: kept as two independent conditions rather than one
        # if/else, deliberately. Coupling them (as a single "available
        # is healthy and collapsed_since is None" test previously did)
        # let one instantaneous raw dip — an oven cycling on, say —
        # reset _above_since even while the smoothed surplus never
        # left the healthy range: _collapsed_since flips on and off
        # every tick the raw reading dips, and each flip took the
        # else-branch and cleared _above_since, so the 300s start
        # delay could never accrue on a day with ample average
        # surplus. _above_since answers a question only the smoothed
        # figure should decide.
        if available >= state.floor_w:
            if self._above_since is None:
                self._above_since = now
        else:
            self._above_since = None

        # _below_since is unaffected by that split: this is exactly
        # the negation of the old combined condition
        # (available >= floor and collapsed_since is None), so every
        # existing anchor case — including the one where the smoothed
        # figure is still healthy but the raw reading has already
        # collapsed and _track_thresholds must anchor the stop clock
        # to that collapse, not to whenever the average catches up —
        # keeps behaving exactly as before.
        if available < state.floor_w or self._collapsed_since is not None:
            if self._below_since is None:
                self._below_since = self._collapsed_since or now
        else:
            self._below_since = None

    def _check_ignored_start(self, now: float, car_connected: bool) -> None:
        """Back off if a car we started never began drawing.

        When a car finishes it stops drawing while surplus is still
        high. The charger goes idle, the controller sees "not charging,
        plenty of surplus", and starts again. Without this the cycle
        repeats until sunset.

        Reads ``_start_issued_at``, never ``_started_at``: the latter
        is the minimum-run clock ``decide()`` consumes, and can be
        seeded from a charge the controller did not itself start (a
        charge already running when Home Assistant restarts — see
        Task 10). Judging that against a start that never happened
        would arm an hour-long back-off on a perfectly healthy charge
        the moment it passes through the wait-for-EV state every
        start goes through. Only a start this method's own caller
        actually issued, and that reached the charger, sets
        ``_start_issued_at`` in the first place.

        A disconnected car is checked before the grace period: a car
        that has been unplugged reads as idle at 0 W, which otherwise
        satisfies every condition this method checks for — but the
        car did not ignore the start, it left, so the mark is cleared
        without arming anything.

        Draw is read through ``_car_draw_w`` rather than the raw
        ``instantPowerAsWatt`` field, and its None is treated as "wait
        and see", not "not drawing": a missing or unparseable reading
        while the charger is mid-session says nothing about the car,
        and backing off on that would arm an hour-long pause on a
        car that may already be drawing fine.
        """
        if self._start_issued_at is None:
            return

        if not car_connected:
            self._start_issued_at = None
            return

        if now - self._start_issued_at < DRAW_GRACE_SECONDS:
            return

        data = self._coordinator.data or {}
        draw = self._car_draw_w(data)
        if draw is None:
            return

        if draw >= MIN_MEANINGFUL_DRAW_W:
            # Confirmed drawing: the "waiting to see" period is over.
            self._start_issued_at = None
            return

        self._backoff_until = now + IGNORED_START_BACKOFF_SECONDS
        self._start_issued_at = None
        _LOGGER.info(
            "The car did not draw within %ds of starting; backing off for "
            "%d minutes",
            DRAW_GRACE_SECONDS,
            IGNORED_START_BACKOFF_SECONDS // 60,
        )

    async def _send_command(
        self,
        description: str,
        call: Callable[[], Any],
        retry_key: str,
        on_retry_exhausted: Callable[[], None] | None = None,
    ) -> bool | None:
        """Issue one command, handing a stuck link to the background retry.

        Mirrors the handling in number.py: an RPC failure — the Daze
        service could not reach the wallbox over its own link — is
        handed to the coordinator's existing background retry rather
        than retried here, since that already covers minutes of
        attempts. Anything else (auth failure, an outright rejection,
        a bare timeout outside the API's own exception hierarchy — no
        total timeout is configured on the session, so aiohttp's
        default eventually raises one) is not retryable and is only
        logged; a bad command will not start succeeding because it is
        repeated, and autonomous code needs a wider net than a service
        call a human is watching, or this becomes an unhandled task
        exception in the event loop every two minutes.

        Args:
            description: Used in log messages and the retry's own
                description.
            call: Performs the command. Must be safe to call again if
                handed to the background retry.
            retry_key: Identifies this command for the background
                retry, so a newer one supersedes an older one, and
                shared with whatever manual entity can act on the same
                physical setting so the two supersede each other too.
            on_retry_exhausted: Called, in addition to the warning log,
                if the background retry's own chain of attempts runs
                out without the command ever landing. Only the STOP
                path uses this today (see the C1 fix note in
                _carry_out): a stop that never lands must not leave
                the minimum-run clock seeded against a charge that
                never actually stopped.

        Returns:
            True if the command reached the charger. None if it was
            only handed to the background retry — accepted for now,
            but not yet confirmed, so a caller that must not proceed
            until the charger has actually applied the change (see
            the START branch of _carry_out) has to treat this the same
            as failure. False if it was not sent and will not be
            retried.

        """

        def _on_failure(message: str) -> None:
            _LOGGER.warning("%s", message)
            if on_retry_exhausted is not None:
                on_retry_exhausted()

        try:
            await call()
        except ApiAuthError as err:
            _LOGGER.warning("Auth error %s: %s", description, err)
            return False
        except ApiCommandRejectedError as err:
            if err.code == COMMAND_ERROR_CODE_RPC_FAILURE:
                self._coordinator.async_retry_in_background(
                    key=retry_key,
                    action=call,
                    description=description,
                    on_failure=_on_failure,
                )
                return None

            _LOGGER.info("Charger refused %s: %s", description, err)
            return False
        except ApiError as err:
            _LOGGER.warning("API error %s: %s", description, err)
            return False
        except asyncio.TimeoutError as err:
            _LOGGER.warning("Timed out %s: %s", description, err)
            return False

        return True

    async def _carry_out(self, decision: SolarDecision, now: float) -> None:
        """Issue the command a decision calls for.

        A command that fails outright is only logged; do not retry it
        here (see _send_command). Each individual attempt is counted
        against the hourly backstop regardless of outcome, and
        separately per command — a START issues both a current-set and
        a start-charge, and counting the branch once rather than each
        call would let a start-heavy failure mode burn the real API at
        twice the rate the backstop assumes.

        Retry keys are shared with whatever manual entity can act on
        the same physical setting: f"{serial}:current" with number.py's
        current and power entities, f"{serial}:charge" with switch.py's
        start/stop switch. That gives supersession for free in both
        directions through the coordinator's own machinery — a newer
        retry cancels an older one under the same key on the way in,
        and every command-issuing module, this one included, cancels
        its own key on a successful direct send — rather than a stale
        queued retry landing minutes later and undoing whichever side
        acted more recently. A send only queued for the background
        retry (None, not True) is not cancelled: it has not reached the
        charger yet, so the retry it would cancel is the only thing
        still trying to get the change applied.
        """
        client = self._coordinator.api_client
        serial = self._coordinator.serial_number
        data = self._coordinator.data or {}
        current_key = f"{serial}:current"
        charge_key = f"{serial}:charge"

        _LOGGER.info(
            "Solar control: %s — %s", decision.action.value, decision.reason
        )

        if decision.action is SolarAction.STOP:
            self._command_times.append(now)
            stop_sent = await self._send_command(
                "stopping the charge",
                lambda: client.async_stop_charge(serial),
                charge_key,
                # C1: a STOP only ever queued for the background retry
                # clears _started_at below (stop_sent is not False)
                # while the charge is still running, so _charge_seeded
                # stays True and the minimum-run clock can never
                # re-seed — decide() reads seconds_since_start as 0.0
                # for ever and the car imports from the grid until
                # someone notices. If that retry chain later exhausts
                # without the stop landing, release the seed so the
                # very next tick that still observes charging re-seeds
                # _started_at and a fresh STOP can be issued.
                on_retry_exhausted=lambda: setattr(
                    self, "_charge_seeded", False
                ),
            )
            if stop_sent is True:
                self._coordinator.async_cancel_background_retry(charge_key)
            if stop_sent is not False:
                self._started_at = None
                # A stopped charge is no longer waiting to see if the
                # car draws. Left set, a stale mark here would anchor
                # the *next* start's draw-grace clock to this one's
                # issue time instead of its own.
                self._start_issued_at = None

        elif decision.action is SolarAction.START:
            # decide() has no memory of an outstanding start: while
            # the charger sits idle with surplus sustained it returns
            # START on every tick regardless. A further START while
            # one is already outstanding and its grace has not
            # elapsed is a retry of a start already in flight, not a
            # fresh one — "backs off ... rather than retrying" means
            # declining it, not resending it every 120s until the
            # grace catches up. Must not touch _start_issued_at here
            # either way — resetting it on decline would rebuild the
            # exact bug fix round 1 closed (C1).
            if (
                self._start_issued_at is not None
                and now - self._start_issued_at < DRAW_GRACE_SECONDS
            ):
                _LOGGER.info(
                    "Solar control: a start is already outstanding, "
                    "waiting to see if the car draws before retrying"
                )
                return

            sent = True
            if decision.target_watts is not None:
                milliamps = watts_to_milliamps(decision.target_watts, data)
                self._command_times.append(now)
                sent = await self._send_command(
                    f"setting the charging current to {milliamps} mA",
                    lambda: client.async_set_max_charging_current(
                        serial, milliamps
                    ),
                    current_key,
                )
                if sent is True:
                    self._coordinator.async_cancel_background_retry(
                        current_key
                    )

            # I2: _carry_out awaits the current-set above, and a user
            # action landing during that await (the power slider, the
            # charge switch, a service call) calls disarm() — which
            # sets the mode to OFF but does not, and cannot, cancel
            # this coroutine already in flight. Without re-testing the
            # mode here, resuming after that await would send the
            # start-charge anyway: the car starts on grid power against
            # the user's own action, and solar is now OFF, so it will
            # never stop it either. Re-checked here rather than once at
            # the top of _carry_out because the mode is guaranteed
            # ACTIVE at entry (the only two callers, both in
            # _async_evaluate, already filter OFF and SIMULATE) and can
            # only have changed by drifting across an await since.
            if self._mode is not SolarMode.ACTIVE:
                return

            # A limit only queued for the background retry has not
            # reached the charger yet. Starting anyway would run the
            # car at whatever limit it already had — importing from
            # the grid, the one outcome pure-solar mode exists to
            # prevent — so "queued" is not treated as "sent" here.
            if sent is True:
                self._command_times.append(now)
                start_sent = await self._send_command(
                    "starting the charge",
                    lambda: client.async_start_charge(serial),
                    charge_key,
                )
                if start_sent is True:
                    self._coordinator.async_cancel_background_retry(
                        charge_key
                    )
                if start_sent is not False:
                    self._started_at = now

                # The draw-grace clock, unlike the minimum-run clock
                # just above, must not restart on every repeat START:
                # decide() has no memory of already having started, so
                # while the charger sits idle with surplus sustained
                # it returns START on every tick. Resetting this on
                # each one would mean the 300 s grace never elapses,
                # and the car would be restarted, uselessly, until the
                # hourly rate limit — a backstop against bugs, not a
                # substitute for this — finally blunts it. A queued
                # send (None) does not count either: it has not
                # reached the charger, so there is nothing yet for the
                # car to have ignored.
                if start_sent is True and self._start_issued_at is None:
                    self._start_issued_at = now

        elif decision.action is SolarAction.SET:
            if decision.target_watts is None:
                return

            self._command_times.append(now)
            milliamps = watts_to_milliamps(decision.target_watts, data)
            set_sent = await self._send_command(
                f"setting the charging current to {milliamps} mA",
                lambda: client.async_set_max_charging_current(
                    serial, milliamps
                ),
                current_key,
            )
            if set_sent is True:
                self._coordinator.async_cancel_background_retry(current_key)

        self._coordinator.async_schedule_refresh_in(10)

    def _notify(self) -> None:
        """Tell the entities to redraw."""
        for listener in list(self._listeners):
            listener()
