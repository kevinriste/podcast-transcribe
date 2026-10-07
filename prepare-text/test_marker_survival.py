"""The block-quote marker survives normal cleaning; surrounding cleaning still applies.

Guard test: the production change that would make this fail is choosing a marker that
prepare-text mangles (e.g. one using brackets, underscores, or collapsible whitespace),
or adding a cleaning step that strips it.
"""

import logging
import pathlib
import tempfile

from podcast_shared import ASIDE_MARKER, BLOCKQUOTE_MARKER, LISTENING_TIME_MARKER, split_intro, split_metadata

import prepare_text as pt

QUOTE_LINE = f"{BLOCKQUOTE_MARKER}A quoted passage worth voicing distinctly."
ASIDE_LINE = f"{ASIDE_MARKER}The author shares a tweet from @x: hello."
BODY = f"Author writes this with a link http://x.com in it.\n\n{QUOTE_LINE}\n\nAuthor responds in their own words."


def _require(cond: bool, msg: str) -> None:  # noqa: FBT001
    if not cond:
        raise AssertionError(msg)


def _run_process(raw_text: str, tmp: pathlib.Path) -> str:
    for name in ("CLEANED_OUTPUT_DIR", "RAW_ARCHIVE_DIR", "CLEANED_ARCHIVE_DIR", "FILTERED_DIR"):
        d = tmp / name
        d.mkdir(parents=True, exist_ok=True)
        setattr(pt, name, str(d))
    src = tmp / "20260813-120000-Some Author- Post.txt"
    _ = src.write_text(raw_text, encoding="utf-8")
    stats: dict[str, pt.FileStats] = {}
    pt.process_file(src, {}, stats)
    cleaned = (tmp / "CLEANED_OUTPUT_DIR" / src.name).read_text(encoding="utf-8")
    _, body = split_metadata(cleaned)
    return body


def check_marker_survives_and_cleaning_applies() -> None:
    """Keep the marked quote line intact while still cleaning the URL beside it."""
    raw = "META_FROM: Some Author\nMETA_SOURCE_KIND: substack\nMETA_INTAKE_TYPE: email\n\n" + BODY
    with tempfile.TemporaryDirectory() as d:
        body = _run_process(raw, pathlib.Path(d))
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    _require(QUOTE_LINE in lines, f"marked quote line not preserved: {body!r}")
    _require(body.count(BLOCKQUOTE_MARKER) == 1, f"marker count changed: {body!r}")
    _require("http://x.com" not in body, f"URL cleaning stopped working: {body!r}")


def check_aside_marker_survives() -> None:
    """Keep the rendered-embed aside marker intact through cleaning, like the quote marker."""
    body_text = f"Author writes this.\n\n{ASIDE_LINE}\n\nAuthor continues."
    raw = "META_FROM: Some Author\nMETA_SOURCE_KIND: substack\nMETA_INTAKE_TYPE: email\n\n" + body_text
    with tempfile.TemporaryDirectory() as d:
        body = _run_process(raw, pathlib.Path(d))
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    _require(ASIDE_LINE in lines, f"aside line not preserved: {body!r}")
    _require(body.count(ASIDE_MARKER) == 1, f"aside marker count changed: {body!r}")


def check_listening_time_marker_after_header() -> None:
    """Put the listening-time marker right after the author/title header prepare-text adds."""
    raw = "META_FROM: Some Author\nMETA_TITLE: A Post\nMETA_INTAKE_TYPE: email\n\nThe body starts here."
    with tempfile.TemporaryDirectory() as d:
        body = _run_process(raw, pathlib.Path(d))
    _require(body.count(LISTENING_TIME_MARKER) == 1, f"expected one listening-time marker: {body!r}")
    intro, rest = split_intro(body)
    _require(intro == "Some Author.\nA Post.", f"intro should be the header: {intro!r}")
    _require(rest.startswith("The body starts here"), f"body should follow the marker: {rest!r}")


def check_listening_time_marker_after_existing_byline() -> None:
    """Lead a title-only body with the author line, keeping the marker after the title."""
    raw = (
        "META_FROM: Example Blog\nMETA_TITLE: A Post\nMETA_INTAKE_TYPE: archive\n\n"
        "A Post\nOriginally published: 2013-05-12\n\nThe body starts here."
    )
    with tempfile.TemporaryDirectory() as d:
        body = _run_process(raw, pathlib.Path(d))
    intro, rest = split_intro(body)
    _require(intro == "Example Blog.\nA Post.", f"intro should be the author then the title: {intro!r}")
    _require(rest.startswith("Originally published"), f"body should follow the marker: {rest!r}")


if __name__ == "__main__":
    check_marker_survives_and_cleaning_applies()
    check_aside_marker_survives()
    check_listening_time_marker_after_header()
    check_listening_time_marker_after_existing_byline()
    logging.info("Marker survival tests passed.")
