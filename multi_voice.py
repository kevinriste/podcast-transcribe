"""Multi-voice TTS prototype.

Parses newsletter HTML to split content into narration and quote
segments (via <blockquote> and border-left styling), synthesizes
each with a different Gemini Pro TTS voice, and stitches into MP3.

Supports both raw HTML (source.html) and E-C markers as input.

Usage:
    source /etc/profile.d/podcast-transcribe.sh
    source /etc/profile.d/google-gemini.sh
    cd text-to-speech && uv run python3 ../multi_voice.py \
        ../html-comparison/.../source.html --input-format html \
        --podcast
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import re
import sys
import time
import wave
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag
from google import genai
from google.cloud import texttospeech
from google.genai import types
from pydub import AudioSegment

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

GEMINI_MODEL = "gemini-2.5-pro-preview-tts"
NARRATOR_VOICE = "Kore"
QUOTE_VOICE = "Charon"
WAVENET_NARRATOR = "en-US-Wavenet-F"
WAVENET_QUOTE = "en-US-Wavenet-D"
DROPCASTER_AUDIO_DIR = "../dropcaster-docker/audio"
AUDIO_EFFECTS_PROFILE = "headphone-class-device"

NARRATOR_STYLE = (
    "Read this as a calm, articulate podcast host narrating a newsletter article. "
    "Measured pace, slight warmth, not performative. Prioritize clarity."
)

QUOTE_STYLE = (
    "Read this as if quoting someone else's written words in a podcast. "
    "Slightly different energy from the narrator — a touch more direct and deliberate, "
    "as if presenting an argument or observation that deserves attention. "
    "Not a character voice, just a subtle tonal shift."
)


# ---------------------------------------------------------------------------
# Segment parsing
# ---------------------------------------------------------------------------

SKIP_TAGS = frozenset({
    "script", "style", "nav", "footer", "header", "noscript",
    "svg", "iframe", "form", "button", "input", "select",
    "textarea", "meta", "link", "img", "head",
})

CONTAINER_TAGS = frozenset({
    "div", "section", "article", "main", "td", "th",
    "table", "tr", "tbody", "html", "body",
})

_ATTRIBUTION_RE = re.compile(
    r"^(.{3,80}?)\s+(?:writes?|wrote|says?|said)\s*,?\s*$",
)


def _is_styled_quote(el: Tag) -> bool:
    """Check if an element is styled as a quote via border-left."""
    style = el.get("style", "")
    if isinstance(style, list):
        style = " ".join(style)
    return "border-left" in style


def _get_text(el: Tag) -> str:
    """Extract clean text from an element."""
    return el.get_text(" ", strip=True)


def _detect_speaker(text: str) -> str:
    """Try to extract speaker name from preceding narration text."""
    lines = text.strip().split("\n")
    for line in reversed(lines):
        line = line.strip()
        match = _ATTRIBUTION_RE.match(line)
        if match:
            return match.group(1)
    return ""


def parse_segments_html(html: str) -> list[dict[str, str]]:
    """Parse HTML into narration and quote segments using <blockquote> and border-left.

    Returns:
        List of dicts with 'type' ('narration' or 'quote'), 'text', and optional 'speaker'.

    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(SKIP_TAGS):
        tag.decompose()

    segments: list[dict[str, str]] = []
    current_narration: list[str] = []

    def flush_narration() -> None:
        joined = " ".join(current_narration).strip()
        joined = re.sub(r"\s+", " ", joined)
        if joined:
            segments.append({"type": "narration", "text": joined, "speaker": ""})
        current_narration.clear()

    def walk(element: Tag) -> None:
        for child in element.children:
            if not isinstance(child, Tag):
                if isinstance(child, NavigableString):
                    text = str(child).strip()
                    if text:
                        current_narration.append(text)
                continue

            name = child.name
            if name in SKIP_TAGS:
                continue

            # Blockquote — definitive quote
            if name == "blockquote":
                flush_narration()
                quote_text = _get_text(child)
                if quote_text:
                    # Look back at most recent narration for speaker attribution
                    speaker = ""
                    if segments and segments[-1]["type"] == "narration":
                        speaker = _detect_speaker(segments[-1]["text"])
                    segments.append({"type": "quote", "text": quote_text, "speaker": speaker})
                continue

            # Border-left styled element — visual quote
            if _is_styled_quote(child) and name in {"p", "td", "div"}:
                text = _get_text(child)
                if text and len(text) > 30:
                    flush_narration()
                    speaker = ""
                    if segments and segments[-1]["type"] == "narration":
                        speaker = _detect_speaker(segments[-1]["text"])
                    segments.append({"type": "quote", "text": text, "speaker": speaker})
                    continue

            # Headings — narration with emphasis
            if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                text = _get_text(child)
                if text:
                    flush_narration()
                    segments.append({"type": "narration", "text": text, "speaker": ""})
                continue

            # Paragraphs — narration
            if name == "p":
                text = _get_text(child)
                if text:
                    current_narration.append(text)
                continue

            # Lists
            if name in {"ul", "ol"}:
                for li in child.find_all("li", recursive=False):
                    text = _get_text(li)
                    if text:
                        current_narration.append(text)
                continue

            # Container — recurse
            if name in CONTAINER_TAGS:
                walk(child)

    walk(soup)
    flush_narration()
    return segments


def parse_segments_markers(text: str) -> list[dict[str, str]]:
    """Parse E-C marker text into narration and quote segments (fallback).

    Returns:
        List of dicts with 'type' ('narration' or 'quote'), 'text', and optional 'speaker'.

    """
    lines = text.split("\n")
    segments: list[dict[str, str]] = []
    current_narration: list[str] = []
    current_speaker = ""

    def flush_narration() -> None:
        joined = "\n".join(current_narration).strip()
        if joined:
            segments.append({"type": "narration", "text": joined, "speaker": ""})
        current_narration.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("[QUOTE]"):
            flush_narration()
            quote_text = stripped[len("[QUOTE]"):].strip()
            i += 1
            while i < len(lines) and lines[i].strip():
                quote_text += " " + lines[i].strip()
                i += 1
            segments.append({"type": "quote", "text": quote_text, "speaker": current_speaker})
            continue

        match = _ATTRIBUTION_RE.match(stripped)
        if match:
            flush_narration()
            current_speaker = match.group(1)
            segments.append({"type": "narration", "text": stripped, "speaker": ""})
            i += 1
            continue

        current_narration.append(line)
        i += 1

    flush_narration()
    return segments


# ---------------------------------------------------------------------------
# Gemini TTS synthesis
# ---------------------------------------------------------------------------


def synthesize_segment(
    client: genai.Client,
    text: str,
    voice: str,
    style: str,
    label: str,
) -> AudioSegment | None:
    """Synthesize one segment via Gemini Pro TTS.

    Returns:
        pydub AudioSegment, or None on failure.

    """
    try:
        logging.info("  [%s] Synthesizing %d chars with voice %s...", label, len(text), voice)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"{style}\n\n{text}",
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=voice,
                        ),
                    ),
                ),
            ),
        )

        audio_data = b""
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith("audio/"):
                audio_data += part.inline_data.data

        if not audio_data:
            logging.warning("  [%s] No audio returned", label)
            return None

        # Convert raw PCM to AudioSegment
        wav_io = io.BytesIO()
        with wave.open(wav_io, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(audio_data)
        wav_io.seek(0)
        return AudioSegment.from_wav(wav_io)

    except Exception:
        logging.exception("  [%s] Synthesis failed", label)
        return None


# ---------------------------------------------------------------------------
# WaveNet TTS synthesis
# ---------------------------------------------------------------------------


def synthesize_segment_wavenet(
    tts_client: texttospeech.TextToSpeechClient,
    text: str,
    voice_name: str,
    label: str,
) -> AudioSegment | None:
    """Synthesize one segment via Google Cloud WaveNet.

    Returns:
        pydub AudioSegment, or None on failure.

    """
    try:
        logging.info("  [%s] Synthesizing %d chars with WaveNet %s...", label, len(text), voice_name)
        synth_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(language_code="en-US", name=voice_name)
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            effects_profile_id=[AUDIO_EFFECTS_PROFILE],
        )
        response = tts_client.synthesize_speech(input=synth_input, voice=voice, audio_config=audio_config)
        return AudioSegment.from_mp3(io.BytesIO(response.audio_content))
    except Exception:
        logging.exception("  [%s] WaveNet synthesis failed", label)
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Build a multi-voice podcast episode."""
    arg_parser = argparse.ArgumentParser(description="Multi-voice TTS from HTML or markers")
    arg_parser.add_argument("input_file", help="Path to source HTML or E-C marker text file")
    arg_parser.add_argument("--input-format", default="html", choices=["html", "markers"],
                            help="Input format: html (source.html) or markers (E-C text)")
    arg_parser.add_argument("--engine", default="gemini-pro", choices=["gemini-pro", "gemini-flash", "wavenet"],
                            help="TTS engine (default: gemini-pro)")
    arg_parser.add_argument("--narrator-voice", default=None, help="Narrator voice (default: per engine)")
    arg_parser.add_argument("--quote-voice", default=None, help="Quote voice (default: per engine)")
    arg_parser.add_argument("--output", default="./multi_voice_output.mp3", help="Output MP3 path")
    arg_parser.add_argument("--podcast", action="store_true", help="Also drop into dropcaster audio dir")
    arg_parser.add_argument("--max-chars", type=int, default=0, help="Truncate total chars (0 = full)")
    args = arg_parser.parse_args()

    # Set voice defaults per engine
    if args.engine == "wavenet":
        narrator_voice = args.narrator_voice or WAVENET_NARRATOR
        quote_voice = args.quote_voice or WAVENET_QUOTE
    else:
        narrator_voice = args.narrator_voice or NARRATOR_VOICE
        quote_voice = args.quote_voice or QUOTE_VOICE

    # Initialize clients
    gemini_client = None
    tts_client = None

    if args.engine in {"gemini-pro", "gemini-flash"}:
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            logging.error("GEMINI_API_KEY required for Gemini engine")
            sys.exit(1)
        gemini_client = genai.Client(api_key=api_key)
    else:
        tts_client = texttospeech.TextToSpeechClient()

    text = Path(args.input_file).read_text(encoding="utf-8")

    if args.max_chars > 0:
        text = text[:args.max_chars]

    if args.input_format == "html":
        segments = parse_segments_html(text)
    else:
        segments = parse_segments_markers(text)
    logging.info("Parsed %d segments: %d narration, %d quotes (engine: %s)",
                 len(segments),
                 sum(1 for s in segments if s["type"] == "narration"),
                 sum(1 for s in segments if s["type"] == "quote"),
                 args.engine)

    for i, seg in enumerate(segments):
        speaker_info = f" (speaker: {seg['speaker']})" if seg["speaker"] else ""
        logging.info("  Segment %d: %s, %d chars%s", i + 1, seg["type"], len(seg["text"]), speaker_info)

    logging.info("Voices: narrator=%s, quote=%s", narrator_voice, quote_voice)

    # Synthesize each segment
    audio_parts: list[AudioSegment] = []
    pause_between = AudioSegment.silent(duration=400)

    gemini_model = GEMINI_MODEL if args.engine == "gemini-pro" else "gemini-2.5-flash-preview-tts"

    for i, seg in enumerate(segments):
        is_quote = seg["type"] == "quote"
        voice = quote_voice if is_quote else narrator_voice
        label = f"{'quote' if is_quote else 'narr'}-{i + 1}"

        if args.engine == "wavenet":
            audio = synthesize_segment_wavenet(tts_client, seg["text"], voice, label)
        else:
            style = QUOTE_STYLE if is_quote else NARRATOR_STYLE
            audio = synthesize_segment(gemini_client, seg["text"], voice, style, label)

        if audio:
            if audio_parts:
                audio_parts.append(pause_between)
            audio_parts.append(audio)
        time.sleep(0.1 if args.engine == "wavenet" else 0.5)

    if not audio_parts:
        logging.error("No audio segments produced")
        sys.exit(1)

    # Stitch
    combined = audio_parts[0]
    for part in audio_parts[1:]:
        combined += part

    # Export MP3
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    _ = combined.export(buf, format="mp3")
    out_path.write_bytes(buf.getvalue())
    logging.info("Output: %s (%d bytes, %.1f seconds)", out_path, out_path.stat().st_size, len(combined) / 1000)

    if args.podcast:
        from podcast_shared import apply_id3_tags

        dropcaster_path = Path(DROPCASTER_AUDIO_DIR) / f"TEST-MULTIVOICE-{args.engine}.mp3"
        dropcaster_path.parent.mkdir(parents=True, exist_ok=True)
        dropcaster_path.write_bytes(buf.getvalue())
        title = f"[TEST-MULTIVOICE-{args.engine}] {narrator_voice}+{quote_voice}"
        apply_id3_tags(str(dropcaster_path), title=title, description="Multi-voice TTS test", source_url="", v1=1)
        logging.info("Podcast episode: %s", dropcaster_path)


if __name__ == "__main__":
    main()
