"""Turn article HTML into an ordered block tree and serialise it for the pipeline.

Pure and offline: no network, no LLM. Later plans add handlers (image/video/...),
the LLM fallback, non-Substack region detection, and the renderer.
"""

from __future__ import annotations

import re
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


def find_content_region(html: str) -> Tag:
    """Return the DOM subtree holding the article body.

    Prefers Substack's ``div.body.markup``, then the first ``<article>``, then the
    whole parsed document. Always returns a Tag (embeds survive — we never route the
    body through trafilatura).

    Args:
        html: The source HTML.

    Returns:
        The content-region tag.

    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("div.body.markup")
    if body is not None:
        return body
    article = soup.find("article")
    if isinstance(article, Tag):
        return article
    return soup


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
    for el in region.find_all((*_TEXT_TAGS, "table")):
        if any(id(ancestor) in consumed for ancestor in el.parents):
            continue
        if is_tweet(el):
            blocks.append(extract_tweet(el))
            consumed.add(id(el))
            previous_text = None
            continue
        if el.name not in _TEXT_TAGS:
            continue
        if el.find(_TEXT_TAGS):  # skip block nested in a block (dedupe)
            continue
        text = " ".join(el.get_text(" ").split())
        if not text or text == previous_text:
            continue
        previous_text = text
        blocks.append(Block(type="text", payload={"text": text}))
    return blocks


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
