# Structural Extractor Core — Implementation Plan (Plan 2 of the embed-recovery series)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A pure `shared` library that turns article HTML into an ordered, nesting-capable block tree (text + tweet to start) and serialises it to marker-annotated text plus a sidecar payload map — the substrate every later handler and the renderer build on.

**Architecture:** `structural_extract.py` finds the content region (Substack `div.body.markup`, else `<article>`, else whole document), walks it in document order into `Block(type, payload, children)` nodes, and `serialize()` emits reader text with `⟦EMBED:<id>⟧` markers whose payloads live in a sidecar dict. No pipeline wiring, no network, no LLM — that is Plan 3+.

**Tech Stack:** Python 3.12, `beautifulsoup4` (already a `shared`… no — added here), ruff, basedpyright. Tests are script-style (repo house style), run via `python3`.

**Spec:** `docs/superpowers/specs/2026-08-26-embed-recovery-design.md` (§5 Structural extractor, §6 tweet handler row). This plan implements the §5 core + the tweet slice of §6.

## Global Constraints

- Line length 120; ruff `ALL` (per root config); basedpyright `typeCheckingMode = "all"`, **zero errors**. No `cast()`; narrow with `isinstance`/`str()`/`getattr()`. No pyright-ignore comments.
- `@dataclass(slots=True)` (not frozen). Plain dicts/lists.
- Tests are script-style: fully typed, `raise AssertionError` (no bare `assert`, no pytest fixtures), a `run_tests()` + `if __name__ == "__main__"`, run with `uv run python3 <file>`. This matches the repo (`prepare-text/test_*.py`, `shared/test_intake_store.py`) and passes strict tooling without suppressions.
- `bs4` is not yet a dependency of `shared`; Task 1 adds `beautifulsoup4`.
- The embed marker must survive `prepare-text` cleaning (like `BLOCKQUOTE_MARKER`): no brackets/underscores/collapsible whitespace beyond the sentinel chars, and never occur in prose. Use guillemet-bracket sentinels `⟦ … ⟧` (U+27E6/U+27E7) with an `EMBED:` tag.

---

### Task 1: `Block` type + content-region detection

**Files:**
- Create: `shared/podcast_shared/structural_extract.py`
- Modify: `shared/pyproject.toml` (add `beautifulsoup4>=4.12`)
- Test: `shared/test_structural_extract.py`

**Interfaces:**
- Produces:
  - `Block` — `@dataclass(slots=True)` with `type: str`, `payload: dict[str, str]`, `children: list[Block]` (default empty).
  - `find_content_region(html: str) -> bs4.element.Tag` — returns the Substack body (`div.body.markup`), else the first `<article>`, else the parsed document root.

- [ ] **Step 1: Add bs4 dependency**

In `shared/pyproject.toml` `dependencies`, add `"beautifulsoup4>=4.12"`. Then:

Run: `cd shared && uv sync`
Expected: resolves with bs4 installed.

- [ ] **Step 2: Write the failing test**

Create `shared/test_structural_extract.py`:

```python
"""Tests for the structural extractor (script-style, repo house style)."""

import logging

from podcast_shared.structural_extract import Block, find_content_region

logging.basicConfig(level=logging.INFO)


def _fail(msg: str) -> None:
    """Raise an AssertionError.

    Raises:
        AssertionError: Always.

    """
    raise AssertionError(msg)


def test_region_prefers_substack_body() -> None:
    """Substack's div.body.markup wins over the surrounding chrome."""
    html = '<html><body><div class="header">nav</div>' \
        '<div class="body markup"><p>real content</p></div></body></html>'
    region = find_content_region(html)
    text = region.get_text(" ", strip=True)
    if "real content" not in text or "nav" in text:
        _fail(f"region text was {text!r}")


def test_region_falls_back_to_article_then_document() -> None:
    """Without a Substack body, prefer <article>, else the whole document."""
    art = find_content_region("<html><body><article><p>A</p></article><p>B</p></body></html>")
    if art.get_text(" ", strip=True) != "A":
        _fail(f"article region text was {art.get_text(' ', strip=True)!r}")
    whole = find_content_region("<html><body><p>only</p></body></html>")
    if "only" not in whole.get_text(" ", strip=True):
        _fail("document fallback lost content")


def test_block_is_a_tree() -> None:
    """Block carries a type, payload and children."""
    b = Block(type="tweet", payload={"handle": "@x", "text": "hi"}, children=[Block(type="image", payload={})])
    if b.type != "tweet" or b.payload["handle"] != "@x" or len(b.children) != 1:
        _fail("Block shape wrong")


def run_tests() -> None:
    """Run region + Block tests."""
    test_region_prefers_substack_body()
    test_region_falls_back_to_article_then_document()
    test_block_is_a_tree()
    logging.info("region/Block tests passed")


if __name__ == "__main__":
    run_tests()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd shared && uv run python3 test_structural_extract.py`
Expected: FAIL — `ModuleNotFoundError: podcast_shared.structural_extract`.

- [ ] **Step 4: Write minimal implementation**

Create `shared/podcast_shared/structural_extract.py`:

```python
"""Turn article HTML into an ordered block tree and serialise it for the pipeline.

Pure and offline: no network, no LLM. Later plans add handlers (image/video/…),
the LLM fallback, non-Substack region detection, and the renderer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from bs4.element import Tag


@dataclass(slots=True)
class Block:
    """One node in the extracted content tree.

    ``type`` is a handler key (``text``/``tweet``/``image``/…); ``payload`` holds
    the type's spoken fields; ``children`` holds nested embeds (e.g. a tweet's image).
    """

    type: str
    payload: dict[str, str]
    children: list[Block] = field(default_factory=list)


def find_content_region(html: str) -> Tag:
    """Return the DOM subtree holding the article body.

    Prefers Substack's ``div.body.markup``, then the first ``<article>``, then the
    whole parsed document. Always returns a Tag (embeds survive — we never route
    the body through trafilatura).
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("div.body.markup")
    if body is not None:
        return body
    article = soup.find("article")
    if isinstance(article, Tag):
        return article
    return soup
```

- [ ] **Step 5: Run tests + lint + types**

Run: `cd shared && uv run python3 test_structural_extract.py && uv run ruff check . && uv run basedpyright`
Expected: tests PASS; ruff clean; basedpyright zero errors.

- [ ] **Step 6: Commit**

```bash
git add shared/podcast_shared/structural_extract.py shared/test_structural_extract.py shared/pyproject.toml shared/uv.lock
git commit -m "feat(shared): structural extractor Block type + content-region detection"
```

---

### Task 2: Tweet detection + extraction

**Files:**
- Modify: `shared/podcast_shared/structural_extract.py`
- Test: `shared/test_structural_extract.py`

**Interfaces:**
- Produces:
  - `is_tweet(el: Tag) -> bool` — true for Substack `data-component-name` starting `Tweet`, or a class containing `tweet`/`twitter`.
  - `extract_tweet(el: Tag) -> Block` — `Block("tweet", {"handle": "@…", "text": "<cleaned body>"})`. `text` has engagement/timestamp trailers (`8:59 PM · Jul 25, 2026 · 568K Views 354 Replies · 249 Reposts · 3.51K Likes`) stripped; `handle` is the first `@\w+` found, else `""`.

- [ ] **Step 1: Write the failing test (append)**

Append to `shared/test_structural_extract.py` (and add the calls to `run_tests`):

```python
from podcast_shared.structural_extract import extract_tweet, is_tweet  # noqa: E402

_TWEET_HTML = (
    '<table class="twitter-embed tweet" data-component-name="TweetToDOMStatic"><tbody><tr><td>'
    '<div>roon</div><div>@tszzl</div>'
    '<div>if we could coordinate a global capabilities slowdown today i would likely press that magic button</div>'
    '<div>8:59 PM · Jul 25, 2026 · 568K Views</div>'
    '<div>354 Replies · 249 Reposts · 3.51K Likes</div>'
    '</td></tr></tbody></table>'
)


def test_is_tweet_detects_substack_embed() -> None:
    """The Substack tweet table is recognised."""
    from bs4 import BeautifulSoup

    el = BeautifulSoup(_TWEET_HTML, "html.parser").find("table")
    if not isinstance(el, Tag) or not is_tweet(el):
        _fail("tweet table not detected")


def test_extract_tweet_pulls_handle_and_clean_text() -> None:
    """Handle and body are extracted; engagement/time trailers are stripped."""
    from bs4 import BeautifulSoup

    el = BeautifulSoup(_TWEET_HTML, "html.parser").find("table")
    if not isinstance(el, Tag):
        _fail("no table")
        return
    block = extract_tweet(el)
    if block.type != "tweet":
        _fail(f"type {block.type!r}")
    if block.payload.get("handle") != "@tszzl":
        _fail(f"handle {block.payload.get('handle')!r}")
    text = block.payload.get("text", "")
    if "press that magic button" not in text:
        _fail(f"body missing: {text!r}")
    for junk in ("568K Views", "Replies", "Reposts", "Likes", "8:59 PM"):
        if junk in text:
            _fail(f"engagement junk {junk!r} not stripped: {text!r}")
```

Add `Tag` to the imports at the top: `from bs4.element import Tag` (needed by the new tests). Add the two new tests to `run_tests()`.

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run python3 test_structural_extract.py`
Expected: FAIL — `ImportError` for `extract_tweet`/`is_tweet`.

- [ ] **Step 3: Implement**

Add to `structural_extract.py`:

```python
import re

_HANDLE_RE = re.compile(r"@\w+")
# Trailing engagement/time line Substack renders under a tweet, e.g.
# "8:59 PM · Jul 25, 2026 · 568K Views 354 Replies · 249 Reposts · 3.51K Likes".
_ENGAGEMENT_RE = re.compile(
    r"\s*\d{1,2}:\d{2}\s*(?:AM|PM)\b.*$"
    r"|\s*[\d.,KMB]+\s*(?:Views|Replies|Reposts|Likes|Quotes|Bookmarks)\b",
    re.IGNORECASE,
)


def _classes(el: Tag) -> str:
    value = el.get("class")
    return " ".join(value).lower() if isinstance(value, list) else str(value or "").lower()


def is_tweet(el: Tag) -> bool:
    """Whether ``el`` is a tweet/X embed."""
    dcn = str(el.get("data-component-name") or "")
    if dcn.startswith("Tweet"):
        return True
    cls = _classes(el)
    return "tweet" in cls or "twitter" in cls


def extract_tweet(el: Tag) -> Block:
    """Extract a tweet embed into a Block, stripping engagement/time trailers."""
    pieces = [seg for seg in (s.strip() for s in el.stripped_strings) if seg]
    joined = " ".join(pieces)
    handle_match = _HANDLE_RE.search(joined)
    handle = handle_match.group(0) if handle_match else ""
    # Drop the author name + handle prefix, then everything from the engagement line on.
    body = joined
    if handle:
        body = body.split(handle, 1)[1].strip()
    body = _ENGAGEMENT_RE.sub("", body).strip()
    return Block(type="tweet", payload={"handle": handle, "text": body})
```

- [ ] **Step 4: Run tests + lint + types**

Run: `cd shared && uv run python3 test_structural_extract.py && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors.

- [ ] **Step 5: Commit**

```bash
git add shared/podcast_shared/structural_extract.py shared/test_structural_extract.py
git commit -m "feat(shared): tweet detection + extraction for structural extractor"
```

---

### Task 3: Ordered block walk (text + tweet, position-preserving)

**Files:**
- Modify: `shared/podcast_shared/structural_extract.py`
- Test: `shared/test_structural_extract.py`

**Interfaces:**
- Produces:
  - `extract_blocks(region: Tag) -> list[Block]` — walks ``region`` in document order, emitting a `tweet` Block for each tweet embed and a `text` Block (`payload={"text": …}`) for each block-level text element (`p`, `li`, `blockquote`, `h1`–`h4`) that is not inside a tweet. Consecutive duplicate texts are dropped (mirrors `extract_body_text`). Order matches the page, so an embed lands between the surrounding paragraphs.

- [ ] **Step 1: Write the failing test (append)**

```python
from podcast_shared.structural_extract import extract_blocks  # noqa: E402


def test_extract_blocks_preserves_tweet_position() -> None:
    """A tweet between two paragraphs yields text, tweet, text in order."""
    html = (
        '<div class="body markup">'
        '<p>He seems spooked:</p>'
        + _TWEET_HTML
        + '<p>leading to a conversation.</p>'
        '</div>'
    )
    region = find_content_region(html)
    blocks = extract_blocks(region)
    kinds = [b.type for b in blocks]
    if kinds != ["text", "tweet", "text"]:
        _fail(f"block kinds were {kinds}")
    if blocks[0].payload["text"] != "He seems spooked:":
        _fail(f"first text {blocks[0].payload['text']!r}")
    if blocks[1].payload["handle"] != "@tszzl":
        _fail(f"tweet handle {blocks[1].payload['handle']!r}")
    if "conversation" not in blocks[2].payload["text"]:
        _fail(f"last text {blocks[2].payload['text']!r}")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run python3 test_structural_extract.py`
Expected: FAIL — `ImportError` for `extract_blocks`.

- [ ] **Step 3: Implement**

Add to `structural_extract.py`:

```python
_TEXT_TAGS = ("p", "li", "blockquote", "h1", "h2", "h3", "h4")
_TWEET_TAGS = ("table", "blockquote", "div")


def extract_blocks(region: Tag) -> list[Block]:
    """Walk ``region`` in document order into text and tweet Blocks."""
    blocks: list[Block] = []
    previous_text: str | None = None
    consumed: set[int] = set()  # id() of elements already emitted as a tweet
    for el in region.find_all(_TEXT_TAGS + ("table",)):
        if not isinstance(el, Tag):
            continue
        if any(id(ancestor) in consumed for ancestor in el.parents):
            continue
        if is_tweet(el):
            blocks.append(extract_tweet(el))
            consumed.add(id(el))
            previous_text = None
            continue
        if el.name not in _TEXT_TAGS:
            continue
        if el.find(_TEXT_TAGS):  # skip block nested in a block (dedupe)
            continue
        text = " ".join(el.get_text(" ").split())
        if not text or text == previous_text:
            continue
        previous_text = text
        blocks.append(Block(type="text", payload={"text": text}))
    return blocks
```

Note: `_TWEET_TAGS` is declared for a later plan's generic tweet scan; if ruff flags it as unused now, inline the tuple into `find_all` instead and drop the constant.

- [ ] **Step 4: Run tests + lint + types**

Run: `cd shared && uv run python3 test_structural_extract.py && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors. (If `_TWEET_TAGS` is flagged unused, remove it.)

- [ ] **Step 5: Commit**

```bash
git add shared/podcast_shared/structural_extract.py shared/test_structural_extract.py
git commit -m "feat(shared): position-preserving block walk (text + tweet)"
```

---

### Task 4: Serialization to marker text + sidecar

**Files:**
- Modify: `shared/podcast_shared/structural_extract.py`
- Modify: `shared/podcast_shared/__init__.py` (export `EMBED_MARKER_PREFIX`, `EMBED_MARKER_SUFFIX`, `serialize_blocks`, `Block`, `find_content_region`, `extract_blocks`)
- Test: `shared/test_structural_extract.py`

**Interfaces:**
- Produces:
  - `EMBED_MARKER_PREFIX = "⟦EMBED:"`, `EMBED_MARKER_SUFFIX = "⟧"`.
  - `serialize_blocks(blocks: list[Block]) -> tuple[str, dict[str, dict[str, object]]]` — returns `(text, sidecar)`. Text joins blocks with blank lines; a `text` block contributes its text, any other block contributes a marker line `⟦EMBED:NNNN⟧` (zero-padded 4-digit id). `sidecar[id] = {"type", "payload", "children": [ …recursively… ]}`.

- [ ] **Step 1: Write the failing test (append)**

```python
from podcast_shared.structural_extract import serialize_blocks  # noqa: E402


def test_serialize_emits_markers_and_sidecar() -> None:
    """Text stays inline; embeds become markers with sidecar payloads."""
    blocks = [
        Block(type="text", payload={"text": "He seems spooked:"}),
        Block(type="tweet", payload={"handle": "@tszzl", "text": "press the button"}),
        Block(type="text", payload={"text": "leading on."}),
    ]
    text, sidecar = serialize_blocks(blocks)
    if "He seems spooked:" not in text or "leading on." not in text:
        _fail(f"narrative text lost: {text!r}")
    if "⟦EMBED:0000⟧" not in text:
        _fail(f"marker missing: {text!r}")
    entry = sidecar.get("0000")
    if entry is None or entry.get("type") != "tweet":
        _fail(f"sidecar entry wrong: {sidecar!r}")
    payload = entry.get("payload")
    if not isinstance(payload, dict) or payload.get("handle") != "@tszzl":
        _fail(f"sidecar payload wrong: {entry!r}")


def test_serialize_recurses_children() -> None:
    """Nested children (tweet-with-image) are serialised recursively."""
    blocks = [Block(type="tweet", payload={"handle": "@x", "text": "t"},
                    children=[Block(type="image", payload={"alt": "a chart"})])]
    _text, sidecar = serialize_blocks(blocks)
    entry = sidecar["0000"]
    children = entry.get("children")
    if not isinstance(children, list) or not children:
        _fail(f"children not serialised: {entry!r}")
    first = children[0]
    if not isinstance(first, dict) or first.get("type") != "image":
        _fail(f"child wrong: {children!r}")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run python3 test_structural_extract.py`
Expected: FAIL — `ImportError` for `serialize_blocks`.

- [ ] **Step 3: Implement**

Add to `structural_extract.py`:

```python
EMBED_MARKER_PREFIX = "⟦EMBED:"
EMBED_MARKER_SUFFIX = "⟧"


def _block_to_dict(block: Block) -> dict[str, object]:
    return {
        "type": block.type,
        "payload": dict(block.payload),
        "children": [_block_to_dict(child) for child in block.children],
    }


def serialize_blocks(blocks: list[Block]) -> tuple[str, dict[str, dict[str, object]]]:
    """Serialise blocks to marker-annotated text plus a sidecar payload map."""
    lines: list[str] = []
    sidecar: dict[str, dict[str, object]] = {}
    counter = 0
    for block in blocks:
        if block.type == "text":
            lines.append(block.payload.get("text", ""))
            continue
        marker_id = f"{counter:04d}"
        counter += 1
        lines.append(f"{EMBED_MARKER_PREFIX}{marker_id}{EMBED_MARKER_SUFFIX}")
        sidecar[marker_id] = _block_to_dict(block)
    return "\n\n".join(lines), sidecar
```

Add the explicit re-exports to `shared/podcast_shared/__init__.py` (redundant-alias form, matching `store_intake_html`):

```python
from podcast_shared.structural_extract import Block as Block
from podcast_shared.structural_extract import EMBED_MARKER_PREFIX as EMBED_MARKER_PREFIX
from podcast_shared.structural_extract import EMBED_MARKER_SUFFIX as EMBED_MARKER_SUFFIX
from podcast_shared.structural_extract import extract_blocks as extract_blocks
from podcast_shared.structural_extract import find_content_region as find_content_region
from podcast_shared.structural_extract import serialize_blocks as serialize_blocks
```

- [ ] **Step 4: Run tests + lint + types**

Run: `cd shared && uv run python3 test_structural_extract.py && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors.

- [ ] **Step 5: Commit**

```bash
git add shared/podcast_shared/structural_extract.py shared/podcast_shared/__init__.py shared/test_structural_extract.py
git commit -m "feat(shared): serialize block tree to marker text + sidecar"
```

---

## Self-Review

- **Spec coverage:** §5.1 region detection (T1, Substack + article + document; trafilatura-anchored non-Substack path is deferred to Plan 4 as designed), §5.2 block tree + nesting (T1 `Block`, T3 walk, T4 recursive serialise), §5.3 marker + sidecar serialization (T4), §6 tweet row incl. engagement-junk stripping (T2). Image/video/footnote/card/code handlers, LLM fallback, toggles, rendering, and intake wiring are later plans — intentionally out of scope.
- **Types:** `find_content_region`/`extract_blocks` return `Tag`; `Block.children` recursion is consistent; `serialize_blocks` returns the same `(str, dict)` shape the tests assert; re-exports use the redundant-alias form proven in Plan 1.
- **Placeholders:** none — every step has runnable code or an exact command. The one conditional (`_TWEET_TAGS` unused) has an explicit remove-if-flagged instruction.
- **House-style tests:** script-style, fully typed, `raise AssertionError`, run via `python3` — no pytest, no suppressions.
