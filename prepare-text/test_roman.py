"""Tests for Roman-numeral normalization (TTS reads e.g. IV as 'eye-vee')."""

import logging

from prepare_text import normalize_roman_numerals

logging.basicConfig(level=logging.INFO)


def check(text: str, expected: str) -> None:
    """Assert normalize_roman_numerals(text) equals expected.

    Raises:
        AssertionError: If the normalized text does not match expected.

    """
    result, _ = normalize_roman_numerals(text)
    if result != expected:
        msg = f"\n  in:  {text!r}\n  got: {result!r}\n  exp: {expected!r}"
        raise AssertionError(msg)


def run_tests() -> None:
    """Run all Roman-numeral normalization tests."""
    # Section-header lines (e.g. I. II. III. dividers) — the only case
    check("Intro\n\nIV.\n\nBody text.", "Intro\n\nSection four.\n\nBody text.")
    check("I.\n\nfoo\n\nII.\n\nbar", "Section one.\n\nfoo\n\nSection two.\n\nbar")
    check("VI.", "Section six.")
    check("  III.  ", "Section three.")  # surrounding whitespace on the line
    check("XIV.", "Section fourteen.")

    # Numerals NOT alone on a line must never convert (this is scoped to headers only)
    check("This is a Title IX case.", "This is a Title IX case.")
    check("The Mark IV headset.", "The Mark IV headset.")
    check("They fought in World War II.", "They fought in World War II.")
    check("A line with IV. in the middle of it.", "A line with IV. in the middle of it.")
    check("He lives in Washington DC now.", "He lives in Washington DC now.")
    check("Only I know the answer.", "Only I know the answer.")

    # Out-of-range header (not 1-40) is left alone
    check("MMX.", "MMX.")

    logging.info("All normalize_roman_numerals tests passed successfully!")


if __name__ == "__main__":
    run_tests()
