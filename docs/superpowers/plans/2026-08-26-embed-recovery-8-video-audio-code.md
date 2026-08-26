# Video / Audio / Code Handlers — Implementation Plan (Plan 8 of the embed-recovery series)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover three more embed types as asides — embedded videos (YouTube/Vimeo/...), embedded audio (Spotify/SoundCloud/podcasts), and code blocks — by extending the extractor and adding render templates. No pipeline edits: intake already runs `extract_blocks → serialize_flat`, so new block types flow to asides automatically.

**Architecture:** `<iframe>` elements are classified by host into `video`/`audio` (unknown hosts skipped as chrome); `<pre>` becomes `code`. Render templates announce each (`"The author shares a video titled '…'."`, etc.); code is announced, not read aloud (reading source aloud is noise). Pure, offline, unit-tested.

**Tech Stack:** Python 3.12, `beautifulsoup4`, ruff, basedpyright. Script-style tests.

**Spec:** `docs/superpowers/specs/2026-08-26-embed-recovery-design.md` (§6 video/audio/code rows).

## Global Constraints

- Line length 120; ruff `ALL`; basedpyright zero errors; no `cast()`.
- Reuse `Block`, `_classes`, `extract_blocks`, `render_block_aside`.
- Unknown/ad iframes (no recognized host) are skipped, not emitted.
- Elements already consumed (inside a tweet/figure) must not double-emit — reuse the existing `consumed`/ancestor guard.

---

### Task 1: Classify + extract iframe embeds (video/audio)

**Files:**
- Modify: `shared/podcast_shared/structural_extract.py`
- Test: `shared/test_structural_extract.py` (append)

**Interfaces:**
- Produces:
  - `iframe_kind(src: str) -> str` — `"video"` for youtube/youtu.be/vimeo/loom/wistia; `"audio"` for spotify/soundcloud/transistor or a `/podcast` path or apple.com podcast; else `""`.
  - `extract_iframe(el: Tag) -> Block | None` — a `video`/`audio` Block `{"title": …, "src": …}` (title from the iframe `title` attr, else ""), or `None` for an unrecognized host.

- [ ] **Step 1: Write the failing test (append + add to run_tests)**

```python
from podcast_shared.structural_extract import extract_iframe, iframe_kind  # noqa: E402


def test_iframe_kind_classifies_hosts() -> None:
    """Hosts map to video/audio; unknown hosts are ''."""
    cases = {
        "https://www.youtube.com/embed/abc": "video",
        "https://player.vimeo.com/video/1": "video",
        "https://open.spotify.com/embed/x": "audio",
        "https://w.soundcloud.com/player/?url=y": "audio",
        "https://ads.example.com/widget": "",
    }
    for src, kind in cases.items():
        if iframe_kind(src) != kind:
            _fail(f"iframe_kind({src!r}) = {iframe_kind(src)!r}, expected {kind!r}")


def test_extract_iframe_builds_block() -> None:
    """A YouTube iframe with a title yields a video block; ads yield None."""
    from bs4 import BeautifulSoup

    yt = BeautifulSoup('<iframe src="https://youtube.com/embed/x" title="My Talk"></iframe>', "html.parser").find(
        "iframe"
    )
    if not isinstance(yt, Tag):
        _fail("no iframe")
    block = extract_iframe(yt)
    if block is None or block.type != "video" or block.payload.get("title") != "My Talk":
        _fail(f"video block wrong: {block!r}")
    ad = BeautifulSoup('<iframe src="https://ads.x/w"></iframe>', "html.parser").find("iframe")
    if isinstance(ad, Tag) and extract_iframe(ad) is not None:
        _fail("ad iframe should be None")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run python3 test_structural_extract.py`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement**

Add to `structural_extract.py`:

```python
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "loom.com", "wistia.")
_AUDIO_HOSTS = ("spotify.com", "soundcloud.com", "transistor.fm")


def iframe_kind(src: str) -> str:
    """Classify an iframe ``src`` into ``"video"``/``"audio"``/``""``.

    Returns:
        The embed kind, or "" for an unrecognized host.

    """
    low = src.lower()
    if any(host in low for host in _VIDEO_HOSTS):
        return "video"
    if any(host in low for host in _AUDIO_HOSTS) or "/podcast" in low or "apple.com" in low and "podcast" in low:
        return "audio"
    return ""


def extract_iframe(el: Tag) -> Block | None:
    """Extract a video/audio iframe embed into a Block, or None for unknown hosts.

    Returns:
        A ``video``/``audio`` Block, or None.

    """
    src = str(el.get("src") or "")
    kind = iframe_kind(src)
    if not kind:
        return None
    title = str(el.get("title") or "").strip()
    return Block(type=kind, payload={"title": title, "src": src})
```

- [ ] **Step 4: Run tests + lint + types**

Run: `cd shared && uv run python3 test_structural_extract.py && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors. (If the `apple.com`/`podcast` boolean precedence reads oddly to ruff, parenthesize: `("apple.com" in low and "podcast" in low)`.)

- [ ] **Step 5: Commit**

```bash
git add shared/podcast_shared/structural_extract.py shared/test_structural_extract.py
git commit -m "feat(shared): classify + extract video/audio iframe embeds"
```

---

### Task 2: Walk iframes + `<pre>` in extract_blocks

**Files:**
- Modify: `shared/podcast_shared/structural_extract.py` (`extract_blocks`)
- Test: `shared/test_structural_extract.py` (append)

**Interfaces:**
- Changed: `extract_blocks` also walks `iframe` (→ `extract_iframe`, skipping `None`) and `pre` (→ `Block("code", {"text": <code text>})`), in document order, respecting the `consumed` guard.

- [ ] **Step 1: Write the failing test (append + run_tests)**

```python
def test_extract_blocks_video_and_code() -> None:
    """An iframe becomes a video block and a <pre> becomes a code block, in order."""
    html = (
        '<div class="body markup">'
        "<p>Watch this.</p>"
        '<iframe src="https://youtube.com/embed/x" title="Talk"></iframe>'
        "<pre>print(42)</pre>"
        '<iframe src="https://ads.x/w"></iframe>'
        "</div>"
    )
    blocks = extract_blocks(find_content_region(html))
    kinds = [b.type for b in blocks]
    if kinds != ["text", "video", "code"]:
        _fail(f"kinds were {kinds}")
    if blocks[1].payload.get("title") != "Talk":
        _fail(f"video title wrong: {blocks[1].payload}")
    if blocks[2].payload.get("text") != "print(42)":
        _fail(f"code text wrong: {blocks[2].payload}")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run python3 test_structural_extract.py`
Expected: FAIL — iframe/pre not walked.

- [ ] **Step 3: Implement**

In `extract_blocks`, extend the `find_all` set to include `"iframe"` and `"pre"`, and add dispatch before the text handling:

```python
    for el in region.find_all((*_TEXT_TAGS, "table", "figure", "img", "iframe", "pre")):
        if any(id(ancestor) in consumed for ancestor in el.parents):
            continue
        if is_tweet(el):
            ...
        if el.name == "figure":
            ...
        if el.name == "img":
            ...
        if el.name == "iframe":
            embed = extract_iframe(el)
            if embed is not None:
                blocks.append(embed)
                previous_text = None
            continue
        if el.name == "pre":
            code_text = el.get_text("\n").strip()
            if code_text:
                blocks.append(Block(type="code", payload={"text": code_text}))
                previous_text = None
            continue
        if el.name not in _TEXT_TAGS:
            continue
```

- [ ] **Step 4: Run tests + lint + types**

Run: `cd shared && uv run python3 test_structural_extract.py && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors.

- [ ] **Step 5: Commit**

```bash
git add shared/podcast_shared/structural_extract.py shared/test_structural_extract.py
git commit -m "feat(shared): walk iframe (video/audio) and pre (code) in extract_blocks"
```

---

### Task 3: Render templates for video / audio / code

**Files:**
- Modify: `shared/podcast_shared/aside_render.py` (`_render_own`)
- Test: `shared/test_aside_render.py` (append)

**Interfaces:**
- Changed: `_render_own` handles `video`, `audio`, `code`:
  - `video`: `"The author shares a video titled '<title>'."` (drop `titled '…'` when title empty → `"The author shares a video."`).
  - `audio`: same shape with "an audio clip".
  - `code`: `"The author includes a code block."` (announced, not read aloud).

- [ ] **Step 1: Write the failing test (append + run_tests)**

```python
def test_render_video_audio_code() -> None:
    """Video/audio/code render as concise announced asides."""
    v = render_block_aside(Block(type="video", payload={"title": "My Talk", "src": "u"}))
    if v != "The author shares a video titled 'My Talk'.":
        _fail(f"video aside was {v!r}")
    v2 = render_block_aside(Block(type="video", payload={"title": "", "src": "u"}))
    if v2 != "The author shares a video.":
        _fail(f"titleless video aside was {v2!r}")
    a = render_block_aside(Block(type="audio", payload={"title": "Ep 12", "src": "u"}))
    if a != "The author shares an audio clip titled 'Ep 12'.":
        _fail(f"audio aside was {a!r}")
    c = render_block_aside(Block(type="code", payload={"text": "print(1)"}))
    if c != "The author includes a code block.":
        _fail(f"code aside was {c!r}")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run python3 test_aside_render.py`
Expected: FAIL — these currently hit the generic fallback.

- [ ] **Step 3: Implement**

In `aside_render.py` `_render_own`, add branches before the generic fallback:

```python
    if block.type in {"video", "audio"}:
        noun = "a video" if block.type == "video" else "an audio clip"
        title = block.payload.get("title", "")
        if title:
            return f"The author shares {noun} titled '{title}'."
        return f"The author shares {noun}."
    if block.type == "code":
        return "The author includes a code block."
```

- [ ] **Step 4: Run ALL shared tests + lint + types + corpus check**

Run: `cd shared && for t in test_aside_render test_structural_extract test_describe test_intake_store; do uv run python3 $t.py; done && uv run ruff check . && uv run basedpyright`
Expected: PASS; clean; zero errors.

Corpus sanity (garbageday is embed-dense):

```bash
cd shared && uv run python3 -c "
from pathlib import Path
from collections import Counter
from podcast_shared import find_content_region, extract_blocks
html = next(Path('../email-corpus/hi-www-garbageday-email').glob('*.html')).read_text(errors='replace')
print(Counter(b.type for b in extract_blocks(find_content_region(html))))
"
```
Expected: a Counter that may include `video`/`audio` alongside text/image.

- [ ] **Step 5: Commit**

```bash
git add shared/podcast_shared/aside_render.py shared/test_aside_render.py
git commit -m "feat(shared): render video/audio/code asides"
```

---

## Self-Review

- **Spec coverage:** §6 video/audio/code rows (mechanical). Cards (div-container) and Matt-Levine footnotes (reference→definition inline) are deferred to their own plan; LLM fallback + toggles later.
- **Auto-live:** no pipeline edits — imap already runs `extract_blocks → serialize_flat`, so the new block types render as asides immediately (aside voice on WaveNet).
- **Types:** `extract_iframe` returns `Block | None`; callers guard `None`; new payload keys (`title`/`src`/`text`) match the render templates.
- **Placeholders:** none — runnable code or exact commands throughout.
