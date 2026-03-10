# pyright: reportExplicitAny=false, reportAny=false, reportPrivateUsage=false, reportUnusedCallResult=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false
"""Tests for pure/testable functions in check-rss.py."""

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pyrsistent import PMap


def _load_check_rss() -> ModuleType:
    """Load check-rss.py as a module (main() guard prevents import-time execution).

    Returns
    -------
    - The loaded check-rss module.

    """
    # Remove previously cached version
    sys.modules.pop("check_rss", None)

    module_path = Path(__file__).parent / "check-rss.py"
    spec = importlib.util.spec_from_file_location("check_rss", module_path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)

    # Create a dummy feeds.txt if it doesn't exist (e.g. in CI where *.txt is gitignored).
    feeds_path = Path(__file__).parent / "feeds.txt"
    created_feeds = False
    if not feeds_path.exists():
        feeds_path.write_text("https://example.com/feed.xml\n", encoding="utf-8")
        created_feeds = True

    # Module no longer runs the feed loop at import time (wrapped in main()),
    # but we still need feeds.txt to exist for load_feeds().
    try:
        spec.loader.exec_module(mod)
    finally:
        if created_feeds:
            feeds_path.unlink(missing_ok=True)

    sys.modules["check_rss"] = mod
    return mod


@pytest.fixture(scope="session")
def mod():
    """Load the module once per test session.

    Returns
    -------
    - The loaded check-rss module.

    """
    return _load_check_rss()


# ===========================================================================
# get_entry_link
# ===========================================================================
class TestGetEntryLink:
    def test_returns_link_attribute(self, mod):
        entry = SimpleNamespace(link="https://example.com/article")
        assert mod.get_entry_link(entry) == "https://example.com/article"

    def test_returns_href_from_links_when_no_link(self, mod):
        entry = SimpleNamespace(link=None)
        entry.get = lambda key: [{"href": "https://fallback.com/page"}] if key == "links" else None
        assert mod.get_entry_link(entry) == "https://fallback.com/page"

    def test_returns_empty_string_when_no_link_or_links(self, mod):
        entry = SimpleNamespace(link=None)
        entry.get = lambda _key: None
        result = mod.get_entry_link(entry)
        assert result == ""  # noqa: PLC1901

    def test_skips_empty_hrefs_in_links(self, mod):
        entry = SimpleNamespace(link=None)
        entry.get = lambda key: [{"href": ""}, {"href": "https://second.com"}] if key == "links" else None
        assert mod.get_entry_link(entry) == "https://second.com"

    def test_returns_empty_when_links_have_no_href(self, mod):
        entry = SimpleNamespace(link=None)
        entry.get = lambda key: [{"type": "text/html"}] if key == "links" else None
        result = mod.get_entry_link(entry)
        assert result == ""  # noqa: PLC1901


# ===========================================================================
# build_metadata_block
# ===========================================================================
class TestBuildMetadataBlock:
    def test_basic_metadata(self, mod):
        result = mod.build_metadata_block("My Feed", "Article Title", "https://example.com/article")
        assert "META_FROM: My Feed" in result
        assert "META_TITLE: Article Title" in result
        assert "META_SOURCE_URL: https://example.com/article" in result
        assert "META_SOURCE_KIND: rss" in result
        assert "META_INTAKE_TYPE: rss" in result

    def test_metadata_lines_are_newline_separated(self, mod):
        result = mod.build_metadata_block("Feed", "Title", "https://url.com")
        lines = result.split("\n")
        assert len(lines) == 5

    def test_empty_strings(self, mod):
        result = mod.build_metadata_block("", "", "")
        assert "META_FROM: " in result
        assert "META_TITLE: " in result
        assert "META_SOURCE_URL: " in result


# ===========================================================================
# snapshot_to_dict
# ===========================================================================
class TestSnapshotToDict:
    def test_converts_snapshot_attributes_to_pmap(self, mod):
        snapshot = MagicMock()
        snapshot.archive_url = "https://web.archive.org/web/123/https://example.com"
        snapshot.datetime_timestamp = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
        snapshot.original = "https://example.com"
        snapshot.statuscode = "200"

        result = mod.snapshot_to_dict(snapshot)

        assert isinstance(result, PMap)
        # datetime values should be ISO-formatted strings
        assert "2024-01-15" in result["datetime_timestamp"]

    def test_returns_immutable_pmap(self, mod):
        snapshot = MagicMock()
        snapshot.url = "https://example.com"
        result = mod.snapshot_to_dict(snapshot)
        assert isinstance(result, PMap)


# ===========================================================================
# filter_calendar_captures
# ===========================================================================
class TestFilterCalendarCaptures:
    def test_filters_matching_collection(self, mod):
        items = [[1234567890, 200, 0]]
        collections = [["global.nytimes.com"]]
        result = mod.filter_calendar_captures(
            items, collections, 2024, "https://nytimes.com/article", "global.nytimes.com"
        )
        assert len(result) == 1
        assert "web.archive.org" in result[0]
        assert "https://nytimes.com/article" in result[0]

    def test_skips_non_matching_collection(self, mod):
        items = [[1234567890, 200, 0]]
        collections = [["other.collection"]]
        result = mod.filter_calendar_captures(
            items, collections, 2024, "https://nytimes.com/article", "global.nytimes.com"
        )
        assert len(result) == 0

    def test_skips_out_of_range_collection_index(self, mod):
        items = [[1234567890, 200, 5]]  # index 5, but only 1 collection
        collections = [["global.nytimes.com"]]
        result = mod.filter_calendar_captures(
            items, collections, 2024, "https://nytimes.com/article", "global.nytimes.com"
        )
        assert len(result) == 0

    def test_pads_9_digit_timestamps(self, mod):
        # 9-digit timestamp should be zero-padded to 10 digits
        items = [[123456789, 200, 0]]
        collections = [["global.nytimes.com"]]
        result = mod.filter_calendar_captures(
            items, collections, 2024, "https://nytimes.com/article", "global.nytimes.com"
        )
        assert len(result) == 1
        # Should contain the year + zero-padded timestamp
        assert "20240123456789" in result[0]

    def test_does_not_pad_non_9_digit_timestamps(self, mod):
        items = [[12345678901, 200, 0]]  # 11 digits, should not be padded
        collections = [["global.nytimes.com"]]
        result = mod.filter_calendar_captures(
            items, collections, 2024, "https://nytimes.com/article", "global.nytimes.com"
        )
        assert len(result) == 1
        assert "202412345678901" in result[0]

    def test_multiple_items(self, mod):
        items = [
            [1111111111, 200, 0],
            [2222222222, 200, 1],
            [3333333333, 200, 0],
        ]
        collections = [["global.nytimes.com"], ["other.collection"]]
        result = mod.filter_calendar_captures(
            items, collections, 2024, "https://nytimes.com/article", "global.nytimes.com"
        )
        # Only items with collection index 0 match
        assert len(result) == 2

    def test_returns_tuple(self, mod):
        result = mod.filter_calendar_captures([], [], 2024, "https://example.com", "nytimes")
        assert isinstance(result, tuple)

    def test_empty_items(self, mod):
        result = mod.filter_calendar_captures([], [["global.nytimes.com"]], 2024, "https://example.com", "nytimes")
        assert result == ()


# ===========================================================================
# find_most_recent_guid_index
# ===========================================================================
class TestFindMostRecentGuidIndex:
    def test_returns_index_when_found(self, mod):
        guids = ("a", "b", "c", "d")
        assert mod.find_most_recent_guid_index(guids, "c") == 2

    def test_returns_none_when_not_found(self, mod):
        guids = ("a", "b", "c")
        assert mod.find_most_recent_guid_index(guids, "z") is None

    def test_returns_none_when_guid_is_none(self, mod):
        guids = ("a", "b", "c")
        assert mod.find_most_recent_guid_index(guids, None) is None

    def test_returns_zero_for_first_element(self, mod):
        guids = ("x", "y", "z")
        assert mod.find_most_recent_guid_index(guids, "x") == 0

    def test_empty_guids(self, mod):
        assert mod.find_most_recent_guid_index((), "a") is None

    def test_works_with_list(self, mod):
        guids = ["a", "b", "c"]
        assert mod.find_most_recent_guid_index(guids, "b") == 1


# ===========================================================================
# send_gotify_notification
# ===========================================================================
class TestSendGotifyNotification:
    def test_sends_notification_with_correct_data(self, mod, monkeypatch):
        monkeypatch.setenv("GOTIFY_SERVER", "https://gotify.example.com")
        monkeypatch.setenv("GOTIFY_TOKEN", "test-token-123")

        mock_post = MagicMock()
        monkeypatch.setattr(mod.requests, "post", mock_post)

        mod.send_gotify_notification("Test Title", "Test Message", priority=8)

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "https://gotify.example.com/message?token=test-token-123"
        assert call_args[1]["data"]["title"] == "Test Title"
        assert call_args[1]["data"]["message"] == "Test Message"
        assert call_args[1]["data"]["priority"] == 8
        assert call_args[1]["timeout"] == 30

    def test_default_priority_is_six(self, mod, monkeypatch):
        monkeypatch.setenv("GOTIFY_SERVER", "https://gotify.example.com")
        monkeypatch.setenv("GOTIFY_TOKEN", "test-token")

        mock_post = MagicMock()
        monkeypatch.setattr(mod.requests, "post", mock_post)

        mod.send_gotify_notification("Title", "Message")

        call_args = mock_post.call_args
        assert call_args[1]["data"]["priority"] == 6

    def test_skips_when_gotify_server_not_set(self, mod, monkeypatch):
        monkeypatch.delenv("GOTIFY_SERVER", raising=False)
        monkeypatch.delenv("GOTIFY_TOKEN", raising=False)

        mock_post = MagicMock()
        monkeypatch.setattr(mod.requests, "post", mock_post)

        mod.send_gotify_notification("Title", "Message")

        mock_post.assert_not_called()

    def test_skips_when_gotify_token_not_set(self, mod, monkeypatch):
        monkeypatch.setenv("GOTIFY_SERVER", "https://gotify.example.com")
        monkeypatch.delenv("GOTIFY_TOKEN", raising=False)

        mock_post = MagicMock()
        monkeypatch.setattr(mod.requests, "post", mock_post)

        mod.send_gotify_notification("Title", "Message")

        mock_post.assert_not_called()

    def test_does_not_raise_when_post_throws(self, mod, monkeypatch):
        monkeypatch.setenv("GOTIFY_SERVER", "https://gotify.example.com")
        monkeypatch.setenv("GOTIFY_TOKEN", "test-token")

        mock_post = MagicMock(side_effect=mod.requests.ConnectionError("network down"))
        monkeypatch.setattr(mod.requests, "post", mock_post)

        # Should not raise
        mod.send_gotify_notification("Title", "Message")


# ===========================================================================
# Module-level constants
# ===========================================================================
class TestModuleConstants:
    def test_wayback_feeds_is_tuple(self, mod):
        assert isinstance(mod.wayback_feeds, tuple)

    def test_load_feeds_returns_tuple(self, mod, tmp_path, monkeypatch):
        feeds_path = tmp_path / "feeds.txt"
        feeds_path.write_text("https://example.com/feed1.xml\nhttps://example.com/feed2.xml\n", encoding="utf-8")
        monkeypatch.setattr(mod, "feeds_file", str(feeds_path))
        result = mod.load_feeds()
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_load_feeds_skips_empty_lines(self, mod, tmp_path, monkeypatch):
        feeds_path = tmp_path / "feeds.txt"
        feeds_path.write_text("https://example.com/feed.xml\n\n", encoding="utf-8")
        monkeypatch.setattr(mod, "feeds_file", str(feeds_path))
        result = mod.load_feeds()
        assert result == ("https://example.com/feed.xml",)

    def test_wayback_feeds_contains_nyt_urls(self, mod):
        assert all("nytimes.com" in url for url in mod.wayback_feeds)
