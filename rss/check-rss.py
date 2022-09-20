import re
from dateutil import parser
import feedparser
from bs4 import BeautifulSoup

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
        if latestEntry:
            output_file = open(guid_filename, "w")
            output_file.write(parsedFeedEntry.id)
            output_file.close()
            latestEntry = False
        raw_date = parser.parse(parsedFeedEntry.published)
        date = raw_date.strftime('%Y%m%d-%H%M%S-%f')[0:15]
        clean_subject = re.sub(r'[^A-Za-z0-9 ]+', '', parsedFeedEntry.title)
        output_filename = f'{output_folder}/{date}-{clean_from}{clean_subject}.txt'
        soup = BeautifulSoup(parsedFeedEntry.content[0].value, "html.parser")
        content_text = soup.get_text()
        content_text = clean_from + '.\n' + clean_subject + '.\n' + '\n' + content_text
        output_file = open(output_filename, "w")
        output_file.write(content_text)
        output_file.close()
