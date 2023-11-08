import os
import re
from trafilatura import extract, bare_extraction
from requests_html import HTMLSession
from imap_tools import MailBox, AND, MailMessageFlags
import youtube_dl
import logging
import requests
import pyppeteer
from waybackpy import WaybackMachineSaveAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

output_folder = "../text-to-speech/text-input"
gmail_user = os.getenv("GMAIL_PODCAST_ACCOUNT")
gmail_password = os.getenv("GMAIL_PODCAST_ACCOUNT_APP_PASSWORD")

# get list of email subjects from INBOX folder
with MailBox("imap.gmail.com").login(gmail_user, gmail_password) as mailbox:
    msgs = mailbox.fetch(AND(seen=False), mark_seen=False)
    for msg in msgs:
        subject = msg.subject.replace("Fwd: ", "")
        date = msg.date.strftime("%Y%m%d-%H%M%S-%f")[0:15]
        from_ = msg.from_values.name
        clean_from = re.sub(r"[^A-Za-z0-9 ]+", "", from_)
        clean_from = clean_from + "- " if clean_from != "" else ""
        clean_subject = re.sub(r"[^A-Za-z0-9 ]+", "", subject)
        if clean_subject != "link" and clean_subject != "youtube":
            output_filename = f"{output_folder}/{date}-{clean_from}{clean_subject}.txt"
            logging.info(f"parsing email: {output_filename}")
            email_text = msg.text
            first_clean_email_text = re.sub(
                r"https?:\/\/(www\.)?[-a-zA-Z0-9@:%._\+~#=]{2,256}\.[a-z]{2,5}\b([-a-zA-Z0-9@:%_\+.~#?&//=]*)",
                "",
                email_text,
            )
            second_clean_email_text = re.sub(r"\[\]", "", first_clean_email_text)
            third_clean_email_text = re.sub(r"\(\)", "", second_clean_email_text)
            clean_email_text = re.sub(r"<>", "", third_clean_email_text)
            if len(clean_email_text) > 0:
                clean_email_text = (
                    clean_from + ".\n" + clean_subject + ".\n" + "\n" + clean_email_text
                )
            move_to_podcast = True
            if clean_from == "Paul Krugman- ":
                move_to_podcast = False
            if clean_from == "Ross Douthat- ":
                move_to_podcast = False
            if "Jessica Valenti- Abortion Every Day" in output_filename:
                move_to_podcast = False
            if "Serious Trouble- " in output_filename:
                move_to_podcast = False
            if move_to_podcast:
                output_file = open(output_filename, "w")
                output_file.write(clean_email_text)
                output_file.close()
        elif clean_subject == "youtube":
            email_text = msg.text
            email_text = re.sub(r"[^\S]+", "", email_text)
            logging.info(f"fetching youtube audio: {email_text}")
            ydl_opts = {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
                "outtmpl": "../dropcaster-docker/audio/%(channel)s- %(title)s.%(ext)s",
            }
            with youtube_dl.YoutubeDL(ydl_opts) as ydl:
                ydl.download([email_text])
        else:
            email_text = msg.text
            email_text = re.sub(r"[^\S]+", "", email_text)
            logging.info(f"fetching webpage: {email_text}")
            original_url = email_text
            user_agent = (
                "Mozilla/5.0 (Windows NT 5.1; rv:40.0) Gecko/20100101 Firefox/40.0"
            )
            save_api = WaybackMachineSaveAPI(original_url, user_agent)
            save_api.save()
            archive_url = save_api.archive_url
            session = HTMLSession()
            html_fetch = session.get(archive_url)
            try:
                html_fetch.raise_for_status()
                html_fetch.html.render(timeout=60)
            except (requests.HTTPError, pyppeteer.errors.TimeoutError) as e:
                logging.info(f"{archive_url} URL caused the issue.")
                raise e
            html_content = html_fetch.html.html
            html_content_parsed_for_title = bare_extraction(html_content)
            webpage_text = extract(html_content, include_comments=False)
            webpage_text = (
                html_content_parsed_for_title.get("title") + ".\n" + "\n" + webpage_text
            )
            clean_title = re.sub(
                r"[^A-Za-z0-9 ]+", "", html_content_parsed_for_title.get("title")
            )
            output_filename = f"{output_folder}/{date}-{clean_title}.txt"
            output_file = open(output_filename, "w")
            output_file.write(webpage_text)
            output_file.close()
        flags = MailMessageFlags.SEEN
        mailbox.flag(msg.uid, flags, True)
