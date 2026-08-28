"""Re-stamp published episode mtimes so Dropcaster re-orders them by original date.

Dropcaster orders the feed by each mp3's file mtime (its ID3-TRDA path is unusable
via mutagen). A batch re-render collapses every episode's mtime to "now", scrambling
the order. This tool fixes already-published files without re-rendering: it reads the
``YYYYMMDD-HHMMSS`` stamp embedded in each filename (the original receipt/publish date,
written by intake) and sets the file's mtime to it.

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
import re
from datetime import datetime

from podcast_shared import set_file_pub_date

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_STAMP_RE = re.compile(r"(\d{8})-(\d{6})")


def stamp_from_name(stem: str) -> datetime | None:
    """Extract the first embedded ``YYYYMMDD-HHMMSS`` stamp from a published filename.

    Published names carry the stamp after the source prefix (e.g. ``Source- 20130502-120000- Title-...``),
    so it is searched for anywhere, not anchored at the start.

    Returns:
        The parsed naive-local datetime, or None if no valid stamp is present.

    """
    match = _STAMP_RE.search(stem)
    if not match:
        return None
    try:
        return datetime.strptime(f"{match.group(1)}-{match.group(2)}", "%Y%m%d-%H%M%S")  # noqa: DTZ007
    except ValueError:
        return None


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

    explicit_date: datetime | None = None
    if explicit_iso is not None:
        if len(files) != 1:
            logging.error("--date requires exactly one mp3 file (got %d)", len(files))
            return
        try:
            explicit_date = datetime.fromisoformat(explicit_iso)
        except ValueError:
            logging.exception("could not parse --date %r as ISO datetime", explicit_iso)
            return

    changed = 0
    for mp3 in files:
        pub_date = explicit_date or stamp_from_name(mp3.stem)
        if pub_date is None:
            logging.warning("no date stamp in filename, skipping: %s", mp3.name)
            continue
        verb = "setting" if apply else "would set"
        logging.info("%s mtime %s -> %s", verb, pub_date.isoformat(), mp3.name)
        if apply:
            set_file_pub_date(str(mp3), pub_date)
        changed += 1

    if apply:
        logging.info("re-stamped %d file(s). Let Dropcaster regenerate index.rss.", changed)
    else:
        logging.info("dry run: %d file(s) would change. Re-run with --apply to write.", changed)


if __name__ == "__main__":
    main()
