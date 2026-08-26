# Image Vision Description — Implementation Plan (Plan 7 of the embed-recovery series)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Describe content images with a vision model so an image aside says what the picture shows, not just its caption/alt — layered on top of any alt/caption, applied at imap intake, gracefully degrading when disabled or unavailable.

**Architecture:** A `describe.py` provider module: `describe_image(src, alt, caption)` calls the OpenAI Responses API with the image URL (GPT vision; Substack serves plain URLs, so no fetching) and returns one concise sentence, or `""` on any failure/no key. `enrich_images(blocks, describer)` walks the block tree and fills each image block's `description` payload via an injected describer (pure + unit-testable). imap calls `enrich_images(blocks, describe_image)` after `extract_blocks`, gated by `EMBED_VISION`. `render_block_aside` already prefers `description` > `caption` > `alt`, so enriched text flows through with no render change.

**Tech Stack:** Python 3.12, `openai` (add to `shared`), ruff, basedpyright. Script-style tests (vision call mocked / no-network).

**Spec:** `docs/superpowers/specs/2026-08-26-embed-recovery-design.md` (§6 image row "vision description"; §8 provider — GPT-5.6 default).

## Global Constraints

- Line length 120; ruff `ALL`; basedpyright zero errors; no `cast()`.
- Vision is **opt-outable and degrades safely**: no `OPENAI_API_KEY` or `EMBED_VISION=0` → no API calls, asides fall back to caption/alt. Never let a vision failure break intake.
- The pure extractor stays offline; the network lives only in `describe_image`, injected into `enrich_images`.
- `openai>=1.60.0` (same floor as `archive`) added to `shared`.

---

### Task 1: `describe.py` — `describe_image` + `enrich_images`

**Files:**
- Create: `shared/podcast_shared/describe.py`
- Modify: `shared/pyproject.toml` (add `openai>=1.60.0`)
- Modify: `shared/podcast_shared/__init__.py` (export `describe_image`, `enrich_images`)
- Test: `shared/test_describe.py`

**Interfaces:**
- Produces:
  - `Describer = Callable[[str, str, str], str]` — `(src, alt, caption) -> description`.
  - `describe_image(src: str, alt: str = "", caption: str = "") -> str` — vision description via OpenAI Responses API; `""` when `src` is empty, `OPENAI_API_KEY` unset, or the call fails.
  - `enrich_images(blocks: list[Block], describer: Describer) -> None` — in place, sets `payload["description"]` on every `image` block (recursively) that has a `src` and no existing non-empty `description`.

- [ ] **Step 1: Add openai dependency**

Add `"openai>=1.60.0"` to `shared/pyproject.toml` `dependencies`; then `cd shared && uv sync`.

- [ ] **Step 2: Write the failing test**

Create `shared/test_describe.py`:

```python
"""Tests for image enrichment (script-style; the vision call is not exercised)."""

import logging

from podcast_shared.describe import describe_image, enrich_images
from podcast_shared.structural_extract import Block

logging.basicConfig(level=logging.INFO)


def _fail(msg: str) -> None:
    """Raise an AssertionError.

    Raises:
        AssertionError: Always.

    """
    raise AssertionError(msg)


def _fake_describer(src: str, alt: str, caption: str) -> str:
    return f"desc<{src}|{alt}|{caption}>"


def test_enrich_sets_description_on_images() -> None:
    """Every image block (incl. nested) gets a description from the describer."""
    blocks = [
        Block(type="text", payload={"text": "hi"}),
        Block(type="image", payload={"alt": "a", "caption": "c", "src": "u1"}),
        Block(
            type="tweet",
            payload={"handle": "@x", "text": "t"},
            children=[Block(type="image", payload={"alt": "", "caption": "", "src": "u2"})],
        ),
    ]
    enrich_images(blocks, _fake_describer)
    if blocks[1].payload.get("description") != "desc<u1|a|c>":
        _fail(f"top image not enriched: {blocks[1].payload}")
    if blocks[2].children[0].payload.get("description") != "desc<u2||>":
        _fail(f"nested image not enriched: {blocks[2].children[0].payload}")


def test_enrich_skips_existing_and_srcless() -> None:
    """Images with a description already, or no src, are left alone."""
    blocks = [
        Block(type="image", payload={"src": "u", "description": "kept"}),
        Block(type="image", payload={"alt": "a"}),  # no src
    ]
    enrich_images(blocks, _fake_describer)
    if blocks[0].payload.get("description") != "kept":
        _fail("existing description overwritten")
    if blocks[1].payload.get("description"):
        _fail("srcless image got a description")


def test_describe_image_no_key_returns_empty(monkeypatch_env: None = None) -> None:
    """Without OPENAI_API_KEY, describe_image returns '' (no network)."""
    import os

    saved = os.environ.pop("OPENAI_API_KEY", None)
    try:
        if describe_image("http://x/img.png", "alt", "cap") != "":
            _fail("expected empty description without API key")
    finally:
        if saved is not None:
            os.environ["OPENAI_API_KEY"] = saved


def run_tests() -> None:
    """Run enrichment tests."""
    test_enrich_sets_description_on_images()
    test_enrich_skips_existing_and_srcless()
    test_describe_image_no_key_returns_empty()
    logging.info("describe/enrich tests passed")


if __name__ == "__main__":
    run_tests()
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd shared && uv run python3 test_describe.py`
Expected: FAIL — `ModuleNotFoundError: podcast_shared.describe`.

- [ ] **Step 4: Implement**

Create `shared/podcast_shared/describe.py`:

```python
"""Vision description of content images, layered on top of alt/caption.

The network lives here (OpenAI Responses API vision) and is injected into
``enrich_images`` so the extractor stays pure. Degrades to "" whenever vision is
unavailable, so intake never breaks on a failed or disabled describe.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from openai import OpenAI, OpenAIError

from podcast_shared.structural_extract import Block

Describer = Callable[[str, str, str], str]

VISION_MODEL = os.environ.get("EMBED_VISION_MODEL", "gpt-5.6")

_PROMPT = (
    "Describe this image for a podcast listener in one concise sentence. State what it "
    "shows; do not start with 'The image' or 'This image'. If it is a chart, give the "
    "headline finding."
)


def describe_image(src: str, alt: str = "", caption: str = "") -> str:
    """Return a one-sentence vision description of an image URL, or '' on any failure."""
    if not src:
        return ""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return ""
    hint = f" Caption: {caption}." if caption else f" Alt text: {alt}." if alt else ""
    try:
        client = OpenAI(api_key=key, max_retries=3)
        response = client.responses.create(
            model=VISION_MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": _PROMPT + hint},
                        {"type": "input_image", "image_url": src},
                    ],
                }
            ],
            timeout=120,
            prompt_cache_options={"mode": "explicit"},
        )
    except OpenAIError:
        logging.exception("Vision description failed for %s", src)
        return ""
    return response.output_text.strip()


def enrich_images(blocks: list[Block], describer: Describer) -> None:
    """Fill each image block's ``description`` payload via ``describer`` (in place, recursive)."""
    for block in blocks:
        if block.type == "image" and block.payload.get("src") and not block.payload.get("description"):
            block.payload["description"] = describer(
                block.payload.get("src", ""),
                block.payload.get("alt", ""),
                block.payload.get("caption", ""),
            )
        if block.children:
            enrich_images(block.children, describer)
```

Add explicit re-exports to `shared/podcast_shared/__init__.py`:

```python
from podcast_shared.describe import describe_image as describe_image
from podcast_shared.describe import enrich_images as enrich_images
```

Note: the test param `monkeypatch_env` is a throwaway default arg to keep the function zero-arg for the runner; if ruff flags it (ARG001/unused), drop the param and inline the env save/restore.

- [ ] **Step 5: Run tests + lint + types**

Run: `cd shared && uv run python3 test_describe.py && uv run python3 test_structural_extract.py && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors. If the OpenAI `input=[...]` dict trips basedpyright typing, adjust to the SDK's typed params or a minimal `# pyright: ignore[reportArgumentType]` at that single call (boundary, mirrors repo convention) — prefer typed if feasible.

- [ ] **Step 6: Commit**

```bash
git add shared/podcast_shared/describe.py shared/podcast_shared/__init__.py shared/test_describe.py shared/pyproject.toml shared/uv.lock
git commit -m "feat(shared): image vision description (describe_image + enrich_images)"
```

---

### Task 2: Wire vision enrichment into imap intake (gated)

**Files:**
- Modify: `imap/parse_email.py` (`extract_body_from_html`; imports)

**Interfaces:**
- Changed: `extract_body_from_html` runs `enrich_images(blocks, describe_image)` between `extract_blocks` and `serialize_flat`, only when `EMBED_VISION` is not `"0"`. With vision off/unavailable, image asides fall back to caption/alt exactly as in Plan 6.

- [ ] **Step 1: Import the helpers**

Add `describe_image`, `enrich_images` to the `from podcast_shared import (...)` block in `imap/parse_email.py`.

- [ ] **Step 2: Enrich before serialising**

Update `extract_body_from_html`:

```python
def extract_body_from_html(msg: MailMessage) -> str | None:
    """Extract flat marker body text (narration + quotes + embed asides) from the email HTML.

    Content images are vision-described (layered on alt/caption) unless EMBED_VISION=0
    or no OPENAI_API_KEY is set, in which case asides fall back to caption/alt.

    Returns:
        The serialized body text, or None when there is no HTML or no extractable content.

    """
    if not msg.html:
        return None
    blocks = extract_blocks(find_content_region(msg.html))
    if os.environ.get("EMBED_VISION", "1") != "0":
        enrich_images(blocks, describe_image)
    body = serialize_flat(blocks)
    return body or None
```

Ensure `import os` is present at the top of `parse_email.py` (it is used elsewhere; confirm).

- [ ] **Step 3: Lint + types + existing tests**

Run: `cd imap && uv run ruff check . && uv run basedpyright && uv run python3 test_html_body.py`
Expected: clean; zero errors; HTML-body tests pass (they exercise `extract_body_text`, unaffected).

- [ ] **Step 4: Offline gate check (no network)**

Run:

```bash
cd imap && EMBED_VISION=0 uv run python3 -c "
from pathlib import Path
from podcast_shared import find_content_region, extract_blocks, serialize_flat, enrich_images, describe_image
import os
html = Path('../email-corpus/maangchi-substack-com').glob('*.html').__next__().read_text(errors='replace')
blocks = extract_blocks(find_content_region(html))
# EMBED_VISION=0 path: no enrichment
body = serialize_flat(blocks)
print('image asides fall back to caption/alt:', 'Image:' in body)
"
```
Expected: prints `True` (images still produce asides from caption/alt with vision off).

- [ ] **Step 5: Commit**

```bash
git add imap/parse_email.py
git commit -m "feat(imap): vision-describe content images at intake (gated by EMBED_VISION)"
```

---

## Self-Review

- **Spec coverage:** §6 image vision description layered on alt/caption (T1); §8 OpenAI provider default (GPT-5.6). Applied at extraction/intake time per the Plan 5 architecture pivot. Gemini vision alternate and the toggle matrix remain later work.
- **Safety:** vision is gated (`EMBED_VISION`) and degrades to `""` without a key or on error; the pure extractor is untouched (network injected); intake never breaks on a vision failure.
- **Types:** `Describer` alias shared by `enrich_images` and its tests; `enrich_images` mutates payload dicts (str→str) in place; the only external-typing risk is the OpenAI `input` param (handle per Step 5).
- **Placeholders:** none — runnable code or exact commands throughout.
