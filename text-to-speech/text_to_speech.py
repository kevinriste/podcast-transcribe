"""Convert cleaned text files to MP3 podcast episodes via Google Cloud TTS or Gemini TTS.

Per-source narrator selection is configured in narrators.yaml (see
narrators.example.yaml). Sources routed to a Gemini engine are synthesized
through the Gemini Batch API: each run first collects finished batch jobs from
batch-pending/, then submits new ones. Everything else is synthesized
synchronously via Google Cloud TTS as before.
"""

import functools
import io
import json
import logging
import operator
import pathlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict

import yaml
from google.cloud import texttospeech
from google.genai import types as genai_types
from podcast_shared import (
    apply_id3_tags,
    generate_summary,
    get_gemini_client,
    send_gotify_notification,
    set_file_pub_date,
    split_metadata,
)
from pydub import AudioSegment

from multivoice import (
    DEFAULT_NARRATOR_VOICE,
    DEFAULT_QUOTE_POOL,
    parse_segments,
    plan_article_utterances,
    plan_utterances,
    render_utterances,
    strip_markers,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

input_dir = "../prepare-text/text-input-cleaned"
final_output_dir = "../dropcaster-docker/audio"
batch_pending_dir = "batch-pending"
narrator_config_file = "narrators.yaml"
stats_dir = "stats"

# --- Feed routing (two feeds) -------------------------------------------------
# The optional "evergreen" feed collects long-form/backlog episodes. It lives in
# a subdirectory of the main audio dir (Dropcaster globs *.mp3 non-recursively,
# so the main feed never picks these up) and is served at <domain>/<dir_name>.
# Everything else goes to the default ("topical") feed. Routing is config-driven
# via the evergreen_feed: section of narrators.yaml; when no sources are
# configured, every episode stays in the topical feed.
DEFAULT_EVERGREEN_DIR = "evergreen"

# Google Cloud TTS API limit is 5000 bytes per request (not characters)
WAVENET_MIN_STEP_BYTES = 3000
WAVENET_MAX_STEP_BYTES = 5000
# Gemini TTS accepts much larger inputs; bigger chunks keep prosody continuous
GEMINI_MIN_STEP_BYTES = 8000
GEMINI_MAX_STEP_BYTES = 12000
# Gemini TTS returns raw signed 16-bit little-endian mono PCM at 24 kHz
GEMINI_PCM_FRAME_RATE = 24000
GEMINI_PCM_SAMPLE_WIDTH = 2

GEMINI_TTS_MODELS = {
    "gemini-flash": "gemini-2.5-flash-preview-tts",
    "gemini-pro": "gemini-2.5-pro-preview-tts",
    "gemini-3.1-flash": "gemini-3.1-flash-tts-preview",
}
DEFAULT_GEMINI_VOICE = "Sulafat"

BATCH_JOB_RUNNING_STATES = frozenset({
    genai_types.JobState.JOB_STATE_UNSPECIFIED,
    genai_types.JobState.JOB_STATE_QUEUED,
    genai_types.JobState.JOB_STATE_PENDING,
    genai_types.JobState.JOB_STATE_RUNNING,
    genai_types.JobState.JOB_STATE_PAUSED,
    genai_types.JobState.JOB_STATE_UPDATING,
})


INTAKE_TYPE_LABELS = {
    "email": "Email",
    "rss": "RSS",
    "link": "Link",
    "youtube": "YouTube",
    "archive": "Archive",
    "archive-comments": "Archive comments",
}


@dataclass(slots=True)
class NarratorRule:
    """A single narrator-selection rule from narrators.yaml."""

    match_from: str
    engine: str
    voice: str
    style_prompt: str


class NarratorEntry(TypedDict, total=False):
    """A single entry under narrators: in narrators.yaml."""

    match_from: str
    engine: str
    voice: str
    style_prompt: str


class CommentVoicesConfig(TypedDict, total=False):
    """The comment_voices: section of narrators.yaml."""

    narrator: str
    quote_pool: list[str]
    aside_voice: str


class EvergreenFeedConfig(TypedDict, total=False):
    """The evergreen_feed: section of narrators.yaml."""

    dir_name: str
    whole_sources: list[str]
    length_gated_sources: list[str]
    length_gate_words: int


class NarratorConfig(TypedDict, total=False):
    """The narrators.yaml document."""

    default_voice: str
    narrators: list[NarratorEntry]
    comment_voices: CommentVoicesConfig
    evergreen_feed: EvergreenFeedConfig


@dataclass(slots=True)
class EvergreenRouting:
    """Resolved evergreen-feed routing config from narrators.yaml."""

    output_dir: str
    whole_sources: tuple[str, ...]
    length_gated_sources: tuple[str, ...]
    length_gate_words: int

    @property
    def enabled(self) -> bool:
        """Return True when any source is configured to route to the evergreen feed.

        Returns:
            True if a whole-source or length-gated source is configured.

        """
        return bool(self.whole_sources or self.length_gated_sources)


class UsageStats(TypedDict):
    """Token usage for one Gemini batch TTS episode, written to stats/YYYY-MM-DD.json."""

    timestamp: str
    model: str
    voice: str
    chunks: int
    prompt_tokens: int
    audio_tokens: int
    audio_seconds: float


@dataclass(slots=True)
class DecodedBatch:
    """Audio segments and token usage decoded from a succeeded batch job."""

    segments: list[AudioSegment]
    prompt_tokens: int
    audio_tokens: int


class BatchState(TypedDict, total=False):
    """A batch-pending/<name>.json state file for one in-flight Gemini batch job."""

    job_name: str
    engine: str
    model: str
    voice: str
    txt_file: str
    submitted_at: str


def load_narrator_rules() -> list[NarratorRule]:
    """Load narrator rules from narrators.yaml; missing file means all-Wavenet.

    Returns:
        The configured rules, in order.

    """
    config_path = pathlib.Path(narrator_config_file)
    if not config_path.exists():
        return []
    config: NarratorConfig = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    default_voice = config.get("default_voice", DEFAULT_GEMINI_VOICE)
    rules: list[NarratorRule] = []
    for entry in config.get("narrators", []):
        engine = entry.get("engine", "wavenet")
        if engine != "wavenet" and engine not in GEMINI_TTS_MODELS:
            logging.warning("Ignoring narrator rule with unknown engine %r", engine)
            continue
        rules.append(
            NarratorRule(
                match_from=entry.get("match_from", ""),
                engine=engine,
                voice=entry.get("voice", default_voice),
                style_prompt=entry.get("style_prompt", ""),
            )
        )
    return rules


def resolve_narrator(metadata: dict[str, str], rules: list[NarratorRule]) -> NarratorRule:
    """Pick the narrator rule for a file by matching META_FROM; first match wins.

    Returns:
        The matching rule, or a Wavenet default when nothing matches.

    """
    meta_from = metadata.get("from", "").casefold()
    for rule in rules:
        if rule.match_from and rule.match_from.casefold() in meta_from:
            return rule
    return NarratorRule(match_from="", engine="wavenet", voice="", style_prompt="")


def build_description(
    summary: str, title: str, source_url: str, source_kind: str, source_name: str = "", intake_type: str = ""
) -> str:
    """Build an HTML description string for the MP3 ID3 tag.

    Returns:
        HTML-formatted description with summary, title, and source link.

    """
    description_body = summary or "Summary unavailable."
    title_line = title or "Untitled"
    parts = [description_body, f"Title: {title_line}"]
    if intake_type:
        intake_label = INTAKE_TYPE_LABELS.get(intake_type, intake_type)
        parts.append(f"Via: {intake_label}")
    if source_url:
        display_text = source_url
        if source_kind == "beehiiv" and source_name:
            display_text = source_name
        parts.append(f'Source: <a href="{source_url}">{display_text}</a>')
    return "<br/><br/>".join(parts)


def to_base36(value: int) -> str:
    """Convert a non-negative integer to a base-36 string.

    Returns:
        The base-36 representation.

    """
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append(alphabet[remainder])
    return "".join(reversed(digits))


def chunk_text(content_text: str, min_step_bytes: int, max_step_bytes: int) -> list[str]:
    """Split text into chunks of min-max bytes, breaking at sentence/line boundaries.

    Returns:
        The chunks, in order.

    """
    compiled_regex_for_first_whitespace = re.compile(r"(\r\n|\r|\n|\.)+\s+")
    chunks: list[str] = []
    next_text_starter_position = 0
    while next_text_starter_position < len(content_text):
        remaining = content_text[next_text_starter_position:]
        remaining_bytes = remaining.encode("utf-8")
        if len(remaining_bytes) <= max_step_bytes:
            chunks.append(remaining)
            break
        max_chunk = remaining_bytes[:max_step_bytes].decode("utf-8", errors="ignore")
        min_chars = len(remaining_bytes[:min_step_bytes].decode("utf-8", errors="ignore"))
        match = compiled_regex_for_first_whitespace.search(max_chunk, min_chars)
        if match:
            text_to_process = max_chunk[: match.end()]
        else:
            logging.info("max_step_bytes met before a boundary was found")
            text_to_process = max_chunk
        chunks.append(text_to_process)
        next_text_starter_position += len(text_to_process)
    return chunks


def synthesize_wavenet(content_text: str) -> list[AudioSegment]:
    """Synthesize text synchronously via Google Cloud TTS Wavenet.

    Returns:
        One MP3 audio segment per chunk, in order.

    """
    client = texttospeech.TextToSpeechClient()
    chunks = chunk_text(content_text, WAVENET_MIN_STEP_BYTES, WAVENET_MAX_STEP_BYTES)
    segments: list[AudioSegment] = []
    for counter, text_to_process in enumerate(chunks, start=1):
        synthesis_input = texttospeech.SynthesisInput(text=text_to_process)
        voice = texttospeech.VoiceSelectionParams(
            language_code="en-US",
            name="en-US-Wavenet-F",
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            # plan.md Phase 1: tune output for the primary listening device
            effects_profile_id=["headphone-class-device"],
        )
        logging.info("Synthesizing speech for chunk %s of %s", counter, len(chunks))
        response = client.synthesize_speech(  # pyright: ignore[reportUnknownMemberType]
            request={
                "input": synthesis_input,
                "voice": voice,
                "audio_config": audio_config,
            },
        )
        segments.append(AudioSegment.from_mp3(io.BytesIO(response.audio_content)))
    return segments


def submit_gemini_batch(incoming_filename: pathlib.Path, content_text: str, rule: NarratorRule) -> None:
    """Submit one Gemini Batch API TTS job for a text file and park the file in batch-pending/.

    Raises:
        RuntimeError: If the created batch job has no name to poll by.

    """
    name = incoming_filename.stem
    chunks = chunk_text(content_text, GEMINI_MIN_STEP_BYTES, GEMINI_MAX_STEP_BYTES)
    generate_config = genai_types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=genai_types.SpeechConfig(
            voice_config=genai_types.VoiceConfig(
                prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name=rule.voice),
            ),
        ),
    )
    requests = [
        genai_types.InlinedRequest(
            contents=[
                genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=f"{rule.style_prompt}\n\n{chunk}" if rule.style_prompt else chunk)],
                )
            ],
            config=generate_config,
        )
        for chunk in chunks
    ]
    client = get_gemini_client()
    model = GEMINI_TTS_MODELS[rule.engine]
    logging.info("Submitting %d-chunk %s batch job for %s", len(requests), model, name)
    job = client.batches.create(
        model=model,
        src=requests,
        config=genai_types.CreateBatchJobConfig(display_name=name[:118]),
    )
    if not job.name:
        msg = f"Batch job for {name} was created without a name"
        raise RuntimeError(msg)
    pending_dir = pathlib.Path(batch_pending_dir)
    pending_dir.mkdir(exist_ok=True)
    held_txt = pending_dir / incoming_filename.name
    _ = incoming_filename.rename(held_txt)
    state = {
        "job_name": job.name,
        "engine": rule.engine,
        "model": model,
        "voice": rule.voice,
        "txt_file": held_txt.name,
        "submitted_at": datetime.now(tz=UTC).isoformat(),
    }
    state_path = pending_dir / f"{name}.json"
    _ = state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    logging.info("Batch job %s submitted; state written to %s", job.name, state_path)


def decode_batch_audio(job: genai_types.BatchJob) -> DecodedBatch | None:
    """Decode the PCM audio chunks and token usage from a succeeded batch job's inlined responses.

    Returns:
        One audio segment per request (order preserved) plus summed token counts,
        or None if any chunk is unusable.

    """
    if job.dest is None or not job.dest.inlined_responses:
        logging.error("Batch job %s has no inlined responses", job.name)
        return None
    decoded = DecodedBatch(segments=[], prompt_tokens=0, audio_tokens=0)
    for idx, inlined in enumerate(job.dest.inlined_responses):
        if inlined.error is not None:
            logging.error("Batch job %s chunk %d failed: %s", job.name, idx, inlined.error)
            return None
        response = inlined.response
        candidates = response.candidates if response else None
        content = candidates[0].content if candidates else None
        parts = content.parts if content else None
        data = parts[0].inline_data.data if parts and parts[0].inline_data else None
        if not data:
            logging.error("Batch job %s chunk %d has no audio data", job.name, idx)
            return None
        decoded.segments.append(
            AudioSegment(
                data=data,
                sample_width=GEMINI_PCM_SAMPLE_WIDTH,
                frame_rate=GEMINI_PCM_FRAME_RATE,
                channels=1,
            )
        )
        usage = response.usage_metadata if response else None
        if usage:
            decoded.prompt_tokens += usage.prompt_token_count or 0
            decoded.audio_tokens += usage.candidates_token_count or 0
    return decoded


def record_usage_stats(name: str, state: BatchState, decoded: DecodedBatch) -> None:
    """Merge one episode's token usage into today's stats JSON file."""
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    stats_path = pathlib.Path(stats_dir) / f"{today}.json"
    all_stats: dict[str, UsageStats] = {}
    if stats_path.exists():
        all_stats = json.loads(stats_path.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
    audio_seconds = sum(segment.duration_seconds for segment in decoded.segments)
    all_stats[name] = {
        "timestamp": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "model": state.get("model", ""),
        "voice": state.get("voice", ""),
        "chunks": len(decoded.segments),
        "prompt_tokens": decoded.prompt_tokens,
        "audio_tokens": decoded.audio_tokens,
        "audio_seconds": round(audio_seconds, 1),
    }
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    _ = stats_path.write_text(json.dumps(all_stats, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logging.info(
        "Usage for %s: %d prompt + %d audio tokens for %.0f s of audio",
        name,
        decoded.prompt_tokens,
        decoded.audio_tokens,
        audio_seconds,
    )


def collect_batch_jobs() -> None:
    """Poll pending Gemini batch jobs; finalize finished ones, fall back to Wavenet on failure."""
    pending_dir = pathlib.Path(batch_pending_dir)
    state_files = sorted(pending_dir.glob("*.json"))
    if not state_files:
        return
    client = get_gemini_client()
    for state_path in state_files:
        state: BatchState = json.loads(state_path.read_text(encoding="utf-8")) or {}
        job_name = state.get("job_name", "")
        held_txt = pending_dir / state.get("txt_file", "")
        job = client.batches.get(name=job_name)
        if job.state in BATCH_JOB_RUNNING_STATES:
            logging.info("Batch job %s still %s", job_name, job.state)
            continue
        metadata, content_text = split_metadata(held_txt.read_text(encoding="utf-8"))
        name = held_txt.stem
        segments: list[AudioSegment] | None = None
        if job.state == genai_types.JobState.JOB_STATE_SUCCEEDED:
            decoded = decode_batch_audio(job)
            if decoded is not None:
                segments = decoded.segments
                record_usage_stats(name, state, decoded)
        if segments is None:
            logging.error("Batch job %s ended in state %s; falling back to Wavenet", job_name, job.state)
            send_gotify_notification(
                "TTS batch job failed",
                f"{name}: job {job_name} ended in state {job.state}; falling back to Wavenet.",
            )
            segments = synthesize_wavenet(content_text)
        finalize_episode(name, metadata, content_text, segments)
        held_txt.unlink()
        state_path.unlink()


def load_evergreen_routing() -> EvergreenRouting:
    """Load evergreen-feed routing from the evergreen_feed: section of narrators.yaml.

    Returns:
        The resolved routing; disabled (no sources) when the config or section
        is absent, in which case every episode stays in the topical feed.

    """
    cfg: EvergreenFeedConfig = {}
    config_path = pathlib.Path(narrator_config_file)
    if config_path.exists():
        config: NarratorConfig = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        cfg = config.get("evergreen_feed", {})
    dir_name = cfg.get("dir_name", DEFAULT_EVERGREEN_DIR)
    return EvergreenRouting(
        output_dir=f"{final_output_dir}/{dir_name}",
        whole_sources=tuple(s.casefold() for s in cfg.get("whole_sources", [])),
        length_gated_sources=tuple(s.casefold() for s in cfg.get("length_gated_sources", [])),
        length_gate_words=cfg.get("length_gate_words", 0),
    )


def resolve_feed_dir(meta_from: str, content_text: str, routing: EvergreenRouting) -> str:
    """Pick the output feed directory (topical vs evergreen) for an episode.

    Whole-source matches always route to the evergreen feed; length-gated sources
    route there only when the word count reaches ``length_gate_words``. Everything
    else — and everything, when the evergreen feed is not configured — stays in
    the default feed.

    Returns:
        The output directory path for the resolved feed (``routing.output_dir``
        or ``final_output_dir``).

    """
    if not routing.enabled:
        return final_output_dir
    meta_from_folded = meta_from.casefold()
    if any(source in meta_from_folded for source in routing.whole_sources):
        return routing.output_dir
    if (
        routing.length_gate_words
        and any(source in meta_from_folded for source in routing.length_gated_sources)
        and len(content_text.split()) >= routing.length_gate_words
    ):
        return routing.output_dir
    return final_output_dir


def is_comment_episode(metadata: dict[str, str]) -> bool:
    """Return True when this file is a comment-highlights episode.

    Returns:
        True if the intake type is archive-comments.

    """
    return metadata.get("intake_type", "") == "archive-comments"


# An article needs at least this many block quotes before it is worth rendering with
# distinct quote voices; below it, the extra voices add noise, not signal.
MIN_BLOCKQUOTE_RUNS = 1


def article_multivoice_plan(content_text: str, engine: str) -> list[tuple[str, str]] | None:
    """Return a narrator/quote utterance plan when an article qualifies for multi-voice.

    Only WaveNet episodes qualify (the shared renderer is Google Cloud TTS), and only
    when the body holds at least ``MIN_BLOCKQUOTE_RUNS`` block quotes — so plain essays
    with no quotes stay single-voice.

    Returns:
        The ``(text, speaker)`` plan, or None when the episode should stay single-voice.

    """
    if engine != "wavenet":
        return None
    utterances = plan_article_utterances(content_text)
    quote_runs = sum(1 for _, speaker in utterances if speaker != "NARRATOR")
    return utterances if quote_runs >= MIN_BLOCKQUOTE_RUNS else None


# Voice for embedded-content asides (tweets/images/videos/...) when none is configured.
DEFAULT_ASIDE_VOICE = "en-US-Wavenet-B"


def load_comment_voices() -> tuple[str, list[str], str]:
    """Load the narrator, quote-voice pool, and aside voice from narrators.yaml.

    Returns:
        (narrator_voice, quote_pool, aside_voice), falling back to WaveNet defaults
        when the comment_voices section is absent.

    """
    config_path = pathlib.Path(narrator_config_file)
    if not config_path.exists():
        return DEFAULT_NARRATOR_VOICE, DEFAULT_QUOTE_POOL, DEFAULT_ASIDE_VOICE
    config: NarratorConfig = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    cv = config.get("comment_voices", {})
    narrator = cv.get("narrator") or DEFAULT_NARRATOR_VOICE
    quote_pool = cv.get("quote_pool") or DEFAULT_QUOTE_POOL
    aside_voice = cv.get("aside_voice") or DEFAULT_ASIDE_VOICE
    return narrator, quote_pool, aside_voice


def parse_pub_date(metadata: dict[str, str]) -> datetime | None:
    """Parse META_PUB_DATE into a datetime.

    Returns:
        The parsed datetime, or None if absent or unparseable.

    """
    raw = metadata.get("pub_date", "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def finalize_episode(
    name: str,
    metadata: dict[str, str],
    content_text: str,
    segments: list[AudioSegment],
    annotation: str = "",
    summary_override: str = "",
) -> None:
    """Stitch audio segments into the final MP3, generate the summary, and write ID3 tags.

    An annotation (e.g. "gemini-3.1-flash Callirrhoe") is appended to the output
    filename and ID3 title so audition renders are distinguishable in the feed.
    ``summary_override`` supplies a precomputed description summary (used by comment
    episodes to lead their show notes with the original article's summary).
    """
    meta_from = metadata.get("from", "").strip()
    meta_title = metadata.get("title", "").strip()
    meta_source_url = metadata.get("source_url", "").strip()
    meta_source_kind = metadata.get("source_kind", "").strip()
    meta_source_name = metadata.get("source_name", "").strip()
    meta_intake_type = metadata.get("intake_type", "").strip()
    if meta_title or meta_source_url:
        logging.info("Using metadata for summary and description")
    summary = summary_override or generate_summary(content_text, meta_title)
    description = build_description(
        summary,
        meta_title,
        meta_source_url,
        meta_source_kind,
        meta_source_name,
        meta_intake_type,
    )

    logging.info("Stitching together %d audio segments for %s", len(segments), name)
    audio: AudioSegment = functools.reduce(operator.add, segments)  # pyright: ignore[reportAny]

    current_datetime = datetime.now(tz=UTC).strftime("%Y%m%d")
    if annotation:
        annotation_slug = re.sub(r"[^A-Za-z0-9.]+", "-", annotation).strip("-").lower()
        current_datetime = f"{annotation_slug}-{current_datetime}"
    # Filename format: "YYYYMMDD-HHMMSS-<rest>"
    date_match = re.match(r"^(\d{8}-\d{6})-(.+)$", name)
    if date_match:
        date_prefix = date_match.group(1) + "-"
        name_without_date = date_match.group(2)
    else:
        date_prefix = ""
        name_without_date = name
    dash_index = name_without_date.find("-")

    output_dir = resolve_feed_dir(meta_from, content_text, load_evergreen_routing())
    if dash_index != -1:
        output_filename = f"{output_dir}/{name_without_date[: dash_index + 1]} {date_prefix} {name_without_date[dash_index + 1 :]}-{current_datetime}.mp3"
    else:
        output_filename = f"{output_dir}/{name_without_date}-{date_prefix}{current_datetime}.mp3"

    logging.info("Exporting %s", output_filename)
    _ = audio.export(output_filename, format="mp3")
    file_title = pathlib.Path(output_filename).stem
    file_title = re.sub(r"-\d{8}$", "", file_title)
    if meta_from and meta_title:
        now = datetime.now(tz=UTC)
        base36_width = 6 if now.year <= 2037 else 7
        unix_seconds_base36 = to_base36(int(now.timestamp())).zfill(
            base36_width,
        )
        title_for_tag = f"{meta_from}- {unix_seconds_base36}- {meta_title}"
    else:
        title_for_tag = meta_title or file_title
    if annotation:
        title_for_tag = f"{title_for_tag} [{annotation}]"
    apply_id3_tags(output_filename, title=title_for_tag, description=description, source_url=meta_source_url)

    # Set mtime from META_PUB_DATE so Dropcaster orders episodes deterministically
    # (its ID3-TRDA path is unusable via mutagen, so it falls back to file mtime).
    pub_date = parse_pub_date(metadata)
    if pub_date is not None:
        set_file_pub_date(output_filename, pub_date)


def text_to_speech(incoming_filename: str | pathlib.Path, rules: list[NarratorRule]) -> None:
    """Route a single text file to its narrator: synchronous Wavenet or a Gemini batch job."""
    incoming_path = pathlib.Path(incoming_filename)
    logging.info("Synthesizing speech for %s", incoming_path.name)
    input_text_raw = incoming_path.read_text(encoding="utf-8")
    metadata, content_text = split_metadata(input_text_raw)
    if not content_text:
        logging.warning("Skipping %s: file has no content.", incoming_path.name)
        return
    name = incoming_path.stem
    if is_comment_episode(metadata):
        logging.info("Routing %s to multi-voice comment synthesis", incoming_path.name)
        narrator_voice, quote_pool, _ = load_comment_voices()
        article_summary = metadata.get("article_summary", "").strip()
        utterances = plan_utterances(
            parse_segments(content_text),
            metadata.get("from", "").strip(),
            metadata.get("title", "").strip(),
            article_summary,
        )
        segments = render_utterances(utterances, narrator_voice, quote_pool)
        if not segments:
            logging.error("No audio synthesized for comment episode %s; skipping", incoming_path.name)
            send_gotify_notification("Comment episode failed", f"No audio for {incoming_path.name}")
        else:
            finalize_episode(name, metadata, content_text, segments, summary_override=article_summary)
        incoming_path.unlink()
        return
    rule = resolve_narrator(metadata, rules)
    # Articles that embed block quotes get the same multi-voice treatment as comment
    # episodes when they clear the density gate; the marker is stripped for every other
    # path so it is never spoken.
    plan = article_multivoice_plan(content_text, rule.engine)
    clean_content = strip_markers(content_text)
    if plan is not None:
        quote_count = sum(1 for _, speaker in plan if speaker != "NARRATOR")
        logging.info("Routing %s to multi-voice article synthesis (%d quotes)", incoming_path.name, quote_count)
        narrator_voice, quote_pool, aside_voice = load_comment_voices()
        segments = render_utterances(plan, narrator_voice, quote_pool, aside_voice)
        if segments:
            finalize_episode(name, metadata, clean_content, segments)
            incoming_path.unlink()
            return
        logging.error("No audio for multi-voice article %s; falling back to single voice", incoming_path.name)
        send_gotify_notification("Article multi-voice failed", f"Falling back to single voice for {incoming_path.name}")
    if rule.engine in GEMINI_TTS_MODELS:
        logging.info("Routing %s to %s (voice %s)", incoming_path.name, rule.engine, rule.voice)
        submit_gemini_batch(incoming_path, clean_content, rule)
        return
    segments = synthesize_wavenet(clean_content)
    finalize_episode(name, metadata, clean_content, segments)
    logging.info("Removing original text file")
    incoming_path.unlink()


def process_files() -> None:
    """Collect finished Gemini batch jobs, then process all cleaned text files."""
    rules = load_narrator_rules()
    collect_batch_jobs()
    txt_files = sorted(pathlib.Path(input_dir).glob("*.txt"))
    for f in txt_files:
        text_to_speech(f, rules)


if __name__ == "__main__":
    process_files()
