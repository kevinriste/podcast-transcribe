"""Tests for feed routing: which episodes go to the evergreen vs topical feed.

Routing keys on META_FROM (not the filename prefix): whole_sources always route
to the evergreen feed, length_gated_sources route there only at/above the word
gate, and an unconfigured (disabled) routing keeps everything topical.
"""

import logging

from text_to_speech import EvergreenRouting, final_output_dir, resolve_feed_dir

logging.basicConfig(level=logging.INFO)

_ROUTING = EvergreenRouting(
    output_dir=f"{final_output_dir}/evergreen",
    whole_sources=("example long-form blog", "another evergreen source"),
    length_gated_sources=("occasional long read author",),
    length_gate_words=2000,
)
_DISABLED = EvergreenRouting(
    output_dir=f"{final_output_dir}/evergreen", whole_sources=(), length_gated_sources=(), length_gate_words=0
)

_AT_GATE = "word " * 2000  # exactly length_gate_words -> evergreen (>=)
_BELOW_GATE = "word " * 1999  # one word short -> topical
_SHORT = "word " * 100


def check(meta_from: str, body: str, routing: EvergreenRouting, expected: str) -> None:
    """Assert resolve_feed_dir routes to the expected directory.

    Raises:
        AssertionError: If the resolved feed directory does not match expected.

    """
    result = resolve_feed_dir(meta_from, body, routing)
    if result != expected:
        msg = f"\n  from: {meta_from!r}\n  got:  {result!r}\n  exp:  {expected!r}"
        raise AssertionError(msg)


def run_tests() -> None:
    """Run all feed-routing tests."""
    # Whole-source evergreen -> evergreen feed, regardless of length.
    check("Example Long-form Blog", _SHORT, _ROUTING, _ROUTING.output_dir)
    check("Another Evergreen Source", _SHORT, _ROUTING, _ROUTING.output_dir)

    # Length-gated source -> evergreen only at/above the gate.
    check("Occasional Long Read Author", _AT_GATE, _ROUTING, _ROUTING.output_dir)
    check("Occasional Long Read Author", _BELOW_GATE, _ROUTING, final_output_dir)
    # Substring match still applies to co-authored / decorated bylines.
    check("Occasional Long Read Author and A. Coauthor", _AT_GATE, _ROUTING, _ROUTING.output_dir)

    # Everything else stays topical, even when long.
    check("Some Topical Columnist", _AT_GATE, _ROUTING, final_output_dir)
    check("Another Writer", _SHORT, _ROUTING, final_output_dir)

    # Disabled routing keeps everything topical.
    check("Example Long-form Blog", _AT_GATE, _DISABLED, final_output_dir)

    logging.info("All resolve_feed_dir tests passed successfully!")


if __name__ == "__main__":
    run_tests()
