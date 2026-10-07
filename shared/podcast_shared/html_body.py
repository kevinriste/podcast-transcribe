"""Turn an intake's article HTML into flat marker body text.

This is the HTML stage of the pipeline. An intake that has an article's HTML (a newsletter
email, an archive post) writes it to ``text-input-raw/`` as the body, with
``META_BODY_FORMAT: html``, instead of extracting text itself. prepare-text then runs every
such file through here: find the article region, walk it into blocks (quotes, tweets,
images, tables, ...), describe the images, and serialize to the flat marker text the rest
of the pipeline reads. Plain-text intakes skip this stage.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from podcast_shared.aside_render import serialize_flat
from podcast_shared.describe import enrich_images
from podcast_shared.structural_extract import Block, extract_blocks, find_content_region_matched

if TYPE_CHECKING:
    from podcast_shared.describe import Describer

# ``META_BODY_FORMAT`` value marking a raw file whose body is HTML for the HTML stage.
BODY_FORMAT_HTML = "html"


@dataclass(slots=True)
class HtmlBody:
    """An article's extracted blocks, and whether a known content region was found."""

    blocks: list[Block]
    structural: bool


def _absolutize_images(blocks: list[Block], base_url: str) -> None:
    """Resolve relative image ``src`` URLs against the page URL (in place, recursive)."""
    for block in blocks:
        src = block.payload.get("src", "")
        if block.type == "image" and src:
            block.payload["src"] = urljoin(base_url, src)
        if block.children:
            _absolutize_images(block.children, base_url)


def parse_html_body(html: str, *, content_selector: str = "", base_url: str = "") -> HtmlBody:
    """Extract an article's blocks from its HTML.

    Args:
        html: The page or email HTML.
        content_selector: CSS selector for the article element. Empty means detect the
            region (Substack, Beehiiv, ``<article>``, else the whole document).
        base_url: The page URL, for resolving relative image URLs.

    Returns:
        The blocks; ``structural`` is False only for the whole-document fallback.

    Raises:
        ValueError: If ``content_selector`` matches nothing (likely not the article page).

    """
    if content_selector:
        region = BeautifulSoup(html, "html.parser").select_one(content_selector)
        if region is None:
            msg = f"content selector {content_selector!r} matched nothing"
            raise ValueError(msg)
        structural = True
    else:
        region, structural = find_content_region_matched(html)
    blocks = extract_blocks(region)
    if base_url:
        _absolutize_images(blocks, base_url)
    return HtmlBody(blocks=blocks, structural=structural)


def render_html_body(body: HtmlBody, describer: Describer | None) -> str:
    """Describe the body's images (unless ``EMBED_VISION=0``) and serialize it.

    Image descriptions are layered on caption/alt; with no ``describer`` (or vision off)
    image asides use caption/alt alone. Embed types listed in ``EMBED_DROP_TYPES`` are left
    out. A ``VisionUnavailableError`` from ``describer`` propagates so the caller can defer.

    Returns:
        The flat marker body text (blocks separated by blank lines).

    """
    if describer is not None and os.environ.get("EMBED_VISION", "1") != "0":
        enrich_images(body.blocks, describer)
    drop_types = frozenset(t.strip().lower() for t in os.environ.get("EMBED_DROP_TYPES", "").split(",") if t.strip())
    return serialize_flat(body.blocks, drop_types=drop_types)
