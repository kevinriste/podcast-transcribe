"""Tests for podly_process filter action and Podly integration in prepare_text."""

from __future__ import annotations

import pathlib
import tempfile
from unittest.mock import patch

import prepare_text as pt


def _require(cond: bool, msg: str) -> None:  # noqa: FBT001
    """Require condition to be true or raise AssertionError.

    Raises:
        AssertionError: If cond is false.

    """
    if not cond:
        raise AssertionError(msg)


def test_validate_config_accepts_podly_action() -> None:
    """Test validate_config allows podly_process and podly configuration."""
    valid_cfg: pt.PipelineConfig = {
        "filters": [
            {
                "match": {
                    "from": {"contains": "Bill Simmons"},
                    "title": {"contains": "Cousin Sal"},
                },
                "action": "podly_process",
                "reason": "Bill Simmons Cousin Sal episode",
                "podly": {
                    "url": "https://podly.test",
                    "username": "user",
                    "password": "pwd",
                },
            },
            {
                "match": {
                    "from": {"contains": "Bill Simmons"},
                },
                "action": "skip",
                "reason": "Bill Simmons general skip",
            },
        ],
    }
    # Should not raise
    pt.validate_config(valid_cfg)
    errors = pt.validate_rule_ordering(valid_cfg["filters"])
    _require(len(errors) == 0, f"Expected 0 ordering errors, got: {errors}")


def test_rule_ordering_rejects_broad_podly_before_specific() -> None:
    """Test rule ordering detects broad podly_process before specific rule."""
    invalid_cfg: pt.PipelineConfig = {
        "filters": [
            {
                "match": {
                    "from": {"contains": "Bill Simmons"},
                },
                "action": "podly_process",
                "reason": "Bill Simmons broad rule",
            },
            {
                "match": {
                    "from": {"contains": "Bill Simmons"},
                    "title": {"contains": "Cousin Sal"},
                },
                "action": "skip",
                "reason": "Cousin Sal specific rule",
            },
        ],
    }
    errors = pt.validate_rule_ordering(invalid_cfg["filters"])
    _require(len(errors) > 0, "Expected ordering error when broad podly_process precedes specific rule")


def test_process_file_executes_podly_process_and_skips_tts() -> None:
    """Verify podly_process triggers enable_post_in_podly, logs only, and filters file."""
    raw_content = (
        "META_FROM: The Bill Simmons Podcast\n"
        "META_TITLE: NFL Week 1 With Cousin Sal\n"
        "META_GUID: simmons-guid-456\n"
        "META_SOURCE_URL: https://example.com/audio.mp3\n"
        "META_SOURCE_NAME: The Bill Simmons Podcast\n"
        "\n"
        "Episode summary notes here."
    )

    config: pt.PipelineConfig = {
        "filters": [
            {
                "match": {
                    "from": {"contains": "Bill Simmons"},
                    "title": {"contains": "Cousin Sal"},
                },
                "action": "podly_process",
                "reason": "Bill Simmons Cousin Sal episode",
            },
            {
                "match": {
                    "from": {"contains": "Bill Simmons"},
                },
                "action": "skip",
                "reason": "Bill Simmons general skip",
            },
        ],
    }

    with (
        tempfile.TemporaryDirectory() as d,
        patch("prepare_text.enable_post_in_podly", return_value=True) as mock_podly,
        patch("prepare_text.send_gotify_notification") as mock_notify,
    ):
        tmp = pathlib.Path(d)
        for name in ("CLEANED_OUTPUT_DIR", "RAW_ARCHIVE_DIR", "CLEANED_ARCHIVE_DIR", "FILTERED_DIR"):
            p = tmp / name
            p.mkdir(parents=True, exist_ok=True)
            setattr(pt, name, str(p))

        src = tmp / "20260907-120000-The Bill Simmons Podcast- NFL Week 1 With Cousin Sal.txt"
        _ = src.write_text(raw_content, encoding="utf-8")
        stats: dict[str, pt.FileStats] = {}

        pt.process_file(src, config, stats)

        # 1. enable_post_in_podly must have been called with metadata
        _require(mock_podly.called, "enable_post_in_podly was not called")
        mock_podly.assert_called_once_with(
            guid="simmons-guid-456",
            download_url="https://example.com/audio.mp3",
            title="NFL Week 1 With Cousin Sal",
            feed_name="The Bill Simmons Podcast",
            podly_url=None,
            username=None,
            password=None,
        )

        # 2. No Gotify notification should be sent (log only)
        _require(not mock_notify.called, "send_gotify_notification should not have been called")

        # 3. File was moved to FILTERED_DIR, not CLEANED_OUTPUT_DIR
        filtered_path = tmp / "FILTERED_DIR" / src.name
        cleaned_path = tmp / "CLEANED_OUTPUT_DIR" / src.name
        _require(filtered_path.exists(), f"File was not moved to filtered dir: {filtered_path}")
        _require(not cleaned_path.exists(), f"File should not exist in cleaned dir: {cleaned_path}")

        # 4. Raw file was archived
        raw_archived = tmp / "RAW_ARCHIVE_DIR" / src.name
        _require(raw_archived.exists(), f"File was not archived in raw archive: {raw_archived}")

        # 5. Outcome recorded as filtered
        file_stat = next((s for s in stats.values() if s.get("file") == src.name), None)
        _require(file_stat is not None, "Stats not recorded for file")
        if file_stat is not None:
            _require(file_stat.get("outcome") == "filtered", f"Unexpected outcome: {file_stat.get('outcome')}")
