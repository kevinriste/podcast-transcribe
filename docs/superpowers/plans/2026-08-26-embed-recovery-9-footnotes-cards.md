# Footnotes + Embedded-Post Cards + Type Toggle — Implementation Plan (Plan 9)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Recover two more embed types from Substack email HTML — footnotes (read as a numbered section at the end) and embedded-post cards (one-line link announcement) — and add a per-type toggle so any embed type can be switched off at intake.

**Key corpus facts (verified):**
- Footnote **definitions** live in `div.footnote` (each with `span.footnote-number` + `div.footnote-content`) at the end of `div.body.markup`. Substack email HTML contains **no in-text footnote reference markers** (no `sup`, no `a.footnote-anchor`), so inline-at-reference is impossible — footnotes are emitted in document order (naturally last) as numbered asides.
- Cards live in `div.embedded-post-wrap` → `.embedded-post-publication-name` (publication) + `a.embedded-post-title` (title + href). Their text is in `span`/`a`/`div` tags the walk never visited, so today they are silently dropped.

**Architecture:** Add a `div` branch to the `extract_blocks` walk that recognizes `embedded-post-wrap`/`embedded-post` (→ `card`) and `footnote` (→ `footnote`), consuming the container so descendants don't double-emit. Render templates announce each. A `drop_types` argument on `serialize_flat` (fed by imap from `EMBED_DROP_TYPES`) suppresses chosen types. Pure, offline, unit-tested.

**Tech Stack:** Python 3.12, `beautifulsoup4`, ruff `ALL`, basedpyright `all`. Script-style tests.

## Global Constraints
- Line length 120; zero basedpyright errors; no `cast()`; reuse `Block`, `_classes`, `consumed`.
- Adding `"div"` to the walk must not emit text for non-card/non-footnote divs (they fall through to the existing `if el.name not in _TEXT_TAGS: continue`).
- Consumed-container guard prevents inner elements (`.footnote-content` `<p>`, inner `embedded-post`) from double-emitting.

---

### Task 1: Extract footnotes + cards (div handlers)

**Files:**
- Modify: `shared/podcast_shared/structural_extract.py`
- Test: `shared/test_structural_extract.py` (append)

**Interfaces:**
- Produces:
  - `extract_footnote(el: Tag) -> Block | None` — `Block("footnote", {"number": str, "text": str})`; `text` from `.footnote-content` (fallback: element text minus a leading number); `None` if no text.
  - `extract_card(el: Tag) -> Block | None` — `Block("card", {"title": str, "publication": str, "href": str})` from `a.embedded-post-title` (+ `href`) and `.embedded-post-publication-name`; `None` if no title.
- Changed: `extract_blocks` walk includes `"div"`; a `div` branch dispatches `embedded-post-wrap`/`embedded-post` → `extract_card`, `footnote` → `extract_footnote`, consuming the container; all other divs are ignored.

- [ ] **Step 1: Failing test (append + register in run_tests)**

```python
def test_extract_footnote_and_card() -> None:
    """A footnote div and an embedded-post card become footnote/card blocks; order preserved."""
    html = (
        '<div class="body markup">'
        "<p>Body text.</p>"
        '<div class="embedded-post-wrap"><div class="embedded-post">'
        '<div class="embedded-post-header"><span class="embedded-post-publication-name">The NNN Newsletter</span></div>'
        '<div class="embedded-post-title-wrapper"><a class="embedded-post-title" href="https://ex.com/p">'
        "Can You Control Your Beliefs?</a></div>"
        '<a class="embedded-post-cta">Read more</a>'
        '<div class="embedded-post-meta">15 days ago · 15 likes · Turi Munthe</div>'
        "</div></div>"
        '<div class="footnote"><span class="footnote-number">1</span>'
        '<div class="footnote-content">Counting headwords.</div></div>'
        "</div>"
    )
    blocks = extract_blocks(find_content_region(html))
    kinds = [b.type for b in blocks]
    if kinds != ["text", "card", "footnote"]:
        _fail(f"kinds were {kinds}")
    card = blocks[1]
    if card.payload.get("title") != "Can You Control Your Beliefs?" or card.payload.get("publication") != "The NNN Newsletter":
        _fail(f"card payload wrong: {card.payload}")
    if card.payload.get("href") != "https://ex.com/p":
        _fail(f"card href wrong: {card.payload}")
    fn = blocks[2]
    if fn.payload.get("number") != "1" or fn.payload.get("text") != "Counting headwords.":
        _fail(f"footnote payload wrong: {fn.payload}")
```

- [ ] **Step 2: Run — FAIL** (`ImportError` / div not walked). `cd shared && uv run python3 test_structural_extract.py`

- [ ] **Step 3: Implement**

```python
def extract_footnote(el: Tag) -> Block | None:
    """Extract a Substack footnote definition div into a footnote Block.

    Returns:
        A ``footnote`` Block, or None when it has no text.

    """
    number_el = el.find(class_="footnote-number")
    number = number_el.get_text(" ").strip() if isinstance(number_el, Tag) else ""
    content_el = el.find(class_="footnote-content")
    if isinstance(content_el, Tag):
        text = " ".join(content_el.get_text(" ").split())
    else:
        text = " ".join(el.get_text(" ").split())
        if number and text.startswith(number):
            text = text[len(number) :].strip()
    if not text:
        return None
    return Block(type="footnote", payload={"number": number, "text": text})


def extract_card(el: Tag) -> Block | None:
    """Extract a Substack embedded-post card into a card Block.

    Returns:
        A ``card`` Block, or None when it has no title.

    """
    title_el = el.find("a", class_="embedded-post-title")
    title = title_el.get_text(" ").strip() if isinstance(title_el, Tag) else ""
    if not title:
        return None
    href = str(title_el.get("href") or "") if isinstance(title_el, Tag) else ""
    pub_el = el.find(class_="embedded-post-publication-name")
    publication = pub_el.get_text(" ").strip() if isinstance(pub_el, Tag) else ""
    return Block(type="card", payload={"title": title, "publication": publication, "href": href})
```

In the `extract_blocks` walk, add `"div"` to `find_all` and a branch before the `_TEXT_TAGS` fallthrough:

```python
        if el.name == "div":
            classes = _classes(el)
            if "embedded-post-wrap" in classes or "embedded-post" in classes:
                card = extract_card(el)
                if card is not None:
                    blocks.append(card)
                    consumed.add(id(el))
                    previous_text = None
                continue
            if "footnote" in classes:
                footnote = extract_footnote(el)
                if footnote is not None:
                    blocks.append(footnote)
                    consumed.add(id(el))
                    previous_text = None
                continue
            continue
```

- [ ] **Step 4: Tests + lint + types.** `cd shared && uv run python3 test_structural_extract.py && uv run ruff check . && uv run basedpyright`

- [ ] **Step 5: Commit** — `git commit -m "feat(shared): extract footnote + embedded-post-card blocks"`

---

### Task 2: Render footnote + card asides

**Files:**
- Modify: `shared/podcast_shared/aside_render.py` (`_render_own`)
- Test: `shared/test_aside_render.py` (append)

**Interfaces:**
- `footnote`: `"Footnote <number>: <text>."` (drop `<number>` → `"Footnote: <text>."` when empty).
- `card`: `"The author links to a post titled '<title>' from <publication>."` (drop `from <publication>` when empty).

- [ ] **Step 1: Failing test (append + register)**

```python
def test_render_footnote_and_card() -> None:
    """Footnotes read as numbered notes; cards as a one-line link announcement."""
    fn = render_block_aside(Block(type="footnote", payload={"number": "1", "text": "Counting headwords."}))
    if fn != "Footnote 1: Counting headwords.":
        _fail(f"footnote aside was {fn!r}")
    fn0 = render_block_aside(Block(type="footnote", payload={"number": "", "text": "A note."}))
    if fn0 != "Footnote: A note.":
        _fail(f"numberless footnote was {fn0!r}")
    card = render_block_aside(Block(type="card", payload={"title": "On Beliefs", "publication": "NNN", "href": "u"}))
    if card != "The author links to a post titled 'On Beliefs' from NNN.":
        _fail(f"card aside was {card!r}")
    card0 = render_block_aside(Block(type="card", payload={"title": "On Beliefs", "publication": "", "href": "u"}))
    if card0 != "The author links to a post titled 'On Beliefs'.":
        _fail(f"publicationless card was {card0!r}")
```

- [ ] **Step 2: Run — FAIL** (generic fallback). `cd shared && uv run python3 test_aside_render.py`

- [ ] **Step 3: Implement** — in `_render_own`, before the generic fallback:

```python
    if block.type == "footnote":
        number = block.payload.get("number", "")
        text = block.payload.get("text", "")
        return f"Footnote {number}: {text}." if number else f"Footnote: {text}."
    if block.type == "card":
        title = block.payload.get("title", "")
        publication = block.payload.get("publication", "")
        if publication:
            return f"The author links to a post titled '{title}' from {publication}."
        return f"The author links to a post titled '{title}'."
```

Note: footnote/card `text`/`title` already end without a period in practice; the trailing `.` is intentional. If a footnote `text` ends in `.`, a double period is acceptable (matches the image `Image: ….` template) — do not special-case.

- [ ] **Step 4: All shared tests + lint + types.** `cd shared && for t in test_aside_render test_structural_extract test_describe test_intake_store; do uv run python3 $t.py; done && uv run ruff check . && uv run basedpyright`

- [ ] **Step 5: Commit** — `git commit -m "feat(shared): render footnote + card asides"`

---

### Task 3: Per-type toggle (`drop_types`) + imap wiring

**Files:**
- Modify: `shared/podcast_shared/aside_render.py` (`serialize_flat`)
- Modify: `imap/parse_email.py` (`extract_body_from_html`)
- Test: `shared/test_aside_render.py` (append)

**Interfaces:**
- Changed: `serialize_flat(blocks, *, drop_types: frozenset[str] = frozenset()) -> str` — blocks whose `type` is in `drop_types` are skipped entirely (no text, no aside).
- Changed: imap reads `EMBED_DROP_TYPES` (comma-separated, e.g. `card,footnote`), lowercased/trimmed, and passes it as `drop_types`.

- [ ] **Step 1: Failing test (append + register)**

```python
def test_serialize_flat_drop_types() -> None:
    """drop_types suppresses chosen block types entirely."""
    blocks = [
        Block(type="text", payload={"text": "Body."}),
        Block(type="card", payload={"title": "T", "publication": "P", "href": "u"}),
        Block(type="footnote", payload={"number": "1", "text": "Note."}),
    ]
    out = serialize_flat(blocks, drop_types=frozenset({"card"}))
    if "titled 'T'" in out:
        _fail(f"card should be dropped: {out!r}")
    if "Footnote 1" not in out or "Body." not in out:
        _fail(f"non-dropped content missing: {out!r}")
```

- [ ] **Step 2: Run — FAIL** (`drop_types` unknown kwarg). `cd shared && uv run python3 test_aside_render.py`

- [ ] **Step 3: Implement**

In `serialize_flat`, add the keyword-only param and skip dropped types at the top of the per-block loop:

```python
def serialize_flat(blocks: list[Block], *, drop_types: frozenset[str] = frozenset()) -> str:
    ...
    for block in blocks:
        if block.type in drop_types:
            continue
        ...
```

In `imap/parse_email.py` `extract_body_from_html`, after enrich_images:

```python
    drop_types = frozenset(t.strip().lower() for t in os.environ.get("EMBED_DROP_TYPES", "").split(",") if t.strip())
    body = serialize_flat(blocks, drop_types=drop_types)
```

- [ ] **Step 4: Verify everything**

```bash
cd shared && for t in test_aside_render test_structural_extract test_describe test_intake_store; do uv run python3 $t.py; done && uv run ruff check . && uv run basedpyright
cd ../imap && uv sync -q && uv run ruff check . && uv run basedpyright && uv run python3 test_html_body.py
```

Corpus sanity — footnotes + cards recovered on Colin Gorrie / Arnold Kling:

```bash
cd ../shared && uv run python3 -c "
from pathlib import Path
from collections import Counter
from podcast_shared import find_content_region, extract_blocks
for name in ['colingorrie-substack-com', 'arnoldkling-substack-com']:
    html = next(Path(f'../email-corpus/{name}').glob('*.html')).read_text(errors='replace')
    print(name, Counter(b.type for b in extract_blocks(find_content_region(html))))
"
```
Expected: `footnote` count > 0 for Colin Gorrie; `card` count > 0 for Arnold Kling.

- [ ] **Step 5: Commit** — `git commit -m "feat(shared,imap): per-type embed toggle via EMBED_DROP_TYPES"`

---

## Self-Review
- **Footnote reality:** email HTML has no reference markers → footnotes emitted in document order (last) as numbered asides; documented in the plan and code.
- **Card recovery:** previously dropped; now a one-line announcement; social-chrome meta + CTA ignored by construction (only title/publication/href read).
- **Toggle:** global via env for now; per-source matrix deferred. `drop_types` suppresses both standalone and aside emission because it skips the block before rendering.
- **Walk safety:** `"div"` added to `find_all`, but only card/footnote divs are handled; every other div hits the existing fallthrough `continue`, so no new text is emitted.
- **Auto-live:** no other pipeline edits — imap already serializes at intake.
