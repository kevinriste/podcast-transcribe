import datetime
import functools
import logging
import math
import operator
import os
import pathlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from google import genai
from google.cloud import texttospeech
from mutagen.id3 import ID3, TIT2, TT3, WXXX, ID3NoHeaderError  # pyright: ignore[reportPrivateImportUsage]
from pydub import AudioSegment  # type: ignore[import-untyped]  # pyright: ignore[reportMissingTypeStubs]
from pyrsistent import PMap, PVector, pmap, pvector

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

INPUT_DIR: Final = "../prepare-text/text-input-cleaned"
TEMP_OUTPUT_DIR: Final = "temp-output"
FINAL_OUTPUT_DIR: Final = "../dropcaster-docker/audio"
SUMMARY_MODEL: Final = "gemini-3.1-flash-lite-preview"
_BASE36_YEAR_THRESHOLD: Final = 2037
_BASE36_ALPHABET: Final = "0123456789abcdefghijklmnopqrstuvwxyz"

INTAKE_TYPE_LABELS: Final[PMap[str, str]] = pmap(
    {
        "email": "Email",
        "rss": "RSS",
        "link": "Link",
        "youtube": "YouTube",
    }
)

_gemini_client: genai.Client | None = None


def _get_gemini_client() -> genai.Client:
    global _gemini_client  # noqa: PLW0603
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _gemini_client


def split_metadata(raw_text: str) -> tuple[PMap[str, str], str]:
    if not raw_text.startswith("META_"):
        return pmap({}), raw_text
    logging.info("Parsing metadata header")
    lines: Final = raw_text.splitlines()
    # Mutable: accumulate metadata then freeze to PMap at return boundary
    metadata_acc: dict[str, str] = {}
    current_key: str | None = None
    content_start: int = len(lines)
    for idx, line in enumerate(lines):
        if line.startswith("META_"):
            if ":" not in line:
                content_start = idx
                break
            key, value = line.split(":", 1)
            current_key = key.replace("META_", "").lower()
            metadata_acc[current_key] = value.strip()
            continue
        if line.startswith((" ", "\t")) and current_key:
            metadata_acc[current_key] = f"{metadata_acc.get(current_key, '')} {line.strip()}".strip()
            continue
        if not line.strip():
            content_start = idx + 1
            break
        content_start = idx
        break
    content: Final = "\n".join(lines[content_start:]) if content_start < len(lines) else ""
    return pmap(metadata_acc), content


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
        client: Final = _get_gemini_client()
        response: Final = client.models.generate_content(  # pyright: ignore[reportUnknownMemberType]
            model=SUMMARY_MODEL,
            contents=prompt,
        )
        logging.info("Summary generated")
        response_text: Final = response.text
        if response_text is None:
            return ""
        return response_text.strip()
    except Exception:
        logging.exception("Summary generation failed")
        return ""


@dataclass(frozen=True, slots=True)
class DescriptionParams:
    summary: str
    title: str
    source_url: str
    source_kind: str
    source_name: str = ""
    intake_type: str = ""


def build_description(params: DescriptionParams) -> str:
    description_body: Final = params.summary or "Summary unavailable."
    title_line: Final = params.title or "Untitled"
    # Mutable: accumulate parts then freeze to PVector at return boundary
    parts_acc: list[str] = [description_body, f"Title: {title_line}"]
    if params.intake_type:
        intake_label: Final = INTAKE_TYPE_LABELS.get(params.intake_type, params.intake_type)
        parts_acc.append(f"Via: {intake_label}")
    if params.source_url:
        display_text: str = params.source_url
        if params.source_kind == "beehiiv" and params.source_name:
            display_text = params.source_name
        parts_acc.append(f'Source: <a href="{params.source_url}">{display_text}</a>')
    parts: Final[PVector[str]] = pvector(parts_acc)
    return "<br/><br/>".join(parts)


def apply_id3_tags(mp3_path: str, description: str, source_url: str, title: str) -> None:
    # Mutable: mutagen ID3 requires mutable dict-like objects
    logging.info("Writing ID3 tags to MP3")
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
    _ = tags.save(mp3_path, v1=2)  # pyright: ignore[reportUnknownMemberType]


def to_base36(value: int) -> str:
    if value == 0:
        return "0"
    # Mutable: accumulate digits then freeze to PVector
    digits_acc: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        digits_acc.append(_BASE36_ALPHABET[remainder])
    digits: Final[PVector[str]] = pvector(digits_acc)
    return "".join(reversed(digits))


def _build_output_filename(name: str) -> str:
    current_datetime: Final = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d")
    date_and_dash_from_text_file: Final = name[:16]
    name_without_date: Final = name[16:]
    dash_index: Final = name_without_date.find("-")

    if dash_index != -1:
        return (
            f"{FINAL_OUTPUT_DIR}/{name_without_date[: dash_index + 1]}"
            f" {date_and_dash_from_text_file}"
            f" {name_without_date[dash_index + 1 :]}-{current_datetime}.mp3"
        )
    return f"{FINAL_OUTPUT_DIR}/{name_without_date}-{date_and_dash_from_text_file}{current_datetime}.mp3"


def _build_title_for_tag(metadata: Mapping[str, str], file_title: str) -> str:
    meta_from: Final = metadata.get("from", "").strip()
    meta_title: Final = metadata.get("title", "").strip()
    if meta_from and meta_title:
        now: Final = datetime.datetime.now(tz=datetime.UTC)
        base36_width: Final = 6 if now.year <= _BASE36_YEAR_THRESHOLD else 7
        unix_seconds_base36: Final = to_base36(int(now.timestamp())).zfill(base36_width)
        return f"{meta_from}- {unix_seconds_base36}- {meta_title}"
    return meta_title or file_title


def process_files() -> None:
    txt_files: Final = sorted(pathlib.Path(INPUT_DIR).glob("*.txt"))
    for f in txt_files:
        text_to_speech(str(f))


def text_to_speech(incoming_filename: str) -> None:
    incoming_path: Final = pathlib.Path(incoming_filename)
    raw_bytes: Final = incoming_path.read_bytes()
    logging.info("Synthesizing speech for email %s", incoming_path.name)
    name: Final = incoming_path.stem
    input_text_raw: Final = raw_bytes.decode("utf8")
    metadata: PMap[str, str]
    content_text: str
    metadata, content_text = split_metadata(input_text_raw)
    # Mutable: google TTS client requires mutable request dicts
    client: Final = texttospeech.TextToSpeechClient()
    # Mutable: accumulate mp3 filenames then freeze to PVector
    mp3files_acc: list[str] = []
    min_step_size: Final = 3000
    max_step_size: Final = 5000
    compiled_regex_for_first_whitespace: Final = re.compile(r"(\r\n|\r|\n|\.)+\s+")
    next_text_starter_position: int = 0
    counter: int = 0
    max_steps: Final = math.floor(1 + len(content_text) / min_step_size)
    if len(content_text) > 0:
        meta_source_url: Final = metadata.get("source_url", "").strip()
        meta_source_kind: Final = metadata.get("source_kind", "").strip()
        meta_source_name: Final = metadata.get("source_name", "").strip()
        meta_intake_type: Final = metadata.get("intake_type", "").strip()
        meta_title: Final = metadata.get("title", "").strip()
        if meta_title or meta_source_url:
            logging.info("Using metadata for summary and description")
        summary: Final = generate_summary(content_text, meta_title)
        description: Final = build_description(
            DescriptionParams(
                summary=summary,
                title=meta_title,
                source_url=meta_source_url,
                source_kind=meta_source_kind,
                source_name=meta_source_name,
                intake_type=meta_intake_type,
            ),
        )
        while next_text_starter_position < len(content_text):
            counter += 1
            first_whitespace_after_min_step_size_search = compiled_regex_for_first_whitespace.search(
                content_text,
                next_text_starter_position + min_step_size,
                next_text_starter_position + max_step_size,
            )
            if first_whitespace_after_min_step_size_search is not None:
                first_whitespace_after_min_step_size: int = first_whitespace_after_min_step_size_search.end()
            else:
                first_whitespace_after_min_step_size = next_text_starter_position + max_step_size
                if first_whitespace_after_min_step_size < len(content_text):
                    logging.info(
                        "max_step_size met before end of email %s",
                        incoming_path.name,
                    )
            text_to_process = content_text[next_text_starter_position:first_whitespace_after_min_step_size]
            next_text_starter_position = first_whitespace_after_min_step_size

            # Mutable: google TTS API requires mutable request dict
            synthesis_input = texttospeech.SynthesisInput(text=text_to_process)
            voice = texttospeech.VoiceSelectionParams(
                language_code="en-US",
                name="en-US-Wavenet-F",
            )
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
            )
            logging.info(
                "Synthesizing speech for file %s of at most %s",
                counter,
                max_steps,
            )
            response = client.synthesize_speech(  # pyright: ignore[reportUnknownMemberType]
                request={
                    "input": synthesis_input,
                    "voice": voice,
                    "audio_config": audio_config,
                },
            )
            mp3_filename = f"{TEMP_OUTPUT_DIR}/{uuid.uuid4()}.mp3"
            _ = pathlib.Path(mp3_filename).write_bytes(response.audio_content)
            logging.info('Audio content written to file "%s"', mp3_filename)
            mp3files_acc.append(mp3_filename)

        mp3files: Final[PVector[str]] = pvector(mp3files_acc)
        # Mutable: pydub requires list of AudioSegment objects
        segments: Final[list[AudioSegment]] = [
            AudioSegment.from_mp3(seg)  # pyright: ignore[reportUnknownMemberType]
            for seg in mp3files
        ]  # type: ignore[no-untyped-call]

        logging.info("Stitching together %s mp3 files for %s", len(segments), name)
        audio: Final[AudioSegment] = functools.reduce(operator.add, segments)  # type: ignore[arg-type]  # pyright: ignore[reportAny]

        output_filename: Final = _build_output_filename(name)

        logging.info("Exporting %s", output_filename)
        _ = audio.export(output_filename, format="mp3")  # type: ignore[no-untyped-call]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        file_title: str = pathlib.Path(output_filename).stem
        file_title = re.sub(r"-\d{8}$", "", file_title)
        title_for_tag: Final = _build_title_for_tag(metadata, file_title)
        apply_id3_tags(output_filename, description, meta_source_url, title_for_tag)

        logging.info("Removing intermediate files")
        for seg_file in mp3files:
            pathlib.Path(seg_file).unlink()

        logging.info("Removing original text file")
        incoming_path.unlink()
    else:
        logging.warning(
            "Skipping %s: file has no content.",
            incoming_path.name,
        )


if __name__ == "__main__":
    process_files()
