"""DataUpdateCoordinator for Daze Wallbox integration."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import ApiAuthError, ApiError, ApiNotFoundError, DazeApiClient
from .api.auth import DazeAuthClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_NETWORK_UID,
    CONF_REFRESH_TOKEN,
    CONF_SERIAL_NUMBER,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .models import RechargeSession

_LOGGER = logging.getLogger(__name__)

type DazeCoordinatorData = dict[str, Any]

# Session history changes only when a charge ends, so it does not need
# the live metric cadence. Re-requesting the full history on every poll
# was wasteful and risked upstream rate limiting.
SESSION_FETCH_INTERVAL = 300  # seconds

# Some accounts get HTTP 404 from the recharge-session endpoint. That is
# a durable condition, not a transient error, so back off hard instead
# of retrying every poll and filling the log with warnings.
SESSION_MISSING_RETRY_INTERVAL = 3600  # seconds


class DazeDataUpdateCoordinator(
    DataUpdateCoordinator[DazeCoordinatorData]
):
    """Coordinator for polling Daze wallbox socket data.

    Fetches live metrics from the socket remoteInfo endpoint at a
    configurable interval and propagates the data to all sensor, switch,
    number, and select entities.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api_client: DazeApiClient,
        serial_number: str,
        network_uid: str,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ) -> None:
        """Initialise the coordinator.

        Args:
            hass: The HomeAssistant instance.
            api_client: An authenticated DazeApiClient.
            serial_number: The wallbox serial number.
            network_uid: The network UID for the wallbox.
            poll_interval: Polling interval in seconds (default 30).

        """
        self._api_client = api_client
        self._serial_number = serial_number
        self._network_uid = network_uid
        self._last_success_time: float | None = None
        self._last_fail_time: float | None = None
        self._total_updates: int = 0
        self._consecutive_failures: int = 0
        self._cached_sessions: list[RechargeSession] = []
        self._next_session_fetch: float = 0.0
        self._sessions_missing_logged: bool = False

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{serial_number}",
            update_interval=timedelta(seconds=poll_interval),
        )

    @property
    def last_success_time(self) -> float | None:
        """Return UNIX timestamp of the last successful update."""
        return self._last_success_time

    @property
    def last_fail_time(self) -> float | None:
        """Return UNIX timestamp of the last failed update."""
        return self._last_fail_time

    @property
    def total_updates(self) -> int:
        """Return total number of coordinator update attempts."""
        return self._total_updates

    @property
    def consecutive_failures(self) -> int:
        """Return consecutive failed update count."""
        return self._consecutive_failures

    @property
    def api_client(self) -> DazeApiClient:
        """Return the underlying API client."""
        return self._api_client

    @property
    def serial_number(self) -> str:
        """Return the wallbox serial number."""
        return self._serial_number

    @property
    def network_uid(self) -> str:
        """Return the network UID."""
        return self._network_uid

    async def _async_update_data(self) -> DazeCoordinatorData:
        """Fetch the latest socket remote info and session data.

        Socket data is fetched first — if it fails, the coordinator
        raises (auth triggers re-auth, other errors are transient).
        Session data is a secondary fetch; failures are logged but
        do NOT fail the coordinator so live sensor data keeps working.

        Returns:
            A dict with socket remote info fields plus a ``sessions``
            key containing a list of ``RechargeSession`` objects.

        Raises:
            ConfigEntryAuthFailed: If the 401 retry also fails (refresh
                token expired) — triggers the HA re-auth flow.
            UpdateFailed: For transient API or network errors.

        """
        self._total_updates += 1

        try:
            data = await self._api_client.async_get_socket_remote_info(
                self._serial_number
            )
            _LOGGER.debug(
                "Coordinator fetched socket data for %s",
                self._serial_number,
            )
        except ApiAuthError as err:
            self._last_fail_time = time.time()
            self._consecutive_failures += 1
            _LOGGER.warning(
                "Authentication failed during coordinator update: %s",
                err,
            )
            raise ConfigEntryAuthFailed(
                "Authentication failed, re-authentication required"
            ) from err

        except ApiError as err:
            self._last_fail_time = time.time()
            self._consecutive_failures += 1
            _LOGGER.warning(
                "API error during coordinator update: %s", err
            )
            raise UpdateFailed(str(err)) from err

        except Exception as err:
            self._last_fail_time = time.time()
            self._consecutive_failures += 1
            _LOGGER.exception(
                "Unexpected error during coordinator update"
            )
            raise UpdateFailed(str(err)) from err

        # Track success
        self._last_success_time = time.time()
        self._consecutive_failures = 0

        # Fetch session data (secondary — failures are non-fatal).
        # Throttled: history only changes when a charge ends.
        if time.time() >= self._next_session_fetch:
            self._cached_sessions = await self._async_fetch_sessions()

        sessions = self._cached_sessions
        data["sessions"] = sessions
        data.update(self._compute_session_fields(sessions))

        _LOGGER.debug(
            "Coordinator data for %s: %d sessions loaded",
            self._serial_number,
            len(sessions),
        )

        return data

    @staticmethod
    def _compute_session_fields(
        sessions: list[RechargeSession],
    ) -> dict[str, Any]:
        """Compute derived session sensor values from session list.

        Sessions are expected newest-first. "Last session" is the
        first entry (index 0).

        Returns:
            A dict of computed fields to merge into coordinator data.

        """
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
            # In-progress session — duration since start
            delta = datetime.now(timezone.utc) - last.start_time
            fields["last_session_duration"] = delta.total_seconds() / 60.0

        # Compute lifetime energy from all sessions
        lifetime = 0.0
        for ses in sessions:
            if ses.energy_wh is not None:
                lifetime += ses.energy_wh
        fields["lifetime_energy"] = lifetime

        return fields

    async def _async_fetch_sessions(
        self,
    ) -> list[RechargeSession]:
        """Fetch recharge session history.

        Failures are logged and return an empty list — the coordinator
        continues to work with live socket data even if sessions are
        temporarily unavailable.

        Returns:
            A list of RechargeSession objects (may be empty).

        """
        self._next_session_fetch = time.time() + SESSION_FETCH_INTERVAL

        try:
            sessions_raw = (
                await self._api_client.async_get_recharge_sessions(
                    self._network_uid,
                )
            )
            _LOGGER.debug(
                "Fetched %d recharge sessions for network %s",
                len(sessions_raw),
                self._network_uid,
            )
            self._sessions_missing_logged = False
            return [
                RechargeSession.from_dict(s) for s in sessions_raw
            ]

        except ApiNotFoundError:
            # The endpoint is absent for this account. Say so once, then
            # back off: retrying every poll only spams the log.
            self._next_session_fetch = (
                time.time() + SESSION_MISSING_RETRY_INTERVAL
            )

            if not self._sessions_missing_logged:
                self._sessions_missing_logged = True
                _LOGGER.info(
                    "Recharge session history is unavailable for network "
                    "%s (the API returned 404). Session and lifetime "
                    "sensors will stay empty; live metrics and charge "
                    "control are unaffected. Retrying hourly.",
                    self._network_uid,
                )

            return []

        except ApiAuthError:
            # Auth errors on session endpoint are unexpected (the
            # socket fetch already validated the token), but handle
            # gracefully — don't double-trigger re-auth.
            _LOGGER.warning(
                "Auth error fetching sessions for %s — sessions "
                "unavailable until next poll",
                self._serial_number,
            )
            return []

        except ApiError as err:
            _LOGGER.warning(
                "API error fetching sessions for %s: %s — "
                "sessions unavailable until next poll",
                self._serial_number,
                err,
            )
            return []

        except Exception:
            _LOGGER.exception(
                "Unexpected error fetching sessions for %s",
                self._serial_number,
            )
            return []


async def async_setup_coordinator(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> DazeDataUpdateCoordinator:
    """Create and start the DataUpdateCoordinator for a config entry.

    This instantiates the auth client, API client, and coordinator, then
    performs the first refresh so device registry data is available.

    Args:
        hass: The HomeAssistant instance.
        entry: The config entry containing tokens and device info.

    Returns:
        The initialised DazeDataUpdateCoordinator.

    """
    access_token = entry.data[CONF_ACCESS_TOKEN]
    refresh_token = entry.data[CONF_REFRESH_TOKEN]
    serial_number = entry.data[CONF_SERIAL_NUMBER]
    network_uid = entry.data[CONF_NETWORK_UID]

    session = async_get_clientsession(hass)
    auth_client = DazeAuthClient(access_token, refresh_token)
    api_client = DazeApiClient(auth_client, session)

    coordinator = DazeDataUpdateCoordinator(
        hass=hass,
        api_client=api_client,
        serial_number=serial_number,
        network_uid=network_uid,
    )

    # Perform first refresh to populate coordinator data
    await coordinator.async_config_entry_first_refresh()

    return coordinator
