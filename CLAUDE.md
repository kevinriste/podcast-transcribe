# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Converts incoming emails (Substack/Beehiiv newsletters, links, YouTube) and RSS feeds into podcast episodes using Google Cloud TTS, published via Dropcaster. Runs every 20 minutes via cron.

## Commands

There are 5 independent uv-managed Python subprojects: `imap/`, `rss/`, `prepare-text/`, `text-to-speech/`, and `shared/`. Always `cd` into the subproject directory first.

```bash
# Install deps
cd imap && uv sync
cd rss && uv sync
cd prepare-text && uv sync
cd text-to-speech && uv sync
cd shared && uv sync

# Run scripts
cd imap && uv run python3 parse_email.py
cd rss && uv run python3 check-rss.py
cd prepare-text && uv run python3 prepare_text.py
cd text-to-speech && uv run python3 text_to_speech.py

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
   - Default (newsletters): extract text, detect Beehiiv/Substack, find source URL, write text file with metadata headers to `prepare-text/text-input-raw/`
   - `link`: fetch full article via Playwright + trafilatura through local scraper at `localhost:3001`
   - `youtube`: download audio via yt-dlp, write ID3 tags directly (bypasses TTS pipeline)

2. **rss/check-rss.py** — Polls feeds from `rss/feeds.txt` (NYT columns via Wayback Machine, Bill Simmons via Megaphone). Bill Simmons feed extracts description text. Other feeds use BeautifulSoup on `entry.content`. GUIDs tracked in `rss/feed-guids/`. Output to `prepare-text/text-input-raw/`.

3. **prepare-text/prepare_text.py** — Reads raw text from `text-input-raw/`, applies filters and text cleaning rules from `filters.yaml`, writes cleaned output to `text-input-cleaned/`. Handles filtering (skip/notify), general cleaning (URL removal, bracket cleanup, whitespace collapse, etc.), and YAML-configured text removals/replacements. Archives raw and cleaned files. Tracks per-file stats.

4. **text-to-speech/text_to_speech.py** — Reads `prepare-text/text-input-cleaned/*.txt`, parses metadata headers, chunks into 3-5k char segments, calls Google Cloud TTS (en-US-Wavenet-F), stitches MP3 chunks with pydub, generates Gemini summary, writes ID3 tags. Output goes to `dropcaster-docker/audio/`.

5. **Dropcaster** (Docker) regenerates `index.rss` when audio files change. Audio older than 8 weeks is archived.

**Filters** are configured in `prepare-text/filters.yaml` (not in parse_email.py):
- Bill Simmons: NFL notification via Gemini LLM check, then skip all (podcast feed, no article text)
- Jessica Valenti: skip unless subject contains "the week in"
- K-Culture with Jae-Ha Kim: skip when subject contains "BTS"

## Shared module

`shared/podcast_shared/` contains utilities used across subprojects:
- `send_gotify_notification` — push notifications (intentionally no error handling; see REVIEW-FINDINGS.md #6)
- `get_gemini_client` — singleton Gemini client
- `generate_summary` — article summarization via Gemini
- `split_metadata` — parse META_ headers from text files
- `apply_id3_tags` — write ID3 tags to MP3 files (keyword-only args after mp3_path)

## Key conventions

- **Metadata headers**: All text input files start with `META_` prefixed lines, blank line, then content. imap and rss produce these; prepare_text parses them for filtering; TTS parses them for summaries and ID3 tags.
- **Gemini**: Model `gemini-3.1-flash-lite-preview`, used in imap (YouTube summaries), prepare-text (LLM filter checks), and text-to-speech (article summaries). Client initialized via `GEMINI_API_KEY` env var.
- **Gotify notifications**: Sent on errors and notable events (unknown email source, filter matches, content too large).
- **Text cleaning happens in prepare_text.py only**: general cleaning steps + YAML-configured removals/replacements. Intake scripts (imap, rss) write raw text.

## Environment variables

- `GMAIL_PODCAST_ACCOUNT`, `GMAIL_PODCAST_ACCOUNT_APP_PASSWORD` — IMAP access
- `GEMINI_API_KEY` — Gemini summaries and LLM checks
- `GOOGLE_APPLICATION_CREDENTIALS` — Google Cloud TTS service account JSON
- `GOTIFY_SERVER`, `GOTIFY_TOKEN` — push notifications
- `PODCAST_DOMAIN_PRIMARY` — Dropcaster RSS URL
