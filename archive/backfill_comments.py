"""One-off: backfill comment-highlights episodes for specific archive post indices.

Does NOT touch state.json (targets already-published posts). Each comment episode is
stamped COMMENT_OFFSET after the article it discusses -- the article's date lives only
in its already-published filename (archive posts are stamped at walk time, not their
original 2013 date), so it is looked up from the audio dir by title. A post whose
article is not found falls back to a staggered fresh-batch date. Run prepare_text.py
then text_to_speech.py afterwards.

Usage:
    cd archive && uv run python3 backfill_comments.py 6 17 18 20 41
"""

import json
import logging
import pathlib
import re
import sys
import time
from datetime import UTC, datetime, timedelta

import requests
from podcast_shared import (
    COMMENT_OFFSET,
    generate_summary,
    is_comment_filename,
    stamp_from_name,
    title_key,
)

from article_extract import extract_body
from comment_briefing import (
    MIN_COMMENTS,
    build_briefing,
    comment_metadata_block,
    extract_comments,
    load_source_config,
)

OUTPUT_FOLDER = "../prepare-text/text-input-raw"
# Archive episodes route to the evergreen feed; also scan the topical dir for older ones.
AUDIO_DIRS = ("../dropcaster-docker/audio/evergreen", "../dropcaster-docker/audio")
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
STAGGER = timedelta(minutes=2)
PACING_SECONDS = 45

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def published_article_dates(audio_dirs: tuple[str, ...]) -> dict[str, datetime]:
    """Map each published article's cleaned title to its filename date.

    Comment episodes and duplicate renders are ignored; the first date seen for a title
    wins. Used to anchor a backfilled comment to the article it discusses.

    Returns:
        A mapping of cleaned article title to the article's publication datetime.

    """
    dates: dict[str, datetime] = {}
    for directory in audio_dirs:
        base = pathlib.Path(directory)
        if not base.is_dir():
            continue
        for mp3 in base.glob("*.mp3"):
            if is_comment_filename(mp3.stem):
                continue
            date = stamp_from_name(mp3.stem)
            if date is not None:
                _ = dates.setdefault(title_key(mp3.stem), date)
    return dates


def main() -> None:
    """Generate comment episodes for the post indices given on the command line."""
    indices = sorted(int(a) for a in sys.argv[1:])
    if not indices:
        logging.error("Provide post indices, e.g. backfill_comments.py 6 17 18 20 41")
        return
    config = load_source_config()
    source_name, posts_file = config.source_name, config.posts_file
    posts: list[dict[str, str]] = json.loads(  # pyright: ignore[reportAny]
        pathlib.Path(posts_file).read_text(encoding="utf-8"),
    )
    pathlib.Path(OUTPUT_FOLDER).mkdir(parents=True, exist_ok=True)
    article_dates = published_article_dates(AUDIO_DIRS)
    base = datetime.now(tz=UTC)

    for rank, idx in enumerate(indices):
        post = posts[idx]
        url = f"{post['url']}"
        title = f"{post['title']}"
        logging.info("Backfilling comments for idx %d: %s", idx, title)
        html = requests.get(url, headers=HEADERS, timeout=45).text
        comments = extract_comments(html)
        if len(comments) < MIN_COMMENTS:
            logging.info("  only %d comments (<%d); skipping", len(comments), MIN_COMMENTS)
            continue
        try:
            article_text = extract_body(html, config.content_selector, url)
        except ValueError:
            logging.warning("  no article text extracted; summarizing from the title alone")
            article_text = title
        article_summary = generate_summary(f"{title}\n\n{article_text}", title)
        briefing = build_briefing(title, comments, article_summary)
        if briefing is None:
            logging.error("  Comment briefing failed for %s; skipping", url)
            continue
        clean_title = re.sub(r"[^A-Za-z0-9 ]+", "", title)
        article_date = article_dates.get(clean_title)
        if article_date is not None:
            pub_date = article_date + COMMENT_OFFSET  # sits right after its article in the feed
        else:
            pub_date = base + rank * STAGGER  # article not found; land as an ordered fresh batch
            logging.warning("  no published article found for %r; using fresh-batch date", clean_title)
        stamp = pub_date.strftime("%Y%m%d-%H%M%S")
        filename = f"{OUTPUT_FOLDER}/{stamp}-ARCHIVE-COMMENTS-{clean_title}.txt"
        block = comment_metadata_block(source_name, title, url, pub_date, article_summary)
        _ = pathlib.Path(filename).write_text(block + "\n\n" + briefing, encoding="utf-8")
        logging.info("  wrote %s (%d comments)", filename, len(comments))
        if idx != indices[-1]:
            time.sleep(PACING_SECONDS)  # let per-minute token limits reset between posts


if __name__ == "__main__":
    main()
