"""Tests for the structural extractor (script-style, repo house style)."""

import json
import logging
from typing import NoReturn

from bs4 import BeautifulSoup
from bs4.element import Tag

from podcast_shared.structural_extract import (
    Block,
    block_from_dict,
    extract_blocks,
    extract_tweet,
    find_content_region,
    is_tweet,
    serialize_blocks,
)

logging.basicConfig(level=logging.INFO)

_TWEET_HTML = (
    '<table class="twitter-embed tweet" data-component-name="TweetToDOMStatic"><tbody><tr><td>'
    "<div>roon</div><div>@tszzl</div>"
    "<div>if we could coordinate a global capabilities slowdown today i would likely press that magic button</div>"
    "<div>8:59 PM · Jul 25, 2026 · 568K Views</div>"
    "<div>354 Replies · 249 Reposts · 3.51K Likes</div>"
    "</td></tr></tbody></table>"
)


def _fail(msg: str) -> NoReturn:
    """Raise an AssertionError.

    Raises:
        AssertionError: Always.

    """
    raise AssertionError(msg)


def _tweet_tag() -> Tag:
    el = BeautifulSoup(_TWEET_HTML, "html.parser").find("table")
    if not isinstance(el, Tag):
        _fail("fixture has no table")
    return el


def test_region_prefers_substack_body() -> None:
    """Substack's div.body.markup wins over the surrounding chrome."""
    html = (
        '<html><body><div class="header">nav</div>'
        '<div class="body markup"><p>real content</p></div></body></html>'
    )
    region = find_content_region(html)
    text = region.get_text(" ", strip=True)
    if "real content" not in text or "nav" in text:
        _fail(f"region text was {text!r}")


def test_region_falls_back_to_article_then_document() -> None:
    """Without a Substack body, prefer <article>, else the whole document."""
    art = find_content_region("<html><body><article><p>A</p></article><p>B</p></body></html>")
    if art.get_text(" ", strip=True) != "A":
        _fail(f"article region text was {art.get_text(' ', strip=True)!r}")
    whole = find_content_region("<html><body><p>only</p></body></html>")
    if "only" not in whole.get_text(" ", strip=True):
        _fail("document fallback lost content")


def test_block_is_a_tree() -> None:
    """Block carries a type, payload and children."""
    b = Block(type="tweet", payload={"handle": "@x", "text": "hi"}, children=[Block(type="image", payload={})])
    if b.type != "tweet" or b.payload["handle"] != "@x" or len(b.children) != 1:
        _fail("Block shape wrong")


def test_is_tweet_detects_substack_embed() -> None:
    """The Substack tweet table is recognised."""
    if not is_tweet(_tweet_tag()):
        _fail("tweet table not detected")


def test_extract_tweet_pulls_handle_and_clean_text() -> None:
    """Handle and body are extracted; engagement/time trailers are stripped."""
    block = extract_tweet(_tweet_tag())
    if block.type != "tweet":
        _fail(f"type {block.type!r}")
    if block.payload.get("handle") != "@tszzl":
        _fail(f"handle {block.payload.get('handle')!r}")
    text = block.payload.get("text", "")
    if "press that magic button" not in text:
        _fail(f"body missing: {text!r}")
    for junk in ("568K Views", "Replies", "Reposts", "Likes", "8:59 PM"):
        if junk in text:
            _fail(f"engagement junk {junk!r} not stripped: {text!r}")


def test_extract_blocks_preserves_tweet_position() -> None:
    """A tweet between two paragraphs yields text, tweet, text in order."""
    html = (
        '<div class="body markup"><p>He seems spooked:</p>'
        + _TWEET_HTML
        + "<p>leading to a conversation.</p></div>"
    )
    blocks = extract_blocks(find_content_region(html))
    kinds = [b.type for b in blocks]
    if kinds != ["text", "tweet", "text"]:
        _fail(f"block kinds were {kinds}")
    if blocks[0].payload["text"] != "He seems spooked:":
        _fail(f"first text {blocks[0].payload['text']!r}")
    if blocks[1].payload["handle"] != "@tszzl":
        _fail(f"tweet handle {blocks[1].payload['handle']!r}")
    if "conversation" not in blocks[2].payload["text"]:
        _fail(f"last text {blocks[2].payload['text']!r}")


def test_serialize_emits_markers_and_sidecar() -> None:
    """Text stays inline; embeds become markers with sidecar payloads."""
    blocks = [
        Block(type="text", payload={"text": "He seems spooked:"}),
        Block(type="tweet", payload={"handle": "@tszzl", "text": "press the button"}),
        Block(type="text", payload={"text": "leading on."}),
    ]
    text, sidecar = serialize_blocks(blocks)
    if "He seems spooked:" not in text or "leading on." not in text:
        _fail(f"narrative text lost: {text!r}")
    if "⟦EMBED:0000⟧" not in text:
        _fail(f"marker missing: {text!r}")
    dumped = json.dumps(sidecar, sort_keys=True)
    for fragment in ('"0000"', '"type": "tweet"', '"handle": "@tszzl"'):
        if fragment not in dumped:
            _fail(f"sidecar missing {fragment!r}: {dumped}")


def test_serialize_recurses_children() -> None:
    """Nested children (tweet-with-image) are serialised recursively."""
    blocks = [
        Block(
            type="tweet",
            payload={"handle": "@x", "text": "t"},
            children=[Block(type="image", payload={"alt": "a chart"})],
        )
    ]
    _, sidecar = serialize_blocks(blocks)
    dumped = json.dumps(sidecar)
    for fragment in ('"type": "image"', '"alt": "a chart"'):
        if fragment not in dumped:
            _fail(f"nested child missing {fragment!r}: {dumped}")


def test_block_from_dict_roundtrips() -> None:
    """Serialize -> block_from_dict rebuilds the tree (incl. nested children)."""
    original = [
        Block(
            type="tweet",
            payload={"handle": "@x", "text": "hi"},
            children=[Block(type="image", payload={"alt": "a chart"})],
        )
    ]
    _, sidecar = serialize_blocks(original)
    rebuilt = block_from_dict(sidecar["0000"])
    if rebuilt.type != "tweet" or rebuilt.payload["handle"] != "@x":
        _fail(f"top block wrong: {rebuilt!r}")
    if len(rebuilt.children) != 1 or rebuilt.children[0].type != "image":
        _fail(f"children wrong: {rebuilt.children!r}")
    if rebuilt.children[0].payload.get("alt") != "a chart":
        _fail(f"child payload wrong: {rebuilt.children[0].payload!r}")


def run_tests() -> None:
    """Run all structural-extractor tests."""
    test_region_prefers_substack_body()
    test_region_falls_back_to_article_then_document()
    test_block_is_a_tree()
    test_is_tweet_detects_substack_embed()
    test_extract_tweet_pulls_handle_and_clean_text()
    test_extract_blocks_preserves_tweet_position()
    test_serialize_emits_markers_and_sidecar()
    test_serialize_recurses_children()
    test_block_from_dict_roundtrips()
    logging.info("all structural-extractor tests passed")


if __name__ == "__main__":
    run_tests()
