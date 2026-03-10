import logging
import os
import pathlib
import re
from collections.abc import Mapping, Sequence
from typing import Final
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
import yt_dlp
from bs4 import BeautifulSoup
from google import genai
from imap_tools.consts import MailMessageFlags
from imap_tools.mailbox import MailBox
from imap_tools.query import AND
from mutagen.id3 import ID3, TIT2, TIT3, WXXX, ID3NoHeaderError  # pyright: ignore[reportPrivateImportUsage]
from playwright.sync_api import sync_playwright
from pyrsistent import PMap, PVector, pmap, pvector
from requests.adapters import HTTPAdapter
from trafilatura import bare_extraction, extract
from trafilatura.settings import Document
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

output_folder: Final = "../prepare-text/text-input-raw"
gmail_user: Final = os.getenv("GMAIL_PODCAST_ACCOUNT")
gmail_password: Final = os.getenv("GMAIL_PODCAST_ACCOUNT_APP_PASSWORD")
local_scraper_url: Final = "http://localhost:3001/fetch"
summary_model: Final = "gemini-3.1-flash-lite-preview"
_gemini_client: genai.Client | None = None

retry_strategy: Final = Retry(
    total=0,
    connect=0,
    read=0,
    redirect=0,
    status=0,
)

adapter: Final = HTTPAdapter(max_retries=retry_strategy)


def send_gotify_notification(title: str, message: str, priority: int = 6) -> None:
    gotify_server: Final = os.environ.get("GOTIFY_SERVER")
    gotify_token: Final = os.environ.get("GOTIFY_TOKEN")
    if not gotify_server or not gotify_token:
        logging.warning("Gotify env vars not set; skipping notification.")
        return
    logging.info("Sending Gotify notification")
    gotify_url: Final = f"{gotify_server}/message?token={gotify_token}"
    # Mutable: requests requires dict
    data: Final[dict[str, str | int]] = {"title": title, "message": message, "priority": priority}
    try:
        _ = requests.post(gotify_url, data=data, timeout=30)
    except requests.RequestException:
        logging.exception("Failed to send Gotify notification")


def normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().split())


def unfold_header_value(value: str | None) -> str:
    if not value:
        return ""
    unfolded: Final = re.sub(r"\r?\n[ \t]+", " ", value)
    unfolded2: Final = re.sub(r"[\r\n]+", " ", unfolded)
    return unfolded2.strip()


def clean_substack_url(url: str) -> str:
    try:
        parsed: Final = urlparse(url)
        if "substack.com" not in parsed.netloc or not parsed.query:
            return url
        params: Final = parse_qs(parsed.query)
        publication_id: Final = params.get("publication_id", [None])[0]
        post_id: Final = params.get("post_id", [None])[0]
        if not publication_id or not post_id:
            return url
        query: Final = urlencode({"publication_id": publication_id, "post_id": post_id})
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, ""))
    except Exception:  # noqa: BLE001
        return url


def extract_links_from_email(msg: object) -> PVector[PMap[str, str]]:
    html_body: Final = getattr(msg, "html", None)
    text_body: Final = getattr(msg, "text", None)
    raw_links: list[PMap[str, str]] = []  # Mutable: accumulation before dedup
    if html_body:
        logging.info("Parsing HTML to extract links")
        soup: Final = BeautifulSoup(html_body, "html.parser")  # pyright: ignore[reportAny]
        for anchor in soup.find_all("a", href=True):  # pyright: ignore[reportAny]
            text = anchor.get_text(" ", strip=True)  # pyright: ignore[reportAny]
            raw_links.append(pmap({"href": str(anchor["href"]), "text": str(text)}))  # pyright: ignore[reportAny]
    if text_body:
        logging.info("Parsing plain text to extract links")
        raw_links.extend(
            pmap({"href": str(url), "text": ""})  # pyright: ignore[reportAny]
            for url in re.findall(r"https?://[^\s)<>\"']+", text_body)  # pyright: ignore[reportAny]
        )
    return _dedup_links(raw_links)


def _dedup_links(links: Sequence[PMap[str, str]]) -> PVector[PMap[str, str]]:
    seen: set[str] = set()  # Mutable: tracking seen hrefs during dedup
    deduped: list[PMap[str, str]] = []  # Mutable: accumulation before freeze
    for link in links:
        href = link["href"]
        if href not in seen:
            seen.add(href)
            deduped.append(link)
    return pvector(deduped)


def find_source_url(links: Sequence[Mapping[str, str]], source_kind: str, subject: str) -> str:  # noqa: PLR0911
    subject_norm: Final = normalize_text(subject)
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


def get_gemini_client() -> genai.Client:
    global _gemini_client  # noqa: PLW0603
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _gemini_client


def generate_summary(text: str, title: str) -> str:
    if not text.strip():
        logging.info("Summary skipped: empty content")
        return ""
    logging.info("Generating summary via Gemini")
    prompt: Final = (
        "Summarize the article in 2-3 sentences. Focus on key points and keep it concise.\n\n"
        f"Title: {title}\n\nArticle:\n{text}"
    )
    try:
        client: Final = get_gemini_client()
        response: Final = client.models.generate_content(  # pyright: ignore[reportUnknownMemberType]
            model=summary_model,
            contents=prompt,
        )
        logging.info("Summary generated")
    except Exception:
        logging.exception("Summary generation failed")
        return ""
    else:
        summary_text: Final = response.text
        return summary_text.strip() if summary_text else ""


def apply_id3_tags(mp3_path: str, title: str, description: str, source_url: str) -> None:
    logging.info("Writing ID3 tags to MP3")
    try:
        tags = ID3(mp3_path)  # Mutable: mutagen requires mutable ID3
    except ID3NoHeaderError:
        tags = ID3()  # Mutable: mutagen requires mutable ID3
    if title:
        tags.add(TIT2(encoding=3, text=title))  # pyright: ignore[reportUnknownMemberType]
    if description:
        tags.add(TIT3(encoding=3, text=description))  # pyright: ignore[reportUnknownMemberType]
    if source_url:
        tags.add(WXXX(encoding=3, desc="Source", url=source_url))  # pyright: ignore[reportUnknownMemberType]
    tags.save(mp3_path)  # pyright: ignore[reportUnknownMemberType]


def fetch_and_process_html(
    url: str,
    *,
    request_body: Mapping[str, str] | None = None,
) -> tuple[Document | None, str | None]:
    """Fetch HTML via Playwright and extract article text using trafilatura.

    Parameters
    ----------
    - url: The URL to fetch the HTML content from.
    - request_body: Optional mapping of data for making a GET request with query parameter.

    Returns
    -------
    - A tuple of (parsed document, content text), or (None, None) if fetching fails.

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

                html_content: str | None = page.content()

            except Exception:
                logging.exception("Error occurred while fetching %s", url)
                html_content = None

            finally:
                browser.close()

        if html_content is None:
            return None, None

        html_content_parsed_for_title: Final = bare_extraction(
            html_content,
            with_metadata=True,
        )
        webpage_text: Final = extract(html_content, include_comments=False, favor_recall=True)

    except Exception:
        logging.exception("Error fetching %s", url)
        return None, None
    else:
        if html_content_parsed_for_title is None:
            return None, None
        if not isinstance(html_content_parsed_for_title, Document):
            return None, None
        parsed_title: Final = html_content_parsed_for_title.as_dict().get("title") or ""
        content_text: Final = parsed_title + ".\n" + "\n" + (webpage_text or "")
        return html_content_parsed_for_title, content_text


def main() -> None:
    with MailBox("imap.gmail.com").login(gmail_user, gmail_password) as mailbox:  # pyright: ignore[reportArgumentType]
        msgs = mailbox.fetch(AND(seen=False), mark_seen=False)  # pyright: ignore[reportUnknownMemberType]
        for msg in msgs:  # Mutable: imap_tools iterator
            subject_raw = unfold_header_value(msg.subject).replace("Fwd: ", "")
            date_stamp = msg.date.strftime("%Y%m%d-%H%M%S-%f")[0:15]
            from_values = msg.from_values
            if from_values is None:
                logging.warning("Skipping message with no from_values")
                continue
            from_name_raw = unfold_header_value(from_values.name)
            from_email = from_values.email or ""
            from_name_for_filename = re.sub(r"[^A-Za-z0-9 ]+", "", from_name_raw)
            from_prefix_for_filename = from_name_for_filename + "- " if from_name_for_filename else ""
            subject_for_filename = re.sub(r"[^A-Za-z0-9 ]+", "", subject_raw)
            subject_for_filter_lower = subject_for_filename.lower()
            if subject_for_filter_lower not in {"link", "youtube"}:
                output_filename = f"{output_folder}/{date_stamp}-{from_prefix_for_filename}{subject_for_filename}.txt"
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
                    (
                        f"META_FROM: {from_name_raw}",
                        f"META_TITLE: {subject_raw}",
                        f"META_SOURCE_URL: {source_url}",
                        f"META_SOURCE_KIND: {source_kind}",
                        f"META_SOURCE_NAME: {from_name_raw}",
                        "META_INTAKE_TYPE: email",
                    ),
                )
                logging.info("Writing raw metadata and text to text input")
                _ = pathlib.Path(output_filename).write_text(metadata_block + "\n\n" + email_text_raw, encoding="utf-8")
            elif subject_for_filter_lower == "youtube":
                email_text_raw = msg.text
                youtube_url = re.sub(r"[^\S]+", "", email_text_raw)
                logging.info("fetching youtube audio: %s", youtube_url)
                ydl_opts = {  # Mutable: yt_dlp requires dict
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
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # pyright: ignore[reportArgumentType]
                    info = ydl.extract_info(youtube_url, download=True)
                    base_filename = ydl.prepare_filename(info)
                    mp3_filename = str(pathlib.Path(str(base_filename)).with_suffix(".mp3"))
                    video_title = str(info.get("title") or "YouTube Video")
                    video_url = str(info.get("webpage_url") or youtube_url)
                    video_description = str(info.get("description") or "")
                    summary = generate_summary(video_description, video_title)
                    description_body = summary or "Summary unavailable."
                    description = (
                        f"{video_title}<br/><br/>{description_body}"
                        f'<br/><br/>Source: <a href="{video_url}">{video_url}</a>'
                    )
                    if pathlib.Path(mp3_filename).exists():
                        apply_id3_tags(mp3_filename, video_title, description, video_url)
                    else:
                        logging.error("Expected MP3 not found: %s", mp3_filename)
            else:
                email_text_raw = msg.text
                url_text_compact = re.sub(r"[^\S]+", "", email_text_raw)
                logging.info("fetching webpage: %s", url_text_compact)
                original_url = url_text_compact
                html_content_parsed_for_title, webpage_text = fetch_and_process_html(
                    url=local_scraper_url,
                    request_body={"url": original_url},  # Mutable: playwright requires dict
                )
                if webpage_text is None:
                    logging.info(
                        "could not parse webpage, saving for next time: %s",
                        original_url,
                    )
                    continue
                if html_content_parsed_for_title is None:
                    logging.info("could not parse webpage title for: %s", original_url)
                    continue
                raw_title = html_content_parsed_for_title.as_dict().get("title") or "No title available"
                title_for_filename = re.sub(r"[^A-Za-z0-9 ]+", "", raw_title)
                output_filename = f"{output_folder}/{date_stamp}-{title_for_filename}.txt"
                metadata_block = "\n".join(
                    (
                        f"META_FROM: {from_name_raw}",
                        f"META_TITLE: {raw_title}",
                        f"META_SOURCE_URL: {original_url}",
                        "META_SOURCE_KIND: url",
                        "META_INTAKE_TYPE: link",
                    ),
                )
                logging.info("Writing metadata block to text input")
                _ = pathlib.Path(output_filename).write_text(metadata_block + "\n\n" + webpage_text, encoding="utf-8")
            msg_uid = msg.uid
            if msg_uid is not None:
                flags = MailMessageFlags.SEEN
                _ = mailbox.flag(msg_uid, flags, True)  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
            else:
                logging.warning("msg_uid is None for message '%s' — cannot mark as seen", subject_raw)


if __name__ == "__main__":
    main()
