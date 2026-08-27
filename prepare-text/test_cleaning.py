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
    """Structured intake skips plain-text repair steps; plaintext still applies them."""
    sample = "Body.\n\nShare\n\n@\n\nMore."
    struct_stats: dict[str, dict[str, int | bool]] = {}
    struct_out = pt.apply_general_cleaning(sample, {"source_kind": "substack", "extraction": "structured"}, {}, struct_stats)
    if "substack_boilerplate_removal" in struct_stats or "standalone_at_removal" in struct_stats:
        _fail(f"structured should skip repair steps: {list(struct_stats)}")
    if "Share" not in struct_out or "@" not in struct_out:
        _fail(f"structured wrongly mutated content: {struct_out!r}")

    plain_stats: dict[str, dict[str, int | bool]] = {}
    _ = pt.apply_general_cleaning(sample, {"source_kind": "substack", "extraction": "plaintext"}, {}, plain_stats)
    if "substack_boilerplate_removal" not in plain_stats or "standalone_at_removal" not in plain_stats:
        _fail(f"plaintext should apply repair steps: {list(plain_stats)}")


def test_url_step_runs_in_both_profiles() -> None:
    """URL-to-context is non-destructive, so it stays enabled for structured intake too."""
    for extraction in ("structured", "plaintext"):
        stats: dict[str, dict[str, int | bool]] = {}
        out = pt.apply_general_cleaning("Ref https://www.example.com/a here.", {"extraction": extraction}, {}, stats)
        if "a link to example.com" not in out:
            _fail(f"url step missing for {extraction}: {out!r}")


def run_tests() -> None:
    """Run all cleaning tests."""
    logging.basicConfig(level=logging.INFO)
    test_urls_to_context_is_non_destructive()
    test_structured_profile_skips_repair_steps()
    test_url_step_runs_in_both_profiles()
    logging.info("cleaning tests passed")


if __name__ == "__main__":
    run_tests()
