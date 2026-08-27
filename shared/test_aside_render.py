"""Tests for aside rendering (script-style, repo house style)."""

import logging

from podcast_shared.aside_render import render_block_aside, resolve_markers, serialize_flat
from podcast_shared.structural_extract import ASIDE_MARKER, BLOCKQUOTE_MARKER, Block, serialize_blocks

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


def test_render_quote_is_verbatim() -> None:
    """A quote renders as its own text, not an announced aside."""
    out = render_block_aside(Block(type="quote", payload={"text": "To be or not to be."}))
    if out != "To be or not to be.":
        _fail(f"quote aside was {out!r}")


def test_render_image_from_caption() -> None:
    """An image with a caption renders as 'Image: <caption>'."""
    out = render_block_aside(Block(type="image", payload={"alt": "", "caption": "Fig 1: growth", "src": "/c.png"}))
    if out != "Image: Fig 1: growth.":
        _fail(f"image aside was {out!r}")


def test_serialize_flat_marks_quotes_and_asides() -> None:
    """Text stays plain; quotes get BLOCKQUOTE_MARKER; embeds get ASIDE_MARKER."""
    body = serialize_flat(
        [
            Block(type="text", payload={"text": "Intro."}),
            Block(type="quote", payload={"text": "A quoted line."}),
            Block(type="tweet", payload={"handle": "@x", "text": "hello"}),
        ]
    )
    lines = [ln for ln in body.split("\n\n") if ln]
    if lines[0] != "Intro.":
        _fail(f"text line wrong: {lines!r}")
    if lines[1] != f"{BLOCKQUOTE_MARKER}A quoted line.":
        _fail(f"quote line wrong: {lines!r}")
    if lines[2] != f"{ASIDE_MARKER}The author shares a tweet from @x: hello.":
        _fail(f"aside line wrong: {lines!r}")


def test_image_no_double_period() -> None:
    """A vision description ending in a period does not produce 'set..'."""
    out = render_block_aside(Block(type="image", payload={"description": "A dog on a film set."}))
    if out != "Image: A dog on a film set.":
        _fail(f"double-period not fixed: {out!r}")
    out2 = render_block_aside(Block(type="image", payload={"caption": "No terminal punctuation"}))
    if out2 != "Image: No terminal punctuation.":
        _fail(f"period not added: {out2!r}")


def test_render_video_audio_code() -> None:
    """Video/audio/code render as concise announced asides."""
    v = render_block_aside(Block(type="video", payload={"title": "My Talk", "src": "u"}))
    if v != "The author shares a video titled 'My Talk'.":
        _fail(f"video aside was {v!r}")
    v2 = render_block_aside(Block(type="video", payload={"title": "", "src": "u"}))
    if v2 != "The author shares a video.":
        _fail(f"titleless video aside was {v2!r}")
    a = render_block_aside(Block(type="audio", payload={"title": "Ep 12", "src": "u"}))
    if a != "The author shares an audio clip titled 'Ep 12'.":
        _fail(f"audio aside was {a!r}")
    c = render_block_aside(Block(type="code", payload={"text": "print(1)"}))
    if c != "The author includes a code block.":
        _fail(f"code aside was {c!r}")


def test_render_footnote_and_card() -> None:
    """Footnotes read as numbered notes; cards as a one-line link announcement."""
    fn = render_block_aside(Block(type="footnote", payload={"number": "1", "text": "Counting headwords."}))
    if fn != "Footnote 1: Counting headwords.":
        _fail(f"footnote aside was {fn!r}")
    fn0 = render_block_aside(Block(type="footnote", payload={"number": "", "text": "A note"}))
    if fn0 != "Footnote: A note":
        _fail(f"numberless footnote was {fn0!r}")
    card = render_block_aside(Block(type="card", payload={"title": "On Beliefs", "publication": "NNN", "href": "u"}))
    if card != "The author links to a post titled 'On Beliefs' from NNN.":
        _fail(f"card aside was {card!r}")
    card0 = render_block_aside(Block(type="card", payload={"title": "On Beliefs", "publication": "", "href": "u"}))
    if card0 != "The author links to a post titled 'On Beliefs'.":
        _fail(f"publicationless card was {card0!r}")


def test_serialize_flat_drop_types() -> None:
    """drop_types suppresses chosen block types entirely."""
    blocks = [
        Block(type="text", payload={"text": "Body."}),
        Block(type="card", payload={"title": "T", "publication": "P", "href": "u"}),
        Block(type="footnote", payload={"number": "1", "text": "Note."}),
    ]
    out = serialize_flat(blocks, drop_types=frozenset({"card"}))
    if "titled 'T'" in out:
        _fail(f"card should be dropped: {out!r}")
    if "Footnote 1" not in out or "Body." not in out:
        _fail(f"non-dropped content missing: {out!r}")


def run_tests() -> None:
    """Run all aside-render tests."""
    test_render_tweet()
    test_render_tweet_without_handle()
    test_render_tweet_with_image_child()
    test_render_generic_fallback()
    test_resolve_markers_replaces_embed()
    test_resolve_markers_drops_unknown_id()
    test_render_quote_is_verbatim()
    test_render_image_from_caption()
    test_image_no_double_period()
    test_render_video_audio_code()
    test_render_footnote_and_card()
    test_serialize_flat_drop_types()
    test_serialize_flat_marks_quotes_and_asides()
    logging.info("aside render tests passed")


if __name__ == "__main__":
    run_tests()
