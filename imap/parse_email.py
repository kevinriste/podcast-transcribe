import logging
import os
import re

import markdown
import youtube_dl
from bs4 import BeautifulSoup
from imap_tools import AND, MailBox, MailMessageFlags
from requests.adapters import HTTPAdapter
from requests_html import HTMLSession
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

        with HTMLSession() as session:
            session.mount("https://", adapter)
            session.mount("http://", adapter)

            # Handle POST request if request_body is provided
            if request_body:
                logging.info(f"Making POST request to {url}")
                response = session.post(url, headers=headers, json=request_body)
            else:
                logging.info(f"Making GET request to {url}")
                response = session.get(url, headers=headers)

            response.raise_for_status()

            # Render the HTML content
            response.html.render(timeout=60)

        html_content = response.html.html if not request_body else response.text
        html_content_parsed_for_title = bare_extraction(html_content)
        webpage_text = extract(html_content, include_comments=False, favor_recall=True)
        content_text = (
            (html_content_parsed_for_title.get("title") or "")
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
                output_file.write(clean_email_text)
                output_file.close()
        elif clean_subject_lowercase == "youtube":
            email_text = msg.text
            email_text = re.sub(r"[^\S]+", "", email_text)
            logging.info(f"fetching youtube audio: {email_text}")
            ydl_opts = {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
                "outtmpl": "../dropcaster-docker/audio/%(channel)s- %(title)s.%(ext)s",
            }
            with youtube_dl.YoutubeDL(ydl_opts) as ydl:
                ydl.download([email_text])
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
                r"[^A-Za-z0-9 ]+", "", html_content_parsed_for_title.get("title")
            )
            output_filename = f"{output_folder}/{date}-{clean_title}.txt"
            output_file = open(output_filename, "w")
            output_file.write(webpage_text)
            output_file.close()
        flags = MailMessageFlags.SEEN
        mailbox.flag(msg.uid, flags, True)
