"""Tests for OpenAI key and Flex routing (script-style; runs without network)."""

from __future__ import annotations

import logging
import os
from typing import final

import httpx2
from openai import RateLimitError

from podcast_shared.openai_routing import api_key_for, send_with_flex, uses_flex

_RESPONSE = object()


def _fail(msg: str) -> None:
    """Raise an AssertionError.

    Raises:
        AssertionError: Always.

    """
    raise AssertionError(msg)


def _rate_limit() -> RateLimitError:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    return RateLimitError("Resource Unavailable", response=httpx2.Response(429, request=request), body=None)


@final
class _Client:
    """Stands in for OpenAI: records with_options calls and returns itself."""

    def __init__(self) -> None:
        self.options: list[int] = []

    def with_options(self, *, max_retries: int) -> _Client:
        self.options.append(max_retries)
        return self


@final
class _Recorder:
    """A request function that records (tier, timeout) and can fail the Flex attempt."""

    def __init__(self, *, fail_flex: bool = False) -> None:
        self.fail_flex = fail_flex
        self.calls: list[tuple[str, float]] = []

    def __call__(self, _client: object, tier: str, timeout: float) -> object:
        self.calls.append((tier, timeout))
        if tier == "flex" and self.fail_flex:
            raise _rate_limit()
        return _RESPONSE


def _set_env(**env: str | None) -> None:
    for name, value in env.items():
        if value is None:
            _ = os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_key_routing() -> None:
    """Luna uses the noshare key when set; everything else the share key, else OPENAI_API_KEY."""
    _set_env(OPENAI_API_KEY="sk-share", OPENAI_API_KEY_NOSHARE="sk-noshare", OPENAI_FLEX=None)
    if api_key_for("gpt-6-luna") != "sk-noshare" or api_key_for("gpt-5-mini") != "sk-share":
        _fail("key routing")
    _set_env(OPENAI_API_KEY_NOSHARE=None)
    if api_key_for("gpt-6-luna") != "sk-share":
        _fail("luna should fall back to OPENAI_API_KEY without a noshare key")
    _set_env(OPENAI_API_KEY_SHARE="sk-share-project")
    if api_key_for("gpt-5.6-luna") != "sk-share-project":
        _fail("share models should prefer OPENAI_API_KEY_SHARE")
    _set_env(OPENAI_API_KEY_SHARE=None)


def test_flex_only_for_billed_noshare_traffic() -> None:
    """Flex applies to noshare models with a noshare key, unless OPENAI_FLEX=0."""
    _set_env(OPENAI_API_KEY_NOSHARE="sk-noshare", OPENAI_FLEX=None)
    if not uses_flex("gpt-6-luna") or uses_flex("gpt-5-mini"):
        _fail("flex gating by model")
    _set_env(OPENAI_FLEX="0")
    if uses_flex("gpt-6-luna"):
        _fail("OPENAI_FLEX=0 should disable flex")
    _set_env(OPENAI_FLEX=None, OPENAI_API_KEY_NOSHARE=None)
    if uses_flex("gpt-6-luna"):
        _fail("no flex when luna falls back to the free-program key")


def test_flex_falls_back_to_standard() -> None:
    """A Flex 429 falls back to one standard-tier call with the normal timeout."""
    _set_env(OPENAI_API_KEY_NOSHARE="sk-noshare", OPENAI_FLEX=None)
    client, request = _Client(), _Recorder(fail_flex=True)
    result = send_with_flex(client, "gpt-6-luna", request, timeout=60, flex_timeout=180)  # pyright: ignore[reportArgumentType]  (test doubles)
    if result is not _RESPONSE or request.calls != [("flex", 180), ("auto", 60)] or client.options != [0]:
        _fail(f"expected flex (no SDK retries) then standard, got {request.calls} {client.options}")


def test_standard_tier_for_free_models() -> None:
    """Models outside NOSHARE_MODELS get a single standard-tier call."""
    _set_env(OPENAI_API_KEY_NOSHARE="sk-noshare", OPENAI_FLEX=None)
    request = _Recorder()
    _ = send_with_flex(_Client(), "gpt-5-mini", request, timeout=300)  # pyright: ignore[reportArgumentType]  (test doubles)
    if request.calls != [("auto", 300)]:
        _fail(f"unexpected calls: {request.calls}")


def run_tests() -> None:
    """Run every test in this module."""
    test_key_routing()
    test_flex_only_for_billed_noshare_traffic()
    test_flex_falls_back_to_standard()
    test_standard_tier_for_free_models()
    logging.info("openai routing tests passed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_tests()
