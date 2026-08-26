# Go-Live Wiring — Implementation Plan (Plan 6 of the embed-recovery series)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make embed recovery audible in real episodes: switch imap HTML-body intake to the structural extractor (`serialize_flat`), confirm `ASIDE_MARKER` survives prepare-text cleaning, and load/thread an `aside_voice` in text-to-speech.

**Architecture:** imap's `extract_body_from_html` produces flat marker body via `serialize_flat(extract_blocks(find_content_region(html)))` — quotes as `BLOCKQUOTE_MARKER` (parity), embeds as `ASIDE_MARKER` + aside. prepare-text already passes such markers through (guard-tested). text-to-speech: `article_multivoice_plan` already triggers on any non-narrator utterance (asides included); we load `aside_voice` from `narrators.yaml` and pass it to `render_utterances`. Gemini-routed sources stay single-voice with markers stripped (aside text still read).

**Tech Stack:** Python 3.12, ruff, basedpyright. Script-style tests.

**Spec:** `docs/superpowers/specs/2026-08-26-embed-recovery-design.md` (§5, §9). This is the live wiring of the prior plans.

## Global Constraints

- Line length 120; ruff `ALL`; basedpyright zero errors; no `cast()`; no pyright-ignore except at existing boundaries.
- **Backward compatibility:** non-HTML intake paths (link/youtube/rss/archive) unchanged. Existing quote behaviour preserved (serialize_flat emits the same `BLOCKQUOTE_MARKER` for quotes). `load_comment_voices`'s existing callers must be updated for any signature change.
- Verify each subproject after its change: `uv run ruff check . && uv run basedpyright`, plus that subproject's script tests.

---

### Task 1: imap — HTML-body intake via the structural extractor

**Files:**
- Modify: `imap/parse_email.py` (`extract_body_from_html` ~239-246; import block ~22)

**Interfaces:**
- Changed: `extract_body_from_html(msg)` returns `serialize_flat(extract_blocks(find_content_region(msg.html)))` (or `None` when there is no HTML/text). `extract_body_text` remains for now (still referenced by its own unit tests); it is simply no longer the HTML-body path.

- [ ] **Step 1: Check imap tests referencing the extractor**

Run: `cd imap && ls test_*.py 2>/dev/null && grep -ln "extract_body_text\|extract_body_from_html" test_*.py 2>/dev/null || echo "(no imap tests referencing it)"`
Note any test that pins `extract_body_text`; leave that function intact.

- [ ] **Step 2: Import the structural helpers**

Extend the `from podcast_shared import ( ... )` block in `imap/parse_email.py` to also import `extract_blocks`, `find_content_region`, `serialize_flat`.

- [ ] **Step 3: Route HTML-body extraction through the structural extractor**

Replace the body of `extract_body_from_html`:

```python
def extract_body_from_html(msg: MailMessage) -> str | None:
    """Extract flat marker body text (narration + quotes + embed asides) from the email HTML.

    Returns:
        The serialized body, or None when there is no HTML or no extractable content.

    """
    if not msg.html:
        return None
    body = serialize_flat(extract_blocks(find_content_region(msg.html)))
    return body or None
```

- [ ] **Step 4: Manual end-to-end check on a stored corpus email**

Run:

```bash
cd imap && uv run python3 -c "
from pathlib import Path
from podcast_shared import find_content_region, extract_blocks, serialize_flat
html = Path('../email-corpus/astralcodexten-substack-com/10200.html').read_text(errors='replace')
body = serialize_flat(extract_blocks(find_content_region(html)))
import podcast_shared as ps
i = body.find('spooked')
print(body[i:i+180])
print('has ASIDE marker:', ps.ASIDE_MARKER in body, '| has BLOCKQUOTE marker:', ps.BLOCKQUOTE_MARKER in body)
"
```
Expected: the Roon tweet appears as an `ASIDE_MARKER` line right after "spooked"; both markers present.

- [ ] **Step 5: Lint + types**

Run: `cd imap && uv run ruff check . && uv run basedpyright`
Expected: clean; zero errors.

- [ ] **Step 6: Commit**

```bash
git add imap/parse_email.py
git commit -m "feat(imap): HTML-body intake via structural extractor (recovers embeds as asides)"
```

---

### Task 2: prepare-text — guard ASIDE_MARKER survival

**Files:**
- Modify: `prepare-text/test_marker_survival.py`
- (Modify `prepare-text/prepare_text.py` ONLY if the new test fails.)

**Interfaces:**
- No production change expected: `ASIDE_MARKER` (U+2756 + space) has the same cleaning-safe shape as `BLOCKQUOTE_MARKER`, so it should pass through untouched. The new test guards that.

- [ ] **Step 1: Add an ASIDE_MARKER survival check**

In `prepare-text/test_marker_survival.py`, import `ASIDE_MARKER` and add a check that mirrors the block-quote one:

```python
from podcast_shared import ASIDE_MARKER, BLOCKQUOTE_MARKER, split_metadata  # extend existing import

ASIDE_LINE = f"{ASIDE_MARKER}The author shares a tweet from @x: hello."


def check_aside_marker_survives() -> None:
    """The rendered-embed aside marker survives cleaning like the block-quote marker."""
    body_text = (
        "Author writes this.\n\n"
        f"{ASIDE_LINE}\n\n"
        "Author continues."
    )
    raw = "META_FROM: Some Author\nMETA_SOURCE_KIND: substack\nMETA_INTAKE_TYPE: email\n\n" + body_text
    with tempfile.TemporaryDirectory() as d:
        body = _run_process(raw, pathlib.Path(d))
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    _require(ASIDE_LINE in lines, f"aside line not preserved: {body!r}")
    _require(body.count(ASIDE_MARKER) == 1, f"aside marker count changed: {body!r}")
```

Add `check_aside_marker_survives()` to the `__main__` block.

- [ ] **Step 2: Run the guard test**

Run: `cd prepare-text && uv run python3 test_marker_survival.py`
Expected: PASS (marker survives with no production change). **If it fails**, find the cleaning step that strips U+2756 in `prepare_text.py` and exempt the marker (mirror any existing `BLOCKQUOTE_MARKER` handling); then re-run.

- [ ] **Step 3: Run the other prepare-text tests + lint + types**

Run: `cd prepare-text && for t in test_marker_survival test_substack_boilerplate test_unwrap test_roman test_archive_comments_passthrough; do uv run python3 $t.py; done && uv run ruff check . && uv run basedpyright`
Expected: all PASS; clean; zero errors.

- [ ] **Step 4: Commit**

```bash
git add prepare-text/test_marker_survival.py
# include prepare_text.py only if Step 2 required a change
git commit -m "test(prepare-text): guard ASIDE_MARKER survival through cleaning"
```

---

### Task 3: text-to-speech — load + thread the aside voice

**Files:**
- Modify: `text-to-speech/text_to_speech.py` (`CommentVoicesConfig` ~118-121; `load_comment_voices` ~580-595; both call sites ~706 and ~731)
- Modify: `text-to-speech/narrators.example.yaml` (`comment_voices:` section)

**Interfaces:**
- Changed: `load_comment_voices() -> tuple[str, list[str], str]` — now also returns `aside_voice` (from `comment_voices.aside_voice`, default `"en-US-Wavenet-B"`). Both call sites updated; the article path passes `aside_voice` to `render_utterances`.

- [ ] **Step 1: Add `aside_voice` to the config type**

In `CommentVoicesConfig` (TypedDict), add `aside_voice: str`.

- [ ] **Step 2: Return it from `load_comment_voices`**

Update `load_comment_voices` to return a 3-tuple; add a module default beside the others:

```python
DEFAULT_ASIDE_VOICE = "en-US-Wavenet-B"
```

```python
def load_comment_voices() -> tuple[str, list[str], str]:
    """Load the comment/article narrator, quote pool, and aside voice from narrators.yaml.

    Returns:
        (narrator_voice, quote_pool, aside_voice); WaveNet defaults when absent.

    """
    config_path = pathlib.Path(narrator_config_file)
    if not config_path.exists():
        return DEFAULT_NARRATOR_VOICE, DEFAULT_QUOTE_POOL, DEFAULT_ASIDE_VOICE
    config: NarratorConfig = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    cv = config.get("comment_voices", {})
    narrator = cv.get("narrator") or DEFAULT_NARRATOR_VOICE
    quote_pool = cv.get("quote_pool") or DEFAULT_QUOTE_POOL
    aside_voice = cv.get("aside_voice") or DEFAULT_ASIDE_VOICE
    return narrator, quote_pool, aside_voice
```

- [ ] **Step 3: Update both call sites**

Comment path (~706):

```python
        narrator_voice, quote_pool, _aside_voice = load_comment_voices()
```

(the comment path has no asides; the third value is unused there — name it `_aside_voice`).

Article path (~731):

```python
        narrator_voice, quote_pool, aside_voice = load_comment_voices()
        segments = render_utterances(plan, narrator_voice, quote_pool, aside_voice)
```

- [ ] **Step 4: Document `aside_voice` in the example config**

In `narrators.example.yaml`, under `comment_voices:`, add after `narrator:`:

```yaml
  # Voice for embedded-content "asides" (tweets, images, videos, ...) — the meta-
  # narrator that announces content outside the article's own prose. Defaults to
  # en-US-Wavenet-B. Use any Google Cloud TTS voice.
  aside_voice: en-US-Wavenet-B
```

- [ ] **Step 5: Tests + lint + types**

Run: `cd text-to-speech && for t in test_multivoice test_comment_routing; do uv run python3 $t.py; done && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors. (Any other `test_*.py` present should also pass.)

- [ ] **Step 6: Full end-to-end plan check (no audio)**

Run:

```bash
cd text-to-speech && uv run python3 -c "
from pathlib import Path
from podcast_shared import find_content_region, extract_blocks, serialize_flat
from multivoice import plan_article_utterances
html = Path('../email-corpus/astralcodexten-substack-com/10200.html').read_text(errors='replace')
body = serialize_flat(extract_blocks(find_content_region(html)))
plan = plan_article_utterances(body)
from collections import Counter
print(Counter(sp if sp in ('NARRATOR','ASIDE') else 'QUOTE' for _, sp in plan))
print('first aside:', next(t for t,sp in plan if sp=='ASIDE')[:80])
"
```
Expected: a Counter with NARRATOR + ASIDE (and QUOTE if any); first aside is the Roon tweet announcement.

- [ ] **Step 7: Commit**

```bash
git add text-to-speech/text_to_speech.py text-to-speech/narrators.example.yaml
git commit -m "feat(tts): load and thread aside_voice for embedded-content asides"
```

---

## Self-Review

- **Spec coverage:** §5/§9 go-live — imap emits flat marker body (T1); ASIDE_MARKER survives cleaning (T2); TTS voices asides in a distinct aside voice on the WaveNet multivoice path, single-voice-with-text on the Gemini path (T3). Image *vision* enrichment, remaining handlers, non-Substack region detection, toggle matrix, backfill, and the NUL fix remain later plans.
- **Backward compatibility:** only the HTML-body intake path changes shape (quotes still `BLOCKQUOTE_MARKER`); link/youtube/rss/archive untouched; `load_comment_voices`'s comment caller updated; `article_multivoice_plan` already triggered on non-narrator utterances, so asides route correctly with no gate change.
- **Risk & rollback:** all changes are on `feat/embed-recovery`; the running server is on `main`. If an issue appears after merge, reverting these three commits restores prior behaviour. First real episodes should be spot-checked before wide rollout.
- **Placeholders:** none — runnable code or exact commands throughout.
