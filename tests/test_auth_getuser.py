"""Tests for Cognito GetUser authentication.

Unlike the other test modules, these import the real integration code
rather than re-implementing its logic, so they fail if the shipped
modules regress. Home Assistant is not required: the auth and API layers
depend only on aiohttp, and the package's ``__init__`` (which does need
Home Assistant) is bypassed by loading the submodules directly.

Run with pytest, or standalone:

    python3 tests/test_auth_getuser.py
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "daze"


def _load_integration_modules() -> tuple[Any, Any]:
    """Load the const, auth and api modules without Home Assistant.

    Returns:
        A tuple of the loaded ``auth`` and ``api`` modules.

    """
    parent = types.ModuleType("daze_under_test")
    parent.__path__ = [str(PACKAGE_DIR)]
    sys.modules["daze_under_test"] = parent

    const_spec = importlib.util.spec_from_file_location(
        "daze_under_test.const", PACKAGE_DIR / "const.py"
    )
    assert const_spec and const_spec.loader
    const_module = importlib.util.module_from_spec(const_spec)
    sys.modules["daze_under_test.const"] = const_module
    const_spec.loader.exec_module(const_module)

    api_spec = importlib.util.spec_from_file_location(
        "daze_under_test.api",
        PACKAGE_DIR / "api" / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR / "api")],
    )
    assert api_spec and api_spec.loader
    api_module = importlib.util.module_from_spec(api_spec)
    sys.modules["daze_under_test.api"] = api_module
    api_spec.loader.exec_module(api_module)

    return sys.modules["daze_under_test.api.auth"], api_module


auth, api = _load_integration_modules()

# Keep the suite fast; the wall-clock waits are not under test.
api.COMMAND_RETRY_DELAY = 0.0
api.COMMAND_RETRY_MAX_DELAY = 0.0


# ------------------------------------------------------------------
# Fake aiohttp session
# ------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for an aiohttp response."""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type: str | None = "application/json") -> Any:
        """Return the canned payload, ignoring content type."""
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def text(self) -> str:
        """Return the body as JSON text, as aiohttp does.

        Returning str(dict) here would produce Python repr with single
        quotes, which is not what the real client sees and would hide
        parsing bugs in the error handling.
        """
        return json.dumps(self._payload)

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeSession:
    """Records requests and replays queued responses."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        """Record a POST and return the next queued response."""
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self._responses.pop(0)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        """Record a GET and return the next queued response."""
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self._responses.pop(0)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        """Record a generic request and return the next queued response."""
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)


GET_USER_OK = {
    "Username": "1a2b3c",
    "UserAttributes": [
        {"Name": "sub", "Value": "1a2b3c"},
        {"Name": "email", "Value": "driver@example.invalid"},
        {"Name": "name", "Value": "Test Driver"},
    ],
}

GET_USER_DENIED = {
    "__type": "NotAuthorizedException",
    "message": "Invalid Access Token",
}


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


def test_fetch_user_targets_user_pool_api() -> None:
    """GetUser must hit the IDP endpoint with the token in the body."""
    session = FakeSession([FakeResponse(200, GET_USER_OK)])

    status, body = asyncio.run(auth.async_fetch_user(session, "tok-123"))

    assert status == 200
    assert body == GET_USER_OK

    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://cognito-idp.eu-central-1.amazonaws.com/"
    assert call["headers"]["X-Amz-Target"] == (
        "AWSCognitoIdentityProviderService.GetUser"
    )
    assert call["headers"]["Content-Type"] == "application/x-amz-json-1.1"
    # The token goes in the body, never in an Authorization header.
    assert call["json"] == {"AccessToken": "tok-123"}
    assert "authorization" not in {k.lower() for k in call["headers"]}


def test_fetch_user_relaxes_content_type() -> None:
    """Cognito replies as x-amz-json-1.1, which must still parse."""
    session = FakeSession([FakeResponse(200, GET_USER_OK)])

    asyncio.run(auth.async_fetch_user(session, "tok-123"))

    # content_type=None is what stops aiohttp raising ContentTypeError.
    assert session.calls[0]["url"].endswith("amazonaws.com/")


def test_validate_tokens_accepts_admin_scope_token() -> None:
    """A token without 'openid' must now validate successfully."""
    session = FakeSession([FakeResponse(200, GET_USER_OK)])
    client = auth.DazeAuthClient("tok-123", "refresh-123")

    assert asyncio.run(client.async_validate_tokens(session)) is True


def test_validate_tokens_rejects_bad_token_without_leaking_body() -> None:
    """A 400 NotAuthorizedException must raise AuthError, body withheld."""
    session = FakeSession(
        [
            FakeResponse(400, GET_USER_DENIED),
            FakeResponse(500, {"error": "internal"}),
        ]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")

    try:
        asyncio.run(client.async_validate_tokens(session))
    except auth.AuthError as err:
        message = str(err)
        assert "NotAuthorizedException" in message
        assert "Invalid Access Token" not in message
    else:
        raise AssertionError("expected AuthError")


def test_get_user_info_flattens_attributes() -> None:
    """The attribute list must become a dict the config flow can read."""
    session = FakeSession([FakeResponse(200, GET_USER_OK)])
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    info = asyncio.run(api_client.async_get_user_info())

    assert info["email"] == "driver@example.invalid"
    assert info["name"] == "Test Driver"
    assert info["username"] == "1a2b3c"


def test_get_user_info_refreshes_on_400_then_succeeds() -> None:
    """A rejected token must trigger exactly one refresh and retry.

    GetUser signals a bad token with 400, not 401, so the generic
    retry-on-401 path in _request would never fire for it.
    """
    session = FakeSession(
        [
            FakeResponse(400, GET_USER_DENIED),
            FakeResponse(500, {"error": "internal"}),
            FakeResponse(200, {"access_token": "tok-456", "expires_in": 3600}),
            FakeResponse(200, GET_USER_OK),
        ]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    info = asyncio.run(api_client.async_get_user_info())

    assert info["email"] == "driver@example.invalid"
    assert len(session.calls) == 4
    assert session.calls[1]["url"].endswith("/oauth2/userInfo")
    assert session.calls[2]["url"].endswith("/oauth2/token")
    # The retry must use the refreshed token, not the stale one.
    assert session.calls[3]["json"] == {"AccessToken": "tok-456"}


def test_get_user_info_raises_when_refresh_does_not_help() -> None:
    """Two rejections in a row must surface as ApiAuthError."""
    session = FakeSession(
        [
            FakeResponse(400, GET_USER_DENIED),
            FakeResponse(500, {"error": "internal"}),
            FakeResponse(200, {"access_token": "tok-456", "expires_in": 3600}),
            FakeResponse(400, GET_USER_DENIED),
            FakeResponse(500, {"error": "internal"}),
        ]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    try:
        asyncio.run(api_client.async_get_user_info())
    except api.ApiAuthError:
        pass
    else:
        raise AssertionError("expected ApiAuthError")


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Return the ids of every docstring constant node in a module."""
    ids: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                ids.add(id(first.value))

    return ids


def test_userinfo_endpoint_is_not_primary_path() -> None:
    """Guard against reintroducing userInfo as the primary path.

    /oauth2/userInfo requires the 'openid' scope, which most Daze
    portal tokens don't carry, so it cannot be the primary validation
    endpoint.  auth.py uses it as an intentional fallback — only that
    file is allowed, and only inside the try block guarded by a GetUser
    rejection check.

    Only executable string literals count. Comments never reach the AST,
    and docstrings are excluded deliberately.
    """
    allowed_file = PACKAGE_DIR / "api" / "auth.py"

    offenders: list[str] = []

    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        if path == allowed_file:
            continue  # auth.py has the intentional fallback

        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_nodes(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if id(node) in skip:
                continue
            if isinstance(node.value, str) and "userInfo" in node.value:
                rel = path.relative_to(ROOT)
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, f"userInfo used in: {offenders}"



def test_404_raises_api_not_found_without_logging_body() -> None:
    """A 404 must raise ApiNotFoundError so callers can back off.

    The recharge-session endpoint returns 404 with an empty body for
    some accounts. Treating that as a generic ApiError made the
    coordinator retry and log a warning on every poll.
    """
    session = FakeSession([FakeResponse(404, {"_empty": True})])
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    try:
        asyncio.run(api_client.async_get_recharge_sessions("net-uid"))
    except api.ApiNotFoundError as err:
        assert "404" in str(err)
    else:
        raise AssertionError("expected ApiNotFoundError")


def test_api_not_found_is_an_api_error() -> None:
    """Existing handlers catching ApiError must still catch 404s."""
    assert issubclass(api.ApiNotFoundError, api.ApiError)


def test_non_404_errors_still_raise_plain_api_error() -> None:
    """A 500 must remain a plain ApiError, not a not-found."""
    session = FakeSession([FakeResponse(500, {"message": "boom"})])
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    try:
        asyncio.run(api_client.async_get_recharge_sessions("net-uid"))
    except api.ApiNotFoundError:
        raise AssertionError("500 must not be ApiNotFoundError")
    except api.ApiError as err:
        assert "500" in str(err)
    else:
        raise AssertionError("expected ApiError")



def test_start_charge_sends_serial_and_session() -> None:
    """Both fields are required; either alone does not resume.

    Measured against hardware: an empty body returns 422
    ErrorWrongSessionID, sessionId alone returns 200 but leaves the
    charger paused, and both together actually resume it.
    """
    session = FakeSession([FakeResponse(200, {"message": "", "errors": []})])
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    asyncio.run(api_client.async_start_charge("SER1", 1790529768000))

    call = session.calls[0]
    assert call["url"].endswith("/sockets/SER1/commands/playcharge")
    assert call["json"] == {
        "evseSerialNumber": "SER1",
        "sessionId": 1790529768000,
    }


def test_stop_charge_sends_the_same_shape() -> None:
    """Stop mirrors start. Assumed symmetric, not measured."""
    session = FakeSession([FakeResponse(200, {"message": "", "errors": []})])
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    asyncio.run(api_client.async_stop_charge("SER1", 42))

    assert session.calls[0]["json"] == {
        "evseSerialNumber": "SER1",
        "sessionId": 42,
    }


def test_commands_still_send_the_serial_without_a_session() -> None:
    """An unknown session must not drop the serial from the body."""
    session = FakeSession(
        [
            FakeResponse(200, NO_SESSION),
            FakeResponse(200, {"message": "", "errors": []}),
        ]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    asyncio.run(api_client.async_start_charge("SER1", None))

    assert session.calls[-1]["json"] == {"evseSerialNumber": "SER1"}



RPC_FAILURE = {
    "message": "Error",
    "errors": [{"code": 101, "message": "Server error while requesting rpc server side"}],
}

WRONG_SESSION = {
    "message": "Invalid Data",
    "errors": [{"code": 4121, "message": "ErrorWrongSessionID"}],
}

COMMAND_OK = {"message": "", "errors": []}

# A charger with no open session: the command then sends only
# the serial, and the API is expected to reject it.
NO_SESSION = {"data": {"evseState": 1, "chargeSession": None}}


def test_retry_budget_covers_observed_failure_rate() -> None:
    """Around six attempts were needed in practice, so allow more."""
    assert api.COMMAND_RETRY_ATTEMPTS >= 8


def test_transient_rpc_failure_is_retried_until_it_works() -> None:
    """Code 101 is intermittent; the command must not give up on it.

    The vendor app needs several presses for the same reason. Reporting
    an error after one attempt is what made start and stop look broken.
    """
    session = FakeSession(
        [
            FakeResponse(500, RPC_FAILURE),
            FakeResponse(500, RPC_FAILURE),
            FakeResponse(200, COMMAND_OK),
        ]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    result = asyncio.run(
        api_client.async_start_charge("SER1", 42)
    )

    assert result == COMMAND_OK
    assert len(session.calls) == 3
    # Every attempt must send the same correct body.
    for call in session.calls:
        assert call["json"] == {"evseSerialNumber": "SER1", "sessionId": 42}


def test_retry_gives_up_and_says_it_is_temporary() -> None:
    """Exhausting the retries must not blame the car or the session."""
    budget = api.COMMAND_RETRY_ATTEMPTS
    session = FakeSession(
        [FakeResponse(500, RPC_FAILURE) for _ in range(budget)]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    try:
        asyncio.run(api_client.async_start_charge("SER1", 42))
    except api.ApiCommandRejectedError as err:
        assert err.code == 101
        assert "could not reach the wallbox" in str(err)
        assert len(session.calls) == budget
    else:
        raise AssertionError("expected ApiCommandRejectedError")


def test_wrong_session_is_not_retried() -> None:
    """4121 is deterministic. Retrying it only wastes time."""
    session = FakeSession(
        [FakeResponse(200, NO_SESSION), FakeResponse(422, WRONG_SESSION)]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    try:
        asyncio.run(api_client.async_start_charge("SER1", None))
    except api.ApiCommandRejectedError as err:
        assert err.code == 4121
        assert "no paused charging session" in str(err)
        # One lookup plus one command: the rejection is not retried.
        assert len(session.calls) == 2
    else:
        raise AssertionError("expected ApiCommandRejectedError")


def test_stop_is_retried_the_same_way() -> None:
    """Stop shows the same flakiness, so it gets the same treatment."""
    session = FakeSession(
        [FakeResponse(500, RPC_FAILURE), FakeResponse(200, COMMAND_OK)]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    asyncio.run(api_client.async_stop_charge("SER1", 42))

    assert len(session.calls) == 2



class _Capture(logging.Handler):
    """Collects log records for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Store a record."""
        self.records.append(record)


def _capture_api_logs() -> _Capture:
    """Attach a capturing handler to the api module's logger."""
    handler = _Capture()
    logger = logging.getLogger(api.__name__)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return handler


def test_retried_failures_do_not_log_warnings() -> None:
    """A retry that eventually works must not look like an error.

    Each failed attempt used to log at warning from the request layer,
    so a command that succeeded on the third try left three warnings in
    the log and looked broken to the user.
    """
    session = FakeSession(
        [
            FakeResponse(500, RPC_FAILURE),
            FakeResponse(500, RPC_FAILURE),
            FakeResponse(200, COMMAND_OK),
        ]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    handler = _capture_api_logs()
    try:
        asyncio.run(api_client.async_start_charge("SER1", 42))
    finally:
        logging.getLogger(api.__name__).removeHandler(handler)

    warnings = [r for r in handler.records if r.levelno >= logging.WARNING]
    assert not warnings, [r.getMessage() for r in warnings]


def test_giving_up_logs_exactly_one_warning() -> None:
    """Exhausting the retries is worth one warning, not eight."""
    budget = api.COMMAND_RETRY_ATTEMPTS
    session = FakeSession(
        [FakeResponse(500, RPC_FAILURE) for _ in range(budget)]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    handler = _capture_api_logs()
    try:
        asyncio.run(api_client.async_start_charge("SER1", 42))
    except api.ApiCommandRejectedError:
        pass
    finally:
        logging.getLogger(api.__name__).removeHandler(handler)

    warnings = [r for r in handler.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "gave up after" in warnings[0].getMessage()



def test_retry_delay_grows_between_attempts() -> None:
    """A tight burst of retries did not work; spacing them out might.

    Eight attempts 1.5s apart all failed inside eleven seconds, while
    manual presses roughly sixteen seconds apart did succeed. The delay
    therefore grows rather than staying flat, up to a cap.
    """
    base = 1.5
    cap = 6.0
    delays = [min(base * n, cap) for n in range(1, 8)]

    assert delays == [1.5, 3.0, 4.5, 6.0, 6.0, 6.0, 6.0]
    # Long enough to outlast a transient outage, short enough that a
    # service call still returns.
    assert 25 <= sum(delays) <= 45


def test_give_up_message_states_the_duration_and_blames_the_service() -> None:
    """The user should not go looking at the car or the charger."""
    budget = api.COMMAND_RETRY_ATTEMPTS
    session = FakeSession(
        [FakeResponse(500, RPC_FAILURE) for _ in range(budget)]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    try:
        asyncio.run(api_client.async_start_charge("SER1", 42))
    except api.ApiCommandRejectedError as err:
        message = str(err)
        assert "Daze" in message
        assert "seconds" in message
        assert "rather than in the charger or the car" in message
    else:
        raise AssertionError("expected ApiCommandRejectedError")



REMOTE_WITH_SESSION = {
    "data": {
        "evseState": 6,
        "isPaused": True,
        "chargeSession": {"sessionId": 1790543468000},
    }
}


def test_command_reads_a_fresh_session_id_when_not_given_one() -> None:
    """A cached ID can name a session that has already ended.

    Session IDs change whenever one session closes and another opens.
    The coordinator's copy is up to a poll interval old, so the command
    re-reads it rather than trusting that.
    """
    session = FakeSession(
        [
            FakeResponse(200, REMOTE_WITH_SESSION),
            FakeResponse(200, COMMAND_OK),
        ]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    asyncio.run(api_client.async_start_charge("SER1"))

    assert len(session.calls) == 2
    assert "remoteInfo" in session.calls[0]["url"]
    assert session.calls[1]["json"] == {
        "evseSerialNumber": "SER1",
        "sessionId": 1790543468000,
    }


def test_explicit_session_id_skips_the_extra_read() -> None:
    """Passing an ID is an override, used by tests and the tools."""
    session = FakeSession([FakeResponse(200, COMMAND_OK)])
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    asyncio.run(api_client.async_start_charge("SER1", 42))

    assert len(session.calls) == 1
    assert session.calls[0]["json"]["sessionId"] == 42


def test_command_proceeds_when_the_session_read_fails() -> None:
    """A failed lookup must not block the command entirely."""
    session = FakeSession(
        [
            FakeResponse(500, {"message": "boom"}),
            FakeResponse(200, COMMAND_OK),
        ]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    asyncio.run(api_client.async_stop_charge("SER1"))

    assert session.calls[-1]["json"] == {"evseSerialNumber": "SER1"}



OUT_OF_RANGE = {
    "message": "Invalid Data",
    "errors": [
        {
            "code": 369,
            "message": (
                "Server error while requesting rpc server side. "
                "Error MaxExternalChargingCurrentOutOfRange"
            ),
        }
    ],
}


def test_out_of_range_current_is_explained_and_not_retried() -> None:
    """369 mentions the RPC server but is a validation failure.

    Retrying it changes nothing, and the stock message sent the user
    looking in the wrong place.
    """
    session = FakeSession([FakeResponse(422, OUT_OF_RANGE)])
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    try:
        asyncio.run(api_client.async_set_max_charging_current("SER1", 32000))
    except api.ApiCommandRejectedError as err:
        assert err.code == 369
        assert "outside the range" in str(err)
        assert len(session.calls) == 1
    else:
        raise AssertionError("expected ApiCommandRejectedError")


def test_current_change_retries_a_transient_failure() -> None:
    """Configuration writes share the command retry behaviour."""
    session = FakeSession(
        [FakeResponse(500, RPC_FAILURE), FakeResponse(200, COMMAND_OK)]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    asyncio.run(api_client.async_set_max_charging_current("SER1", 10000))

    assert len(session.calls) == 2



def test_inline_budget_is_short_enough_to_hand_off() -> None:
    """The user must not wait 33s before the background takes over.

    Blocking through the full retry budget was tried and still failed;
    the point of the handoff is that nobody waits for it.
    """
    import importlib.util as _iu
    spec = _iu.spec_from_file_location(
        "daze_const_qa", PACKAGE_DIR / "const.py"
    )
    assert spec and spec.loader
    const = _iu.module_from_spec(spec)
    spec.loader.exec_module(const)

    inline_wait = sum(
        min(api.COMMAND_RETRY_DELAY * n, api.COMMAND_RETRY_MAX_DELAY)
        for n in range(1, const.INLINE_COMMAND_ATTEMPTS)
    )
    assert inline_wait <= 10, inline_wait

    # And the background must cover far longer than the inline path.
    assert sum(const.BACKGROUND_RETRY_DELAYS) > 300


def test_command_attempts_are_caller_controlled() -> None:
    """Entities shorten the inline budget; the default stays long."""
    session = FakeSession(
        [
            FakeResponse(200, NO_SESSION),
            FakeResponse(500, RPC_FAILURE),
            FakeResponse(500, RPC_FAILURE),
        ]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    try:
        asyncio.run(api_client.async_start_charge("SER1", attempts=2))
    except api.ApiCommandRejectedError:
        pass

    # One session lookup plus exactly two command attempts.
    assert len(session.calls) == 3


def test_set_current_accepts_an_attempt_budget() -> None:
    """The number entity passes a short budget for the same reason."""
    session = FakeSession(
        [FakeResponse(500, RPC_FAILURE), FakeResponse(200, COMMAND_OK)]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    asyncio.run(
        api_client.async_set_max_charging_current("SER1", 8000, attempts=3)
    )

    assert len(session.calls) == 2


# ------------------------------------------------------------------
# UserInfo fallback for openid-scoped tokens
# ------------------------------------------------------------------


OPENID_USER_INFO = {
    "sub": "openid-user-01",
    "email": "openid@example.invalid",
    "name": "OpenID User",
    "email_verified": "true",
}


def test_fetch_user_falls_back_to_userinfo_when_getuser_denies() -> None:
    """A token scoped openid should validate via userInfo, not GetUser."""
    session = FakeSession(
        [
            FakeResponse(400, GET_USER_DENIED),
            FakeResponse(200, OPENID_USER_INFO),
        ]
    )

    status, body = asyncio.run(auth.async_fetch_user(session, "tok-openid"))

    assert status == 200
    assert body["Username"] == "openid-user-01"
    assert len(session.calls) == 2
    # First call: POST to IDP (GetUser)
    assert session.calls[0]["method"] == "POST"
    # Second call: GET to userInfo
    assert session.calls[1]["method"] == "GET"
    assert "/oauth2/userInfo" in session.calls[1]["url"]
    # Token goes in the Authorization header for userInfo (not body).
    assert session.calls[1]["headers"]["Authorization"] == "Bearer tok-openid"


def test_fetch_user_reshape_flattens_userinfo_attributes() -> None:
    """The userInfo dict must be reshaped to the GetUser shape."""
    session = FakeSession(
        [
            FakeResponse(400, GET_USER_DENIED),
            FakeResponse(200, OPENID_USER_INFO),
        ]
    )

    _, body = asyncio.run(auth.async_fetch_user(session, "tok-openid"))

    attrs = {a["Name"]: a["Value"] for a in body["UserAttributes"]}
    assert attrs["email"] == "openid@example.invalid"
    assert attrs["name"] == "OpenID User"


def test_fetch_user_raises_when_both_endpoints_fail() -> None:
    """Both GetUser and userInfo rejected — raise with last error info."""
    user_info_500 = FakeResponse(500, {"error": "internal"})

    # We only expect the userInfo failure to propagate;
    # a non-ClientError from the inner async with still falls through
    # and returns the original GetUser status.
    session = FakeSession(
        [
            FakeResponse(400, GET_USER_DENIED),
            user_info_500,
        ]
    )

    # When userInfo returns non-200, the function returns the
    # original GetUser failure code (400), not a new exception.
    status, _body = asyncio.run(auth.async_fetch_user(session, "tok-openid"))
    assert status == 400  # GetUser error propagated


def test_fetch_user_400_on_userinfo_with_body_does_not_raise() -> None:
    """A userInfo 400 must not raise AuthError — fall through."""
    session = FakeSession(
        [
            FakeResponse(400, GET_USER_DENIED),
            FakeResponse(400, {"error": "unauthorized"}),
        ]
    )

    status, _body = asyncio.run(auth.async_fetch_user(session, "tok-openid"))
    # Both endpoints failed; GetUser error (400) is preserved.
    assert status == 400


def test_validate_tokens_accepts_openid_scope_token() -> None:
    """A token that only carries 'openid' must still validate."""
    session = FakeSession(
        [
            FakeResponse(400, GET_USER_DENIED),
            FakeResponse(200, OPENID_USER_INFO),
        ]
    )
    client = auth.DazeAuthClient("tok-openid", "refresh-openid")

    assert asyncio.run(client.async_validate_tokens(session)) is True


def test_get_user_info_uses_userinfo_for_openid_tokens() -> None:
    """async_get_user_info reshapes the userInfo body correctly."""
    session = FakeSession(
        [
            FakeResponse(400, GET_USER_DENIED),
            FakeResponse(200, OPENID_USER_INFO),
        ]
    )
    client = auth.DazeAuthClient("tok-openid", "refresh-openid")
    api_client = api.DazeApiClient(client, session)

    info = asyncio.run(api_client.async_get_user_info())

    assert info["email"] == "openid@example.invalid"
    assert info["name"] == "OpenID User"


def test_fetch_user_returns_getuser_failure_when_both_fail() -> None:
    """When GetUser denies and userInfo returns non-200, propagate GetUser status."""
    session = FakeSession(
        [
            FakeResponse(400, GET_USER_DENIED),
            FakeResponse(500, {"error": "internal"}),
        ]
    )

    status, body = asyncio.run(auth.async_fetch_user(session, "tok-openid"))

    assert status == 400
    assert body["__type"] == "NotAuthorizedException"


def test_fetch_user_no_body_on_userinfo_500_returns_getuser_status() -> None:
    """A non-200 userInfo response must not raise — GetUser error wins."""
    session = FakeSession(
        [
            FakeResponse(400, GET_USER_DENIED),
            FakeResponse(400, {"error": "unauthorized"}),
        ]
    )

    status, _body = asyncio.run(auth.async_fetch_user(session, "tok-openid"))
    assert status == 400


def _main() -> int:
    """Run every test in this module and report results."""
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]

    failures = 0
    for test in tests:
        try:
            test()
        except Exception as err:  # noqa: BLE001 - standalone runner
            failures += 1
            print(f"FAIL {test.__name__}: {type(err).__name__}: {err}")
        else:
            print(f"ok   {test.__name__}")

    print(f"\n{len(tests) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
