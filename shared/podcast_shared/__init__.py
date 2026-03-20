# ruff: noqa: RUF067
"""Shared utilities for the podcast-transcribe pipeline."""

import logging
import os

import requests
from google import genai
from mutagen.id3 import ID3
from mutagen.id3._frames import TIT2, TT3, WXXX  # noqa: PLC2701
from mutagen.id3._util import ID3NoHeaderError  # noqa: PLC2701

logger = logging.getLogger(__name__)

SUMMARY_MODEL = "gemini-3.1-flash-lite-preview"

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
    requests.post(gotify_url, data=data, timeout=30)


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
        response = client.models.generate_content(
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
        tags.add(TIT2(encoding=3, text=title))
    if description:
        tags.add(TT3(encoding=3, text=description))
    if source_url:
        tags.add(WXXX(encoding=3, desc="Source", url=source_url))
    tags.save(mp3_path, v1=v1)
