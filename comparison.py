"""TTS Strategy Comparison Tool.

Takes a cleaned article text file and generates audio through multiple
TTS strategies for side-by-side listening comparison. Drops output into
dropcaster audio directory with ID3 tags for podcast feed playback.

Strategies:
  1. wavenet-plain     — WaveNet + plain text (current baseline)
  2. wavenet-ssml      — WaveNet + LLM SSML enrichment (S-1)
  3. wavenet-ssml-det  — WaveNet + deterministic SSML (S-2, needs E-C/E-D input)
  4. chirp3-plain      — Chirp 3 HD + plain text
  5. chirp3-ssml       — Chirp 3 HD + LLM SSML enrichment
  6. gemini-flash      — Gemini 2.5 Flash TTS with style prompt
  7. gemini-pro        — Gemini 2.5 Pro TTS with style prompt

Usage:
  source /etc/profile.d/podcast-transcribe.sh
  source /etc/profile.d/google-gemini.sh
  export GOOGLE_APPLICATION_CREDENTIALS=./EmailPodcast-c69d63681230.json

  # Quick test — first 2000 chars, baseline strategies
  cd text-to-speech && uv run python3 ../comparison.py \\
      ../prepare-text/text-input-cleaned-archive/some_article.txt --max-chars 2000

  # All strategies, full article, drop into podcast feed
  cd text-to-speech && uv run python3 ../comparison.py \\
      ../prepare-text/text-input-cleaned-archive/some_article.txt --podcast

  # Deterministic SSML from E-C markers
  cd text-to-speech && uv run python3 ../comparison.py \\
      ../html-comparison/email-4-.../c_beautifulsoup_selective.txt \\
      --strategies wavenet-ssml-det --input-format markers
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
import wave
import xml.etree.ElementTree as ET
from pathlib import Path

from google import genai
from google.cloud import texttospeech
from google.genai import types
from pydub import AudioSegment

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WAVENET_VOICE = "en-US-Wavenet-F"
CHIRP3_VOICE_DEFAULT = "en-US-Chirp3-HD-Achernar"
GEMINI_VOICE_DEFAULT = "Kore"

SSML_ENRICHMENT_MODEL = "gemini-3.1-flash-lite-preview"
GEMINI_FLASH_MODEL = "gemini-2.5-flash-preview-tts"
GEMINI_PRO_MODEL = "gemini-2.5-pro-preview-tts"

AUDIO_EFFECTS_PROFILE = "headphone-class-device"
CHUNK_SIZE = 4500
DROPCASTER_AUDIO_DIR = "../dropcaster-docker/audio"

GEMINI_STYLE_PROMPT = (
    "Read this as a calm, articulate podcast host narrating a newsletter article "
    "to a solo listener on a morning commute. Measured pace, not rushed. Slight "
    "warmth but not performative. Treat quoted text as if you're reading someone "
    "else's words — subtle shift in tone, not a character voice. Pause naturally "
    "between sections. This is informational, not entertainment — prioritize "
    "clarity and a rhythm that's easy to follow for 5-10 minutes at a time."
)

ALL_STRATEGIES = [
    "wavenet-plain", "wavenet-ssml", "wavenet-ssml-det",
    "chirp3-plain", "chirp3-ssml",
    "gemini-flash", "gemini-pro",
]

STRATEGY_LABELS = {
    "wavenet-plain": "WaveNet Plain",
    "wavenet-ssml": "WaveNet+LLM SSML",
    "wavenet-ssml-det": "WaveNet+Det SSML",
    "chirp3-plain": "Chirp3 HD Plain",
    "chirp3-ssml": "Chirp3 HD+LLM SSML",
    "gemini-flash": "Gemini Flash TTS",
    "gemini-pro": "Gemini Pro TTS",
}

# ---------------------------------------------------------------------------
# SSML enrichment prompt
# ---------------------------------------------------------------------------

SSML_SYSTEM_PROMPT = """\
You are an SSML preprocessor for Google Cloud Text-to-Speech (WaveNet voices).
Your job is to convert plain article text into well-formed SSML that improves
the naturalness and clarity of the spoken output.

## Rules

1. Wrap the entire output in a single <speak> root element.
2. Wrap each paragraph in <p> tags.
3. Wrap each sentence within a paragraph in <s> tags.
4. Use ONLY these SSML tags (no others):
   - <speak> (root)
   - <p> (paragraph)
   - <s> (sentence)
   - <break time="Xms"/> (pauses between sections — use 600-1000ms for major
     section breaks, 300-400ms for minor transitions)
   - <sub alias="spoken form">written form</sub> (pronunciation overrides for
     abbreviations, acronyms, and tricky words)
   - <say-as interpret-as="TYPE">text</say-as> where TYPE is one of:
     cardinal, ordinal, characters, date, time, unit, fraction
   - <emphasis level="moderate">text</emphasis> (use sparingly, only for words
     that clearly carry stress in context)

5. XML escaping is critical:
   - & must be &amp;
   - < must be &lt; (in text content, not tags)
   - > must be &gt; (in text content, not tags)
   - " in text must be &quot; (inside attributes use single quotes or escape)
   - ' in text is fine as-is

6. Keep nesting FLAT:
   - Do NOT put <say-as> inside <emphasis> or vice versa
   - <sub>, <say-as>, <emphasis>, and <break/> go directly inside <s> tags
   - <s> tags go inside <p> tags
   - <p> tags go inside <speak>

7. For the author/title header lines at the start and end, add a
   <break time="800ms"/> after them to separate from the body.

8. Do NOT add any text that wasn't in the original. Do NOT summarize or
   rephrase. Your output must contain ALL of the original text, just
   wrapped in SSML tags.

9. Common substitutions to apply:
   - Single standalone letters/abbreviations: use <say-as interpret-as="characters">
   - Dollar amounts: keep as-is (WaveNet handles "$X" well)
   - Percentages: keep as-is (WaveNet handles "X%" well)
   - Years (4-digit numbers in date context): keep as-is
   - Ordinals like "1st", "2nd", "3rd": use <say-as interpret-as="ordinal">
   - Large numbers: use <say-as interpret-as="cardinal"> only if ambiguous

10. Output ONLY the SSML. No markdown fences, no explanation, no preamble.
"""


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------


def chunk_text(text: str, max_chars: int = CHUNK_SIZE) -> list[str]:
    """Split text into chunks at sentence/paragraph boundaries."""
    chunks: list[str] = []
    current = ""

    paragraphs = text.split("\n\n")
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 <= max_chars:
            current = f"{current}\n\n{para}" if current else para
        else:
            if current:
                chunks.append(current.strip())
            if len(para) > max_chars:
                sentences = para.replace(". ", ".\n").split("\n")
                current = ""
                for sent in sentences:
                    if len(current) + len(sent) + 1 <= max_chars:
                        current = f"{current} {sent}" if current else sent
                    else:
                        if current:
                            chunks.append(current.strip())
                        current = sent
            else:
                current = para

    if current.strip():
        chunks.append(current.strip())
    return chunks


def chunk_ssml(ssml: str, max_chars: int = CHUNK_SIZE) -> list[str]:
    """Split SSML into chunks at </p> boundaries, re-wrapping in <speak>."""
    inner = ssml
    if "<speak>" in ssml:
        start = ssml.index("<speak>") + len("<speak>")
        end = ssml.rindex("</speak>")
        inner = ssml[start:end]

    parts = inner.split("</p>")
    chunks: list[str] = []
    current = ""

    for part in parts:
        part = part.strip()
        if not part:
            continue
        segment = f"{part}</p>"
        if len(current) + len(segment) + len("<speak></speak>") <= max_chars:
            current = f"{current}\n{segment}" if current else segment
        else:
            if current:
                chunks.append(f"<speak>{current}</speak>")
            current = segment

    if current:
        chunks.append(f"<speak>{current}</speak>")
    return chunks if chunks else [ssml]


# ---------------------------------------------------------------------------
# SSML enrichment via Gemini (S-1)
# ---------------------------------------------------------------------------


def enrich_with_ssml(text: str, gemini_client: genai.Client) -> str | None:
    """Use Gemini to convert plain text to SSML.

    Returns:
        SSML string, or None on failure.

    """
    try:
        response = gemini_client.models.generate_content(
            model=SSML_ENRICHMENT_MODEL,
            config=types.GenerateContentConfig(
                system_instruction=SSML_SYSTEM_PROMPT,
                temperature=0.2,
            ),
            contents=text,
        )
        ssml = response.text.strip()

        # Strip markdown fences if model wrapped them
        if ssml.startswith("```"):
            ssml_lines = ssml.split("\n")
            ssml = "\n".join(ssml_lines[1:-1] if ssml_lines[-1].startswith("```") else ssml_lines[1:])
            ssml = ssml.strip()

        ET.fromstring(ssml)
        return ssml

    except ET.ParseError as e:
        logging.warning("SSML validation failed: %s — falling back to plain text", e)
        return None
    except Exception:
        logging.exception("SSML enrichment failed")
        return None


def generate_ssml_s1(body: str, gemini_client: genai.Client, output_dir: Path) -> list[str] | None:
    """Generate LLM SSML (S-1) for the full article body.

    Returns:
        List of SSML strings per chunk, or None on total failure.

    """
    logging.info("Generating LLM SSML enrichment (S-1) via Gemini...")
    enrichment_chunks = chunk_text(body, max_chars=12000)
    ssml_parts: list[str] = []
    fallback_count = 0

    for i, chunk in enumerate(enrichment_chunks):
        logging.info("  Enriching chunk %d/%d...", i + 1, len(enrichment_chunks))
        result = enrich_with_ssml(chunk, gemini_client)
        if result:
            ssml_parts.append(result)
        else:
            fallback_count += 1
            escaped = chunk.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            ssml_parts.append(f"<speak><p>{escaped}</p></speak>")

    if fallback_count:
        logging.info("  %d/%d chunks fell back to plain text wrapping", fallback_count, len(enrichment_chunks))

    total_ssml_chars = sum(len(s) for s in ssml_parts)
    logging.info("  SSML total: %d chars (%.0f%% of original)", total_ssml_chars, total_ssml_chars / len(body) * 100)

    ssml_output = "\n\n---\n\n".join(ssml_parts)
    _ = (output_dir / "enriched_s1.ssml.xml").write_text(ssml_output, encoding="utf-8")
    logging.info("  Saved SSML to %s/enriched_s1.ssml.xml", output_dir)

    return ssml_parts


# ---------------------------------------------------------------------------
# Deterministic SSML (S-2)
# ---------------------------------------------------------------------------


def generate_ssml_s2(body: str, input_format: str, output_dir: Path) -> str | None:
    """Generate deterministic SSML (S-2) from structured input.

    Returns:
        SSML string, or None if input format is unsupported.

    """
    # Import here to avoid requiring bs4 when not using S-2
    sys.path.insert(0, str(Path(__file__).parent))
    from ssml_mapper import html_to_ssml, markers_to_ssml, validate_ssml

    if input_format == "markers":
        logging.info("Generating deterministic SSML (S-2) from E-C markers...")
        ssml = markers_to_ssml(body)
    elif input_format == "html":
        logging.info("Generating deterministic SSML (S-2) from E-D HTML...")
        ssml = html_to_ssml(body)
    else:
        logging.warning("S-2 requires --input-format markers or html, got '%s'", input_format)
        return None

    if not validate_ssml(ssml):
        logging.error("S-2 generated invalid SSML")
        return None

    logging.info("  S-2 SSML: %d chars", len(ssml))
    _ = (output_dir / "enriched_s2.ssml.xml").write_text(ssml, encoding="utf-8")
    logging.info("  Saved SSML to %s/enriched_s2.ssml.xml", output_dir)
    return ssml


# ---------------------------------------------------------------------------
# Cloud TTS synthesis (WaveNet / Chirp 3 HD)
# ---------------------------------------------------------------------------


def synthesize_cloud_tts(
    tts_client: texttospeech.TextToSpeechClient,
    chunks: list[str],
    voice_name: str,
    *,
    is_ssml: bool = False,
    label: str = "",
) -> bytes:
    """Synthesize audio from text/SSML chunks via Cloud TTS.

    Returns:
        Combined MP3 bytes.

    """
    segments: list[AudioSegment] = []

    for i, chunk in enumerate(chunks):
        logging.info("  [%s] Synthesizing chunk %d/%d (%d chars, %s)...",
                     label, i + 1, len(chunks), len(chunk), "SSML" if is_ssml else "text")

        synth_input = (texttospeech.SynthesisInput(ssml=chunk) if is_ssml
                       else texttospeech.SynthesisInput(text=chunk))

        voice = texttospeech.VoiceSelectionParams(language_code="en-US", name=voice_name)
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            effects_profile_id=[AUDIO_EFFECTS_PROFILE],
        )

        response = tts_client.synthesize_speech(input=synth_input, voice=voice, audio_config=audio_config)
        segments.append(AudioSegment.from_mp3(io.BytesIO(response.audio_content)))
        time.sleep(0.1)

    if not segments:
        return b""
    combined: AudioSegment = segments[0]
    for seg in segments[1:]:
        combined += seg
    buf = io.BytesIO()
    _ = combined.export(buf, format="mp3")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Gemini TTS synthesis
# ---------------------------------------------------------------------------


def synthesize_gemini_tts(
    gemini_client: genai.Client,
    text: str,
    *,
    model: str,
    voice: str,
    label: str = "",
) -> bytes | None:
    """Synthesize audio via Gemini TTS.

    Returns:
        Raw PCM audio bytes, or None on failure.

    """
    gemini_chunk_size = 3500
    chunks = chunk_text(text, max_chars=gemini_chunk_size)
    all_audio_data: list[bytes] = []

    for i, chunk in enumerate(chunks):
        logging.info("  [%s] Synthesizing chunk %d/%d (%d chars)...", label, i + 1, len(chunks), len(chunk))
        try:
            response = gemini_client.models.generate_content(
                model=model,
                contents=f"{GEMINI_STYLE_PROMPT}\n\n{chunk}",
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
            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.mime_type.startswith("audio/"):
                    all_audio_data.append(part.inline_data.data)
            time.sleep(0.5)
        except Exception:
            logging.exception("  [%s] Chunk %d failed", label, i + 1)

    if not all_audio_data:
        return None
    return b"".join(all_audio_data)


# ---------------------------------------------------------------------------
# ID3 tagging and dropcaster output
# ---------------------------------------------------------------------------


def write_podcast_mp3(audio_bytes: bytes, strategy: str, test_num: int,
                      author: str, title: str, *, is_wav: bool = False) -> None:
    """Write MP3 to dropcaster audio dir with ID3 tags."""
    from podcast_shared import apply_id3_tags

    label = STRATEGY_LABELS.get(strategy, strategy)
    episode_title = f"[TEST-R1-{test_num}] {label} - {title}"

    dropcaster_dir = Path(DROPCASTER_AUDIO_DIR)
    dropcaster_dir.mkdir(parents=True, exist_ok=True)
    filename = f"TEST-R1-{test_num:02d}-{strategy}.mp3"
    out_path = dropcaster_dir / filename

    if is_wav:
        # Convert WAV to MP3 via pydub
        wav_io = io.BytesIO()
        with wave.open(wav_io, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(audio_bytes)
        wav_io.seek(0)
        segment = AudioSegment.from_wav(wav_io)
        mp3_io = io.BytesIO()
        _ = segment.export(mp3_io, format="mp3")
        out_path.write_bytes(mp3_io.getvalue())
    else:
        out_path.write_bytes(audio_bytes)

    description = f"TTS comparison test: {label}\nArticle: {author} - {title}"
    apply_id3_tags(str(out_path), title=episode_title, description=description, source_url="", v1=1)
    logging.info("  Podcast episode: %s (%d bytes)", out_path, out_path.stat().st_size)


# ---------------------------------------------------------------------------
# Read article
# ---------------------------------------------------------------------------


def read_article(path: str) -> tuple[str, dict[str, str]]:
    """Read a cleaned article file, separating META_ headers from body.

    Returns:
        Tuple of (body text, metadata dict).

    """
    text = Path(path).read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False

    for line in text.split("\n"):
        if not in_body and line.startswith("META_"):
            key, _, value = line.partition(": ")
            clean_key = key.replace("META_", "").lower()
            meta[clean_key] = value.strip()
        elif not in_body and line.strip() == "":
            in_body = True
        else:
            in_body = True
            body_lines.append(line)

    return "\n".join(body_lines).strip(), meta


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run TTS strategy comparison."""
    arg_parser = argparse.ArgumentParser(description="Compare TTS strategies on a sample article")
    arg_parser.add_argument("input_file", help="Path to a cleaned article .txt file")
    arg_parser.add_argument("--output-dir", default="./comparison_output",
                            help="Directory for output audio and SSML files")
    arg_parser.add_argument("--strategies", nargs="+", default=ALL_STRATEGIES, choices=ALL_STRATEGIES,
                            help="Which strategies to run")
    arg_parser.add_argument("--max-chars", type=int, default=0,
                            help="Truncate article to N chars (0 = full article)")
    arg_parser.add_argument("--input-format", default="text", choices=["text", "markers", "html"],
                            help="Input format: text (E-A), markers (E-C), html (E-D)")
    arg_parser.add_argument("--chirp-voice", default=CHIRP3_VOICE_DEFAULT,
                            help=f"Chirp 3 HD voice name (default: {CHIRP3_VOICE_DEFAULT})")
    arg_parser.add_argument("--gemini-voice", default=GEMINI_VOICE_DEFAULT,
                            help=f"Gemini TTS voice name (default: {GEMINI_VOICE_DEFAULT})")
    arg_parser.add_argument("--podcast", action="store_true",
                            help="Also drop MP3s into dropcaster audio dir with ID3 tags")
    args = arg_parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Read article
    body, meta = read_article(args.input_file)
    author = meta.get("from", "Unknown Author")
    title = meta.get("title", Path(args.input_file).stem)

    if args.max_chars > 0:
        body = body[:args.max_chars]
        logging.info("Truncated article to %d chars", args.max_chars)

    logging.info("Article: %s - %s", author, title)
    logging.info("Body: %d chars | Format: %s | Strategies: %s",
                 len(body), args.input_format, ", ".join(args.strategies))
    logging.info("Chirp voice: %s | Gemini voice: %s", args.chirp_voice, args.gemini_voice)
    if args.podcast:
        logging.info("Podcast output: %s", DROPCASTER_AUDIO_DIR)

    # Initialize clients
    needs_cloud = any(s.startswith(("wavenet", "chirp3")) for s in args.strategies)
    needs_gemini = any(s.endswith("ssml") or s.startswith("gemini") for s in args.strategies)

    tts_client = None
    gemini_client = None

    if needs_cloud:
        tts_client = texttospeech.TextToSpeechClient()
        logging.info("Cloud TTS client initialized")

    if needs_gemini:
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            logging.error("GOOGLE_API_KEY or GEMINI_API_KEY required")
            sys.exit(1)
        gemini_client = genai.Client(api_key=api_key)
        logging.info("Gemini client initialized")

    # --- Generate SSML if needed ---
    ssml_s1: list[str] | None = None
    ssml_s2: str | None = None

    if any(s in {"wavenet-ssml", "chirp3-ssml"} for s in args.strategies):
        ssml_s1 = generate_ssml_s1(body, gemini_client, output_dir)

    if "wavenet-ssml-det" in args.strategies:
        ssml_s2 = generate_ssml_s2(body, args.input_format, output_dir)

    # --- Run strategies ---
    test_num = 0
    results: list[dict[str, object]] = []

    def run_strategy(strategy: str, audio: bytes, *, is_wav: bool = False) -> None:
        nonlocal test_num
        test_num += 1
        label = STRATEGY_LABELS[strategy]
        out_ext = ".wav" if is_wav and not args.podcast else ".mp3"
        out_path = output_dir / f"{test_num:02d}_{strategy}{out_ext}"

        if is_wav and not args.podcast:
            with wave.open(str(out_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(audio)
        elif is_wav:
            # Will be converted in write_podcast_mp3
            pass
        else:
            out_path.write_bytes(audio)

        if not is_wav:
            logging.info("  Saved %s (%d bytes)", out_path, len(audio))
        results.append({"strategy": strategy, "label": label, "size": len(audio)})

        if args.podcast:
            write_podcast_mp3(audio, strategy, test_num, author, title, is_wav=is_wav)

    # 1. WaveNet plain
    if "wavenet-plain" in args.strategies:
        logging.info("Strategy: WaveNet + plain text (baseline)")
        chunks = chunk_text(body)
        audio = synthesize_cloud_tts(tts_client, chunks, WAVENET_VOICE, label="wavenet-plain")
        run_strategy("wavenet-plain", audio)

    # 2. WaveNet + LLM SSML (S-1)
    if "wavenet-ssml" in args.strategies and ssml_s1:
        logging.info("Strategy: WaveNet + LLM SSML (S-1)")
        all_chunks: list[str] = []
        for part in ssml_s1:
            all_chunks.extend(chunk_ssml(part))
        audio = synthesize_cloud_tts(tts_client, all_chunks, WAVENET_VOICE, is_ssml=True, label="wavenet-ssml")
        run_strategy("wavenet-ssml", audio)

    # 3. WaveNet + deterministic SSML (S-2)
    if "wavenet-ssml-det" in args.strategies and ssml_s2:
        logging.info("Strategy: WaveNet + deterministic SSML (S-2)")
        ssml_chunks = chunk_ssml(ssml_s2)
        audio = synthesize_cloud_tts(tts_client, ssml_chunks, WAVENET_VOICE, is_ssml=True, label="wavenet-ssml-det")
        run_strategy("wavenet-ssml-det", audio)

    # 4. Chirp 3 HD plain
    if "chirp3-plain" in args.strategies:
        logging.info("Strategy: Chirp 3 HD + plain text")
        chunks = chunk_text(body)
        try:
            audio = synthesize_cloud_tts(tts_client, chunks, args.chirp_voice, label="chirp3-plain")
            run_strategy("chirp3-plain", audio)
        except Exception:
            logging.exception("Chirp 3 HD failed — check --chirp-voice")

    # 5. Chirp 3 HD + LLM SSML
    if "chirp3-ssml" in args.strategies and ssml_s1:
        logging.info("Strategy: Chirp 3 HD + LLM SSML (S-1)")
        all_chunks = []
        for part in ssml_s1:
            all_chunks.extend(chunk_ssml(part))
        try:
            audio = synthesize_cloud_tts(tts_client, all_chunks, args.chirp_voice, is_ssml=True, label="chirp3-ssml")
            run_strategy("chirp3-ssml", audio)
        except Exception:
            logging.exception("Chirp 3 HD + SSML failed")

    # 6. Gemini Flash TTS
    if "gemini-flash" in args.strategies:
        logging.info("Strategy: Gemini 2.5 Flash TTS")
        audio = synthesize_gemini_tts(gemini_client, body, model=GEMINI_FLASH_MODEL,
                                      voice=args.gemini_voice, label="gemini-flash")
        if audio:
            run_strategy("gemini-flash", audio, is_wav=True)
        else:
            logging.error("Gemini Flash TTS produced no audio")

    # 7. Gemini Pro TTS
    if "gemini-pro" in args.strategies:
        logging.info("Strategy: Gemini 2.5 Pro TTS")
        audio = synthesize_gemini_tts(gemini_client, body, model=GEMINI_PRO_MODEL,
                                      voice=args.gemini_voice, label="gemini-pro")
        if audio:
            run_strategy("gemini-pro", audio, is_wav=True)
        else:
            logging.error("Gemini Pro TTS produced no audio")

    # --- Summary ---
    print("\n" + "=" * 60)
    print("Comparison files:")
    for r in results:
        print(f"  {r['label']:<25s}  {int(r['size']):>10,d} bytes")
    if args.podcast:
        print(f"\nPodcast episodes written to {DROPCASTER_AUDIO_DIR}/")
        print("Feed will regenerate on next cron run.")

    # Cost estimate
    plain_chars = len(body)
    ssml_s1_chars = sum(len(s) for s in ssml_s1) if ssml_s1 else 0
    print(f"\nArticle: {plain_chars:,} chars")
    if ssml_s1:
        print(f"LLM SSML (S-1): {ssml_s1_chars:,} chars ({ssml_s1_chars / plain_chars:.2f}x)")
    if ssml_s2:
        print(f"Det SSML (S-2): {len(ssml_s2):,} chars ({len(ssml_s2) / plain_chars:.2f}x)")
    print("\nEstimated monthly cost at ~1.1M chars/mo:")
    print("  WaveNet plain:     $0 (under 4M free tier)")
    print("  WaveNet + SSML:    $0 (under 4M free tier)")
    print(f"  Chirp 3 HD plain:  ~$3/mo")
    print(f"  Chirp 3 HD + SSML: ~$15/mo")
    print(f"  Gemini Flash TTS:  ~$27/mo")
    print(f"  Gemini Pro TTS:    ~$50+/mo (estimate)")


if __name__ == "__main__":
    main()
