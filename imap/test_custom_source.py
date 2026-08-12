"""Tests for custom email-source classification and link extraction."""

import logging
import sys

from parse_email import CustomSource, find_source_url, match_custom_source

logging.basicConfig(level=logging.INFO)

_SOURCE = CustomSource(
    kind="html_newsletter",
    match_sender=("examplepublisher.com", "example columnist"),
    use_html_body=True,
    web_version_link_text=("view in browser", "read the web version"),
    source_url_includes=("examplepublisher.com", "exmpl.co"),
    source_url_excludes=("track.examplepublisher.com",),
)


def run_tests() -> None:
    """Run all custom-source parsing tests."""
    subject = "Newsletter: A Sample Edition"

    # 1. Prefer the configured web-version link text.
    links_1 = [
        {"href": "https://track.examplepublisher.com/click?s=1", "text": ""},
        {"href": "https://exmpl.co/4uX7chX", "text": "View in browser"},
        {"href": "https://examplepublisher.com", "text": "Example"},
    ]
    url_1 = find_source_url(links_1, _SOURCE.kind, subject, _SOURCE)
    if url_1 != "https://exmpl.co/4uX7chX":
        logging.error("Test 1 failed: %s", url_1)
        sys.exit(1)

    # 2. Fall back to include/exclude href filtering (skip tracking links).
    links_2 = [
        {"href": "https://track.examplepublisher.com/click?s=1", "text": ""},
        {"href": "https://examplepublisher.com/news/newsletters/123", "text": "Read the article"},
    ]
    url_2 = find_source_url(links_2, _SOURCE.kind, subject, _SOURCE)
    if url_2 != "https://examplepublisher.com/news/newsletters/123":
        logging.error("Test 2 failed: %s", url_2)
        sys.exit(1)

    # 3. Without a custom source, non-Substack links yield nothing.
    url_3 = find_source_url(links_1, "substack", subject)
    if url_3:
        logging.error("Test 3 failed: %s", url_3)
        sys.exit(1)

    # 4. Sender matching finds the source by email or display name.
    if match_custom_source([_SOURCE], "news@examplepublisher.com", "Someone") is not _SOURCE:
        logging.error("Test 4 failed: sender email should match")
        sys.exit(1)
    if match_custom_source([_SOURCE], "x@other.com", "Example Columnist") is not _SOURCE:
        logging.error("Test 4 failed: sender name should match")
        sys.exit(1)
    if match_custom_source([_SOURCE], "x@other.com", "Nobody") is not None:
        logging.error("Test 4 failed: non-matching sender should return None")
        sys.exit(1)

    logging.info("All custom-source classification tests passed successfully!")


if __name__ == "__main__":
    run_tests()
