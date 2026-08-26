# Extractor Parity (quote + image) — Implementation Plan (Plan 4 of the embed-recovery series)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the structural extractor to emit `quote` blocks (matching the existing blockquote-multivoice feature) and `image` blocks (alt + figcaption), so a later plan can switch the live intake to it **without regressing** blockquote handling or silently dropping images.

**Architecture:** Add block-quotation detection (an element inside `<blockquote>`) and image extraction (`<figure>` = image + `<figcaption>`; bare `<img>` with meaningful alt, skipping chrome) to `extract_blocks`. Add a `quote` render template (read verbatim — quotes are the author's own quoted material, not announced asides). Pure, offline, unit-tested; no pipeline wiring (that's Plan 5).

**Tech Stack:** Python 3.12, `beautifulsoup4`, ruff, basedpyright. Script-style tests via `uv run python3`.

**Spec:** `docs/superpowers/specs/2026-08-26-embed-recovery-design.md` (§5 block tree, §6 image row; blockquote parity with the existing `BLOCKQUOTE_MARKER` behaviour in `imap/parse_email.py:extract_body_text`).

## Global Constraints

- Line length 120; ruff `ALL`; basedpyright `typeCheckingMode = "all"`, **zero errors** (targeted `# pyright: ignore` only at genuine untyped boundaries, as already used in `block_from_dict`). No `cast()`.
- `@dataclass(slots=True)`. Script-style tests (typed, `raise AssertionError`, `run_tests()` + `__main__`).
- Parity reference: `extract_body_text` marks a block as a quote when `element.name == "blockquote"` or `element.find_parent("blockquote") is not None`. Mirror that.
- Reuse `Block`, `is_tweet`, `extract_tweet`, `_classes` from `structural_extract.py`.

---

### Task 1: Emit `quote` blocks for block-quotations

**Files:**
- Modify: `shared/podcast_shared/structural_extract.py` (`extract_blocks`)
- Test: `shared/test_structural_extract.py` (append)

**Interfaces:**
- Changed: `extract_blocks` now emits a `Block(type="quote", payload={"text": …})` for any text element that is a `<blockquote>` or lives inside one; other text stays `type="text"`. Ordering and tweet handling unchanged.

- [ ] **Step 1: Write the failing test (append + add to run_tests)**

```python
def test_extract_blocks_marks_quotes() -> None:
    """A blockquote paragraph becomes a quote block; normal paragraphs stay text."""
    html = (
        '<div class="body markup">'
        "<p>Normal.</p>"
        "<blockquote><p>Quoted line.</p></blockquote>"
        "<p>After.</p>"
        "</div>"
    )
    blocks = extract_blocks(find_content_region(html))
    kinds = [(b.type, b.payload.get("text", "")) for b in blocks]
    if ("quote", "Quoted line.") not in kinds:
        _fail(f"quote not marked: {kinds}")
    if ("text", "Normal.") not in kinds:
        _fail(f"normal text mismarked: {kinds}")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run python3 test_structural_extract.py`
Expected: FAIL — the quoted line is currently emitted as `text`, not `quote`.

- [ ] **Step 3: Implement**

In `extract_blocks`, replace the final text-emitting block:

```python
        text = " ".join(el.get_text(" ").split())
        if not text or text == previous_text:
            continue
        previous_text = text
        blocks.append(Block(type="text", payload={"text": text}))
```

with quote-aware emission:

```python
        text = " ".join(el.get_text(" ").split())
        if not text or text == previous_text:
            continue
        previous_text = text
        is_quote = el.name == "blockquote" or el.find_parent("blockquote") is not None
        blocks.append(Block(type="quote" if is_quote else "text", payload={"text": text}))
```

- [ ] **Step 4: Run tests + lint + types**

Run: `cd shared && uv run python3 test_structural_extract.py && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors.

- [ ] **Step 5: Commit**

```bash
git add shared/podcast_shared/structural_extract.py shared/test_structural_extract.py
git commit -m "feat(shared): emit quote blocks for block-quotations (parity with extract_body_text)"
```

---

### Task 2: Extract `image` blocks (figure + bare img)

**Files:**
- Modify: `shared/podcast_shared/structural_extract.py`
- Test: `shared/test_structural_extract.py` (append)

**Interfaces:**
- Produces:
  - `extract_image(el: Tag) -> Block` — `Block("image", {"alt": …, "caption": …, "src": …})`. For a `<figure>`, reads its inner `<img>` alt/src and `<figcaption>` text; for an `<img>`, reads alt/src (caption empty).
  - `_is_decorative(el: Tag) -> bool` — true for chrome images to skip: empty alt AND (class contains `avatar`/`icon`/`logo`/`badge`/`emoji`, or `src` starts `data:`).
- Changed: `extract_blocks` walks `figure` and `img` too. A `<figure>` emits one image block and marks its inner `<img>` consumed; a bare non-decorative `<img>` emits an image block. Document order is preserved.

- [ ] **Step 1: Write the failing test (append + add to run_tests)**

```python
def test_extract_image_from_figure() -> None:
    """A figure yields an image block with alt + caption."""
    html = (
        '<div class="body markup">'
        '<figure><img src="/c.png" alt="a chart"><figcaption>Fig 1: growth</figcaption></figure>'
        "</div>"
    )
    blocks = extract_blocks(find_content_region(html))
    imgs = [b for b in blocks if b.type == "image"]
    if len(imgs) != 1:
        _fail(f"expected 1 image, got {[b.type for b in blocks]}")
    if imgs[0].payload.get("alt") != "a chart" or imgs[0].payload.get("caption") != "Fig 1: growth":
        _fail(f"image payload wrong: {imgs[0].payload}")


def test_bare_image_and_decorative_filter() -> None:
    """A content img is kept; a decorative (empty-alt icon) img is skipped."""
    html = (
        '<div class="body markup">'
        '<p>x</p><img src="/photo.jpg" alt="a landscape">'
        '<img src="/spacer.gif" alt="" class="icon">'
        "</div>"
    )
    blocks = extract_blocks(find_content_region(html))
    imgs = [b for b in blocks if b.type == "image"]
    if len(imgs) != 1 or imgs[0].payload.get("alt") != "a landscape":
        _fail(f"decorative filter wrong: {[b.payload for b in imgs]}")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run python3 test_structural_extract.py`
Expected: FAIL — `ImportError`/no image blocks (images not yet walked).

- [ ] **Step 3: Implement**

Add helpers to `structural_extract.py`:

```python
_DECORATIVE_CLASS_RE = re.compile(r"\b(?:avatar|icon|logo|badge|emoji)\b")


def _is_decorative(el: Tag) -> bool:
    """Whether an ``<img>`` is chrome (skip it)."""
    alt = str(el.get("alt") or "").strip()
    if alt:
        return False
    src = str(el.get("src") or "")
    return bool(_DECORATIVE_CLASS_RE.search(_classes(el))) or src.startswith("data:")


def extract_image(el: Tag) -> Block:
    """Extract an image/figure into an ``image`` Block.

    Returns:
        A ``image`` Block with ``alt``/``caption``/``src`` payload fields.

    """
    img = el if el.name == "img" else el.find("img")
    alt = ""
    src = ""
    if isinstance(img, Tag):
        alt = str(img.get("alt") or "").strip()
        src = str(img.get("src") or "").strip()
    caption = ""
    figcaption = el.find("figcaption")
    if isinstance(figcaption, Tag):
        caption = " ".join(figcaption.get_text(" ").split())
    return Block(type="image", payload={"alt": alt, "caption": caption, "src": src})
```

In `extract_blocks`, extend the walk set and add image handling. Change the loop header:

```python
    for el in region.find_all((*_TEXT_TAGS, "table", "figure", "img")):
        if any(id(ancestor) in consumed for ancestor in el.parents):
            continue
        if is_tweet(el):
            blocks.append(extract_tweet(el))
            consumed.add(id(el))
            previous_text = None
            continue
        if el.name == "figure":
            blocks.append(extract_image(el))
            inner = el.find("img")
            if isinstance(inner, Tag):
                consumed.add(id(inner))
            previous_text = None
            continue
        if el.name == "img":
            if _is_decorative(el):
                continue
            blocks.append(extract_image(el))
            previous_text = None
            continue
        if el.name not in _TEXT_TAGS:
            continue
```

(Keep the existing text/quote emission below this.)

- [ ] **Step 4: Run tests + lint + types**

Run: `cd shared && uv run python3 test_structural_extract.py && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors.

- [ ] **Step 5: Commit**

```bash
git add shared/podcast_shared/structural_extract.py shared/test_structural_extract.py
git commit -m "feat(shared): extract image blocks (figure + bare img, decorative filter)"
```

---

### Task 3: Render `quote` blocks verbatim

**Files:**
- Modify: `shared/podcast_shared/aside_render.py`
- Test: `shared/test_aside_render.py` (append)

**Interfaces:**
- Changed: `render_block_aside` renders a `quote` block as its verbatim text (no "The author…" framing — quotes are read as quoted material; a distinct *voice* is applied later in TTS wiring). `image` and `tweet` unchanged.

- [ ] **Step 1: Write the failing test (append + add to run_tests)**

```python
def test_render_quote_is_verbatim() -> None:
    """A quote renders as its own text, not an announced aside."""
    out = render_block_aside(Block(type="quote", payload={"text": "To be or not to be."}))
    if out != "To be or not to be.":
        _fail(f"quote aside was {out!r}")


def test_render_image_from_caption() -> None:
    """An image with a caption renders as 'Image: <caption>'."""
    out = render_block_aside(Block(type="image", payload={"alt": "", "caption": "Fig 1: growth", "src": "/c.png"}))
    if out != "Image: Fig 1: growth.":
        _fail(f"image aside was {out!r}")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run python3 test_aside_render.py`
Expected: FAIL — a `quote` currently hits the generic fallback ("The author includes embedded content.").

- [ ] **Step 3: Implement**

In `aside_render.py` `_render_own`, add a `quote` branch before the fallback:

```python
    if block.type == "quote":
        return block.payload.get("text", "")
```

(The image branch already prefers `caption`/`alt`/`description`; `test_render_image_from_caption` should pass once quotes are handled — verify it does.)

- [ ] **Step 4: Run tests + lint + types + corpus check**

Run: `cd shared && uv run python3 test_aside_render.py && uv run python3 test_structural_extract.py && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors.

Corpus sanity (image-heavy source):

```bash
cd shared && uv run python3 -c "
from pathlib import Path
from podcast_shared import find_content_region, extract_blocks
html = Path('../email-corpus/maangchi-substack-com').glob('*.html').__next__().read_text(errors='replace')
blocks = extract_blocks(find_content_region(html))
from collections import Counter
print(Counter(b.type for b in blocks))
"
```
Expected: a `Counter` showing several `image` blocks (maangchi is image-heavy), plus `text`.

- [ ] **Step 5: Commit**

```bash
git add shared/podcast_shared/aside_render.py shared/test_aside_render.py
git commit -m "feat(shared): render quote blocks verbatim; confirm image aside"
```

---

## Self-Review

- **Spec coverage:** §5 block tree gains `quote` + `image` (T1, T2); §6 image row mechanical extraction (alt + figcaption; vision enrichment is the later image+vision plan); parity with `extract_body_text`'s blockquote marking (T1). Video/footnote/card/audio/code handlers, LLM fallback, toggles, non-Substack region detection, and the live wiring remain later plans.
- **Types:** `extract_image`/`_is_decorative` take `Tag`; `find`-returned values narrowed with `isinstance(..., Tag)` before use (no `cast`); quote/image payload keys match what `render_block_aside` reads (`text`, `alt`/`caption`/`description`).
- **Placeholders:** none — runnable code or exact commands throughout.
- **No regression risk:** this plan only extends the pure `shared` library; the live pipeline is untouched until Plan 5 wires it in.
