"""Vision description of content images, layered on top of alt/caption.

The network lives here (OpenAI Responses API vision) and is injected into
``enrich_images`` so the extractor stays pure. Degrades to "" whenever vision is
unavailable, so intake never breaks on a failed or disabled describe.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING

from openai import OpenAI, OpenAIError

if TYPE_CHECKING:
    from podcast_shared.structural_extract import Block

Describer = Callable[[str, str, str], str]

VISION_MODEL = os.environ.get("EMBED_VISION_MODEL", "gpt-5.6")

_PROMPT = (
    "Describe this image for a podcast listener in one concise sentence. State what it "
    "shows; do not start with 'The image' or 'This image'. If it is a chart, give the "
    "headline finding."
)


def describe_image(src: str, alt: str = "", caption: str = "") -> str:
    """Return a one-sentence vision description of an image URL, or '' on any failure.

    Returns:
        The description, or "" when src is empty, OPENAI_API_KEY is unset, or the call fails.

    """
    if not src:
        return ""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return ""
    hint = f" Caption: {caption}." if caption else f" Alt text: {alt}." if alt else ""
    try:
        client = OpenAI(api_key=key, max_retries=3)
        content = [
            {"type": "input_text", "text": _PROMPT + hint},
            {"type": "input_image", "image_url": src},
        ]
        response = client.responses.create(
            model=VISION_MODEL,
            input=[{"role": "user", "content": content}],  # pyright: ignore[reportArgumentType]  (SDK union boundary)
            timeout=120,
            prompt_cache_options={"mode": "explicit"},
        )
    except OpenAIError:
        logging.exception("Vision description failed for %s", src)
        return ""
    return response.output_text.strip()


def enrich_images(blocks: list[Block], describer: Describer) -> None:
    """Fill each image block's ``description`` payload via ``describer`` (in place, recursive)."""
    for block in blocks:
        if block.type == "image" and block.payload.get("src") and not block.payload.get("description"):
            block.payload["description"] = describer(
                block.payload.get("src", ""),
                block.payload.get("alt", ""),
                block.payload.get("caption", ""),
            )
        if block.children:
            enrich_images(block.children, describer)
