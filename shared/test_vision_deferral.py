"""Tests for deferring items whose image descriptions fail (script-style; no network)."""

import contextlib
import json
import logging
import pathlib
import tempfile
from datetime import UTC, datetime

from podcast_shared import VisionRejectedError, VisionUnavailableError, parse_html_body, render_html_body
from podcast_shared.describe import Describer
from podcast_shared.vision_deferral import (
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


def _render(describer: Describer, html: str = _HTML) -> str:
    """Run ``html`` through the HTML stage with ``describer``.

    Returns:
        The rendered body text.

    """
    return render_html_body(parse_html_body(html), describer)


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
    """Inside the retry window, a failure propagates so the item is deferred."""
    describer = make_describer(_Vision({_CHART}), {}, VisionCircuit(), allow_undescribed=False, failures=[])
    try:
        _ = _render(describer)
    except VisionUnavailableError:
        return
    _fail("expected VisionUnavailableError to propagate")


def test_describer_after_expiry_falls_back_per_image() -> None:
    """After expiry a failing image falls back to its caption and the item still renders (review #3)."""
    failures: list[str] = []
    describer = make_describer(_Vision({_CHART}), {}, VisionCircuit(), allow_undescribed=True, failures=failures)
    body = _render(describer)
    if not body or "Quarterly revenue" not in body:
        _fail(f"caption fallback missing for the failing image: {body!r}")
    if _CHART not in failures:
        _fail(f"failure not recorded: {failures!r}")
    # A description obtained on an earlier run is still used for the other image.
    describer2 = make_describer(
        _Vision({_CHART}), {_MAP: "Described map.png"}, VisionCircuit(), allow_undescribed=True, failures=[]
    )
    body2 = _render(describer2)
    if not body2 or "Described map.png" not in body2:
        _fail(f"cached description for the later image not used: {body2!r}")


def test_describer_outage_skips_later_items() -> None:
    """After an outage, later items are skipped (VisionSkippedError) without calling vision."""
    vision = _Vision({_CHART, _MAP})
    circuit = VisionCircuit()
    with contextlib.suppress(VisionUnavailableError):
        _ = _render(make_describer(vision, {}, circuit, allow_undescribed=False, failures=[]))
    calls_after_first = len(vision.calls)
    try:
        _ = _render(make_describer(vision, {}, circuit, allow_undescribed=False, failures=[]))
    except VisionSkippedError:
        pass
    else:
        _fail("second item should be skipped while the circuit is open")
    if len(vision.calls) != calls_after_first:
        _fail(f"vision called again after the circuit opened: {vision.calls!r}")


def test_describer_bad_url_does_not_trip_circuit() -> None:
    """An unfetchable image URL defers only its own item; the next item is still described (review #1)."""
    vision = _Vision(bad_urls={_CHART})
    circuit = VisionCircuit()
    try:
        _ = _render(make_describer(vision, {}, circuit, allow_undescribed=False, failures=[]))
    except VisionSkippedError:
        _fail("a bad URL is a real failure for its item, not a skip")
    except VisionUnavailableError:
        pass
    else:
        _fail("the item with the bad URL should defer")
    if circuit.tripped():
        _fail("a per-image download failure must not trip the circuit")
    body = _render(make_describer(vision, {}, circuit, allow_undescribed=False, failures=[]), _OTHER_HTML)
    if not body or "Described other.png" not in body:
        _fail(f"unrelated item should be described: {body!r}")


def test_describer_rejected_image_falls_back_and_is_reported() -> None:
    """A rejected image uses its caption right away and is listed for the alert (review #2)."""
    failures: list[str] = []
    describer = make_describer(
        _Vision(rejected={_CHART}), {}, VisionCircuit(), allow_undescribed=False, failures=failures
    )
    body = _render(describer)
    if not body or "Quarterly revenue" not in body or "Described map.png" not in body:
        _fail(f"rejected image should fall back while others are described: {body!r}")
    if failures != [_CHART]:
        _fail(f"rejected image not reported: {failures!r}")


def test_describer_time_budget_skips() -> None:
    """Once the run's vision time budget is spent, remaining items are skipped, not failed."""
    vision = _Vision()
    circuit = VisionCircuit(budget=0.0)
    try:
        _ = _render(make_describer(vision, {}, circuit, allow_undescribed=False, failures=[]))
    except VisionSkippedError:
        pass
    else:
        _fail("an exhausted budget should skip the item")
    if vision.calls:
        _fail("vision should not be called over budget")


def test_describer_fills_and_reuses_cache() -> None:
    """New descriptions go into the cache; cached ones are never re-requested."""
    cache: dict[str, str] = {_CHART: "A cached chart"}
    vision = _Vision()
    describer = make_describer(vision, cache, VisionCircuit(), allow_undescribed=False, failures=[])
    body = _render(describer)
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
        if store.plan("7", _NOW).action != "process":
            _fail("unknown item should be processed")
        if not store.record_failure("7", {_CHART: "kept", _MAP: ""}, _NOW):
            _fail("first failure should report a new deferral")
        store.save()
        store = DeferralStore(path)
        if store.plan("7", _NOW + RETRY_INTERVAL / 2).action != "wait":
            _fail("retry before the interval should wait")
        due = store.plan("7", _NOW + RETRY_INTERVAL)
        if due.action != "process" or due.allow_undescribed or due.cache != {_CHART: "kept"}:
            _fail(f"due retry wrong: {due!r}")
        if store.record_failure("7", due.cache, _NOW + RETRY_INTERVAL):
            _fail("a repeat failure is not a new deferral")
        if store.entries["7"]["first_deferred"] != _NOW.isoformat():
            _fail("first_deferred must not move on repeat failures")
        expired = store.plan("7", _NOW + DEFER_LIMIT)
        if expired.action != "process" or not expired.allow_undescribed:
            _fail(f"expired deferral should process with fallback: {expired!r}")
        if not store.clear("7") or "7" in store.entries:
            _fail("clear should remove the entry")
        _ = store.record_failure("8", {}, _NOW)
        if not store.prune({"9"}) or store.entries:
            _fail("entries for items no longer waiting should be pruned")


def test_store_ignores_malformed_state() -> None:
    """Garbage, naive timestamps and non-string descriptions never crash or leak (review #4)."""
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "vision-deferrals.json"
        good = {"first_deferred": _NOW.isoformat(), "last_attempt": _NOW.isoformat()}
        for garbage in (
            "not json",
            "[1, 2]",
            json.dumps({"1": "nope"}),
            json.dumps({"1": {**good, "first_deferred": "2026-09-24T12:00:00"}}),  # naive
            json.dumps({"1": {**good, "last_attempt": "yesterday"}}),
            json.dumps({"1": {**good, "first_deferred": None}}),
        ):
            _ = path.write_text(garbage, encoding="utf-8")
            store = DeferralStore(path)
            if store.entries:
                _fail(f"malformed state {garbage!r} should load as empty: {store.entries!r}")
            _ = store.plan("1", _NOW)  # must not raise
        descs = {"u": None, "v": "ok", "w": 3}
        _ = path.write_text(json.dumps({"1": {**good, "descriptions": descs}}), encoding="utf-8")
        if DeferralStore(path).entries["1"]["descriptions"] != {"v": "ok"}:
            _fail("non-string descriptions must be dropped (no spoken 'None')")


def test_store_stash_keeps_progress_without_an_attempt() -> None:
    """stash() keeps descriptions from a skipped run: new entries are due at once and unalerted."""
    with tempfile.TemporaryDirectory() as d:
        store = DeferralStore(pathlib.Path(d) / "s.json")
        if not store.stash("201", {}, _NOW):
            _fail("a skipped item should start its deferral clock even with nothing cached")
        if store.plan("201", _NOW + DEFER_LIMIT).allow_undescribed is not True:
            _fail("the skipped item's 24h clock should run from the skip")
        if not store.stash("101", {_CHART: "A chart."}, _NOW):
            _fail("progress should be kept")
        if store.stash("101", {_CHART: "A chart."}, _NOW):
            _fail("unchanged progress should not need a save")
        plan = store.plan("101", _NOW)
        if plan.action != "process" or plan.cache != {_CHART: "A chart."}:
            _fail(f"stashed item should be due with its cache: {plan!r}")
        if not store.record_failure("101", plan.cache, _NOW):
            _fail("the first real failure after a stash should still alert")
        if store.record_failure("101", plan.cache, _NOW):
            _fail("a repeat failure should not alert")


def test_store_save_is_atomic() -> None:
    """save() leaves no temp files behind and replaces the file whole."""
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "vision-deferrals.json"
        store = DeferralStore(path)
        _ = store.record_failure("1", {}, _NOW)
        store.save()
        if sorted(p.name for p in pathlib.Path(d).iterdir()) != ["vision-deferrals.json"]:
            _fail(f"temp files left behind: {list(pathlib.Path(d).iterdir())}")
        if "1" not in DeferralStore(path).entries:
            _fail("saved state did not round-trip")


if __name__ == "__main__":
    test_describer_defers_on_first_failure()
    test_describer_after_expiry_falls_back_per_image()
    test_describer_outage_skips_later_items()
    test_describer_bad_url_does_not_trip_circuit()
    test_describer_rejected_image_falls_back_and_is_reported()
    test_describer_time_budget_skips()
    test_describer_fills_and_reuses_cache()
    test_store_plan_record_clear_prune()
    test_store_ignores_malformed_state()
    test_store_stash_keeps_progress_without_an_attempt()
    test_store_save_is_atomic()
    logging.info("vision deferral tests passed")
