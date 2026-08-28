"""Re-stamp published episode mtimes so Dropcaster re-orders them by original date.

Dropcaster orders the feed by each mp3's file mtime (its ID3-TRDA path is unusable
via mutagen). A batch re-render collapses every episode's mtime to "now", scrambling
the order. This tool fixes already-published files without re-rendering: it reads the
``YYYYMMDD-HHMMSS`` stamp embedded in each filename (the original receipt/publish date,
written by intake) and sets the file's mtime to it.

"Highlights From The Comments" episodes are a special case: a backfill can stamp them
far from the article they discuss. So comment episodes are not ordered by their own
filename — instead each is paired to its article (by cleaned title) and stamped
``COMMENT_OFFSET`` after it, so it always lands right after its friend. Comments with no
matching article fall back to their filename stamp.

Dry-run by default — pass --apply to write. After applying, let Dropcaster regenerate
index.rss (it watches the audio dir).

Usage (from text-to-speech/):
    uv run python3 restamp_mtime.py ../dropcaster-docker/audio                 # preview
    uv run python3 restamp_mtime.py ../dropcaster-docker/audio --apply         # write
    uv run python3 restamp_mtime.py "../dropcaster-docker/audio/Some Ep.mp3" \
        --date 2013-05-02T12:00:00 --apply                                     # explicit override
"""

import argparse
import logging
import pathlib
from datetime import datetime

from podcast_shared import (
    COMMENT_OFFSET,
    set_file_pub_date,
    stamp_from_name,
    title_key,
)
from podcast_shared import (
    is_comment_filename as is_comment,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def resolve_pub_dates(files: list[pathlib.Path]) -> dict[pathlib.Path, tuple[datetime, str]]:
    """Decide each file's target mtime, pairing comment episodes to their articles.

    Articles are stamped from their own filename date; each comment episode is stamped
    COMMENT_OFFSET after its matched article, falling back to its own filename date when
    no article matches. Files with no resolvable date are omitted from the result.

    Returns:
        A mapping of file path to (target datetime, human-readable source note).

    """
    articles = [f for f in files if not is_comment(f.stem)]
    comments = [f for f in files if is_comment(f.stem)]

    article_by_title: dict[str, pathlib.Path] = {}
    ambiguous_titles: set[str] = set()
    for article in articles:
        key = title_key(article.stem)
        if key in article_by_title:
            ambiguous_titles.add(key)  # keep the first; only matters if a comment pairs to it
            continue
        article_by_title[key] = article

    plan: dict[pathlib.Path, tuple[datetime, str]] = {}
    for article in articles:
        date = stamp_from_name(article.stem)
        if date is not None:
            plan[article] = (date, "filename")
    for comment in comments:
        key = title_key(comment.stem)
        if key in ambiguous_titles:
            logging.warning("comment %r matches multiple articles titled %r; pairing to the first", comment.name, key)
        matched = article_by_title.get(key)
        article_date = stamp_from_name(matched.stem) if matched is not None else None
        if article_date is not None:
            plan[comment] = (article_date + COMMENT_OFFSET, f"article+{int(COMMENT_OFFSET.total_seconds())}s")
        else:
            fallback = stamp_from_name(comment.stem)
            if fallback is not None:
                plan[comment] = (fallback, "filename (no article match)")
    return plan


def collect_mp3s(paths: list[str]) -> list[pathlib.Path]:
    """Expand the given file/directory paths into a sorted list of mp3 files.

    Returns:
        Every ``*.mp3`` in each directory (non-recursive) plus each mp3 file given, sorted.

    """
    files: set[pathlib.Path] = set()
    for raw in paths:
        p = pathlib.Path(raw)
        if p.is_dir():
            files.update(p.glob("*.mp3"))
        elif p.suffix.lower() == ".mp3":
            files.add(p)
        else:
            logging.warning("skipping non-mp3 path: %s", p)
    return sorted(files)


def main() -> None:
    """Parse arguments and re-stamp the selected episodes' mtimes."""
    parser = argparse.ArgumentParser(description="Re-stamp published episode mtimes from their filename dates.")
    _ = parser.add_argument("paths", nargs="+", help="mp3 files and/or directories of mp3s")
    _ = parser.add_argument(
        "--date",
        help="explicit ISO datetime to set (only valid with a single mp3 file); overrides the filename stamp",
    )
    _ = parser.add_argument("--apply", action="store_true", help="write changes (default is a dry-run preview)")
    args = parser.parse_args()

    paths = [str(p) for p in args.paths]  # pyright: ignore[reportAny]
    explicit_iso = str(args.date) if args.date is not None else None  # pyright: ignore[reportAny]
    apply = bool(args.apply)  # pyright: ignore[reportAny]

    files = collect_mp3s(paths)
    if not files:
        logging.error("no mp3 files found in: %s", ", ".join(paths))
        return

    if explicit_iso is not None:
        if len(files) != 1:
            logging.error("--date requires exactly one mp3 file (got %d)", len(files))
            return
        try:
            explicit_date = datetime.fromisoformat(explicit_iso)
        except ValueError:
            logging.exception("could not parse --date %r as ISO datetime", explicit_iso)
            return
        plan = {files[0]: (explicit_date, "explicit --date")}
    else:
        plan = resolve_pub_dates(files)

    changed = 0
    for mp3 in files:
        entry = plan.get(mp3)
        if entry is None:
            logging.warning("no date stamp in filename, skipping: %s", mp3.name)
            continue
        pub_date, note = entry
        verb = "setting" if apply else "would set"
        logging.info("%s mtime %s [%s] -> %s", verb, pub_date.isoformat(), note, mp3.name)
        if apply:
            set_file_pub_date(str(mp3), pub_date)
        changed += 1

    if apply:
        logging.info("re-stamped %d file(s). Let Dropcaster regenerate index.rss.", changed)
    else:
        logging.info("dry run: %d file(s) would change. Re-run with --apply to write.", changed)


if __name__ == "__main__":
    main()
