import functools
import logging
import math
import os
import re
import shutil
import uuid
from datetime import datetime
from glob import glob

import requests
from google.cloud import texttospeech
from mutagen.id3 import ID3, TT3, WXXX, ID3NoHeaderError, TIT2
from openai import OpenAI
from pydub import AudioSegment

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

input_dir = "text-input"
temp_output_dir = "temp-output"
final_output_dir = "../dropcaster-docker/audio"
character_limit = 150000
summary_model = "gpt-5-mini"
_openai_client = None


# Using regular expressions to clean email text
def clean_text(text):
    text = "".join(text)
    # Remove three or more consecutive dashes
    text = re.sub(r"---+", "", text)
    # Remove URLs from the email text
    text = re.sub(
        r"https?:\/\/(www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-z]{2,5}\b([-a-zA-Z0-9@:%_\+.~#?&//=]*)",
        "",
        text,
    )
    # Remove empty square brackets []
    text = re.sub(r"\[\]", "", text)
    # Remove empty parentheses ()
    text = re.sub(r"\(\)", "", text)
    # Remove empty angle brackets <>
    text = re.sub(r"<>", "", text)
    # get rid of superfluous non-newline whitespace
    text = re.sub(r"[^\S\r\n]+", " ", text)
    # get rid of unsubscribe text
    text = re.sub(r"(\r\n|\r|\n){2}Unsubscribe", "", text)
    # get rid of intro 'view this post on the web' text
    text = re.sub(r"View this post on the web at (\r\n|\r|\n){2}", "", text)
    # get rid of plain text disclaimer on beehiiv emails
    text = re.sub(
        r"You are reading a plain text version of this post. For the best experience, copy and paste this link in your browser to view the post online:",
        "",
        text,
    )
    # get rid of social links at top of Money Illusion posts
    text = re.sub(
        r"Facebook *(\r\n|\r|\n)Twitter *(\r\n|\r|\n)LinkedIn *(\r\n|\r|\n)", "", text
    )
    # get rid of weird image data/captions in beehiiv emails
    text = re.sub(
        r"View image: \(.*?\)(\r\n|\r|\n)?\s*Caption: .*?\s*.*(\r\n|\r|\n)?", "", text
    )
    # fix pronunciation of Keynesian
    text = re.sub(r"Keynesian", "Cainzeean", text, flags=re.IGNORECASE)
    # add punctuation to end of lines without it so that narration pauses briefly
    text = re.sub(r"(\w)\s*(\r\n|\r|\n)", r"\1.\2", text)

    return text


def split_metadata(raw_text):
    if not raw_text.startswith("META_"):
        return {}, raw_text
    logging.info("Parsing metadata header")
    lines = raw_text.splitlines()
    metadata = {}
    content_start = 0
    for idx, line in enumerate(lines):
        if line.startswith("META_"):
            key, value = line.split(":", 1)
            metadata[key.replace("META_", "").lower()] = value.strip()
            continue
        if line.strip() == "":
            content_start = idx + 1
            break
        content_start = idx
        break
    content = "\n".join(lines[content_start:]) if content_start > 0 else raw_text
    return metadata, content


def get_openai_client():
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI()
    return _openai_client


def generate_summary(text, title):
    if not text.strip():
        logging.info("Summary skipped: empty content")
        return ""
    logging.info("Generating summary via OpenAI")
    prompt = (
        "Summarize the article in 2-3 sentences. Focus on key points and keep it concise.\n\n"
        f"Title: {title}\n\nArticle:\n{text}"
    )
    try:
        client = get_openai_client()
        response = client.responses.create(model=summary_model, input=prompt)
        logging.info("Summary generated")
        return response.output_text.strip()
    except Exception as exc:
        logging.error(f"Summary generation failed: {exc}")
        return ""


def build_description(summary, title, source_url, source_kind):
    description_body = summary or "Summary unavailable."
    title_line = title or "Untitled"
    parts = [description_body, f"Title: {title_line}"]
    if source_url:
        display_text = source_url
        if source_kind == "garbageday":
            display_text = "Garbage Day"
        parts.append(f'Source: <a href="{source_url}">{display_text}</a>')
    return "<br/><br/>".join(parts)


def apply_id3_tags(mp3_path, description, source_url, file_title):
    logging.info("Writing ID3 tags to MP3")
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()
    if file_title:
        tags.add(TIT2(encoding=3, text=file_title))
    if description:
        tags.add(TT3(encoding=3, text=description))
    if source_url:
        tags.add(WXXX(encoding=3, desc="Source", url=source_url))
    tags.save(mp3_path, v1=2)


def process_files():
    txt_files = sorted(glob(f"{input_dir}/*.txt"))
    for f in txt_files:
        text_to_speech(f)


def text_to_speech(incoming_filename):
    with open(incoming_filename, "rb") as filename:
        logging.info(f"Synthesizing speech for email {filename.name}")
        name = os.path.basename(filename.name).replace(".txt", "")
        data = filename.read()
        raw_text = data.decode("utf8")
        metadata, content_text = split_metadata(raw_text)
        text = clean_text(content_text)
        # initialize the API client
        client = texttospeech.TextToSpeechClient()
        mp3files = []
        # we can send up to 5000 characters per request, so split up the text
        min_step_size = 3000
        max_step_size = 5000
        compiled_regex_for_first_whitespace = re.compile(r"(\r\n|\r|\n|\.)+\s+")
        next_text_starter_position = 0
        counter = 0
        max_steps = math.floor(1 + len(text) / min_step_size)
        if len(text) < character_limit and len(text) > 0:
            title = metadata.get("title", "").strip()
            source_url = metadata.get("source_url", "").strip()
            source_kind = metadata.get("source_kind", "").strip()
            if title or source_url:
                logging.info("Using metadata for summary and description")
            summary = generate_summary(text, title)
            description = build_description(summary, title, source_url, source_kind)
            while next_text_starter_position < len(text):
                counter = counter + 1
                first_whitespace_after_min_step_size_search = (
                    compiled_regex_for_first_whitespace.search(
                        text,
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
                    if first_whitespace_after_min_step_size < len(text):
                        logging.info(
                            f"max_step_size met before end of email {filename.name}"
                        )
                text_to_process = text[
                    next_text_starter_position:first_whitespace_after_min_step_size
                ]
                next_text_starter_position = first_whitespace_after_min_step_size

                synthesis_input = texttospeech.SynthesisInput(text=text_to_process)
                voice = texttospeech.VoiceSelectionParams(
                    language_code="en-US", name="en-US-Wavenet-F"
                )
                audio_config = texttospeech.AudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.MP3
                )
                logging.info(
                    f"Synthesizing speech for file {counter} of at most {max_steps}"
                )
                response = client.synthesize_speech(
                    request={
                        "input": synthesis_input,
                        "voice": voice,
                        "audio_config": audio_config,
                    }
                )
                mp3_filename = f"{temp_output_dir}/{uuid.uuid4()}.mp3"
                with open(mp3_filename, "wb") as out:
                    # Write the response to the output file.
                    out.write(response.audio_content)
                    logging.info(f'Audio content written to file "{mp3_filename}"')
                mp3files.append(mp3_filename)

            mp3_segments = mp3files
            segments = [AudioSegment.from_mp3(f) for f in mp3_segments]

            logging.info(f"Stitching together {len(segments)} mp3 files for {name}")
            audio = functools.reduce(lambda a, b: a + b, segments)

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

            logging.info(f"Exporting {output_filename}")
            audio.export(output_filename, format="mp3")
            file_title = os.path.splitext(os.path.basename(output_filename))[0]
            file_title = re.sub(r"-\d{8}$", "", file_title)
            apply_id3_tags(output_filename, description, source_url, file_title)

            logging.info("Removing intermediate files")
            for f in mp3_segments:
                os.remove(f)

            logging.info("Removing original text file")
            os.remove(incoming_filename)
        else:
            if len(text) == 0:
                logging.warning(
                    f"Skipping {filename.name}: file is empty after cleaning."
                )
            elif len(text) >= character_limit:
                logging.warning(
                    f"Skipping {filename.name}: text length {len(text)} exceeds {character_limit} character limit."
                )
                gotify_server = os.environ.get("GOTIFY_SERVER")
                gotify_token = os.environ.get("GOTIFY_TOKEN")
                debug_message = "Skipping long text-to-speech content"
                debug_output = f"Skipping {filename.name}: text length {len(text)} exceeds {character_limit} character limit. Moving to holding directory."

                gotify_url = f"{gotify_server}/message?token={gotify_token}"
                data = {
                    "title": debug_message,
                    "message": debug_output,
                    "priority": 6,
                }
                requests.post(gotify_url, data=data)

                # Move the file to a separate directory so the processing isn't repeatedly tried
                parent = os.path.dirname(os.path.abspath(input_dir))
                target_dir = os.path.join(parent, "text-input-too-big")
                os.makedirs(target_dir, exist_ok=True)

                shutil.move(
                    incoming_filename,
                    os.path.join(target_dir, os.path.basename(incoming_filename)),
                )


if __name__ == "__main__":
    process_files()
