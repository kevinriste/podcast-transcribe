"""Tests for TTS failure handling: retries wired in, per-file isolation, no half-built episodes."""

import io
import json
import logging
import os
import pathlib
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from google.api_core.exceptions import InvalidArgument, RetryError, ServiceUnavailable
from google.cloud import texttospeech
from google.genai import errors as genai_errors
from podcast_shared import pub_date_from_filename
from pydub import AudioSegment

import multivoice
import text_to_speech as tts

logging.basicConfig(level=logging.INFO)


def _fail(msg: str) -> None:
    """Raise an AssertionError.

    Raises:
        AssertionError: Always.

    """
    raise AssertionError(msg)


def _mp3_bytes() -> bytes:
    buf = io.BytesIO()
    _ = AudioSegment.silent(duration=50).export(buf, format="mp3")
    return buf.getvalue()


class _RecordingClient:
    """Fake TextToSpeechClient that records the ``retry`` and ``timeout`` passed to each call."""

    def __init__(self, *, reject_effects: bool = False) -> None:
        """Optionally reject the first (effects-profile) request like Studio voices do."""
        self.calls: list[tuple[object, object]] = []
        self.reject_effects: bool = reject_effects

    def synthesize_speech(
        self, *, request: dict[str, object], retry: object = None, timeout: object = None
    ) -> SimpleNamespace:
        """Record the retry policy and timeout, and return a tiny MP3.

        Returns:
            A response-like object with ``audio_content``.

        Raises:
            InvalidArgument: For the effects-profile request when ``reject_effects`` is set.

        """
        self.calls.append((retry, timeout))
        if self.reject_effects and len(self.calls) == 1:
            msg = "effects profile not supported"
            raise InvalidArgument(msg)
        _ = request
        return SimpleNamespace(audio_content=_mp3_bytes())


def test_every_synthesize_call_uses_the_retry_policy() -> None:
    """The wavenet path and both multivoice calls (incl. the plain fallback) pass TTS_RETRY and TTS_TIMEOUT."""
    client = _RecordingClient()
    with patch.object(texttospeech, "TextToSpeechClient", return_value=client):
        _ = tts.synthesize_wavenet("A short sentence to synthesize.")
    fallback_client = _RecordingClient(reject_effects=True)
    _ = multivoice._synth(fallback_client, "Hello there.", "en-US-Studio-O")  # noqa: SLF001  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    calls = client.calls + fallback_client.calls
    expected = (multivoice.TTS_RETRY, multivoice.TTS_TIMEOUT)
    if len(calls) != 3 or any(c[0] is not expected[0] or c[1] != expected[1] for c in calls):
        _fail(f"expected TTS_RETRY + TTS_TIMEOUT on all 3 calls, got {calls!r}")


def test_outage_detection_follows_cause_chain() -> None:
    """A RetryError wrapping a 503 is an outage, even wrapped again; a bad-input error is not."""
    unavailable = ServiceUnavailable("503")
    retry_error = RetryError("deadline exceeded", unavailable)
    wrapped = RuntimeError("chunk failed")
    wrapped.__cause__ = retry_error
    for exc in (unavailable, retry_error, wrapped):
        if not multivoice.is_tts_outage(exc):
            _fail(f"{exc!r} should count as an outage")
    gemini = (
        genai_errors.ServerError(503, {"error": {"message": "unavailable"}}),
        genai_errors.ClientError(429, {"error": {"message": "quota"}}),
        httpx.ConnectError("connection refused"),
    )
    for exc in gemini:
        if not multivoice.is_tts_outage(exc):
            _fail(f"Gemini/transport error {exc!r} should count as an outage")
    for exc in (InvalidArgument("sentence too long"), genai_errors.ClientError(400, {"error": {"message": "bad"}})):
        if multivoice.is_tts_outage(exc):
            _fail(f"{exc!r} is a problem with the input, not an outage")


def _run(
    tmp: pathlib.Path,
    fake_tts: Callable[[str | pathlib.Path, list[tts.NarratorRule]], None],
    collect: Callable[[], list[tuple[pathlib.Path, Exception]]] | None = None,
) -> tuple[str, list[str]]:
    """Run process_files once in ``tmp`` with TTS, batch collection and Gotify stubbed.

    Returns:
        (the RuntimeError message, or "" if it didn't raise; the Gotify alert titles).

    """
    alerts: list[str] = []

    def record_alert(title: str, message: str) -> None:
        _ = message
        alerts.append(title)

    def no_batches() -> list[tuple[pathlib.Path, Exception]]:
        return []

    with ExitStack() as stack:
        for attr, sub in (
            ("input_dir", "in"),
            ("final_output_dir", "audio"),
            ("batch_pending_dir", "pending"),
            ("failed_dir", "failed"),
        ):
            (tmp / sub).mkdir(exist_ok=True)
            _ = stack.enter_context(patch.object(tts, attr, str(tmp / sub)))
        _ = stack.enter_context(patch.object(tts, "strikes_file", str(tmp / "strikes.json")))
        _ = stack.enter_context(patch.object(tts, "text_to_speech", fake_tts))
        _ = stack.enter_context(patch.object(tts, "collect_batch_jobs", collect or no_batches))
        _ = stack.enter_context(patch.object(tts, "load_narrator_rules", list))
        _ = stack.enter_context(patch.object(tts, "send_gotify_notification", record_alert))
        try:
            tts.process_files()
        except RuntimeError as exc:
            return str(exc), alerts
    return "", alerts


def _inputs(tmp: pathlib.Path, *names: str) -> None:
    (tmp / "in").mkdir(exist_ok=True)
    for name in names:
        _ = (tmp / "in" / name).write_text("META_TITLE: T\n\nBody.", encoding="utf-8")


def test_one_failing_file_does_not_block_the_rest() -> None:
    """process_files keeps going past a file with a bad-input error, then raises naming it."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _inputs(tmp, "a.txt", "b.txt", "c.txt")
        processed: list[str] = []

        def fake_tts(path: str | pathlib.Path, rules: list[tts.NarratorRule]) -> None:
            _ = rules
            name = pathlib.Path(path).name
            if name == "b.txt":
                msg = "sentence too long"
                raise InvalidArgument(msg)
            processed.append(name)

        error, _alerts = _run(tmp, fake_tts)
        if "b.txt" not in error:
            _fail(f"error should name the failed file: {error!r}")
        if processed != ["a.txt", "c.txt"]:
            _fail(f"files after the failure were not processed: {processed!r}")


def test_outage_leaves_remaining_files_for_next_run() -> None:
    """Once Google TTS is down, the remaining files are not each tried (and timed out) in turn."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _inputs(tmp, "a.txt", "b.txt", "c.txt")
        attempted: list[str] = []

        def fake_tts(path: str | pathlib.Path, rules: list[tts.NarratorRule]) -> None:
            _ = rules
            attempted.append(pathlib.Path(path).name)
            if pathlib.Path(path).name == "b.txt":
                msg = "503 The service is currently unavailable."
                raise ServiceUnavailable(msg)

        error, _alerts = _run(tmp, fake_tts)
        if attempted != ["a.txt", "b.txt"] or "b.txt" not in error:
            _fail(f"expected to stop after the outage: attempted={attempted!r}, error={error!r}")
        if not (tmp / "in" / "c.txt").exists() or (tmp / "strikes.json").exists():
            _fail("an outage must leave files in place and must not count as a strike")


def test_file_moved_aside_after_repeated_failures() -> None:
    """A file failing MAX_STRIKES runs in a row (not an outage) is moved to failed_dir with one alert."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _inputs(tmp, "bad.txt")

        def fake_tts(path: str | pathlib.Path, rules: list[tts.NarratorRule]) -> None:
            _ = (path, rules)
            msg = "sentence too long"
            raise InvalidArgument(msg)

        all_alerts: list[str] = []
        for _run_no in range(tts.MAX_STRIKES):
            _error, alerts = _run(tmp, fake_tts)
            all_alerts += alerts
        if not (tmp / "failed" / "bad.txt").exists() or (tmp / "in" / "bad.txt").exists():
            _fail("file was not moved to the failed dir")
        if all_alerts != ["TTS gave up on a file"]:
            _fail(f"expected one give-up alert, got {all_alerts!r}")
        if (tmp / "strikes.json").exists():
            _fail("strike count should be cleared once the file is moved")


def test_success_clears_strikes() -> None:
    """A file that fails once and then succeeds starts from zero strikes."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _inputs(tmp, "flaky.txt")
        runs = {"n": 0}

        def fake_tts(path: str | pathlib.Path, rules: list[tts.NarratorRule]) -> None:
            _ = rules
            runs["n"] += 1
            if runs["n"] == 1:
                msg = "odd one-off"
                raise InvalidArgument(msg)
            pathlib.Path(path).unlink()

        _ = _run(tmp, fake_tts)
        if not (tmp / "strikes.json").exists():
            _fail("first failure should be recorded")
        error, _alerts = _run(tmp, fake_tts)
        if error or (tmp / "strikes.json").exists():
            _fail(f"success should clear the strike: error={error!r}")


def test_failing_batch_job_does_not_block_files() -> None:
    """collect_batch_jobs isolates each job, and its failures don't stop the text files."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        pending = tmp / "pending"
        pending.mkdir()
        for name in ("broken", "fine"):
            _ = (pending / f"{name}.json").write_text(json.dumps({"txt_file": f"{name}.txt"}), encoding="utf-8")
        finished: list[str] = []

        def fake_collect(client: object, pending_dir: pathlib.Path, state_path: pathlib.Path) -> None:
            _ = (client, pending_dir)
            if state_path.stem == "broken":
                msg = "corrupt batch output"
                raise ValueError(msg)
            finished.append(state_path.stem)

        with (
            patch.object(tts, "batch_pending_dir", str(pending)),
            patch.object(tts, "get_gemini_client", object),
            patch.object(tts, "_collect_batch_job", fake_collect),
        ):
            failed = tts.collect_batch_jobs()
        if finished != ["fine"] or [p.name for p, _exc in failed] != ["broken.json"]:
            _fail(f"batch jobs not isolated: finished={finished!r}, failed={failed!r}")

        _inputs(tmp, "a.txt")
        processed: list[str] = []

        def fake_tts(path: str | pathlib.Path, rules: list[tts.NarratorRule]) -> None:
            _ = rules
            processed.append(pathlib.Path(path).name)

        def collect() -> list[tuple[pathlib.Path, Exception]]:
            return [(pending / "broken.json", ValueError("corrupt batch output"))]

        error, _alerts = _run(tmp, fake_tts, collect)
        if processed != ["a.txt"] or "broken.json" not in error:
            _fail(f"files blocked by a batch failure: processed={processed!r}, error={error!r}")


def test_outage_never_moves_items_aside() -> None:
    """Batch jobs and files failing because of an outage are never struck, however many runs it lasts."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        pending = tmp / "pending"
        pending.mkdir()
        job = pending / "job.json"
        _ = job.write_text(json.dumps({"txt_file": "job.txt"}), encoding="utf-8")
        _inputs(tmp, "a.txt")

        def collect() -> list[tuple[pathlib.Path, Exception]]:
            return [(job, genai_errors.ServerError(503, {"error": {"message": "unavailable"}}))]

        def fake_tts(path: str | pathlib.Path, rules: list[tts.NarratorRule]) -> None:
            _ = (path, rules)
            raise genai_errors.ServerError(503, {"error": {"message": "unavailable"}})

        for _run_no in range(tts.MAX_STRIKES + 1):
            _ = _run(tmp, fake_tts, collect)
        if list((tmp / "failed").iterdir()) or (tmp / "strikes.json").exists():
            _fail("an outage must not strike or move anything aside")


def test_stale_partial_exports_are_removed() -> None:
    """A .partial left by a killed run is deleted at the start of the next one."""
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        (tmp / "audio" / "evergreen").mkdir(parents=True)
        stale = tmp / "audio" / "evergreen" / "x.mp3.partial"
        _ = stale.write_bytes(b"")
        os.utime(stale, (0, 0))
        fresh = tmp / "audio" / "z.mp3.partial"  # e.g. a manual audition.py export in progress
        _ = fresh.write_bytes(b"")
        keep = tmp / "audio" / "y.mp3"
        _ = keep.write_bytes(b"")

        def fake_tts(path: str | pathlib.Path, rules: list[tts.NarratorRule]) -> None:
            _ = (path, rules)

        _ = _run(tmp, fake_tts)
        if stale.exists() or not keep.exists() or not fresh.exists():
            _fail("only an old .partial should be removed")


def test_failed_tagging_leaves_no_episode_behind() -> None:
    """If tagging fails, neither the .mp3 nor the .partial file is left in the feed dir."""
    with tempfile.TemporaryDirectory() as d:
        feed = pathlib.Path(d)

        def feed_dir(*_: object) -> str:
            return str(feed)

        def summary(*_: object) -> str:
            return "Summary."

        def broken_tags(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        segments = [AudioSegment.silent(duration=50)]
        meta = {"from": "Example Letter", "title": "Title"}
        with ExitStack() as stack:
            _ = stack.enter_context(patch.object(tts, "resolve_feed_dir", feed_dir))
            _ = stack.enter_context(patch.object(tts, "generate_summary", summary))
            _ = stack.enter_context(patch.object(tts, "apply_id3_tags", broken_tags))
            try:
                tts.finalize_episode("20260924-120000-Example Letter- Title", meta, "Body.", segments)
            except OSError:
                pass
            else:
                _fail("tagging failure should propagate")
        if list(feed.iterdir()):
            _fail(f"half-built episode left behind: {list(feed.iterdir())}")

        # And a normal run produces exactly one tagged .mp3 with the publication mtime.
        with ExitStack() as stack:
            _ = stack.enter_context(patch.object(tts, "resolve_feed_dir", feed_dir))
            _ = stack.enter_context(patch.object(tts, "generate_summary", summary))
            tts.finalize_episode("20260924-120000-Example Letter- Title", meta, "Body.", segments)
        files = list(feed.iterdir())
        if len(files) != 1 or files[0].suffix != ".mp3":
            _fail(f"expected one .mp3, got {files!r}")
        expected = pub_date_from_filename("20260924-120000-x")
        if expected is None or int(files[0].stat().st_mtime) != int(expected.timestamp()):
            _fail("publication mtime lost across the rename")


if __name__ == "__main__":
    test_every_synthesize_call_uses_the_retry_policy()
    test_outage_detection_follows_cause_chain()
    test_one_failing_file_does_not_block_the_rest()
    test_outage_leaves_remaining_files_for_next_run()
    test_file_moved_aside_after_repeated_failures()
    test_success_clears_strikes()
    test_failing_batch_job_does_not_block_files()
    test_outage_never_moves_items_aside()
    test_stale_partial_exports_are_removed()
    test_failed_tagging_leaves_no_episode_behind()
    logging.info("TTS failure-handling tests passed.")
