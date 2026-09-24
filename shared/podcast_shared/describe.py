"""Vision description of content images, layered on top of alt/caption.

The network lives here (OpenAI Responses API vision) and is injected into
``enrich_images`` so the extractor stays pure. Returns "" when vision is disabled (no key).
Failures are classified so intake can react proportionately:

- ``VisionRejectedError``: the API refused this particular image (bad format, etc.). It will
  never succeed; intake uses the caption/alt and reports it.
- ``VisionUnavailableError`` with ``outage=False``: this image's URL couldn't be fetched by
  the API after every attempt (e.g. hotlink protection). Only this email is affected.
- ``VisionUnavailableError`` with ``outage=True``: vision itself is down or misconfigured
  (auth, unknown model, unsupported parameter, connection/5xx after every attempt). Other
  emails would fail the same way, so intake stops calling vision for the rest of the run.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from openai import (
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    OpenAI,
    OpenAIError,
    PermissionDeniedError,
    UnprocessableEntityError,
)

if TYPE_CHECKING:
    from podcast_shared.structural_extract import Block

Describer = Callable[[str, str, str], str]

VISION_MODEL = os.environ.get("EMBED_VISION_MODEL", "gpt-6-luna")

# Sentinel the model returns for platform chrome; aside_render drops such images.
DECORATIVE_SENTINEL = "DECORATIVE"

_PROMPT = (
    "Describe this image for a podcast listener in one concise sentence. State what it "
    "shows; do not start with 'The image' or 'This image'. If it is a chart, give the "
    "headline finding. If the image is only page chrome with nothing for a listener (a "
    "logo, icon, divider or rule, spacer, button, app-store or subscribe banner, or ad "
    f"badge), reply with exactly {DECORATIVE_SENTINEL} and nothing else."
)


# Attempts per image before giving up for this run, and the pause before each retry.
# Each attempt also gets one SDK-level retry (connection errors / 429 / 5xx) and a 60 s
# timeout. The attempts also cover the 400 "Unable to download content from the provided
# URL", which is sometimes transient.
_VISION_ATTEMPTS = 3
_VISION_RETRY_DELAYS = (5.0, 20.0)
_VISION_TIMEOUT = 60.0
# Errors no retry can fix: the key or model is wrong. Defer at once and let the alert say so.
_CONFIG_ERRORS = (AuthenticationError, PermissionDeniedError, NotFoundError)
# The API's wording for an image URL it couldn't fetch (a bare "download" could be in a URL).
_DOWNLOAD_FAILURE_RE = re.compile(r"(?:unable to|error while|failed to) download", re.IGNORECASE)


class VisionUnavailableError(RuntimeError):
    """No description could be obtained; the caller should retry the item later.

    ``outage`` is True when vision as a whole is failing (so other images would fail too),
    False when only this image's URL is the problem.
    """

    def __init__(self, message: str, *, outage: bool = True) -> None:
        """Record whether this is a service-wide outage."""
        super().__init__(message)
        self.outage: bool = outage


class VisionRejectedError(RuntimeError):
    """The API refused this particular image; retrying will not help."""


def _error_field(exc: OpenAIError, name: str) -> str:
    value: object = getattr(exc, name, None)
    return value.lower() if isinstance(value, str) else ""


def _is_download_failure(exc: OpenAIError) -> bool:
    """Whether the API couldn't fetch the image URL (per-image, and sometimes transient).

    Returns:
        True for the 400 "Unable to download content from the provided URL".

    """
    if not isinstance(exc, BadRequestError):
        return False
    return "download" in _error_field(exc, "code") or _DOWNLOAD_FAILURE_RE.search(str(exc)) is not None


def _is_image_rejection(exc: OpenAIError) -> bool:
    """Whether the API refused this particular image, as opposed to the request itself.

    Only errors whose code or param names the image count; a 400 about the model or another
    parameter is a configuration problem that would fail for every image.

    Returns:
        True for a 400/422 attributed to the image input.

    """
    if not isinstance(exc, (BadRequestError, UnprocessableEntityError)):
        return False
    return "image" in _error_field(exc, "code") or "image" in _error_field(exc, "param")


def describe_image(src: str, alt: str = "", caption: str = "") -> str:
    """Return a one-sentence vision description of an image URL.

    Returns:
        The description, or "" when src is empty or OPENAI_API_KEY is unset (vision off).

    Raises:
        VisionRejectedError: When the API refuses this image outright.
        VisionUnavailableError: When every attempt fails with an error that may clear up, or
            at once on a configuration error; ``outage`` says whether other images are
            likely to fail too.

    """
    if not src:
        return ""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return ""
    hint = f" Caption: {caption}." if caption else f" Alt text: {alt}." if alt else ""
    client = OpenAI(api_key=key, max_retries=1)
    content = [
        {"type": "input_text", "text": _PROMPT + hint},
        {"type": "input_image", "image_url": src},
    ]
    last_error: OpenAIError | None = None
    outage = False
    for attempt in range(1, _VISION_ATTEMPTS + 1):
        try:
            response = client.responses.create(
                model=VISION_MODEL,
                input=[{"role": "user", "content": content}],  # pyright: ignore[reportArgumentType]  (SDK union boundary)
                timeout=_VISION_TIMEOUT,
                prompt_cache_options={"mode": "explicit"},
            )
        except _CONFIG_ERRORS as exc:
            msg = f"Vision is misconfigured ({type(exc).__name__}; check OPENAI_API_KEY / EMBED_VISION_MODEL): {exc}"
            raise VisionUnavailableError(msg) from exc
        except OpenAIError as exc:
            if not _is_download_failure(exc):
                if _is_image_rejection(exc):
                    msg = f"Vision rejected {src}: {exc}"
                    raise VisionRejectedError(msg) from exc
                if isinstance(exc, (BadRequestError, UnprocessableEntityError)):
                    msg = f"Vision request is invalid (check EMBED_VISION_MODEL and request parameters): {exc}"
                    raise VisionUnavailableError(msg) from exc
                outage = True
            last_error = exc
            logging.warning("Vision attempt %d/%d failed for %s: %s", attempt, _VISION_ATTEMPTS, src, exc)
            if attempt < _VISION_ATTEMPTS:
                time.sleep(_VISION_RETRY_DELAYS[attempt - 1])
            continue
        return response.output_text.strip()
    msg = f"Vision description failed after {_VISION_ATTEMPTS} attempts for {src}"
    raise VisionUnavailableError(msg, outage=outage) from last_error


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
