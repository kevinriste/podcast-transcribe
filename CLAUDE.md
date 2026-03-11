# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Converts incoming emails (Substack/Beehiiv newsletters, links, YouTube) and RSS feeds into podcast episodes using Google Cloud TTS, published via Dropcaster. Runs every 20 minutes via cron.

## Commands

Each subproject (imap/, prepare-text/, rss/, text-to-speech/) is an independent uv-managed Python project. Always `cd` into the subproject directory first.

```bash
# Install deps
cd imap && uv sync
cd prepare-text && uv sync
cd rss && uv sync
cd text-to-speech && uv sync

# Run scripts
cd imap && uv run python3 parse_email.py
cd prepare-text && uv run python3 prepare_text.py
cd rss && uv run python3 check-rss.py
cd text-to-speech && uv run python3 text_to_speech.py

# Lint (from any subproject dir)
uv run ruff check .
uv run ruff check --fix .

# Format check (from any subproject dir)
uv run ruff format --check .

# Type check (from subproject dir)
uv run basedpyright

# Tests (from subproject dir)
uv run pytest -v
```

Unit tests exist for all four subprojects (`test_*.py` files). CI runs pytest, ruff check, ruff format, and basedpyright on every PR and push to main.

## Linting and type checking

Root `pyproject.toml` defines shared ruff + basedpyright config. Subproject `pyproject.toml` files extend it.

- **ruff**: ALL rules enabled except D (docstrings), CPY (copyright), PLR0914 (too many locals), LOG015 (root logger), COM812 (trailing comma — conflicts with ruff format), FBT003 (boolean positional default). Preview mode on. Line length 120. Target Python 3.12.
- **ruff complexity limits**: max-complexity 35, max-branches 30, max-statements 120.
- **ruff per-file ignores for tests**: S101, PLR6301, PLC2701, ANN, PLR2004.
- **basedpyright**: `typeCheckingMode = "all"`, Python 3.12.8.

## Architecture

**Pipeline flow** (`process-caller.sh` → `process.sh`):

1. **imap/parse_email.py** — Fetches unseen Gmail messages. Three intake modes based on subject:
   - Default (newsletters): extract text, detect Beehiiv/Substack, find source URL, write text file with metadata headers
   - `link`: fetch full article via local scraper at `localhost:3001`
   - `youtube`: download audio via yt-dlp, write ID3 tags directly (bypasses TTS)

2. **rss/check-rss.py** — Polls feeds from `rss/feeds.txt`. NYT feeds use local scraper at `localhost:3002` with Wayback Machine fallback. GUIDs tracked in `rss/feed-guids/`.

3. **prepare-text/prepare_text.py** — YAML-driven text filtering, cleaning, and transformation. Reads `filters.yaml` for per-source rules (text removals, replacements, general cleaning). Writes daily JSON stats to `prepare-text/stats/`.

4. **process.sh** orchestrates the pipeline: runs imap, rss, prepare-text, and text-to-speech in sequence, then archives old audio and triggers Dropcaster.

5. **text-to-speech/text_to_speech.py** — Reads `prepare-text/text-input-cleaned/*.txt`, parses metadata headers (`META_FROM`, `META_TITLE`, `META_SOURCE_URL`, `META_SOURCE_KIND`, `META_SOURCE_NAME`, `META_INTAKE_TYPE`), chunks into 3-5k char segments, calls Google Cloud TTS (en-US-Wavenet-F), stitches MP3 chunks with pydub, generates Gemini summary, writes ID3 tags. Output goes to `dropcaster-docker/audio/`.

6. **Dropcaster** (Docker) regenerates `index.rss` when audio files change. Audio older than 8 weeks is archived.

**Email/RSS filters** are configured in `prepare-text/filters.yaml` (YAML-driven, per-source match rules with skip/notify actions and optional LLM checks). See `prepare-text/filters.example.yaml` for schema documentation.

## Key conventions

- **Metadata headers**: All text input files start with `META_` prefixed lines, blank line, then content. Both imap and rss writers produce these; TTS parses them.
- **Gemini**: Model `gemini-3.1-flash-lite-preview`. Used for summaries in imap and text-to-speech, and for LLM filter checks in prepare-text. Client initialized via `GEMINI_API_KEY` env var.
- **Gotify notifications**: Sent on errors and notable events (unknown email source, filter matches, scraper failures, oversized/empty content).
- **Text cleaning is centralized** in `prepare-text/prepare_text.py` via YAML-driven rules (URL removal, bracket cleanup, unsubscribe sections, pronunciation fixes, etc.).
- **Immutability with pyrsistent**: All scripts use `PMap`, `PVector`, `freeze()`, `thaw()` from pyrsistent. `freeze()` at ingestion boundaries (YAML/JSON parse), `thaw()` at serialization. Function parameters use abstract types (`Mapping`, `Sequence`); return types use concrete immutables (`PMap`, `PVector`, `tuple`). Mutable escape hatches are documented with comments where third-party APIs require mutation (e.g., mutagen ID3 tags, pydub audio).
- **`Final` annotations**: Used on all single-assignment local variables (not inside loops — Python limitation).
- **Frozen dataclasses**: `@dataclass(frozen=True, slots=True)` for all dataclasses.

## Environment variables

- `GMAIL_PODCAST_ACCOUNT`, `GMAIL_PODCAST_ACCOUNT_APP_PASSWORD` — IMAP access
- `GEMINI_API_KEY` — Gemini summaries
- `GOOGLE_APPLICATION_CREDENTIALS` — Google Cloud TTS service account JSON
- `GOTIFY_SERVER`, `GOTIFY_TOKEN` — push notifications
- `PODCAST_DOMAIN_PRIMARY` — Dropcaster RSS URL
