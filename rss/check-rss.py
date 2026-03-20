"""Poll RSS feeds for new entries and write raw text files for the pipeline."""

import logging
import pathlib
import re
import shutil
from datetime import UTC, datetime, timedelta

import feedparser
import msgspec
from bs4 import BeautifulSoup
from dateutil import parser

bill_simmons_feed = "https://feeds.megaphone.fm/the-bill-simmons-podcast"

enable_diagnosis = False

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

output_folder = "../prepare-text/text-input-raw"
feeds_file = "feeds.txt"


def get_entry_link(entry: object) -> str:
    """Extract the best URL from a feedparser entry.

    Returns:
        The entry URL, or empty string if none found.

    """
    link = getattr(entry, "link", None)
    if link:
        return link
    for candidate in entry.get("links") or []:
        href = candidate.get("href")
        if href:
            return href
    return ""


def main() -> None:
    """Check all RSS feeds for new entries and write raw text files."""
    feeds = pathlib.Path(feeds_file).read_text(encoding="utf-8").splitlines()
    for feed in feeds:
        try:
            parsed_feed = feedparser.parse(feed)
            if parsed_feed.bozo:
                logging.warning("Feed %s has parsing issues: %s", feed, parsed_feed.bozo_exception)

            # Prepare shared variables for file logging
            now = datetime.now(tz=UTC)
            date_string = now.strftime("%Y%m%d-%H%M%S")
            clean_feed_name = re.sub(r"[^A-Za-z0-9 ]+", "", feed)
            diagnosis_dir = "./diagnosis"

            # Save the serializable feed data to a JSON file
            json_filename = f"{diagnosis_dir}/{clean_feed_name}-{date_string}-json.json"
            json_version_of_parsed_feed = msgspec.json.encode(parsed_feed)

            # Sometimes The Money Illusion returns an old version of its feed.
            # This prevents processing of old items.
            feed_updated_raw = getattr(parsed_feed.feed, "updated", None)
            if feed_updated_raw:
                parsed_feed_updated_date = parser.parse(feed_updated_raw).replace(
                    tzinfo=None,
                )
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

                        _ = pathlib.Path(json_filename).write_bytes(json_version_of_parsed_feed)
                    else:
                        logging.info(
                            "%s-%s was more than 7 days old",
                            clean_feed_name,
                            date_string,
                        )

                    # Go to the next feed and stop processing this one
                    continue

            if enable_diagnosis:
                _ = pathlib.Path(json_filename).write_bytes(json_version_of_parsed_feed)

            feed_title_raw = parsed_feed.feed.title
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
            parsed_feed_entry_guids = [parsed_feed_entry.id for parsed_feed_entry in parsed_feed.entries]
            if most_recent_guid is None and feed == bill_simmons_feed:
                if len(parsed_feed_entry_guids) >= 5:
                    most_recent_guid = parsed_feed_entry_guids[4]
                elif len(parsed_feed_entry_guids) > 0:
                    most_recent_guid = parsed_feed_entry_guids[-1]
            try:
                most_recent_guid_index = parsed_feed_entry_guids.index(most_recent_guid)
            except ValueError:
                most_recent_guid_index = None

            # Get list of RSS items that haven't been processed, process them from oldest to newest
            feed_entries_before_most_recently_processed = parsed_feed.entries[:most_recent_guid_index][::-1]

            if len(feed_entries_before_most_recently_processed) > 0:
                logging.info(
                    "Processing %d entries for %s",
                    len(feed_entries_before_most_recently_processed),
                    feed,
                )

            for parsed_feed_entry in feed_entries_before_most_recently_processed:
                raw_date = parser.parse(parsed_feed_entry.published)
                date_stamp = raw_date.strftime("%Y%m%d-%H%M%S-%f")[0:15]
                entry_title_raw = parsed_feed_entry.title
                entry_title_for_filename = re.sub(r"[^A-Za-z0-9 ]+", "", entry_title_raw)
                output_filename = (
                    f"{output_folder}/{date_stamp}-{feed_prefix_for_filename}{entry_title_for_filename}.txt"
                )
                meta_title = entry_title_raw
                original_url = get_entry_link(parsed_feed_entry)

                if feed == bill_simmons_feed:
                    entry_description_raw = (
                        parsed_feed_entry.get("summary") or parsed_feed_entry.get("description") or ""
                    )
                    content_text = str(entry_description_raw)
                else:
                    soup = BeautifulSoup(parsed_feed_entry.content[0].value, "html.parser")
                    content_text = soup.get_text()
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
                _ = pathlib.Path(guid_filename).write_text(parsed_feed_entry.id, encoding="utf-8")
                # Copy new version of guids txt file
                date_string = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
                if enable_diagnosis:
                    _ = shutil.copy2(
                        guid_filename,
                        f"{diagnosis_dir}/{clean_feed_name}-{date_string}-guids-after.txt",
                    )
        except Exception:
            logging.exception("Error processing feed %s", feed)


if __name__ == "__main__":
    main()
