"""Newsletter emails hand their HTML to prepare-text's HTML stage, else their plain text."""

import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import NoReturn

from imap_tools.message import MailMessage

from parse_email import email_body


def _fail(msg: str) -> NoReturn:
    raise AssertionError(msg)


def _message(*, html: bool) -> MailMessage:
    mime = MIMEMultipart("alternative")
    mime["Subject"] = "Example post"
    mime.attach(MIMEText("Plain body.", "plain"))
    if html:
        mime.attach(MIMEText("<p>HTML body.</p>", "html"))
    return MailMessage.from_bytes(mime.as_bytes())


def check_html_goes_to_the_stage() -> None:
    """Write an email's HTML, marked for the HTML stage."""
    body, header = email_body(_message(html=True))
    if body != "<p>HTML body.</p>" or header != "META_BODY_FORMAT: html":
        _fail(f"got {body!r} / {header!r}")


def check_plain_only_falls_back() -> None:
    """Fall back to the plain text, marked plaintext, when an email has no HTML."""
    body, header = email_body(_message(html=False))
    if body.strip() != "Plain body." or header != "META_EXTRACTION: plaintext":
        _fail(f"got {body!r} / {header!r}")


if __name__ == "__main__":
    check_html_goes_to_the_stage()
    check_plain_only_falls_back()
    logging.info("email body tests passed")
