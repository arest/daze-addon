"""Daze OAuth 2.0 authentication client for Amazon Cognito."""

from __future__ import annotations

import logging
import time
from typing import Any

from aiohttp import ClientSession
from aiohttp.client_exceptions import ClientError

from ..const import (
    CLIENT_ID,
    COGNITO_BASE_URL,
    COGNITO_IDP_URL,
    DEFAULT_TOKEN_EXPIRY_BUFFER,
    GET_USER_CONTENT_TYPE,
    GET_USER_TARGET,
    REDIRECT_URI,
)

_LOGGER = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when authentication fails (invalid/expired tokens, network error)."""


async def async_fetch_user(
    session: ClientSession, access_token: str
) -> tuple[int, dict[str, Any]]:
    """Call the Cognito user pool GetUser operation.

    This replaces the hosted-UI ``/oauth2/userInfo`` endpoint, which
    requires the ``openid`` scope. The Daze web portal issues access
    tokens scoped ``aws.cognito.signin.user.admin`` without ``openid``,
    so userInfo rejects every token a user can obtain. GetUser accepts
    that scope and returns the same profile attributes.

    Two differences from userInfo matter to callers:

    - The access token is sent in the request body, not in an
      ``Authorization`` header.
    - An invalid or expired token yields HTTP 400 with a
      ``NotAuthorizedException`` type, not HTTP 401.

    Args:
        session: An aiohttp ClientSession to use for the request.
        access_token: The Cognito access token to authenticate with.

    Returns:
        A tuple of the HTTP status code and the parsed JSON body. The
        body is an empty dict if the response was not valid JSON.

    Raises:
        AuthError: If the request fails at the network level.

    """
    headers = {
        "Content-Type": GET_USER_CONTENT_TYPE,
        "X-Amz-Target": GET_USER_TARGET,
    }

    try:
        async with session.post(
            COGNITO_IDP_URL,
            headers=headers,
            json={"AccessToken": access_token},
        ) as response:
            # Cognito replies with application/x-amz-json-1.1, which
            # aiohttp refuses to decode unless content_type is relaxed.
            try:
                body = await response.json(content_type=None)
            except (ValueError, TypeError):
                body = {}

            if not isinstance(body, dict):
                body = {}

            return response.status, body

    except ClientError as err:
        _LOGGER.warning("Network error during GetUser: %s", err)
        raise AuthError(f"Network error during GetUser: {err}") from err


def describe_get_user_error(status: int, body: dict[str, Any]) -> str:
    """Summarise a failed GetUser response without leaking the body.

    Args:
        status: The HTTP status code returned by Cognito.
        body: The parsed JSON body.

    Returns:
        A short, safe description for logs and error messages.

    """
    error_type = body.get("__type") or "unknown error"
    return f"HTTP {status}: {error_type}"


class DazeAuthClient:
    """Manages Daze Cognito OAuth token lifecycle.

    Stores the access and refresh tokens, checks expiry, performs token
    refresh, and validates tokens against the Cognito userInfo endpoint.
    """

    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        token_expiry: float | None = None,
    ) -> None:
        """Initialise the auth client.

        Args:
            access_token: The Cognito access token (Bearer token).
            refresh_token: The Cognito refresh token.
            token_expiry: Optional UNIX timestamp of the access token expiry.

        """
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expiry = token_expiry

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_headers(self) -> dict[str, str]:
        """Return the authorisation header dict for API calls."""
        return {"authorization": f"Bearer {self._access_token}"}

    def is_token_expired(self) -> bool:
        """Return True if the access token is expired or near expiry.

        Uses a 60-second buffer before the actual expiry time to allow
        for clock skew and request latency.
        """
        if self._token_expiry is None:
            return False
        return time.time() >= (self._token_expiry - DEFAULT_TOKEN_EXPIRY_BUFFER)

    async def async_refresh_access_token(
        self, session: ClientSession
    ) -> str:
        """Refresh the access token via Cognito OAuth2 token endpoint.

        Args:
            session: An aiohttp ClientSession to use for the request.

        Returns:
            The new access token string.

        Raises:
            AuthError: If the refresh token is expired, invalid, or a
                network error occurs.

        """
        url = f"{COGNITO_BASE_URL}/oauth2/token"
        data = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"
        }

        _LOGGER.debug("Refreshing access token via Cognito")

        try:
            async with session.post(
                url, headers=headers, data=data
            ) as response:
                if response.status in (400, 401):
                    body = await response.text()
                    _LOGGER.warning(
                        "Token refresh failed (HTTP %s): %s",
                        response.status,
                        body,
                    )
                    raise AuthError(
                        f"Token refresh failed with status {response.status}: "
                        f"{body}"
                    )

                if response.status != 200:
                    body = await response.text()
                    _LOGGER.error(
                        "Unexpected token refresh response (HTTP %s): %s",
                        response.status,
                        body,
                    )
                    raise AuthError(
                        f"Token refresh unexpected status {response.status}: "
                        f"{body}"
                    )

                payload: dict[str, Any] = await response.json()
                new_access_token: str = payload["access_token"]
                expires_in: int = payload.get("expires_in", 3600)

                # Update stored tokens
                self._access_token = new_access_token
                self._token_expiry = time.time() + expires_in

                # Cognito may rotate the refresh token (rare)
                if "refresh_token" in payload:
                    self._refresh_token = payload["refresh_token"]

                _LOGGER.debug(
                    "Access token refreshed successfully "
                    "(expires in %ss)",
                    expires_in,
                )

                return new_access_token

        except AuthError:
            raise
        except ClientError as err:
            _LOGGER.warning("Network error during token refresh: %s", err)
            raise AuthError(f"Network error during token refresh: {err}") from err

    async def async_validate_tokens(
        self, session: ClientSession
    ) -> bool:
        """Validate the stored access token against Cognito GetUser.

        Args:
            session: An aiohttp ClientSession to use for the request.

        Returns:
            True if the token is valid (HTTP 200).

        Raises:
            AuthError: If the token is invalid or a network error occurs.

        """
        _LOGGER.debug("Validating tokens via Cognito GetUser")

        status, body = await async_fetch_user(session, self._access_token)

        if status == 200:
            return True

        detail = describe_get_user_error(status, body)
        _LOGGER.warning("Token validation failed (%s)", detail)
        raise AuthError(f"Token validation failed, {detail}")

    @property
    def access_token(self) -> str:
        """Return the current access token."""
        return self._access_token

    @property
    def token_expiry(self) -> float | None:
        """Return the access token expiry UNIX timestamp."""
        return self._token_expiry

    def async_get_tokens_for_store(self) -> dict[str, Any]:
        """Return a dict safe for config entry storage.

        This does NOT include raw API responses or sensitive metadata.
        """
        return {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "token_expiry": self._token_expiry,
        }
