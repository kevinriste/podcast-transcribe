# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Converts incoming emails (Substack/Beehiiv newsletters, links, YouTube) and RSS feeds into podcast episodes using Google Cloud TTS, published via Dropcaster. Runs every 20 minutes via cron.

## Commands

Each subproject (imap/, rss/, text-to-speech/) is an independent uv-managed Python project. Always `cd` into the subproject directory first.

```bash
# Install deps
cd imap && uv sync
cd rss && uv sync
cd text-to-speech && uv sync

# Run scripts
cd imap && uv run python3 parse_email.py
cd rss && uv run python3 check-rss.py
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

- **ruff**: ALL rules enabled except D (docstrings) and CPY (copyright). Preview mode on. Line length 88. Target Python 3.12.
- **basedpyright**: `typeCheckingMode = "all"`, Python 3.12.8.

## Architecture

**Pipeline flow** (`process-caller.sh` → `process.sh`):

1. **imap/parse_email.py** — Fetches unseen Gmail messages. Three intake modes based on subject:
   - Default (newsletters): extract text, detect Beehiiv/Substack, find source URL, write text file with metadata headers
   - `link`: fetch full article via local scraper at `localhost:3001`
   - `youtube`: download audio via yt-dlp, write ID3 tags directly (bypasses TTS)

2. **rss/check-rss.py** — Polls feeds from `rss/feeds.txt`. NYT feeds use local scraper at `localhost:3002` with Wayback Machine fallback. Bill Simmons feed uses Gemini to detect/skip NFL episodes. GUIDs tracked in `rss/feed-guids/`.

3. **process.sh** archives text inputs to `text-to-speech/input-text-archive/`, removes empty files.

4. **text-to-speech/text_to_speech.py** — Reads `text-to-speech/text-input/*.txt`, parses metadata headers (`META_FROM`, `META_TITLE`, `META_SOURCE_URL`, `META_SOURCE_KIND`, `META_SOURCE_NAME`), cleans text aggressively, chunks into 3-5k char segments, calls Google Cloud TTS (en-US-Wavenet-F), stitches MP3 chunks with pydub, generates Gemini summary, writes ID3 tags. Output goes to `dropcaster-docker/audio/`.

5. **Dropcaster** (Docker) regenerates `index.rss` when audio files change. Audio older than 8 weeks is archived.

**Email filters** in `parse_email.py` (set `move_to_podcast = False` to skip):
- Jessica Valenti: skip unless subject contains "the week in"
- K-Culture with Jae-Ha Kim: skip when subject contains "BTS"

## Key conventions

- **Metadata headers**: All text input files start with `META_` prefixed lines, blank line, then content. Both imap and rss writers produce these; TTS parses them.
- **Gemini for summaries**: Model `gemini-3.1-flash-lite-preview`, used in all three subprojects. Client initialized via `GEMINI_API_KEY` env var.
- **Gotify notifications**: Sent on errors and notable events (unknown email source, NFL whitelist, scraper failures).
- **Text cleaning happens twice**: once in the intake scripts (URL removal, bracket cleanup) and again in TTS (more aggressive: unsubscribe sections, social links, pronunciation fixes).

## Environment variables

- `GMAIL_PODCAST_ACCOUNT`, `GMAIL_PODCAST_ACCOUNT_APP_PASSWORD` — IMAP access
- `GEMINI_API_KEY` — Gemini summaries
- `GOOGLE_APPLICATION_CREDENTIALS` — Google Cloud TTS service account JSON
- `GOTIFY_SERVER`, `GOTIFY_TOKEN` — push notifications
- `PODCAST_DOMAIN_PRIMARY` — Dropcaster RSS URL
