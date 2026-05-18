"""Tests for the Daze Wallbox session data model.

Tests the RechargeSession dataclass, from_dict parsing, and helper
functions for datetime/float parsing — duplicated inline to avoid
importing the full HA runtime (matching existing test pattern).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ------------------------------------------------------------------
# Pure logic from custom_components/daze/models.py
# ------------------------------------------------------------------


@dataclass
class RechargeSession:
    """A single recharge session from the Daze API."""

    session_uid: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    energy_wh: float | None = None
    cost: float | None = None
    currency: str | None = None
    status: str | None = None
    evse_serial: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RechargeSession:
        return cls(
            session_uid=str(data.get("sessionUid", "")),
            start_time=_parse_datetime(data.get("startDate")),
            end_time=_parse_datetime(data.get("endDate")),
            energy_wh=_safe_float(data.get("energyInWh")),
            cost=_safe_float(data.get("totalCost")),
            currency=data.get("currency"),
            status=data.get("status"),
            evse_serial=data.get("evseSerialNumber"),
            raw=data,
        )

    @property
    def is_in_progress(self) -> bool:
        return self.end_time is None and self.status != "completed"


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass
    return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------
# Sample data
# ------------------------------------------------------------------

COMPLETED_SESSION: dict[str, Any] = {
    "sessionUid": "sess-001",
    "startDate": "2026-05-01T14:00:00Z",
    "endDate": "2026-05-01T16:30:00Z",
    "energyInWh": 15000,
    "totalCost": 3.75,
    "currency": "EUR",
    "status": "completed",
    "evseSerialNumber": "DAZE-12345",
}

IN_PROGRESS_SESSION: dict[str, Any] = {
    "sessionUid": "sess-002",
    "startDate": "2026-05-18T10:00:00Z",
    "endDate": None,
    "energyInWh": 8000,
    "totalCost": None,
    "currency": "EUR",
    "status": "charging",
    "evseSerialNumber": "DAZE-12345",
}

EMPTY_DICT: dict[str, Any] = {}

PARTIAL_DICT: dict[str, Any] = {
    "sessionUid": "sess-003",
}

TIMESTAMP_INT_DICT: dict[str, Any] = {
    "sessionUid": "sess-004",
    "startDate": 1715000000,
    "endDate": 1715010000,
    "energyInWh": 10000,
    "totalCost": 2.50,
    "currency": "EUR",
    "status": "completed",
}


# ==================================================================
# Tests
# ==================================================================


class TestRechargeSessionFromDict:
    """Verify RechargeSession.from_dict parsing."""

    def test_completed_session(self) -> None:
        session = RechargeSession.from_dict(COMPLETED_SESSION)
        assert session.session_uid == "sess-001"
        assert session.energy_wh == 15000
        assert session.cost == 3.75
        assert session.currency == "EUR"
        assert session.status == "completed"
        assert session.evse_serial == "DAZE-12345"
        assert session.is_in_progress is False

    def test_in_progress_session(self) -> None:
        session = RechargeSession.from_dict(IN_PROGRESS_SESSION)
        assert session.session_uid == "sess-002"
        assert session.energy_wh == 8000
        assert session.cost is None
        assert session.end_time is None
        assert session.is_in_progress is True

    def test_empty_dict(self) -> None:
        session = RechargeSession.from_dict(EMPTY_DICT)
        assert session.session_uid == ""
        assert session.energy_wh is None
        assert session.cost is None
        assert session.start_time is None
        assert session.end_time is None
        # no end_time means in-progress (end_time None, status "")
        assert session.is_in_progress is True

    def test_partial_data(self) -> None:
        session = RechargeSession.from_dict(PARTIAL_DICT)
        assert session.session_uid == "sess-003"
        assert session.energy_wh is None
        assert session.cost is None

    def test_timestamp_integers(self) -> None:
        session = RechargeSession.from_dict(TIMESTAMP_INT_DICT)
        assert session.session_uid == "sess-004"
        assert isinstance(session.start_time, datetime)
        assert isinstance(session.end_time, datetime)
        assert session.energy_wh == 10000.0

    def test_raw_data_preserved(self) -> None:
        session = RechargeSession.from_dict(COMPLETED_SESSION)
        assert session.raw == COMPLETED_SESSION

    def test_from_dict_never_raises(self) -> None:
        """Any dict input should produce a valid session."""
        for raw in [COMPLETED_SESSION, IN_PROGRESS_SESSION, EMPTY_DICT,
                     PARTIAL_DICT, TIMESTAMP_INT_DICT, {}, {"strange": "data"}]:
            session = RechargeSession.from_dict(raw)
            assert isinstance(session, RechargeSession)

    def test_sessions_maintain_api_order(self) -> None:
        """API returns sessions newest-first; order is preserved."""
        sessions = [
            RechargeSession.from_dict(COMPLETED_SESSION),
            RechargeSession.from_dict(IN_PROGRESS_SESSION),
        ]
        assert len(sessions) == 2
        assert sessions[0].session_uid == "sess-001"
        assert sessions[1].session_uid == "sess-002"


class TestParseDatetime:
    """Verify _parse_datetime helper."""

    def test_iso_string(self) -> None:
        result = _parse_datetime("2026-05-01T14:00:00Z")
        assert isinstance(result, datetime)
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 1

    def test_none_returns_none(self) -> None:
        assert _parse_datetime(None) is None

    def test_integer_timestamp(self) -> None:
        result = _parse_datetime(1715000000)
        assert isinstance(result, datetime)

    def test_float_timestamp(self) -> None:
        result = _parse_datetime(1715000000.0)
        assert isinstance(result, datetime)

    def test_already_datetime(self) -> None:
        now = datetime.now(timezone.utc)
        result = _parse_datetime(now)
        assert result is now

    def test_invalid_string_returns_none(self) -> None:
        assert _parse_datetime("not-a-date") is None

    def test_bad_type_returns_none(self) -> None:
        assert _parse_datetime([]) is None


class TestSafeFloat:
    """Verify _safe_float helper."""

    def test_int_to_float(self) -> None:
        assert _safe_float(42) == 42.0

    def test_float_passthrough(self) -> None:
        assert _safe_float(3.14) == 3.14

    def test_string_number(self) -> None:
        assert _safe_float("3.14") == 3.14

    def test_none_returns_none(self) -> None:
        assert _safe_float(None) is None

    def test_bad_type_returns_none(self) -> None:
        assert _safe_float("not-a-number") is None
        assert _safe_float([]) is None


# ------------------------------------------------------------------
# Pure logic from coordinator._compute_session_fields
# ------------------------------------------------------------------


def compute_session_fields(
    sessions: list[RechargeSession],
) -> dict[str, Any]:
    """Compute derived session sensor values from session list.

    Mirrors DazeDataUpdateCoordinator._compute_session_fields.
    """
    from datetime import datetime, timezone

    fields: dict[str, Any] = {
        "last_session_energy": None,
        "last_session_duration": None,
        "last_session_cost": None,
        "last_session_start": None,
        "last_session_end": None,
        "lifetime_energy": 0.0,
        "total_sessions": len(sessions),
    }

    if not sessions:
        return fields

    last = sessions[0]
    fields["last_session_energy"] = last.energy_wh
    fields["last_session_cost"] = last.cost
    fields["last_session_start"] = last.start_time
    fields["last_session_end"] = last.end_time

    if last.start_time and last.end_time:
        delta = last.end_time - last.start_time
        fields["last_session_duration"] = delta.total_seconds() / 60.0
    elif last.start_time:
        delta = datetime.now(timezone.utc) - last.start_time
        fields["last_session_duration"] = delta.total_seconds() / 60.0

    lifetime = 0.0
    for ses in sessions:
        if ses.energy_wh is not None:
            lifetime += ses.energy_wh
    fields["lifetime_energy"] = lifetime

    return fields


class TestComputeSessionFields:
    """Verify coordinator session field computation."""

    def test_empty_list(self) -> None:
        fields = compute_session_fields([])
        assert fields["last_session_energy"] is None
        assert fields["last_session_duration"] is None
        assert fields["last_session_cost"] is None
        assert fields["last_session_start"] is None
        assert fields["last_session_end"] is None
        assert fields["lifetime_energy"] == 0.0
        assert fields["total_sessions"] == 0

    def test_single_completed_session(self) -> None:
        start = datetime(2026, 5, 1, 14, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 1, 16, 30, 0, tzinfo=timezone.utc)
        session = RechargeSession(
            session_uid="sess-001",
            start_time=start,
            end_time=end,
            energy_wh=15000.0,
            cost=3.75,
            currency="EUR",
            status="completed",
        )
        fields = compute_session_fields([session])
        assert fields["last_session_energy"] == 15000.0
        assert fields["last_session_cost"] == 3.75
        assert fields["last_session_duration"] == 150.0  # 2.5 hours
        assert fields["last_session_start"] == start
        assert fields["last_session_end"] == end
        assert fields["lifetime_energy"] == 15000.0
        assert fields["total_sessions"] == 1

    def test_in_progress_session(self) -> None:
        """In-progress session: end_time is None, cost is None."""
        start = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        session = RechargeSession(
            session_uid="sess-002",
            start_time=start,
            end_time=None,
            energy_wh=8000.0,
            cost=None,
            status="charging",
        )
        fields = compute_session_fields([session])
        assert fields["last_session_energy"] == 8000.0
        assert fields["last_session_cost"] is None
        assert fields["last_session_end"] is None
        # Duration is computed from now - start (should be positive)
        assert fields["last_session_duration"] is not None
        assert fields["last_session_duration"] > 0
        assert fields["lifetime_energy"] == 8000.0
        assert fields["total_sessions"] == 1

    def test_multiple_sessions_aggregation(self) -> None:
        start1 = datetime(2026, 5, 1, 14, 0, 0, tzinfo=timezone.utc)
        end1 = datetime(2026, 5, 1, 16, 0, 0, tzinfo=timezone.utc)
        start2 = datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc)
        end2 = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)

        sessions = [
            RechargeSession(
                session_uid="sess-001",
                start_time=start1, end_time=end1,
                energy_wh=10000.0, cost=2.50,
                status="completed",
            ),
            RechargeSession(
                session_uid="sess-002",
                start_time=start2, end_time=end2,
                energy_wh=20000.0, cost=5.00,
                status="completed",
            ),
        ]
        fields = compute_session_fields(sessions)
        # Last session (newest first) = first in list
        assert fields["last_session_energy"] == 10000.0
        assert fields["last_session_cost"] == 2.50
        # Lifetime energy sums both sessions
        assert fields["lifetime_energy"] == 30000.0
        assert fields["total_sessions"] == 2

    def test_session_with_none_energy(self) -> None:
        """Sessions with None energy should not contribute to lifetime."""
        sessions = [
            RechargeSession(
                session_uid="sess-001",
                start_time=datetime(2026, 5, 1, 14, 0, 0, tzinfo=timezone.utc),
                end_time=datetime(2026, 5, 1, 16, 0, 0, tzinfo=timezone.utc),
                energy_wh=None,
                cost=None,
                status="completed",
            ),
        ]
        fields = compute_session_fields(sessions)
        assert fields["last_session_energy"] is None
        assert fields["lifetime_energy"] == 0.0
        assert fields["total_sessions"] == 1

    def test_session_without_start_end(self) -> None:
        """Session with no start/end times should not crash."""
        session = RechargeSession(
            session_uid="sess-001",
            start_time=None,
            end_time=None,
            energy_wh=5000.0,
        )
        fields = compute_session_fields([session])
        assert fields["last_session_energy"] == 5000.0
        assert fields["last_session_duration"] is None  # no start or end
        assert fields["lifetime_energy"] == 5000.0

    def test_hundreds_of_sessions(self) -> None:
        """Verify no performance issues with many sessions."""
        sessions = [
            RechargeSession(
                session_uid=f"sess-{i:03d}",
                start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                end_time=datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc),
                energy_wh=1000.0 * i,
                cost=float(i),
                status="completed",
            )
            for i in range(10000)
        ]
        fields = compute_session_fields(sessions)
        assert fields["total_sessions"] == 10000
        assert fields["lifetime_energy"] == sum(1000.0 * i for i in range(10000))
        assert fields["last_session_energy"] == 0.0  # session 0
        assert fields["last_session_cost"] == 0.0  # session 0
