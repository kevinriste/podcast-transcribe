"""Comment extraction and model-generated highlight briefings.

Extracts (author, body) pairs from a post's comment section and asks an OpenAI
Responses API model to curate a speaker-tagged "Highlights From The Comments"
briefing for multi-voice narration. The model is configurable via the
COMMENT_BRIEFING_MODEL environment variable.
"""

import logging
import os
import pathlib
from datetime import datetime, timedelta

import yaml
from bs4 import BeautifulSoup, Tag
from openai import OpenAI, OpenAIError

MIN_COMMENTS = 5
MODEL = os.environ.get("COMMENT_BRIEFING_MODEL", "gpt-5-mini")

CONFIG_FILE = "source.yaml"
DEFAULT_SOURCE_NAME = "Archive"


def load_source_config() -> tuple[str, str]:
    """Read the source display name and posts-file path from source.yaml.

    Returns:
        (source_name, posts_file), falling back to defaults when the config
        file is absent or a key is missing.

    """
    config_path = pathlib.Path(CONFIG_FILE)
    if not config_path.exists():
        return DEFAULT_SOURCE_NAME, "posts.json"
    config: dict[str, object] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    source_name = str(config.get("source_name") or DEFAULT_SOURCE_NAME)
    posts_file = str(config.get("posts_file") or "posts.json")
    return source_name, posts_file


def article_pub_dates(now: datetime) -> tuple[datetime, datetime]:
    """Return (article_pub_date, comment_pub_date) with the comment 60s later.

    Returns:
        A tuple of the article and comment episode publication datetimes.

    """
    return now, now + timedelta(seconds=60)


def comment_metadata_block(
    source_name: str,
    title: str,
    url: str,
    pub_date: datetime,
    article_summary: str,
) -> str:
    """Build the META_ header block for a comment-highlights episode file.

    Returns:
        The newline-joined metadata header block.

    """
    return "\n".join(
        [
            f"META_FROM: {source_name}",
            f"META_TITLE: Comment Highlights: {title}",
            f"META_SOURCE_URL: {url}",
            "META_SOURCE_KIND: archive",
            "META_INTAKE_TYPE: archive-comments",
            f"META_PUB_DATE: {pub_date.isoformat()}",
            f"META_ARTICLE_SUMMARY: {article_summary}",
        ],
    )


# Stage 1: detect the camps (the spine). No quotes here -- just the structure.
ANALYSIS_PROMPT = """You are analyzing the reader comment section of a blog post
to prepare a "Highlights From The Comments" segment in a fair, curatorial spirit.

You are given the post's summary and its comments as an indented thread outline (each line is
"Author: comment", and a reply is indented under the comment it answers).

Map how the commentariat reacted to the post's central claim (anchored to the summary). Output
plain text with two parts:

FRAMING: 2-4 sentences characterizing the overall shape of the reaction -- how the post landed
and the main axis/axes of disagreement. Brief, like a one-paragraph intro.

CAMPS: a numbered list covering EVERY substantive camp, including minority positions and
meta-level reactions (e.g. "this is really just X" or "you've reinvented Y"). For each camp:
  - Name: a short label
  - Description: 1-2 sentences on the position and roughly how much support it had
  - Exemplars: the commenter names whose comments best exemplify this camp

Be honest: do not invent camps, and say plainly when a camp is small. Do NOT write quotes here.

POST TITLE: {title}

POST SUMMARY: {summary}

COMMENTS:
{comments}
"""

# Stage 2: build the spoken sections to fill the spine, with verbatim evidence.
WRITE_PROMPT = """You are writing the spoken "Highlights From The Comments" segment for a blog
post, read aloud as a podcast episode, in a fair, curatorial spirit.

You are given (a) an ANALYSIS of the comment section -- an overall framing plus the camps to
cover, each with exemplar commenters -- and (b) the comments as an indented thread outline.

Write the segment in this STRICT format:
- Plain lines, each beginning with a role label: "NARRATOR: <text>" or
  "QUOTE <commenter name>: <their exact words>". Separate segments with a blank line. No markdown.
- Start with one or more NARRATOR lines conveying the ANALYSIS's framing.
- Then, for EACH camp in the analysis, in order: a NARRATOR line introducing the camp, followed
  by 1 to 3 QUOTE lines supporting it, drawn from that camp's exemplar commenters. Where camps
  oppose each other, you may juxtapose their quotes.
- Every QUOTE must be VERBATIM text from the comments below, attributed to the real commenter.
  Never invent or paraphrase; draw quotes only from the comments provided. Do NOT prepend the
  commenter's name inside a QUOTE line. Neutral, third-person framing; never write in the
  author's first-person voice.

ANALYSIS:
{analysis}

COMMENTS:
{comments}
"""


def _author_of(li: Tag) -> str:
    """Return a comment li's own author name.

    Returns:
        The commenter name, or "Anonymous".

    """
    cite = li.find("cite", class_="fn")
    return cite.get_text(" ", strip=True) if isinstance(cite, Tag) else "Anonymous"


def extract_comments(html: str) -> list[tuple[str, str, int]]:
    """Return (author, body, depth) for each comment in WordPress-style post HTML, in document order.

    Only the comment's OWN body is captured (nested replies are separate entries,
    not concatenated into the parent). ``depth`` is the reply nesting level (0 for
    top-level), so a thread can be rendered as an indented outline where a reply
    sits under the comment it responds to.

    Returns:
        A list of (author, body, depth) tuples, skipping empty-body comments.

    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str, int]] = []
    for li in soup.find_all("li", class_="comment"):
        author = _author_of(li)
        # Own body only: comment-body divs whose nearest comment ancestor is this li.
        own = [d for d in li.find_all("div", class_="comment-body") if d.find_parent("li", class_="comment") is li]
        text = " ".join(d.get_text(" ", strip=True) for d in own).strip()
        if not text:
            continue
        depth = len(li.find_parents("li", class_="comment"))
        out.append((author, text, depth))
    return out


def _post_model(prompt: str) -> str | None:
    """Call the OpenAI Responses API with the prompt.

    Returns:
        The model's output text, or None on any failure.

    """
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        logging.error("OPENAI_API_KEY not set; cannot generate comment briefing")
        return None
    try:
        client = OpenAI(api_key=key, max_retries=6)
        response = client.responses.create(model=MODEL, input=prompt, timeout=300)
    except OpenAIError:
        logging.exception("Comment briefing request failed")
        return None
    text = response.output_text.strip()
    return text or None


def build_briefing(title: str, comments: list[tuple[str, str, int]], article_summary: str) -> str | None:
    """Generate the speaker-tagged comment briefing via a two-stage model pipeline.

    Stage 1 detects the camps (the spine, anchored to the article summary); stage 2
    writes the framing plus per-camp sections with verbatim supporting quotes. The
    stage-1 analysis is passed to stage 2 as text (no parsing needed).

    Returns:
        The briefing text, or None if either stage failed.

    """
    outline = "\n".join(f"{'    ' * depth}{author}: {text}" for author, text, depth in comments)
    analysis = _post_model(ANALYSIS_PROMPT.format(title=title, summary=article_summary, comments=outline))
    if analysis is None:
        return None
    return _post_model(WRITE_PROMPT.format(analysis=analysis, comments=outline))
