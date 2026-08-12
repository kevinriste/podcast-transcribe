"""Render one text file with a chosen Gemini TTS model + voice into the podcast feed.

An on-demand audition tool: synthesizes synchronously (standard pricing, ~2x
batch — pennies for a single article) so the result is immediate, and annotates
the output filename and ID3 title with the engine and voice so the episode is
distinguishable from the regular render in the feed.

Usage (from text-to-speech/):
    uv run python3 audition.py "../prepare-text/text-input-cleaned-archive/<file>.txt"
        --engine gemini-3.1-flash --voice Callirrhoe
"""

import argparse
import logging
import pathlib

from google.genai import types as genai_types
from podcast_shared import get_gemini_client, split_metadata
from pydub import AudioSegment

from text_to_speech import (
    GEMINI_MAX_STEP_BYTES,
    GEMINI_MIN_STEP_BYTES,
    GEMINI_PCM_FRAME_RATE,
    GEMINI_PCM_SAMPLE_WIDTH,
    GEMINI_TTS_MODELS,
    chunk_text,
    finalize_episode,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def synthesize_gemini_sync(content_text: str, model: str, voice: str, style_prompt: str) -> list[AudioSegment]:
    """Synthesize text via synchronous (non-batch) Gemini TTS calls.

    Returns:
        One audio segment per chunk, in order.

    Raises:
        RuntimeError: If a response comes back without audio data.

    """
    client = get_gemini_client()
    config = genai_types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=genai_types.SpeechConfig(
            voice_config=genai_types.VoiceConfig(
                prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name=voice),
            ),
        ),
    )
    chunks = chunk_text(content_text, GEMINI_MIN_STEP_BYTES, GEMINI_MAX_STEP_BYTES)
    segments: list[AudioSegment] = []
    for counter, chunk in enumerate(chunks, start=1):
        logging.info("Synthesizing chunk %d of %d via %s", counter, len(chunks), model)
        response = client.models.generate_content(  # pyright: ignore[reportUnknownMemberType]
            model=model,
            contents=f"{style_prompt}\n\n{chunk}" if style_prompt else chunk,
            config=config,
        )
        candidates = response.candidates
        content = candidates[0].content if candidates else None
        parts = content.parts if content else None
        data = parts[0].inline_data.data if parts and parts[0].inline_data else None
        if not data:
            msg = f"Chunk {counter}: no audio data in response"
            raise RuntimeError(msg)
        segments.append(
            AudioSegment(
                data=data,
                sample_width=GEMINI_PCM_SAMPLE_WIDTH,
                frame_rate=GEMINI_PCM_FRAME_RATE,
                channels=1,
            )
        )
    return segments


def main() -> None:
    """Parse arguments, synthesize the file, and publish the annotated episode.

    Raises:
        SystemExit: If the input file has no content after its metadata header.

    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("input_file", help="text file with META_ headers (e.g. from text-input-cleaned-archive)")
    _ = parser.add_argument("--engine", choices=sorted(GEMINI_TTS_MODELS), default="gemini-3.1-flash")
    _ = parser.add_argument("--voice", default="Callirrhoe", help="Gemini prebuilt voice name")
    _ = parser.add_argument("--style-prompt", default="", help="optional director's note prepended to each chunk")
    args = parser.parse_args()
    input_file = str(args.input_file)  # pyright: ignore[reportAny]
    engine = str(args.engine)  # pyright: ignore[reportAny]
    voice = str(args.voice)  # pyright: ignore[reportAny]
    style_prompt = str(args.style_prompt)  # pyright: ignore[reportAny]

    input_path = pathlib.Path(input_file)
    metadata, content_text = split_metadata(input_path.read_text(encoding="utf-8"))
    if not content_text:
        msg = f"{input_path.name} has no content"
        raise SystemExit(msg)
    segments = synthesize_gemini_sync(content_text, GEMINI_TTS_MODELS[engine], voice, style_prompt)
    finalize_episode(input_path.stem, metadata, content_text, segments, annotation=f"{engine} {voice}")


if __name__ == "__main__":
    main()
