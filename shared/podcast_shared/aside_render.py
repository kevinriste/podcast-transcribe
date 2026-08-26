"""Render an embed block tree into spoken 'aside' text.

Pure and voice-agnostic: produces the words a meta-narrator would say. How those
words are voiced (a distinct aside voice vs. the single narration voice) is decided
by the renderer in text-to-speech (a later plan).
"""

from __future__ import annotations

import re

from podcast_shared.structural_extract import (
    EMBED_MARKER_PREFIX,
    EMBED_MARKER_SUFFIX,
    Block,
    block_from_dict,
)

_MARKER_RE = re.compile(re.escape(EMBED_MARKER_PREFIX) + r"(\d+)" + re.escape(EMBED_MARKER_SUFFIX))


def _render_own(block: Block) -> str:
    if block.type == "tweet":
        text = block.payload.get("text", "")
        handle = block.payload.get("handle", "")
        if handle:
            return f"The author shares a tweet from {handle}: {text}."
        return f"The author shares a tweet: {text}."
    if block.type == "image":
        desc = block.payload.get("description") or block.payload.get("caption") or block.payload.get("alt") or ""
        return f"Image: {desc}." if desc else "The author includes an image."
    return "The author includes embedded content."


def render_block_aside(block: Block) -> str:
    """Render a block (and its children) into one spoken aside string.

    Returns:
        The aside text; child asides are appended after the parent's own text.

    """
    parts = [_render_own(block)]
    parts.extend(render_block_aside(child) for child in block.children)
    return " ".join(part for part in parts if part)


def resolve_markers(text: str, sidecar: dict[str, dict[str, object]]) -> str:
    """Replace ``⟦EMBED:id⟧`` markers with rendered asides.

    Args:
        text: Serialized narrative text containing embed markers.
        sidecar: Marker id -> serialized block payload.

    Returns:
        Speech-ready text with markers resolved (unknown ids removed).

    """

    def replace(match: re.Match[str]) -> str:
        entry = sidecar.get(match.group(1))
        if entry is None:
            return ""
        return render_block_aside(block_from_dict(entry))

    return _MARKER_RE.sub(replace, text)
