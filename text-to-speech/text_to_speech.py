"""Convert cleaned text files to MP3 podcast episodes via Google Cloud TTS."""

import functools
import logging
import math
import operator
import pathlib
import re
import uuid
from datetime import UTC, datetime

from google.cloud import texttospeech
from podcast_shared import apply_id3_tags, generate_summary, split_metadata
from pydub import AudioSegment

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

input_dir = "../prepare-text/text-input-cleaned"
temp_output_dir = "temp-output"
final_output_dir = "../dropcaster-docker/audio"


INTAKE_TYPE_LABELS = {
    "email": "Email",
    "rss": "RSS",
    "link": "Link",
    "youtube": "YouTube",
}


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
    digits = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append(alphabet[remainder])
    return "".join(reversed(digits))


def process_files() -> None:
    """Process all cleaned text files in the input directory."""
    txt_files = sorted(pathlib.Path(input_dir).glob("*.txt"))
    for f in txt_files:
        text_to_speech(f)


def text_to_speech(incoming_filename: str | pathlib.Path) -> None:
    """Synthesize a single text file into an MP3 with ID3 tags."""
    with pathlib.Path(incoming_filename).open("rb") as filename:
        logging.info("Synthesizing speech for %s", filename.name)
        name = pathlib.Path(filename.name).name.replace(".txt", "")
        data = filename.read()
        input_text_raw = data.decode("utf8")
        metadata, content_text = split_metadata(input_text_raw)
        # initialize the API client
        client = texttospeech.TextToSpeechClient()
        mp3files = []
        # we can send up to 5000 characters per request, so split up the text
        min_step_size = 3000
        max_step_size = 5000
        compiled_regex_for_first_whitespace = re.compile(r"(\r\n|\r|\n|\.)+\s+")
        next_text_starter_position = 0
        counter = 0
        max_steps = math.floor(1 + len(content_text) / min_step_size)
        if len(content_text) > 0:
            meta_from = metadata.get("from", "").strip()
            meta_title = metadata.get("title", "").strip()
            meta_source_url = metadata.get("source_url", "").strip()
            meta_source_kind = metadata.get("source_kind", "").strip()
            meta_source_name = metadata.get("source_name", "").strip()
            meta_intake_type = metadata.get("intake_type", "").strip()
            if meta_title or meta_source_url:
                logging.info("Using metadata for summary and description")
            summary = generate_summary(content_text, meta_title)
            description = build_description(
                summary,
                meta_title,
                meta_source_url,
                meta_source_kind,
                meta_source_name,
                meta_intake_type,
            )
            while next_text_starter_position < len(content_text):
                counter += 1
                first_whitespace_after_min_step_size_search = compiled_regex_for_first_whitespace.search(
                    content_text,
                    next_text_starter_position + min_step_size,
                    next_text_starter_position + max_step_size,
                )
                if first_whitespace_after_min_step_size_search is not None:
                    first_whitespace_after_min_step_size = first_whitespace_after_min_step_size_search.end()
                else:
                    first_whitespace_after_min_step_size = next_text_starter_position + max_step_size
                    if first_whitespace_after_min_step_size < len(content_text):
                        logging.info(
                            "max_step_size met before end of %s",
                            filename.name,
                        )
                text_to_process = content_text[next_text_starter_position:first_whitespace_after_min_step_size]
                next_text_starter_position = first_whitespace_after_min_step_size

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
                response = client.synthesize_speech(
                    request={
                        "input": synthesis_input,
                        "voice": voice,
                        "audio_config": audio_config,
                    },
                )
                mp3_filename = f"{temp_output_dir}/{uuid.uuid4()}.mp3"
                _ = pathlib.Path(mp3_filename).write_bytes(response.audio_content)
                logging.info('Audio content written to file "%s"', mp3_filename)
                mp3files.append(mp3_filename)

            mp3_segments = mp3files
            segments = [AudioSegment.from_mp3(f) for f in mp3_segments]

            logging.info("Stitching together %d mp3 files for %s", len(segments), name)
            audio = functools.reduce(operator.add, segments)

            current_datetime = datetime.now(tz=UTC).strftime("%Y%m%d")
            # Filename format: "YYYYMMDD-HHMMSS-<rest>"
            date_match = re.match(r"^(\d{8}-\d{6})-(.+)$", name)
            if date_match:
                date_prefix = date_match.group(1) + "-"
                name_without_date = date_match.group(2)
            else:
                date_prefix = ""
                name_without_date = name
            dash_index = name_without_date.find("-")

            if dash_index != -1:
                output_filename = f"{final_output_dir}/{name_without_date[: dash_index + 1]} {date_prefix} {name_without_date[dash_index + 1 :]}-{current_datetime}.mp3"
            else:
                output_filename = f"{final_output_dir}/{name_without_date}-{date_prefix}{current_datetime}.mp3"

            logging.info("Exporting %s", output_filename)
            audio.export(output_filename, format="mp3")
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
            apply_id3_tags(output_filename, title=title_for_tag, description=description, source_url=meta_source_url)

            logging.info("Removing intermediate files")
            for f in mp3_segments:
                pathlib.Path(f).unlink()

            logging.info("Removing original text file")
            pathlib.Path(incoming_filename).unlink()
        else:
            logging.warning("Skipping %s: file has no content.", filename.name)


if __name__ == "__main__":
    process_files()
