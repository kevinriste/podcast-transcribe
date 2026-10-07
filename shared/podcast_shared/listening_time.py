"""Spoken "Listening time" announcement placed right after an episode's author/title intro."""

import logging
import os

logger = logging.getLogger(__name__)

# Line prepare-text writes where the author/title intro ends, so text-to-speech can
# synthesize the intro and body separately and splice the listening time between them.
# Same design rules as BLOCKQUOTE_MARKER: survives as its own line, never in prose.
LISTENING_TIME_MARKER = "⟦LISTENING_TIME⟧"

DEFAULT_LISTENING_SPEED = 1.0
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def listening_speed() -> float:
    """Read the playback speed the listening time is quoted at from LISTENING_SPEED.

    Returns:
        The speed multiplier, or DEFAULT_LISTENING_SPEED when unset or not a positive number.

    """
    raw = os.environ.get("LISTENING_SPEED", "").strip()
    if not raw:
        return DEFAULT_LISTENING_SPEED
    try:
        speed = float(raw)
    except ValueError:
        speed = 0.0
    if speed <= 0:
        logger.warning("Ignoring invalid LISTENING_SPEED=%r; using %s", raw, DEFAULT_LISTENING_SPEED)
        return DEFAULT_LISTENING_SPEED
    return speed


def _unit(count: int, name: str) -> str:
    return f"{count} {name}" if count == 1 else f"{count} {name}s"


def format_duration(total_seconds: int) -> str:
    """Spell a duration the way it should be spoken, leaving out zero parts.

    E.g. "45 seconds", "1 minute, 15 seconds", "2 hours, 30 minutes, and 15 seconds".

    Returns:
        The spoken duration.

    """
    hours, rest = divmod(max(total_seconds, 0), _SECONDS_PER_HOUR)
    minutes, seconds = divmod(rest, _SECONDS_PER_MINUTE)
    parts = [_unit(value, name) for value, name in ((hours, "hour"), (minutes, "minute"), (seconds, "second")) if value]
    if not parts:
        return _unit(0, "second")
    if len(parts) == 3:  # only all three parts take a serial "and"
        return f"{parts[0]}, {parts[1]}, and {parts[2]}"
    return ", ".join(parts)


def listening_time_phrase(audio_ms: float, speed: float) -> str:
    """Build the sentence announcing how long an episode takes to hear at ``speed``.

    Returns:
        E.g. "Listening time: 2 minutes, 30 seconds."

    """
    return f"Listening time: {format_duration(round(audio_ms / 1000 / speed))}."


def split_intro(text: str) -> tuple[str, str]:
    """Split text at the listening-time marker line into (intro, body).

    Returns:
        The text before and after the marker line, or ("", text) when there is no marker,
        so the listening time goes at the very start.

    """
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == LISTENING_TIME_MARKER:
            return "\n".join(lines[:idx]).strip(), "\n".join(lines[idx + 1 :]).strip()
    return "", text


def mark_intro_end(text: str, intro_lines: int) -> str:
    """Insert the listening-time marker after the first ``intro_lines`` non-blank lines.

    Returns:
        The text with the marker on its own line; unchanged when ``intro_lines`` is 0.

    """
    if intro_lines <= 0:
        return text
    lines = text.splitlines()
    seen = 0
    for idx, line in enumerate(lines):
        if line.strip():
            seen += 1
            if seen == intro_lines:
                return "\n".join([*lines[: idx + 1], LISTENING_TIME_MARKER, *lines[idx + 1 :]])
    return text
