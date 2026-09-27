"""Constants for the Daze Wallbox integration."""

DOMAIN = "daze"

# API base URLs
API_BASE_URL = "https://webapi.dazeservice.com/v3"
COGNITO_BASE_URL = "https://daze.auth.eu-central-1.amazoncognito.com"

# Cognito user pool API, used to read the signed-in user's profile.
#
# The hosted-UI endpoint COGNITO_BASE_URL/oauth2/userInfo cannot be used:
# it requires the access token to carry the "openid" scope, and the Daze
# web portal issues access tokens scoped "aws.cognito.signin.user.admin"
# only. Those tokens are valid, but userInfo rejects every one of them
# with "Access token does not contain the 'openid' scope". The GetUser
# operation below accepts that scope and returns the same attributes.
COGNITO_IDP_URL = "https://cognito-idp.eu-central-1.amazonaws.com/"
GET_USER_TARGET = "AWSCognitoIdentityProviderService.GetUser"
GET_USER_CONTENT_TYPE = "application/x-amz-json-1.1"

# Cognito OAuth settings
CLIENT_ID = "4m0rp7oqarbrc3hn67ivvonba8"
REDIRECT_URI = "https://webportal.dazeservice.com/authentication/callback"

# Config entry keys
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_TOKEN_EXPIRY = "token_expiry"
CONF_EMAIL = "email"
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

# Bounds for the user-configurable poll interval. The lower bound keeps
# the cloud API from being hammered; the upper bound keeps the entities
# from going obviously stale.
MIN_POLL_INTERVAL = 10  # seconds
MAX_POLL_INTERVAL = 600  # seconds

# How long an optimistic switch state is trusted before the charger's
# own reading takes over again. Observed transitions completed in 9 to
# 12 seconds, so this both covers them and bounds how long the UI can
# disagree with reality if a command silently fails.
OPTIMISTIC_STATE_TIMEOUT = 20  # seconds

# When to re-read the charger after a command. Late enough that the
# transition has happened, rather than immediately, which reads the old
# state back and makes the toggle appear to flip back.
POST_COMMAND_REFRESH_DELAY = 10  # seconds

DEFAULT_TOKEN_EXPIRY_BUFFER = 60  # seconds

# Platform list
PLATFORMS = ["sensor", "switch", "number", "select"]

# Service names
SERVICE_START_CHARGE = "start_charge"
SERVICE_STOP_CHARGE = "stop_charge"
SERVICE_SET_CHARGING_CURRENT = "set_charging_current"
