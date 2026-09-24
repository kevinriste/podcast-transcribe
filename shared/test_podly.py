# ruff: noqa: S105, S106
"""Tests for the Podly API client (script-style; runs without network)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import final
from unittest.mock import patch

from podcast_shared import enable_post_in_podly, get_podly_config

logging.basicConfig(level=logging.INFO)

_WHITELIST_BODY = {"whitelisted": True, "trigger_processing": True}


def _fail(msg: str) -> None:
    """Raise an AssertionError.

    Raises:
        AssertionError: Always.

    """
    raise AssertionError(msg)


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
        self.get_calls: list[str] = []

    def post(self, url: str, json: object = None, timeout: float = 15.0) -> _FakeResponse:
        """Record post call and return next response.

        Returns:
            The next scheduled fake response.

        """
        _ = timeout
        self.post_calls.append((url, json))
        return self.post_responses.pop(0)

    def get(self, url: str, timeout: float = 15.0) -> _FakeResponse:
        """Record get call and return next response.

        Returns:
            The next scheduled fake response.

        """
        _ = timeout
        self.get_calls.append(url)
        return self.get_responses.pop(0)


def _feeds(*titles: str) -> _FakeResponse:
    return _FakeResponse(200, json.dumps([{"id": i + 1, "title": t} for i, t in enumerate(titles)]))


def _posts(*posts: dict[str, str]) -> _FakeResponse:
    return _FakeResponse(200, json.dumps({"items": list(posts)}))


def _discover(
    get_responses: list[_FakeResponse],
    *,
    guid: str | None = "unknown-guid",
    download_url: str | None = None,
    title: str | None = None,
    feed_name: str | None = None,
) -> tuple[bool, _FakeSession]:
    """Run enable_post_in_podly through the refresh-and-search path.

    The direct whitelist (if a GUID is given) 404s, the refresh succeeds, and the final
    whitelist of whatever GUID is discovered succeeds.

    Returns:
        (result, session) for assertions on the calls made.

    """
    posts = [_FakeResponse(200)]  # auth
    if guid:
        posts.append(_FakeResponse(404, "Not found"))  # direct whitelist
    posts += [_FakeResponse(200), _FakeResponse(200)]  # refresh-all, discovered whitelist
    session = _FakeSession(post_responses=posts, get_responses=get_responses)
    with patch("podcast_shared.podly.requests.Session", return_value=session), patch("time.sleep"):
        result = enable_post_in_podly(
            guid=guid,
            download_url=download_url,
            title=title,
            feed_name=feed_name,
            podly_url="https://podly.test",
            username="testuser",
            password="testpassword",
        )
    return result, session


def _whitelisted(session: _FakeSession) -> list[str]:
    return [url for url, body in session.post_calls if url.endswith("/whitelist") and body == _WHITELIST_BODY]


def test_get_podly_config() -> None:
    """Explicit params win and the URL's trailing slash is stripped."""
    url, user, pwd = get_podly_config("https://custom.podly.test/", "admin", "secret")
    if url != "https://custom.podly.test":
        _fail(f"Expected stripped trailing slash, got: {url}")
    if user != "admin" or pwd != "secret":
        _fail(f"Expected credentials, got: {user}, {pwd}")


def test_enable_post_in_podly_direct_guid_success() -> None:
    """A known GUID is whitelisted directly with no feed search."""
    session = _FakeSession(post_responses=[_FakeResponse(200), _FakeResponse(200)])
    with patch("podcast_shared.podly.requests.Session", return_value=session):
        result = enable_post_in_podly(
            guid="test-guid-123",
            title="Sample Episode",
            podly_url="https://podly.test",
            username="testuser",
            password="testpassword",
        )
    if not result:
        _fail("Expected enable_post_in_podly to succeed")
    if _whitelisted(session) != ["https://podly.test/api/posts/test-guid-123/whitelist"]:
        _fail(f"unexpected whitelist calls: {session.post_calls!r}")


def test_url_guid_is_percent_encoded() -> None:
    """A URL-shaped GUID with a query string is encoded into one path segment."""
    session = _FakeSession(post_responses=[_FakeResponse(200), _FakeResponse(200)])
    with patch("podcast_shared.podly.requests.Session", return_value=session):
        result = enable_post_in_podly(
            guid="https://example.com/?p=123#top",
            podly_url="https://podly.test",
            username="testuser",
            password="testpassword",
        )
    expected = "https://podly.test/api/posts/https%3A%2F%2Fexample.com%2F%3Fp%3D123%23top/whitelist"
    if not result or _whitelisted(session) != [expected]:
        _fail(f"GUID not encoded: {session.post_calls!r}")


def test_enable_post_in_podly_refresh_and_discover() -> None:
    """After a 404, the post is found by exact title inside the name-matched feed."""
    result, session = _discover(
        [
            _feeds("Example Show", "Another Show"),
            _posts({"guid": "discovered-guid", "title": "Week 1 With A Guest", "download_url": "http://a/b.mp3"}),
        ],
        title="Week 1  with a guest",  # case/whitespace differences still match exactly
        feed_name="Example Show",
    )
    if not result or _whitelisted(session)[-1:] != ["https://podly.test/api/posts/discovered-guid/whitelist"]:
        _fail(f"discovered GUID not whitelisted: {session.post_calls!r}")
    if session.get_calls[1:] != ["https://podly.test/api/feeds/1/posts?page_size=50"]:
        _fail(f"should search only the name-matched feed: {session.get_calls!r}")


def test_title_match_is_exact_not_substring() -> None:
    """A title that merely contains the wanted title is not a match (previous substring bug)."""
    result, session = _discover(
        [_feeds("Example Show"), _posts({"guid": "other", "title": "Week 1 With A Guest (Part 2)"})],
        title="Week 1 With A Guest",
        feed_name="Example Show",
    )
    if result or len(_whitelisted(session)) != 1:  # only the failed direct attempt
        _fail(f"substring title should not match: {session.post_calls!r}")


def test_empty_title_never_matches() -> None:
    """An empty title must not match every post ('' in x is always true)."""
    result, _session = _discover(
        [_feeds("Example Show"), _posts({"guid": "first-post", "title": "Anything"})],
        title="",
        feed_name="Example Show",
    )
    if result:
        _fail("empty title matched an arbitrary post")


def test_guid_match_beats_earlier_title_match() -> None:
    """Across all posts, an exact GUID match wins over a title match on an earlier post."""
    result, session = _discover(
        [
            _feeds("Example Show"),
            _posts(
                {"guid": "title-twin", "title": "Rerun"},
                {"guid": "real-guid", "title": "Rerun"},
            ),
        ],
        guid="real-guid",
        title="Rerun",
        feed_name="Example Show",
    )
    if not result or _whitelisted(session)[-1] != "https://podly.test/api/posts/real-guid/whitelist":
        _fail(f"GUID match should win: {session.post_calls!r}")


def test_title_match_not_trusted_without_feed_match() -> None:
    """With no name-matched feed, all feeds are searched but a title alone cannot match."""
    result, session = _discover(
        [
            _feeds("Show A", "Show B"),
            _posts({"guid": "a1", "title": "Mailbag"}),
            _posts({"guid": "b1", "title": "Mailbag", "download_url": "http://b/1.mp3"}),
        ],
        title="Mailbag",
        feed_name="Unrelated Name",
    )
    if result:
        _fail(f"title-only match across unrelated feeds was accepted: {session.post_calls!r}")
    result2, session2 = _discover(
        [
            _feeds("Show A", "Show B"),
            _posts({"guid": "a1", "title": "Mailbag"}),
            _posts({"guid": "b1", "title": "Mailbag", "download_url": "http://b/1.mp3"}),
        ],
        download_url="http://b/1.mp3",
        title="Mailbag",
        feed_name="Unrelated Name",
    )
    if not result2 or _whitelisted(session2)[-1] != "https://podly.test/api/posts/b1/whitelist":
        _fail(f"download_url match across all feeds should work: {session2.post_calls!r}")


def test_malformed_feeds_payload_fails_cleanly() -> None:
    """A non-list /feeds payload (or bad JSON) returns False instead of raising."""
    for body in ('{"error": "nope"}', "not json"):
        result, _session = _discover([_FakeResponse(200, body)], title="X", feed_name="Example Show")
        if result:
            _fail(f"malformed /feeds payload {body!r} should fail")


def test_enable_post_in_podly_auth_failure() -> None:
    """A failed login returns False without further calls."""
    session = _FakeSession(post_responses=[_FakeResponse(401, "Unauthorized")])
    with patch("podcast_shared.podly.requests.Session", return_value=session):
        result = enable_post_in_podly(
            guid="test-guid",
            podly_url="https://podly.test",
            username="wronguser",
            password="wrongpassword",
        )
    if result:
        _fail("Expected enable_post_in_podly to fail on auth error")
    if len(session.post_calls) != 1:
        _fail(f"no calls expected after failed auth: {session.post_calls!r}")


def run_tests() -> None:
    """Run all Podly client tests."""
    test_get_podly_config()
    test_enable_post_in_podly_direct_guid_success()
    test_enable_post_in_podly_refresh_and_discover()
    test_title_match_is_exact_not_substring()
    test_empty_title_never_matches()
    test_guid_match_beats_earlier_title_match()
    test_url_guid_is_percent_encoded()
    test_title_match_not_trusted_without_feed_match()
    test_malformed_feeds_payload_fails_cleanly()
    test_enable_post_in_podly_auth_failure()
    logging.info("podly tests passed")


if __name__ == "__main__":
    run_tests()
