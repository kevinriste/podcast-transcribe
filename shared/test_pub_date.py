"""set_file_pub_date sets mtime so Dropcaster (mtime fallback) orders episodes."""

import logging
import pathlib
import tempfile
from datetime import UTC, datetime

from podcast_shared import pub_date_from_filename, set_file_pub_date


def check_filename_parser() -> None:
    """Verify the YYYYMMDD-HHMMSS filename prefix parses to a local datetime.

    Raises:
        AssertionError: If a valid prefix does not parse, or an invalid one parses.

    """
    got = pub_date_from_filename("20130502-120000-Author- Some Title-20240101")
    expected = datetime(2013, 5, 2, 12, 0, 0)  # noqa: DTZ001 — naive local, matches how stamps are created
    if got != expected:
        msg = f"prefix parse {got!r} != {expected!r}"
        raise AssertionError(msg)
    if pub_date_from_filename("no-date-here") is not None:
        msg = "a stem without a leading date prefix must parse to None"
        raise AssertionError(msg)
    if pub_date_from_filename("20139902-120000-bad-month") is not None:
        msg = "an out-of-range date prefix must parse to None"
        raise AssertionError(msg)


def check() -> None:
    """Verify mtime is set to the given pub date, and later dates sort after.

    Raises:
        AssertionError: If mtime is not set as expected or ordering is wrong.

    """
    with tempfile.TemporaryDirectory() as d:
        article = pathlib.Path(d) / "article.mp3"
        comments = pathlib.Path(d) / "comments.mp3"
        _ = article.write_bytes(b"a")
        _ = comments.write_bytes(b"c")
        art_pd = datetime(2013, 5, 2, 12, 0, 0, tzinfo=UTC)
        com_pd = datetime(2013, 5, 2, 12, 1, 0, tzinfo=UTC)
        set_file_pub_date(str(article), art_pd)
        set_file_pub_date(str(comments), com_pd)
        if article.stat().st_mtime != art_pd.timestamp():
            msg = f"article mtime {article.stat().st_mtime} != {art_pd.timestamp()}"
            raise AssertionError(msg)
        if comments.stat().st_mtime <= article.stat().st_mtime:
            msg = "comments episode must sort after the article episode"
            raise AssertionError(msg)


if __name__ == "__main__":
    check_filename_parser()
    check()
    logging.info("set_file_pub_date test passed.")
