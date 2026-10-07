"""Listening-time formatting, speed parsing and intro marker placement."""

import logging
import os

from podcast_shared.listening_time import (
    LISTENING_TIME_MARKER,
    format_duration,
    listening_speed,
    listening_time_phrase,
    mark_intro_end,
    split_intro,
)


def check_format_duration() -> None:
    """Check durations are spelled as spoken, dropping zero parts.

    Raises:
        AssertionError: If a duration is spelled wrong.

    """
    cases = {
        45: "45 seconds",
        1: "1 second",
        0: "0 seconds",
        75: "1 minute, 15 seconds",
        150: "2 minutes, 30 seconds",
        120: "2 minutes",
        3600: "1 hour",
        3605: "1 hour, 5 seconds",
        9015: "2 hours, 30 minutes, and 15 seconds",
        3661: "1 hour, 1 minute, and 1 second",
    }
    for seconds, expected in cases.items():
        got = format_duration(seconds)
        if got != expected:
            msg = f"format_duration({seconds}) = {got!r}, expected {expected!r}"
            raise AssertionError(msg)


def check_phrase_divides_by_speed() -> None:
    """Check the phrase quotes the audio length divided by the listening speed.

    Raises:
        AssertionError: If the phrase is wrong.

    """
    got = listening_time_phrase(195_000, 1.3)  # 195 s at 1.3x = 150 s
    if got != "Listening time: 2 minutes, 30 seconds.":
        msg = f"unexpected phrase {got!r}"
        raise AssertionError(msg)


def check_listening_speed_env() -> None:
    """Check LISTENING_SPEED parsing, with a default for unset or invalid values.

    Raises:
        AssertionError: If the parsed speed is wrong.

    """
    for raw, expected in (("", 1.0), ("1.3", 1.3), ("abc", 1.0), ("0", 1.0), ("-2", 1.0)):
        os.environ["LISTENING_SPEED"] = raw
        if listening_speed() != expected:
            msg = f"LISTENING_SPEED={raw!r} gave {listening_speed()}, expected {expected}"
            raise AssertionError(msg)
    del os.environ["LISTENING_SPEED"]


def check_marker_round_trip() -> None:
    """Check the marker goes after the intro lines and splits back out cleanly.

    Raises:
        AssertionError: If marker placement or splitting is wrong.

    """
    text = "Author.\nTitle.\n\nFirst paragraph.\nSecond."
    marked = mark_intro_end(text, 2)
    if marked != f"Author.\nTitle.\n{LISTENING_TIME_MARKER}\n\nFirst paragraph.\nSecond.":
        msg = f"marker misplaced: {marked!r}"
        raise AssertionError(msg)
    if split_intro(marked) != ("Author.\nTitle.", "First paragraph.\nSecond."):
        msg = f"split wrong: {split_intro(marked)!r}"
        raise AssertionError(msg)
    if split_intro(text) != ("", text) or mark_intro_end(text, 0) != text:
        msg = "unmarked text must split to an empty intro and stay unchanged"
        raise AssertionError(msg)


if __name__ == "__main__":
    check_format_duration()
    check_phrase_divides_by_speed()
    check_listening_speed_env()
    check_marker_round_trip()
    logging.info("listening time tests passed.")
