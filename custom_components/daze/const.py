"""Constants for the Daze Wallbox integration."""

DOMAIN = "daze"

# API base URLs
API_BASE_URL = "https://webapi.dazeservice.com/v3"
COGNITO_BASE_URL = "https://daze.auth.eu-central-1.amazoncognito.com"

# Cognito OAuth settings
CLIENT_ID = "4m0rp7oqarbrc3hn67ivvonba8"
REDIRECT_URI = "https://webportal.dazeservice.com/authentication/callback"

# Config entry keys
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_TOKEN_EXPIRY = "token_expiry"
CONF_NETWORK_UID = "network_uid"
CONF_NETWORK_NAME = "network_name"
CONF_EVSE_NAME = "evse_name"
CONF_SERIAL_NUMBER = "serial_number"
CONF_DEVICE_PROFILE = "device_profile"
CONF_FIRMWARE_VERSION = "firmware_version"
CONF_SOFTWARE_VERSION = "software_version"
CONF_POLL_INTERVAL = "poll_interval"

# Coordinator defaults
DEFAULT_POLL_INTERVAL = 30  # seconds
DEFAULT_TOKEN_EXPIRY_BUFFER = 60  # seconds

# Platform list
PLATFORMS = ["sensor", "switch", "number", "select"]
