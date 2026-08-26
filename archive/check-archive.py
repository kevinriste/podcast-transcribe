"""Scheduled intake for a blog/archive: one post per day into the pipeline."""

import json
import logging
import pathlib
import re
from datetime import UTC, datetime

import requests
from podcast_shared import generate_summary, send_gotify_notification, store_intake_html
from trafilatura import extract

from comment_briefing import (
    MIN_COMMENTS,
    article_pub_dates,
    build_briefing,
    comment_metadata_block,
    extract_comments,
    load_source_config,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

OUTPUT_FOLDER = "../prepare-text/text-input-raw"
STATE_FILE = "state.json"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def fetch_post_html(url: str) -> str:
    """Fetch a post's HTML with a browser-like User-Agent.

    Returns:
        The response body text.

    """
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    return response.text


def extract_article(html: str, title: str, post_date: str, url: str) -> str:
    """Extract the article body and prefix it with the title and publish date.

    Returns:
        The article text with a title/date header for reader context.

    Raises:
        ValueError: If no content could be extracted from the page.

    """
    content_text = extract(html, include_comments=False, favor_recall=True)
    if not content_text:
        msg = f"Trafilatura returned no content for {url}"
        raise ValueError(msg)
    return f"{title}\nOriginally published: {post_date}\n\n{content_text}"


def write_comment_episode(
    source_name: str,
    title: str,
    url: str,
    html: str,
    content_text: str,
    comment_pd: datetime,
    date_stamp: str,
    clean_title: str,
) -> None:
    """Best-effort: write a comment-highlights episode when a post has enough comments.

    Never raises for a missing briefing; logs and notifies instead so the article
    episode is unaffected.
    """
    comments = extract_comments(html)
    if len(comments) < MIN_COMMENTS:
        logging.info("Only %d comments (<%d); skipping comment episode", len(comments), MIN_COMMENTS)
        return
    article_summary = generate_summary(content_text, title)
    briefing = build_briefing(title, comments, article_summary)
    if briefing is None:
        send_gotify_notification("Comment briefing failed", f"Briefing model failed for {url}")
        return
    comment_filename = f"{OUTPUT_FOLDER}/{date_stamp}-ARCHIVE-COMMENTS-{clean_title}.txt"
    block = comment_metadata_block(source_name, title, url, comment_pd, article_summary)
    _ = pathlib.Path(comment_filename).write_text(block + "\n\n" + briefing, encoding="utf-8")
    logging.info("Wrote comment-highlights episode to %s", comment_filename)


def process_post(
    post: dict[str, str],
    source_name: str,
    now: datetime,
    today_str: str,
    next_index: int,
    state: dict[str, object],
    state_path: pathlib.Path,
) -> None:
    """Fetch one post, write its article episode, update state, and add comments."""
    url = f"{post['url']}"
    title = f"{post['title']}"

    # Extract date from URL if present (pattern: .../YYYY/MM/DD/...)
    date_match = re.search(r"/(\d{4}/\d{2}/\d{2})/", url)
    post_date = date_match.group(1).replace("/", "-") if date_match else "Unknown Date"
    logging.info("Processing post %d: %s (%s) [%s]", next_index, title, url, post_date)

    html_content = fetch_post_html(url)
    content_text = extract_article(html_content, title, post_date, url)

    date_stamp = now.strftime("%Y%m%d-%H%M%S")
    clean_title = re.sub(r"[^A-Za-z0-9 ]+", "", title)
    output_filename = f"{OUTPUT_FOLDER}/{date_stamp}-ARCHIVE-{clean_title}.txt"
    article_pd, comment_pd = article_pub_dates(now)

    metadata_block = "\n".join(
        [
            f"META_FROM: {source_name}",
            f"META_TITLE: {title}",
            f"META_SOURCE_URL: {url}",
            "META_SOURCE_KIND: archive",
            "META_INTAKE_TYPE: archive",
            f"META_PUB_DATE: {article_pd.isoformat()}",
        ],
    )

    logging.info("Writing raw metadata and text to %s", output_filename)
    pathlib.Path(OUTPUT_FOLDER).mkdir(parents=True, exist_ok=True)
    _ = pathlib.Path(output_filename).write_text(metadata_block + "\n\n" + content_text, encoding="utf-8")
    _ = store_intake_html(
        source=source_name,
        episode_id=date_stamp,
        html=html_content,
        url=url,
        intake_type="archive",
    )

    state["last_processed_index"] = next_index
    state["last_processed_date"] = today_str
    _ = state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    write_comment_episode(source_name, title, url, html_content, content_text, comment_pd, date_stamp, clean_title)


def main() -> None:
    """Fetch one archive post per day and write a raw text file for the pipeline."""
    source_name, posts_file = load_source_config()
    state_path = pathlib.Path(STATE_FILE)
    posts_path = pathlib.Path(posts_file)

    if not posts_path.exists():
        logging.error("Posts file %s not found", posts_file)
        return

    posts: list[dict[str, str]] = json.loads(posts_path.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]

    state: dict[str, object] = {"last_processed_index": -1, "last_processed_date": ""}
    if state_path.exists() and state_path.stat().st_size > 0:
        state = json.loads(state_path.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]

    now = datetime.now(tz=UTC)
    today_str = now.strftime("%Y-%m-%d")

    if state.get("last_processed_date") == today_str:
        logging.info("Already processed a post today (%s). Skipping.", today_str)
        return

    last_index = state.get("last_processed_index", -1)
    next_index = (last_index if isinstance(last_index, int) else -1) + 1

    if next_index >= len(posts):
        logging.info("All posts from %s have been processed.", posts_file)
        return

    try:
        process_post(posts[next_index], source_name, now, today_str, next_index, state, state_path)
    except Exception:
        url = posts[next_index].get("url", "?")
        logging.exception("Error processing archive post %s", url)
        send_gotify_notification("Archive intake error", f"Error processing post: {url}")


if __name__ == "__main__":
    main()
