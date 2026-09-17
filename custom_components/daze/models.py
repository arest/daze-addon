"""Data models for the Daze Wallbox integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ------------------------------------------------------------------
# Recharge session model
# ------------------------------------------------------------------


@dataclass
class RechargeSession:
    """A single recharge session from the Daze API.

    All fields are optional since the API response structure may vary
    (e.g., in-progress sessions lack end_time and cost).
    """

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
        """Create a RechargeSession from the raw API response dict.

        Handles missing and None fields gracefully so partial sessions
        (in-progress) and unexpected API shapes don't crash.
        """
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
        """Return True if this session has not ended."""
        return self.end_time is None and self.status != "completed"


def _parse_datetime(value: Any) -> datetime | None:
    """Try to parse a datetime string from the API.

    The Daze API may return ISO 8601 strings, timestamps, or None.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)

    # Try ISO 8601 string parsing
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass

    return None


def _safe_float(value: Any) -> float | None:
    """Safely convert a value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
