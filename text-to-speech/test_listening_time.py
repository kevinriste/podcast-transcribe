"""The listening time is spliced right after the intro and quotes the whole episode's length."""

import logging
import os
import pathlib
import tempfile

from podcast_shared import LISTENING_TIME_MARKER
from pydub import AudioSegment

import text_to_speech as tts
from multivoice import PAUSE_MS, render_utterances

# Fake synthesis: 1 second of silence per 10 characters, tagged by length so tests can
# tell intro, phrase and body apart.
MS_PER_CHAR = 100
COMMENT_FILE = """META_FROM: Blog
META_TITLE: Comments: Title
META_INTAKE_TYPE: archive-comments

NARRATOR: The commenters argued.

QUOTE Ann: I disagree."""


def _fake_audio(text: str) -> AudioSegment:
    return AudioSegment.silent(duration=len(text) * MS_PER_CHAR)


def _require(cond: bool, msg: str) -> None:  # noqa: FBT001
    if not cond:
        raise AssertionError(msg)


def check_resynthesizes_when_own_length_changes_time() -> None:
    """Check the sentence is redone once when its own length changes the spoken time."""
    os.environ["LISTENING_SPEED"] = "1.3"
    phrases: list[str] = []

    def synth(phrase: str) -> AudioSegment:
        phrases.append(phrase)
        return AudioSegment.silent(duration=3000)

    intro, body = AudioSegment.silent(duration=2000), AudioSegment.silent(duration=193_000)
    segments = tts.with_listening_time([intro], [body], synth)
    # 195 s / 1.3 = 150 s first; with the 3 s sentence, 198 s / 1.3 = 152 s.
    _require(
        phrases == ["Listening time: 2 minutes, 30 seconds.", "Listening time: 2 minutes, 32 seconds."],
        f"unexpected phrases {phrases!r}",
    )
    _require([len(s) for s in segments] == [2000, 3000, 193_000], f"wrong order: {[len(s) for s in segments]}")


def check_missing_clip_publishes_without_it() -> None:
    """Check a failed sentence leaves the intro and body in order, with no gap left behind."""
    segments = tts.with_listening_time(
        [AudioSegment.silent(duration=1000)], [AudioSegment.silent(duration=5000)], lambda _: None, 400
    )
    _require([len(s) for s in segments] == [1000, 400, 5000], f"unexpected segments {[len(s) for s in segments]}")


def _run_article(content: str) -> tuple[list[str], list[int]]:
    """Run text_to_speech() on a WaveNet article with synthesis and publishing faked.

    Returns:
        The texts sent to synthesis, and the published segment lengths.

    """
    synthesized: list[str] = []
    published: list[int] = []

    def fake_wavenet(text: str) -> list[AudioSegment]:
        if not text:
            return []
        synthesized.append(text)
        return [_fake_audio(text)]

    def fake_finalize(name: str, metadata: dict[str, str], content: str, segments: list[AudioSegment]) -> None:
        del name, metadata, content
        published.extend(len(s) for s in segments)

    real_wavenet, real_finalize = tts.synthesize_wavenet, tts.finalize_episode
    tts.synthesize_wavenet, tts.finalize_episode = fake_wavenet, fake_finalize
    try:
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "20260101-000000-Author- Title.txt"
            _ = path.write_text(f"META_FROM: Author\nMETA_TITLE: Title\n\n{content}", encoding="utf-8")
            tts.text_to_speech(path, [])
    finally:
        tts.synthesize_wavenet, tts.finalize_episode = real_wavenet, real_finalize
    return synthesized, published


def check_wavenet_article_splices_after_intro() -> None:
    """Check a WaveNet article synthesizes intro and body apart and announces between them."""
    os.environ["LISTENING_SPEED"] = "1"
    intro, body = "Author.\nTitle.", "Body text " * 50
    synthesized, published = _run_article(f"{intro}\n{LISTENING_TIME_MARKER}\n\n{body}")
    _require(synthesized[:2] == [intro, body.strip()], f"intro/body not split: {synthesized!r}")
    _require(synthesized[-1].startswith("Listening time:"), f"no listening time synthesized: {synthesized!r}")
    _require(LISTENING_TIME_MARKER not in "".join(synthesized), "marker must never be spoken")
    phrase_ms = len(synthesized[-1]) * MS_PER_CHAR
    expected = [len(intro) * MS_PER_CHAR, phrase_ms, len(body.strip()) * MS_PER_CHAR]
    _require(published == expected, f"published {published}, expected {expected}")


def check_unmarked_article_announces_first() -> None:
    """Check a file without the marker (e.g. queued before this change) announces at the start."""
    os.environ["LISTENING_SPEED"] = "1"
    synthesized, published = _run_article("Just a body.")
    _require(synthesized[0] == "Just a body.", f"unexpected synthesis {synthesized!r}")
    _require(published[0] == len(synthesized[-1]) * MS_PER_CHAR, f"listening time should come first: {published}")


def check_comment_episode_splices_after_header() -> None:
    """Check a comment episode announces right after its narrator header utterance."""
    os.environ["LISTENING_SPEED"] = "1"
    rendered: list[list[tuple[str, str]]] = []
    published: list[int] = []

    def fake_render(utts: list[tuple[str, str]], *_: object) -> list[AudioSegment]:
        rendered.append(utts)
        return [_fake_audio(text) for text, _ in utts]

    def fake_finalize(
        name: str, metadata: dict[str, str], content: str, segments: list[AudioSegment], summary_override: str = ""
    ) -> None:
        del name, metadata, content, summary_override
        published.extend(len(s) for s in segments)

    real_finalize = tts.finalize_episode
    setattr(tts, "render_utterances", fake_render)  # noqa: B010
    tts.finalize_episode = fake_finalize
    try:
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "20260101-000000-ARCHIVE-COMMENTS-Title.txt"
            _ = path.write_text(COMMENT_FILE, encoding="utf-8")
            tts.text_to_speech(path, [])
    finally:
        setattr(tts, "render_utterances", render_utterances)  # noqa: B010
        tts.finalize_episode = real_finalize
    header = rendered[1]
    _require(header == [("Blog. Comments: Title.", "NARRATOR")], f"header rendered wrongly: {rendered!r}")
    phrase = rendered[2][0][0]
    _require(phrase.startswith("Listening time:"), f"no listening time rendered: {rendered!r}")
    header_ms, phrase_ms = len(header[0][0]) * MS_PER_CHAR, len(phrase) * MS_PER_CHAR
    _require(published[:4] == [header_ms, PAUSE_MS, phrase_ms, PAUSE_MS], f"bad splice: {published[:4]}")


if __name__ == "__main__":
    check_resynthesizes_when_own_length_changes_time()
    check_missing_clip_publishes_without_it()
    check_wavenet_article_splices_after_intro()
    check_unmarked_article_announces_first()
    check_comment_episode_splices_after_header()
    logging.info("listening time tests passed.")
