"""build_briefing returns None on failure; prompt/format constants exist."""

import logging
import os

import comment_briefing as cb


def check() -> None:
    """Verify constants, prompt shape, and None-on-failure behavior.

    Raises:
        AssertionError: If a constant, the prompt, or the failure path is wrong.

    """
    if not cb.MODEL:
        msg = f"unexpected model {cb.MODEL!r}"
        raise AssertionError(msg)
    if "CAMPS" not in cb.ANALYSIS_PROMPT or "FRAMING" not in cb.ANALYSIS_PROMPT:
        msg = "stage-1 analysis prompt missing camps/framing instructions"
        raise AssertionError(msg)
    if "NARRATOR:" not in cb.WRITE_PROMPT or "QUOTE" not in cb.WRITE_PROMPT:
        msg = "stage-2 write prompt missing segment-format instructions"
        raise AssertionError(msg)
    # With no API key, _post_model returns None, so build_briefing must return None
    # (never raise) — the caller relies on this to skip the comment episode.
    _ = os.environ.pop("OPENAI_API_KEY", None)
    if cb.build_briefing("T", [("A", "x", 0)], "a summary") is not None:
        msg = "build_briefing should return None when generation fails"
        raise AssertionError(msg)


if __name__ == "__main__":
    check()
    logging.info("build_briefing tests passed.")
