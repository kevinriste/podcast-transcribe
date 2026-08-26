# Aside Rendering Layer — Implementation Plan (Plan 3 of the embed-recovery series)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a serialized block tree (marker text + sidecar) into speech-ready text — resolving each `⟦EMBED:id⟧` marker into a spoken "aside" using per-type templates, recursively for nested embeds — as a pure, unit-tested `shared` function. No pipeline wiring, no audio, no LLM.

**Architecture:** `aside_render.py` reconstructs a `Block` from a sidecar entry (`block_from_dict`, inverse of Plan 2's `_block_to_dict`), renders one block to an aside string via type templates (`render_block_aside`, recursing into children), and rewrites marker text (`resolve_markers`). Decoupled from voice/audio (Plan 4 chooses how the aside is voiced) and from intake (Plan 4 wires it after the extractor reaches blockquote/image parity, so the live pipeline never regresses).

**Tech Stack:** Python 3.12, ruff, basedpyright. Script-style tests via `uv run python3`.

**Spec:** `docs/superpowers/specs/2026-08-26-embed-recovery-design.md` (§6 aside phrasings, §9 rendering — the text half; voice/audio is Plan 4).

## Global Constraints

- Line length 120; ruff `ALL`; basedpyright `typeCheckingMode = "all"`, **zero errors**. No `cast()`; narrow with `isinstance`/`str()`. No pyright-ignore comments.
- `@dataclass(slots=True)`. Tests script-style (typed, `raise AssertionError`, `run_tests()` + `__main__`), run with `uv run python3`.
- Reuse Plan 2's `Block`, `EMBED_MARKER_PREFIX`, `EMBED_MARKER_SUFFIX` from `podcast_shared.structural_extract`.

---

### Task 1: `block_from_dict` — reconstruct a Block from a sidecar entry

**Files:**
- Modify: `shared/podcast_shared/structural_extract.py` (add `block_from_dict`, the inverse of `_block_to_dict`)
- Test: `shared/test_structural_extract.py` (append)

**Interfaces:**
- Produces: `block_from_dict(data: dict[str, object]) -> Block` — rebuilds a `Block` (type/payload/children) from the sidecar dict, narrowing untyped values with `isinstance`/`str()`. Non-dict/str fields degrade gracefully (empty payload/children).

- [ ] **Step 1: Write the failing test (append + add to run_tests)**

```python
from podcast_shared.structural_extract import block_from_dict, serialize_blocks  # noqa: E402


def test_block_from_dict_roundtrips() -> None:
    """serialize -> block_from_dict rebuilds the tree (incl. nested children)."""
    original = [
        Block(
            type="tweet",
            payload={"handle": "@x", "text": "hi"},
            children=[Block(type="image", payload={"alt": "a chart"})],
        )
    ]
    _, sidecar = serialize_blocks(original)
    rebuilt = block_from_dict(sidecar["0000"])
    if rebuilt.type != "tweet" or rebuilt.payload["handle"] != "@x":
        _fail(f"top block wrong: {rebuilt!r}")
    if len(rebuilt.children) != 1 or rebuilt.children[0].type != "image":
        _fail(f"children wrong: {rebuilt.children!r}")
    if rebuilt.children[0].payload.get("alt") != "a chart":
        _fail(f"child payload wrong: {rebuilt.children[0].payload!r}")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run python3 test_structural_extract.py`
Expected: FAIL — `ImportError` for `block_from_dict`.

- [ ] **Step 3: Implement**

Add to `structural_extract.py`:

```python
def block_from_dict(data: dict[str, object]) -> Block:
    """Rebuild a Block from a sidecar entry (inverse of ``_block_to_dict``).

    Returns:
        The reconstructed Block; malformed fields degrade to empty payload/children.

    """
    block_type = str(data.get("type", ""))
    payload_raw = data.get("payload")
    payload: dict[str, str] = (
        {str(k): str(v) for k, v in payload_raw.items()} if isinstance(payload_raw, dict) else {}
    )
    children_raw = data.get("children")
    children: list[Block] = (
        [block_from_dict(child) for child in children_raw if isinstance(child, dict)]
        if isinstance(children_raw, list)
        else []
    )
    return Block(type=block_type, payload=payload, children=children)
```

- [ ] **Step 4: Run tests + lint + types**

Run: `cd shared && uv run python3 test_structural_extract.py && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors.

- [ ] **Step 5: Commit**

```bash
git add shared/podcast_shared/structural_extract.py shared/test_structural_extract.py
git commit -m "feat(shared): block_from_dict to rebuild a Block from its sidecar entry"
```

---

### Task 2: `render_block_aside` — one block to spoken aside text

**Files:**
- Create: `shared/podcast_shared/aside_render.py`
- Test: `shared/test_aside_render.py`

**Interfaces:**
- Produces: `render_block_aside(block: Block) -> str` — a spoken aside string, children appended recursively. Templates:
  - `tweet`: `"The author shares a tweet from {handle}: {text}."` (drop `from {handle}` when handle is empty).
  - `image`: `"Image: {desc}."` where `desc` = payload `description`/`caption`/`alt` (first non-empty), else `"The author includes an image."`.
  - anything else: `"The author includes embedded content."` (generic fallback; real templates arrive with each type's handler in later plans).

- [ ] **Step 1: Write the failing test**

Create `shared/test_aside_render.py`:

```python
"""Tests for aside rendering (script-style, repo house style)."""

import logging

from podcast_shared.aside_render import render_block_aside
from podcast_shared.structural_extract import Block

logging.basicConfig(level=logging.INFO)


def _fail(msg: str) -> None:
    """Raise an AssertionError.

    Raises:
        AssertionError: Always.

    """
    raise AssertionError(msg)


def test_render_tweet() -> None:
    """A tweet renders with handle and text."""
    out = render_block_aside(Block(type="tweet", payload={"handle": "@tszzl", "text": "press the button"}))
    if out != "The author shares a tweet from @tszzl: press the button.":
        _fail(f"tweet aside was {out!r}")


def test_render_tweet_without_handle() -> None:
    """A handle-less tweet omits the 'from' clause."""
    out = render_block_aside(Block(type="tweet", payload={"handle": "", "text": "anon"}))
    if out != "The author shares a tweet: anon.":
        _fail(f"anon tweet aside was {out!r}")


def test_render_tweet_with_image_child() -> None:
    """A nested image is appended to the tweet aside."""
    block = Block(
        type="tweet",
        payload={"handle": "@x", "text": "look"},
        children=[Block(type="image", payload={"alt": "a bar chart"})],
    )
    out = render_block_aside(block)
    if "look." not in out or "Image: a bar chart." not in out:
        _fail(f"nested aside was {out!r}")


def test_render_generic_fallback() -> None:
    """Unknown types get a generic aside."""
    out = render_block_aside(Block(type="poll", payload={}))
    if out != "The author includes embedded content.":
        _fail(f"fallback aside was {out!r}")


def run_tests() -> None:
    """Run render tests."""
    test_render_tweet()
    test_render_tweet_without_handle()
    test_render_tweet_with_image_child()
    test_render_generic_fallback()
    logging.info("aside render tests passed")


if __name__ == "__main__":
    run_tests()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run python3 test_aside_render.py`
Expected: FAIL — `ModuleNotFoundError: podcast_shared.aside_render`.

- [ ] **Step 3: Implement**

Create `shared/podcast_shared/aside_render.py`:

```python
"""Render an embed block tree into spoken 'aside' text.

Pure and voice-agnostic: produces the words a meta-narrator would say. How those
words are voiced (a distinct aside voice vs. the single narration voice) is decided
by the renderer in text-to-speech (a later plan).
"""

from __future__ import annotations

from podcast_shared.structural_extract import Block


def _render_own(block: Block) -> str:
    if block.type == "tweet":
        text = block.payload.get("text", "")
        handle = block.payload.get("handle", "")
        if handle:
            return f"The author shares a tweet from {handle}: {text}."
        return f"The author shares a tweet: {text}."
    if block.type == "image":
        desc = block.payload.get("description") or block.payload.get("caption") or block.payload.get("alt") or ""
        return f"Image: {desc}." if desc else "The author includes an image."
    return "The author includes embedded content."


def render_block_aside(block: Block) -> str:
    """Render a block (and its children) into one spoken aside string.

    Returns:
        The aside text; child asides are appended after the parent's own text.

    """
    parts = [_render_own(block)]
    parts.extend(render_block_aside(child) for child in block.children)
    return " ".join(part for part in parts if part)
```

- [ ] **Step 4: Run tests + lint + types**

Run: `cd shared && uv run python3 test_aside_render.py && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors.

- [ ] **Step 5: Commit**

```bash
git add shared/podcast_shared/aside_render.py shared/test_aside_render.py
git commit -m "feat(shared): render_block_aside — block tree to spoken aside text"
```

---

### Task 3: `resolve_markers` — rewrite marker text into speech-ready text

**Files:**
- Modify: `shared/podcast_shared/aside_render.py`
- Modify: `shared/podcast_shared/__init__.py` (export `render_block_aside`, `resolve_markers`, `block_from_dict`)
- Test: `shared/test_aside_render.py` (append)

**Interfaces:**
- Produces: `resolve_markers(text: str, sidecar: dict[str, dict[str, object]]) -> str` — replaces each `⟦EMBED:NNNN⟧` marker with the rendered aside for its sidecar entry; markers with no sidecar entry are removed. Non-marker text is unchanged.

- [ ] **Step 1: Write the failing test (append + add to run_tests)**

```python
from podcast_shared.aside_render import resolve_markers  # noqa: E402
from podcast_shared.structural_extract import serialize_blocks  # noqa: E402


def test_resolve_markers_replaces_embed() -> None:
    """A marker is replaced by its rendered aside; narrative text is preserved."""
    text, sidecar = serialize_blocks([
        Block(type="text", payload={"text": "He seems spooked:"}),
        Block(type="tweet", payload={"handle": "@tszzl", "text": "press it"}),
        Block(type="text", payload={"text": "leading on."}),
    ])
    out = resolve_markers(text, sidecar)
    if "⟦EMBED" in out:
        _fail(f"marker not resolved: {out!r}")
    if "He seems spooked:" not in out or "leading on." not in out:
        _fail(f"narrative lost: {out!r}")
    if "The author shares a tweet from @tszzl: press it." not in out:
        _fail(f"aside missing: {out!r}")


def test_resolve_markers_drops_unknown_id() -> None:
    """A marker with no sidecar entry is removed, not left literal."""
    out = resolve_markers("before ⟦EMBED:0007⟧ after", {})
    if "⟦EMBED" in out:
        _fail(f"unknown marker left: {out!r}")
    if "before" not in out or "after" not in out:
        _fail(f"surrounding text lost: {out!r}")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run python3 test_aside_render.py`
Expected: FAIL — `ImportError` for `resolve_markers`.

- [ ] **Step 3: Implement**

Add to `aside_render.py`:

```python
import re

from podcast_shared.structural_extract import (
    EMBED_MARKER_PREFIX,
    EMBED_MARKER_SUFFIX,
    block_from_dict,
)

_MARKER_RE = re.compile(re.escape(EMBED_MARKER_PREFIX) + r"(\d+)" + re.escape(EMBED_MARKER_SUFFIX))


def resolve_markers(text: str, sidecar: dict[str, dict[str, object]]) -> str:
    """Replace ``⟦EMBED:id⟧`` markers with rendered asides.

    Args:
        text: Serialized narrative text containing embed markers.
        sidecar: Marker id -> serialized block payload.

    Returns:
        Speech-ready text with markers resolved (unknown ids removed).

    """

    def replace(match: re.Match[str]) -> str:
        entry = sidecar.get(match.group(1))
        if entry is None:
            return ""
        return render_block_aside(block_from_dict(entry))

    return _MARKER_RE.sub(replace, text)
```

Move the `import re` to the top of the file with the other imports (do not leave a mid-file import). Add explicit re-exports to `shared/podcast_shared/__init__.py`:

```python
from podcast_shared.aside_render import render_block_aside as render_block_aside
from podcast_shared.aside_render import resolve_markers as resolve_markers
from podcast_shared.structural_extract import block_from_dict as block_from_dict
```

- [ ] **Step 4: Run tests + lint + types**

Run: `cd shared && uv run python3 test_aside_render.py && uv run python3 test_structural_extract.py && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors.

- [ ] **Step 5: End-to-end check on the real corpus email**

Run:

```bash
cd shared && uv run python3 -c "
from pathlib import Path
from podcast_shared import find_content_region, extract_blocks, serialize_blocks, resolve_markers
html = Path('../email-corpus/astralcodexten-substack-com/10200.html').read_text(errors='replace')
text, sidecar = serialize_blocks(extract_blocks(find_content_region(html)))
out = resolve_markers(text, sidecar)
i = out.find('spooked')
print(out[i:i+240])
"
```
Expected: the passage now reads "...He seems spooked: The author shares a tweet from @tszzl: …" — the dropped tweet spoken inline, no `⟦EMBED⟧` markers remaining.

- [ ] **Step 6: Commit**

```bash
git add shared/podcast_shared/aside_render.py shared/podcast_shared/__init__.py shared/test_aside_render.py
git commit -m "feat(shared): resolve_markers — marker text to speech-ready asides"
```

---

## Self-Review

- **Spec coverage:** §6 tweet/image aside phrasings (T2 templates; remaining types get real templates alongside their handlers), §9 marker resolution into narration (T3). Voice differentiation (aside voice vs. single voice), WaveNet/Gemini paths, and the intake→prepare-text→TTS wiring are Plan 4 — deferred so the live blockquote-multivoice feature isn't regressed before the extractor reaches parity.
- **Types:** `block_from_dict` mirrors `_block_to_dict`; `resolve_markers`'s `sidecar` type matches `serialize_blocks`'s return; re-exports use the redundant-alias form.
- **Placeholders:** none — runnable code or exact commands throughout.
- **House-style tests:** script-style, typed, `raise AssertionError`, `python3` — no pytest, no suppressions.
