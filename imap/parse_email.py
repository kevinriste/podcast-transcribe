"""Fetch unseen Gmail messages and write raw text files for the pipeline."""

import logging
import os
import pathlib
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

if TYPE_CHECKING:
    from yt_dlp import _Params  # pyright: ignore[reportPrivateUsage]

import yt_dlp
from bs4 import BeautifulSoup
from imap_tools.consts import MailMessageFlags
from imap_tools.mailbox import MailBox
from imap_tools.message import MailMessage
from imap_tools.query import AND
from playwright.sync_api import sync_playwright
from podcast_shared import apply_id3_tags, generate_summary, send_gotify_notification
from trafilatura import bare_extraction, extract

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

output_folder = "../prepare-text/text-input-raw"
gmail_user = os.getenv("GMAIL_PODCAST_ACCOUNT")
gmail_password = os.getenv("GMAIL_PODCAST_ACCOUNT_APP_PASSWORD")
local_scraper_url = "http://localhost:3001/fetch"
nyt_scraper_url = "http://localhost:3002/fetch"


def extract_title(obj: object) -> str:
    """Extract the title from a trafilatura result via as_dict().

    Returns:
        The title string, or empty string if unavailable.

    """
    as_dict_fn = getattr(obj, "as_dict", None)
    raw: object = as_dict_fn() if callable(as_dict_fn) else None
    if isinstance(raw, dict):
        return str(raw.get("title") or "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return ""


def normalize_text(value: str) -> str:
    """Lowercase, strip, and collapse whitespace in a string.

    Returns:
        The normalized text.

    """
    return " ".join(value.strip().lower().split())


def unfold_header_value(value: str | None) -> str:
    """Unfold RFC 2822 folded header values into a single line.

    Returns:
        The unfolded header string.

    """
    if not value:
        return ""
    unfolded = re.sub(r"\r?\n[ \t]+", " ", value)
    unfolded = re.sub(r"[\r\n]+", " ", unfolded)
    return unfolded.strip()


def clean_substack_url(url: str) -> str:
    """Strip tracking parameters from a Substack URL, keeping only IDs.

    Returns:
        The cleaned URL, or the original if cleaning fails.

    """
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
    except (ValueError, KeyError, IndexError):
        return url


def extract_links_from_email(msg: MailMessage) -> list[dict[str, str]]:
    """Extract all unique hyperlinks from an email's HTML and plain text.

    Returns:
        Deduplicated list of {href, text} dicts.

    """
    links: list[dict[str, str]] = []
    if msg.html:
        logging.info("Parsing HTML to extract links")
        soup = BeautifulSoup(msg.html, "html.parser")
        for anchor in soup.find_all("a", href=True):  # pyright: ignore[reportAny]
            text: str = anchor.get_text(" ", strip=True)  # pyright: ignore[reportAny]
            links.append({"href": str(anchor["href"]), "text": text})  # pyright: ignore[reportAny]
    if msg.text:
        logging.info("Parsing plain text to extract links")
        links.extend({"href": url, "text": ""} for url in re.findall(r"https?://[^\s)<>\"']+", msg.text))  # pyright: ignore[reportAny]
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in links:
        href = link["href"]
        if href not in seen:
            seen.add(href)
            deduped.append(link)
    return deduped


def find_source_url(links: list[dict[str, str]], source_kind: str, subject: str) -> str:
    """Find the best source URL from email links based on the newsletter platform.

    Returns:
        The source URL, or empty string if none found.

    """
    subject_norm = normalize_text(subject)
    logging.info("Selecting source URL for %s email", source_kind)
    if source_kind == "beehiiv":
        for link in links:
            if normalize_text(link["text"]) == "read online":
                logging.info("Found Beehiiv 'Read Online' link")
                return link["href"]
    if source_kind == "substack":
        for link in links:
            if normalize_text(link["text"]) == subject_norm and "substack.com/app-link/post" in link["href"]:
                logging.info("Found Substack post link by title match")
                return clean_substack_url(link["href"])
        for link in links:
            if normalize_text(link["text"]) == subject_norm and "open.substack.com" in link["href"]:
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


def fetch_and_process_html(url: str, request_body: dict[str, str] | None = None) -> tuple[object | None, str | None]:
    """Fetch a URL via headless Chromium and extract text with trafilatura.

    Parameters
    ----------
    url : str
        The URL to fetch. When *request_body* is given the actual navigation
        target becomes ``url?url=<request_body['url']>``.
    request_body : dict | None
        If provided, its ``url`` value is appended as a query parameter.

    Returns
    -------
    tuple[object | None, str | None]
        ``(trafilatura_metadata, extracted_text)`` on success, or
        ``(None, None)`` when the page could not be fetched or parsed.

    """
    try:
        logging.info("Fetching %s", url)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            try:
                if request_body:
                    logging.info(
                        "Making GET request to %s with url query parameter",
                        url,
                    )
                    _ = page.goto(
                        f"{url}?url={request_body['url']}",
                        wait_until="networkidle",
                        timeout=180000,
                    )
                else:
                    logging.info("Making GET request to %s", url)
                    _ = page.goto(url, wait_until="networkidle", timeout=180000)

                # Get rendered HTML content
                html_content = page.content()

            except Exception:
                logging.exception("Error occurred while fetching %s", url)
                html_content = None

            finally:
                browser.close()

        if html_content is None:
            logging.error("Playwright returned no content for %s", url)
            return None, None

        trafilatura_result: object | None = bare_extraction(
            html_content,
            with_metadata=True,
        )
        if trafilatura_result is None:
            logging.error("trafilatura returned no metadata for %s", url)
            return None, None
        webpage_text: str = str(extract(html_content, include_comments=False, favor_recall=True) or "")
        title: str = extract_title(trafilatura_result)
        content_text: str = title + ".\n" + "\n" + webpage_text

        return trafilatura_result, content_text

    except Exception:
        logging.exception("Error occurred")
        return None, None


def main() -> None:
    """Fetch unseen emails and route them through the intake pipeline."""
    if not gmail_user or not gmail_password:
        logging.error("Gmail credentials not set")
        return
    with MailBox("imap.gmail.com").login(gmail_user, gmail_password) as mailbox:
        msgs = mailbox.fetch(AND(seen=False), mark_seen=False)  # pyright: ignore[reportUnknownMemberType]
        for msg in msgs:
            try:
                subject_raw = unfold_header_value(msg.subject).replace("Fwd: ", "")
                date_stamp = msg.date.strftime("%Y%m%d-%H%M%S-%f")[0:15]
                from_values = msg.from_values
                if not from_values:
                    logging.warning("Skipping email with no from_values")
                    continue
                from_name_raw = unfold_header_value(from_values.name)
                from_email = from_values.email or ""
                from_name_for_filename = re.sub(r"[^A-Za-z0-9 ]+", "", from_name_raw)
                from_prefix_for_filename = from_name_for_filename + "- " if from_name_for_filename else ""
                subject_for_filename = re.sub(r"[^A-Za-z0-9 ]+", "", subject_raw)
                subject_for_filter_lower = subject_for_filename.lower()
                if subject_for_filter_lower not in {"link", "youtube"}:
                    output_filename = (
                        f"{output_folder}/{date_stamp}-{from_prefix_for_filename}{subject_for_filename}.txt"
                    )
                    logging.info("parsing email: %s", output_filename)
                    email_text_raw = msg.text
                    has_beehiiv = bool(msg.headers.get("x-beehiiv-ids"))
                    source_kind = "beehiiv" if has_beehiiv else "substack"
                    all_links = extract_links_from_email(msg)
                    source_url = find_source_url(all_links, source_kind, subject_raw)
                    if not source_url:
                        source_kind = "unknown"
                        send_gotify_notification(
                            "Unknown email source",
                            f"No source link found for {from_email} ({subject_raw}).",
                        )
                    metadata_block = "\n".join(
                        [
                            f"META_FROM: {from_name_raw}",
                            f"META_TITLE: {subject_raw}",
                            f"META_SOURCE_URL: {source_url}",
                            f"META_SOURCE_KIND: {source_kind}",
                            f"META_SOURCE_NAME: {from_name_raw}",
                            "META_INTAKE_TYPE: email",
                        ],
                    )
                    logging.info("Writing raw metadata and text to text input")
                    _ = pathlib.Path(output_filename).write_text(
                        metadata_block + "\n\n" + email_text_raw, encoding="utf-8"
                    )
                elif subject_for_filter_lower == "youtube":
                    email_text_raw = msg.text
                    youtube_url = re.sub(r"[^\S]+", "", email_text_raw)
                    logging.info("fetching youtube audio: %s", youtube_url)
                    ydl_opts: _Params = {
                        "format": "bestaudio[protocol!=m3u8][protocol!=m3u8_native]/bestaudio/best",
                        "extractor_args": {"youtube": {"player_client": ["android"]}},
                        "fragment_retries": 10,
                        "retries": 5,
                        "postprocessors": [
                            {
                                "key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3",
                                "preferredquality": "192",
                            },
                        ],
                        "outtmpl": "../dropcaster-docker/audio/%(uploader)s- %(title)s.%(ext)s",
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(youtube_url, download=True)
                        base_filename: str = str(ydl.prepare_filename(info))
                        mp3_filename = str(pathlib.Path(base_filename).with_suffix(".mp3"))
                        info_dict: dict[str, object] = dict(info) if info else {}
                        video_title: str = str(info_dict.get("title") or "YouTube Video")
                        video_url: str = str(info_dict.get("webpage_url") or youtube_url)
                        video_description: str = str(info_dict.get("description") or "")
                        summary = generate_summary(video_description, video_title)
                        description_body = summary or "Summary unavailable."
                        description = f'{video_title}<br/><br/>{description_body}<br/><br/>Source: <a href="{video_url}">{video_url}</a>'
                        if pathlib.Path(mp3_filename).exists():
                            apply_id3_tags(
                                mp3_filename, title=video_title, description=description, source_url=video_url, v1=1
                            )
                        else:
                            logging.error("Expected MP3 not found: %s", mp3_filename)
                else:
                    email_text_raw = msg.text
                    url_text_compact = re.sub(r"[^\S]+", "", email_text_raw)
                    logging.info("fetching webpage: %s", url_text_compact)
                    original_url = url_text_compact
                    scraper_url = nyt_scraper_url if "nytimes.com" in original_url else local_scraper_url
                    html_content_parsed_for_title, webpage_text = fetch_and_process_html(
                        url=scraper_url,
                        request_body={"url": original_url},
                    )
                    if webpage_text is None or html_content_parsed_for_title is None:
                        logging.info(
                            "could not parse webpage, saving for next time: %s",
                            original_url,
                        )
                        continue
                    raw_title: str = extract_title(html_content_parsed_for_title) or "No title available"
                    title_for_filename = re.sub(r"[^A-Za-z0-9 ]+", "", raw_title)
                    output_filename = f"{output_folder}/{date_stamp}-{title_for_filename}.txt"
                    metadata_block = "\n".join(
                        [
                            f"META_FROM: {from_name_raw}",
                            f"META_TITLE: {raw_title}",
                            f"META_SOURCE_URL: {original_url}",
                            "META_SOURCE_KIND: url",
                            "META_INTAKE_TYPE: link",
                        ],
                    )
                    logging.info("Writing metadata block to text input")
                    _ = pathlib.Path(output_filename).write_text(
                        metadata_block + "\n\n" + webpage_text, encoding="utf-8"
                    )
                flags = MailMessageFlags.SEEN
                uid: str = msg.uid or ""
                _ = mailbox.flag(uid, flags, value=True)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            except Exception:
                error_from = msg.from_values
                from_email_for_error = error_from.email if error_from else "unknown"
                logging.exception("Error processing email from %s: %s", from_email_for_error, msg.subject)
                send_gotify_notification(
                    "Email processing error",
                    f"Failed to process email from {from_email_for_error}: {msg.subject}",
                )


if __name__ == "__main__":
    main()
