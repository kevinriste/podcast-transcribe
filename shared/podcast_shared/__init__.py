# ruff: noqa: RUF067
"""Shared utilities for the podcast-transcribe pipeline."""

import logging
import os
import re
from datetime import datetime, timedelta

import requests
from google import genai
from mutagen.id3 import ID3
from mutagen.id3._frames import TIT2, TT3, WXXX  # noqa: PLC2701
from mutagen.id3._util import ID3NoHeaderError  # noqa: PLC2701

from podcast_shared.aside_render import render_block_aside as render_block_aside
from podcast_shared.aside_render import resolve_markers as resolve_markers
from podcast_shared.aside_render import serialize_flat as serialize_flat
from podcast_shared.describe import describe_image as describe_image
from podcast_shared.describe import enrich_images as enrich_images
from podcast_shared.intake_store import slug_source as slug_source
from podcast_shared.intake_store import store_intake_html as store_intake_html
from podcast_shared.podly import enable_post_in_podly as enable_post_in_podly
from podcast_shared.podly import get_podly_config as get_podly_config
from podcast_shared.structural_extract import ASIDE_MARKER as ASIDE_MARKER
from podcast_shared.structural_extract import BLOCKQUOTE_MARKER as BLOCKQUOTE_MARKER
from podcast_shared.structural_extract import EMBED_MARKER_PREFIX as EMBED_MARKER_PREFIX
from podcast_shared.structural_extract import EMBED_MARKER_SUFFIX as EMBED_MARKER_SUFFIX
from podcast_shared.structural_extract import Block as Block
from podcast_shared.structural_extract import block_from_dict as block_from_dict
from podcast_shared.structural_extract import extract_blocks as extract_blocks
from podcast_shared.structural_extract import find_content_region as find_content_region
from podcast_shared.structural_extract import find_content_region_matched as find_content_region_matched
from podcast_shared.structural_extract import serialize_blocks as serialize_blocks

logger = logging.getLogger(__name__)

SUMMARY_MODEL = "gemini-3.1-flash-lite"

# BLOCKQUOTE_MARKER and ASIDE_MARKER are defined in the leaf module structural_extract
# (so aside_render can import them without a circular dependency) and re-exported above;
# every `from podcast_shared import BLOCKQUOTE_MARKER` keeps working unchanged.

_gemini_client: genai.Client | None = None


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------


def get_gemini_client() -> genai.Client:
    """Return the singleton Gemini client, initializing on first call.

    Returns:
        The shared Gemini client instance.

    """
    global _gemini_client  # noqa: PLW0603
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _gemini_client


# ---------------------------------------------------------------------------
# Gotify notifications
# ---------------------------------------------------------------------------


def send_gotify_notification(title: str, message: str, priority: int = 6) -> None:
    """Send a push notification via Gotify."""
    # Intentionally no error handling here. Gotify is the alerting mechanism --
    # if Gotify itself is down, logging that fact just goes to a log file nobody
    # watches. The alternative (wrapping in try/except + logging.error) gives a
    # false sense of safety without actually reaching the user.
    gotify_server = os.environ.get("GOTIFY_SERVER")
    gotify_token = os.environ.get("GOTIFY_TOKEN")
    if not gotify_server or not gotify_token:
        logger.warning("Gotify env vars not set; skipping notification.")
        return
    gotify_url = f"{gotify_server}/message?token={gotify_token}"
    data = {"title": title, "message": message, "priority": priority}
    _ = requests.post(gotify_url, data=data, timeout=30)


# ---------------------------------------------------------------------------
# Metadata parsing
# ---------------------------------------------------------------------------


def split_metadata(raw_text: str) -> tuple[dict[str, str], str]:
    """Parse META_ headers from a text file into a metadata dict and content body.

    Returns:
        A (metadata, content) tuple.

    """
    if not raw_text.startswith("META_"):
        return {}, raw_text
    logger.info("Parsing metadata header")
    lines = raw_text.splitlines()
    metadata: dict[str, str] = {}
    current_key: str | None = None
    content_start = len(lines)
    for idx, line in enumerate(lines):
        if line.startswith("META_"):
            if ":" not in line:
                content_start = idx
                break
            key, value = line.split(":", 1)
            current_key = key.replace("META_", "").lower()
            metadata[current_key] = value.strip()
            continue
        if line.startswith((" ", "\t")) and current_key:
            metadata[current_key] = f"{metadata.get(current_key, '')} {line.strip()}".strip()
            continue
        if not line.strip():
            content_start = idx + 1
            break
        content_start = idx
        break
    content = "\n".join(lines[content_start:]) if content_start < len(lines) else ""
    return metadata, content


# ---------------------------------------------------------------------------
# Gemini summaries
# ---------------------------------------------------------------------------


def generate_summary(text: str, title: str) -> str:
    """Generate a 2-3 sentence article summary via Gemini.

    Returns:
        The summary text, or empty string on failure.

    """
    if not text.strip():
        logger.info("Summary skipped: empty content")
        return ""
    logger.info("Generating summary via Gemini")
    prompt = (
        "Summarize the article in 2-3 sentences. Focus on key points and keep it concise.\n\n"
        f"Title: {title}\n\nArticle:\n{text}"
    )
    try:
        client = get_gemini_client()
        response = client.models.generate_content(  # pyright: ignore[reportUnknownMemberType]
            model=SUMMARY_MODEL,
            contents=prompt,
        )
        if response.text is None:
            logger.warning("Gemini returned no text for summary")
            return ""
        logger.info("Summary generated")
        return response.text.strip()
    except Exception:
        logger.exception("Summary generation failed")
        return ""


# ---------------------------------------------------------------------------
# ID3 tagging
# ---------------------------------------------------------------------------


def apply_id3_tags(
    mp3_path: str,
    *,
    title: str,
    description: str,
    source_url: str,
    v1: int = 2,
) -> None:
    """Write ID3 tags (title, description, source URL) to an MP3 file."""
    logger.info("Writing ID3 tags to MP3")
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()
    if title:
        tags.add(TIT2(encoding=3, text=title))  # pyright: ignore[reportUnknownMemberType]
    if description:
        tags.add(TT3(encoding=3, text=description))  # pyright: ignore[reportUnknownMemberType]
    if source_url:
        tags.add(WXXX(encoding=3, desc="Source", url=source_url))  # pyright: ignore[reportUnknownMemberType]
    tags.save(mp3_path, v1=v1)  # pyright: ignore[reportUnknownMemberType]


def set_file_pub_date(path: str, pub_date: datetime) -> None:
    """Set a file's mtime to pub_date so Dropcaster orders episodes by it.

    Dropcaster derives an episode's pubDate from the ID3 TRDA frame, but mutagen
    cannot write TRDA, so it falls back to the file's modification time. Setting
    mtime explicitly makes feed ordering deterministic (independent of the order
    files happen to be synthesized in).
    """
    ts = pub_date.timestamp()
    os.utime(path, (ts, ts))


def pub_date_from_filename(stem: str) -> datetime | None:
    """Parse a leading ``YYYYMMDD-HHMMSS`` filename prefix into a naive local datetime.

    Every intake path (imap, rss, archive) names its output with this prefix, built
    from the source's own date (email date, feed ``published``, post date). The prefix
    survives the pipeline, so it is the original-receipt fallback for episode ordering
    when no explicit ``META_PUB_DATE`` header is present.

    Returns:
        The parsed datetime, or None if the stem has no valid date prefix.

    """
    match = re.match(r"^(\d{8})-(\d{6})", stem)
    if not match:
        return None
    try:
        return datetime.strptime(f"{match.group(1)}-{match.group(2)}", "%Y%m%d-%H%M%S")  # noqa: DTZ007
    except ValueError:
        return None


# A "Highlights From The Comments" episode is placed this long after the article it
# discusses so it sorts immediately after its friend. Small enough never to leapfrog a
# closely following episode. Shared by every producer/re-orderer of comment episodes.
COMMENT_OFFSET = timedelta(seconds=60)

_PUBLISHED_STAMP_RE = re.compile(r"(\d{8})-(\d{6})")


def stamp_from_name(stem: str) -> datetime | None:
    """Extract the first embedded ``YYYYMMDD-HHMMSS`` stamp from a *published* filename.

    Unlike ``pub_date_from_filename`` (anchored, for clean intake stems), published
    names carry the stamp after the source prefix (e.g. ``Source- 20130502-120000- Title``),
    so it is searched for anywhere.

    Returns:
        The parsed naive-local datetime, or None if no valid stamp is present.

    """
    match = _PUBLISHED_STAMP_RE.search(stem)
    if not match:
        return None
    try:
        return datetime.strptime(f"{match.group(1)}-{match.group(2)}", "%Y%m%d-%H%M%S")  # noqa: DTZ007
    except ValueError:
        return None


def is_comment_filename(stem: str) -> bool:
    """Detect whether a published filename is a comment-highlights episode.

    Returns:
        True if the filename carries the ``COMMENTS-`` marker.

    """
    return "COMMENTS-" in stem


def title_key(stem: str) -> str:
    """Reduce a published filename to the title shared by an article and its comment episode.

    Strips the trailing ``-YYYYMMDD`` render date, the leading ``<source>- <stamp>- ``
    prefix, and a comment's ``COMMENTS-`` marker. Pairing a comment to its article keys
    on this value.

    Returns:
        The shared title portion of the filename.

    """
    key = re.sub(r"-\d{8}$", "", stem)
    key = re.sub(r"^.*?\d{8}-\d{6}-\s*", "", key)
    key = re.sub(r"^COMMENTS-\s*", "", key)
    return key.strip()
