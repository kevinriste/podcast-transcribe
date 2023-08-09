import re
from dateutil import parser
import feedparser
from bs4 import BeautifulSoup
from waybackpy import WaybackMachineSaveAPI
from trafilatura import extract, bare_extraction
from requests_html import HTMLSession

output_folder = '../text-to-speech/text-input'
feedsFile = 'feeds.txt'

with open(feedsFile) as feeds_file:
    feeds = feeds_file.readlines()

for feed in feeds:
    parsedFeed = feedparser.parse(feed)
    from_ = parsedFeed.feed.title
    clean_from_original = re.sub(r'[^A-Za-z0-9 ]+', '', from_)
    clean_from = clean_from_original + '- ' if clean_from_original != '' else ''
    guid_filename = f'./feed-guids/{clean_from_original}.txt'
    try:
        with open(guid_filename) as guid_file:
            guids = guid_file.readlines()
    except FileNotFoundError:
        guids = ['']
    if not guids:
        guids = ['']
    mostRecentGuid = guids.pop()
    latestEntry = True
    for parsedFeedEntry in parsedFeed.entries:
        if parsedFeedEntry.id == mostRecentGuid:
            break
        raw_date = parser.parse(parsedFeedEntry.published)
        date = raw_date.strftime('%Y%m%d-%H%M%S-%f')[0:15]
        clean_subject = re.sub(r'[^A-Za-z0-9 ]+', '', parsedFeedEntry.title)
        output_filename = f'{output_folder}/{date}-{clean_from}{clean_subject}.txt'
        if feed == "https://www.nytimes.com/svc/collections/v1/publish/www.nytimes.com/column/ross-douthat/rss.xml":
            original_url = parsedFeedEntry.link
            user_agent = "Mozilla/5.0 (Windows NT 5.1; rv:40.0) Gecko/20100101 Firefox/40.0"
            save_api = WaybackMachineSaveAPI(original_url, user_agent)
            save_api.save()
            archive_url = save_api.archive_url
            session = HTMLSession()
            html_fetch = session.get(archive_url)
            html_fetch.html.render()
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
        if latestEntry:
            output_file = open(guid_filename, "w")
            output_file.write(parsedFeedEntry.id)
            output_file.close()
            latestEntry = False
