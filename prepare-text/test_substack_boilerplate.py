"""Substack UI/footer boilerplate is stripped from HTML-derived bodies; content is kept."""

import logging

from prepare_text import apply_general_cleaning


def _clean(text: str, source_kind: str = "substack") -> str:
    return apply_general_cleaning(text, {"source_kind": source_kind}, {}, {})


_FOOTER = (
    "Real final paragraph of the post.\n\n"
    "You're currently a free subscriber to In My Tribe. For the full experience, upgrade your subscription.\n\n"
    "Upgrade to paid\n\n"
    "© 2026 Arnold Kling548 Market Street PMB 72296, San Francisco, CA 94104 Unsubscribe"
)


def check_footer_removed() -> None:
    """Strip the copyright/address/unsubscribe trailer and the free-subscriber upgrade CTA.

    Raises:
        AssertionError: If any footer boilerplate survives cleaning.

    """
    out = _clean(_FOOTER)
    if "Real final paragraph of the post." not in out:
        msg = f"content paragraph was removed: {out!r}"
        raise AssertionError(msg)
    for needle in ("548 Market Street", "Unsubscribe", "upgrade your subscription", "Upgrade to paid", "© 2026"):
        if needle in out:
            msg = f"footer boilerplate {needle!r} survived: {out!r}"
            raise AssertionError(msg)


def check_ui_buttons_removed() -> None:
    """Strip standalone Substack UI-chrome lines (Share/Comment/Like/etc.), keep real content.

    Raises:
        AssertionError: If a button line survives or real content is removed.

    """
    text = (
        "Here is a real paragraph that mentions how readers can share this widely.\n\n"
        "Share\n\nComment\n\nLike\n\nRestack\n\n"
        "The next real paragraph continues the argument."
    )
    out = _clean(text)
    if "real paragraph that mentions how readers can share" not in out:
        msg = f"content line containing 'share' was removed: {out!r}"
        raise AssertionError(msg)
    if "The next real paragraph continues the argument." not in out:
        msg = f"content line was removed: {out!r}"
        raise AssertionError(msg)
    for line in ("Share", "Comment", "Like", "Restack"):
        if any(stripped == line for stripped in (ln.strip(" .") for ln in out.splitlines())):
            msg = f"standalone UI button {line!r} survived: {out!r}"
            raise AssertionError(msg)


def check_preceding_content_preserved() -> None:
    """Keep real body text (e.g. a sponsor block) that sits just above the footer.

    Raises:
        AssertionError: If content preceding the footer is removed.

    """
    text = (
        "A message from my sponsor, Mechanize:\n\n"
        "We're hiring software engineers.\n\n"
        "© 2026 Ssumner548 Market Street PMB 72296, San Francisco, CA 94104 Unsubscribe"
    )
    out = _clean(text)
    if "A message from my sponsor" not in out or "hiring software engineers" not in out:
        msg = f"real content was removed: {out!r}"
        raise AssertionError(msg)
    if "548 Market Street" in out:
        msg = f"footer trailer survived: {out!r}"
        raise AssertionError(msg)


def check_not_applied_to_non_substack() -> None:
    """Leave the trailer untouched for non-Substack sources (it is Substack-specific).

    Raises:
        AssertionError: If the footer step fires for a non-Substack source.

    """
    out = _clean(_FOOTER, source_kind="url")
    if "548 Market Street" not in out:
        msg = f"footer wrongly stripped for non-substack source: {out!r}"
        raise AssertionError(msg)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    check_footer_removed()
    check_ui_buttons_removed()
    check_preceding_content_preserved()
    check_not_applied_to_non_substack()
    logging.info("Substack boilerplate removal tests passed.")
