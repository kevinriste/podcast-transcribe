import logging
import os
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import markdown
import requests
import yt_dlp
from bs4 import BeautifulSoup
from imap_tools import AND, MailBox, MailMessageFlags
from mutagen.id3 import ID3, TIT2, TT3, WXXX, ID3NoHeaderError
from openai import OpenAI
from playwright.sync_api import sync_playwright
from requests.adapters import HTTPAdapter
from trafilatura import bare_extraction, extract
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

output_folder = "../text-to-speech/text-input"
gmail_user = os.getenv("GMAIL_PODCAST_ACCOUNT")
gmail_password = os.getenv("GMAIL_PODCAST_ACCOUNT_APP_PASSWORD")
markdown_email_addresses = [
    "beehiiv",
    "garbageday.email",
]
local_scraper_url = "http://localhost:3001/fetch"
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


def markdown_to_plain_text(markdown_text):
    # Convert Markdown to HTML
    html = markdown.markdown(markdown_text)

    # Use BeautifulSoup to extract text
    soup = BeautifulSoup(html, features="html.parser")
    plain_text = soup.get_text()

    return plain_text


def send_gotify_notification(title, message, priority=6):
    gotify_server = os.environ.get("GOTIFY_SERVER")
    gotify_token = os.environ.get("GOTIFY_TOKEN")
    if not gotify_server or not gotify_token:
        logging.warning("Gotify env vars not set; skipping notification.")
        return
    logging.info("Sending Gotify notification")
    gotify_url = f"{gotify_server}/message?token={gotify_token}"
    data = {"title": title, "message": message, "priority": priority}
    requests.post(gotify_url, data=data)


def normalize_text(value):
    return " ".join(value.strip().lower().split())


def clean_substack_url(url):
    try:
        parsed = urlparse(url)
        if "substack.com" not in parsed.netloc or not parsed.query:
            return url
        params = parse_qs(parsed.query)
        publication_id = params.get("publication_id", [None])[0]
        post_id = params.get("post_id", [None])[0]
        if not publication_id or not post_id:
            return url
        query = urlencode({"publication_id": publication_id, "post_id": post_id})
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, ""))
    except Exception:
        return url


def extract_links_from_email(msg):
    links = []
    if msg.html:
        logging.info("Parsing HTML to extract links")
        soup = BeautifulSoup(msg.html, "html.parser")
        for anchor in soup.find_all("a", href=True):
            text = anchor.get_text(" ", strip=True)
            links.append({"href": anchor["href"], "text": text})
    if msg.text:
        logging.info("Parsing plain text to extract links")
        for url in re.findall(r"https?://[^\s)<>\"']+", msg.text):
            links.append({"href": url, "text": ""})
    deduped = []
    seen = set()
    for link in links:
        href = link["href"]
        if href not in seen:
            seen.add(href)
            deduped.append(link)
    return deduped


def find_source_url(links, source_kind, subject):
    subject_norm = normalize_text(subject)
    logging.info(f"Selecting source URL for {source_kind} email")
    if source_kind == "garbageday":
        for link in links:
            if normalize_text(link["text"]) == "read online":
                logging.info("Found Garbage Day 'Read Online' link")
                return link["href"]
        for link in links:
            if "garbageday" in link["href"]:
                logging.info("Found Garbage Day domain link")
                return link["href"]
    if source_kind == "substack":
        for link in links:
            if (
                normalize_text(link["text"]) == subject_norm
                and "substack.com/app-link/post" in link["href"]
            ):
                logging.info("Found Substack post link by title match")
                return clean_substack_url(link["href"])
        for link in links:
            if (
                normalize_text(link["text"]) == subject_norm
                and "open.substack.com" in link["href"]
            ):
                logging.info("Found Substack open link by title match")
                return link["href"]
        for link in links:
            if "substack.com/app-link/post" in link["href"]:
                logging.info("Found Substack app-link post URL")
                return clean_substack_url(link["href"])
        for link in links:
            if "open.substack.com" in link["href"]:
                logging.info("Found Substack open link")
                return link["href"]
        for link in links:
            if "substack.com" in link["href"]:
                logging.info("Found Substack link")
                return clean_substack_url(link["href"])
    return ""


def get_openai_client():
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI()
    return _openai_client


def generate_summary(text, title):
    if not text.strip():
        logging.info("Summary skipped: empty content")
        return ""
    logging.info("Generating summary via OpenAI")
    prompt = (
        "Summarize the article in 2-3 sentences. Focus on key points and keep it concise.\n\n"
        f"Title: {title}\n\nArticle:\n{text}"
    )
    try:
        client = get_openai_client()
        response = client.responses.create(
            model=summary_model,
            input=prompt,
        )
        logging.info("Summary generated")
        return response.output_text.strip()
    except Exception as exc:
        logging.error(f"Summary generation failed: {exc}")
        return ""


def apply_id3_tags(mp3_path, title, description, source_url):
    logging.info("Writing ID3 tags to MP3")
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()
    if title:
        tags.add(TIT2(encoding=3, text=title))
    if description:
        tags.add(TT3(encoding=3, text=description))
    if source_url:
        tags.add(WXXX(encoding=3, desc="Source", url=source_url))
    tags.save(mp3_path)


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
    try:
        logging.info(f"Fetching {url}")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            try:
                if request_body:
                    logging.info(
                        f"Making GET request to {url} with url query parameter"
                    )
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

        html_content_parsed_for_title = bare_extraction(
            html_content, with_metadata=True
        )
        webpage_text = extract(html_content, include_comments=False, favor_recall=True)
        content_text = (
            (html_content_parsed_for_title.as_dict().get("title") or "")
            + ".\n"
            + "\n"
            + (webpage_text or "")
        )

        return html_content_parsed_for_title, content_text

    except Exception as e:
        logging.error(f"Error occurred: {e}")
        return None, None


# get list of email subjects from INBOX folder
with MailBox("imap.gmail.com").login(gmail_user, gmail_password) as mailbox:
    msgs = mailbox.fetch(AND(seen=False), mark_seen=False)
    for msg in msgs:
        subject = msg.subject.replace("Fwd: ", "")
        date = msg.date.strftime("%Y%m%d-%H%M%S-%f")[0:15]
        from_ = msg.from_values.name
        from_email = msg.from_values.email
        clean_from = re.sub(r"[^A-Za-z0-9 ]+", "", from_)
        clean_from = clean_from + "- " if clean_from != "" else ""
        clean_subject = re.sub(r"[^A-Za-z0-9 ]+", "", subject)
        clean_subject_lowercase = clean_subject.lower()
        if clean_subject_lowercase != "link" and clean_subject_lowercase != "youtube":
            output_filename = f"{output_folder}/{date}-{clean_from}{clean_subject}.txt"
            logging.info(f"parsing email: {output_filename}")
            email_text = msg.text
            source_kind = "garbageday" if "garbageday" in from_email else "substack"
            all_links = extract_links_from_email(msg)
            source_url = find_source_url(all_links, source_kind, clean_subject)
            if not source_url:
                source_kind = "unknown"
                send_gotify_notification(
                    "Unknown email source",
                    f"No Substack/Garbage Day link found for {from_email} ({subject}).",
                )
            if any(
                markdown_email_address in from_email
                for markdown_email_address in markdown_email_addresses
            ):
                email_text = markdown_to_plain_text(email_text)
            first_clean_email_text = re.sub(
                r"https?:\/\/(www\.)?[-a-zA-Z0-9@:%._\+~#=]{2,256}\.[a-z]{2,5}\b([-a-zA-Z0-9@:%_\+.~#?&//=]*)",
                "",
                email_text,
            )
            second_clean_email_text = re.sub(r"\[\]", "", first_clean_email_text)
            third_clean_email_text = re.sub(r"\(\)", "", second_clean_email_text)
            clean_email_text = re.sub(r"<>", "", third_clean_email_text)
            if len(clean_email_text) > 0:
                clean_email_text = (
                    clean_from + ".\n" + clean_subject + ".\n" + "\n" + clean_email_text
                )

            move_to_podcast = True
            if (
                "Jessica Valenti" in clean_from
                and "the week in" not in clean_subject_lowercase
            ):
                move_to_podcast = False
            if move_to_podcast:
                output_file = open(output_filename, "w")
                metadata_block = "\n".join(
                    [
                        f"META_TITLE: {clean_subject}",
                        f"META_SOURCE_URL: {source_url}",
                        f"META_SOURCE_KIND: {source_kind}",
                    ]
                )
                logging.info("Writing metadata block to text input")
                output_file.write(metadata_block + "\n\n" + clean_email_text)
                output_file.close()
        elif clean_subject_lowercase == "youtube":
            email_text = msg.text
            email_text = re.sub(r"[^\S]+", "", email_text)
            logging.info(f"fetching youtube audio: {email_text}")
            ydl_opts = {
                "format": "bestaudio[protocol!=m3u8][protocol!=m3u8_native]/bestaudio/best",
                "extractor_args": {"youtube": {"player_client": ["android"]}},
                "fragment_retries": 10,
                "retries": 5,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
                "outtmpl": "../dropcaster-docker/audio/%(uploader)s- %(title)s.%(ext)s",
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(email_text, download=True)
                base_filename = ydl.prepare_filename(info)
                mp3_filename = os.path.splitext(base_filename)[0] + ".mp3"
                video_title = info.get("title") or "YouTube Video"
                video_url = info.get("webpage_url") or email_text
                video_description = info.get("description") or ""
                summary = generate_summary(video_description, video_title)
                description_body = summary or "Summary unavailable."
                description = f'{video_title}<br/><br/>{description_body}<br/><br/>Source: <a href="{video_url}">{video_url}</a>'
                if os.path.exists(mp3_filename):
                    apply_id3_tags(mp3_filename, video_title, description, video_url)
                else:
                    logging.error(f"Expected MP3 not found: {mp3_filename}")
        else:
            email_text = msg.text
            email_text = re.sub(r"[^\S]+", "", email_text)
            logging.info(f"fetching webpage: {email_text}")
            original_url = email_text
            html_content_parsed_for_title, webpage_text = fetch_and_process_html(
                url=local_scraper_url,
                headers={"Content-Type": "application/json"},
                request_body={"url": original_url},
            )
            if webpage_text is None:
                logging.info(
                    f"could not parse webpage, saving for next time: {email_text}"
                )
                continue
            clean_title = re.sub(
                r"[^A-Za-z0-9 ]+",
                "",
                html_content_parsed_for_title.as_dict().get("title")
                or "No title available",
            )
            output_filename = f"{output_folder}/{date}-{clean_title}.txt"
            output_file = open(output_filename, "w")
            metadata_block = "\n".join(
                [
                    f"META_TITLE: {clean_title}",
                    f"META_SOURCE_URL: {original_url}",
                    "META_SOURCE_KIND: url",
                ]
            )
            logging.info("Writing metadata block to text input")
            output_file.write(metadata_block + "\n\n" + webpage_text)
            output_file.close()
        flags = MailMessageFlags.SEEN
        mailbox.flag(msg.uid, flags, True)
