"""Scoped article extraction ignores sidebars and handles short and image-only posts."""

import logging

from article_extract import extract_body

SIDEBAR = "".join(
    f"<p>Sponsor {i} is a company that makes products you might like and helps people do things well.</p>"
    for i in range(12)
)


def page(body: str) -> str:
    """Build a blog page with a long sidebar next to the post body.

    Returns:
        The page HTML.

    """
    return (
        "<html><body><div class='main'><h1>Post</h1>"
        f"<div class='post-body'>{body}</div></div>"
        f"<div class='sidebar'>{SIDEBAR}</div></body></html>"
    )


def check_short_post_ignores_sidebar() -> None:
    """Check a short post yields its own text, never the sidebar.

    Raises:
        AssertionError: If extraction is wrong.

    """
    text = extract_body(page("<p>Just a short note about the weather today.</p>"), "div.post-body", "u")
    if "short note about the weather" not in text or "Sponsor" in text:
        msg = f"short post extracted wrongly: {text!r}"
        raise AssertionError(msg)


def check_lead_paragraph_kept() -> None:
    """Check a link list keeps its lead paragraph and is not duplicated.

    Raises:
        AssertionError: If extraction is wrong.

    """
    body = "<p>The following are my posts:</p><p>1. <a href='/a'>First</a><br/>2. <a href='/b'>Second</a></p>"
    text = extract_body(page(body), "div.post-body", "u")
    if "The following are my posts" not in text or text.count("First") != 1:
        msg = f"link list extracted wrongly: {text!r}"
        raise AssertionError(msg)


def check_image_only_post() -> None:
    """Check an image-only post falls back to the image's title text.

    Raises:
        AssertionError: If extraction is wrong.

    """
    body = "<p><img src='x.png' alt='' title='A comic about philosophy.'/></p>"
    text = extract_body(page(body), "div.post-body", "u")
    if text != "Image: A comic about philosophy.":
        msg = f"image-only post extracted wrongly: {text!r}"
        raise AssertionError(msg)


def check_missing_selector_raises() -> None:
    """Check an unmatched selector raises instead of falling back to the whole page.

    Raises:
        AssertionError: If extraction is wrong.

    """
    try:
        _ = extract_body(page("<p>Body.</p>"), "div.nope", "u")
    except ValueError:
        return
    msg = "expected ValueError for an unmatched selector"
    raise AssertionError(msg)


if __name__ == "__main__":
    check_short_post_ignores_sidebar()
    check_lead_paragraph_kept()
    check_image_only_post()
    check_missing_selector_raises()
    logging.info("article extraction tests passed.")
