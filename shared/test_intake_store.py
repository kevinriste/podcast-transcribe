"""Tests for the raw-HTML intake store (script-style, matching the repo's tests)."""

import logging
import tempfile
from pathlib import Path

from podcast_shared.intake_store import slug_source, store_intake_html

logging.basicConfig(level=logging.INFO)


def _fail(msg: str) -> None:
    """Raise an AssertionError with the given message.

    Raises:
        AssertionError: Always.

    """
    raise AssertionError(msg)


def test_slug_source_normalizes() -> None:
    """slug_source lowercases, hyphenates, and defaults empty input."""
    for source, expected in (
        ("Astral Codex Ten", "astral-codex-ten"),
        ("hi@www.garbageday.email", "hi-www-garbageday-email"),
        ("", "unknown"),
    ):
        result = slug_source(source)
        if result != expected:
            _fail(f"slug_source({source!r}) = {result!r}, expected {expected!r}")


def test_store_writes_html_and_meta() -> None:
    """store_intake_html writes the HTML file and a metadata sidecar."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        html_path = store_intake_html(
            source="Astral Codex Ten",
            episode_id="20260730-010716",
            html="<html><body><p>hi</p></body></html>",
            url="https://example.com/p/x",
            intake_type="email",
            store_root=root,
        )
        expected_path = root / "astral-codex-ten" / "20260730-010716.html"
        if html_path != expected_path:
            _fail(f"path {html_path} != {expected_path}")
        if not html_path.read_text(encoding="utf-8").startswith("<html>"):
            _fail("HTML content not written")
        meta_raw = (root / "astral-codex-ten" / "20260730-010716.meta.json").read_text(encoding="utf-8")
        for fragment in (
            '"source": "Astral Codex Ten"',
            '"url": "https://example.com/p/x"',
            '"intake_type": "email"',
            '"episode_id": "20260730-010716"',
        ):
            if fragment not in meta_raw:
                _fail(f"metadata sidecar missing {fragment!r}; got:\n{meta_raw}")


def test_store_skips_empty_html() -> None:
    """Empty/whitespace HTML is a no-op (no file written)."""
    with tempfile.TemporaryDirectory() as tmp:
        html_path = store_intake_html(
            source="x", episode_id="1", html="   ", url="u", intake_type="email", store_root=Path(tmp),
        )
        if html_path.exists():
            _fail("empty HTML should not have been written")


def test_store_overwrites_idempotently() -> None:
    """Re-storing the same episode id overwrites the HTML."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for body in ("<p>a</p>", "<p>b</p>"):
            _ = store_intake_html(source="s", episode_id="1", html=body, url="u", intake_type="rss", store_root=root)
        final = (root / "s" / "1.html").read_text(encoding="utf-8")
        if final != "<p>b</p>":
            _fail(f"expected overwrite to <p>b</p>, got {final!r}")


def run_tests() -> None:
    """Run all intake-store tests."""
    test_slug_source_normalizes()
    test_store_writes_html_and_meta()
    test_store_skips_empty_html()
    test_store_overwrites_idempotently()
    logging.info("all intake-store tests passed")


if __name__ == "__main__":
    run_tests()
