"""Segment parsing, utterance planning, and stable voice assignment."""

import logging

from comment_voices import DEFAULT_NARRATOR_VOICE, DEFAULT_QUOTE_POOL, assign_voice, parse_segments, plan_utterances

BODY = (
    "NARRATOR: Here are the highlights on X.\n\n"
    "QUOTE Alice Reader: Making up numbers is inherently dubious.\n\n"
    "QUOTE Bob Reader: How many QALYs can I expect to gain?\n"
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


if __name__ == "__main__":
    check_parse()
    check_plan()
    check_voices()
    logging.info("comment_voices tests passed.")
