"""Multi-voice helpers for comment-highlights episodes.

Parses the speaker-tagged briefing (NARRATOR / QUOTE <name> segments) and renders
it through Google Cloud TTS: the narrator voice speaks framing plus each quote's
attribution ("<name> wrote:"), and a configurable rotating pool of quote voices
reads the verbatim comment text. Voices are configured in narrators.yaml so the
tier can be flipped without code changes.
"""

import hashlib
import io
import logging
import re

from google.api_core.exceptions import InvalidArgument
from google.cloud import texttospeech
from pydub import AudioSegment

# Defaults (overridable via narrators.yaml `comment_voices:`). Narrator matches the
# article default (en-US-Wavenet-F); quote pool is other WaveNet speakers.
DEFAULT_NARRATOR_VOICE = "en-US-Wavenet-F"
DEFAULT_QUOTE_POOL = ["en-US-Wavenet-D", "en-US-Wavenet-C", "en-US-Wavenet-A", "en-US-Wavenet-E"]

_PAUSE_MS = 400
_MAX_TTS_BYTES = 4800  # Google Cloud TTS hard limit is 5000 bytes per request

_QUOTE_RE = re.compile(r"^QUOTE\s+(.+?):\s*(.*)$", re.DOTALL)
_NARRATOR_RE = re.compile(r"^NARRATOR:\s*(.*)$", re.DOTALL)


def parse_segments(body: str) -> list[tuple[str, str]]:
    """Split a speaker-tagged briefing into (role, text) segments.

    Returns:
        One (role, text) tuple per non-blank segment; role is "NARRATOR" or the
        commenter name. Blocks matching neither prefix extend the previous segment.

    """
    segments: list[tuple[str, str]] = []
    for raw_block in re.split(r"\n\s*\n", body.strip()):
        block = raw_block.strip()
        if not block:
            continue
        narrator = _NARRATOR_RE.match(block)
        if narrator:
            segments.append(("NARRATOR", narrator.group(1).strip()))
            continue
        quote = _QUOTE_RE.match(block)
        if quote:
            segments.append((quote.group(1).strip(), quote.group(2).strip()))
            continue
        if segments:
            role, text = segments[-1]
            segments[-1] = (role, f"{text} {block}".strip())
    return segments


def plan_utterances(
    segments: list[tuple[str, str]],
    from_name: str,
    title: str,
    article_summary: str = "",
) -> list[tuple[str, str]]:
    """Expand parsed segments into ordered (text, speaker) utterances.

    The narrator speaks the title/author intro, the original article summary, framing,
    and each quote's attribution ("<name> wrote:"); the commenter speaker reads only
    their words.

    Returns:
        Ordered (text, speaker) pairs; speaker is "NARRATOR" or a commenter name.

    """
    header = f"{from_name}. {title}." if (from_name or title) else ""
    out: list[tuple[str, str]] = []
    if header:
        out.append((header, "NARRATOR"))
    if article_summary.strip():
        out.append((article_summary.strip(), "NARRATOR"))
    for role, text in segments:
        if not text:
            continue
        if role == "NARRATOR":
            out.append((text, "NARRATOR"))
        else:
            out.extend([(f"{role} wrote:", "NARRATOR"), (text, role)])
    if header:
        out.append((header, "NARRATOR"))
    return out


def assign_voice(speaker: str, narrator_voice: str, quote_pool: list[str]) -> str:
    """Return the Google TTS voice for a speaker, stable across runs.

    Returns:
        The narrator voice for "NARRATOR"; otherwise a pool voice chosen by a
        stable hash of the commenter name.

    """
    if speaker == "NARRATOR":
        return narrator_voice
    digest = hashlib.sha256(speaker.encode("utf-8")).digest()
    return quote_pool[digest[0] % len(quote_pool)]


def _chunk(text: str, max_bytes: int) -> list[str]:
    """Split text into <= max_bytes chunks at sentence/space boundaries.

    Returns:
        The chunks, in order.

    """
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    chunks: list[str] = []
    current = ""
    for piece in re.split(r"(?<=[.!?])\s+", text):
        candidate = f"{current} {piece}".strip()
        if len(candidate.encode("utf-8")) > max_bytes and current:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _synth(client: texttospeech.TextToSpeechClient, text: str, voice: str) -> AudioSegment | None:
    """Synthesize one utterance via Google Cloud TTS (chunked if long).

    Returns:
        The audio, or None if nothing was produced.

    """
    parts: list[AudioSegment] = []
    for chunk in _chunk(text, _MAX_TTS_BYTES):
        voice_params = texttospeech.VoiceSelectionParams(language_code="en-US", name=voice)
        synthesis_input = texttospeech.SynthesisInput(text=chunk)
        with_fx = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            effects_profile_id=["headphone-class-device"],
        )
        try:
            response = client.synthesize_speech(  # pyright: ignore[reportUnknownMemberType]
                request={"input": synthesis_input, "voice": voice_params, "audio_config": with_fx},
            )
        except InvalidArgument:
            # Studio / Chirp3-HD voices reject effects profiles; retry plain.
            plain = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
            response = client.synthesize_speech(  # pyright: ignore[reportUnknownMemberType]
                request={"input": synthesis_input, "voice": voice_params, "audio_config": plain},
            )
        parts.append(AudioSegment.from_mp3(io.BytesIO(response.audio_content)))
    if not parts:
        return None
    combined = parts[0]
    for part in parts[1:]:
        combined += part
    return combined


def render_comment_episode(
    utterances: list[tuple[str, str]],
    narrator_voice: str,
    quote_pool: list[str],
) -> list[AudioSegment]:
    """Synthesize planned utterances, each in its assigned voice.

    Returns:
        Audio segments interleaved with short pauses, ready to stitch. Utterances
        that fail to synthesize are skipped.

    """
    client = texttospeech.TextToSpeechClient()
    pause = AudioSegment.silent(duration=_PAUSE_MS)
    out: list[AudioSegment] = []
    for text, speaker in utterances:
        audio = _synth(client, text, assign_voice(speaker, narrator_voice, quote_pool))
        if audio is None:
            logging.warning("No audio for utterance by %s; skipping", speaker)
            continue
        if out:
            out.append(pause)
        out.append(audio)
    return out
