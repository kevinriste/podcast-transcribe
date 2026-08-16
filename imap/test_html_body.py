"""Tests for HTML-body extraction, including block-quotation marking."""

import logging

from podcast_shared import BLOCKQUOTE_MARKER

from parse_email import extract_body_text

logging.basicConfig(level=logging.INFO)


def check_blockquote_is_marked() -> None:
    """Prefix a <blockquote> block with the marker while leaving surrounding prose bare.

    Raises:
        AssertionError: If marking is wrong.

    """
    html = "<p>Author intro.</p><blockquote>A quoted line.</blockquote><p>Author reply.</p>"
    body = extract_body_text(html) or ""
    expected = ["Author intro.", f"{BLOCKQUOTE_MARKER}A quoted line.", "Author reply."]
    if body.split("\n\n") != expected:
        msg = f"got {body!r}"
        raise AssertionError(msg)


def check_multiparagraph_blockquote_marked() -> None:
    """Mark each paragraph inside a multi-paragraph <blockquote>.

    Raises:
        AssertionError: If inner paragraphs are not marked.

    """
    html = "<blockquote><p>Quote para one.</p><p>Quote para two.</p></blockquote>"
    body = extract_body_text(html) or ""
    expected = [f"{BLOCKQUOTE_MARKER}Quote para one.", f"{BLOCKQUOTE_MARKER}Quote para two."]
    if body.split("\n\n") != expected:
        msg = f"got {body!r}"
        raise AssertionError(msg)


def check_no_blockquote_unchanged() -> None:
    """Leave prose with no block quote free of markers.

    Raises:
        AssertionError: If a marker leaks into unquoted text.

    """
    body = extract_body_text("<p>Just prose.</p><p>More prose.</p>") or ""
    if BLOCKQUOTE_MARKER in body:
        msg = f"unexpected marker in {body!r}"
        raise AssertionError(msg)


if __name__ == "__main__":
    check_blockquote_is_marked()
    check_multiparagraph_blockquote_marked()
    check_no_blockquote_unchanged()
    logging.info("HTML body extraction tests passed.")
