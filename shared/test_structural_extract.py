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
    extract_iframe,
    extract_tweet,
    find_content_region,
    iframe_kind,
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


def test_extract_blocks_marks_quotes() -> None:
    """A blockquote paragraph becomes a quote block; normal paragraphs stay text."""
    html = (
        '<div class="body markup">'
        "<p>Normal.</p>"
        "<blockquote><p>Quoted line.</p></blockquote>"
        "<p>After.</p>"
        "</div>"
    )
    blocks = extract_blocks(find_content_region(html))
    kinds = [(b.type, b.payload.get("text", "")) for b in blocks]
    if ("quote", "Quoted line.") not in kinds:
        _fail(f"quote not marked: {kinds}")
    if ("text", "Normal.") not in kinds:
        _fail(f"normal text mismarked: {kinds}")


def test_extract_image_from_figure() -> None:
    """A figure yields an image block with alt + caption."""
    html = (
        '<div class="body markup">'
        '<figure><img src="/c.png" alt="a chart"><figcaption>Fig 1: growth</figcaption></figure>'
        "</div>"
    )
    blocks = extract_blocks(find_content_region(html))
    imgs = [b for b in blocks if b.type == "image"]
    if len(imgs) != 1:
        _fail(f"expected 1 image, got {[b.type for b in blocks]}")
    if imgs[0].payload.get("alt") != "a chart" or imgs[0].payload.get("caption") != "Fig 1: growth":
        _fail(f"image payload wrong: {imgs[0].payload}")


def test_bare_image_and_decorative_filter() -> None:
    """A content img is kept; a decorative (empty-alt icon) img is skipped."""
    html = (
        '<div class="body markup">'
        '<p>x</p><img src="/photo.jpg" alt="a landscape">'
        '<img src="/spacer.gif" alt="" class="icon">'
        "</div>"
    )
    blocks = extract_blocks(find_content_region(html))
    imgs = [b for b in blocks if b.type == "image"]
    if len(imgs) != 1 or imgs[0].payload.get("alt") != "a landscape":
        _fail(f"decorative filter wrong: {[b.payload for b in imgs]}")


def test_iframe_kind_classifies_hosts() -> None:
    """Hosts map to video/audio; unknown hosts are ''."""
    cases = {
        "https://www.youtube.com/embed/abc": "video",
        "https://player.vimeo.com/video/1": "video",
        "https://open.spotify.com/embed/x": "audio",
        "https://w.soundcloud.com/player/?url=y": "audio",
        "https://ads.example.com/widget": "",
    }
    for src, kind in cases.items():
        if iframe_kind(src) != kind:
            _fail(f"iframe_kind({src!r}) = {iframe_kind(src)!r}, expected {kind!r}")


def test_extract_iframe_builds_block() -> None:
    """A YouTube iframe with a title yields a video block; ads yield None."""
    yt = BeautifulSoup('<iframe src="https://youtube.com/embed/x" title="My Talk"></iframe>', "html.parser").find(
        "iframe"
    )
    if not isinstance(yt, Tag):
        _fail("no iframe")
    block = extract_iframe(yt)
    if block is None or block.type != "video" or block.payload.get("title") != "My Talk":
        _fail(f"video block wrong: {block!r}")
    ad = BeautifulSoup('<iframe src="https://ads.x/w"></iframe>', "html.parser").find("iframe")
    if isinstance(ad, Tag) and extract_iframe(ad) is not None:
        _fail("ad iframe should be None")


def test_extract_blocks_video_and_code() -> None:
    """An iframe becomes a video block and a <pre> becomes a code block, in order."""
    html = (
        '<div class="body markup">'
        "<p>Watch this.</p>"
        '<iframe src="https://youtube.com/embed/x" title="Talk"></iframe>'
        "<pre>print(42)</pre>"
        '<iframe src="https://ads.x/w"></iframe>'
        "</div>"
    )
    blocks = extract_blocks(find_content_region(html))
    kinds = [b.type for b in blocks]
    if kinds != ["text", "video", "code"]:
        _fail(f"kinds were {kinds}")
    if blocks[1].payload.get("title") != "Talk":
        _fail(f"video title wrong: {blocks[1].payload}")
    if blocks[2].payload.get("text") != "print(42)":
        _fail(f"code text wrong: {blocks[2].payload}")


def test_extract_footnote_and_card() -> None:
    """A footnote div and an embedded-post card become footnote/card blocks; order preserved."""
    html = (
        '<div class="body markup">'
        "<p>Body text.</p>"
        '<div class="embedded-post-wrap"><div class="embedded-post">'
        '<div class="embedded-post-header"><span class="embedded-post-publication-name">The NNN Newsletter</span></div>'
        '<div class="embedded-post-title-wrapper"><a class="embedded-post-title" href="https://ex.com/p">'
        "Can You Control Your Beliefs?</a></div>"
        '<a class="embedded-post-cta">Read more</a>'
        '<div class="embedded-post-meta">15 days ago · 15 likes · Turi Munthe</div>'
        "</div></div>"
        '<div class="footnote"><span class="footnote-number">1</span>'
        '<div class="footnote-content">Counting headwords.</div></div>'
        "</div>"
    )
    blocks = extract_blocks(find_content_region(html))
    kinds = [b.type for b in blocks]
    if kinds != ["text", "card", "footnote"]:
        _fail(f"kinds were {kinds}")
    card = blocks[1]
    if card.payload.get("title") != "Can You Control Your Beliefs?":
        _fail(f"card title wrong: {card.payload}")
    if card.payload.get("publication") != "The NNN Newsletter":
        _fail(f"card publication wrong: {card.payload}")
    if card.payload.get("href") != "https://ex.com/p":
        _fail(f"card href wrong: {card.payload}")
    fn = blocks[2]
    if fn.payload.get("number") != "1" or fn.payload.get("text") != "Counting headwords.":
        _fail(f"footnote payload wrong: {fn.payload}")


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
    test_extract_blocks_marks_quotes()
    test_extract_image_from_figure()
    test_bare_image_and_decorative_filter()
    test_iframe_kind_classifies_hosts()
    test_extract_iframe_builds_block()
    test_extract_blocks_video_and_code()
    test_extract_footnote_and_card()
    logging.info("all structural-extractor tests passed")


if __name__ == "__main__":
    run_tests()
