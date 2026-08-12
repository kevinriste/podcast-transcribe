"""Comment-episode metadata block + pub-date ordering are correct."""

import logging
from datetime import UTC, datetime

from comment_briefing import article_pub_dates, comment_metadata_block


def check() -> None:
    """Verify the comment episode is dated 60s after the article and headers are right.

    Raises:
        AssertionError: If ordering delta or header fields are wrong.

    """
    now = datetime(2013, 5, 2, 12, 0, 0, tzinfo=UTC)
    art, com = article_pub_dates(now)
    if (com - art).total_seconds() != 60:
        msg = f"comment must be 60s after article, got {(com - art).total_seconds()}s"
        raise AssertionError(msg)
    block = comment_metadata_block("Example Blog", "My Post", "http://x", com, "The post argues X.")
    for needle in (
        "META_FROM: Example Blog",
        "META_INTAKE_TYPE: archive-comments",
        "META_TITLE: Comment Highlights: My Post",
        f"META_PUB_DATE: {com.isoformat()}",
        "META_ARTICLE_SUMMARY: The post argues X.",
    ):
        if needle not in block:
            msg = f"missing {needle!r} in block:\n{block}"
            raise AssertionError(msg)


if __name__ == "__main__":
    check()
    logging.info("check-archive output tests passed.")
