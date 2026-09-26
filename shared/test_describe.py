"""Tests for image enrichment (script-style; the vision call is faked, never made)."""

import logging
import os
from contextlib import AbstractContextManager, ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import httpx2
from openai import AuthenticationError, BadRequestError, NotFoundError, OpenAIError

from podcast_shared.describe import VisionRejectedError, VisionUnavailableError, describe_image, enrich_images
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


class _FlakyResponses:
    """Stand-in for ``client.responses`` failing ``failures`` times, then succeeding."""

    def __init__(self, failures: int, error: OpenAIError | None = None) -> None:
        """Remember how many calls should fail, and with what (default: a generic transient error)."""
        self.failures: int = failures
        self.error: OpenAIError = error or OpenAIError("Unable to download content from the provided URL")
        self.calls: int = 0

    def create(self, **_: object) -> SimpleNamespace:
        """Fail with an OpenAIError until the failure budget is spent.

        Raises ``self.error`` while failures remain.

        Returns:
            A response-like object with ``output_text``.

        """
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return SimpleNamespace(output_text=" A chart. ")


def _with_fake_client(responses: _FlakyResponses) -> AbstractContextManager[object]:
    """Patch OpenAI (and retry sleeps) so describe_image talks to ``responses``.

    Returns:
        A context manager applying the patches.

    """
    stack = ExitStack()
    if "OPENAI_API_KEY" not in os.environ:
        os.environ["OPENAI_API_KEY"] = "test-key"
        _ = stack.callback(os.environ.pop, "OPENAI_API_KEY", None)
    fake_client = SimpleNamespace(responses=responses)

    def fake_openai(**_: object) -> SimpleNamespace:
        return fake_client

    def no_sleep(_seconds: float) -> None:
        return None

    _ = stack.enter_context(patch("podcast_shared.describe.OpenAI", new=fake_openai))
    _ = stack.enter_context(patch("podcast_shared.describe.time.sleep", new=no_sleep))
    return stack


def test_describe_image_retries_then_succeeds() -> None:
    """A transient failure is retried and the eventual description returned."""
    responses = _FlakyResponses(failures=2)
    with _with_fake_client(responses):
        desc = describe_image("http://x/img.png")
    if desc != "A chart." or responses.calls != 3:
        _fail(f"expected success on 3rd attempt, got {desc!r} after {responses.calls} calls")


def test_describe_image_raises_after_all_attempts() -> None:
    """When every attempt fails with a service error, VisionUnavailableError(outage=True) is raised."""
    responses = _FlakyResponses(failures=99)
    with _with_fake_client(responses):
        try:
            _ = describe_image("http://x/img.png")
        except VisionUnavailableError as exc:
            if not exc.outage or responses.calls != 3:
                _fail(f"expected an outage after 3 calls, got outage={exc.outage} after {responses.calls}")
            return
    _fail("expected VisionUnavailableError")


def _status_error(
    cls: type[BadRequestError | AuthenticationError | NotFoundError],
    status: int,
    message: str,
    body: dict[str, str] | None = None,
) -> OpenAIError:
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.test/v1/responses"))
    return cls(message, response=response, body=body)


def test_describe_image_config_error_defers_without_retrying() -> None:
    """A bad key, model or request parameter raises an outage on the first call; retrying can't help."""
    for error in (
        _status_error(AuthenticationError, 401, "Incorrect API key provided"),
        _status_error(NotFoundError, 404, "The model does not exist"),
        _status_error(BadRequestError, 400, "The requested model does not exist", {"param": "model"}),
        _status_error(
            BadRequestError, 400, "Unsupported parameter: 'service_tier'", {"param": "service_tier"}
        ),
    ):
        responses = _FlakyResponses(failures=99, error=error)
        with _with_fake_client(responses):
            try:
                _ = describe_image("http://x/img.png")
            except VisionUnavailableError as exc:
                if not exc.outage:
                    _fail(f"{error} should count as an outage (it affects every image)")
            else:
                _fail(f"{error} should raise VisionUnavailableError")
        if responses.calls != 1:
            _fail(f"{error} was retried ({responses.calls} calls)")


def test_describe_image_rejected_image_raises_rejection() -> None:
    """An image the API refuses outright raises VisionRejectedError at once (reported, not deferred)."""
    for message, body in (
        ("Invalid image", {"code": "invalid_image_format"}),
        ("Invalid image", {"param": "input[0].content[1].image_url"}),
        ("Invalid image https://cdn.example.com/download/a.svg", {"code": "invalid_image_format"}),
    ):
        responses = _FlakyResponses(failures=99, error=_status_error(BadRequestError, 400, message, body))
        with _with_fake_client(responses):
            try:
                _ = describe_image("http://x/img.svg")
            except VisionRejectedError:
                pass
            else:
                _fail(f"{body} should raise VisionRejectedError")
        if responses.calls != 1:
            _fail(f"rejected image was retried ({responses.calls} calls)")


def test_describe_image_download_400_is_retried() -> None:
    """The 400 'Unable to download' is retried, and if it persists it blames only this image."""
    error = _status_error(
        BadRequestError, 400, "Unable to download content from the provided URL", {"code": "invalid_image_url"}
    )
    responses = _FlakyResponses(failures=1, error=error)
    with _with_fake_client(responses):
        desc = describe_image("http://x/img.png")
    if desc != "A chart." or responses.calls != 2:
        _fail(f"download 400 should be retried, got {desc!r} after {responses.calls} calls")
    responses = _FlakyResponses(failures=99, error=error)
    with _with_fake_client(responses):
        try:
            _ = describe_image("http://x/img.png")
        except VisionUnavailableError as exc:
            if exc.outage or responses.calls != 3:
                _fail(f"persistent download failure: outage={exc.outage}, calls={responses.calls}")
        else:
            _fail("persistent download failure should raise")


def run_tests() -> None:
    """Run enrichment tests."""
    test_enrich_sets_description_on_images()
    test_enrich_skips_existing_and_srcless()
    test_describe_image_no_key_returns_empty()
    test_describe_image_retries_then_succeeds()
    test_describe_image_raises_after_all_attempts()
    test_describe_image_config_error_defers_without_retrying()
    test_describe_image_rejected_image_raises_rejection()
    test_describe_image_download_400_is_retried()
    logging.info("describe/enrich tests passed")


if __name__ == "__main__":
    run_tests()
