"""Tests for restamp_mtime: filename date parsing and comment-to-article pairing.

Comment-highlights episodes must land right after the article they discuss, even
when a backfill stamped them far away. resolve_pub_dates pairs each comment to its
article by cleaned title and stamps it COMMENT_OFFSET later; unmatched comments and
all articles fall back to their own filename stamp.
"""

import logging
import pathlib
from datetime import datetime

from podcast_shared import (
    COMMENT_OFFSET,
    stamp_from_name,
    title_key,
)
from podcast_shared import (
    is_comment_filename as is_comment,
)
from restamp_mtime import resolve_pub_dates

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def check_stamp_and_title() -> None:
    """Verify stamp extraction, comment detection, and title-key normalization.

    Raises:
        AssertionError: If any parsed value is wrong.

    """
    art = "SSC- 20260817-000015- Raikoth Economics Relationships-20260817"
    com = "ARCHIVE- 20260812-221926- COMMENTS-Raikoth Economics Relationships-20260812"
    if stamp_from_name(art) != datetime(2026, 8, 17, 0, 0, 15):  # noqa: DTZ001
        msg = "article stamp parse wrong"
        raise AssertionError(msg)
    if is_comment(art) or not is_comment(com):
        msg = "comment detection wrong"
        raise AssertionError(msg)
    if title_key(art) != "Raikoth Economics Relationships":
        msg = f"article title_key wrong: {title_key(art)!r}"
        raise AssertionError(msg)
    if title_key(com) != title_key(art):
        msg = f"comment title_key {title_key(com)!r} != article {title_key(art)!r}"
        raise AssertionError(msg)


def check_pairing() -> None:
    """Verify a comment is stamped COMMENT_OFFSET after its article, wherever it started.

    Raises:
        AssertionError: If the comment is not re-anchored to its article.

    """
    article = pathlib.Path("SSC- 20260817-000015- Raikoth Economics Relationships-20260817.mp3")
    # This comment's own filename date is months away (a backfill mass) -- it must be ignored.
    comment = pathlib.Path("ARCHIVE- 20260812-221926- COMMENTS-Raikoth Economics Relationships-20260812.mp3")
    orphan = pathlib.Path("ARCHIVE- 20260101-090000- COMMENTS-No Such Article-20260101.mp3")

    plan = resolve_pub_dates([comment, article, orphan])

    art_date = datetime(2026, 8, 17, 0, 0, 15)  # noqa: DTZ001
    if plan[article][0] != art_date:
        msg = "article should keep its filename date"
        raise AssertionError(msg)
    if plan[comment][0] != art_date + COMMENT_OFFSET:
        msg = f"comment should be article+{COMMENT_OFFSET}; got {plan[comment][0]}"
        raise AssertionError(msg)
    if plan[comment][0] <= plan[article][0]:
        msg = "comment must sort after its article"
        raise AssertionError(msg)
    # An orphan comment (no matching article) falls back to its own filename date.
    if plan[orphan][0] != datetime(2026, 1, 1, 9, 0, 0):  # noqa: DTZ001
        msg = "orphan comment should fall back to its filename date"
        raise AssertionError(msg)


def run_tests() -> None:
    """Run all restamp tests."""
    check_stamp_and_title()
    check_pairing()
    logging.info("All restamp tests passed successfully!")


if __name__ == "__main__":
    run_tests()
