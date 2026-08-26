# Raw-HTML Store at Intake — Implementation Plan (Plan 1 of the embed-recovery series)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist each intake path's raw source HTML to a gitignored on-disk store, keyed to the same episode identity as the text output, so the back catalog can later be re-processed by an improved extractor.

**Architecture:** A small `shared/podcast_shared` helper writes `<store>/<source-slug>/<episode-id>.html` plus a `.meta.json` sidecar. Each intake script (imap email + link, rss content + full_scraper, archive) calls it at the point it already has HTML in hand. Two scraper helpers are lightly refactored to surface the raw HTML they currently discard. No behavior changes to existing text output.

**Tech Stack:** Python 3.12, uv per-subproject, pytest, ruff, basedpyright. `imap_tools`, `trafilatura`, `BeautifulSoup` already present.

**Spec:** `docs/superpowers/specs/2026-08-26-embed-recovery-design.md` (§4 Raw-HTML store; this plan is spec phase 1).

## Global Constraints

- Line length 120; ruff "ALL" rules per root config; basedpyright `typeCheckingMode = "all"`, **zero errors**. Never use `cast()` — narrow with `isinstance`/`str()`/`getattr()`. No pyright-ignore comments.
- `@dataclass(slots=True)` (not frozen) for dataclasses; plain dicts/lists.
- Paths are **relative to the subproject CWD** (scripts run from `imap/`, `rss/`, `archive/`). The store root is `../intake-html`, mirroring `output_folder = "../prepare-text/text-input-raw"`. Do **not** derive paths from `__file__` — `podcast-shared` is installed copied, not editable.
- `shared` is imported as `podcast_shared` and installed into each subproject via `[tool.uv.sources] podcast-shared = { path = "../shared" }`. After editing `shared`, dependents pick it up on their next `uv sync`.
- Store is gitignored; it holds private content.
- Use `shutil.copy2` semantics for timestamp preservation where copying (n/a here).

---

### Task 1: `store_intake_html` helper in `shared` (+ gitignore)

**Files:**
- Create: `shared/podcast_shared/intake_store.py`
- Modify: `shared/podcast_shared/__init__.py` (re-export)
- Modify: `shared/pyproject.toml` (ensure `pytest` in `[dependency-groups] dev`)
- Modify: `.gitignore` (add `/intake-html/`)
- Test: `shared/test_intake_store.py`

**Interfaces:**
- Produces:
  - `slug_source(source: str) -> str` — filesystem-safe slug (lowercase, non-alnum→`-`, trimmed, ≤80 chars, `"unknown"` when empty).
  - `store_intake_html(*, source: str, episode_id: str, html: str, url: str, intake_type: str, store_root: pathlib.Path = pathlib.Path("../intake-html")) -> pathlib.Path` — writes `<store_root>/<slug>/<episode_id>.html` and `<episode_id>.meta.json`; returns the HTML path. No-ops (returns the path without writing) when `html` is empty/whitespace. Overwrites idempotently. Creates parent dirs.

- [ ] **Step 1: Ensure pytest is available in `shared`**

Confirm `shared/pyproject.toml` `[dependency-groups] dev` contains `"pytest"`. If absent, add it, then:

Run: `cd shared && uv sync`
Expected: resolves with pytest installed.

- [ ] **Step 2: Write the failing test**

Create `shared/test_intake_store.py`:

```python
import json

from podcast_shared.intake_store import slug_source, store_intake_html


def test_slug_source_normalizes():
    assert slug_source("Astral Codex Ten") == "astral-codex-ten"
    assert slug_source("hi@www.garbageday.email") == "hi-www-garbageday-email"
    assert slug_source("") == "unknown"


def test_store_writes_html_and_meta(tmp_path):
    html_path = store_intake_html(
        source="Astral Codex Ten",
        episode_id="20260730-010716",
        html="<html><body><p>hi</p></body></html>",
        url="https://example.com/p/x",
        intake_type="email",
        store_root=tmp_path,
    )
    assert html_path == tmp_path / "astral-codex-ten" / "20260730-010716.html"
    assert html_path.read_text(encoding="utf-8").startswith("<html>")
    meta = json.loads((tmp_path / "astral-codex-ten" / "20260730-010716.meta.json").read_text())
    assert meta["source"] == "Astral Codex Ten"
    assert meta["url"] == "https://example.com/p/x"
    assert meta["intake_type"] == "email"
    assert meta["episode_id"] == "20260730-010716"


def test_store_skips_empty_html(tmp_path):
    html_path = store_intake_html(
        source="x", episode_id="1", html="   ", url="u", intake_type="email", store_root=tmp_path,
    )
    assert not html_path.exists()


def test_store_overwrites_idempotently(tmp_path):
    for body in ("<p>a</p>", "<p>b</p>"):
        store_intake_html(source="s", episode_id="1", html=body, url="u", intake_type="rss", store_root=tmp_path)
    assert (tmp_path / "s" / "1.html").read_text(encoding="utf-8") == "<p>b</p>"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd shared && uv run pytest test_intake_store.py -v`
Expected: FAIL — `ModuleNotFoundError: podcast_shared.intake_store`.

- [ ] **Step 4: Write minimal implementation**

Create `shared/podcast_shared/intake_store.py`:

```python
"""Persist raw source HTML at intake, keyed to episode identity, for later backfill.

Paths are relative to the subproject CWD (imap/ rss/ archive/), matching the
existing pipeline convention (e.g. ../prepare-text/text-input-raw). The store is
gitignored; it holds private source content.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_STORE_ROOT = Path("../intake-html")


def slug_source(source: str) -> str:
    """Return a filesystem-safe slug for a source/sender name."""
    slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
    return slug[:80] or "unknown"


def store_intake_html(
    *,
    source: str,
    episode_id: str,
    html: str,
    url: str,
    intake_type: str,
    store_root: Path = DEFAULT_STORE_ROOT,
) -> Path:
    """Write raw HTML + a metadata sidecar for one intake item; return the HTML path.

    No-ops (still returns the intended path) when ``html`` is empty or whitespace.
    Overwrites idempotently and creates parent directories.
    """
    html_path = store_root / slug_source(source) / f"{episode_id}.html"
    if not html.strip():
        return html_path
    html_path.parent.mkdir(parents=True, exist_ok=True)
    _ = html_path.write_text(html, encoding="utf-8")
    meta = {
        "source": source,
        "episode_id": episode_id,
        "url": url,
        "intake_type": intake_type,
        "stored_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
    }
    _ = html_path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return html_path
```

Add to `shared/podcast_shared/__init__.py` (with the other imports/exports; keep alphabetical if the file is ordered):

```python
from podcast_shared.intake_store import slug_source, store_intake_html
```

If `__init__.py` defines `__all__`, add `"slug_source"` and `"store_intake_html"`.

- [ ] **Step 5: Add gitignore entry**

Add to `.gitignore` (near the other data dirs):

```
/intake-html/
```

- [ ] **Step 6: Run tests + lint + types**

Run: `cd shared && uv run pytest test_intake_store.py -v && uv run ruff check . && uv run basedpyright`
Expected: tests PASS; ruff clean; basedpyright zero errors.

- [ ] **Step 7: Commit**

```bash
git add shared/podcast_shared/intake_store.py shared/podcast_shared/__init__.py shared/test_intake_store.py shared/pyproject.toml .gitignore
git commit -m "feat(shared): raw-HTML intake store helper"
```

---

### Task 2: Wire imap email intake

**Files:**
- Modify: `imap/parse_email.py` (email branch, around the `META_INTAKE_TYPE: email` write, ~line 487; `date_stamp` at ~425)

**Interfaces:**
- Consumes: `store_intake_html` (Task 1). Available data at the write site: `msg.html` (raw email HTML part), `from_name_raw`, `source_url`, `date_stamp`.

- [ ] **Step 1: Import the helper**

At the top of `imap/parse_email.py`, extend the existing `from podcast_shared import ...` line to include `store_intake_html`.

- [ ] **Step 2: Call the store right after the email text is written**

Immediately after:

```python
                    _ = pathlib.Path(output_filename).write_text(
                        metadata_block + "\n\n" + email_text_raw, encoding="utf-8"
                    )
```

add:

```python
                    _ = store_intake_html(
                        source=from_name_raw,
                        episode_id=date_stamp,
                        html=msg.html or "",
                        url=source_url,
                        intake_type="email",
                    )
```

- [ ] **Step 3: Smoke-verify against a saved corpus email**

Run (uses an already-saved corpus HTML to exercise the helper end-to-end without IMAP):

```bash
cd imap && uv run python3 -c "
from podcast_shared import store_intake_html
from pathlib import Path
h = next(Path('../email-corpus/astralcodexten-substack-com').glob('*.html')).read_text()
p = store_intake_html(source='Astral Codex Ten', episode_id='smoketest', html=h, url='u', intake_type='email', store_root=Path('/tmp/intake-html-smoke'))
print('wrote', p, p.exists())
"
```
Expected: prints `wrote /tmp/intake-html-smoke/astral-codex-ten/smoketest.html True`.

- [ ] **Step 4: Lint + types**

Run: `cd imap && uv run ruff check . && uv run basedpyright`
Expected: clean; zero errors.

- [ ] **Step 5: Commit**

```bash
git add imap/parse_email.py
git commit -m "feat(imap): store raw email HTML at intake"
```

---

### Task 3: Wire archive intake

**Files:**
- Modify: `archive/check-archive.py` (~line 107–127; `html_content = fetch_post_html(url)` already holds raw HTML; `date_stamp`, `source_name`, `url`)

**Interfaces:**
- Consumes: `store_intake_html` (Task 1). Data: `html_content`, `source_name`, `url`, `date_stamp`.

- [ ] **Step 1: Import the helper**

Extend the existing `from podcast_shared import ...` in `archive/check-archive.py` to include `store_intake_html`.

- [ ] **Step 2: Call the store right after the article text is written**

Immediately after:

```python
    _ = pathlib.Path(output_filename).write_text(metadata_block + "\n\n" + content_text, encoding="utf-8")
```

add:

```python
    _ = store_intake_html(
        source=source_name,
        episode_id=date_stamp,
        html=html_content,
        url=url,
        intake_type="archive",
    )
```

- [ ] **Step 3: Lint + types**

Run: `cd archive && uv run ruff check . && uv run basedpyright`
Expected: clean; zero errors.

- [ ] **Step 4: Commit**

```bash
git add archive/check-archive.py
git commit -m "feat(archive): store raw post HTML at intake"
```

---

### Task 4: Wire rss content-mode intake

**Files:**
- Modify: `rss/check-rss.py` (~line 382–399; content mode has `content_value`; `feed_title_raw`, `original_url`, `date_stamp`)

**Interfaces:**
- Consumes: `store_intake_html` (Task 1). Data in content mode: `content_value` (the `entry.content[0].value` HTML). description mode has no HTML (skip). full_scraper handled in Task 6.

- [ ] **Step 1: Import the helper**

Extend the existing `from podcast_shared import ...` in `rss/check-rss.py` to include `store_intake_html`.

- [ ] **Step 2: Capture the HTML fragment for storage in content mode**

In the `else` (content) branch, the code currently is:

```python
                else:
                    content_list: list[object] = getattr(parsed_feed_entry, "content", [])
                    if content_list:
                        content_value: str = str(getattr(content_list[0], "value", ""))
                        soup = BeautifulSoup(content_value, "html.parser")
                        content_text = soup.get_text()
                    else:
                        content_text = str(getattr(parsed_feed_entry, "summary", "") or "")
```

Introduce a `raw_html` variable initialized to `""` before the `if feed.mode` chain, and set it to `content_value` in the content branch:

```python
                raw_html = ""
                content_text: str
                if feed.mode == "description":
                    ...
                elif feed.mode == "full_scraper":
                    ...
                else:
                    content_list: list[object] = getattr(parsed_feed_entry, "content", [])
                    if content_list:
                        content_value: str = str(getattr(content_list[0], "value", ""))
                        raw_html = content_value
                        soup = BeautifulSoup(content_value, "html.parser")
                        content_text = soup.get_text()
                    else:
                        content_text = str(getattr(parsed_feed_entry, "summary", "") or "")
```

- [ ] **Step 3: Call the store right after the text is written**

Immediately after:

```python
                _ = pathlib.Path(output_filename).write_text(metadata_block + "\n\n" + content_text, encoding="utf-8")
```

add:

```python
                _ = store_intake_html(
                    source=feed_title_raw,
                    episode_id=date_stamp,
                    html=raw_html,
                    url=original_url,
                    intake_type="rss",
                )
```

(Empty `raw_html` for description mode is a no-op by design.)

- [ ] **Step 4: Lint + types**

Run: `cd rss && uv run ruff check . && uv run basedpyright`
Expected: clean; zero errors.

- [ ] **Step 5: Commit**

```bash
git add rss/check-rss.py
git commit -m "feat(rss): store raw content HTML at intake (content mode)"
```

---

### Task 5: Surface + store raw HTML from imap link scraper

**Files:**
- Modify: `imap/parse_email.py` (`fetch_and_process_html` ~line 342–407, and the link branch ~line 530–556)

**Interfaces:**
- `fetch_and_process_html` currently returns `(trafilatura_result, content_text)`. It fetches raw page HTML internally as `html_content` (~line 396 area). Change it to also return that raw HTML.
- Produces: `fetch_and_process_html(...) -> tuple[object | None, str | None, str | None]` where the new third element is the raw page HTML (or `None` on failure).
- Consumes: `store_intake_html` (Task 1).

- [ ] **Step 1: Return raw HTML from `fetch_and_process_html`**

In `fetch_and_process_html`, locate the local raw page HTML (the string passed to `bare_extraction`/`extract`, e.g. `html_content`). Change the success return to include it as a third element, and the failure/`None` returns to yield `(None, None, None)`. Update the function's return type annotation to `tuple[object | None, str | None, str | None]` and its docstring.

- [ ] **Step 2: Update the link-branch call site**

Change:

```python
                    html_content_parsed_for_title, webpage_text = fetch_and_process_html(
                        url=scraper_url,
                        request_body={"url": original_url},
                    )
```

to unpack three values:

```python
                    html_content_parsed_for_title, webpage_text, raw_page_html = fetch_and_process_html(
                        url=scraper_url,
                        request_body={"url": original_url},
                    )
```

- [ ] **Step 3: Store after the link text is written**

Immediately after the link-branch `write_text(... webpage_text ...)`, add:

```python
                    _ = store_intake_html(
                        source=from_name_raw,
                        episode_id=date_stamp,
                        html=raw_page_html or "",
                        url=original_url,
                        intake_type="link",
                    )
```

- [ ] **Step 4: Confirm no other caller of `fetch_and_process_html` breaks**

Run: `cd imap && grep -n "fetch_and_process_html" parse_email.py`
Expected: only the definition and the one call site (now 3-tuple). If any other caller exists, update it to the 3-tuple.

- [ ] **Step 5: Lint + types**

Run: `cd imap && uv run ruff check . && uv run basedpyright`
Expected: clean; zero errors.

- [ ] **Step 6: Commit**

```bash
git add imap/parse_email.py
git commit -m "feat(imap): surface and store raw page HTML for link intake"
```

---

### Task 6: Surface + store raw HTML from rss full_scraper

**Files:**
- Modify: `rss/check-rss.py` (`ScrapeResult` dataclass ~line 146–156; `fetch_full_article` ~line 184–241; full_scraper branch ~line 371–380)

**Interfaces:**
- `ScrapeResult` currently carries `content: str | None` and `error: str | None`. Add `raw_html: str | None = None`.
- `fetch_full_article` fetches the page HTML internally as `html_content` (~line 198). On success, set `raw_html=html_content` on the returned `ScrapeResult`.
- Consumes: `store_intake_html` (Task 1); the `raw_html` variable introduced in Task 4.

- [ ] **Step 1: Add `raw_html` to `ScrapeResult`**

In the `@dataclass(slots=True)` `ScrapeResult`, add:

```python
    raw_html: str | None = None
```

- [ ] **Step 2: Populate it on success in `fetch_full_article`**

Change the success return `return ScrapeResult(content=content_text)` to:

```python
    return ScrapeResult(content=content_text, raw_html=html_content)
```

- [ ] **Step 3: Feed it into `raw_html` in the full_scraper branch**

In the `elif feed.mode == "full_scraper":` branch, after `content_text = scrape.content`, add:

```python
                    raw_html = scrape.raw_html or ""
```

(This reuses the `raw_html` variable from Task 4; the existing `store_intake_html` call at the end of the loop now covers full_scraper too.)

- [ ] **Step 4: Lint + types**

Run: `cd rss && uv run ruff check . && uv run basedpyright`
Expected: clean; zero errors.

- [ ] **Step 5: Commit**

```bash
git add rss/check-rss.py
git commit -m "feat(rss): surface and store raw page HTML for full_scraper mode"
```

---

## Self-Review

- **Spec coverage (§4):** store location/naming (Task 1), keyed to episode id (all tasks use `date_stamp`), written by imap email (T2), archive (T3), rss content (T4), imap link (T5), rss full_scraper (T6), `.meta.json` sidecar (T1), gitignore (T1). description-mode RSS has no HTML by nature — correctly a no-op. Covered.
- **Not in this plan (later plans):** the structural extractor, handlers, provider, rendering, toggles, backfill, NUL fix. Intentional — Plan 1 is the substrate only.
- **Types:** `store_intake_html` keyword-only signature is used identically at every call site; `fetch_and_process_html` return arity change (Task 5) has its sole caller updated; `ScrapeResult.raw_html` optional default keeps existing constructions valid.
- **Placeholders:** none — every step has concrete code or an exact command.
