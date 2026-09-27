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
        """Return a textual body."""
        return str(self._payload)

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
    session = FakeSession([FakeResponse(400, GET_USER_DENIED)])
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
            FakeResponse(200, {"access_token": "tok-456", "expires_in": 3600}),
            FakeResponse(200, GET_USER_OK),
        ]
    )
    client = auth.DazeAuthClient("tok-123", "refresh-123")
    api_client = api.DazeApiClient(client, session)

    info = asyncio.run(api_client.async_get_user_info())

    assert info["email"] == "driver@example.invalid"
    assert len(session.calls) == 3
    assert session.calls[1]["url"].endswith("/oauth2/token")
    # The retry must use the refreshed token, not the stale one.
    assert session.calls[2]["json"] == {"AccessToken": "tok-456"}


def test_get_user_info_raises_when_refresh_does_not_help() -> None:
    """Two rejections in a row must surface as ApiAuthError."""
    session = FakeSession(
        [
            FakeResponse(400, GET_USER_DENIED),
            FakeResponse(200, {"access_token": "tok-456", "expires_in": 3600}),
            FakeResponse(400, GET_USER_DENIED),
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


def test_userinfo_endpoint_is_gone() -> None:
    """Guard against the unusable endpoint being reintroduced.

    /oauth2/userInfo requires the 'openid' scope, which Daze never
    issues, so calling it means setup is broken again.

    Only executable string literals count. Comments never reach the AST,
    and docstrings are excluded deliberately, so the explanations of why
    this endpoint is avoided do not trip the guard.
    """
    offenders: list[str] = []

    for path in sorted(PACKAGE_DIR.rglob("*.py")):
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
