"""Tests for deferring emails whose image descriptions fail (script-style; no network)."""

import contextlib
import json
import logging
import pathlib
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from email.mime.text import MIMEText
from typing import ClassVar, Self
from unittest.mock import MagicMock, patch

from imap_tools.message import MailMessage
from podcast_shared import VisionRejectedError, VisionUnavailableError

import parse_email as pe
from vision_deferral import (
    DEFER_LIMIT,
    RETRY_INTERVAL,
    DeferralStore,
    VisionCircuit,
    VisionSkippedError,
    make_describer,
)

logging.basicConfig(level=logging.INFO)

_HTML = (
    '<html><body><div class="body markup"><p>Intro paragraph.</p>'
    '<figure><img src="https://example.com/chart.png" alt="" width="600" height="400">'
    "<figcaption>Quarterly revenue</figcaption></figure>"
    '<figure><img src="https://example.com/map.png" alt="" width="600" height="400">'
    "<figcaption>Regional map</figcaption></figure>"
    "<p>Closing paragraph.</p></div></body></html>"
)
_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
_CHART = "https://example.com/chart.png"
_MAP = "https://example.com/map.png"
_OTHER = "https://example.com/other.png"
_OTHER_HTML = _HTML.replace("chart.png", "other.png").replace("map.png", "other2.png")


def _fail(msg: str) -> None:
    """Raise an AssertionError.

    Raises:
        AssertionError: Always.

    """
    raise AssertionError(msg)


def _message(uid: str | None = "101", message_id: str = "<m101@example.com>", html: str = _HTML) -> MailMessage:
    mime = MIMEText(html, "html")
    mime["Subject"] = "Example post"
    mime["From"] = "Example Letter <hi@example.com>"
    mime["Date"] = "Thu, 24 Sep 2026 12:00:00 +0000"
    mime["Message-ID"] = message_id
    raw = mime.as_bytes()
    # Shaped like an IMAP FETCH response item, which is where MailMessage reads the UID from.
    envelope = f"1 (UID {uid} RFC822 {{{len(raw)}}}".encode() if uid else b""
    return MailMessage([(envelope, raw)])


class _Vision:
    """Scripted describe_image, recording every call.

    URLs in ``failing`` fail like an outage, in ``bad_urls`` like an unfetchable URL, and in
    ``rejected`` like an image the API refuses.
    """

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


# ---- describer wrapper ------------------------------------------------------------


def test_describer_defers_on_first_failure() -> None:
    """Inside the retry window, a failure propagates so the email stays unseen."""
    describer = make_describer(_Vision({_CHART}), {}, VisionCircuit(), allow_undescribed=False, failures=[])
    try:
        _ = pe.extract_body_from_html(_message(), describer=describer)
    except VisionUnavailableError:
        return
    _fail("expected VisionUnavailableError to propagate")


def test_describer_after_expiry_falls_back_per_image() -> None:
    """After expiry a failing image falls back to its caption and the email still extracts (review #3)."""
    failures: list[str] = []
    describer = make_describer(_Vision({_CHART}), {}, VisionCircuit(), allow_undescribed=True, failures=failures)
    body, _structural = pe.extract_body_from_html(_message(), describer=describer)
    if not body or "Quarterly revenue" not in body:
        _fail(f"caption fallback missing for the failing image: {body!r}")
    if _CHART not in failures:
        _fail(f"failure not recorded: {failures!r}")
    # A description obtained on an earlier run is still used for the other image.
    describer2 = make_describer(
        _Vision({_CHART}), {_MAP: "Described map.png"}, VisionCircuit(), allow_undescribed=True, failures=[]
    )
    body2, _ = pe.extract_body_from_html(_message(), describer=describer2)
    if not body2 or "Described map.png" not in body2:
        _fail(f"cached description for the later image not used: {body2!r}")


def test_describer_outage_skips_later_emails() -> None:
    """After an outage, later emails are skipped (VisionSkippedError) without calling vision."""
    vision = _Vision({_CHART, _MAP})
    circuit = VisionCircuit()
    with contextlib.suppress(VisionUnavailableError):
        _ = pe.extract_body_from_html(
            _message(), describer=make_describer(vision, {}, circuit, allow_undescribed=False, failures=[])
        )
    calls_after_first = len(vision.calls)
    try:
        _ = pe.extract_body_from_html(
            _message(uid="102"), describer=make_describer(vision, {}, circuit, allow_undescribed=False, failures=[])
        )
    except VisionSkippedError:
        pass
    else:
        _fail("second email should be skipped while the circuit is open")
    if len(vision.calls) != calls_after_first:
        _fail(f"vision called again after the circuit opened: {vision.calls!r}")


def test_describer_bad_url_does_not_trip_circuit() -> None:
    """An unfetchable image URL defers only its own email; the next email is still described (review #1)."""
    vision = _Vision(bad_urls={_CHART})
    circuit = VisionCircuit()
    try:
        _ = pe.extract_body_from_html(
            _message(), describer=make_describer(vision, {}, circuit, allow_undescribed=False, failures=[])
        )
    except VisionSkippedError:
        _fail("a bad URL is a real failure for its email, not a skip")
    except VisionUnavailableError:
        pass
    else:
        _fail("the email with the bad URL should defer")
    if circuit.tripped():
        _fail("a per-image download failure must not trip the circuit")
    body, _ = pe.extract_body_from_html(
        _message(uid="102", html=_OTHER_HTML),
        describer=make_describer(vision, {}, circuit, allow_undescribed=False, failures=[]),
    )
    if not body or "Described other.png" not in body:
        _fail(f"unrelated email should be described: {body!r}")


def test_describer_rejected_image_falls_back_and_is_reported() -> None:
    """A rejected image uses its caption right away and is listed for the alert (review #2)."""
    failures: list[str] = []
    describer = make_describer(
        _Vision(rejected={_CHART}), {}, VisionCircuit(), allow_undescribed=False, failures=failures
    )
    body, _ = pe.extract_body_from_html(_message(), describer=describer)
    if not body or "Quarterly revenue" not in body or "Described map.png" not in body:
        _fail(f"rejected image should fall back while others are described: {body!r}")
    if failures != [_CHART]:
        _fail(f"rejected image not reported: {failures!r}")


def test_describer_time_budget_skips() -> None:
    """Once the run's vision time budget is spent, remaining emails are skipped, not failed."""
    vision = _Vision()
    circuit = VisionCircuit(budget=0.0)
    try:
        _ = pe.extract_body_from_html(
            _message(), describer=make_describer(vision, {}, circuit, allow_undescribed=False, failures=[])
        )
    except VisionSkippedError:
        pass
    else:
        _fail("an exhausted budget should skip the email")
    if vision.calls:
        _fail("vision should not be called over budget")


def test_describer_fills_and_reuses_cache() -> None:
    """New descriptions go into the cache; cached ones are never re-requested."""
    cache: dict[str, str] = {_CHART: "A cached chart"}
    vision = _Vision()
    describer = make_describer(vision, cache, VisionCircuit(), allow_undescribed=False, failures=[])
    body, _ = pe.extract_body_from_html(_message(), describer=describer)
    if vision.calls != [_MAP]:
        _fail(f"only the uncached image should hit vision: {vision.calls!r}")
    if cache.get(_MAP) != "Described map.png" or not body or "A cached chart" not in body:
        _fail(f"cache not used/filled: {cache!r} {body!r}")


# ---- state store ------------------------------------------------------------------


def test_store_plan_record_clear_prune() -> None:
    """plan/record_failure/clear/prune follow the retry schedule and survive a save/load."""
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "vision-deferrals.json"
        store = DeferralStore(path)
        if store.plan("7", "<a>", _NOW).action != "process":
            _fail("unknown email should be processed")
        if not store.plan("", "<a>", _NOW).allow_undescribed:
            _fail("an email without a UID can't be retried, so it must not defer")
        if not store.record_failure("7", "<a>", {_CHART: "kept", _MAP: ""}, _NOW):
            _fail("first failure should report a new deferral")
        store.save()
        store = DeferralStore(path)
        if store.plan("7", "<a>", _NOW + RETRY_INTERVAL / 2).action != "wait":
            _fail("retry before the interval should wait")
        due = store.plan("7", "<a>", _NOW + RETRY_INTERVAL)
        if due.action != "process" or due.allow_undescribed or due.cache != {_CHART: "kept"}:
            _fail(f"due retry wrong: {due!r}")
        if store.record_failure("7", "<a>", due.cache, _NOW + RETRY_INTERVAL):
            _fail("a repeat failure is not a new deferral")
        if store.entries["7"]["first_deferred"] != _NOW.isoformat():
            _fail("first_deferred must not move on repeat failures")
        expired = store.plan("7", "<a>", _NOW + DEFER_LIMIT)
        if expired.action != "process" or not expired.allow_undescribed:
            _fail(f"expired deferral should process with fallback: {expired!r}")
        reused = store.plan("7", "<other>", _NOW)
        if reused.action != "process" or reused.cache or reused.allow_undescribed:
            _fail("a reused UID with a different Message-ID must start fresh")
        if not store.clear("7") or "7" in store.entries:
            _fail("clear should remove the entry")
        _ = store.record_failure("8", "<b>", {}, _NOW)
        if not store.prune({"9"}) or store.entries:
            _fail("entries for emails no longer unseen should be pruned")


def test_store_ignores_malformed_state() -> None:
    """Garbage, naive timestamps and non-string descriptions never crash or leak (review #4)."""
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "vision-deferrals.json"
        good = {"message_id": "<a>", "first_deferred": _NOW.isoformat(), "last_attempt": _NOW.isoformat()}
        for garbage in (
            "not json",
            "[1, 2]",
            json.dumps({"1": "nope"}),
            json.dumps({"1": {**good, "first_deferred": "2026-09-24T12:00:00"}}),  # naive
            json.dumps({"1": {**good, "last_attempt": "yesterday"}}),
            json.dumps({"1": {**good, "message_id": None}}),
        ):
            _ = path.write_text(garbage, encoding="utf-8")
            store = DeferralStore(path)
            if store.entries:
                _fail(f"malformed state {garbage!r} should load as empty: {store.entries!r}")
            _ = store.plan("1", "<a>", _NOW)  # must not raise
        descs = {"u": None, "v": "ok", "w": 3}
        _ = path.write_text(json.dumps({"1": {**good, "descriptions": descs}}), encoding="utf-8")
        if DeferralStore(path).entries["1"]["descriptions"] != {"v": "ok"}:
            _fail("non-string descriptions must be dropped (no spoken 'None')")


def test_store_stash_keeps_progress_without_an_attempt() -> None:
    """stash() keeps descriptions from a skipped run: new entries are due at once and unalerted."""
    with tempfile.TemporaryDirectory() as d:
        store = DeferralStore(pathlib.Path(d) / "s.json")
        if not store.stash("201", "<m2>", {}, _NOW):
            _fail("a skipped email should start its deferral clock even with nothing cached")
        if store.plan("201", "<m2>", _NOW + DEFER_LIMIT).allow_undescribed is not True:
            _fail("the skipped email's 24h clock should run from the skip")
        if not store.stash("101", "<m>", {_CHART: "A chart."}, _NOW):
            _fail("progress should be kept")
        if store.stash("101", "<m>", {_CHART: "A chart."}, _NOW):
            _fail("unchanged progress should not need a save")
        plan = store.plan("101", "<m>", _NOW)
        if plan.action != "process" or plan.cache != {_CHART: "A chart."}:
            _fail(f"stashed email should be due with its cache: {plan!r}")
        if not store.record_failure("101", "<m>", plan.cache, _NOW):
            _fail("the first real failure after a stash should still alert")
        if store.record_failure("101", "<m>", plan.cache, _NOW):
            _fail("a repeat failure should not alert")


def test_store_save_is_atomic() -> None:
    """save() leaves no temp files behind and replaces the file whole."""
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "vision-deferrals.json"
        store = DeferralStore(path)
        _ = store.record_failure("1", "<a>", {}, _NOW)
        store.save()
        if sorted(p.name for p in pathlib.Path(d).iterdir()) != ["vision-deferrals.json"]:
            _fail(f"temp files left behind: {list(pathlib.Path(d).iterdir())}")
        if "1" not in DeferralStore(path).entries:
            _fail("saved state did not round-trip")


# ---- main() end to end ------------------------------------------------------------


class _FakeMailBox:
    """Stands in for imap_tools.MailBox: serves ``messages`` and records SEEN flags."""

    messages: ClassVar[list[MailMessage]] = []
    flagged: ClassVar[list[str]] = []

    def __init__(self, host: str) -> None:
        """Accept the host like the real MailBox."""
        _ = host

    def login(self, user: str, password: str) -> Self:
        """Pretend to log in.

        Returns:
            Itself, as a context manager.

        """
        _ = (user, password)
        return self

    def __enter__(self) -> Self:
        """Enter the context.

        Returns:
            Itself.

        """
        return self

    def __exit__(self, *_: object) -> None:
        """Leave the context."""

    def fetch(self, *_: object, **_kwargs: object) -> list[MailMessage]:
        """Return the scripted unseen messages.

        Returns:
            The messages.

        """
        return list(self.messages)

    def flag(self, uid: str, *_: object, **_kwargs: object) -> None:
        """Record a message being marked seen."""
        type(self).flagged.append(uid)


def _run_main(tmp: pathlib.Path, vision: _Vision, messages: list[MailMessage]) -> list[str]:
    """Run parse_email.main() once against the fake mailbox.

    Returns:
        The titles of the Gotify alerts sent.

    """
    _FakeMailBox.messages = messages
    _FakeMailBox.flagged = []
    out = tmp / "raw"
    out.mkdir(exist_ok=True)
    alerts: list[str] = []

    def notify(title: str, message: str, *_args: object, **_kwargs: object) -> None:
        _ = message
        alerts.append(title)

    with ExitStack() as stack:
        _ = stack.enter_context(patch.object(pe, "MailBox", _FakeMailBox))
        _ = stack.enter_context(patch.object(pe, "gmail_user", "user"))
        _ = stack.enter_context(patch.object(pe, "gmail_password", "pw"))
        _ = stack.enter_context(patch.object(pe, "sources_config_file", str(tmp / "missing.yaml")))
        _ = stack.enter_context(patch.object(pe, "output_folder", str(out)))
        _ = stack.enter_context(patch.object(pe, "VISION_DEFERRALS_PATH", tmp / "vision-deferrals.json"))
        _ = stack.enter_context(patch.object(pe, "describe_image", vision))
        _ = stack.enter_context(patch.object(pe, "store_intake_html", MagicMock()))
        _ = stack.enter_context(patch.object(pe, "send_gotify_notification", notify))
        pe.main()
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


def test_main_defers_waits_retries_and_publishes() -> None:
    """Full lifecycle through main(): defer + alert once, wait, retry, publish, clear state."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        raw = tmp / "raw"

        # Run 1: vision down -> unseen, deferred, one alert, nothing written.
        notify = _run_main(tmp, _Vision({_CHART, _MAP}), [_message()])
        if _FakeMailBox.flagged or any(raw.iterdir()):
            _fail("a deferred email must stay unseen and write nothing")
        if notify != ["Email deferred: image descriptions unavailable"]:
            _fail(f"expected one deferral alert, got {notify!r}")
        if "101" not in _state(tmp).entries:
            _fail("deferral not saved")

        # Run 2 (20 min later): retry not due -> vision not called, no alert.
        vision = _Vision()
        notify = _run_main(tmp, vision, [_message()])
        if vision.calls or _FakeMailBox.flagged or notify:
            _fail(f"should wait: calls={vision.calls!r} flagged={_FakeMailBox.flagged!r} alerts={notify!r}")

        # Run 3 (after the interval): still down -> stays deferred, no second alert.
        _shift_state(tmp, first=timedelta(hours=2), last=timedelta(hours=2))
        notify = _run_main(tmp, _Vision({_CHART, _MAP}), [_message()])
        if notify or _FakeMailBox.flagged:
            _fail(f"repeat failure should not alert again: {notify!r}")

        # Run 4 (after the interval): vision back -> published, marked seen, state cleared.
        _shift_state(tmp, first=timedelta(hours=3), last=timedelta(hours=2))
        _ = _run_main(tmp, _Vision(), [_message()])
        if _FakeMailBox.flagged != ["101"]:
            _fail(f"published email should be marked seen: {_FakeMailBox.flagged!r}")
        written = list(raw.iterdir())
        if len(written) != 1 or "Described chart.png" not in written[0].read_text(encoding="utf-8"):
            _fail(f"episode text not written with descriptions: {written!r}")
        if _state(tmp).entries:
            _fail("state should be empty after publishing")


def test_main_expired_deferral_publishes_with_fallback() -> None:
    """Past the limit, main() publishes with caption/alt, alerts, and clears the deferral."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _ = _run_main(tmp, _Vision({_CHART, _MAP}), [_message()])
        _shift_state(tmp, first=DEFER_LIMIT + timedelta(minutes=1), last=timedelta(hours=2))
        notify = _run_main(tmp, _Vision({_CHART, _MAP}), [_message()])
        if _FakeMailBox.flagged != ["101"]:
            _fail("expired email should publish")
        if notify.count("Image descriptions unavailable") != 1:
            _fail(f"expected the fallback alert: {notify!r}")
        text = next((tmp / "raw").iterdir()).read_text(encoding="utf-8")
        if "Quarterly revenue" not in text or "Regional map" not in text:
            _fail(f"captions missing from fallback text: {text!r}")
        if _state(tmp).entries:
            _fail("state should be empty after the fallback publish")


def test_main_outage_alerts_once_and_skips_the_rest() -> None:
    """During an outage the first email is deferred with one alert; later ones are skipped quietly."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        other = _message(uid="102", message_id="<m102@example.com>", html=_OTHER_HTML)
        vision = _Vision({_CHART, _MAP, _OTHER})
        notify = _run_main(tmp, vision, [_message(), other])
        if notify != ["Email deferred: image descriptions unavailable"]:
            _fail(f"expected exactly one alert, got {notify!r}")
        if _FakeMailBox.flagged or _OTHER in vision.calls:
            _fail(f"second email should be skipped: flagged={_FakeMailBox.flagged!r} calls={vision.calls!r}")
        entries = _state(tmp).entries
        if set(entries) != {"101", "102"} or entries["102"]["alerted"]:
            _fail(f"both clocks should start, only the attempted one alerted: {entries!r}")


def test_main_bad_url_does_not_block_other_emails() -> None:
    """An email with an unfetchable image defers alone; the next email publishes the same run."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        other = _message(uid="102", message_id="<m102@example.com>", html=_OTHER_HTML)
        notify = _run_main(tmp, _Vision(bad_urls={_CHART}), [_message(), other])
        if _FakeMailBox.flagged != ["102"]:
            _fail(f"unrelated email should publish: {_FakeMailBox.flagged!r}")
        if "Email deferred: image descriptions unavailable" not in notify:
            _fail(f"the bad-URL email should alert as deferred: {notify!r}")


def test_main_rejected_image_publishes_with_alert() -> None:
    """A rejected image doesn't defer; the email publishes now and the fallback is reported."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        notify = _run_main(tmp, _Vision(rejected={_CHART}), [_message()])
        if _FakeMailBox.flagged != ["101"] or "Image descriptions unavailable" not in notify:
            _fail(f"expected publish + fallback alert: flagged={_FakeMailBox.flagged!r} alerts={notify!r}")


def test_main_prunes_emails_read_elsewhere() -> None:
    """A deferred email that is no longer unseen (read by hand) is dropped from state."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _ = _run_main(tmp, _Vision({_CHART, _MAP}), [_message()])
        _ = _run_main(tmp, _Vision(), [])
        if _state(tmp).entries:
            _fail("deferral for an email no longer unseen should be pruned")


if __name__ == "__main__":
    test_describer_defers_on_first_failure()
    test_describer_after_expiry_falls_back_per_image()
    test_describer_outage_skips_later_emails()
    test_describer_bad_url_does_not_trip_circuit()
    test_describer_rejected_image_falls_back_and_is_reported()
    test_describer_time_budget_skips()
    test_describer_fills_and_reuses_cache()
    test_store_plan_record_clear_prune()
    test_store_ignores_malformed_state()
    test_store_stash_keeps_progress_without_an_attempt()
    test_store_save_is_atomic()
    test_main_defers_waits_retries_and_publishes()
    test_main_expired_deferral_publishes_with_fallback()
    test_main_outage_alerts_once_and_skips_the_rest()
    test_main_bad_url_does_not_block_other_emails()
    test_main_rejected_image_publishes_with_alert()
    test_main_prunes_emails_read_elsewhere()
    logging.info("vision deferral tests passed")
