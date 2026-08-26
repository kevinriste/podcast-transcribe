# Embed Recovery — Design Spec

Status: Draft for review • Date: 2026-08-26 • Branch: `feat/embed-recovery`

> Feature-branch design doc. Per `CLAUDE.md`, this file must be deleted before
> merging to `main`. Companion scratch notes (with the raw audit data) live in the
> gitignored `EMBED-RECOVERY-NOTES.md` at repo root.

## 1. Problem

Every "clean" text extractor in the pipeline discards content that a visual web
reader would see: embedded tweets, images/charts, YouTube/other videos, footnotes,
embedded post cards, audio/podcast embeds, and code blocks. Verified across the
whole intake surface:

| Path | Extractor | Embeds |
|---|---|---|
| imap HTML (Substack/Beehiiv) | `extract_body_text` block-walk (`p/li/blockquote/h1-h4`) | dropped |
| imap link | trafilatura | dropped |
| rss `full_scraper` | trafilatura | dropped |
| archive | trafilatura | dropped |
| rss `content` | flat `get_text()` | kept but mangled (word-joined + engagement junk) |

trafilatura is a boilerplate remover by design; its structured outputs
(`markdown`/`xml`/`html`) drop embeds **even with `include_images/links/formatting`
on** (measured: maangchi 9 images→0, garbageday 17→3, tweets always 0). So it cannot
be the embed substrate.

Corpus evidence (1,238 emails, 18 senders, 3-month window; see notes file):
captioned images dominate (nearly every source; maangchi is ~all photos), tweets are
common (ACX, Yglesias, minutes; garbageday as bare links), footnotes are heavy
(Colin Gorrie, ACX, Bloomberg), plus scattered YouTube, post cards, podcast embeds,
and code.

## 2. Goals / Non-goals

**Goals**
- Recover every embed a visual reader would see, positioned where it occurs.
- Render each type as audio via a distinct **meta-narrator "aside" voice**.
- Persist raw source HTML at intake so the entire back catalog can be **backfilled**.
- Granular enable/disable at global / per-source / per-element-type (incl. specific
  unknown shapes) — default everything on.
- Mechanical fast-path per type; **Gotify + LLM fallback** on breakage/unknown shapes.

**Non-goals**
- Not changing narration voice selection for existing article bodies.
- Not perfect visual fidelity — audio-appropriate descriptions, not pixel transcripts.
- The RSS-feed NUL-byte bug (Section 12) is tracked here but fixed independently.

## 3. Architecture

```
intake (imap / rss / archive)
  ├─ writes raw HTML → intake-html store          [§4]
  └─ structural_extract(html, source) → Block tree [§5]
        └─ serialize → cleaned text + inline markers + sidecar payloads.json [§5.3]
prepare-text
  └─ passes markers/sidecar through untouched (like BLOCKQUOTE_MARKER)       [§10]
text-to-speech / multivoice
  └─ resolve markers → render narration + asides (meta-narrator voice)       [§9]
backfill
  └─ re-run structural_extract over intake-html → regenerate episodes        [§11]
```

New module: `shared/podcast_shared/structural_extract.py` (extractor + block tree),
`shared/podcast_shared/embed_handlers.py` (per-type handlers), and
`shared/podcast_shared/describe.py` (LLM/vision provider). Rendering lives in
`text-to-speech/multivoice.py`.

## 4. Raw-HTML store

- Location: gitignored `intake-html/<source-slug>/<episode-id>.html`, where
  `<episode-id>` is the same stamp used for the text file (`YYYYMMDD-HHMMSS-…`), so
  HTML ↔ text ↔ audio are trivially joinable.
- Written by each intake path at the moment it has HTML in hand: imap (email HTML
  part), rss (`entry.content` value or scraped page), archive (fetched post HTML).
  For link/scraper paths the store holds the fetched page HTML.
- Retention: keep indefinitely (HTML is tiny vs. MP3s). Add `/intake-html/` to
  `.gitignore`.
- A `<episode-id>.meta.json` sidecar records `source`, `url`, `intake_type`, `fetched_at`.

## 5. Structural extractor

### 5.1 Content-region detection
Pluggable, best-match-first, with a generic fallback:
1. Platform hint table — Substack: `div.body.markup`; extend as learned.
2. Semantic: first `<article>`, else the `<main>`.
3. Density fallback: the element maximizing contained text length / descendant count,
   excluding `nav/header/footer/aside`.
Region detection failure → Gotify + treat whole document (still parse; never crash).

### 5.2 Block tree
`Block(type, payload: dict, children: list[Block])`, `@dataclass(slots=True)`.
Types: `text`, `heading`, `quote`, `tweet`, `image`, `video`, `audio`, `footnote`,
`card`, `code`, `table`, `unknown`. The extractor walks the region in document order;
container embeds recurse into children (tweet→image, figure→caption, card→image,
quote-tweet→tweet). Nesting is first-class; there are no flat special-cases.

### 5.3 Serialization (pipeline-compatible)
The text file stays human-readable and marker-driven, mirroring today's
`BLOCKQUOTE_MARKER` approach so `prepare-text` needs no structural awareness:
- Narrative text is written inline as today.
- Each non-text node emits a single marker line `⟦EMBED:<id>⟧` at its DOM position
  (footnotes emit their marker at the reference point, not a tail — Matt Levine model).
- Payloads (including large vision descriptions and nested child trees) live in a
  companion `<episode-id>.embeds.json` keyed by `<id>`; each entry carries `type`,
  `payload`, `children`, and the toggle-relevant `source` + `signature`.
- `shared` owns the marker constant, alongside `BLOCKQUOTE_MARKER`.

## 6. Per-type handlers

Each handler: `detect(el) -> bool`, `extract(el, ctx) -> Block` (recursing into
children via the dispatcher), and an aside **template**. Mechanical first; on
exception or unexpected shape → Gotify + LLM fallback (§7).

| Type | Mechanical extraction | Aside phrasing (aside voice) |
|---|---|---|
| `tweet` | Substack `table[data-component-name^=Tweet]` / generic `blockquote.twitter-tweet`: handle + text; recurse images/quote-tweet | "A tweet from @handle: '…'." + child renders |
| `image` | `alt` + `<figcaption>`; **vision description** layered on top (§8) | "Image: \<caption/description\>." |
| `video` | YouTube → yt-dlp metadata (title, channel, description; already used by youtube intake); other iframes → oEmbed/title | "A YouTube video titled '…' by …: \<short desc\>." |
| `footnote` | collect referenced note text; render **inline at ref point** | note text, in aside voice |
| `card` | embedded post/link card: title, author, url | "The author links to '\<title\>' by \<author\>." |
| `audio` | Spotify/SoundCloud/podcast iframe: title | "An audio embed: '\<title\>'." |
| `code` | `<pre>` text | announce; read verbatim only if short (≤ N lines), else "a code block" |
| `table` | small data tables → linearize; large → summarize via LLM | linearized / summarized |
| `unknown` | none | LLM fallback (§7) |

Decorative/tiny images (spacers, tracking pixels, avatars < threshold) are dropped
before handling.

## 7. Resilience — Gotify + LLM fallback

Any handler that raises, or a node whose shape doesn't match its detector's
expectations, triggers:
1. A Gotify notification including source, episode id, node `type`, and a **signature**
   (`tag` + sorted `class` tokens + `data-component-name`).
2. LLM fallback: the node's raw `outerHTML` is handed to the provider (§8) with a
   prompt to produce a concise spoken description; the result becomes the aside.
Unknown-type nodes always take the fallback. Signatures are logged so recurring
unknown shapes can be promoted to real handlers or toggled off (§10).

## 8. LLM / vision provider

### 8.1 Interface
`describe(*, html: str | None = None, image: bytes | None = None, alt: str | None,
context: str) -> str` in `shared/podcast_shared/describe.py`. One provider-agnostic
entry; images and raw-HTML both route through it.

### 8.2 Providers
- Default **GPT-5.6** via the OpenAI Responses API (infra already present for archive
  comment briefings; `OPENAI_API_KEY`). Free to us at present.
- Alternate **Gemini Flash** (`get_gemini_client`, existing).
- Selection via config key `describe_provider` (default `gpt-5.6`), overridable per
  source. Batchable — reuse the Gemini Batch pattern / async fan-out for image-heavy
  issues (maangchi) to control latency/cost.

## 9. Rendering (`multivoice.py`)

- Add a **meta-narrator / aside voice**, deterministically distinct from narration
  and from quote voices, configured in `narrators.yaml` (new `aside_voice` field;
  `narrators.example.yaml` updated).
- The renderer resolves each `⟦EMBED:<id>⟧` marker from the sidecar, applies the
  toggle matrix (§10), renders enabled nodes recursively into aside segments, and
  stitches them into the audio at the marker position (pydub), same as blockquotes.
- Footnotes render inline at their marker (aside voice). Nested children render as
  additional aside sentences within the parent aside.
- Works on both WaveNet (multi-voice) and Gemini-routed paths; Gemini-routed sources
  currently strip markers — instead they resolve them to aside text read by the
  single Gemini voice unless an aside voice is configured.

## 10. Toggle configuration

New `embeds:` section (in `narrators.yaml`, or a dedicated `embeds.yaml` with a
committed `embeds.example.yaml`):

```yaml
embeds:
  default: on                 # global master
  types:                      # per element-type global overrides
    footnote: on
    image: on
  unknown:
    "table.pencraft.tweet-xyz": off   # specific unknown signature
  sources:
    "maangchi@substack.com":
      image: on
      video: on
    "hi@www.garbageday.email":
      tweet: on
```

Resolution order (most specific wins): `source+signature → source+type → type →
global`. Applies to all source types including `unknown`. Default everything on.

## 11. Backfill

A `shared`/one-off script re-runs `structural_extract` over `intake-html/**` to
regenerate `text-input-raw` + sidecars for historical episodes, then the normal
pipeline re-renders them. Opt-in, per-source, idempotent, resumable (checkpoint like
`_scan.py`). Corpus already on disk under `intake-html/` (and the spike's
`email-corpus/` can seed it).

## 12. Related bug (tracked, fixed separately)

`dropcaster-docker/audio/evergreen/index.rss` carries 2 illegal NUL (`0x00`) bytes
before `</itunes:summary>` (items `post_id=208816543`, `212354052`), which strict
podcast clients treat as invalid XML and drop the description. Fix: sanitize control
chars when writing ID3 (`apply_id3_tags`) and/or post-process dropcaster output.
Root cause of why only 2/261 items leak the trailing NUL still open.

## 13. Testing

- Unit tests (pytest — the repo wants tests) per handler using **fixtures drawn from
  the saved corpus HTML** (`email-corpus/<sender>/*.html`): tweet, tweet-with-image,
  figure/caption, footnote-inline, card, quote-tweet nesting, unknown→fallback.
- Extractor tests: content-region detection per platform; DOM-order preservation;
  nesting depth.
- Toggle-resolution tests: precedence order incl. unknown signatures.
- Serialization round-trip: markers ↔ sidecar; `prepare-text` passthrough leaves
  markers intact (extend existing `test_marker_survival.py`).
- Provider calls mocked; no network in tests.

## 14. Phasing

1. Raw-HTML store at intake + `.gitignore` (unlocks backfill immediately).
2. Structural extractor + block tree + serialization; wire imap Substack path first.
3. Handlers: tweet, image(+vision), footnote — the top-3 by prevalence.
4. Rendering: aside voice in `multivoice.py`.
5. Toggle config + Gotify/LLM fallback + unknown signatures.
6. Remaining handlers (video, card, audio, code, table); generalize region detection
   to non-Substack (link/rss/archive/garbageday).
7. Backfill script.
8. NUL-byte fix (independent).

## 15. Open questions / risks

- Vision cost/latency on image-heavy issues (maangchi) — mitigate via batching; GPT-5.6
  free window may close (keep Gemini path warm).
- Content-region density fallback quality on arbitrary sites (link mode).
- Short-code threshold `N` for reading code verbatim — pick during impl.
- Aside-voice choice on the Gemini-routed single-voice path (no true second voice).
- yt-dlp metadata fetches add network to intake — cache in the store.
