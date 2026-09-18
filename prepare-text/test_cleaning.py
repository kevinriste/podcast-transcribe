"""Tests for intake-aware cleaning: URL-to-context and the structured/plaintext split."""

from __future__ import annotations

import logging

import prepare_text as pt


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def test_urls_to_context_is_non_destructive() -> None:
    """Bare URLs become "a link to <host>" (www stripped) instead of being deleted."""
    text, count = pt.urls_to_context("See https://www.hyvee.com/deals for more.")
    if text != "See a link to hyvee.com for more.":
        _fail(f"url rewrite wrong: {text!r}")
    if count != 1:
        _fail(f"count wrong: {count}")
    # A footnote whose whole content was a link keeps a spoken reference (no longer empties).
    fn, _ = pt.urls_to_context("Footnote 3: https://archives.universityaffairs.ca/x")
    if fn != "Footnote 3: a link to archives.universityaffairs.ca":
        _fail(f"footnote url rewrite wrong: {fn!r}")


def test_structured_profile_skips_repair_steps() -> None:
    """Structured intake skips plain-text repair steps like @ removal, but runs Substack boilerplate removal."""
    sample = "Body.\n\nShare\n\n@\n\nMore."
    struct_stats: dict[str, dict[str, int | bool]] = {}
    struct_out = pt.apply_general_cleaning(sample, {"source_kind": "substack", "extraction": "structured"}, {}, struct_stats)
    if "standalone_at_removal" in struct_stats:
        _fail(f"structured should skip standalone_at_removal: {list(struct_stats)}")
    if "substack_boilerplate_removal" not in struct_stats:
        _fail(f"structured should run substack_boilerplate_removal: {list(struct_stats)}")
    if "Share" in struct_out:
        _fail(f"structured failed to remove Share boilerplate: {struct_out!r}")
    if "@" not in struct_out:
        _fail(f"structured wrongly removed @: {struct_out!r}")

    plain_stats: dict[str, dict[str, int | bool]] = {}
    _ = pt.apply_general_cleaning(sample, {"source_kind": "substack", "extraction": "plaintext"}, {}, plain_stats)
    if "substack_boilerplate_removal" not in plain_stats or "standalone_at_removal" not in plain_stats:
        _fail(f"plaintext should apply repair steps: {list(plain_stats)}")


def test_byline_deduplication() -> None:
    """If body already leads with author or headline, header is not duplicated."""
    body_with_author = "Ross Douthat.\n\nWho Are the Good Guys?\n\nBody paragraph."
    if not pt.body_leads_with_byline(body_with_author, "Ross Douthat", "Who Are the Good Guys?"):
        _fail("failed to detect leading author in body")

    body_plain = "The White House announced a new initiative today.\n\nSecond paragraph."
    if pt.body_leads_with_byline(body_plain, "Matthew Yglesias", "Tariffs"):
        _fail("falsely detected leading author in plain body")


def test_url_step_runs_in_both_profiles() -> None:
    """URL-to-context is non-destructive, so it stays enabled for structured intake too."""
    for extraction in ("structured", "plaintext"):
        stats: dict[str, dict[str, int | bool]] = {}
        out = pt.apply_general_cleaning("Ref https://www.example.com/a here.", {"extraction": extraction}, {}, stats)
        if "a link to example.com" not in out:
            _fail(f"url step missing for {extraction}: {out!r}")


def test_relocate_footnotes_as_aside() -> None:
    """Footnotes relocated inline are formatted with ASIDE_MARKER."""
    sample = "First point.[1] Second point.\n\n[1] Details on the first point."
    out, count = pt.relocate_footnotes(sample)
    if count != 1:
        _fail(f"expected 1 footnote relocated, got {count}")
    expected_aside = "❖ Footnote 1: Details on the first point."
    if expected_aside not in out:
        _fail(f"aside marker not found in output: {out!r}")


def test_empty_brackets_and_dividers() -> None:
    """Empty brackets with whitespace and non-ASCII/spaced dividers are removed."""
    sample = "Start [ ] with (   ) and <  > brackets.\n\n———\n\nMiddle text.\n\n* * *\n\nEnd."
    stats: dict[str, dict[str, int | bool]] = {}
    out = pt.apply_general_cleaning(sample, {}, {}, stats)
    for token in ("[ ]", "(   )", "<  >", "———", "* * *"):
        if token in out:
            _fail(f"token {token!r} was not removed: {out!r}")


def run_tests() -> None:
    """Run all cleaning tests."""
    logging.basicConfig(level=logging.INFO)
    test_urls_to_context_is_non_destructive()
    test_structured_profile_skips_repair_steps()
    test_url_step_runs_in_both_profiles()
    test_relocate_footnotes_as_aside()
    test_empty_brackets_and_dividers()
    test_byline_deduplication()
    logging.info("cleaning tests passed")


if __name__ == "__main__":
    run_tests()
