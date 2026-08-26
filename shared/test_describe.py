"""Tests for image enrichment (script-style; the vision call is not exercised)."""

import logging
import os

from podcast_shared.describe import describe_image, enrich_images
from podcast_shared.structural_extract import Block

logging.basicConfig(level=logging.INFO)


def _fail(msg: str) -> None:
    """Raise an AssertionError.

    Raises:
        AssertionError: Always.

    """
    raise AssertionError(msg)


def _fake_describer(src: str, alt: str, caption: str) -> str:
    return f"desc<{src}|{alt}|{caption}>"


def test_enrich_sets_description_on_images() -> None:
    """Every image block (incl. nested) gets a description from the describer."""
    blocks = [
        Block(type="text", payload={"text": "hi"}),
        Block(type="image", payload={"alt": "a", "caption": "c", "src": "u1"}),
        Block(
            type="tweet",
            payload={"handle": "@x", "text": "t"},
            children=[Block(type="image", payload={"alt": "", "caption": "", "src": "u2"})],
        ),
    ]
    enrich_images(blocks, _fake_describer)
    if blocks[1].payload.get("description") != "desc<u1|a|c>":
        _fail(f"top image not enriched: {blocks[1].payload}")
    if blocks[2].children[0].payload.get("description") != "desc<u2||>":
        _fail(f"nested image not enriched: {blocks[2].children[0].payload}")


def test_enrich_skips_existing_and_srcless() -> None:
    """Images with a description already, or no src, are left alone."""
    blocks = [
        Block(type="image", payload={"src": "u", "description": "kept"}),
        Block(type="image", payload={"alt": "a"}),  # no src
    ]
    enrich_images(blocks, _fake_describer)
    if blocks[0].payload.get("description") != "kept":
        _fail("existing description overwritten")
    if blocks[1].payload.get("description"):
        _fail("srcless image got a description")


def test_describe_image_no_key_returns_empty() -> None:
    """Without OPENAI_API_KEY, describe_image returns '' (no network)."""
    saved = os.environ.pop("OPENAI_API_KEY", None)
    try:
        if describe_image("http://x/img.png", "alt", "cap"):
            _fail("expected empty description without API key")
    finally:
        if saved is not None:
            os.environ["OPENAI_API_KEY"] = saved


def run_tests() -> None:
    """Run enrichment tests."""
    test_enrich_sets_description_on_images()
    test_enrich_skips_existing_and_srcless()
    test_describe_image_no_key_returns_empty()
    logging.info("describe/enrich tests passed")


if __name__ == "__main__":
    run_tests()
