import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta

import feedparser
import msgspec
import playwright.sync_api
import requests
import urllib3
import waybackpy
from bs4 import BeautifulSoup
from dateutil import parser
from openai import OpenAI
from playwright.sync_api import sync_playwright
from requests.adapters import HTTPAdapter
from trafilatura import bare_extraction, extract
from urllib3.util.retry import Retry
from waybackpy import WaybackMachineCDXServerAPI

local_scraper_url = "http://localhost:3002/fetch"
bill_simmons_feed = "https://feeds.megaphone.fm/the-bill-simmons-podcast"
summary_model = "gpt-5-mini"
_openai_client = None

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
    "https://www.nytimes.com/svc/collections/v1/publish/www.nytimes.com/column/ezra-klein/rss.xml",
    "https://www.nytimes.com/svc/collections/v1/publish/www.nytimes.com/column/thomas-l-friedman/rss.xml",
]

feeds = [line.rstrip() for line in open(feedsFile)]


def get_openai_client():
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI()
    return _openai_client


def send_gotify_notification(title, message, priority=6):
    gotify_server = os.environ.get("GOTIFY_SERVER")
    gotify_token = os.environ.get("GOTIFY_TOKEN")
    if not gotify_server or not gotify_token:
        logging.warning("Gotify env vars not set; skipping notification.")
        return
    gotify_url = f"{gotify_server}/message?token={gotify_token}"
    data = {"title": title, "message": message, "priority": priority}
    requests.post(gotify_url, data=data)


def is_nfl_related(title, description):
    if not title and not description:
        return False
    prompt = (
        "Determine if this podcast episode involves NFL football. "
        "Respond with YES or NO only.\n\n"
        f"Title: {title}\n\nDescription:\n{description}"
    )
    try:
        client = get_openai_client()
        response = client.responses.create(model=summary_model, input=prompt)
        return response.output_text.strip().upper().startswith("YES")
    except Exception as exc:
        logging.error(f"NFL relevance check failed: {exc}")
        return False


def fetch_and_process_html(url, final_request=False, headers=None, request_body=None):
    """
    Fetches HTML content from a given URL, processes it, and checks if it contains a specific phrase.

    Parameters:
    - url: The URL to fetch the HTML content from.
    - final_request: Boolean indicating if this is the last attempt to fetch the article.
    - headers: Optional dictionary of headers to include in the request (GET or POST).
    - request_body: Optional dictionary of data for making a POST request instead of a GET.

    Returns:
    - content_text: The processed HTML content as text, or None if the check fails.
    """
    check_phrases = [
        "has been an Opinion columnist",
        "From Beirut to Jerusalem",
        "joined Opinion in 2021",
    ]

    logging.info(f"Fetching {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            if request_body:
                logging.info(f"Making GET request to {url} with url query parameter")
                page.goto(
                    f"{url}?url={request_body['url']}",
                    wait_until="networkidle",
                    timeout=180000,
                )
            else:
                logging.info(f"Making GET request to {url}")
                page.goto(url, wait_until="networkidle", timeout=180000)

            # Get rendered HTML content
            html_content = page.content()

        except Exception as e:
            logging.error(f"Error occurred while fetching {url}: {e}")
            html_content = None

        finally:
            browser.close()

    html_content_parsed_for_title = bare_extraction(html_content, with_metadata=True)
    webpage_text = extract(html_content, include_comments=False, favor_recall=True)
    content_text = (
        (html_content_parsed_for_title.as_dict().get("title") or "")
        + ".\n"
        + "\n"
        + (webpage_text or "")
    )

    if all(phrase not in content_text for phrase in check_phrases):
        logging.error(
            f"Kevin's or Wayback Machine's version of {url} did not include the full article."
        )
        debug_message = "Error: Incomplete NYT article in Wayback Machine"
        debug_output = f"{url}: {content_text}"

        if final_request:
            send_gotify_notification(debug_message, debug_output, priority=2)
        return None

    return content_text


for feed in feeds:
    try:
        parsed_feed = feedparser.parse(feed)

        # Prepare shared variables for file logging
        now = datetime.now()
        date_string = now.strftime("%Y%m%d-%H%M%S")
        clean_feed_name = re.sub(r"[^A-Za-z0-9 ]+", "", feed)
        diagnosis_dir = "./diagnosis"

        # Save the serializable feed data to a JSON file
        json_filename = f"{diagnosis_dir}/{clean_feed_name}-{date_string}-json.json"
        json_version_of_parsed_feed = msgspec.json.encode(parsed_feed)

        # Sometimes The Money Illusion returns an old version of its feed for some reason. This prevents that from causing processing of old items.
        parsed_feed_updated_date = parser.parse(parsed_feed.feed.updated).replace(
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

        feed_title_raw = parsed_feed.feed.title
        feed_title_for_filename = re.sub(r"[^A-Za-z0-9 ]+", "", feed_title_raw)
        feed_prefix_for_filename = (
            feed_title_for_filename + "- " if feed_title_for_filename != "" else ""
        )
        guid_dir = "./feed-guids"
        guid_filename = f"{guid_dir}/{feed_title_for_filename}.txt"
        try:
            with open(guid_filename) as guid_file:
                most_recent_guid = guid_file.read()
            # Copy current version of guids txt file
            if enable_diagnosis:
                shutil.copy(
                    guid_filename,
                    f"{diagnosis_dir}/{clean_feed_name}-{date_string}-guids-before.txt",
                )
        except FileNotFoundError:
            most_recent_guid = None
        parsed_feed_entry_guids = [
            parsed_feed_entry.id for parsed_feed_entry in parsed_feed.entries
        ]
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
        feed_entries_before_most_recently_processed = parsed_feed.entries[
            :most_recent_guid_index
        ][::-1]

        if len(feed_entries_before_most_recently_processed) > 0:
            logging.info(
                f"Processing {len(feed_entries_before_most_recently_processed)} entries for {feed}"
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
            original_url = parsed_feed_entry.link
            write_output = True

            if feed == bill_simmons_feed:
                entry_description_raw = (
                    parsed_feed_entry.get("summary")
                    or parsed_feed_entry.get("description")
                    or ""
                )
                entry_description = str(entry_description_raw)
                if is_nfl_related(entry_title_raw, entry_description):
                    notify_title = (
                        "New Bill Simmons podcast to whitelist: "
                        f"{entry_title_raw} https://podly.klt.pw"
                    )
                    send_gotify_notification(notify_title, entry_description)
                content_text = ""
                write_output = False
            elif feed in wayback_feeds:
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
                    content_text = fetch_and_process_html(
                        url=local_scraper_url,
                        headers={"Content-Type": "application/json"},
                        request_body={"url": original_url},
                    )

                    if content_text is None:
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

                    # Temporary comment out of save API call since Wayback isn't working
                    #
                    # if content_text is None:
                    #     save_api = WaybackMachineSaveAPI(original_url, user_agent)
                    #     save_api.save()
                    #     new_archive_url = save_api.archive_url

                    #     content_text = fetch_and_process_html(
                    #         url=new_archive_url,
                    #         final_request=send_error_with_gotify,
                    #     )

                    # If we failed to get the real article, stop processing this feed altogether so the article doesn't get skipped next time.
                    if content_text is None:
                        break
                except (
                    requests.HTTPError,
                    requests.ConnectionError,
                    requests.exceptions.RetryError,
                    playwright.sync_api.TimeoutError,
                    urllib3.exceptions.NewConnectionError,
                    urllib3.exceptions.MaxRetryError,
                    waybackpy.exceptions.TooManyRequestsError,
                    waybackpy.exceptions.MaximumSaveRetriesExceeded,
                ) as e:
                    logging.error(f"Error occurred: {e}")
                    logging.info(f"{original_url} URL caused the issue.")
                    debug_message = "RSS URL catastrophic error"
                    debug_output = f"{original_url}: {e}"

                    if send_error_with_gotify:
                        send_gotify_notification(debug_message, debug_output, priority=6)
                    break
            else:
                soup = BeautifulSoup(
                    parsed_feed_entry.content[0].value, "html.parser"
                )
                content_text_raw = soup.get_text()
                content_text_with_prefix = (
                    (feed_title_raw + ".\n" if feed_title_raw else "")
                    + entry_title_raw
                    + ".\n"
                    + "\n"
                    + content_text_raw
                )
                content_text = content_text_with_prefix
            if write_output:
                output_file = open(output_filename, "w")
                metadata_block = "\n".join(
                    [
                        f"META_FROM: {feed_title_raw}",
                        f"META_TITLE: {meta_title}",
                        f"META_SOURCE_URL: {original_url}",
                        "META_SOURCE_KIND: rss",
                    ]
                )
                logging.info("Writing metadata block to text input")
                output_file.write(metadata_block + "\n\n" + content_text)
                output_file.close()
            guidDirExists = os.path.exists(guid_dir)
            if not guidDirExists:
                os.makedirs(guid_dir)
            guid_output_file = open(guid_filename, "w")
            guid_output_file.write(parsed_feed_entry.id)
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
