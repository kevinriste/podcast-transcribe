"""archive-comments files bypass content-mutating cleaning; normal files do not."""

import logging
import pathlib
import tempfile

from podcast_shared import split_metadata

import prepare_text as pt

BODY = "NARRATOR: Here are the highlights.\n\nQUOTE Alice Reader: Making up [numbers] is dubious http://x.com\n"


def _require(cond: bool, msg: str) -> None:  # noqa: FBT001
    if not cond:
        raise AssertionError(msg)


def check_helper() -> None:
    """Check the passthrough predicate only fires for archive-comments."""
    _require(pt.is_passthrough({"intake_type": "archive-comments"}), "archive-comments should pass through")
    _require(not pt.is_passthrough({"intake_type": "archive"}), "archive should not pass through")
    _require(not pt.is_passthrough({}), "missing intake_type should not pass through")


def _run_process(raw_text: str, tmp: pathlib.Path) -> str:
    """Run process_file with all IO dirs redirected under tmp.

    Returns:
        The cleaned-output body (metadata stripped).

    """
    for name in ("CLEANED_OUTPUT_DIR", "RAW_ARCHIVE_DIR", "CLEANED_ARCHIVE_DIR", "FILTERED_DIR"):
        d = tmp / name
        d.mkdir(parents=True, exist_ok=True)
        setattr(pt, name, str(d))
    src = tmp / "20130502-120000-ARCHIVE-COMMENTS-x.txt"
    _ = src.write_text(raw_text, encoding="utf-8")
    stats: dict[str, pt.FileStats] = {}
    pt.process_file(src, {}, stats)
    cleaned = (tmp / "CLEANED_OUTPUT_DIR" / src.name).read_text(encoding="utf-8")
    _, body = split_metadata(cleaned)
    return body


def check_passthrough_integration() -> None:
    """Verify a real archive-comments file survives process_file byte-for-byte."""
    raw = "META_FROM: Example Blog\nMETA_INTAKE_TYPE: archive-comments\n\n" + BODY
    with tempfile.TemporaryDirectory() as d:
        body = _run_process(raw, pathlib.Path(d))
    _require("http://x.com" in body and "[numbers]" in body, f"passthrough stripped content: {body!r}")


def check_normal_is_cleaned() -> None:
    """Verify a normal file still has URLs cleaned."""
    raw = "META_FROM: Some Author\nMETA_INTAKE_TYPE: newsletter\n\n" + BODY
    with tempfile.TemporaryDirectory() as d:
        body = _run_process(raw, pathlib.Path(d))
    _require("http://x.com" not in body, f"normal file should have URL cleaned: {body!r}")


if __name__ == "__main__":
    check_helper()
    check_passthrough_integration()
    check_normal_is_cleaned()
    logging.info("All archive-comments passthrough tests passed.")
