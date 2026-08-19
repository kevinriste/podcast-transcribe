"""Poll RSS feeds for new entries and write raw text files for the pipeline."""

import logging
import pathlib
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import feedparser  # pyright: ignore[reportMissingTypeStubs]
import msgspec
import yaml
from bs4 import BeautifulSoup
from dateutil import parser
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from podcast_shared import send_gotify_notification
from trafilatura import bare_extraction, extract

enable_diagnosis = False

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

output_folder = "../prepare-text/text-input-raw"
feeds_file = "feeds.yaml"
poll_state_dir = "./feed-poll-state"
DEFAULT_SCRAPER_URL = "http://localhost:3002/fetch"


@dataclass(slots=True)
class FeedConfig:
    """Per-feed handling config loaded from feeds.yaml.

    ``mode`` selects how article text is obtained:
      - ``content`` (default): BeautifulSoup on the entry's ``content`` value.
      - ``description``: use the entry summary/description (e.g. podcast feeds).
      - ``full_scraper``: fetch the full article via the authenticated scraper,
        verified by ``check_phrases``.
    ``seed_backfill`` seeds the most-recent GUID N entries back on the first run
    (0 disables), useful to avoid re-reading a long backlog on a new feed.
    ``poll_interval_hours`` throttles how often the feed is fetched (0 = every
    run); useful for feeds behind bot protection that flakes under frequent polls.
    """

    url: str
    mode: str = "content"
    check_phrases: tuple[str, ...] = ()
    seed_backfill: int = 0
    poll_interval_hours: float = 0.0


class FeedEntry(TypedDict, total=False):
    """A single entry under feeds: in feeds.yaml."""

    url: str
    mode: str
    check_phrases: list[str]
    seed_backfill: int
    poll_interval_hours: float


class FeedsConfig(TypedDict, total=False):
    """The feeds.yaml document."""

    scraper_url: str
    feeds: list[FeedEntry]


def load_feeds() -> tuple[str, list[FeedConfig]]:
    """Load the scraper URL and per-feed configs from feeds.yaml.

    Returns:
        (scraper_url, feeds). Each feed entry is a mapping with
        url/mode/check_phrases/seed_backfill (mode defaults to ``content``).

    """
    config: FeedsConfig = yaml.safe_load(pathlib.Path(feeds_file).read_text(encoding="utf-8")) or {}
    scraper_url = config.get("scraper_url", DEFAULT_SCRAPER_URL)
    feeds = [
        FeedConfig(
            url=entry.get("url", ""),
            mode=entry.get("mode", "content"),
            check_phrases=tuple(entry.get("check_phrases", [])),
            seed_backfill=entry.get("seed_backfill", 0),
            poll_interval_hours=entry.get("poll_interval_hours", 0.0),
        )
        for entry in config.get("feeds", [])
    ]
    return scraper_url, feeds


def get_entry_link(entry: object) -> str:
    """Extract the best URL from a feedparser entry.

    Returns:
        The entry URL, or empty string if none found.

    """
    link: str | None = getattr(entry, "link", None)
    if link:
        return link
    links: list[dict[str, str]] = getattr(entry, "links", [])
    for candidate in links:
        href = candidate.get("href")
        if href:
            return href
    return ""


def poll_is_due(clean_feed_name: str, interval_hours: float, now: datetime) -> bool:
    """Whether a throttled feed is due for another poll.

    Reads the last-poll timestamp recorded for ``clean_feed_name``. Feeds with
    ``interval_hours <= 0`` are always due; a missing or unreadable timestamp
    also counts as due (fail open, so a fresh feed is never starved).

    Returns:
        True if at least ``interval_hours`` have elapsed since the last poll.

    """
    if interval_hours <= 0:
        return True
    stamp_path = pathlib.Path(poll_state_dir) / f"{clean_feed_name}.txt"
    try:
        last = datetime.fromisoformat(stamp_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (now - last) >= timedelta(hours=interval_hours)


def record_poll(clean_feed_name: str, now: datetime) -> None:
    """Record ``now`` as the last-poll time for ``clean_feed_name``."""
    stamp_dir = pathlib.Path(poll_state_dir)
    stamp_dir.mkdir(parents=True, exist_ok=True)
    _ = (stamp_dir / f"{clean_feed_name}.txt").write_text(now.isoformat(), encoding="utf-8")


@dataclass(slots=True)
class ScrapeResult:
    """Outcome of a full-article scrape attempt.

    Exactly one field is set: ``content`` holds the article text on success,
    ``error`` holds a human-readable reason on failure. The reason is written to
    the logs and forwarded to Gotify so failures are diagnosable without shelling
    into the container.
    """

    content: str | None = None
    error: str | None = None


def _body_snippet(html_content: str, limit: int = 300) -> str:
    """Collapse scraper HTML to a short single-line snippet for error messages.

    Returns:
        Whitespace-collapsed text truncated to ``limit`` characters. Surfaces the
        scraper container's own error payload (e.g. a JSON error the browser
        rendered) when a fetch fails.

    """
    return " ".join(html_content.split())[:limit]


def fetch_full_article(original_url: str, scraper_url: str, check_phrases: tuple[str, ...]) -> ScrapeResult:
    """Fetch a full article via the local scraper and verify completeness.

    Uses Playwright to navigate to the local scraper service, then extracts
    article text with trafilatura. When ``check_phrases`` is non-empty, verifies
    the full article was captured by requiring at least one phrase to be present.

    Returns:
        A :class:`ScrapeResult` carrying the article text on success, or a
        specific ``error`` reason for each distinct failure mode (unreachable
        scraper, timeout, non-2xx status, empty extraction, failed verification).

    """
    logging.info("Fetching full article via scraper: %s", original_url)

    status: int | None = None
    html_content: str | None = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            try:
                response = page.goto(
                    f"{scraper_url}?url={original_url}",
                    wait_until="networkidle",
                    timeout=180000,
                )
                status = response.status if response is not None else None
                html_content = page.content()
            except PlaywrightTimeoutError as exc:
                logging.exception("Timeout fetching %s via scraper", original_url)
                return ScrapeResult(error=f"scraper timed out after 180s: {exc}")
            except PlaywrightError as exc:
                logging.exception("Error fetching %s via scraper", original_url)
                return ScrapeResult(error=f"scraper navigation failed: {exc}")
            finally:
                browser.close()
    except PlaywrightError as exc:
        logging.exception("Playwright failed to start for %s", original_url)
        return ScrapeResult(error=f"Playwright failed to start: {exc}")

    logging.info("Scraper returned HTTP %s for %s", status, original_url)

    if status is not None and status >= 400:
        return ScrapeResult(error=f"scraper returned HTTP {status}; body: {_body_snippet(html_content)}")

    trafilatura_result: object | None = bare_extraction(html_content, with_metadata=True)
    webpage_text: str = str(extract(html_content, include_comments=False, favor_recall=True) or "")

    if not webpage_text.strip():
        return ScrapeResult(
            error=f"scraper page had no extractable article text (HTTP {status}); body: {_body_snippet(html_content)}",
        )

    title = ""
    if trafilatura_result is not None:
        as_dict_fn = getattr(trafilatura_result, "as_dict", None)
        raw: object = as_dict_fn() if callable(as_dict_fn) else None
        if isinstance(raw, dict):
            title = str(raw.get("title") or "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]

    content_text = title + ".\n" + "\n" + webpage_text

    if check_phrases and all(phrase not in content_text for phrase in check_phrases):
        return ScrapeResult(
            error=(
                f"article failed verification (HTTP {status}): none of {list(check_phrases)} found "
                f"in {len(content_text)} chars — likely paywalled or incomplete"
            ),
        )

    return ScrapeResult(content=content_text)


def main() -> None:
    """Check all RSS feeds for new entries and write raw text files."""
    scraper_url, feeds = load_feeds()
    for feed in feeds:
        try:
            now = datetime.now(tz=UTC)
            clean_feed_name = re.sub(r"[^A-Za-z0-9 ]+", "", feed.url)

            # Throttle feeds behind flaky bot protection (e.g. NYT/DataDome):
            # skip until the configured interval has elapsed since the last poll.
            if not poll_is_due(clean_feed_name, feed.poll_interval_hours, now):
                logging.info(
                    "Skipping %s; polled within the last %sh",
                    feed.url,
                    feed.poll_interval_hours,
                )
                continue
            record_poll(clean_feed_name, now)

            parsed_feed: object = feedparser.parse(feed.url)  # pyright: ignore[reportUnknownMemberType]
            feed_meta: object = getattr(parsed_feed, "feed", None)
            feed_entries: list[object] = list(getattr(parsed_feed, "entries", []))
            if bool(getattr(parsed_feed, "bozo", False)):
                bozo_exc: object = getattr(parsed_feed, "bozo_exception", None)
                logging.warning("Feed %s has parsing issues: %s", feed.url, bozo_exc)
                # A malformed response with no usable entries is almost always a
                # transient bot challenge (HTML served instead of RSS). Notify
                # and move on; the next poll recovers without losing entries.
                if not feed_entries:
                    logging.error("Feed %s returned no parseable entries; will retry next poll", feed.url)
                    bozo_message = f"{feed.url}\n\nReturned invalid content (likely a bot challenge): {bozo_exc}\nWill retry next poll."
                    send_gotify_notification("RSS feed unavailable", bozo_message)
                    continue

            # Prepare shared variables for file logging
            date_string = now.strftime("%Y%m%d-%H%M%S")
            diagnosis_dir = "./diagnosis"
            json_filename = f"{diagnosis_dir}/{clean_feed_name}-{date_string}-json.json"

            # Some feeds occasionally serve a stale/cached copy; skip when the
            # feed's own "updated" timestamp is too old to prevent reprocessing.
            feed_updated_raw: str | None = getattr(feed_meta, "updated", None)
            if feed_updated_raw:
                parsed_feed_updated_date = parser.parse(feed_updated_raw)
                if parsed_feed_updated_date.tzinfo is None:
                    parsed_feed_updated_date = parsed_feed_updated_date.replace(tzinfo=UTC)
                max_timedelta_since_feed_last_updated = timedelta(days=7)
                timedelta_since_feed_last_updated = now - parsed_feed_updated_date
                if timedelta_since_feed_last_updated > max_timedelta_since_feed_last_updated:
                    error_threshold_timedelta_since_feed_last_updated = timedelta(days=30)
                    if timedelta_since_feed_last_updated > error_threshold_timedelta_since_feed_last_updated:
                        logging.error(
                            "Error: %s-%s was more than 30 days old",
                            clean_feed_name,
                            date_string,
                        )

                        _ = pathlib.Path(json_filename).write_bytes(msgspec.json.encode(parsed_feed))
                    else:
                        logging.info(
                            "%s-%s was more than 7 days old",
                            clean_feed_name,
                            date_string,
                        )

                    # Go to the next feed and stop processing this one
                    continue

            if enable_diagnosis:
                _ = pathlib.Path(json_filename).write_bytes(msgspec.json.encode(parsed_feed))

            feed_title_raw: str = str(getattr(feed_meta, "title", ""))
            feed_title_for_filename = re.sub(r"[^A-Za-z0-9 ]+", "", feed_title_raw)
            feed_prefix_for_filename = feed_title_for_filename + "- " if feed_title_for_filename else ""
            guid_dir = "./feed-guids"
            guid_filename = f"{guid_dir}/{feed_title_for_filename}.txt"
            try:
                most_recent_guid = pathlib.Path(guid_filename).read_text(encoding="utf-8")
                if enable_diagnosis:
                    _ = shutil.copy2(
                        guid_filename,
                        f"{diagnosis_dir}/{clean_feed_name}-{date_string}-guids-before.txt",
                    )
            except FileNotFoundError:
                most_recent_guid = None
            parsed_feed_entry_guids: list[str] = [str(getattr(e, "id", "")) for e in feed_entries]
            if most_recent_guid is None and feed.seed_backfill > 0:
                if len(parsed_feed_entry_guids) >= feed.seed_backfill:
                    most_recent_guid = parsed_feed_entry_guids[feed.seed_backfill - 1]
                elif len(parsed_feed_entry_guids) > 0:
                    most_recent_guid = parsed_feed_entry_guids[-1]
            if most_recent_guid is not None:
                try:
                    most_recent_guid_index = parsed_feed_entry_guids.index(most_recent_guid)
                except ValueError:
                    most_recent_guid_index = None
            else:
                most_recent_guid_index = None

            # Get list of RSS items that haven't been processed, process them from oldest to newest
            feed_entries_before_most_recently_processed = feed_entries[:most_recent_guid_index][::-1]

            if len(feed_entries_before_most_recently_processed) > 0:
                logging.info(
                    "Processing %d entries for %s",
                    len(feed_entries_before_most_recently_processed),
                    feed.url,
                )

            for parsed_feed_entry in feed_entries_before_most_recently_processed:
                published: str = str(getattr(parsed_feed_entry, "published", ""))
                raw_date = parser.parse(published)
                date_stamp = raw_date.strftime("%Y%m%d-%H%M%S-%f")[0:15]
                entry_title_raw: str = str(getattr(parsed_feed_entry, "title", ""))
                entry_title_for_filename = re.sub(r"[^A-Za-z0-9 ]+", "", entry_title_raw)
                output_filename = (
                    f"{output_folder}/{date_stamp}-{feed_prefix_for_filename}{entry_title_for_filename}.txt"
                )
                meta_title = entry_title_raw
                original_url = get_entry_link(parsed_feed_entry)

                content_text: str
                if feed.mode == "description":
                    summary: str = str(getattr(parsed_feed_entry, "summary", "") or "")
                    description: str = str(getattr(parsed_feed_entry, "description", "") or "")
                    content_text = summary or description
                elif feed.mode == "full_scraper":
                    scrape = fetch_full_article(original_url, scraper_url, feed.check_phrases)
                    if scrape.content is None:
                        reason = scrape.error or "unknown error"
                        logging.error("Full-article fetch failed for %s: %s", original_url, reason)
                        send_gotify_notification(
                            "RSS full-article fetch failed",
                            f"{feed_title_raw}: {entry_title_raw}\n{original_url}\n\n{reason}",
                        )
                        break
                    content_text = scrape.content
                else:
                    content_list: list[object] = getattr(parsed_feed_entry, "content", [])
                    if content_list:
                        content_value: str = str(getattr(content_list[0], "value", ""))
                        soup = BeautifulSoup(content_value, "html.parser")
                        content_text = soup.get_text()
                    else:
                        content_text = str(getattr(parsed_feed_entry, "summary", "") or "")
                metadata_block = "\n".join(
                    [
                        f"META_FROM: {feed_title_raw}",
                        f"META_TITLE: {meta_title}",
                        f"META_SOURCE_URL: {original_url}",
                        "META_SOURCE_KIND: rss",
                        "META_INTAKE_TYPE: rss",
                    ],
                )
                logging.info("Writing raw metadata and text to text input")
                _ = pathlib.Path(output_filename).write_text(metadata_block + "\n\n" + content_text, encoding="utf-8")
                pathlib.Path(guid_dir).mkdir(parents=True, exist_ok=True)
                entry_id: str = str(getattr(parsed_feed_entry, "id", ""))
                _ = pathlib.Path(guid_filename).write_text(entry_id, encoding="utf-8")
                # Copy new version of guids txt file
                date_string = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
                if enable_diagnosis:
                    _ = shutil.copy2(
                        guid_filename,
                        f"{diagnosis_dir}/{clean_feed_name}-{date_string}-guids-after.txt",
                    )
        except Exception:
            logging.exception("Error processing feed %s", feed)
            send_gotify_notification(
                "RSS feed processing error",
                f"Error processing feed: {feed}",
            )


if __name__ == "__main__":
    main()
