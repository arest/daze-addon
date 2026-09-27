"""Async REST API client for the Daze Wallbox web API."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from aiohttp import ClientSession
from aiohttp.client_exceptions import ClientError

from ..const import API_BASE_URL
from .auth import (
    AuthError,
    DazeAuthClient,
    async_fetch_user,
    describe_get_user_error,
)

_LOGGER = logging.getLogger(__name__)

# The command RPC fails intermittently and needs more attempts than
# is comfortable: around six were observed before a start or stop
# took effect. Eight tries 1.5s apart gives headroom over that while
# keeping the worst case near fifteen seconds, which is tolerable
# for a command that physically switches a charger.
COMMAND_RETRY_ATTEMPTS = 8
COMMAND_RETRY_DELAY = 1.5


class ApiAuthError(Exception):
    """Raised when the API returns 401 after a token refresh attempt.

    This signals that the refresh token itself is invalid/expired,
    triggering the re-authentication flow in Home Assistant.
    """


class ApiError(Exception):
    """Raised for non-auth API errors (4xx, 5xx, network issues)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        """Store the HTTP status alongside the message."""
        super().__init__(message)
        self.status = status


class ApiCommandRejectedError(ApiError):
    """Raised when the charger refuses a command it cannot perform.

    Distinguished from a generic ApiError so callers can tell the user
    what the charger is objecting to, rather than repeating a stock
    "check the car is connected" for every failure.
    """

    def __init__(self, message: str, *, code: int | None = None) -> None:
        """Store the upstream error code alongside the message."""
        super().__init__(message)
        self.code = code


# Upstream error codes seen from the command endpoints, with what each
# actually meant when observed.
COMMAND_ERROR_CODE_RPC_FAILURE = 101
COMMAND_ERROR_CODE_WRONG_SESSION = 4121

COMMAND_ERROR_HINTS: dict[int, str] = {
    # Sent with no session ID, or naming a session that is not paused.
    COMMAND_ERROR_CODE_WRONG_SESSION: (
        "there is no paused charging session to resume"
    ),
    # The Daze service could not reach the wallbox over its own RPC
    # link. Intermittent: the same command succeeds on a later attempt,
    # which is why the vendor app needs several presses. Retried
    # rather than reported.
    COMMAND_ERROR_CODE_RPC_FAILURE: (
        "the Daze service could not reach the wallbox"
    ),
}


def _code_and_hint_from_error(err: Exception) -> tuple[int | None, str]:
    """Recover the upstream error code from a raised ApiError.

    The request layer folds the response body into the exception
    message, so the structured code has to be read back out of it.
    """
    match = re.search(r'"code"\s*:\s*(\d+)', str(err))
    if not match:
        return None, ""

    code = int(match.group(1))
    return code, COMMAND_ERROR_HINTS.get(code, "")


class ApiNotFoundError(ApiError):
    """Raised when the API answers 404 for a resource.

    Subclasses ApiError so existing handlers keep working, while letting
    callers treat a missing resource as a durable condition rather than
    a transient failure worth retrying every poll.
    """


def _flatten_user_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Cognito GetUser response into a plain attribute dict.

    GetUser returns attributes as a list of ``{"Name": ..., "Value": ...}``
    entries. Callers expect a mapping, so convert it and carry the
    username across as well.

    Args:
        payload: The parsed GetUser response body.

    Returns:
        A dict mapping attribute names to values.

    """
    attributes: dict[str, Any] = {
        attr["Name"]: attr.get("Value")
        for attr in payload.get("UserAttributes", [])
        if isinstance(attr, dict) and attr.get("Name")
    }

    username = payload.get("Username")
    if username:
        attributes.setdefault("username", username)

    return attributes


class DazeApiClient:
    """Async REST client for the Daze web API.

    Wraps all Daze REST endpoints, handles token refresh on 401, and
    classifies errors for the caller.
    """

    def __init__(
        self,
        auth_client: DazeAuthClient,
        session: ClientSession,
    ) -> None:
        """Initialise the API client.

        Args:
            auth_client: A DazeAuthClient instance for token management.
            session: An aiohttp ClientSession for HTTP requests.

        """
        self._auth = auth_client
        self._session = session

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def auth_client(self) -> DazeAuthClient:
        """Return the underlying auth client."""
        return self._auth

    # ------------------------------------------------------------------
    # Base request helper
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        log_errors: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Make an authenticated HTTP request with automatic token refresh.

        On the first 401 response, the access token is refreshed and the
        request is retried once. If the retry also returns 401,
        ApiAuthError is raised to trigger re-authentication.

        Args:
            method: HTTP method (GET, POST, etc.).
            url: Full URL for the request.
            log_errors: Whether to log failures here. Callers that
                retry set this to False so the retries stay quiet, and
                log once themselves if they finally give up.
            **kwargs: Additional arguments passed to aiohttp.request.

        Returns:
            Parsed JSON response body.

        Raises:
            ApiAuthError: If token refresh fails or retry still 401s.
            ApiError: For other HTTP errors or network failures.

        """
        headers = kwargs.pop("headers", {})
        headers.update(self._auth.get_headers())

        _LOGGER.debug("API request: %s %s", method, url)

        try:
            async with self._session.request(
                method, url, headers=headers, **kwargs
            ) as response:
                if response.status == 401:
                    _LOGGER.info(
                        "Received 401 on %s %s — attempting token refresh",
                        method,
                        url,
                    )
                    return await self._handle_401(method, url, **kwargs)

                if response.status == 404:
                    # Logged by the caller, which knows whether a
                    # missing resource is expected. Logging here too
                    # would duplicate every message.
                    raise ApiNotFoundError(
                        f"API {method} {url} returned 404"
                    )

                if response.status >= 400:
                    body = await response.text()
                    if log_errors:
                        _LOGGER.warning(
                            "API error (HTTP %s) on %s %s: %s",
                            response.status,
                            method,
                            url,
                            body,
                        )
                    else:
                        _LOGGER.debug(
                            "API error (HTTP %s) on %s %s, handled by "
                            "caller: %s",
                            response.status,
                            method,
                            url,
                            body,
                        )
                    raise ApiError(
                        f"API {method} {url} failed with status "
                        f"{response.status}: {body}",
                        status=response.status,
                    )

                return await response.json()

        except ClientError as err:
            _LOGGER.warning(
                "Network error on %s %s: %s", method, url, err
            )
            raise ApiError(
                f"Network error on {method} {url}: {err}"
            ) from err

    async def _handle_401(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Any:
        """Handle a 401 by refreshing the token and retrying once.

        Args:
            method: HTTP method being retried.
            url: URL being retried.
            **kwargs: Request kwargs (without headers).

        Returns:
            Parsed JSON from the retried request.

        Raises:
            ApiAuthError: If refresh fails or retry also 401s.

        """
        try:
            await self._auth.async_refresh_access_token(self._session)
        except AuthError as err:
            _LOGGER.warning(
                "Token refresh failed — triggering re-auth: %s", err
            )
            raise ApiAuthError(
                "Token refresh failed, re-authentication required"
            ) from err

        # Retry with new token
        headers = kwargs.pop("headers", {})
        headers.update(self._auth.get_headers())

        _LOGGER.debug("Retrying %s %s after token refresh", method, url)

        try:
            async with self._session.request(
                method, url, headers=headers, **kwargs
            ) as response:
                if response.status == 401:
                    # Double 401 — refresh token is invalid
                    body = await response.text()
                    _LOGGER.warning(
                        "Retry also returned 401 on %s %s: %s — "
                        "refresh token is invalid",
                        method,
                        url,
                        body,
                    )
                    raise ApiAuthError(
                        "Authentication failed after token refresh, "
                        "re-authentication required"
                    )

                if response.status == 404:
                    raise ApiNotFoundError(
                        f"API {method} {url} returned 404"
                    )

                if response.status >= 400:
                    body = await response.text()
                    _LOGGER.warning(
                        "API error (HTTP %s) on retry %s %s: %s",
                        response.status,
                        method,
                        url,
                        body,
                    )
                    raise ApiError(
                        f"API {method} {url} failed with status "
                        f"{response.status}: {body}",
                        status=response.status,
                    )

                return await response.json()

        except ClientError as err:
            _LOGGER.warning(
                "Network error on retry %s %s: %s", method, url, err
            )
            raise ApiError(
                f"Network error on retry {method} {url}: {err}"
            ) from err

    # ------------------------------------------------------------------
    # API endpoints
    # ------------------------------------------------------------------

    async def async_get_user_info(self) -> dict[str, Any]:
        """Fetch the signed-in user's profile from Cognito.

        Uses the user pool GetUser operation rather than the hosted-UI
        userInfo endpoint, which rejects the scope Daze issues. See
        ``async_fetch_user`` for the details.

        The GetUser attribute list is flattened into a plain dict so
        callers can read ``email`` directly, matching the shape the
        previous userInfo call returned.

        Because GetUser signals an invalid token with HTTP 400 rather
        than HTTP 401, the generic ``_request`` retry path does not
        apply; the refresh-and-retry is handled explicitly here.

        Returns:
            A dict of user attributes, including ``email``.

        Raises:
            ApiAuthError: If the token is rejected and refreshing it
                does not recover access.

        """
        status, body = await async_fetch_user(
            self._session, self._auth.access_token
        )

        if status != 200:
            _LOGGER.info(
                "GetUser rejected the access token (%s) — refreshing",
                describe_get_user_error(status, body),
            )

            try:
                await self._auth.async_refresh_access_token(self._session)
            except AuthError as err:
                raise ApiAuthError(
                    "Token refresh failed, re-authentication required"
                ) from err

            status, body = await async_fetch_user(
                self._session, self._auth.access_token
            )

            if status != 200:
                detail = describe_get_user_error(status, body)
                _LOGGER.warning("GetUser failed after refresh (%s)", detail)
                raise ApiAuthError(
                    "Authentication failed after token refresh, "
                    "re-authentication required"
                )

        return _flatten_user_attributes(body)

    async def async_get_networks(
        self, email: str
    ) -> list[dict[str, Any]]:
        """Fetch the list of networks (installations) for a user.

        GET /v3/users/{email}/networks?includeStats=true

        Args:
            email: The user's email address from Cognito userInfo.

        Returns:
            List of network objects.

        """
        url = f"{API_BASE_URL}/users/{email}/networks?includeStats=true"
        data = await self._request("GET", url)
        return data.get("data", [])

    async def async_get_evses(
        self, network_uid: str
    ) -> list[dict[str, Any]]:
        """Fetch EVSEs (chargers) for a given network.

        GET /v3/networks/{uid}/evses?includeEcoInfo=false

        Args:
            network_uid: The unique ID of the network.

        Returns:
            List of EVSE objects.

        """
        url = f"{API_BASE_URL}/networks/{network_uid}/evses?includeEcoInfo=false"
        data = await self._request("GET", url)
        return data.get("data", [])

    async def async_get_evse_record(
        self, network_uid: str, serial: str
    ) -> dict[str, Any]:
        """Fetch the charger record for one serial number.

        The socket remoteInfo response carries the live session but not
        the charger's temperatures, grid limits, eco mode or configured
        current. Those live here, so the coordinator needs both.

        Args:
            network_uid: The unique ID of the network.
            serial: The serial number to select from the network.

        Returns:
            The matching EVSE record, or an empty dict if absent.

        """
        url = f"{API_BASE_URL}/networks/{network_uid}/evses?includeEcoInfo=true"
        data = await self._request("GET", url)

        for evse in data.get("data", []):
            if isinstance(evse, dict) and evse.get("serialNumber") == serial:
                return evse

        return {}

    async def async_get_socket_remote_info(
        self, serial: str
    ) -> dict[str, Any]:
        """Fetch live socket remote info (metrics, state).

        GET /v3/sockets/{serial}/remoteInfo?includeEcoInfo=true&includeNextSchedule=true

        Args:
            serial: The serial number of the wallbox.

        Returns:
            Socket remote info data dict.

        """
        url = (
            f"{API_BASE_URL}/sockets/{serial}/remoteInfo"
            "?includeEcoInfo=true&includeNextSchedule=true"
        )
        data = await self._request("GET", url)
        return data.get("data", {})

    async def async_set_max_charging_current(
        self, serial: str, current_ma: int
    ) -> dict[str, Any]:
        """Set the maximum charging current for a wallbox.

        POST /v3/evses/{serial}/configurations/maxExternalChargingCurrent

        Args:
            serial: The serial number of the wallbox.
            current_ma: The current limit in milliamps.

        Returns:
            The response dict.

        """
        url = (
            f"{API_BASE_URL}/evses/{serial}"
            "/configurations/maxExternalChargingCurrent"
        )
        payload = {
            "evseSerialNumber": serial,
            "maxExternalChargingCurrentInMilliAmps": current_ma,
        }
        return await self._request("POST", url, json=payload)

    async def async_set_eco_mode(
        self, serial: str, eco_mode_enabled: bool
    ) -> dict[str, Any]:
        """Enable or disable eco mode on a wallbox.

        POST /v3/evses/{serial}/configurations/ecoMode

        Args:
            serial: The serial number of the wallbox.
            eco_mode_enabled: True to enable eco mode, False to disable.

        Returns:
            The response dict.

        """
        url = (
            f"{API_BASE_URL}/evses/{serial}"
            "/configurations/ecoMode"
        )
        payload = {
            "evseSerialNumber": serial,
            "ecoModeEnabled": eco_mode_enabled,
        }
        return await self._request("POST", url, json=payload)

    async def _post_command(
        self,
        url: str,
        payload: dict[str, Any],
        attempts: int = COMMAND_RETRY_ATTEMPTS,
        delay: float = COMMAND_RETRY_DELAY,
    ) -> dict[str, Any]:
        """POST a charger command, retrying transient RPC failures.

        The Daze service relays commands to the wallbox over its own
        RPC link, and that link fails intermittently with HTTP 500 and
        error code 101. The same command succeeds on a later attempt:
        roughly six were needed in observed cases. This is why the
        vendor app appears to need several presses to start or stop a
        charge, and why reporting an error after one attempt made the
        integration look broken when it was not.

        Only that failure is retried. A wrong-session rejection (4121)
        is deterministic, so it is surfaced immediately.

        Args:
            url: The command endpoint.
            payload: The JSON body.
            attempts: Total tries, including the first.
            delay: Seconds to wait between tries.

        Returns:
            The response from the first attempt that succeeds.

        Raises:
            ApiCommandRejectedError: If the charger refused the command,
                or if every attempt hit the transient failure.

        """
        last_error: ApiError | None = None

        for attempt_number in range(1, attempts + 1):
            try:
                result = await self._request(
                    "POST", url, log_errors=False, json=payload
                )
            except ApiAuthError:
                raise
            except ApiError as err:
                last_error = err
                code, hint = _code_and_hint_from_error(err)

                retryable = code == COMMAND_ERROR_CODE_RPC_FAILURE or (
                    code is None and (getattr(err, "status", None) or 0) >= 500
                )

                if not retryable:
                    if hint:
                        raise ApiCommandRejectedError(
                            f"The charger refused the command because "
                            f"{hint}.",
                            code=code,
                        ) from err
                    raise

                if attempt_number < attempts:
                    _LOGGER.debug(
                        "Transient RPC failure on attempt %d of %d, "
                        "retrying in %.1fs",
                        attempt_number,
                        attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
            else:
                if attempt_number > 1:
                    _LOGGER.info(
                        "Command succeeded on attempt %d of %d",
                        attempt_number,
                        attempts,
                    )
                return result

        code, _ = _code_and_hint_from_error(last_error or Exception())
        _LOGGER.warning(
            "Command %s gave up after %d attempts over %.0fs. The Daze "
            "service could not reach the wallbox. Last error: %s",
            url.rsplit("/", 1)[-1],
            attempts,
            (attempts - 1) * delay,
            last_error,
        )
        raise ApiCommandRejectedError(
            f"The Daze service could not reach the wallbox after "
            f"{attempts} attempts. This is intermittent rather than a "
            "fault; try again shortly.",
            code=code,
        ) from last_error

    async def async_start_charge(
        self, serial: str, session_id: int | None = None
    ) -> dict[str, Any]:
        """Resume charging on a wallbox.

        POST /v3/sockets/{serial}/commands/playcharge

        The body must carry both the serial and the session ID. All
        three behaviours below were observed against hardware:

        - empty body: HTTP 422, ``ErrorWrongSessionID`` (code 4121)
        - ``{"sessionId": ...}``: HTTP 200, but the charger stays
          paused. Accepted is not resumed.
        - ``{"evseSerialNumber": ..., "sessionId": ...}``: HTTP 200 and
          the charger resumes within about ten seconds, with evseState
          moving 6 to 5 and isPaused clearing.

        A paused session keeps its ID, so the caller reads it from the
        coordinator's ``sessionId`` field.

        Args:
            serial: The serial number of the wallbox.
            session_id: The session to resume. Omitted when unknown,
                which the API rejects with 422.

        Returns:
            The response dict.

        """
        url = f"{API_BASE_URL}/sockets/{serial}/commands/playcharge"
        payload: dict[str, Any] = {"evseSerialNumber": serial}
        if session_id is not None:
            payload["sessionId"] = session_id
        return await self._post_command(url, payload)

    async def async_stop_charge(
        self, serial: str, session_id: int | None = None
    ) -> dict[str, Any]:
        """Suspend charging on a wallbox.

        POST /v3/sockets/{serial}/commands/stopcharge

        Sends the same body shape as playcharge. The symmetry is
        assumed, not measured: only the resume direction has been
        verified against hardware.

        Args:
            serial: The serial number of the wallbox.
            session_id: The session to suspend, when known.

        Returns:
            The response dict.

        """
        url = f"{API_BASE_URL}/sockets/{serial}/commands/stopcharge"
        payload: dict[str, Any] = {"evseSerialNumber": serial}
        if session_id is not None:
            payload["sessionId"] = session_id
        return await self._post_command(url, payload)

    async def async_get_recharge_sessions(
        self, network_uid: str, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """Fetch recharge session history for a network.

        GET /v3/networks/{uid}/rechargeSessions

        Args:
            network_uid: The unique ID of the network.
            limit: Maximum number of sessions to return (default 1000).

        Returns:
            List of recharge session objects.

        """
        url = (
            f"{API_BASE_URL}/networks/{network_uid}"
            f"/rechargeSessions?TotalLimit={limit}"
        )
        data = await self._request("GET", url)
        return data.get("data", [])
