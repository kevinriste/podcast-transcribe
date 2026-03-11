import json
import logging
import os
import pathlib
import re
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

import feedparser  # pyright: ignore[reportMissingTypeStubs]
import msgspec
import playwright.sync_api
import requests
import urllib3
from bs4 import BeautifulSoup
from dateutil import parser
from feedparser import FeedParserDict  # pyright: ignore[reportMissingTypeStubs]
from playwright.sync_api import sync_playwright
from pyrsistent import PMap, PVector, freeze, pvector, thaw
from requests.adapters import HTTPAdapter
from trafilatura import bare_extraction, extract
from urllib3.util.retry import Retry
from waybackpy import WaybackMachineCDXServerAPI
from waybackpy.cdx_snapshot import CDXSnapshot
from waybackpy.exceptions import MaximumSaveRetriesExceeded, TooManyRequestsError

local_scraper_url: Final = "http://localhost:3002/fetch"
bill_simmons_feed: Final = "https://feeds.megaphone.fm/the-bill-simmons-podcast"
retry_strategy: Final = Retry(
    total=0,
    connect=0,
    read=0,
    redirect=0,
    status=0,
)

adapter: Final = HTTPAdapter(max_retries=retry_strategy)

enable_diagnosis: Final = False

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

output_folder: Final = "../prepare-text/text-input-raw"
feeds_file: Final = "feeds.txt"
wayback_feeds: Final = (
    "https://www.nytimes.com/svc/collections/v1/publish/www.nytimes.com/column/ross-douthat/rss.xml",
    "https://www.nytimes.com/svc/collections/v1/publish/www.nytimes.com/column/ezra-klein/rss.xml",
    "https://www.nytimes.com/svc/collections/v1/publish/www.nytimes.com/column/thomas-l-friedman/rss.xml",
)

EXPECTED_TIMESTAMP_LENGTH: Final = 9


def load_feeds() -> tuple[str, ...]:
    with pathlib.Path(feeds_file).open(encoding="utf-8") as feeds_fh:
        return tuple(stripped for line in feeds_fh if (stripped := line.rstrip()))


def send_gotify_notification(title: str, message: str, priority: int = 6) -> None:
    gotify_server: Final = os.environ.get("GOTIFY_SERVER")
    gotify_token: Final = os.environ.get("GOTIFY_TOKEN")
    if not gotify_server or not gotify_token:
        logging.warning("Gotify env vars not set; skipping notification.")
        return
    gotify_url: Final = f"{gotify_server}/message?token={gotify_token}"
    # Mutable: requests.post requires dict
    data: Final[dict[str, str | int]] = {"title": title, "message": message, "priority": priority}
    try:
        _ = requests.post(gotify_url, data=data, timeout=30)
    except requests.RequestException:
        logging.exception("Failed to send Gotify notification")


def get_entry_link(entry: FeedParserDict) -> str:
    link: Final = getattr(entry, "link", None)
    if link:
        return str(link)  # pyright: ignore[reportAny]
    for candidate in entry.get("links") or []:  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        href = candidate.get("href")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        if href:
            return str(href)  # pyright: ignore[reportUnknownArgumentType]
    return ""


def build_metadata_block(feed_title: str, meta_title: str, original_url: str) -> str:
    """Build the metadata block string for an RSS entry.

    Returns
    -------
    - Newline-joined metadata block with META_ prefixed lines.

    """
    return "\n".join(
        (
            f"META_FROM: {feed_title}",
            f"META_TITLE: {meta_title}",
            f"META_SOURCE_URL: {original_url}",
            "META_SOURCE_KIND: rss",
            "META_INTAKE_TYPE: rss",
        ),
    )


def fetch_and_process_html(
    url: str,
    *,
    request_body: Mapping[str, str] | None = None,
) -> str | None:
    """Fetch HTML via Playwright and extract article text, verifying NYT authorship phrases.

    Parameters
    ----------
    - url: The URL to fetch the HTML content from.
    - request_body: Optional mapping of data for making a GET request with query parameter.

    Returns
    -------
    - content_text: The processed HTML content as text, or None if the check fails.

    """
    check_phrases: Final = (
        "has been an Opinion columnist",
        "From Beirut to Jerusalem",
        "joined Opinion in 2021",
    )

    logging.info("Fetching %s", url)

    html_content: str | None = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            if request_body:
                logging.info("Making GET request to %s with url query parameter", url)
                _ = page.goto(
                    f"{url}?url={request_body['url']}",
                    wait_until="networkidle",
                    timeout=180000,
                )
            else:
                logging.info("Making GET request to %s", url)
                _ = page.goto(url, wait_until="networkidle", timeout=180000)

            html_content = page.content()

        except Exception:
            logging.exception("Error occurred while fetching %s", url)
            html_content = None

        finally:
            browser.close()

    if html_content is None:
        return None

    html_content_parsed_for_title: Final = bare_extraction(html_content, with_metadata=True)
    webpage_text: Final = extract(html_content, include_comments=False, favor_recall=True)

    title_text: str = ""
    if html_content_parsed_for_title is not None and hasattr(html_content_parsed_for_title, "as_dict"):
        title_text = str(html_content_parsed_for_title.as_dict().get("title") or "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportAttributeAccessIssue]
    content_text: Final = title_text + ".\n" + "\n" + (webpage_text or "")

    if all(phrase not in content_text for phrase in check_phrases):
        logging.error(
            "Kevin's or Wayback Machine's version of %s did not include the full article.",
            url,
        )
        return None

    return content_text


def snapshot_to_dict(snapshot: CDXSnapshot) -> PMap[str, str]:
    """Convert a CDXSnapshot to an immutable PMap.

    Returns
    -------
    - PMap with snapshot attribute names as keys and string values.

    """
    result: dict[str, str] = {}  # Mutable: built incrementally then frozen
    for attr in dir(snapshot):
        if not attr.startswith("_") and not callable(
            getattr(snapshot, attr),  # pyright: ignore[reportAny]
        ):
            value = getattr(snapshot, attr)  # pyright: ignore[reportAny]
            if isinstance(value, datetime):
                value = value.isoformat()
            result[attr] = str(value)
    return freeze(result)


def filter_calendar_captures(
    items: Sequence[Sequence[int]],
    collections: Sequence[Sequence[int]],
    year: int,
    original_url: str,
    collection_name: str,
) -> tuple[str, ...]:
    """Filter Wayback Machine calendar captures and return matching archive URLs.

    Returns
    -------
    - Tuple of Wayback Machine archive URLs that match the collection name.

    """
    filtered: list[str] = []  # Mutable: accumulation then converted to tuple
    for item in items:
        timestamp, _status_code, collection_index = item
        if collection_index < len(collections):
            collection = collections[collection_index]
            if collection_name in str(collection):
                timestamp_str = str(timestamp)
                if len(timestamp_str) == EXPECTED_TIMESTAMP_LENGTH:
                    timestamp_str = timestamp_str.zfill(10)
                formatted_timestamp = str(year) + timestamp_str
                final_url = f"https://web.archive.org/web/{formatted_timestamp}/{original_url}"
                filtered.append(final_url)
    return tuple(filtered)


def find_most_recent_guid_index(
    entry_guids: Sequence[str],
    most_recent_guid: str | None,
) -> int | None:
    """Return the index of the most recently processed GUID, or None if not found.

    Returns
    -------
    - Index of most_recent_guid in entry_guids, or None if not found.

    """
    try:
        return entry_guids.index(
            most_recent_guid,
        )
    except ValueError:
        return None


def main() -> None:  # noqa: PLR0915
    feeds: Final = load_feeds()
    for feed in feeds:  # noqa: PLR1702
        try:
            parsed_feed = feedparser.parse(feed)  # pyright: ignore[reportUnknownMemberType]

            now = datetime.now(tz=UTC)
            date_string = now.strftime("%Y%m%d-%H%M%S")
            clean_feed_name = re.sub(r"[^A-Za-z0-9 ]+", "", feed)
            diagnosis_dir = "./diagnosis"

            json_filename = f"{diagnosis_dir}/{clean_feed_name}-{date_string}-json.json"
            json_version_of_parsed_feed = msgspec.json.encode(parsed_feed)

            # Sometimes The Money Illusion returns an old version of its feed for some reason.
            # This prevents that from causing processing of old items.
            feed_updated_raw = getattr(parsed_feed.feed, "updated", None)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            if feed_updated_raw:
                parsed_feed_updated_date = parser.parse(str(feed_updated_raw)).replace(  # pyright: ignore[reportAny]
                    tzinfo=None,
                )
                max_timedelta_since_feed_last_updated = timedelta(days=7)
                timedelta_since_feed_last_updated = now.replace(tzinfo=None) - parsed_feed_updated_date
                if timedelta_since_feed_last_updated > max_timedelta_since_feed_last_updated:
                    error_threshold_timedelta_since_feed_last_updated = timedelta(days=30)
                    if timedelta_since_feed_last_updated > error_threshold_timedelta_since_feed_last_updated:
                        logging.error(
                            "Error: %s-%s was more than 30 days old",
                            clean_feed_name,
                            date_string,
                        )

                        _ = pathlib.Path(json_filename).write_bytes(json_version_of_parsed_feed)
                    else:
                        logging.info(
                            "%s-%s was more than 7 days old",
                            clean_feed_name,
                            date_string,
                        )

                    continue

            if enable_diagnosis:
                _ = pathlib.Path(json_filename).write_bytes(json_version_of_parsed_feed)

            feed_title_raw: str = str(parsed_feed.feed.title)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportAttributeAccessIssue]
            feed_title_for_filename = re.sub(r"[^A-Za-z0-9 ]+", "", feed_title_raw)
            feed_prefix_for_filename = feed_title_for_filename + "- " if feed_title_for_filename else ""
            guid_dir = "./feed-guids"
            guid_filename = f"{guid_dir}/{feed_title_for_filename}.txt"
            try:
                most_recent_guid: str | None = pathlib.Path(guid_filename).read_text(encoding="utf-8")
                if enable_diagnosis:
                    _ = shutil.copy(
                        guid_filename,
                        f"{diagnosis_dir}/{clean_feed_name}-{date_string}-guids-before.txt",
                    )
            except FileNotFoundError:
                most_recent_guid = None
            parsed_feed_entry_guids: tuple[str, ...] = tuple(
                str(parsed_feed_entry.id)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                for parsed_feed_entry in parsed_feed.entries  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            )
            if most_recent_guid is None and feed == bill_simmons_feed:
                if len(parsed_feed_entry_guids) >= 5:  # noqa: PLR2004
                    most_recent_guid = parsed_feed_entry_guids[4]
                elif len(parsed_feed_entry_guids) > 0:
                    most_recent_guid = parsed_feed_entry_guids[-1]

            most_recent_guid_index = find_most_recent_guid_index(parsed_feed_entry_guids, most_recent_guid)

            feed_entries_before_most_recently_processed = tuple(
                parsed_feed.entries[:most_recent_guid_index][::-1],  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            )

            if len(feed_entries_before_most_recently_processed) > 0:
                logging.info(
                    "Processing %s entries for %s",
                    len(feed_entries_before_most_recently_processed),
                    feed,
                )

            for parsed_feed_entry in feed_entries_before_most_recently_processed:
                raw_date = parser.parse(str(parsed_feed_entry.published))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                date_stamp = raw_date.strftime("%Y%m%d-%H%M%S-%f")[0:15]
                entry_title_raw: str = str(parsed_feed_entry.title)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                entry_title_for_filename = re.sub(r"[^A-Za-z0-9 ]+", "", entry_title_raw)
                output_filename = (
                    f"{output_folder}/{date_stamp}-{feed_prefix_for_filename}{entry_title_for_filename}.txt"
                )
                meta_title = entry_title_raw
                original_url = get_entry_link(parsed_feed_entry)

                content_text: str | None
                send_error_with_gotify: bool = False
                two_most_recent_snapshots: PVector[CDXSnapshot] = pvector()

                if feed == bill_simmons_feed:
                    entry_description_raw = str(
                        parsed_feed_entry.get("summary")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        or parsed_feed_entry.get("description")  # pyright: ignore[reportUnknownMemberType]
                        or "",
                    )
                    content_text = entry_description_raw
                elif feed in wayback_feeds:
                    try:
                        max_timedelta_since_article_added_to_feed = timedelta(days=2)
                        timedelta_since_article_added_to_feed = now.replace(tzinfo=None) - raw_date.replace(
                            tzinfo=None,
                        )
                        if timedelta_since_article_added_to_feed > max_timedelta_since_article_added_to_feed:
                            send_error_with_gotify = True
                        content_text = fetch_and_process_html(
                            url=local_scraper_url,
                            request_body={"url": original_url},
                        )

                        if content_text is None:
                            user_agent = "Mozilla/5.0 (Windows NT 5.1; rv:40.0) Gecko/20100101 Firefox/40.0"
                            cdx_api = WaybackMachineCDXServerAPI(original_url, user_agent)
                            snapshots_gen = list(cdx_api.snapshots())
                            sorted_snapshots = tuple(
                                sorted(
                                    snapshots_gen,
                                    key=lambda x: x.datetime_timestamp,
                                    reverse=True,
                                ),
                            )
                            two_most_recent_snapshots = pvector(sorted_snapshots[:2])

                            if send_error_with_gotify or enable_diagnosis:
                                snapshots_pv = pvector(snapshot_to_dict(snapshot) for snapshot in snapshots_gen)
                                snapshots_json_filename = (
                                    f"{diagnosis_dir}/{clean_feed_name}-{date_string}-snapshots-json.json"
                                )
                                # Mutable: json.dump requires list/dict
                                snapshots_list_for_json = thaw(snapshots_pv)
                                with pathlib.Path(snapshots_json_filename).open(
                                    "w",
                                    encoding="utf-8",
                                ) as json_file:
                                    json.dump(snapshots_list_for_json, json_file, indent=2)

                            this_year = datetime.now(tz=UTC).year
                            calendar_captures_api_url_this_year = (
                                f"https://web.archive.org/__wb/calendarcaptures/2?url={original_url}&date={this_year}"
                            )
                            this_year_response = requests.get(
                                calendar_captures_api_url_this_year,
                                timeout=30,
                            )
                            this_year_data: PMap[str, PVector[PVector[int]]] = freeze(this_year_response.json())  # pyright: ignore[reportAny, reportUnknownVariableType]

                            last_year = this_year - 1
                            calendar_captures_api_url_last_year = (
                                f"https://web.archive.org/__wb/calendarcaptures/2?url={original_url}&date={last_year}"
                            )
                            last_year_response = requests.get(
                                calendar_captures_api_url_last_year,
                                timeout=30,
                            )
                            last_year_data: PMap[str, PVector[PVector[int]]] = freeze(last_year_response.json())  # pyright: ignore[reportAny, reportUnknownVariableType]

                            collection_name = "global.nytimes.com"

                            # Thaw collections/items for filter_calendar_captures which accepts Sequence
                            this_year_collections = thaw(this_year_data.get("colls", pvector()))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                            this_year_items = thaw(this_year_data.get("items", pvector()))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]

                            last_year_collections = thaw(last_year_data.get("colls", pvector()))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                            last_year_items = thaw(last_year_data.get("items", pvector()))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]

                            this_year_captures = filter_calendar_captures(
                                this_year_items,
                                this_year_collections,
                                this_year,
                                original_url,
                                collection_name,
                            )
                            last_year_captures = filter_calendar_captures(
                                last_year_items,
                                last_year_collections,
                                this_year,
                                original_url,
                                collection_name,
                            )
                            filtered_captures = this_year_captures + last_year_captures

                            for filtered_capture in filtered_captures:
                                content_text = fetch_and_process_html(url=filtered_capture)
                                if content_text is not None:
                                    break

                        if content_text is None:
                            for snapshot in two_most_recent_snapshots:
                                content_text = fetch_and_process_html(
                                    url=snapshot.archive_url,
                                )
                                if content_text is not None:
                                    break

                        # If we failed to get the real article, stop processing this feed
                        # altogether so the article doesn't get skipped next time.
                        if content_text is None:
                            send_gotify_notification(
                                "Incomplete NYT article — all fallbacks failed",
                                f"URL: {original_url}",
                                priority=2,
                            )
                            break
                    except (
                        requests.HTTPError,
                        requests.ConnectionError,
                        requests.exceptions.RetryError,
                        playwright.sync_api.TimeoutError,
                        urllib3.exceptions.NewConnectionError,
                        urllib3.exceptions.MaxRetryError,
                        TooManyRequestsError,
                        MaximumSaveRetriesExceeded,
                    ):
                        logging.exception("Error occurred")
                        logging.info("%s URL caused the issue.", original_url)
                        debug_message = "RSS URL catastrophic error"
                        debug_output = original_url

                        if send_error_with_gotify:
                            send_gotify_notification(
                                debug_message,
                                debug_output,
                                priority=6,
                            )
                        break
                else:
                    soup = BeautifulSoup(
                        str(parsed_feed_entry.content[0].value),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        "html.parser",
                    )
                    content_text = soup.get_text()
                metadata_block = build_metadata_block(feed_title_raw, meta_title, original_url)
                logging.info("Writing raw metadata and text to text input")
                _ = pathlib.Path(output_filename).write_text(metadata_block + "\n\n" + content_text, encoding="utf-8")
                if not pathlib.Path(guid_dir).exists():
                    pathlib.Path(guid_dir).mkdir(parents=True)
                _ = pathlib.Path(guid_filename).write_text(
                    str(parsed_feed_entry.id),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                    encoding="utf-8",
                )
                date_string = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
                if enable_diagnosis:
                    _ = shutil.copy(
                        guid_filename,
                        f"{diagnosis_dir}/{clean_feed_name}-{date_string}-guids-after.txt",
                    )
        except Exception:
            logging.exception("Error occurred")
            logging.info("%s URL caused the issue.", feed)


if __name__ == "__main__":
    main()
