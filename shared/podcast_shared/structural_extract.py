"""Turn article HTML into an ordered block tree and serialise it for the pipeline.

Pure and offline: no network, no LLM. Later plans add handlers (image/video/...),
the LLM fallback, non-Substack region detection, and the renderer.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from bs4.element import Tag


@dataclass(slots=True)
class Block:
    """One node in the extracted content tree.

    ``type`` is a handler key (``text``/``tweet``/``image``/...); ``payload`` holds
    the type's spoken fields; ``children`` holds nested embeds (e.g. a tweet's image).
    """

    type: str
    payload: dict[str, str]
    children: list[Block] = field(default_factory=list)


def find_content_region_matched(html: str) -> tuple[Tag, bool]:
    """Return the article-body subtree and whether a known container was matched.

    Prefers Substack's ``div.body.markup``, then Beehiiv's ``#content-blocks`` (both
    exclude the email masthead/footer/social chrome by DOM position), then the first
    ``<article>``. Falls back to the whole parsed document — signalled by ``False``, so
    callers can decline to treat that (chrome-laden) extraction as structurally clean.

    Args:
        html: The source HTML.

    Returns:
        ``(region, matched)`` — the content-region tag, and True when a recognized
        container was found (False for the whole-document fallback).

    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("div.body.markup")
    if body is not None:
        return body, True
    beehiiv = soup.select_one("#content-blocks")
    if beehiiv is not None:
        return beehiiv, True
    article = soup.find("article")
    if isinstance(article, Tag):
        return article, True
    return soup, False


def find_content_region(html: str) -> Tag:
    """Return the DOM subtree holding the article body (see :func:`find_content_region_matched`).

    Returns:
        The content-region tag (the whole document when no known container is found).

    """
    return find_content_region_matched(html)[0]


_HANDLE_RE = re.compile(r"@\w+")
# Trailing engagement/time line Substack renders under a tweet, e.g.
# "8:59 PM · Jul 25, 2026 · 568K Views 354 Replies · 249 Reposts · 3.51K Likes".
_ENGAGEMENT_RE = re.compile(
    r"\s*\d{1,2}:\d{2}\s*(?:AM|PM)\b.*$|\s*[\d.,KMB]+\s*(?:Views|Replies|Reposts|Likes|Quotes|Bookmarks)\b",
    re.IGNORECASE,
)


def _classes(el: Tag) -> str:
    value = el.get("class")
    return " ".join(value).lower() if isinstance(value, list) else str(value or "").lower()


def is_tweet(el: Tag) -> bool:
    """Whether ``el`` is a tweet/X embed.

    Returns:
        True when the element is a Substack/generic tweet embed.

    """
    dcn = str(el.get("data-component-name") or "")
    if dcn.startswith("Tweet"):
        return True
    cls = _classes(el)
    return "tweet" in cls or "twitter" in cls


def extract_tweet(el: Tag) -> Block:
    """Extract a tweet embed into a Block, stripping engagement/time trailers.

    Returns:
        A ``tweet`` Block with ``handle`` and cleaned ``text`` payload fields.

    """
    pieces = [seg for seg in (s.strip() for s in el.stripped_strings) if seg]
    joined = " ".join(pieces)
    handle_match = _HANDLE_RE.search(joined)
    handle = handle_match.group(0) if handle_match else ""
    # Drop the author name + handle prefix, then everything from the engagement line on.
    body = joined
    if handle:
        body = body.split(handle, 1)[1].strip()
    body = _ENGAGEMENT_RE.sub("", body).strip()
    return Block(type="tweet", payload={"handle": handle, "text": body})


_DECORATIVE_CLASS_RE = re.compile(r"\b(?:avatar|icon|logo|badge|emoji)\b")
# Images narrower than this (explicit px width) are platform icons/like-buttons/dividers
# (e.g. Beehiiv's 68-75px social glyphs), not article content — skip them.
_MIN_CONTENT_IMG_WIDTH = 100


def _is_decorative(el: Tag) -> bool:
    """Whether an ``<img>`` is chrome (skip it).

    Returns:
        True for empty-alt icon/avatar/logo/badge/emoji or ``data:`` images.

    """
    alt = str(el.get("alt") or "").strip()
    if alt:
        return False
    width = str(el.get("width") or "")
    if width.isdigit() and int(width) < _MIN_CONTENT_IMG_WIDTH:  # icon/logo/emoji, not content
        return True
    src = str(el.get("src") or "")
    return bool(_DECORATIVE_CLASS_RE.search(_classes(el))) or src.startswith("data:")


def extract_image(el: Tag) -> Block:
    """Extract an image/figure into an ``image`` Block.

    Returns:
        An ``image`` Block with ``alt``/``caption``/``src`` payload fields.

    """
    img = el if el.name == "img" else el.find("img")
    alt = ""
    src = ""
    if isinstance(img, Tag):
        alt = str(img.get("alt") or "").strip()
        src = str(img.get("src") or "").strip()
    caption = ""
    figcaption = el.find("figcaption")
    if isinstance(figcaption, Tag):
        caption = " ".join(figcaption.get_text(" ").split())
    return Block(type="image", payload={"alt": alt, "caption": caption, "src": src})


_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "loom.com", "wistia.")
_AUDIO_HOSTS = ("spotify.com", "soundcloud.com", "transistor.fm")


def iframe_kind(src: str) -> str:
    """Classify an iframe ``src`` into ``"video"``/``"audio"``/``""``.

    Returns:
        The embed kind, or "" for an unrecognized host.

    """
    low = src.lower()
    if any(host in low for host in _VIDEO_HOSTS):
        return "video"
    if any(host in low for host in _AUDIO_HOSTS) or "/podcast" in low or ("apple.com" in low and "podcast" in low):
        return "audio"
    return ""


def extract_iframe(el: Tag) -> Block | None:
    """Extract a video/audio iframe embed into a Block, or None for unknown hosts.

    Returns:
        A ``video``/``audio`` Block, or None.

    """
    src = str(el.get("src") or "")
    kind = iframe_kind(src)
    if not kind:
        return None
    title = str(el.get("title") or "").strip()
    return Block(type=kind, payload={"title": title, "src": src})


_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", string.digits)


def _footnote_refs(el: Tag) -> list[Tag]:
    """Find in-text footnote reference elements (``span.footnote-anchor-email``, ...).

    Returns:
        The reference elements in document order.

    """
    return list(el.find_all(class_=re.compile(r"footnote-anchor")))


def _collect_footnotes(region: Tag) -> tuple[dict[str, Block], list[str]]:
    """Collect footnote definition divs into a number→Block map and remove them from ``region``.

    Definitions live at the end of the body; pulling them out here lets the walk inline
    each footnote right after the paragraph whose reference marker cites it.

    Returns:
        ``(defs, order)`` — the number→Block map and the definition order (for orphans).

    """
    defs: dict[str, Block] = {}
    order: list[str] = []
    def_divs = [d for d in region.find_all("div") if "footnote" in _classes(d) and "footnote-content" not in _classes(d)]
    for d in def_divs:
        block = extract_footnote(d)
        if block is not None:
            number = str(block.payload.get("number", "")) or str(len(order) + 1)
            defs[number] = block
            order.append(number)
    for d in def_divs:
        d.decompose()
    return defs, order


def _resolve_footnote(
    number: str, defs: dict[str, Block], order: list[str], referenced: set[str]
) -> Block | None:
    """Resolve a reference number to its footnote Block, once.

    Prefers an exact number match; falls back to the next unreferenced definition in
    document order (covering references whose glyph did not parse to a known number).

    Returns:
        The footnote Block to inline, or None when already used or unresolvable.

    """
    if number in defs and number not in referenced:
        referenced.add(number)
        return defs[number]
    for candidate in order:
        if candidate not in referenced:
            referenced.add(candidate)
            return defs[candidate]
    return None


def extract_footnote(el: Tag) -> Block | None:
    """Extract a Substack footnote definition div into a footnote Block.

    Returns:
        A ``footnote`` Block, or None when it has no text.

    """
    number_el = el.find(class_="footnote-number")
    number = number_el.get_text(" ").strip() if isinstance(number_el, Tag) else ""
    content_el = el.find(class_="footnote-content")
    if isinstance(content_el, Tag):
        text = " ".join(content_el.get_text(" ").split())
    else:
        text = " ".join(el.get_text(" ").split())
        if number and text.startswith(number):
            text = text[len(number) :].strip()
    if not text:
        return None
    return Block(type="footnote", payload={"number": number, "text": text})


def extract_card(el: Tag) -> Block | None:
    """Extract a Substack embedded-post card into a card Block.

    Returns:
        A ``card`` Block, or None when it has no title.

    """
    title_el = el.find("a", class_="embedded-post-title")
    title = title_el.get_text(" ").strip() if isinstance(title_el, Tag) else ""
    if not title:
        return None
    href = str(title_el.get("href") or "") if isinstance(title_el, Tag) else ""
    pub_el = el.find(class_="embedded-post-publication-name")
    publication = pub_el.get_text(" ").strip() if isinstance(pub_el, Tag) else ""
    return Block(type="card", payload={"title": title, "publication": publication, "href": href})


_TEXT_TAGS = ("p", "li", "blockquote", "h1", "h2", "h3", "h4")


def extract_blocks(region: Tag) -> list[Block]:
    """Walk ``region`` in document order into text and tweet Blocks.

    Args:
        region: The content-region tag from :func:`find_content_region`.

    Returns:
        Ordered blocks; a tweet lands between its surrounding paragraphs.

    """
    blocks: list[Block] = []
    previous_text: str | None = None
    consumed: set[int] = set()  # id() of elements already emitted as a tweet
    footnote_defs, footnote_order = _collect_footnotes(region)  # removes def divs from region
    referenced: set[str] = set()
    for el in region.find_all((*_TEXT_TAGS, "table", "figure", "img", "iframe", "pre", "div")):
        if any(id(ancestor) in consumed for ancestor in el.parents):
            continue
        if is_tweet(el):
            blocks.append(extract_tweet(el))
            consumed.add(id(el))
            previous_text = None
            continue
        if el.name == "figure":
            blocks.append(extract_image(el))
            consumed.add(id(el))  # skip the figure's inner <img> via the ancestor check
            previous_text = None
            continue
        if el.name == "img":
            if _is_decorative(el):
                continue
            blocks.append(extract_image(el))
            previous_text = None
            continue
        if el.name == "iframe":
            embed = extract_iframe(el)
            if embed is not None:
                blocks.append(embed)
                previous_text = None
            continue
        if el.name == "pre":
            code_text = el.get_text("\n").strip()
            if code_text:
                blocks.append(Block(type="code", payload={"text": code_text}))
                previous_text = None
            continue
        if el.name == "div":
            classes = _classes(el)
            if "embedded-post-wrap" in classes or "embedded-post" in classes:
                card = extract_card(el)
                if card is not None:
                    blocks.append(card)
                    consumed.add(id(el))
                    previous_text = None
                continue
            continue
        if el.name not in _TEXT_TAGS:
            continue
        if el.find(_TEXT_TAGS):  # skip block nested in a block (dedupe)
            continue
        ref_numbers = [r.get_text().translate(_SUPERSCRIPT).strip() for r in _footnote_refs(el)]
        for r in _footnote_refs(el):
            r.decompose()  # drop the superscript glyph so it is not read aloud
        text = " ".join(el.get_text(" ").split())
        if not text or text == previous_text:
            continue
        previous_text = text
        is_quote = el.name == "blockquote" or el.find_parent("blockquote") is not None
        blocks.append(Block(type="quote" if is_quote else "text", payload={"text": text}))
        for number in ref_numbers:  # inline each cited footnote right after its paragraph
            footnote = _resolve_footnote(number, footnote_defs, footnote_order, referenced)
            if footnote is not None:
                blocks.append(footnote)
    for number in footnote_order:  # any footnotes never cited in text, appended in order
        if number not in referenced:
            blocks.append(footnote_defs[number])
            referenced.add(number)
    return blocks


# Line prefix intake writes ahead of each block-quotation line so downstream stages
# can tell quoted passages from the author's own words. Chosen to survive prepare-text
# cleaning (no brackets/underscores/collapsible whitespace) and to never occur in prose;
# stripped before any text reaches the synthesizer. Defined here (a leaf module) so
# aside_render and structural code can import it without a circular dependency; the
# public name is re-exported from podcast_shared.__init__.
BLOCKQUOTE_MARKER = chr(0x276F) + " "  # U+276F HEAVY RIGHT-POINTING ANGLE QUOTATION MARK ORNAMENT, then a space
# Line prefix intake writes ahead of each rendered embed "aside" (tweet/image/...), so
# text-to-speech can voice it in the meta-narrator aside voice. Same design rules as
# BLOCKQUOTE_MARKER (survives cleaning, never in prose).
ASIDE_MARKER = chr(0x2756) + " "  # U+2756 BLACK DIAMOND MINUS WHITE X, then a space

EMBED_MARKER_PREFIX = "⟦EMBED:"
EMBED_MARKER_SUFFIX = "⟧"


def _block_to_dict(block: Block) -> dict[str, object]:
    return {
        "type": block.type,
        "payload": dict(block.payload),
        "children": [_block_to_dict(child) for child in block.children],
    }


def block_from_dict(data: dict[str, object]) -> Block:
    """Rebuild a Block from a sidecar entry (inverse of ``_block_to_dict``).

    ``data`` is a deserialization boundary (a JSON-like mapping); narrowed
    defensively so a malformed entry degrades to an empty payload/children rather
    than raising. The two ``pyright: ignore`` comments cover the unavoidable
    ``Unknown`` that ``isinstance(x, dict/list)`` yields on ``object`` values — the
    same boundary the repo already suppresses for ``json.loads``.

    Returns:
        The reconstructed Block.

    """
    block_type = str(data.get("type", ""))
    payload: dict[str, str] = {}
    payload_raw = data.get("payload")
    if isinstance(payload_raw, dict):
        payload = {str(k): str(v) for k, v in payload_raw.items()}  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    children: list[Block] = []
    children_raw = data.get("children")
    if isinstance(children_raw, list):
        children = [block_from_dict(c) for c in children_raw if isinstance(c, dict)]  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    return Block(type=block_type, payload=payload, children=children)


def serialize_blocks(blocks: list[Block]) -> tuple[str, dict[str, dict[str, object]]]:
    """Serialise blocks to marker-annotated text plus a sidecar payload map.

    Args:
        blocks: The ordered block list.

    Returns:
        ``(text, sidecar)`` where text keeps narrative inline and replaces each embed
        with a ``⟦EMBED:NNNN⟧`` marker resolved from ``sidecar``.

    """
    lines: list[str] = []
    sidecar: dict[str, dict[str, object]] = {}
    counter = 0
    for block in blocks:
        if block.type == "text":
            lines.append(block.payload.get("text", ""))
            continue
        marker_id = f"{counter:04d}"
        counter += 1
        lines.append(f"{EMBED_MARKER_PREFIX}{marker_id}{EMBED_MARKER_SUFFIX}")
        sidecar[marker_id] = _block_to_dict(block)
    return "\n\n".join(lines), sidecar
