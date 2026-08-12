"""One-off: backfill comment-highlights episodes for specific archive post indices.

Does NOT touch state.json (targets already-published posts). Writes comment
episode files into the pipeline with staggered pub dates so they land as an
ordered fresh batch. Run prepare_text.py then text_to_speech.py afterwards.

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
from podcast_shared import generate_summary
from trafilatura import extract

from comment_briefing import (
    MIN_COMMENTS,
    build_briefing,
    comment_metadata_block,
    extract_comments,
    load_source_config,
)

OUTPUT_FOLDER = "../prepare-text/text-input-raw"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
STAGGER = timedelta(minutes=2)
PACING_SECONDS = 45

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main() -> None:
    """Generate comment episodes for the post indices given on the command line."""
    indices = sorted(int(a) for a in sys.argv[1:])
    if not indices:
        logging.error("Provide post indices, e.g. backfill_comments.py 6 17 18 20 41")
        return
    source_name, posts_file = load_source_config()
    posts: list[dict[str, str]] = json.loads(  # pyright: ignore[reportAny]
        pathlib.Path(posts_file).read_text(encoding="utf-8"),
    )
    pathlib.Path(OUTPUT_FOLDER).mkdir(parents=True, exist_ok=True)
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
        article_text = extract(html, include_comments=False, favor_recall=True) or title
        article_summary = generate_summary(f"{title}\n\n{article_text}", title)
        briefing = build_briefing(title, comments, article_summary)
        if briefing is None:
            logging.error("  Comment briefing failed for %s; skipping", url)
            continue
        pub_date = base + rank * STAGGER
        clean_title = re.sub(r"[^A-Za-z0-9 ]+", "", title)
        stamp = pub_date.strftime("%Y%m%d-%H%M%S")
        filename = f"{OUTPUT_FOLDER}/{stamp}-ARCHIVE-COMMENTS-{clean_title}.txt"
        block = comment_metadata_block(source_name, title, url, pub_date, article_summary)
        _ = pathlib.Path(filename).write_text(block + "\n\n" + briefing, encoding="utf-8")
        logging.info("  wrote %s (%d comments)", filename, len(comments))
        if idx != indices[-1]:
            time.sleep(PACING_SECONDS)  # let per-minute token limits reset between posts


if __name__ == "__main__":
    main()
