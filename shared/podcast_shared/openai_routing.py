"""OpenAI key and service-tier routing shared by the pipeline's OpenAI callers.

gpt-6-luna is in OpenAI's 1M free-token group with Astra/Sol but ~100x cheaper, so it
runs under a separate project with data sharing OFF (OPENAI_API_KEY_NOSHARE) and is
simply paid for. Those billed calls use Flex processing (Batch rates, -50%), falling
back to the standard tier when Flex is out of capacity (429, not billed) or too slow.
Free-program (data-sharing) traffic stays on the standard tier.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Literal

from openai import APITimeoutError, RateLimitError

if TYPE_CHECKING:
    from collections.abc import Callable

    from openai import OpenAI
    from openai.types.responses import Response

Tier = Literal["auto", "flex"]

NOSHARE_MODELS = {m.strip() for m in os.environ.get("NOSHARE_MODELS", "gpt-6-luna").split(",") if m.strip()}
# Flex can queue well past the standard tier's latency; OpenAI suggests up to 15 min.
FLEX_TIMEOUT = float(os.environ.get("OPENAI_FLEX_TIMEOUT", "900"))


def api_key_for(model: str) -> str | None:
    """Pick the OpenAI key for a model.

    Returns:
        OPENAI_API_KEY_NOSHARE for NOSHARE_MODELS when it is set, else OPENAI_API_KEY.

    """
    if model in NOSHARE_MODELS and os.environ.get("OPENAI_API_KEY_NOSHARE"):
        return os.environ["OPENAI_API_KEY_NOSHARE"]
    return os.environ.get("OPENAI_API_KEY")


def uses_flex(model: str) -> bool:
    """Decide whether a model's calls go to the Flex tier.

    Returns:
        True only for billed noshare traffic, unless OPENAI_FLEX=0.

    """
    return (
        model in NOSHARE_MODELS
        and bool(os.environ.get("OPENAI_API_KEY_NOSHARE"))
        and os.environ.get("OPENAI_FLEX", "1") != "0"
    )


def send_with_flex(
    client: OpenAI,
    model: str,
    request: Callable[[OpenAI, Tier, float], Response],
    *,
    timeout: float,
    flex_timeout: float = FLEX_TIMEOUT,
) -> Response:
    """Send a request at the Flex tier for billed models, falling back to the standard tier.

    Returns:
        The response from whichever tier answered.

    """
    if not uses_flex(model):
        return request(client, "auto", timeout)
    try:
        # No SDK retries: a Flex 429 means "no capacity", so fall back at once instead.
        return request(client.with_options(max_retries=0), "flex", flex_timeout)
    except (RateLimitError, APITimeoutError) as exc:
        logging.info("Flex unavailable for %s (%s); retrying at standard tier", model, type(exc).__name__)
        return request(client, "auto", timeout)
