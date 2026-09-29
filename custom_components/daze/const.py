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

# Solar surplus control. The two grid sensors are chosen by the user in
# the options flow; both are required before solar control can leave
# "off".
CONF_GRID_IMPORT_SENSOR = "grid_import_sensor"
CONF_GRID_EXPORT_SENSOR = "grid_export_sensor"

# How many phases feed the house. Declared by the user, because the
# Daze payload does not say: its only phase field, evseIsThreePhase,
# describes the charger, and payload.min_charging_current already reads
# it that way. There is deliberately no default — a three-phase meter
# reports surplus netted across phases, and following it with a
# single-phase charger loads the one phase the charger is on.
CONF_SUPPLY_PHASES = "supply_phases"
SUPPLY_PHASES_SINGLE = "single"
SUPPLY_PHASES_THREE = "three"

# Watts to leave for the house before the car gets any. Site-specific,
# so it is an entity rather than a constant; this is only its default.
CONF_SOLAR_RESERVE = "solar_reserve"
DEFAULT_SOLAR_RESERVE = 0
MAX_SOLAR_RESERVE = 5000

# How long an optimistic switch state is trusted before the charger's
# own reading takes over again. Observed transitions completed in 9 to
# 12 seconds, so this both covers them and bounds how long the UI can
# disagree with reality if a command silently fails.
OPTIMISTIC_STATE_TIMEOUT = 20  # seconds

# When to re-read the charger after a command. Late enough that the
# transition has happened, rather than immediately, which reads the old
# state back and makes the toggle appear to flip back.
POST_COMMAND_REFRESH_DELAY = 10  # seconds

# A command that the Daze RPC link refuses is retried in the
# background rather than held open. Blocking eight attempts across 33
# seconds still failed, and holding a service call longer than that is
# not reasonable.
#
# These offsets spread further attempts over roughly seven and a half
# minutes, which covers an outage of the length observed without the
# user waiting on any of it.
BACKGROUND_RETRY_DELAYS = (15, 30, 60, 120, 240)

# An optimistic value waiting on a background retry is held past
# the usual timeout, but not forever: if the retry chain is
# superseded its callbacks never fire, and without this cap the
# entity would show a stale request until Home Assistant restarts.
MAX_OPTIMISTIC_HOLD = sum(BACKGROUND_RETRY_DELAYS) + 60

# Attempts made while the user waits, before handing off to the
# background. Kept short: a healthy link answers on the first try.
INLINE_COMMAND_ATTEMPTS = 3

DEFAULT_TOKEN_EXPIRY_BUFFER = 60  # seconds

# Platform list
PLATFORMS = ["sensor", "switch", "number", "select"]

# Service names
SERVICE_START_CHARGE = "start_charge"
SERVICE_STOP_CHARGE = "stop_charge"
SERVICE_SET_CHARGING_CURRENT = "set_charging_current"
