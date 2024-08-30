import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta

import feedparser
import msgspec
import pyppeteer
import requests
import urllib3
import waybackpy
from bs4 import BeautifulSoup
from dateutil import parser
from requests.adapters import HTTPAdapter
from requests_html import HTMLSession
from trafilatura import bare_extraction, extract
from urllib3.util.retry import Retry
from waybackpy import WaybackMachineCDXServerAPI, WaybackMachineSaveAPI

# Create a Retry object with zero retries
retry_strategy = Retry(
    total=0,  # Total number of retries to allow
    connect=0,  # Number of connection-related retries to allow
    read=0,  # Number of read-related retries to allow
    redirect=0,  # Number of redirection-related retries to allow
    status=0,  # Number of retries to allow based on HTTP response status codes
)

# Create an HTTPAdapter with your retry strategy
adapter = HTTPAdapter(max_retries=retry_strategy)

enable_diagnosis = False

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

output_folder = "../text-to-speech/text-input"
feedsFile = "feeds.txt"
wayback_feeds = [
    "https://www.nytimes.com/svc/collections/v1/publish/www.nytimes.com/column/ross-douthat/rss.xml",
    "https://www.nytimes.com/svc/collections/v1/publish/www.nytimes.com/column/paul-krugman/rss.xml",
    "https://www.nytimes.com/svc/collections/v1/publish/www.nytimes.com/column/ezra-klein/rss.xml",
    "https://www.nytimes.com/svc/collections/v1/publish/www.nytimes.com/column/thomas-l-friedman/rss.xml",
]

feeds = [line.rstrip() for line in open(feedsFile)]


def fetch_and_process_html(url, final_request=False):
    """
    Fetches HTML content from a given URL, processes it, and checks if it contains a specific phrase.

    Parameters:
    - url: The URL to fetch the HTML content from.
    - final_request: boolean indicating if this is the last time the
       RSS article view is being attempted, meaning that all the
       previous article views failed and a save was attempted. This
       is sufficient failure to warrant a gotify notification.

    Returns:
    - content_text: The processed HTML content as text, or None if the check fails.
    """
    check_phrases = [
        "has been an Opinion columnist",
        "The Times is committed to publishing a diversity of letters to the editor",
    ]

    logging.info(f"Fetching {url}")

    with HTMLSession() as session:
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        html_fetch = session.get(url)
        html_fetch.raise_for_status()
        html_fetch.html.render(timeout=60)

    html_content = html_fetch.html.html
    html_content_parsed_for_title = bare_extraction(html_content)
    webpage_text = extract(html_content, include_comments=False, favor_recall=True)
    content_text = (
        (html_content_parsed_for_title.get("title") or "")
        + ".\n"
        + "\n"
        + (webpage_text or "")
    )

    if all(phrase not in content_text for phrase in check_phrases):
        logging.error(
            f"Wayback Machine's version of {url} did not include the full article."
        )
        gotify_server = os.environ.get("GOTIFY_SERVER")
        gotify_token = os.environ.get("GOTIFY_TOKEN")
        debug_message = "Error: Incomplete NYT article in Wayback Machine"
        debug_output = f"{url}: {content_text}"

        gotify_url = f"{gotify_server}/message?token={gotify_token}"
        data = {"title": debug_message, "message": debug_output, "priority": 2}

        if final_request:
            requests.post(gotify_url, data=data)
        return None

    return content_text


for feed in feeds:
    try:
        parsedFeed = feedparser.parse(feed)

        # Prepare shared variables for file logging
        now = datetime.now()
        date_string = now.strftime("%Y%m%d-%H%M%S")
        clean_feed_name = re.sub(r"[^A-Za-z0-9 ]+", "", feed)
        diagnosis_dir = "./diagnosis"

        # Save the serializable feed data to a JSON file
        json_filename = f"{diagnosis_dir}/{clean_feed_name}-{date_string}-json.json"
        json_version_of_parsed_feed = msgspec.json.encode(parsedFeed)

        # Sometimes The Money Illusion returns an old version of its feed for some reason. This prevents that from causing processing of old items.
        parsed_feed_updated_date = parser.parse(parsedFeed.feed.updated).replace(
            tzinfo=None
        )
        max_timedelta_since_feed_last_updated = timedelta(days=7)
        timedelta_since_feed_last_updated = now - parsed_feed_updated_date
        if timedelta_since_feed_last_updated > max_timedelta_since_feed_last_updated:
            error_threshold_timedelta_since_feed_last_updated = timedelta(days=30)
            if (
                timedelta_since_feed_last_updated
                > error_threshold_timedelta_since_feed_last_updated
            ):
                logging.error(
                    f"Error: {clean_feed_name}-{date_string} was more than 30 days old"
                )

                with open(json_filename, "wb") as json_file:
                    json_file.write(json_version_of_parsed_feed)
            else:
                logging.info(
                    f"{clean_feed_name}-{date_string} was more than 7 days old"
                )

            # Go to the next feed and stop processing this one
            continue

        if enable_diagnosis:
            with open(json_filename, "wb") as json_file:
                json_file.write(json_version_of_parsed_feed)

        from_ = parsedFeed.feed.title
        clean_from_original = re.sub(r"[^A-Za-z0-9 ]+", "", from_)
        clean_from = clean_from_original + "- " if clean_from_original != "" else ""
        guid_dir = "./feed-guids"
        guid_filename = f"{guid_dir}/{clean_from_original}.txt"
        try:
            with open(guid_filename) as guid_file:
                mostRecentGuid = guid_file.read()
            # Copy current version of guids txt file
            if enable_diagnosis:
                shutil.copy(
                    guid_filename,
                    f"{diagnosis_dir}/{clean_feed_name}-{date_string}-guids-before.txt",
                )
        except FileNotFoundError:
            mostRecentGuid = None
        parsedFeedEntryGuids = [
            parsedFeedEntry.id for parsedFeedEntry in parsedFeed.entries
        ]
        try:
            most_recent_guid_index = parsedFeedEntryGuids.index(mostRecentGuid)
        except ValueError:
            most_recent_guid_index = None

        # Get list of RSS items that haven't been processed, process them from oldest to newest
        feed_entries_before_most_recently_processed = parsedFeed.entries[
            :most_recent_guid_index
        ][::-1]

        if len(feed_entries_before_most_recently_processed) > 0:
            logging.info(
                f"Processing {len(feed_entries_before_most_recently_processed)} entries for {feed}"
            )

        for parsedFeedEntry in feed_entries_before_most_recently_processed:
            raw_date = parser.parse(parsedFeedEntry.published)
            date = raw_date.strftime("%Y%m%d-%H%M%S-%f")[0:15]
            clean_subject = re.sub(r"[^A-Za-z0-9 ]+", "", parsedFeedEntry.title)
            output_filename = f"{output_folder}/{date}-{clean_from}{clean_subject}.txt"

            if feed in wayback_feeds:
                try:
                    send_error_with_gotify = False
                    max_timedelta_since_article_added_to_feed = timedelta(days=2)
                    timedelta_since_article_added_to_feed = now - raw_date.replace(
                        tzinfo=None
                    )
                    if (
                        timedelta_since_article_added_to_feed
                        > max_timedelta_since_article_added_to_feed
                    ):
                        send_error_with_gotify = True
                    original_url = parsedFeedEntry.link
                    user_agent = "Mozilla/5.0 (Windows NT 5.1; rv:40.0) Gecko/20100101 Firefox/40.0"
                    cdx_api = WaybackMachineCDXServerAPI(original_url, user_agent)
                    snapshots = list(cdx_api.snapshots())
                    sorted_snapshots = sorted(
                        snapshots, key=lambda x: x.datetime_timestamp, reverse=True
                    )
                    two_most_recent_snapshots = sorted_snapshots[:2]

                    if send_error_with_gotify or enable_diagnosis:
                        # Function to convert a CDXSnapshot object to a dictionary dynamically
                        def snapshot_to_dict(snapshot):
                            result = {}
                            for attr in dir(snapshot):
                                if not attr.startswith("_") and not callable(
                                    getattr(snapshot, attr)
                                ):
                                    value = getattr(snapshot, attr)
                                    if isinstance(value, datetime):
                                        value = value.isoformat()
                                    result[attr] = value
                            return result

                        snapshots_list = [
                            snapshot_to_dict(snapshot) for snapshot in snapshots
                        ]
                        snapshots_json_filename = f"{diagnosis_dir}/{clean_feed_name}-{date_string}-snapshots-json.json"
                        with open(snapshots_json_filename, "w") as json_file:
                            json.dump(snapshots_list, json_file, indent=2)

                    content_text = None

                    this_year = datetime.now().year
                    calendar_captures_api_url_this_year = f"https://web.archive.org/__wb/calendarcaptures/2?url={original_url}&date={this_year}"
                    this_year_response = requests.get(
                        calendar_captures_api_url_this_year
                    )
                    this_year_data = this_year_response.json()

                    last_year = this_year - 1
                    calendar_captures_api_url_last_year = f"https://web.archive.org/__wb/calendarcaptures/2?url={original_url}&date={last_year}"
                    last_year_response = requests.get(
                        calendar_captures_api_url_last_year
                    )
                    last_year_data = last_year_response.json()

                    # Collection name we are interested in
                    collection_name = "global.nytimes.com"

                    # Extract the collections and items
                    this_year_collections = this_year_data.get("colls") or []
                    this_year_items = this_year_data.get("items") or []

                    # Extract the collections and items
                    last_year_collections = last_year_data.get("colls") or []
                    last_year_items = last_year_data.get("items") or []

                    # Filter items by the "global.nytimes.com" collection
                    filtered_captures = []

                    for item in this_year_items:
                        timestamp, status_code, collection_index = item
                        if collection_index < len(this_year_collections):
                            collection = this_year_collections[collection_index]
                            if collection_name in collection:
                                timestamp_str = str(timestamp)
                                if len(timestamp_str) == 9:
                                    timestamp_str = timestamp_str.zfill(10)
                                formatted_timestamp = str(this_year) + timestamp_str
                                final_url = f"https://web.archive.org/web/{formatted_timestamp}/{original_url}"
                                filtered_captures.append(final_url)

                    for item in last_year_items:
                        timestamp, status_code, collection_index = item
                        if collection_index < len(last_year_collections):
                            collection = last_year_collections[collection_index]
                            if collection_name in collection:
                                timestamp_str = str(timestamp)
                                if len(timestamp_str) == 9:
                                    timestamp_str = timestamp_str.zfill(10)
                                formatted_timestamp = str(this_year) + timestamp_str
                                final_url = f"https://web.archive.org/web/{formatted_timestamp}/{original_url}"
                                filtered_captures.append(final_url)

                    for filtered_capture in filtered_captures:
                        content_text = fetch_and_process_html(url=filtered_capture)
                        if content_text is not None:
                            break

                    if content_text is None:
                        for snapshot in two_most_recent_snapshots:
                            content_text = fetch_and_process_html(
                                url=snapshot.archive_url
                            )
                            if content_text is not None:
                                break

                    if content_text is None:
                        save_api = WaybackMachineSaveAPI(original_url, user_agent)
                        save_api.save()
                        new_archive_url = save_api.archive_url

                        content_text = fetch_and_process_html(
                            url=new_archive_url,
                            final_request=send_error_with_gotify,
                        )
                    # If we failed to get the real article, stop processing this feed altogether so the article doesn't get skipped next time.
                    if content_text is None:
                        break
                except (
                    requests.HTTPError,
                    requests.ConnectionError,
                    requests.exceptions.RetryError,
                    pyppeteer.errors.TimeoutError,
                    pyppeteer.errors.PageError,
                    urllib3.exceptions.NewConnectionError,
                    urllib3.exceptions.MaxRetryError,
                    waybackpy.exceptions.TooManyRequestsError,
                    waybackpy.exceptions.MaximumSaveRetriesExceeded,
                ) as e:
                    logging.error(f"Error occurred: {e}")
                    logging.info(f"{original_url} URL caused the issue.")
                    gotify_server = os.environ.get("GOTIFY_SERVER")
                    gotify_token = os.environ.get("GOTIFY_TOKEN")
                    debug_message = "RSS URL catastrophic error"
                    debug_output = f"{original_url}: {e}"

                    gotify_url = f"{gotify_server}/message?token={gotify_token}"
                    data = {
                        "title": debug_message,
                        "message": debug_output,
                        "priority": 6,
                    }

                    if send_error_with_gotify:
                        requests.post(gotify_url, data=data)
                    break
            else:
                soup = BeautifulSoup(parsedFeedEntry.content[0].value, "html.parser")
                content_text = soup.get_text()
                content_text = (
                    clean_from + ".\n" + clean_subject + ".\n" + "\n" + content_text
                )
            output_file = open(output_filename, "w")
            output_file.write(content_text)
            output_file.close()
            guidDirExists = os.path.exists(guid_dir)
            if not guidDirExists:
                os.makedirs(guid_dir)
            guid_output_file = open(guid_filename, "w")
            guid_output_file.write(parsedFeedEntry.id)
            guid_output_file.close()
            # Copy new version of guids txt file
            date_string = datetime.now().strftime("%Y%m%d-%H%M%S")
            if enable_diagnosis:
                shutil.copy(
                    guid_filename,
                    f"{diagnosis_dir}/{clean_feed_name}-{date_string}-guids-after.txt",
                )
    except Exception as e:
        logging.error(f"Error occurred: {e}")
        logging.info(f"{feed} URL caused the issue.")
