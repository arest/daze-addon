"""DataUpdateCoordinator for Daze Wallbox integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import ApiAuthError, ApiError, DazeApiClient
from .api.auth import DazeAuthClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_NETWORK_UID,
    CONF_REFRESH_TOKEN,
    CONF_SERIAL_NUMBER,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

type DazeCoordinatorData = dict[str, Any]


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

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{serial_number}",
            update_interval=timedelta(seconds=poll_interval),
        )

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
        """Fetch the latest socket remote info data.

        Returns:
            A dict with all socket remote info fields.

        Raises:
            ConfigEntryAuthFailed: If the 401 retry also fails (refresh
                token expired) — triggers the HA re-auth flow.
            UpdateFailed: For transient API or network errors.

        """
        try:
            data = await self._api_client.async_get_socket_remote_info(
                self._serial_number
            )
            _LOGGER.debug(
                "Coordinator fetched socket data for %s: %s",
                self._serial_number,
                data,
            )
            return data

        except ApiAuthError as err:
            # Refresh token is invalid/expired — trigger re-auth
            _LOGGER.warning(
                "Authentication failed during coordinator update: %s",
                err,
            )
            raise ConfigEntryAuthFailed(
                "Authentication failed, re-authentication required"
            ) from err

        except ApiError as err:
            # Transient error — coordinator will retry
            _LOGGER.warning(
                "API error during coordinator update: %s", err
            )
            raise UpdateFailed(str(err)) from err

        except Exception as err:
            _LOGGER.exception(
                "Unexpected error during coordinator update"
            )
            raise UpdateFailed(str(err)) from err


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
