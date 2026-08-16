"""Segment parsing, utterance planning, and stable voice assignment."""

import logging

from multivoice import (
    BLOCKQUOTE_MARKER,
    DEFAULT_NARRATOR_VOICE,
    DEFAULT_QUOTE_POOL,
    assign_voice,
    parse_segments,
    plan_article_utterances,
    plan_utterances,
)

BODY = (
    "NARRATOR: Here are the highlights on X.\n\n"
    "QUOTE Alice Reader: Making up numbers is inherently dubious.\n\n"
    "QUOTE Bob Reader: How many QALYs can I expect to gain?\n"
)

_M = BLOCKQUOTE_MARKER
ARTICLE = (
    "The author opens with a claim.\n\n"
    f"{_M}First quoted passage from a source.\n\n"
    "The author responds in their own voice.\n\n"
    f"{_M}Second quoted passage, a different source.\n"
    f"{_M}continuing the same quote.\n\n"
    "A closing thought."
)


def check_parse() -> None:
    """Verify segments parse into (role, text) with names extracted.

    Raises:
        AssertionError: If parsing does not match the expected segments.

    """
    segs = parse_segments(BODY)
    expected = [
        ("NARRATOR", "Here are the highlights on X."),
        ("Alice Reader", "Making up numbers is inherently dubious."),
        ("Bob Reader", "How many QALYs can I expect to gain?"),
    ]
    if segs != expected:
        msg = f"got {segs!r}"
        raise AssertionError(msg)


def check_plan() -> None:
    """Verify attribution is narrator-voiced, quotes carry only the words, with intro/outro.

    Raises:
        AssertionError: If the planned utterances are wrong.

    """
    utter = plan_utterances(parse_segments(BODY), "Example Blog", "Comment Highlights: X", "The post argues X.")
    expected = [
        ("Example Blog. Comment Highlights: X.", "NARRATOR"),
        ("The post argues X.", "NARRATOR"),
        ("Here are the highlights on X.", "NARRATOR"),
        ("Alice Reader wrote:", "NARRATOR"),
        ("Making up numbers is inherently dubious.", "Alice Reader"),
        ("Bob Reader wrote:", "NARRATOR"),
        ("How many QALYs can I expect to gain?", "Bob Reader"),
        ("Example Blog. Comment Highlights: X.", "NARRATOR"),
    ]
    if utter != expected:
        msg = f"got {utter!r}"
        raise AssertionError(msg)


def check_voices() -> None:
    """Verify narrator maps to its voice and commenters map stably into the pool.

    Raises:
        AssertionError: If voice assignment is wrong or unstable.

    """
    if assign_voice("NARRATOR", DEFAULT_NARRATOR_VOICE, DEFAULT_QUOTE_POOL) != DEFAULT_NARRATOR_VOICE:
        msg = "narrator voice mismatch"
        raise AssertionError(msg)
    v = assign_voice("Alice Reader", DEFAULT_NARRATOR_VOICE, DEFAULT_QUOTE_POOL)
    if v not in DEFAULT_QUOTE_POOL or v == DEFAULT_NARRATOR_VOICE:
        msg = f"commenter voice {v!r} not from pool"
        raise AssertionError(msg)
    if assign_voice("Alice Reader", DEFAULT_NARRATOR_VOICE, DEFAULT_QUOTE_POOL) != v:
        msg = "voice assignment must be stable across calls"
        raise AssertionError(msg)


def check_article_plan() -> None:
    """Verify article body maps to narrator/quote utterances, markers stripped, runs merged.

    Raises:
        AssertionError: If the planned utterances are wrong.

    """
    utter = plan_article_utterances(ARTICLE)
    quote_two = "Second quoted passage, a different source. continuing the same quote."
    expected = [
        ("The author opens with a claim.", "NARRATOR"),
        ("First quoted passage from a source.", "First quoted passage from a source."),
        ("The author responds in their own voice.", "NARRATOR"),
        (quote_two, quote_two),
        ("A closing thought.", "NARRATOR"),
    ]
    if utter != expected:
        msg = f"got {utter!r}"
        raise AssertionError(msg)


def check_article_voices_shared() -> None:
    """Verify article quotes route through the shared assign_voice: narrator voiced, quotes from pool.

    Raises:
        AssertionError: If any utterance maps to the wrong voice tier.

    """
    for _, speaker in plan_article_utterances(ARTICLE):
        voice = assign_voice(speaker, DEFAULT_NARRATOR_VOICE, DEFAULT_QUOTE_POOL)
        if speaker == "NARRATOR":
            if voice != DEFAULT_NARRATOR_VOICE:
                msg = "narrator utterance must use the narrator voice"
                raise AssertionError(msg)
        elif voice not in DEFAULT_QUOTE_POOL or voice == DEFAULT_NARRATOR_VOICE:
            msg = f"quote voice {voice!r} not from pool"
            raise AssertionError(msg)


def check_article_no_markers_all_narrator() -> None:
    """Verify body without markers yields narrator-only utterances.

    Raises:
        AssertionError: If unmarked text produces a non-narrator speaker.

    """
    utter = plan_article_utterances("Just prose.\n\nMore prose.")
    if utter != [("Just prose.", "NARRATOR"), ("More prose.", "NARRATOR")]:
        msg = f"got {utter!r}"
        raise AssertionError(msg)


if __name__ == "__main__":
    check_parse()
    check_plan()
    check_voices()
    check_article_plan()
    check_article_voices_shared()
    check_article_no_markers_all_narrator()
    logging.info("multivoice tests passed.")
