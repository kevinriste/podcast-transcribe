"""Tests for the podly_process filter action in prepare_text (script-style)."""

from __future__ import annotations

import logging
import pathlib
import tempfile
from unittest.mock import MagicMock, patch

import yaml

import prepare_text as pt

logging.basicConfig(level=logging.INFO)

_RAW = (
    "META_FROM: Example Show\n"
    "META_TITLE: Week 1 With A Guest\n"
    "META_GUID: example-guid-456\n"
    "META_SOURCE_URL: https://example.com/audio.mp3\n"
    "META_SOURCE_NAME: Example Show\n"
    "\n"
    "Episode summary notes here."
)


def _fail(msg: str) -> None:
    """Raise an AssertionError.

    Raises:
        AssertionError: Always.

    """
    raise AssertionError(msg)


def _config() -> pt.PipelineConfig:
    return {
        "filters": [
            {
                "match": {"from": {"contains": "Example Show"}, "title": {"contains": "Guest"}},
                "action": "podly_process",
                "reason": "Example Show guest episode",
            },
            {
                "match": {"from": {"contains": "Example Show"}},
                "action": "skip",
                "reason": "Example Show general skip",
            },
        ],
    }


def test_validate_config_accepts_podly_action() -> None:
    """podly_process is a valid action and a specific-before-broad ordering is clean."""
    cfg = _config()
    pt.validate_config(cfg)
    errors = pt.validate_rule_ordering(cfg.get("filters", []))
    if errors:
        _fail(f"Expected 0 ordering errors, got: {errors}")


def test_validate_config_rejects_unknown_filter_key() -> None:
    """Retired keys like a per-rule podly block (or typos) are rejected, not ignored."""
    for extra in ("podly", "podly_process", "actoin"):
        cfg = _config()
        rules = cfg.get("filters", [])
        bad = dict(rules[0])
        bad[extra] = True
        # Round-trip through YAML, as load_config does, to get a config the types can't vouch for.
        parsed: pt.PipelineConfig = yaml.safe_load(yaml.safe_dump({"filters": [bad, rules[1]]})) or {}
        try:
            pt.validate_config(parsed)
        except ValueError:
            continue
        _fail(f"unknown filter key {extra!r} was accepted")


def test_validate_config_rejects_retired_podly_alias() -> None:
    """The old 'podly' action alias is no longer accepted."""
    cfg = _config()
    rules = cfg.get("filters", [])
    rules[0]["action"] = "podly"
    try:
        pt.validate_config(cfg)
    except ValueError:
        return
    _fail("action 'podly' was accepted")


def test_rule_ordering_rejects_broad_podly_before_specific() -> None:
    """A broad podly_process rule shadowing a specific rule is an ordering error."""
    filters: list[pt.FilterRule] = [
        {"match": {"from": {"contains": "Example Show"}}, "action": "podly_process", "reason": "broad"},
        {
            "match": {"from": {"contains": "Example Show"}, "title": {"contains": "Guest"}},
            "action": "skip",
            "reason": "specific",
        },
    ]
    if not pt.validate_rule_ordering(filters):
        _fail("Expected ordering error when broad podly_process precedes specific rule")


def _run_podly_file(*, enabled: bool) -> tuple[MagicMock, MagicMock, str, pt.FileStats | None]:
    """Process one podly_process file with Podly and Gotify mocked.

    Returns:
        (podly mock, gotify mock, filtered file text, the file's stats entry).

    """
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        dirs = {name: tmp / name for name in ("cleaned", "raw_archive", "cleaned_archive", "filtered")}
        for path in dirs.values():
            path.mkdir()
        with (
            patch("prepare_text.enable_post_in_podly", return_value=enabled) as mock_podly,
            patch("prepare_text.send_gotify_notification") as mock_notify,
            patch.object(pt, "CLEANED_OUTPUT_DIR", str(dirs["cleaned"])),
            patch.object(pt, "RAW_ARCHIVE_DIR", str(dirs["raw_archive"])),
            patch.object(pt, "CLEANED_ARCHIVE_DIR", str(dirs["cleaned_archive"])),
            patch.object(pt, "FILTERED_DIR", str(dirs["filtered"])),
        ):
            src = tmp / "20260907-120000-Example Show- Week 1 With A Guest.txt"
            _ = src.write_text(_RAW, encoding="utf-8")
            stats: dict[str, pt.FileStats] = {}
            pt.process_file(src, _config(), stats)
        filtered = dirs["filtered"] / src.name
        if not filtered.exists():
            _fail("file was not moved to the filtered dir")
        if (dirs["cleaned"] / src.name).exists():
            _fail("podly_process file must not reach TTS")
        if not (dirs["raw_archive"] / src.name).exists():
            _fail("raw file was not archived")
        file_stat = next((s for s in stats.values() if s.get("file") == src.name), None)
        return mock_podly, mock_notify, filtered.read_text(encoding="utf-8"), file_stat


def test_podly_process_enables_and_skips_tts() -> None:
    """A successful enable filters the file silently, passing the episode identifiers."""
    mock_podly, mock_notify, filtered_text, file_stat = _run_podly_file(enabled=True)
    mock_podly.assert_called_once_with(
        guid="example-guid-456",
        download_url="https://example.com/audio.mp3",
        title="Week 1 With A Guest",
        feed_name="Example Show",
    )
    if mock_notify.called:
        _fail("no Gotify alert expected on success")
    if file_stat is None or file_stat.get("outcome") != "filtered":
        _fail(f"unexpected stats: {file_stat!r}")
    if "FAILED" in filtered_text:
        _fail(f"success recorded as failure: {filtered_text!r}")


def test_podly_process_failure_alerts_and_records_reason() -> None:
    """A failed enable still filters the file, but alerts and says so in the recorded reason."""
    _mock_podly, mock_notify, filtered_text, _file_stat = _run_podly_file(enabled=False)
    if not mock_notify.called:
        _fail("expected a Gotify alert when the Podly enable fails")
    if "Podly enable FAILED" not in filtered_text:
        _fail(f"failure not reflected in filtered reason: {filtered_text!r}")


def run_tests() -> None:
    """Run all podly filter tests."""
    test_validate_config_accepts_podly_action()
    test_validate_config_rejects_unknown_filter_key()
    test_validate_config_rejects_retired_podly_alias()
    test_rule_ordering_rejects_broad_podly_before_specific()
    test_podly_process_enables_and_skips_tts()
    test_podly_process_failure_alerts_and_records_reason()
    logging.info("podly filter tests passed")


if __name__ == "__main__":
    run_tests()
