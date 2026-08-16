"""archive-comments routes to multi-voice; META_PUB_DATE parses to a datetime."""

import logging
from datetime import datetime

from multivoice import BLOCKQUOTE_MARKER, strip_markers
from text_to_speech import article_multivoice_plan, is_comment_episode, parse_pub_date

_M = BLOCKQUOTE_MARKER
_TWO_QUOTES = f"Intro paragraph.\n\n{_M}First quote.\n\nMiddle paragraph.\n\n{_M}Second quote.\n\nEnd."
_ONE_QUOTE = f"Intro paragraph.\n\n{_M}Only quote.\n\nEnd."
_NO_QUOTES = "Just prose.\n\nMore prose."


def check() -> None:
    """Verify comment-episode detection and pub-date parsing.

    Raises:
        AssertionError: If routing detection or date parsing is wrong.

    """
    if not is_comment_episode({"intake_type": "archive-comments"}):
        msg = "archive-comments should be detected as a comment episode"
        raise AssertionError(msg)
    if is_comment_episode({"intake_type": "archive"}):
        msg = "archive should not be a comment episode"
        raise AssertionError(msg)
    dt = parse_pub_date({"pub_date": "2013-05-02T12:01:00+00:00"})
    if not isinstance(dt, datetime) or dt.minute != 1:
        msg = f"unexpected parsed pub date: {dt!r}"
        raise AssertionError(msg)
    if parse_pub_date({}) is not None:
        msg = "missing pub_date should parse to None"
        raise AssertionError(msg)


def check_article_gate() -> None:
    """Verify multi-voice fires for WaveNet episodes with any block quote, never for Gemini.

    Raises:
        AssertionError: If the gate or engine restriction is wrong.

    """
    plan = article_multivoice_plan(_TWO_QUOTES, "wavenet")
    if plan is None or sum(1 for _, speaker in plan if speaker != "NARRATOR") != 2:
        msg = f"two-quote WaveNet episode should plan two quotes: {plan!r}"
        raise AssertionError(msg)
    if article_multivoice_plan(_ONE_QUOTE, "wavenet") is None:
        msg = "a single quote should qualify for multi-voice"
        raise AssertionError(msg)
    if article_multivoice_plan(_NO_QUOTES, "wavenet") is not None:
        msg = "prose with no quotes should stay single-voice"
        raise AssertionError(msg)
    if article_multivoice_plan(_TWO_QUOTES, "gemini-flash") is not None:
        msg = "Gemini-routed episodes must not use the WaveNet multi-voice path"
        raise AssertionError(msg)


def check_strip_markers() -> None:
    """Verify strip_markers removes markers while leaving the quoted text and layout.

    Raises:
        AssertionError: If stripping is wrong.

    """
    if strip_markers(f"A line.\n\n{_M}A quote.\n\nAnother line.") != "A line.\n\nA quote.\n\nAnother line.":
        msg = "strip_markers should remove only the marker prefix"
        raise AssertionError(msg)


if __name__ == "__main__":
    check()
    check_article_gate()
    check_strip_markers()
    logging.info("comment routing tests passed.")
