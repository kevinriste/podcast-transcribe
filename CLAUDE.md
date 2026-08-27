# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Public repository standard

This repo is public and must stay generic and portable — free of the maintainer's private specifics so anyone can clone and run it. Nothing private lives in tracked files: every private or machine-specific value is supplied by a gitignored file or an environment variable. The following are gitignored, each with a committed `*.example.*` template using generic placeholders:

- **Content-source identities** — specific publications/blogs/newsletters/podcasts/authors, their feed URLs, and source-specific routing/classification. Pipeline code stays source-agnostic and config-driven. Generic *platforms* (Substack, Beehiiv) are fine — detected structurally.
- **Personal feed identity** — feed titles, descriptions, cover art, landing-page copy, and domains for the maintainer's own feeds.
- **Infrastructure & secrets** — real domains/hostnames, tokens/keys, account emails, cloud project IDs, credential filenames, absolute home paths. Scripts use `$HOME`/repo-relative paths and read secrets/locations from `.env`; ports default to `localhost:PORT`.
- **Active config & state** — real `*.yaml`/`*.json` config and runtime state; each has a committed `*.example.*` template.

Machine-specific values live in a gitignored root `.env` (copy from `.env.example`). Personal one-off/scratch scripts are not committed. The server runs `main` directly with these gitignored files — no private branch. Design/planning docs under `docs/superpowers/` are encouraged on feature branches but **must be deleted before merging to `main`**.

## What this project does

Converts incoming emails (Substack/Beehiiv newsletters, links, YouTube) and RSS feeds into podcast episodes using Google Cloud TTS, published via Dropcaster. Runs every 20 minutes via cron.

## Commands

There are 6 independent uv-managed Python subprojects: `imap/`, `rss/`, `archive/`, `prepare-text/`, `text-to-speech/`, and `shared/`. Always `cd` into the subproject directory first.

```bash
# Install deps
cd imap && uv sync
cd rss && uv sync
cd archive && uv sync
cd prepare-text && uv sync
cd text-to-speech && uv sync
cd shared && uv sync

# Run scripts
cd imap && uv run python3 parse_email.py
cd rss && uv run python3 check-rss.py
cd archive && uv run python3 check-archive.py
cd prepare-text && uv run python3 prepare_text.py
cd text-to-speech && uv run python3 text_to_speech.py

# One-off audition: render a text file with a chosen Gemini TTS engine + voice
# into the feed, annotated (synchronous, not batch)
cd text-to-speech && uv run python3 audition.py "<file.txt>" --engine gemini-3.1-flash --voice Callirrhoe

# Lint (from any subproject dir, or root for shared config)
uv run ruff check .
uv run ruff check --fix .

# Type check (from subproject dir)
uv run basedpyright
```

There are no tests. Validation is manual.

## Linting and type checking

Root `pyproject.toml` defines shared ruff + basedpyright config. Subproject `pyproject.toml` files extend it.

- **ruff**: ALL rules enabled except CPY (copyright) and specific complexity/style rules (C901, PLR0911-PLR2004, LOG015, TRY300, COM812, E501). D (docstrings) enabled with D213/D203 ignored. Preview mode on. Line length 120. Target Python 3.12.
- **ruff format**: Enabled, line-length 120. E501 is not linted — the formatter handles it.
- **basedpyright**: `typeCheckingMode = "all"`, Python 3.12.8. Zero errors across all subprojects. Untyped library boundaries narrowed with `isinstance`/`str()`/`getattr()` — never use `cast()`.

## Architecture

**Pipeline flow** (`process-caller.sh` → `process.sh`):

1. **imap/parse_email.py** — Fetches unseen Gmail messages. Three intake modes based on subject:
   - Default (newsletters): extract text, detect Beehiiv/Substack, find source URL, write text file with metadata headers to `prepare-text/text-input-raw/`. Substack (and other HTML-body sources) extract the body from the email's HTML part rather than its lossy plain-text part (recovering truncated content and preventing anchor word-joins); `extract_body_text` also prefixes each block quotation with `BLOCKQUOTE_MARKER` so TTS can voice quotes distinctly. Recognized platforms (Substack, Beehiiv) instead run through the structural extractor (`shared/podcast_shared/structural_extract.py`), which walks the HTML into an ordered block tree and recovers embedded content — tweets, images, videos, link cards, footnotes — as `ASIDE_MARKER` asides that TTS voices in a distinct "aside" voice. Content images, **including a tweet's own media image** (attached as a nested `image` block), are described via OpenAI Responses-API vision (`shared/podcast_shared/describe.py`), gated by `EMBED_VISION`; individual embed types can be suppressed with `EMBED_DROP_TYPES`. Publisher-specific handling (HTML-body extraction, custom link-finding) is configured in `imap/sources.yaml` (gitignored; `sources.example.yaml` is the schema doc).
   - `link`: fetch full article via Playwright + trafilatura; routes URLs whose host matches a configured authenticated domain to the authenticated scraper (default `localhost:3002`), all others to the general scraper (default `localhost:3001`). Scraper endpoints and authenticated domains are configured in `imap/sources.yaml`.
   - `youtube`: download audio via yt-dlp, write ID3 tags directly (bypasses TTS pipeline)

2. **rss/check-rss.py** — Polls feeds configured in `rss/feeds.yaml` (gitignored; `feeds.example.yaml` is the schema doc). Each feed has a handling `mode`: `content` (BeautifulSoup on `entry.content`, the default), `description` (use the entry summary, e.g. podcast feeds), or `full_scraper` (fetch the full article via the authenticated scraper, verified by per-feed check phrases; on failure, sends Gotify notification and breaks, preserving the GUID for retry). GUIDs tracked in `rss/feed-guids/`. Output to `prepare-text/text-input-raw/`.

3. **archive/check-archive.py** — Scheduled intake that walks a blog/archive one post per day from a gitignored `posts.json`, tracking progress in `state.json`. The source display name comes from `archive/source.yaml` (gitignored; `source.example.yaml` is the schema doc). When a post has enough comments, it also writes a multi-voice "Highlights From The Comments" episode (`archive/comment_briefing.py`, via the OpenAI Responses API model in `COMMENT_BRIEFING_MODEL`).

4. **prepare-text/prepare_text.py** — Reads raw text from `text-input-raw/`, applies filters and text cleaning rules from `filters.yaml`, writes cleaned output to `text-input-cleaned/`. Handles filtering (skip/notify), general cleaning (URL removal, bracket cleanup, whitespace collapse, etc.), and YAML-configured text removals/replacements. `archive-comments` episodes pass through content-mutating cleaning untouched (their speaker tags are load-bearing). Archives raw and cleaned files. Tracks per-file stats.

5. **text-to-speech/text_to_speech.py** — Reads `prepare-text/text-input-cleaned/*.txt`, parses metadata headers, and routes each file to a narrator based on `narrators.yaml` (gitignored; `narrators.example.yaml` is the checked-in schema doc). Default: chunk into 3-5kB segments, call Google Cloud TTS (en-US-Wavenet-F) synchronously. Sources routed to `gemini-flash`/`gemini-pro` are chunked into 8-12kB segments and submitted as one Gemini Batch API job (half-price audio tokens); the text file is held in `text-to-speech/batch-pending/` with a state JSON, and each run collects finished jobs before submitting new ones (failed jobs notify via Gotify and fall back to Wavenet). WaveNet articles carrying at least one `BLOCKQUOTE_MARKER` quote are rendered multi-voice (narrator plus deterministically varied quote voices) through the shared `multivoice.py` core — the same module that renders comment-highlights episodes; for quote-free articles, or Gemini-routed sources, the marker is stripped and the article is read single-voice. An optional second ("evergreen") feed collects long-form/backlog episodes, routed via the `evergreen_feed` section of `narrators.yaml`. All paths stitch audio with pydub, generate a Gemini summary, and write ID3 tags. Output goes to `dropcaster-docker/audio/` (or `dropcaster-docker/audio/<evergreen-dir>/`).

6. **Dropcaster** (Docker) regenerates `index.rss` when audio files change. Topical-feed audio older than `PODCAST_RETENTION_WEEKS` (default 8) is moved to `dropcaster-docker/audio-archive/` — moved, never deleted (the evergreen feed's subdirectory is left to accumulate).

**Filters** are configured in `prepare-text/filters.yaml` (gitignored; `filters.example.yaml` is the schema doc), not in parse_email.py. Filters match on the sender/source and can skip or send a Gotify notification (optionally gated by a Gemini LLM check), e.g. skip a source unless the subject contains a keyword.

## Shared module

`shared/podcast_shared/` contains utilities used across subprojects:
- `send_gotify_notification` — push notifications (intentionally no error handling; see REVIEW-FINDINGS.md #6)
- `get_gemini_client` — singleton Gemini client
- `generate_summary` — article summarization via Gemini
- `split_metadata` — parse META_ headers from text files
- `apply_id3_tags` — write ID3 tags to MP3 files (keyword-only args after mp3_path)
- `BLOCKQUOTE_MARKER` — line prefix intake writes ahead of each quoted passage; the single source of truth shared by imap (marking), prepare-text (survives cleaning), and text-to-speech (`multivoice.py` voices/strips it)
- `structural_extract.py` — HTML → ordered `Block` tree (`text`/`quote`/`tweet`/`image`/`video`/`card`/`footnote`), with nested children (e.g. a tweet's media image); pure and offline. Defines `ASIDE_MARKER` (the aside counterpart to `BLOCKQUOTE_MARKER`) and `serialize_flat`
- `aside_render.py` — renders an embed `Block` (and its children, recursively) into the spoken "aside" text a meta-narrator would say
- `describe.py` — `describe_image`/`enrich_images`: OpenAI Responses-API vision, injected into the extractor so it stays pure; degrades to `""` when `EMBED_VISION=0`, `OPENAI_API_KEY` is unset, or the call fails
- **`podcast_shared` is an editable path dependency** in every subproject (`{ path = "../shared", editable = true }`), so edits to `shared/` are live immediately — no reinstall needed. If a change to `shared/` isn't taking effect, the dep has regressed to non-editable; re-run `uv sync --reinstall-package podcast-shared`

## Key conventions

- **Metadata headers**: All text input files start with `META_` prefixed lines, blank line, then content. imap and rss produce these; prepare_text parses them for filtering; TTS parses them for summaries and ID3 tags.
- **Gemini**: Model `gemini-3.1-flash-lite`, used in imap (YouTube summaries), prepare-text (LLM filter checks), and text-to-speech (article summaries). Client initialized via `GEMINI_API_KEY` env var.
- **Gotify notifications**: Sent on errors and notable events (unknown email source, filter matches, content too large).
- **Text cleaning happens in prepare_text.py only**: general cleaning steps + YAML-configured removals/replacements. Intake scripts (imap, rss) write raw text.

## Environment variables

- `GMAIL_PODCAST_ACCOUNT`, `GMAIL_PODCAST_ACCOUNT_APP_PASSWORD` — IMAP access
- `GEMINI_API_KEY` — Gemini summaries, LLM checks, and Gemini TTS (set in `.env`)
- `GOOGLE_APPLICATION_CREDENTIALS` — absolute path to the Google Cloud TTS service-account JSON (set in `.env`)
- `GOTIFY_SERVER`, `GOTIFY_TOKEN` — push notifications
- `PODCAST_DOMAIN_PRIMARY` — Dropcaster RSS URL
- `PODCAST_RETENTION_WEEKS` — weeks of audio kept in the topical feed before archiving (default 8; set in `.env`)
- `OPENAI_API_KEY` — comment-highlights briefings (archive intake, `COMMENT_BRIEFING_MODEL` overrides the model) **and** embed image vision (imap intake, `EMBED_VISION_MODEL` overrides the model, default `gpt-5.6`)
- `EMBED_VISION` — set to `0` to disable OpenAI vision descriptions of embedded images (asides then fall back to caption/alt); `EMBED_DROP_TYPES` — comma-separated embed types to drop entirely (e.g. `video,card`)

## Running your own instance

1. `cp .env.example .env` and fill in real values.
2. Copy each `*.example.*` template to its real name and edit:
   `rss/feeds.example.yaml`→`feeds.yaml`, `imap/sources.example.yaml`→`sources.yaml`,
   `archive/source.example.yaml`→`source.yaml`, `text-to-speech/narrators.example.yaml`→`narrators.yaml`,
   `prepare-text/filters.example.yaml`→`filters.yaml`,
   `dropcaster-docker/audio/channel.example.yml`→`channel.yml` (+ `index.html`, cover image),
   `dropcaster-docker/audio/evergreen/*.example.*`→real (optional second feed).
3. Provide `archive/posts.json` if using the archive intake.

All of these are gitignored; the pipeline code is generic and reads them at runtime.
