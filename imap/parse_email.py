"""Fetch unseen Gmail messages and write raw text files for the pipeline."""

import logging
import os
import pathlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

if TYPE_CHECKING:
    from yt_dlp import _Params  # pyright: ignore[reportPrivateUsage]

import yaml
import yt_dlp
from bs4 import BeautifulSoup
from imap_tools.consts import MailMessageFlags
from imap_tools.mailbox import MailBox
from imap_tools.message import MailMessage
from imap_tools.query import AND
from playwright.sync_api import sync_playwright
from podcast_shared import (
    BLOCKQUOTE_MARKER,
    apply_id3_tags,
    generate_summary,
    send_gotify_notification,
    store_intake_html,
)
from trafilatura import bare_extraction, extract

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

output_folder = "../prepare-text/text-input-raw"
gmail_user = os.getenv("GMAIL_PODCAST_ACCOUNT")
gmail_password = os.getenv("GMAIL_PODCAST_ACCOUNT_APP_PASSWORD")
sources_config_file = "sources.yaml"
DEFAULT_GENERAL_SCRAPER = "http://localhost:3001/fetch"
DEFAULT_AUTHENTICATED_SCRAPER = "http://localhost:3002/fetch"


@dataclass(slots=True)
class CustomSource:
    """A publisher-specific email source matched by sender, beyond Beehiiv/Substack.

    Built-in platform detection (Beehiiv via the x-beehiiv-ids header, Substack as
    the default) covers most newsletters; custom sources handle publishers that
    need their own link-finding or HTML-body extraction.
    """

    kind: str
    match_sender: tuple[str, ...]
    use_html_body: bool
    web_version_link_text: tuple[str, ...]
    source_url_includes: tuple[str, ...]
    source_url_excludes: tuple[str, ...]


@dataclass(slots=True)
class ScraperConfig:
    """Scraper endpoints for the "link" intake."""

    general_url: str
    authenticated_url: str
    authenticated_domains: tuple[str, ...]


@dataclass(slots=True)
class ImapConfig:
    """Loaded imap configuration: custom sources plus scraper endpoints."""

    sources: list[CustomSource]
    scrapers: ScraperConfig


class SourceEntry(TypedDict, total=False):
    """A single entry under sources: in sources.yaml."""

    kind: str
    match_sender: list[str]
    use_html_body: bool
    web_version_link_text: list[str]
    source_url_includes: list[str]
    source_url_excludes: list[str]


class ScrapersEntry(TypedDict, total=False):
    """The scrapers: section of sources.yaml."""

    general_url: str
    authenticated_url: str
    authenticated_domains: list[str]


class SourcesConfig(TypedDict, total=False):
    """The sources.yaml document."""

    sources: list[SourceEntry]
    scrapers: ScrapersEntry


def load_imap_config() -> ImapConfig:
    """Load custom sources and scraper endpoints from sources.yaml.

    Returns:
        The parsed config; empty sources and default scraper endpoints when the
        file or a section is absent.

    """
    default_scrapers = ScraperConfig(DEFAULT_GENERAL_SCRAPER, DEFAULT_AUTHENTICATED_SCRAPER, ())
    config_path = pathlib.Path(sources_config_file)
    if not config_path.exists():
        return ImapConfig(sources=[], scrapers=default_scrapers)
    config: SourcesConfig = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    sources = [
        CustomSource(
            kind=entry.get("kind", "custom"),
            match_sender=tuple(s.casefold() for s in entry.get("match_sender", [])),
            use_html_body=entry.get("use_html_body", False),
            web_version_link_text=tuple(normalize_text(s) for s in entry.get("web_version_link_text", [])),
            source_url_includes=tuple(entry.get("source_url_includes", [])),
            source_url_excludes=tuple(entry.get("source_url_excludes", [])),
        )
        for entry in config.get("sources", [])
    ]
    scraper_entry = config.get("scrapers", {})
    scrapers = ScraperConfig(
        general_url=scraper_entry.get("general_url", DEFAULT_GENERAL_SCRAPER),
        authenticated_url=scraper_entry.get("authenticated_url", DEFAULT_AUTHENTICATED_SCRAPER),
        authenticated_domains=tuple(scraper_entry.get("authenticated_domains", [])),
    )
    return ImapConfig(sources=sources, scrapers=scrapers)


def match_custom_source(sources: list[CustomSource], from_email: str, from_name: str) -> CustomSource | None:
    """Return the first custom source whose match_sender matches the sender.

    Returns:
        The matching CustomSource, or None when no custom source applies.

    """
    sender = f"{from_email} {from_name}".casefold()
    for source in sources:
        if any(needle in sender for needle in source.match_sender):
            return source
    return None


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


_BLOCK_TAGS = ("p", "li", "blockquote", "h1", "h2", "h3", "h4")
_LIST_MARKER_RE = re.compile(r"^\s*[*•\-]\s+")


def extract_body_from_html(msg: MailMessage) -> str | None:
    """Extract readable body text from an email's HTML part.

    Returns:
        The extracted body text, or None if there is no HTML or no text.

    """
    return extract_body_text(msg.html)


def extract_body_text(html: str | None) -> str | None:
    """Extract readable body text from an HTML string.

    Walks block-level elements and joins them with blank lines, preserving
    paragraph and list structure. Text within each block is extracted without
    stripping (then whitespace-normalised) so that whitespace around inline
    hyperlinks is retained — this is what prevents anchor text from fusing onto
    adjacent words, the root cause of some publishers' plain-text word-joins.
    Block quotations (the element, or a paragraph inside one) are prefixed with
    ``BLOCKQUOTE_MARKER`` so later stages can voice them distinctly.

    Returns:
        The extracted body text, or None if there is no HTML or no text.

    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["style", "script", "head"]):  # pyright: ignore[reportAny]
        tag.decompose()  # pyright: ignore[reportAny]
    blocks: list[str] = []
    previous: str | None = None
    for element in soup.find_all(_BLOCK_TAGS):  # pyright: ignore[reportAny]
        if element.find(_BLOCK_TAGS):  # pyright: ignore[reportAny] — skip blocks nested in blocks (dedupe table nesting)
            continue
        text = " ".join(element.get_text("").split())  # pyright: ignore[reportAny]
        text = _LIST_MARKER_RE.sub("", text)
        if not text or text == previous:  # drop empties and consecutive duplicates
            continue
        previous = text
        # Mark quoted passages (the block, or a paragraph inside one) so downstream
        # stages can voice them distinctly; compare unmarked text above for dedupe.
        is_quote = element.name == "blockquote" or element.find_parent("blockquote") is not None  # pyright: ignore[reportAny]
        blocks.append(f"{BLOCKQUOTE_MARKER}{text}" if is_quote else text)
    if not blocks:
        return None
    return "\n\n".join(blocks)


def find_source_url(
    links: list[dict[str, str]],
    source_kind: str,
    subject: str,
    custom: CustomSource | None = None,
) -> str:
    """Find the best source URL from email links based on the newsletter platform.

    Built-in handling covers Beehiiv and Substack; a matched ``custom`` source
    uses its configured web-version link text plus include/exclude href filters.

    Returns:
        The source URL, or empty string if none found.

    """
    subject_norm = normalize_text(subject)
    logging.info("Selecting source URL for %s email", source_kind)
    if custom is not None:
        for link in links:
            if normalize_text(link["text"]) in custom.web_version_link_text:
                logging.info("Found %s web-version link", source_kind)
                return link["href"]
        for link in links:
            href = link["href"]
            if any(inc in href for inc in custom.source_url_includes) and not any(
                exc in href for exc in custom.source_url_excludes
            ):
                logging.info("Found %s link: %s", source_kind, href)
                return href
        return ""
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


def fetch_and_process_html(
    url: str, request_body: dict[str, str] | None = None
) -> tuple[object | None, str | None, str | None]:
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
    tuple[object | None, str | None, str | None]
        ``(trafilatura_metadata, extracted_text, raw_page_html)`` on success, or
        ``(None, None, None)`` when the page could not be fetched or parsed.

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
            return None, None, None

        trafilatura_result: object | None = bare_extraction(
            html_content,
            with_metadata=True,
        )
        if trafilatura_result is None:
            logging.error("trafilatura returned no metadata for %s", url)
            return None, None, None
        webpage_text: str = str(extract(html_content, include_comments=False, favor_recall=True) or "")
        title: str = extract_title(trafilatura_result)
        content_text: str = title + ".\n" + "\n" + webpage_text

        return trafilatura_result, content_text, html_content

    except Exception:
        logging.exception("Error occurred")
        return None, None, None


def main() -> None:
    """Fetch unseen emails and route them through the intake pipeline."""
    if not gmail_user or not gmail_password:
        logging.error("Gmail credentials not set")
        return
    imap_config = load_imap_config()
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
                    custom_source: CustomSource | None = None
                    if has_beehiiv:
                        source_kind = "beehiiv"
                    elif (
                        custom_source := match_custom_source(imap_config.sources, from_email, from_name_raw)
                    ) is not None:
                        source_kind = custom_source.kind
                        if custom_source.use_html_body:
                            # Some publishers fuse hyperlink anchors onto adjacent words in
                            # the plain-text part; extract from HTML, falling back to plain.
                            html_body = extract_body_from_html(msg)
                            if html_body:
                                email_text_raw = html_body
                            else:
                                logging.warning("No HTML body for %s email; using plain text", source_kind)
                    else:
                        source_kind = "substack"
                        # Substack's plain-text part is lossy (truncated) and flattens block
                        # quotes; extract from HTML to recover full content and mark quotes,
                        # falling back to plain text when there is no HTML part.
                        html_body = extract_body_from_html(msg)
                        if html_body:
                            email_text_raw = html_body
                        else:
                            logging.warning("No HTML body for %s email; using plain text", source_kind)
                    all_links = extract_links_from_email(msg)
                    source_url = find_source_url(all_links, source_kind, subject_raw, custom_source)
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
                    _ = store_intake_html(
                        source=from_name_raw,
                        episode_id=date_stamp,
                        html=msg.html or "",
                        url=source_url,
                        intake_type="email",
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
                    scrapers = imap_config.scrapers
                    use_authenticated = any(domain in original_url for domain in scrapers.authenticated_domains)
                    scraper_url = scrapers.authenticated_url if use_authenticated else scrapers.general_url
                    html_content_parsed_for_title, webpage_text, raw_page_html = fetch_and_process_html(
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
                    _ = store_intake_html(
                        source=from_name_raw,
                        episode_id=date_stamp,
                        html=raw_page_html or "",
                        url=original_url,
                        intake_type="link",
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
