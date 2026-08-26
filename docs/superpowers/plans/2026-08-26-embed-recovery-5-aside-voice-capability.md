# Aside-Voice Render Capability — Implementation Plan (Plan 5 of the embed-recovery series)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the pipeline the *capability* to voice embeds as asides — a shared `ASIDE_MARKER`, a `serialize_flat` that turns the block tree into flat marker body text (quotes → `BLOCKQUOTE_MARKER`, other embeds → `ASIDE_MARKER` + pre-rendered aside), and `multivoice` support for an aside voice — without changing any live behaviour. Going live (intake switch, prepare-text passthrough, TTS config) is Plan 6.

**Architecture (flat-marker):** Reuse the existing article multi-voice machinery. `serialize_flat(blocks)` emits body text; `plan_article_utterances` gains an `ASIDE_MARKER` branch producing `("<aside text>", "ASIDE")` utterances; `assign_voice`/`render_utterances` gain an optional `aside_voice`; `strip_markers` also removes `ASIDE_MARKER`. All additions are backward-compatible defaults, so with no `ASIDE_MARKER` in today's content, every current episode renders identically.

**Tech Stack:** Python 3.12, `pydub`, Google Cloud TTS, ruff, basedpyright. Script-style tests via `uv run python3`.

**Spec:** `docs/superpowers/specs/2026-08-26-embed-recovery-design.md` (§9 rendering). Deviation noted there-and-here: flat marker (extraction-time aside rendering) instead of render-time sidecar, for pipeline simplicity and reuse.

## Global Constraints

- Line length 120; ruff `ALL`; basedpyright `typeCheckingMode = "all"`, **zero errors** (targeted `# pyright: ignore` only at genuine boundaries). No `cast()`.
- Script-style tests (typed, `raise AssertionError`, `run_tests()` + `__main__`), matching `text-to-speech/test_multivoice.py`.
- `ASIDE_MARKER` must, like `BLOCKQUOTE_MARKER`, survive prepare-text cleaning: a single non-ASCII ornament char + space, no brackets/underscores/collapsible whitespace, never in prose. Use `chr(0x2756) + " "` (❖).
- Backward compatibility is mandatory: existing `multivoice` callers (`text_to_speech.py`, comment path) must keep working unchanged. New params are optional with defaults.

---

### Task 1: `ASIDE_MARKER` + `serialize_flat`

**Files:**
- Modify: `shared/podcast_shared/structural_extract.py` (define `ASIDE_MARKER`)
- Modify: `shared/podcast_shared/aside_render.py` (add `serialize_flat`)
- Modify: `shared/podcast_shared/__init__.py` (export both)
- Test: `shared/test_aside_render.py` (append)

**Interfaces:**
- Produces:
  - `ASIDE_MARKER = chr(0x2756) + " "` (in `structural_extract.py`, beside the embed-marker constants).
  - `serialize_flat(blocks: list[Block]) -> str` (in `aside_render.py`) — joins blocks with blank lines: `text` → its text; `quote` → `BLOCKQUOTE_MARKER + text`; any other type → `ASIDE_MARKER + render_block_aside(block)` (skipped when the aside renders empty).

- [ ] **Step 1: Write the failing test (append to `test_aside_render.py` + run_tests)**

```python
from podcast_shared.aside_render import serialize_flat  # noqa: E402
from podcast_shared.structural_extract import ASIDE_MARKER, BLOCKQUOTE_MARKER  # noqa: E402


def test_serialize_flat_marks_quotes_and_asides() -> None:
    """text stays plain; quotes get BLOCKQUOTE_MARKER; embeds get ASIDE_MARKER."""
    body = serialize_flat(
        [
            Block(type="text", payload={"text": "Intro."}),
            Block(type="quote", payload={"text": "A quoted line."}),
            Block(type="tweet", payload={"handle": "@x", "text": "hello"}),
        ]
    )
    lines = [ln for ln in body.split("\n\n") if ln]
    if lines[0] != "Intro.":
        _fail(f"text line wrong: {lines!r}")
    if lines[1] != f"{BLOCKQUOTE_MARKER}A quoted line.":
        _fail(f"quote line wrong: {lines!r}")
    if lines[2] != f"{ASIDE_MARKER}The author shares a tweet from @x: hello.":
        _fail(f"aside line wrong: {lines!r}")
```

Note: `BLOCKQUOTE_MARKER` is re-exported from `structural_extract` for the test's import convenience — see Step 3.

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run python3 test_aside_render.py`
Expected: FAIL — `ImportError` for `serialize_flat`/`ASIDE_MARKER`.

- [ ] **Step 3: Implement**

In `structural_extract.py`, below `EMBED_MARKER_SUFFIX`, add (and re-export `BLOCKQUOTE_MARKER` for one-stop importing):

```python
from podcast_shared import BLOCKQUOTE_MARKER as BLOCKQUOTE_MARKER  # noqa: E402  (re-export)

# Line prefix intake writes ahead of each rendered embed "aside" (tweet/image/...),
# so text-to-speech can voice it in the meta-narrator aside voice. Chosen like
# BLOCKQUOTE_MARKER to survive prepare-text cleaning and never occur in prose.
ASIDE_MARKER = chr(0x2756) + " "  # U+2756 BLACK DIAMOND MINUS WHITE X, then a space
```

Wait — `structural_extract` is imported by `podcast_shared/__init__`, so it must not import from `podcast_shared` (circular). Instead define `BLOCKQUOTE_MARKER`'s value is already in `__init__`; do NOT re-import it here. Put `ASIDE_MARKER` in `structural_extract.py` with no cross-import:

```python
ASIDE_MARKER = chr(0x2756) + " "  # U+2756 BLACK DIAMOND MINUS WHITE X, then a space
```

In `aside_render.py`, import the markers from `podcast_shared` is likewise circular; import `BLOCKQUOTE_MARKER` from `podcast_shared.__init__`? No — `aside_render` is also imported by `__init__`. Import `ASIDE_MARKER` from `structural_extract` (safe, already a dependency) and `BLOCKQUOTE_MARKER` by defining it where both can see it. Since `BLOCKQUOTE_MARKER` currently lives in `__init__`, move its definition to `structural_extract.py` (a leaf module) and re-export from `__init__` (keeping the public name stable). Then `aside_render` imports both from `structural_extract`.

Concretely:
1. In `structural_extract.py` add:
   ```python
   BLOCKQUOTE_MARKER = chr(0x276F) + " "  # U+276F ... (moved here from __init__ so leaf modules can import it)
   ASIDE_MARKER = chr(0x2756) + " "  # U+2756 BLACK DIAMOND MINUS WHITE X, then a space
   ```
2. In `podcast_shared/__init__.py`, replace the inline `BLOCKQUOTE_MARKER = ...` definition with a re-export:
   ```python
   from podcast_shared.structural_extract import ASIDE_MARKER as ASIDE_MARKER
   from podcast_shared.structural_extract import BLOCKQUOTE_MARKER as BLOCKQUOTE_MARKER
   ```
   (Keep the explanatory comment above the re-export.)
3. In `aside_render.py`, add to its existing `from podcast_shared.structural_extract import (...)`: `ASIDE_MARKER`, `BLOCKQUOTE_MARKER`, and append:

```python
def serialize_flat(blocks: list[Block]) -> str:
    """Serialise blocks to flat marker body text for the TTS pipeline.

    ``text`` stays plain; ``quote`` gets ``BLOCKQUOTE_MARKER`` (reusing the existing
    quote-voice machinery); every other embed becomes ``ASIDE_MARKER`` + its rendered
    aside (skipped when the aside is empty).

    Returns:
        The flat marker body, blocks joined by blank lines.

    """
    lines: list[str] = []
    for block in blocks:
        if block.type == "text":
            lines.append(block.payload.get("text", ""))
        elif block.type == "quote":
            lines.append(f"{BLOCKQUOTE_MARKER}{block.payload.get('text', '')}")
        else:
            aside = render_block_aside(block)
            if aside:
                lines.append(f"{ASIDE_MARKER}{aside}")
    return "\n\n".join(lines)
```

Verify `podcast_shared/multivoice`-side and every existing `from podcast_shared import BLOCKQUOTE_MARKER` still resolves (it now re-exports from `structural_extract`).

- [ ] **Step 4: Run ALL shared tests + lint + types**

Run: `cd shared && uv run python3 test_aside_render.py && uv run python3 test_structural_extract.py && uv run python3 test_intake_store.py && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors. (The `BLOCKQUOTE_MARKER` move must not break its import elsewhere.)

- [ ] **Step 5: Confirm dependents still import the marker**

Run: `cd ../text-to-speech && uv sync && uv run python3 -c "from multivoice import BLOCKQUOTE_MARKER; print('ok', repr(BLOCKQUOTE_MARKER))"`
Expected: `ok '❯ '` (unchanged value).

- [ ] **Step 6: Commit**

```bash
git add shared/podcast_shared/structural_extract.py shared/podcast_shared/aside_render.py shared/podcast_shared/__init__.py shared/test_aside_render.py
git commit -m "feat(shared): ASIDE_MARKER + serialize_flat (flat marker body for TTS)"
```

---

### Task 2: `multivoice` aside-voice support (backward-compatible)

**Files:**
- Modify: `text-to-speech/multivoice.py`
- Test: `text-to-speech/test_multivoice.py` (append; match its existing style)

**Interfaces:**
- Changed (all backward-compatible):
  - `plan_article_utterances(body, marker=BLOCKQUOTE_MARKER, aside_marker=ASIDE_MARKER)` — lines starting with `aside_marker` emit `(aside_text, "ASIDE")`; quote/narrator behaviour unchanged.
  - `assign_voice(speaker, narrator_voice, quote_pool, aside_voice="")` — `"ASIDE"` → `aside_voice or narrator_voice`.
  - `render_utterances(utterances, narrator_voice, quote_pool, aside_voice="")` — passes `aside_voice` through.
  - `strip_markers(text, marker=BLOCKQUOTE_MARKER)` — also removes `ASIDE_MARKER`.
  - Re-export `ASIDE_MARKER` in `multivoice.__all__`.

- [ ] **Step 1: Read the existing test style**

Run: `sed -n '1,40p' text-to-speech/test_multivoice.py`
Match its import/assertion idiom in the new tests.

- [ ] **Step 2: Write the failing tests (append + wire into its runner)**

```python
from multivoice import ASIDE_MARKER  # add to existing imports


def test_aside_marker_becomes_aside_speaker() -> None:
    body = f"Narration line.\n\n{ASIDE_MARKER}The author shares a tweet from @x: hi."
    plan = plan_article_utterances(body)
    speakers = [sp for _, sp in plan]
    if "ASIDE" not in speakers:
        raise AssertionError(f"no ASIDE speaker: {plan!r}")
    aside_text = next(t for t, sp in plan if sp == "ASIDE")
    if aside_text != "The author shares a tweet from @x: hi.":
        raise AssertionError(f"aside text wrong: {aside_text!r}")


def test_assign_voice_routes_aside() -> None:
    if assign_voice("ASIDE", "NARV", ["Q1"], "ASIDEV") != "ASIDEV":
        raise AssertionError("aside voice not used")
    if assign_voice("ASIDE", "NARV", ["Q1"]) != "NARV":
        raise AssertionError("aside should fall back to narrator when unset")


def test_strip_markers_removes_aside_marker() -> None:
    if ASIDE_MARKER in strip_markers(f"{ASIDE_MARKER}spoken aside"):
        raise AssertionError("aside marker not stripped")
```

(Add these to the module's `run_tests`/`__main__` runner, mirroring the existing file.)

- [ ] **Step 3: Run to verify they fail**

Run: `cd text-to-speech && uv run python3 test_multivoice.py`
Expected: FAIL — `ImportError` for `ASIDE_MARKER` / assertions.

- [ ] **Step 4: Implement**

In `multivoice.py`:
- Import: `from podcast_shared import ASIDE_MARKER, BLOCKQUOTE_MARKER`.
- Add `"ASIDE_MARKER"` to `__all__`.
- `plan_article_utterances` — after the quote-marker branch, before the narrator `else`, insert:

```python
        elif line.startswith(aside_key):
            flush_quote()
            out.append((line[len(aside_key) :].strip(), "ASIDE"))
```

with the signature/locals updated:

```python
def plan_article_utterances(
    body: str, marker: str = BLOCKQUOTE_MARKER, aside_marker: str = ASIDE_MARKER
) -> list[tuple[str, str]]:
    ...
    marker_key = marker.strip()
    aside_key = aside_marker.strip()
    ...
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(marker_key):
            quote_run.append(line[len(marker_key) :].strip())
        elif line.startswith(aside_key):
            flush_quote()
            out.append((line[len(aside_key) :].strip(), "ASIDE"))
        else:
            flush_quote()
            out.append((line, "NARRATOR"))
    flush_quote()
    return out
```

Note ordering: quote first, then aside, then narrator. `flush_quote()` must run before appending the aside (as shown).

- `assign_voice`:

```python
def assign_voice(speaker: str, narrator_voice: str, quote_pool: list[str], aside_voice: str = "") -> str:
    if speaker == "NARRATOR":
        return narrator_voice
    if speaker == "ASIDE":
        return aside_voice or narrator_voice
    digest = hashlib.sha256(speaker.encode("utf-8")).digest()
    return quote_pool[digest[0] % len(quote_pool)]
```

- `render_utterances`: add `aside_voice: str = ""` param and pass it: `assign_voice(speaker, narrator_voice, quote_pool, aside_voice)`.

- `strip_markers`: also remove `ASIDE_MARKER`:

```python
def strip_markers(text: str, marker: str = BLOCKQUOTE_MARKER) -> str:
    """..."""
    return text.replace(marker, "").replace(ASIDE_MARKER, "")
```

- [ ] **Step 5: Run tests + lint + types (whole subproject)**

Run: `cd text-to-speech && uv run python3 test_multivoice.py && uv run python3 test_comment_routing.py && uv run ruff check . && uv run basedpyright`
Expected: PASS (incl. existing multivoice + comment-routing tests — proving backward compatibility); ruff clean; basedpyright zero errors.

- [ ] **Step 6: Commit**

```bash
git add text-to-speech/multivoice.py text-to-speech/test_multivoice.py
git commit -m "feat(tts): multivoice aside-voice support (ASIDE_MARKER, backward-compatible)"
```

---

## Self-Review

- **Spec coverage:** §9 rendering — the aside voice and marker-driven aside utterances (flat-marker variant; documented deviation). Existing quote/comment behaviour preserved via optional params + inert new marker.
- **No live behaviour change:** no current content contains `ASIDE_MARKER`; all new params default to prior behaviour; `strip_markers` removing an absent marker is a no-op. Existing `multivoice`/comment tests must still pass (Task 2 Step 5).
- **Circular-import safety:** `BLOCKQUOTE_MARKER` moves to leaf module `structural_extract.py`; `__init__` re-exports it (public name unchanged); `aside_render` imports markers from `structural_extract`.
- **Types/placeholders:** new params typed; `serialize_flat` return `str`; no placeholders.
- **Deferred to Plan 6 (go-live):** switch imap Substack intake to `serialize_flat`; `prepare-text` `ASIDE_MARKER` passthrough + survival test; `text_to_speech` load `aside_voice` from `narrators.yaml` and trigger multi-voice on asides; `narrators.example.yaml` doc.
