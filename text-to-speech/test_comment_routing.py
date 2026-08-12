"""archive-comments routes to multi-voice; META_PUB_DATE parses to a datetime."""

import logging
from datetime import datetime

from text_to_speech import is_comment_episode, parse_pub_date


def check() -> None:
    """Verify comment-episode detection and pub-date parsing.

    Raises:
        AssertionError: If routing detection or date parsing is wrong.

    """
    if not is_comment_episode({"intake_type": "archive-comments"}):
        msg = "archive-comments should be detected as a comment episode"
        raise AssertionError(msg)
    if is_comment_episode({"intake_type": "archive"}):
        msg = "archive should not be a comment episode"
        raise AssertionError(msg)
    dt = parse_pub_date({"pub_date": "2013-05-02T12:01:00+00:00"})
    if not isinstance(dt, datetime) or dt.minute != 1:
        msg = f"unexpected parsed pub date: {dt!r}"
        raise AssertionError(msg)
    if parse_pub_date({}) is not None:
        msg = "missing pub_date should parse to None"
        raise AssertionError(msg)


if __name__ == "__main__":
    check()
    logging.info("comment routing tests passed.")
