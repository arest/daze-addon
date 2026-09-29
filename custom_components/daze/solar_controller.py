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
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later

from .payload import (
    charger_offline_reason,
    is_charge_enabled,
    max_charging_current,
    milliamps_to_watts,
    min_charging_current,
    watts_to_milliamps,
)
from .solar import (
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
        """Initialise in simulate mode, the safe default on first enable.

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

        self._mode = SolarMode.SIMULATE
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
