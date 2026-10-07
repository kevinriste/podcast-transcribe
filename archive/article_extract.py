"""Article-body extraction for archive posts, optionally scoped to a configured content element."""

import copy
import re

from bs4 import BeautifulSoup, Tag
from trafilatura import extract

# Minimum share of the content element's words that trafilatura's output must cover before
# we trust it; below this it has dropped or mangled the post and the element's own text wins.
MIN_COVERAGE = 0.8
BLOCK_TAGS = ["p", "div", "li", "blockquote", "pre", "tr", "h1", "h2", "h3", "h4", "h5", "h6"]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _coverage(extracted: str, reference: str) -> float:
    reference_words = _words(reference)
    if not reference_words:
        return 1.0
    extracted_words = set(_words(extracted))
    return sum(word in extracted_words for word in reference_words) / len(reference_words)


def _element_text(element: Tag) -> str:
    """Flatten the element's visible text to one line per block, dropping blank lines.

    Returns:
        The element's text.

    """
    element = copy.copy(element)
    for br in element.find_all("br"):
        _ = br.replace_with("\n")
    for block in element.find_all(BLOCK_TAGS):
        _ = block.append("\n")
    lines = (re.sub(r"\s+", " ", line).strip() for line in element.get_text().splitlines())
    return "\n".join(line for line in lines if line)


def _image_texts(element: Tag) -> str:
    """Collect the title (else alt) text of the element's images, for posts that are only an image.

    Returns:
        One "Image: ..." line per image that has such text.

    """
    texts: list[str] = []
    for img in element.find_all("img"):
        for attr in ("title", "alt"):
            value = img.get(attr)
            if isinstance(value, str) and value.strip():
                texts.append(f"Image: {value.strip()}")
                break
    return "\n".join(texts)


def extract_body(html: str, content_selector: str, url: str) -> str:
    """Extract a post's body text.

    Without a selector, trafilatura picks the main content from the whole page. That fails
    on very short posts, where a sidebar can outweigh the article and be chosen instead, so
    a configured ``content_selector`` (a CSS selector for the post's content element) scopes
    extraction to that element. Within it, trafilatura's output is used when it covers the
    element's text; otherwise the element's own text, and for image-only posts the images'
    title/alt text.

    Returns:
        The extracted body text.

    Raises:
        ValueError: If the selector matches nothing or no text could be extracted.

    """
    if not content_selector:
        text = extract(html, include_comments=False, favor_recall=True)
        if not text:
            msg = f"Trafilatura returned no content for {url}"
            raise ValueError(msg)
        return text

    element = BeautifulSoup(html, "html.parser").select_one(content_selector)
    if element is None:
        msg = f"Content selector {content_selector!r} matched nothing on {url}; has the page layout changed?"
        raise ValueError(msg)
    element_text = _element_text(element)
    scoped = extract(f"<html><body>{element}</body></html>", include_comments=False, favor_recall=True) or ""
    if scoped and _coverage(scoped, element_text) >= MIN_COVERAGE:
        return scoped
    text = element_text or _image_texts(element)
    if not text:
        msg = f"No text found in {content_selector!r} on {url}"
        raise ValueError(msg)
    return text
