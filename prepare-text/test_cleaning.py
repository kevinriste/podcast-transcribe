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
    struct_out = pt.apply_general_cleaning(
        sample, {"source_kind": "substack", "extraction": "structured"}, {}, struct_stats
    )
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
    # Positive case 1: Leading author line and title line
    body_with_author = "Jane Author.\n\nWho Are the Good Guys?\n\nBody paragraph."
    if not pt.body_leads_with_byline(body_with_author, "Jane Author", "Who Are the Good Guys?"):
        _fail("failed to detect leading author in body")

    # Positive case 2: Blog archive post where line 0 is the title
    body_with_title_only = "Can Skeptics Appreciate Poetry?\n\nPoetry was never my thing..."
    if not pt.body_leads_with_byline(body_with_title_only, "Example Blog", "Can Skeptics Appreciate Poetry?"):
        _fail("failed to detect leading title in body")

    # Negative case 1: Plain body without author or title
    body_plain = "The White House announced a new initiative today.\n\nSecond paragraph."
    if pt.body_leads_with_byline(body_plain, "John Writer", "Tariffs"):
        _fail("falsely detected leading author in plain body")

    # Negative case 2: Publication name appears inside a body sentence (e.g. a newsletter quoting itself)
    body_mentioning_from = (
        "I Talked To Someone Running A Viral Video Account.\n\n"
        "The national meltdown over clipping has officially become a moral panic.\n\n"
        "A source told Example Weekly they had been covering the trend for years."
    )
    if pt.body_leads_with_byline(body_mentioning_from, "Example Weekly", "Everything's probably fake now"):
        _fail("falsely detected leading author when publication name appears inside body sentence")

    # Negative case 3: Title words appear inside an opening sentence (e.g. a title echoed in the first sentence)
    body_mentioning_title = (
        "My friends and I remember the movies of the 1970s. If you are old enough, see how many you can recall."
    )
    if pt.body_leads_with_byline(body_mentioning_title, "Sam Writer from Example Letter", "Movies of the 1970s"):
        _fail("falsely detected leading title when title appears inside opening prose sentence")

    # Line counts tell prepare-text where the existing intro ends
    good_guys = ("Jane Author", "Who Are the Good Guys?")
    poetry = ("Example Blog", "Can Skeptics Appreciate Poetry?")
    for body, (from_name, title), expected in (
        (body_with_author, good_guys, 2),
        ("Jane Author\nUnrelated line", good_guys, 1),
        (body_with_title_only, poetry, 1),
        (body_plain, good_guys, 0),
    ):
        got = pt.byline_line_count(body, from_name, title)
        if got != expected:
            _fail(f"byline_line_count({body!r}) = {got}, expected {expected}")


def test_period_runs_after_removals() -> None:
    """Text removals with line-end anchors match because period append runs after removals."""
    config: pt.PipelineConfig = {
        "text_removals": [
            {
                "pattern": r"^Advertisement$",
                "reason": "strip ads",
                "flags": "multiline",
            }
        ]
    }
    raw = "Lead paragraph\n\nAdvertisement\n\nFollowup paragraph\n"
    gc_stats: dict[str, dict[str, int | bool]] = {}
    cleaned = pt.apply_general_cleaning(raw, {}, config, gc_stats)
    removal_stats: dict[str, dict[str, int]] = {}
    cleaned = pt.apply_text_removals(cleaned, config, removal_stats)
    cleaned = pt.apply_end_of_line_punctuation(cleaned, {}, config, gc_stats)
    if "Advertisement" in cleaned:
        _fail(f"Advertisement was not removed: {cleaned!r}")
    if "Lead paragraph." not in cleaned:
        _fail(f"Period was not appended to lead paragraph: {cleaned!r}")


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


def test_divider_variants() -> None:
    """Whole-line dividers go; inline '---' reads as a dash; ***wrappers*** unwrap; f*** stays."""
    sample = "Intro.\n\n-----\n\nThe vote was 5---4 today.\n\n***Bold claim***\n\nWhat the f*** happened."
    stats: dict[str, dict[str, int | bool]] = {}
    out = pt.apply_general_cleaning(sample, {}, {}, stats)
    if "-----" in out or "---" in out:
        _fail(f"dash divider left: {out!r}")
    if "5 \u2014 4" not in out:
        _fail(f"inline --- should become a spoken dash: {out!r}")
    if "Bold claim" not in out or "***" in out.replace("f***", ""):
        _fail(f"***wrapper*** not unwrapped: {out!r}")
    if "f***" not in out:
        _fail(f"censored word mangled: {out!r}")
    # All three sub-rules share one stats key; counts accumulate instead of overwriting.
    if stats.get("triple_dash_removal", {}).get("matches") != 3:
        _fail(f"triple_dash_removal should count 3 matches: {stats.get('triple_dash_removal')}")


def test_relocate_footnote_mid_sentence() -> None:
    """A mid-sentence reference defers the aside to the end of the citing sentence."""
    sample = "Smith[1], who wrote it, disagreed. Next sentence.\n\n[1] A note."
    out, count = pt.relocate_footnotes(sample)
    if count != 1:
        _fail(f"expected 1 footnote relocated, got {count}")
    first = out.split("\n\n")[0]
    if first != "Smith, who wrote it, disagreed.":
        _fail(f"sentence was split by the aside: {out!r}")
    if "Next sentence." not in out.split("Footnote 1")[1]:
        _fail(f"aside should sit between the sentences: {out!r}")


def test_relocate_footnote_after_period() -> None:
    """A reference placed after the period ("sentence.[1]") puts the aside right there, not a sentence later."""
    out, _ = pt.relocate_footnotes("First sentence.[1] Second sentence. Third.\n\n[1] The note.")
    if out != "First sentence.\n\n\u2756 Footnote 1: The note.\n\nSecond sentence. Third.":
        _fail(f"aside should follow the citing sentence: {out!r}")
    # At a paragraph end, no extra blank lines; at the very end, no trailing break.
    out2, _ = pt.relocate_footnotes("End of para[1]\n\nNext para. Last line.[2]\n\n[1] N1.\n\n[2] N2.")
    expected = "End of para\n\n\u2756 Footnote 1: N1.\n\nNext para. Last line.\n\n\u2756 Footnote 2: N2."
    if out2 != expected:
        _fail(f"paragraph-end placement wrong: {out2!r}")
    # Abbreviations are not sentence ends, before or after the marker.
    out3, _ = pt.relocate_footnotes("Smith et al.[1] found it in the U.S. and e.g. Canada. Next.\n\n[1] N.")
    if out3 != "Smith et al. found it in the U.S. and e.g. Canada.\n\n\u2756 Footnote 1: N.\n\nNext.":
        _fail(f"abbreviation treated as a sentence end: {out3!r}")


def test_cleaning_override_can_enable_a_globally_disabled_step() -> None:
    """A matching per-source override set to true beats a global false (and vice versa)."""
    config: pt.PipelineConfig = {
        "general_cleaning": {
            "end_of_line_punctuation": False,
            "overrides": [{"match": {"from": {"contains": "Example"}}, "end_of_line_punctuation": True}],
        }
    }
    if not pt.is_cleaning_step_enabled("end_of_line_punctuation", {"from": "Example Letter"}, config):
        _fail("override true should win over global false")
    if pt.is_cleaning_step_enabled("end_of_line_punctuation", {"from": "Other"}, config):
        _fail("non-matching source should fall back to the global false")
    if pt.is_cleaning_step_enabled("unwrap_hard_wraps", {}, {}):
        _fail("unwrap_hard_wraps defaults off")
    if not pt.is_cleaning_step_enabled("triple_dash_removal", {}, {}):
        _fail("other steps default on")


def run_tests() -> None:
    """Run all cleaning tests."""
    logging.basicConfig(level=logging.INFO)
    test_urls_to_context_is_non_destructive()
    test_structured_profile_skips_repair_steps()
    test_url_step_runs_in_both_profiles()
    test_relocate_footnotes_as_aside()
    test_empty_brackets_and_dividers()
    test_byline_deduplication()
    test_period_runs_after_removals()
    test_divider_variants()
    test_relocate_footnote_mid_sentence()
    test_relocate_footnote_after_period()
    test_cleaning_override_can_enable_a_globally_disabled_step()
    logging.info("cleaning tests passed")


if __name__ == "__main__":
    run_tests()
