import re
from dateutil import parser
import feedparser
import requests
from bs4 import BeautifulSoup
from waybackpy import WaybackMachineSaveAPI
from trafilatura import extract, bare_extraction
from requests_html import HTMLSession
import pyppeteer
import logging
import os

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')

output_folder = '../text-to-speech/text-input'
feedsFile = 'feeds.txt'
wayback_feeds = [
    "https://www.nytimes.com/svc/collections/v1/publish/www.nytimes.com/column/ross-douthat/rss.xml",
    "https://www.nytimes.com/svc/collections/v1/publish/www.nytimes.com/column/paul-krugman/rss.xml",
    "https://www.nytimes.com/svc/collections/v1/publish/www.nytimes.com/column/ezra-klein/rss.xml"
    ]

feeds = [line.rstrip() for line in open(feedsFile)]

for feed in feeds:
    parsedFeed = feedparser.parse(feed)
    from_ = parsedFeed.feed.title
    clean_from_original = re.sub(r'[^A-Za-z0-9 ]+', '', from_)
    clean_from = clean_from_original + '- ' if clean_from_original != '' else ''
    guid_dir = f'./feed-guids'
    guid_filename = f'{guid_dir}/{clean_from_original}.txt'
    try:
        with open(guid_filename) as guid_file:
            mostRecentGuid = guid_file.read()
    except FileNotFoundError:
        mostRecentGuid = None
    parsedFeedEntryGuids = [parsedFeedEntry.id for parsedFeedEntry in parsedFeed.entries]
    try:
        most_recent_guid_index = parsedFeedEntryGuids.index(mostRecentGuid)
    except ValueError:
        most_recent_guid_index = None

    # Get list of RSS items that haven't been processed, process them from oldest to newest
    feed_entries_before_most_recently_processed = parsedFeed.entries[:most_recent_guid_index][::-1]

    if len(feed_entries_before_most_recently_processed) > 0:
        logging.info(f'Processing {len(feed_entries_before_most_recently_processed)} entries for {feed}')
        
    for parsedFeedEntry in feed_entries_before_most_recently_processed:
        raw_date = parser.parse(parsedFeedEntry.published)
        date = raw_date.strftime('%Y%m%d-%H%M%S-%f')[0:15]
        clean_subject = re.sub(r'[^A-Za-z0-9 ]+', '', parsedFeedEntry.title)
        output_filename = f'{output_folder}/{date}-{clean_from}{clean_subject}.txt'

        if feed in wayback_feeds:
            original_url = parsedFeedEntry.link
            user_agent = "Mozilla/5.0 (Windows NT 5.1; rv:40.0) Gecko/20100101 Firefox/40.0"
            save_api = WaybackMachineSaveAPI(original_url, user_agent)
            save_api.save()
            archive_url = save_api.archive_url
            session = HTMLSession()
            html_fetch = session.get(archive_url)
            try:
                html_fetch.raise_for_status()
                html_fetch.html.render(timeout=60)
            except (requests.HTTPError, pyppeteer.errors.TimeoutError) as e:
                logging.info(f"{archive_url} URL caused the issue.")
                raise e
            html_content = html_fetch.html.html
            html_content_parsed_for_title = bare_extraction(html_content)
            webpage_text = extract(html_content, include_comments=False)
            content_text = html_content_parsed_for_title.get('title') + '.\n' + '\n' + webpage_text
        else:
            soup = BeautifulSoup(parsedFeedEntry.content[0].value, "html.parser")
            content_text = soup.get_text()
            content_text = clean_from + '.\n' + clean_subject + '.\n' + '\n' + content_text
        output_file = open(output_filename, "w")
        output_file.write(content_text)
        output_file.close()
        guidDirExists = os.path.exists(guid_dir)
        if not guidDirExists:
            os.makedirs(guid_dir)
        guid_output_file = open(guid_filename, "w")
        guid_output_file.write(parsedFeedEntry.id)
        guid_output_file.close()
