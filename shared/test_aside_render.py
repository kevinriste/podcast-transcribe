"""Tests for aside rendering (script-style, repo house style)."""

import logging

from podcast_shared.aside_render import render_block_aside, resolve_markers
from podcast_shared.structural_extract import Block, serialize_blocks

logging.basicConfig(level=logging.INFO)


def _fail(msg: str) -> None:
    """Raise an AssertionError.

    Raises:
        AssertionError: Always.

    """
    raise AssertionError(msg)


def test_render_tweet() -> None:
    """A tweet renders with handle and text."""
    out = render_block_aside(Block(type="tweet", payload={"handle": "@tszzl", "text": "press the button"}))
    if out != "The author shares a tweet from @tszzl: press the button.":
        _fail(f"tweet aside was {out!r}")


def test_render_tweet_without_handle() -> None:
    """A handle-less tweet omits the 'from' clause."""
    out = render_block_aside(Block(type="tweet", payload={"handle": "", "text": "anon"}))
    if out != "The author shares a tweet: anon.":
        _fail(f"anon tweet aside was {out!r}")


def test_render_tweet_with_image_child() -> None:
    """A nested image is appended to the tweet aside."""
    block = Block(
        type="tweet",
        payload={"handle": "@x", "text": "look"},
        children=[Block(type="image", payload={"alt": "a bar chart"})],
    )
    out = render_block_aside(block)
    if "look." not in out or "Image: a bar chart." not in out:
        _fail(f"nested aside was {out!r}")


def test_render_generic_fallback() -> None:
    """Unknown types get a generic aside."""
    out = render_block_aside(Block(type="poll", payload={}))
    if out != "The author includes embedded content.":
        _fail(f"fallback aside was {out!r}")


def test_resolve_markers_replaces_embed() -> None:
    """A marker is replaced by its rendered aside; narrative text is preserved."""
    text, sidecar = serialize_blocks(
        [
            Block(type="text", payload={"text": "He seems spooked:"}),
            Block(type="tweet", payload={"handle": "@tszzl", "text": "press it"}),
            Block(type="text", payload={"text": "leading on."}),
        ]
    )
    out = resolve_markers(text, sidecar)
    if "⟦EMBED" in out:
        _fail(f"marker not resolved: {out!r}")
    if "He seems spooked:" not in out or "leading on." not in out:
        _fail(f"narrative lost: {out!r}")
    if "The author shares a tweet from @tszzl: press it." not in out:
        _fail(f"aside missing: {out!r}")


def test_resolve_markers_drops_unknown_id() -> None:
    """A marker with no sidecar entry is removed, not left literal."""
    out = resolve_markers("before ⟦EMBED:0007⟧ after", {})
    if "⟦EMBED" in out:
        _fail(f"unknown marker left: {out!r}")
    if "before" not in out or "after" not in out:
        _fail(f"surrounding text lost: {out!r}")


def run_tests() -> None:
    """Run all aside-render tests."""
    test_render_tweet()
    test_render_tweet_without_handle()
    test_render_tweet_with_image_child()
    test_render_generic_fallback()
    test_resolve_markers_replaces_embed()
    test_resolve_markers_drops_unknown_id()
    logging.info("aside render tests passed")


if __name__ == "__main__":
    run_tests()
