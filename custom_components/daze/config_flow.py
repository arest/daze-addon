"""Config flow for Daze Wallbox integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DazeApiClient
from .api.auth import AuthError, DazeAuthClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_PROFILE,
    CONF_EMAIL,
    CONF_EVSE_NAME,
    CONF_FIRMWARE_VERSION,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_NETWORK_NAME,
    CONF_NETWORK_UID,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_SERIAL_NUMBER,
    CONF_SOFTWARE_VERSION,
    CONF_SUPPLY_PHASES,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    SUPPLY_PHASES_SINGLE,
    SUPPLY_PHASES_THREE,
)
from .payload import device_name

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCESS_TOKEN): str,
        vol.Required(CONF_REFRESH_TOKEN): str,
    }
)


async def _validate_tokens(
    hass: HomeAssistant, access_token: str, refresh_token: str
) -> dict[str, Any]:
    """Validate tokens and return user info.

    Returns a dict with keys:
        - "email": the user's email address
        - "access_token", "refresh_token" (passed through)

    Raises vol.Invalid if validation fails.
    """
    session = async_get_clientsession(hass)
    auth_client = DazeAuthClient(access_token, refresh_token)

    try:
        await auth_client.async_validate_tokens(session)
    except AuthError as err:
        _LOGGER.warning("Token validation failed: %s", err)
        raise vol.Invalid("invalid_token") from err

    # Fetch user info to get the email address
    api = DazeApiClient(auth_client, session)
    user_info = await api.async_get_user_info()

    return {
        "email": user_info.get("email", ""),
        CONF_ACCESS_TOKEN: access_token,
        CONF_REFRESH_TOKEN: refresh_token,
    }


async def _fetch_networks(
    hass: HomeAssistant,
    api_client: DazeApiClient,
    email: str,
) -> list[dict[str, Any]]:
    """Fetch available networks for the user."""
    try:
        return await api_client.async_get_networks(email)
    except Exception as err:
        _LOGGER.error("Failed to fetch networks: %s", err)
        raise vol.Invalid("network_error") from err


class DazeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Daze Wallbox."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the config flow."""
        self._reauth_entry: ConfigEntry | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._email: str | None = None
        self._networks: list[dict[str, Any]] = []
        self._network_uid: str | None = None
        self._network_name: str | None = None
        self._evse_name: str | None = None
        self._serial_number: str | None = None
        self._device_profile: str | None = None
        self._firmware_version: str | None = None
        self._software_version: str | None = None

    # ------------------------------------------------------------------
    # Step 1: Token entry
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — token entry.

        The user provides their Daze access token and refresh token.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            access_token = user_input[CONF_ACCESS_TOKEN]
            refresh_token = user_input[CONF_REFRESH_TOKEN]

            try:
                info = await _validate_tokens(
                    self.hass, access_token, refresh_token
                )
            except vol.Invalid as err:
                errors["base"] = str(err)
            except Exception:
                _LOGGER.exception("Unexpected error during token validation")
                errors["base"] = "network_error"
            else:
                self._access_token = info[CONF_ACCESS_TOKEN]
                self._refresh_token = info[CONF_REFRESH_TOKEN]
                self._email = info["email"]

                # Proceed to network selection
                return await self.async_step_network()

        # Show the re-auth title if this is a re-authentication flow
        if self._reauth_entry:
            self.context["title_placeholders"] = {
                "name": self._reauth_entry.title
            }

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2: Network selection
    # ------------------------------------------------------------------

    async def async_step_network(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the network selection step."""
        errors: dict[str, str] = {}

        # Fetch networks on first load
        if not self._networks:
            session = async_get_clientsession(self.hass)
            auth_client = DazeAuthClient(
                self._access_token, self._refresh_token  # type: ignore[arg-type]
            )
            api_client = DazeApiClient(auth_client, session)

            try:
                self._networks = await _fetch_networks(
                    self.hass, api_client, self._email  # type: ignore[arg-type]
                )
            except vol.Invalid as err:
                errors["base"] = str(err)
                return self.async_show_form(
                    step_id="network",
                    data_schema=vol.Schema({}),
                    errors=errors,
                )
            except Exception:
                _LOGGER.exception(
                    "Unexpected error fetching networks"
                )
                errors["base"] = "network_error"
                return self.async_show_form(
                    step_id="network",
                    data_schema=vol.Schema({}),
                    errors=errors,
                )

            if not self._networks:
                errors["base"] = "no_networks"
                return self.async_show_form(
                    step_id="network",
                    data_schema=vol.Schema({}),
                    errors=errors,
                )

        if user_input is not None:
            self._network_uid = user_input[CONF_NETWORK_UID]
            # Find the network name from the selected UID
            for net in self._networks:
                if net.get("uid") == self._network_uid:
                    self._network_name = net.get("name", "")
                    break

            return await self.async_step_confirm()

        # Build selector options from available networks
        network_options = {
            net["uid"]: net.get("name", "Unknown")
            for net in self._networks
        }

        network_schema = vol.Schema(
            {
                vol.Required(CONF_NETWORK_UID): vol.In(network_options),
            }
        )

        return self.async_show_form(
            step_id="network",
            data_schema=network_schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 3: Confirmation
    # ------------------------------------------------------------------

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the confirmation step — fetches EVSE info and creates the entry."""
        errors: dict[str, str] = {}

        if user_input is not None or self._serial_number:
            # Apply the user-chosen device name from the form field
            if user_input is not None and CONF_EVSE_NAME in user_input:
                self._evse_name = user_input[CONF_EVSE_NAME]

            # Create the config entry
            data = {
                CONF_ACCESS_TOKEN: self._access_token,
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_EMAIL: self._email,
                CONF_NETWORK_UID: self._network_uid,
                CONF_NETWORK_NAME: self._network_name,
                CONF_EVSE_NAME: self._evse_name,
                CONF_SERIAL_NUMBER: self._serial_number,
                CONF_DEVICE_PROFILE: self._device_profile,
                CONF_FIRMWARE_VERSION: self._firmware_version,
                CONF_SOFTWARE_VERSION: self._software_version,
            }

            if self._reauth_entry:
                # Update existing entry (re-auth flow)
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry, data=data
                )
                await self.hass.config_entries.async_reload(
                    self._reauth_entry.entry_id
                )
                return self.async_abort(reason="reauth_successful")

            # Set unique ID to prevent duplicate entries
            await self.async_set_unique_id(self._serial_number)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=self._evse_name or "Daze Wallbox",
                data=data,
            )

        # Fetch EVSE info to populate confirmation details
        session = async_get_clientsession(self.hass)
        auth_client = DazeAuthClient(
            self._access_token, self._refresh_token  # type: ignore[arg-type]
        )
        api_client = DazeApiClient(auth_client, session)

        try:
            evses = await api_client.async_get_evses(self._network_uid)  # type: ignore[arg-type]
        except Exception:
            _LOGGER.exception("Failed to fetch EVSEs for confirmation")
            errors["base"] = "network_error"
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        if not evses:
            errors["base"] = "no_evses"
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        evse = evses[0]
        original_evse_name = evse.get("evseName", "Daze Wallbox")
        # Used as reported: the charger usually names itself after the
        # vendor already, so adding a suffix duplicated it.
        self._evse_name = device_name(evse)
        self._serial_number = evse.get("serialNumber", "")
        self._device_profile = evse.get("deviceProfile", "")
        self._firmware_version = evse.get("firmwareVersion", "")
        self._software_version = evse.get("softwareVersion", "")

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_EVSE_NAME, default=self._evse_name
                ): str,
            }),
            description_placeholders={
                "network_name": str(self._network_name or ""),
                "evse_name": str(original_evse_name),
                "serial_number": str(self._serial_number or ""),
                "device_profile": str(self._device_profile or ""),
            },
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Re-authentication
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle re-authentication when tokens are expired/invalid."""
        entry_id = self.context.get("entry_id")
        assert isinstance(entry_id, str)

        self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id)
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        """Create the options flow."""
        return DazeOptionsFlowHandler(config_entry)


class DazeOptionsFlowHandler(OptionsFlow):
    """Handle Daze Wallbox options."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialise options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose the poll interval and the grid sensors.

        Faster polling makes the entities more responsive at the cost
        of more requests against the Daze cloud API. The entry reloads
        on save, so the new interval takes effect immediately.

        The two grid sensors feed solar surplus control and are
        optional: leaving them empty is a supported configuration, and
        solar control simply refuses to arm without them.
        """
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.options.get(
            CONF_POLL_INTERVAL,
            self._config_entry.data.get(
                CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
            ),
        )

        options = self._config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL, default=current
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
                ),
                # Optional so the integration works without solar. Solar
                # control refuses to leave "off" until both are set.
                vol.Optional(
                    CONF_GRID_IMPORT_SENSOR,
                    description={
                        "suggested_value": options.get(CONF_GRID_IMPORT_SENSOR)
                    },
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="power"
                    )
                ),
                vol.Optional(
                    CONF_GRID_EXPORT_SENSOR,
                    description={
                        "suggested_value": options.get(CONF_GRID_EXPORT_SENSOR)
                    },
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="power"
                    )
                ),
                # Optional so the form can still be saved without it,
                # not because it has a default: solar control refuses
                # to arm until it is answered.
                vol.Optional(
                    CONF_SUPPLY_PHASES,
                    description={
                        "suggested_value": options.get(CONF_SUPPLY_PHASES)
                    },
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[SUPPLY_PHASES_SINGLE, SUPPLY_PHASES_THREE],
                        translation_key="supply_phases",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
