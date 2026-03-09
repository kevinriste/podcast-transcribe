import functools
import logging
import math
import operator
import os
import pathlib
import re
import uuid
from datetime import datetime
from glob import glob

from google import genai
from google.cloud import texttospeech
from mutagen.id3 import ID3, TIT2, TT3, WXXX, ID3NoHeaderError
from pydub import AudioSegment

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

input_dir = "../prepare-text/text-input-cleaned"
temp_output_dir = "temp-output"
final_output_dir = "../dropcaster-docker/audio"
summary_model = "gemini-3.1-flash-lite-preview"
_gemini_client = None


def split_metadata(raw_text):
    if not raw_text.startswith("META_"):
        return {}, raw_text
    logging.info("Parsing metadata header")
    lines = raw_text.splitlines()
    metadata = {}
    current_key = None
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
            metadata[current_key] = (
                f"{metadata.get(current_key, '')} {line.strip()}".strip()
            )
            continue
        if not line.strip():
            content_start = idx + 1
            break
        content_start = idx
        break
    content = "\n".join(lines[content_start:]) if content_start < len(lines) else ""
    return metadata, content


def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _gemini_client


def generate_summary(text, title):
    if not text.strip():
        logging.info("Summary skipped: empty content")
        return ""
    logging.info("Generating summary via Gemini")
    prompt = (
        "Summarize the article in 2-3 sentences. Focus on key points and keep it concise.\n\n"
        f"Title: {title}\n\nArticle:\n{text}"
    )
    try:
        client = get_gemini_client()
        response = client.models.generate_content(
            model=summary_model,
            contents=prompt,
        )
        logging.info("Summary generated")
        return response.text.strip()
    except Exception as exc:
        logging.exception("Summary generation failed: %s", exc)
        return ""


INTAKE_TYPE_LABELS = {
    "email": "Email",
    "rss": "RSS",
    "link": "Link",
    "youtube": "YouTube",
}


def build_description(summary, title, source_url, source_kind, source_name="", intake_type=""):
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


def apply_id3_tags(mp3_path, description, source_url, title) -> None:
    logging.info("Writing ID3 tags to MP3")
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
    tags.save(mp3_path, v1=2)


def to_base36(value):
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    digits = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append(alphabet[remainder])
    return "".join(reversed(digits))


def process_files() -> None:
    txt_files = sorted(glob(f"{input_dir}/*.txt"))
    for f in txt_files:
        text_to_speech(f)


def text_to_speech(incoming_filename) -> None:
    with pathlib.Path(incoming_filename).open("rb") as filename:
        logging.info(f"Synthesizing speech for email {filename.name}")
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
                first_whitespace_after_min_step_size_search = (
                    compiled_regex_for_first_whitespace.search(
                        content_text,
                        next_text_starter_position + min_step_size,
                        next_text_starter_position + max_step_size,
                    )
                )
                if first_whitespace_after_min_step_size_search is not None:
                    first_whitespace_after_min_step_size = (
                        first_whitespace_after_min_step_size_search.end()
                    )
                else:
                    first_whitespace_after_min_step_size = (
                        next_text_starter_position + max_step_size
                    )
                    if first_whitespace_after_min_step_size < len(content_text):
                        logging.info(
                            f"max_step_size met before end of email {filename.name}",
                        )
                text_to_process = content_text[
                    next_text_starter_position:first_whitespace_after_min_step_size
                ]
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
                with pathlib.Path(mp3_filename).open("wb") as out:
                    # Write the response to the output file.
                    out.write(response.audio_content)
                    logging.info('Audio content written to file "%s"', mp3_filename)
                mp3files.append(mp3_filename)

            mp3_segments = mp3files
            segments = [AudioSegment.from_mp3(f) for f in mp3_segments]

            logging.info(f"Stitching together {len(segments)} mp3 files for {name}")
            audio = functools.reduce(operator.add, segments)

            current_datetime = datetime.now().strftime("%Y%m%d")
            date_and_dash_from_text_file = name[:16]
            name_without_date = name[16:]
            # Check if "-" exists in name_without_date
            dash_index = name_without_date.find("-")

            if dash_index != -1:
                # If "-" exists, insert the date after the first "-" with an additional "-" after it
                output_filename = f"{final_output_dir}/{name_without_date[: dash_index + 1]} {date_and_dash_from_text_file} {name_without_date[dash_index + 1 :]}-{current_datetime}.mp3"
            else:
                # If "-" does not exist, add "-" before and after the date at the end
                output_filename = f"{final_output_dir}/{name_without_date}-{date_and_dash_from_text_file}{current_datetime}.mp3"

            logging.info("Exporting %s", output_filename)
            audio.export(output_filename, format="mp3")
            file_title = os.path.splitext(pathlib.Path(output_filename).name)[0]
            file_title = re.sub(r"-\d{8}$", "", file_title)
            if meta_from and meta_title:
                now = datetime.now()
                base36_width = 6 if now.year <= 2037 else 7
                unix_seconds_base36 = to_base36(int(now.timestamp())).zfill(
                    base36_width,
                )
                title_for_tag = f"{meta_from}- {unix_seconds_base36}- {meta_title}"
            else:
                title_for_tag = meta_title or file_title
            apply_id3_tags(output_filename, description, meta_source_url, title_for_tag)

            logging.info("Removing intermediate files")
            for f in mp3_segments:
                pathlib.Path(f).unlink()

            logging.info("Removing original text file")
            pathlib.Path(incoming_filename).unlink()
        else:
            logging.warning(
                f"Skipping {filename.name}: file has no content.",
            )


if __name__ == "__main__":
    process_files()
