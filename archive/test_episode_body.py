"""An archive post with a content selector goes to the HTML stage; without one, plain text."""

import logging
from typing import NoReturn

from podcast_shared import split_metadata

from article_extract import article_episode_body

_PAGE = (
    '<html><body><div class="sidebar"><p>Sponsored by someone else entirely.</p></div>'
    '<div class="post"><p>The post body, long enough to be the article.</p></div></body></html>'
)


def _fail(msg: str) -> NoReturn:
    raise AssertionError(msg)


def check_selector_sends_html() -> None:
    """With a selector the raw body is the page HTML, headed for the HTML stage."""
    headers, body, article_text = article_episode_body(
        _PAGE, "A Post", "2013-05-12", "https://blog.example.com/a-post/", "div.post"
    )
    metadata, _ = split_metadata(headers + "\n\nx")
    expected = {"body_format": "html", "content_selector": "div.post", "preface": "Originally published: 2013-05-12"}
    if metadata != expected:
        _fail(f"headers wrong: {metadata!r}")
    if body != _PAGE:
        _fail("the raw body should be the page HTML")
    if article_text != "The post body, long enough to be the article.":
        _fail(f"briefing text should be the selected article: {article_text!r}")


def check_unmatched_selector_raises() -> None:
    """Fail at intake, instead of publishing junk, when the selector matches nothing."""
    try:
        _ = article_episode_body(_PAGE, "A Post", "2013-05-12", "https://x/", "div.missing")
    except ValueError:
        return
    _fail("an unmatched selector should raise")


def check_no_selector_writes_plain_text() -> None:
    """Without a selector the article text is extracted here and written as plain text."""
    page = (
        "<html><body><article><h1>A Post</h1>"
        + "<p>This is a reasonably long paragraph of article text for extraction.</p>" * 5
        + "</article></body></html>"
    )
    headers, body, _ = article_episode_body(page, "A Post", "2013-05-12", "https://x/", "")
    if headers != "META_EXTRACTION: plaintext":
        _fail(f"headers wrong: {headers!r}")
    if not body.startswith("A Post\nOriginally published: 2013-05-12\n\n") or "article text" not in body:
        _fail(f"plain-text body wrong: {body!r}")


if __name__ == "__main__":
    check_selector_sends_html()
    check_unmatched_selector_raises()
    check_no_selector_writes_plain_text()
    logging.info("archive episode body tests passed")
