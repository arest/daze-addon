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
    DEFAULT_TOKEN_EXPIRY_BUFFER,
    REDIRECT_URI,
)

_LOGGER = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when authentication fails (invalid/expired tokens, network error)."""


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
        """Validate the stored access token against Cognito userInfo.

        Args:
            session: An aiohttp ClientSession to use for the request.

        Returns:
            True if the token is valid (HTTP 200).

        Raises:
            AuthError: If the token is invalid or a network error occurs.

        """
        url = f"{COGNITO_BASE_URL}/oauth2/userInfo"
        headers = self.get_headers()

        _LOGGER.debug("Validating tokens via Cognito userInfo")

        try:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    return True

                body = await response.text()
                _LOGGER.warning(
                    "Token validation failed (HTTP %s): %s",
                    response.status,
                    body,
                )
                raise AuthError(
                    f"Token validation failed with status {response.status}: "
                    f"{body}"
                )

        except AuthError:
            raise
        except ClientError as err:
            _LOGGER.warning(
                "Network error during token validation: %s", err
            )
            raise AuthError(
                f"Network error during token validation: {err}"
            ) from err

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
