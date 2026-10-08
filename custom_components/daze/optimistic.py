"""Shared optimistic-state handling for the controllable entities.

A command takes seconds to show up in the charger's own reading, and a
background retry can take minutes. Reporting the charger's reading over
that window shows the pre-command value, so a toggle appears to flip
back and a slider appears to snap to its old position.

Every control needs the same behaviour, and it was previously written
out separately in the switch, the current number, the power number and
the mode select. Three review findings came from those copies drifting
apart: the switch ignored a pending retry, the power entity compared
derived watts for exact equality, and a superseded retry could pin a
value until a restart. This module is the single implementation.

Importing nothing from Home Assistant keeps it directly testable.
"""

from __future__ import annotations

import time
from typing import Any

from .const import MAX_OPTIMISTIC_HOLD, OPTIMISTIC_STATE_TIMEOUT


class OptimisticState:
    """Tracks a requested value until the charger confirms it.

    Not an entity mixin: entities hold one of these rather than
    inheriting, so the same logic can be tested without constructing a
    Home Assistant entity.
    """

    def __init__(self, tolerance: float = 0) -> None:
        """Initialise with no pending value.

        Args:
            tolerance: How far the charger's reading may differ from
                the request and still count as agreement. Needed where
                the value is derived from a fluctuating measurement,
                such as watts computed from the live voltage, where
                exact equality would practically never hold.

        """
        self._tolerance = tolerance
        self._value: Any | None = None
        self._since: float = 0.0
        self._awaiting_retry: bool = False

    @property
    def pending(self) -> bool:
        """Whether a requested value is currently being shown."""
        return self._value is not None

    @property
    def value(self) -> Any | None:
        """The requested value, or None."""
        return self._value

    def request(self, value: Any, awaiting_retry: bool = False) -> None:
        """Start showing a requested value.

        Args:
            value: What the user asked for.
            awaiting_retry: True when the command did not reach the
                charger and is queued for a background retry, which
                means it stays displayed for far longer.

        """
        self._value = value
        self._since = time.monotonic()
        self._awaiting_retry = awaiting_retry

    def clear(self) -> None:
        """Stop showing a requested value."""
        self._value = None
        self._awaiting_retry = False

    def expired(self) -> bool:
        """Whether the requested value has been shown for too long.

        A pending retry extends the window, because the request really
        is still outstanding. It does not extend it indefinitely: a
        retry chain that gets superseded never reports back, and
        without a cap the entity would show a stale request until Home
        Assistant restarts.
        """
        if self._value is None:
            return True

        held = time.monotonic() - self._since

        if self._awaiting_retry:
            return held > MAX_OPTIMISTIC_HOLD

        return held > OPTIMISTIC_STATE_TIMEOUT

    def matches(self, actual: Any | None) -> bool:
        """Whether the charger's reading agrees with the request."""
        if self._value is None or actual is None:
            return False

        if self._tolerance and isinstance(actual, (int, float)):
            if not isinstance(self._value, (int, float)):
                return False
            return abs(actual - self._value) <= self._tolerance

        return bool(actual == self._value)

    def resolve(self, actual: Any | None) -> Any | None:
        """Return what the entity should report, and update state.

        Drops the request once the charger agrees, so a later change
        made elsewhere is not masked, and once it has been held too
        long, so a command that silently failed cannot leave the entity
        asserting something untrue.

        Args:
            actual: What the charger currently reports.

        Returns:
            The value to display.

        """
        if self._value is None:
            return actual

        if self.expired() or self.matches(actual):
            self.clear()
            return actual

        return self._value

    def settle(self, actual: Any | None) -> None:
        """Drop the request if the charger has caught up.

        Called on a coordinator update, where the aim is only to stop
        tracking rather than to produce a value.
        """
        if self._value is not None and (self.matches(actual) or self.expired()):
            self.clear()
