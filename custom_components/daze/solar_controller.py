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
from homeassistant.helpers.event import async_call_later

from .api import (
    COMMAND_ERROR_CODE_RPC_FAILURE,
    ApiAuthError,
    ApiCommandRejectedError,
    ApiError,
)
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
    MIN_MEANINGFUL_DRAW_W,
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
        self._stopped = False

        self._above_since: float | None = None
        self._below_since: float | None = None
        # The minimum-run clock: how long ago the current charge
        # began, consumed by decide() via seconds_since_start. Not
        # necessarily a start this controller issued — Task 10 seeds
        # this from a charge already running when the controller
        # starts, so a healthy charge is not stopped moments after
        # boot. Because of that, this must stay agnostic to who or
        # what started the charge; _check_ignored_start reads
        # _start_issued_at instead, never this one.
        self._started_at: float | None = None
        # "We issued a start and are waiting to see whether the car
        # draws." Set only in _carry_out, only when a start genuinely
        # reached the charger, so a charge this controller did not
        # itself start (seeded at boot, or started manually) is never
        # judged against a start that never happened.
        self._start_issued_at: float | None = None
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
        # A start issued before a detour through OFF or SIMULATE must
        # not survive it: the first tick back in ACTIVE would evaluate
        # it against an already-expired grace and arm an hour-long
        # back-off from a start that may be long irrelevant by now.
        self._start_issued_at = None
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
        self._stopped = False
        self._schedule_tick()

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
            # Absence of information is never grounds for acting, and
            # must not silently continue a confirmation or stop delay
            # that was timed against a period nobody actually observed.
            self._above_since = None
            self._below_since = None

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
            self._above_since = None
            self._below_since = None
            return

        state = self._build_state(smoothed, now)
        self._track_thresholds(state, now)

        # A start only simulated never reached the charger, so the
        # car was never given the chance to draw. Checking anyway
        # would let a dry run arm a real hour-long back-off from a
        # start that never happened.
        if self._mode is SolarMode.ACTIVE:
            self._check_ignored_start(now, state.car_connected)

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

    def _build_state(self, smoothed: float, now: float) -> SolarState:
        """Assemble everything the decision depends on."""
        data = self._coordinator.data or {}

        limit_ma = _coerce_float(
            data.get("maxExternalChargingCurrentInMilliAmps")
        ) or 0.0
        charging = bool(is_charge_enabled(data))
        schedules = data.get("schedules")

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
        self, description: str, call: Callable[[], Any], retry_key: str
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

        Returns:
            True if the command reached the charger. None if it was
            only handed to the background retry — accepted for now,
            but not yet confirmed, so a caller that must not proceed
            until the charger has actually applied the change (see
            the START branch of _carry_out) has to treat this the same
            as failure. False if it was not sent and will not be
            retried.

        """
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
                    on_failure=lambda message: _LOGGER.warning(
                        "%s", message
                    ),
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
