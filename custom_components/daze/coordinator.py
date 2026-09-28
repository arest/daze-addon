"""DataUpdateCoordinator for Daze Wallbox integration."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import ApiAuthError, ApiError, ApiNotFoundError, DazeApiClient
from .api.auth import DazeAuthClient
from .const import (
    BACKGROUND_RETRY_DELAYS,
    CONF_ACCESS_TOKEN,
    CONF_NETWORK_UID,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_SERIAL_NUMBER,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .models import RechargeSession
from .optimistic import OptimisticState
from .payload import merge_payload

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

# After a transient failure, try again sooner than the normal
# interval but not on every poll.
SESSION_ERROR_RETRY_INTERVAL = 60  # seconds

# The charger record holds configuration and slow-moving readings,
# so it does not need the live metric cadence.
EVSE_FETCH_INTERVAL = 120  # seconds

# A charger does not change state the instant a command is accepted.
# Starting passes through waiting-for-EV before charging, and pausing
# takes its own time to register. A single refresh straight after the
# command reads the old state and leaves the UI stale until the next
# ordinary poll, up to DEFAULT_POLL_INTERVAL later.
#
# These offsets re-read the charger over the following half minute so
# the entities follow the transition. They are scheduled rather than
# awaited, so a service call still returns promptly.
SETTLE_REFRESH_DELAYS = (3, 8, 15, 30)


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
        self._cached_evse: dict[str, Any] = {}
        self._next_evse_fetch: float = 0.0
        self._next_session_fetch: float = 0.0
        self._sessions_missing_logged: bool = False
        self._pending_retries: dict[str, Callable[[], None]] = {}
        self._pending_timers: set[Callable[[], None]] = set()
        # The charging limit is one setting with two views, in amps
        # and in watts. Held here, in milliamps, so both entities
        # show a pending change at once instead of disagreeing
        # until the next refresh.
        self._limit_state = OptimisticState()
        self._limit_listeners: list[Callable[[], None]] = []

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

    def async_retry_in_background(
        self,
        key: str,
        action: Callable[[], Awaitable[Any]],
        description: str,
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        """Keep retrying a command after the user has stopped waiting.

        The Daze RPC link refuses commands for minutes at a time. Held
        open, that means a service call that blocks and then fails.
        Retried in the background, the command usually lands and the
        user never sees a failure at all.

        A second request for the same key replaces the first, so
        repeatedly nudging a control does not stack up retries.

        Args:
            key: Identifies the command, so a newer one supersedes it.
            action: Awaitable performing the command. Raising means
                the attempt failed.
            description: Used in log messages and the failure notice.
            on_failure: Called with a message when every attempt fails.

        """
        self.async_cancel_background_retry(key)

        attempts = list(BACKGROUND_RETRY_DELAYS)
        state = {"index": 0, "cancelled": False}

        async def _attempt(_now: Any) -> None:
            """Run one background attempt and schedule the next."""
            if state["cancelled"]:
                return

            index = state["index"]
            try:
                await action()
            except Exception as err:  # noqa: BLE001 - reported below
                state["index"] = index + 1

                if state["index"] < len(attempts):
                    delay = attempts[state["index"]]
                    _LOGGER.debug(
                        "Background retry %d/%d for %s failed (%s), "
                        "next in %ss",
                        index + 1,
                        len(attempts),
                        description,
                        err,
                        delay,
                    )
                    _schedule(delay)
                    return

                self._pending_retries.pop(key, None)
                _LOGGER.warning(
                    "%s never succeeded after %d background attempts: %s",
                    description,
                    len(attempts),
                    err,
                )
                if on_failure is not None:
                    on_failure(
                        f"{description} could not be delivered to the "
                        f"charger. The Daze service was unreachable for "
                        f"several minutes."
                    )
                return

            self._pending_retries.pop(key, None)
            _LOGGER.info(
                "%s succeeded on background attempt %d", description, index + 1
            )
            await self.async_request_refresh()

        def _schedule(delay: int) -> None:
            """Queue the next attempt and remember how to cancel it."""
            cancel = async_call_later(self.hass, delay, _attempt)

            def _cancel() -> None:
                state["cancelled"] = True
                cancel()

            self._pending_retries[key] = _cancel

        _LOGGER.info(
            "%s did not reach the charger; retrying in the background "
            "over the next %d seconds",
            description,
            sum(attempts),
        )
        _schedule(attempts[0])

    @property
    def limit_state(self) -> OptimisticState:
        """Return the shared pending charging limit, in milliamps."""
        return self._limit_state

    def async_add_limit_listener(
        self, listener: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a callback for changes to the pending limit.

        Args:
            listener: Called when the pending limit changes.

        Returns:
            A callable that unregisters the listener.

        """
        self._limit_listeners.append(listener)

        def _remove() -> None:
            if listener in self._limit_listeners:
                self._limit_listeners.remove(listener)

        return _remove

    def async_notify_limit_listeners(self) -> None:
        """Tell both views of the limit to redraw.

        Called after one of them requests a change, so the other does
        not keep showing the previous value until the next poll.
        """
        for listener in list(self._limit_listeners):
            listener()

    def async_shutdown_timers(self) -> None:
        """Cancel every callback this coordinator has scheduled.

        Settle refreshes and background retries outlive the code that
        scheduled them. Unloading the entry, which also happens on
        every options change, otherwise leaves them firing against a
        discarded coordinator and a closed API client. Home Assistant
        reports those as lingering timers.
        """
        for cancel in list(self._pending_timers):
            cancel()
        self._pending_timers.clear()

        for key in list(self._pending_retries):
            self.async_cancel_background_retry(key)

        self._limit_listeners.clear()

        _LOGGER.debug("Cancelled pending timers for %s", self._serial_number)

    def async_cancel_background_retry(self, key: str) -> None:
        """Drop any pending background retry for a command."""
        cancel = self._pending_retries.pop(key, None)
        if cancel is not None:
            cancel()

    def _schedule_tracked_refresh(self, delay: int, reason: str) -> None:
        """Schedule one refresh and keep a handle so it can be cancelled.

        Each call gets its own scope. Scheduling inside a loop and
        closing over the loop variable would leave every callback
        discarding the last handle rather than its own.

        Args:
            delay: Seconds until the refresh runs.
            reason: Used in the debug log.

        """
        handle: list[Any] = []

        async def _refresh(_now: Any) -> None:
            """Re-read the charger."""
            if handle:
                self._pending_timers.discard(handle[0])

            _LOGGER.debug(
                "%s refresh for %s at +%ss",
                reason,
                self._serial_number,
                delay,
            )
            # The charging current and eco mode live only in the EVSE
            # record, which is cached. Without dropping that cache the
            # refresh re-reads a stale copy and the entity reverts.
            self._next_evse_fetch = 0.0
            await self.async_request_refresh()

        cancel = async_call_later(self.hass, delay, _refresh)
        handle.append(cancel)
        self._pending_timers.add(cancel)

    def async_schedule_refresh_in(self, delay: int) -> None:
        """Re-read the charger once, after a delay.

        Used after a command. Refreshing immediately reads the state
        from before the change, because the cloud API lags the charger
        by several seconds.

        Scheduled, not awaited: the caller returns immediately.
        """
        self._next_evse_fetch = 0.0
        self._schedule_tracked_refresh(delay, "Post-command")

    def async_schedule_settle_refresh(self) -> None:
        """Re-read the charger a few times after a command.

        Commands take effect asynchronously: the charger moves through
        intermediate states for several seconds. Refreshing once
        immediately captures the state before the change, so schedule
        further reads across the transition.

        Scheduled, not awaited: the caller returns immediately.
        """
        self._next_evse_fetch = 0.0

        for delay in SETTLE_REFRESH_DELAYS:
            self._schedule_tracked_refresh(delay, "Settle")

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
            remote_info = await self._api_client.async_get_socket_remote_info(
                self._serial_number
            )

            # The EVSE record supplies temperatures, grid limits, eco
            # mode and the configured current, none of which appear in
            # remoteInfo. It changes slowly, so it is cached.
            if time.time() >= self._next_evse_fetch:
                try:
                    self._cached_evse = (
                        await self._api_client.async_get_evse_record(
                            self._network_uid, self._serial_number
                        )
                    )
                except ApiError as err:
                    _LOGGER.debug(
                        "Could not refresh the EVSE record: %s", err
                    )
                else:
                    self._next_evse_fetch = time.time() + EVSE_FETCH_INTERVAL

            data = merge_payload(remote_info, self._cached_evse)

            _LOGGER.debug(
                "Coordinator fetched socket data for %s (%d fields)",
                self._serial_number,
                len(data),
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
            fetched = await self._async_fetch_sessions()
            if fetched is not None:
                self._cached_sessions = fetched

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
    ) -> list[RechargeSession] | None:
        """Fetch recharge session history.

        Returns None on failure rather than an empty list. An empty
        list is a real answer meaning "no sessions", and assigning it
        over a good history resets lifetime_energy to zero. That sensor
        is total_increasing, so Home Assistant reads the drop as a
        meter reset and double counts on recovery.

        Returns:
            The sessions, or None if they could not be fetched.

        """
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
            # Armed only on success: arming first meant a transient
            # error silently froze the history for five minutes.
            self._next_session_fetch = time.time() + SESSION_FETCH_INTERVAL
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

            # A missing endpoint genuinely means no history, unlike a
            # transient error, so an empty list is the right answer.
            return []

        except ApiAuthError:
            # Auth errors on session endpoint are unexpected (the
            # socket fetch already validated the token), but handle
            # gracefully — don't double-trigger re-auth.
            _LOGGER.warning(
                "Auth error fetching sessions for %s — keeping the "
                "previous history",
                self._serial_number,
            )
            self._next_session_fetch = (
                time.time() + SESSION_ERROR_RETRY_INTERVAL
            )
            return None

        except ApiError as err:
            _LOGGER.warning(
                "API error fetching sessions for %s: %s — keeping the "
                "previous history",
                self._serial_number,
                err,
            )
            self._next_session_fetch = (
                time.time() + SESSION_ERROR_RETRY_INTERVAL
            )
            return None

        except Exception:
            _LOGGER.exception(
                "Unexpected error fetching sessions for %s",
                self._serial_number,
            )
            self._next_session_fetch = (
                time.time() + SESSION_ERROR_RETRY_INTERVAL
            )
            return None


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
    # Options win over the value captured at setup, so changing the
    # interval takes effect on reload without reconfiguring.
    poll_interval = entry.options.get(
        CONF_POLL_INTERVAL,
        entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
    )
    try:
        poll_interval = int(poll_interval)
    except (TypeError, ValueError):
        poll_interval = DEFAULT_POLL_INTERVAL

    poll_interval = max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, poll_interval))

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
        poll_interval=poll_interval,
    )

    _LOGGER.debug(
        "Coordinator for %s polling every %ss", serial_number, poll_interval
    )

    # Perform first refresh to populate coordinator data
    await coordinator.async_config_entry_first_refresh()

    return coordinator
