"""HTML raw files go through the HTML stage: extraction, image descriptions, and deferral.

Drives ``process_files()`` end to end over temp directories, with a scripted vision
function and captured Gotify alerts (no network).
"""

import logging
import pathlib
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import NoReturn
from unittest.mock import patch

from podcast_shared import VisionRejectedError, VisionUnavailableError, split_intro, split_metadata
from podcast_shared.vision_deferral import DEFER_LIMIT, DeferralStore

import html_stage
import prepare_text as pt

logging.basicConfig(level=logging.INFO)

_HTML = (
    '<html><body><div class="body markup"><p>Intro paragraph.</p>'
    '<figure><img src="https://example.com/chart.png" alt="" width="600" height="400">'
    "<figcaption>Quarterly revenue</figcaption></figure>"
    '<figure><img src="https://example.com/map.png" alt="" width="600" height="400">'
    "<figcaption>Regional map</figcaption></figure>"
    "<p>Closing paragraph.</p></div></body></html>"
)
_OTHER_HTML = (
    '<html><body><div class="body markup"><p>Other post.</p>'
    '<figure><img src="https://example.com/other.png" alt="" width="600" height="400">'
    "<figcaption>Other figure</figcaption></figure></div></body></html>"
)
_CHART = "https://example.com/chart.png"
_MAP = "https://example.com/map.png"
_OTHER = "https://example.com/other.png"
_DEFERRED = "Episode deferred: image descriptions unavailable"
_FALLBACK = "Image descriptions unavailable"
_FIRST = "20260924-120000-Example Letter- Example post.txt"
_SECOND = "20260924-120100-Example Letter- Other post.txt"


def _fail(msg: str) -> NoReturn:
    raise AssertionError(msg)


class _Vision:
    """Scripted describe_image recording calls: ``failing`` = outage, ``bad_urls``, ``rejected``."""

    def __init__(
        self, failing: set[str] | None = None, *, bad_urls: set[str] | None = None, rejected: set[str] | None = None
    ) -> None:
        """Set which image URLs fail, and how."""
        self.failing: set[str] = failing or set()
        self.bad_urls: set[str] = bad_urls or set()
        self.rejected: set[str] = rejected or set()
        self.calls: list[str] = []

    def __call__(self, src: str, alt: str, caption: str) -> str:
        """Describe ``src``.

        Returns:
            A description naming the image.

        Raises:
            VisionUnavailableError: For URLs in ``failing`` (outage) or ``bad_urls`` (not).
            VisionRejectedError: For URLs in ``rejected``.

        """
        _ = (alt, caption)
        self.calls.append(src)
        if src in self.failing:
            msg = f"vision unavailable for {src}"
            raise VisionUnavailableError(msg)
        if src in self.bad_urls:
            msg = f"could not download {src}"
            raise VisionUnavailableError(msg, outage=False)
        if src in self.rejected:
            msg = f"rejected {src}"
            raise VisionRejectedError(msg)
        return f"Described {src.rsplit('/', 1)[-1]}"


def _email_file(html: str = _HTML, title: str = "Example post") -> str:
    return (
        f"META_FROM: Example Letter\nMETA_TITLE: {title}\nMETA_SOURCE_URL: https://example.com/p/post\n"
        f"META_SOURCE_KIND: substack\nMETA_INTAKE_TYPE: email\nMETA_BODY_FORMAT: html\n\n{html}"
    )


def _run(tmp: pathlib.Path, vision: _Vision, files: dict[str, str] | None = None) -> list[str]:
    """Write ``files`` into the raw dir, run process_files() once, and collect alerts.

    Returns:
        The titles of the Gotify alerts sent.

    """
    raw = tmp / "raw"
    raw.mkdir(exist_ok=True)
    for name, text in (files or {}).items():
        _ = (raw / name).write_text(text, encoding="utf-8")
    alerts: list[str] = []

    def notify(title: str, message: str, *_args: object, **_kwargs: object) -> None:
        _ = message
        alerts.append(title)

    with ExitStack() as stack:
        dirs = {
            "RAW_INPUT_DIR": raw,
            "RAW_ARCHIVE_DIR": tmp / "raw-archive",
            "CLEANED_OUTPUT_DIR": tmp / "cleaned",
            "CLEANED_ARCHIVE_DIR": tmp / "cleaned-archive",
            "FILTERED_DIR": tmp / "filtered",
            "STATS_DIR": tmp / "stats",
        }
        for name, path in dirs.items():
            _ = stack.enter_context(patch.object(pt, name, str(path)))
        _ = stack.enter_context(patch.object(pt, "load_config", dict))
        _ = stack.enter_context(patch.object(pt, "send_gotify_notification", notify))
        _ = stack.enter_context(patch.object(html_stage, "send_gotify_notification", notify))
        _ = stack.enter_context(patch.object(html_stage, "describe_image", vision))
        _ = stack.enter_context(patch.object(html_stage, "VISION_DEFERRALS_PATH", tmp / "vision-deferrals.json"))
        pt.process_files()
    return alerts


def _state(tmp: pathlib.Path) -> DeferralStore:
    return DeferralStore(tmp / "vision-deferrals.json")


def _shift_state(tmp: pathlib.Path, *, first: timedelta, last: timedelta) -> None:
    """Move every deferral's timestamps into the past to simulate elapsed time."""
    store = _state(tmp)
    now = datetime.now(tz=UTC)
    for entry in store.entries.values():
        entry["first_deferred"] = (now - first).isoformat()
        entry["last_attempt"] = (now - last).isoformat()
    store.save()


def _cleaned(tmp: pathlib.Path, name: str) -> tuple[dict[str, str], str]:
    return split_metadata((tmp / "cleaned" / name).read_text(encoding="utf-8"))


def test_defers_waits_retries_and_publishes() -> None:
    """Full lifecycle: defer + alert once, wait, retry, publish, clear state."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        raw_file = tmp / "raw" / _FIRST

        # Run 1: vision down -> file stays in raw, one alert, nothing cleaned.
        alerts = _run(tmp, _Vision({_CHART, _MAP}), {_FIRST: _email_file()})
        if not raw_file.exists() or (tmp / "cleaned" / _FIRST).exists():
            _fail("a deferred file must stay in raw and produce nothing")
        if alerts != [_DEFERRED]:
            _fail(f"expected one deferral alert, got {alerts!r}")

        # Run 2 (20 min later): retry not due -> vision not called, no alert.
        vision = _Vision()
        alerts = _run(tmp, vision)
        if vision.calls or alerts or not raw_file.exists():
            _fail(f"should wait: calls={vision.calls!r} alerts={alerts!r}")

        # Run 3 (after the interval): still down -> stays deferred, no second alert.
        _shift_state(tmp, first=timedelta(hours=2), last=timedelta(hours=2))
        alerts = _run(tmp, _Vision({_CHART, _MAP}))
        if alerts or not raw_file.exists():
            _fail(f"repeat failure should not alert again: {alerts!r}")

        # Run 4 (after the interval): vision back -> published, raw consumed, state cleared.
        _shift_state(tmp, first=timedelta(hours=3), last=timedelta(hours=2))
        _ = _run(tmp, _Vision())
        if raw_file.exists():
            _fail("published file should leave raw")
        metadata, body = _cleaned(tmp, _FIRST)
        if "Described chart.png" not in body or "Described map.png" not in body:
            _fail(f"episode text not written with descriptions: {body!r}")
        if metadata.get("extraction") != "structured" or "body_format" in metadata:
            _fail(f"cleaned headers wrong: {metadata!r}")
        if _state(tmp).entries:
            _fail("state should be empty after publishing")


def test_expired_deferral_publishes_with_fallback() -> None:
    """Past the limit, the file publishes with caption/alt, alerts, and clears the deferral."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _ = _run(tmp, _Vision({_CHART, _MAP}), {_FIRST: _email_file()})
        _shift_state(tmp, first=DEFER_LIMIT + timedelta(minutes=1), last=timedelta(hours=2))
        alerts = _run(tmp, _Vision({_CHART, _MAP}))
        if alerts.count(_FALLBACK) != 1:
            _fail(f"expected the fallback alert: {alerts!r}")
        _, body = _cleaned(tmp, _FIRST)
        if "Quarterly revenue" not in body or "Regional map" not in body:
            _fail(f"captions missing from fallback text: {body!r}")
        if _state(tmp).entries:
            _fail("state should be empty after the fallback publish")


def test_outage_alerts_once_and_skips_the_rest() -> None:
    """During an outage the first file is deferred with one alert; later ones are skipped quietly."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        vision = _Vision({_CHART, _MAP, _OTHER})
        alerts = _run(tmp, vision, {_FIRST: _email_file(), _SECOND: _email_file(_OTHER_HTML, "Other post")})
        if alerts != [_DEFERRED]:
            _fail(f"expected exactly one alert, got {alerts!r}")
        if _OTHER in vision.calls:
            _fail(f"second file should be skipped: calls={vision.calls!r}")
        entries = _state(tmp).entries
        if set(entries) != {_FIRST, _SECOND} or entries[_SECOND]["alerted"]:
            _fail(f"both clocks should start, only the attempted one alerted: {entries!r}")


def test_bad_url_does_not_block_other_files() -> None:
    """A file with an unfetchable image defers alone; the next file publishes the same run."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        alerts = _run(
            tmp, _Vision(bad_urls={_CHART}), {_FIRST: _email_file(), _SECOND: _email_file(_OTHER_HTML, "Other post")}
        )
        if not (tmp / "cleaned" / _SECOND).exists() or (tmp / "cleaned" / _FIRST).exists():
            _fail("only the unrelated file should publish")
        if _DEFERRED not in alerts:
            _fail(f"the bad-URL file should alert as deferred: {alerts!r}")


def test_rejected_image_publishes_with_alert() -> None:
    """A rejected image doesn't defer; the file publishes now and the fallback is reported."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        alerts = _run(tmp, _Vision(rejected={_CHART}), {_FIRST: _email_file()})
        if not (tmp / "cleaned" / _FIRST).exists() or _FALLBACK not in alerts:
            _fail(f"expected publish + fallback alert: {alerts!r}")


def test_prunes_files_no_longer_waiting() -> None:
    """A deferral for a file removed from raw by hand is dropped from state."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _ = _run(tmp, _Vision({_CHART, _MAP}), {_FIRST: _email_file()})
        (tmp / "raw" / _FIRST).unlink()
        _ = _run(tmp, _Vision())
        if _state(tmp).entries:
            _fail("deferral for a file no longer in raw should be pruned")


def test_selector_preface_and_relative_images() -> None:
    """An archive page: the selector picks the article, the preface leads it, image URLs resolve."""
    page = (
        '<html><body><div class="sidebar"><p>Sponsored by someone.</p></div>'
        '<div class="post"><p>The post body.</p><img src="/images/graph.png" alt=""></div></body></html>'
    )
    headers = (
        "META_FROM: Example Blog\nMETA_TITLE: A Post\nMETA_SOURCE_URL: https://blog.example.com/2013/05/12/a-post/\n"
        "META_SOURCE_KIND: archive\nMETA_INTAKE_TYPE: archive\nMETA_BODY_FORMAT: html\n"
        "META_CONTENT_SELECTOR: div.post\nMETA_PREFACE: Originally published: 2013-05-12\n\n"
    )
    raw = headers + page
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        vision = _Vision()
        _ = _run(tmp, vision, {_FIRST: raw})
        metadata, body = _cleaned(tmp, _FIRST)
    if vision.calls != ["https://blog.example.com/images/graph.png"]:
        _fail(f"relative image URL not resolved: {vision.calls!r}")
    intro, rest = split_intro(body)
    if intro != "Example Blog.\nA Post.":
        _fail(f"intro should be the author and title: {intro!r}")
    if not rest.lstrip().startswith("Originally published: 2013-05-12."):
        _fail(f"preface should open the body: {rest!r}")
    if "Sponsored" in body or "Described graph.png" not in body:
        _fail(f"selector or image description wrong: {body!r}")
    if {"content_selector", "preface", "body_format"} & set(metadata):
        _fail(f"stage headers should be dropped: {metadata!r}")


def test_plain_text_file_skips_the_stage() -> None:
    """A plain-text raw file is cleaned as before, with no vision calls."""
    raw = "META_FROM: Example Letter\nMETA_TITLE: Example post\nMETA_INTAKE_TYPE: rss\n\nJust some text."
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        vision = _Vision()
        _ = _run(tmp, vision, {_FIRST: raw})
        _, body = _cleaned(tmp, _FIRST)
    if vision.calls or "Just some text." not in body:
        _fail(f"plain text path changed: calls={vision.calls!r} body={body!r}")


if __name__ == "__main__":
    test_defers_waits_retries_and_publishes()
    test_expired_deferral_publishes_with_fallback()
    test_outage_alerts_once_and_skips_the_rest()
    test_bad_url_does_not_block_other_files()
    test_rejected_image_publishes_with_alert()
    test_prunes_files_no_longer_waiting()
    test_selector_preface_and_relative_images()
    test_plain_text_file_skips_the_stage()
    logging.info("HTML stage tests passed")
