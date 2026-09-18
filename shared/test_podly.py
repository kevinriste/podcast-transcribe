# ruff: noqa: S105, S106
"""Tests for Podly API client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import final
from unittest.mock import patch

from podcast_shared import enable_post_in_podly, get_podly_config


@final
@dataclass(frozen=True)
class _FakeResponse:
    """Fake requests.Response with status_code and text."""

    status_code: int
    text: str = ""


@final
class _FakeSession:
    """Fake requests.Session recording calls and returning sequenced responses."""

    def __init__(
        self,
        post_responses: list[_FakeResponse],
        get_responses: list[_FakeResponse] | None = None,
    ) -> None:
        """Initialize fake session with pre-canned responses."""
        self.post_responses: list[_FakeResponse] = list(post_responses)
        self.get_responses: list[_FakeResponse] = list(get_responses or [])
        self.post_calls: list[tuple[str, object]] = []

    def post(self, url: str, json: object = None, timeout: float = 15.0) -> _FakeResponse:
        """Record post call and return next response.

        Returns:
            The next scheduled fake response.

        """
        _ = timeout
        self.post_calls.append((url, json))
        return self.post_responses.pop(0)

    def get(self, url: str, timeout: float = 15.0) -> _FakeResponse:
        """Return next get response.

        Returns:
            The next scheduled fake response.

        """
        _ = (url, timeout)
        return self.get_responses.pop(0)


def test_get_podly_config() -> None:
    """Test get_podly_config with explicit params and environment defaults.

    Raises:
        AssertionError: If URL or credentials do not match expected values.

    """
    url, user, pwd = get_podly_config("https://custom.podly.test/", "admin", "secret")
    if url != "https://custom.podly.test":
        msg = f"Expected stripped trailing slash, got: {url}"
        raise AssertionError(msg)
    if user != "admin" or pwd != "secret":
        msg = f"Expected credentials, got: {user}, {pwd}"
        raise AssertionError(msg)


def test_enable_post_in_podly_direct_guid_success() -> None:
    """Test enable_post_in_podly when GUID directly succeeds on whitelist endpoint.

    Raises:
        AssertionError: If enable_post_in_podly fails unexpectedly.

    """
    fake_session = _FakeSession(
        post_responses=[
            _FakeResponse(200),  # Auth
            _FakeResponse(200),  # Whitelist
        ]
    )

    with patch("podcast_shared.podly.requests.Session", return_value=fake_session):
        result = enable_post_in_podly(
            guid="test-guid-123",
            title="Bill Simmons and Cousin Sal",
            podly_url="https://podly.test",
            username="testuser",
            password="testpassword",
        )

    if not result:
        msg = "Expected enable_post_in_podly to succeed"
        raise AssertionError(msg)

    expected_call = (
        "https://podly.test/api/posts/test-guid-123/whitelist",
        {"whitelisted": True, "trigger_processing": True},
    )
    if expected_call not in fake_session.post_calls:
        msg = f"Expected call {expected_call!r} not in {fake_session.post_calls!r}"
        raise AssertionError(msg)


def test_enable_post_in_podly_refresh_and_discover() -> None:
    """Test enable_post_in_podly when GUID is 404 at first, then found via feeds query.

    Raises:
        AssertionError: If enable_post_in_podly fails to find and enable discovered episode.

    """
    fake_session = _FakeSession(
        post_responses=[
            _FakeResponse(200),  # Auth
            _FakeResponse(404, "Not found"),  # Whitelist attempt 1 (404)
            _FakeResponse(200),  # Refresh all feeds
            _FakeResponse(200),  # Whitelist attempt 2 with discovered guid
        ],
        get_responses=[
            _FakeResponse(200, '[{"id": 42, "title": "The Bill Simmons Podcast"}]'),
            _FakeResponse(
                200,
                '{"items": [{"guid": "discovered-guid", "title": "Week 1 with Cousin Sal", "download_url": "http://a/b.mp3"}]}',
            ),
        ],
    )

    with patch("podcast_shared.podly.requests.Session", return_value=fake_session), patch("time.sleep"):
        result = enable_post_in_podly(
            guid="initial-guid",
            title="Week 1 with Cousin Sal",
            feed_name="Bill Simmons",
            podly_url="https://podly.test",
            username="testuser",
            password="testpassword",
        )

    if not result:
        msg = "Expected enable_post_in_podly to succeed after discovery"
        raise AssertionError(msg)

    expected_call = (
        "https://podly.test/api/posts/discovered-guid/whitelist",
        {"whitelisted": True, "trigger_processing": True},
    )
    if expected_call not in fake_session.post_calls:
        msg = f"Expected call {expected_call!r} not in {fake_session.post_calls!r}"
        raise AssertionError(msg)


def test_enable_post_in_podly_auth_failure() -> None:
    """Test enable_post_in_podly returns False when login fails.

    Raises:
        AssertionError: If enable_post_in_podly succeeds despite auth failure.

    """
    fake_session = _FakeSession(
        post_responses=[
            _FakeResponse(401, "Unauthorized"),
        ]
    )

    with patch("podcast_shared.podly.requests.Session", return_value=fake_session):
        result = enable_post_in_podly(
            guid="test-guid",
            podly_url="https://podly.test",
            username="wronguser",
            password="wrongpassword",
        )

    if result:
        msg = "Expected enable_post_in_podly to fail on auth error"
        raise AssertionError(msg)
