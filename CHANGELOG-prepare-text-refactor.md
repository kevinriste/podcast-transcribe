# Changelog: prepare-text refactor

## Summary

Migrated all post-processing (filtering, text cleaning, text transformation) out of intake scripts and TTS into a new dedicated script `prepare-text/prepare_text.py`. Intake scripts now write raw content only. TTS reads pre-cleaned content only.

## New files

### `prepare-text/prepare_text.py`

New preprocessing script that runs between intake (imap/rss) and TTS. Handles:

- **Filtering**: YAML-configured rules that skip files based on metadata (from, title, source_url, etc.)
  - Two action types: `skip` (stops processing, moves to filtered dir) and `notify` (sends Gotify, continues)
  - Rules execute in YAML declaration order; `skip` terminates evaluation for that file
  - LLM-based filtering via `llm_check` (sends prompt to Gemini, expects yes/no)
  - Rule ordering validation: detects skip rules that shadow later rules, sends Gotify alert, leaves affected files unprocessed
- **General cleaning** (all on by default, can be disabled globally or per-source in YAML):
  - Beehiiv markdown-to-plaintext conversion and emphasis marker removal
  - URL removal
  - Legal citation bracket unwrap (`[t]he` → `the`)
  - Triple dash removal
  - Empty bracket/paren/angle removal
  - Whitespace collapse
  - Unsubscribe section removal
  - "View this post on the web" removal
  - Substack "substacks referenced above" block removal
  - Standalone @ line removal
  - End-of-line punctuation insertion
- **YAML text removals** (regex patterns to strip source-specific boilerplate)
- **YAML text replacements** (regex find/replace for pronunciation fixes etc.)
- **Author/title prepend and append**: Adds `{from}.\n{title}.\n` to start and end of every article from metadata
- **Empty and too-big file checks**: Moved from TTS; routes to filtered directory with reason
- **Stats logging**: Daily JSON files in `prepare-text/stats/` with full per-file detail (which rules fired, match counts, before/after char counts, archive paths). 12-month retention with automatic rotation.
- **Atomic per-file processing**: Read raw → process → write outputs → archive → delete raw. If anything fails mid-file, raw file stays for retry next run.
- **Config validation**: Three layers — YAML syntax errors crash (process.sh handles), schema violations crash with clear message, rule ordering issues send targeted Gotify and skip affected files only.
- Variables use `Final` from typing where possible (not inside loops due to Python limitation).

### `prepare-text/filters.example.yaml`

Documented example config with all confirmed filters and removals:

**Filters:**
- Bill Simmons NFL detection (notify via Gotify, then skip all)
- Jessica Valenti (skip unless "the week in" in subject)
- K-Culture/JaeHa Kim (skip when "bts" in subject)

**Text removals (confirmed patterns):**
- NYT: Advertisement lines (mid-article), Supported by, Opinion | prefix, letters-to-editor block, social media footer, Ross Douthat bio, Ezra Klein bio, Related Content
- Substack: reader-supported publication nag, "feel free to share" prompt, substacks referenced above block, standalone @ lines, view online prompt
- Beehiiv: plain-text disclaimer, image captions
- Garbage Day: typos disclaimer, TikTok Video placeholder, Instagram placeholder
- K-Culture: copyright line, unicode small-caps promotional text
- Money Illusion: social links header

**Text replacements:**
- Keynesian → Cainzeean (pronunciation fix)

**Not added (user declined):**
- Garbage Day "P.S. here's {something}" lines
- K-Culture Zoom meet-up promo

### `prepare-text/pyproject.toml`

New uv subproject. Dependencies: beautifulsoup4, google-genai, markdown, pyyaml, requests.

### `PLAN-prepare-text-refactor.md`

Full design plan document covering architecture, YAML schema, stats format, directory layout, and all decisions made during planning.

## Modified files

### `imap/parse_email.py`

- **Removed**: All text transformation — URL stripping, empty bracket/paren/angle cleanup, Beehiiv markdown-to-plaintext conversion, Beehiiv emphasis marker removal, author+subject line prepending
- **Removed**: Jessica Valenti and JaeHa Kim email filters (moved to YAML config)
- **Removed**: `markdown_to_plain_text()` function and `markdown` import
- **Changed**: Output directory from `text-to-speech/text-input/` to `prepare-text/text-input-raw/`
- **Added**: `META_INTAKE_TYPE: email` for newsletters, `META_INTAKE_TYPE: link` for link mode
- **Unchanged**: YouTube bypass (still writes directly to audio), source URL detection, Gotify notification for unknown sources

### `rss/check-rss.py`

- **Removed**: `is_nfl_related()` function and all NFL detection logic (moved to YAML LLM filter)
- **Removed**: Bill Simmons skip logic — now writes all episodes with description as content
- **Removed**: Feed title + entry title prepending for generic RSS feeds (moved to prepare_text.py)
- **Removed**: `get_gemini_client()`, `_gemini_client`, `summary_model`, `pydantic` import, `genai` import
- **Changed**: Output directory from `text-to-speech/text-input/` to `prepare-text/text-input-raw/`
- **Added**: `META_INTAKE_TYPE: rss`
- **Unchanged**: Wayback Machine fallback logic, feed staleness checks, GUID tracking

### `text-to-speech/text_to_speech.py`

- **Removed**: `clean_text()` function entirely (moved to prepare_text.py)
- **Removed**: Empty-after-cleaning handling with Gotify notification and move to holding directory
- **Removed**: Too-big file handling with Gotify notification and move to holding directory
- **Removed**: `character_limit` variable, `shutil` and `requests` imports
- **Changed**: Input directory from `text-input/` to `../prepare-text/text-input-cleaned/`
- **Changed**: Content variable now reads pre-cleaned text directly (no `clean_text()` call)
- **Changed**: Empty file check simplified to log-and-skip (no longer moves files)
- **Added**: `META_INTAKE_TYPE` read from metadata, passed to `build_description()`
- **Added**: `INTAKE_TYPE_LABELS` mapping and "Via: {type}" line in episode description
- **Added**: `intake_type` parameter to `build_description()`

### `process.sh`

- **Added**: `prepare-text` step between RSS and TTS (`uv sync` + `uv run python3 prepare_text.py`)
- **Removed**: Archive copy step (`cp --update=none text-to-speech/text-input/*.txt ...`) — now handled by prepare_text.py
- **Removed**: Empty file move step (`find ./text-input -size 0 ...`) — now handled by prepare_text.py

### `pyproject.toml`

- **Changed**: `line-length` from 88 to 120

### `.gitignore`

- **Added**: `prepare-text/filters.yaml` (user's active config, not checked in)
- **Added**: `prepare-text/stats/` (daily stats logs)

## Directory layout (new)

```
prepare-text/
  prepare_text.py              # preprocessing script
  filters.yaml                 # gitignored — user's active config
  filters.example.yaml         # checked in — documents schema
  stats/                       # gitignored — daily JSON logs
  text-input-raw/              # intake scripts write here
  text-input-raw-archive/      # permanent archive of originals
  text-input-cleaned/          # output for TTS
  text-input-cleaned-archive/  # permanent archive of cleaned versions
  text-input-filtered/         # files that hit a filter (with META_FILTERED_REASON)
  pyproject.toml               # uv subproject
```

## Pipeline flow (updated)

```
imap/parse_email.py  →  rss/check-rss.py  →  prepare-text/prepare_text.py  →  text-to-speech/text_to_speech.py  →  dropcaster
     (raw email)          (raw RSS)              (filter + clean)                  (TTS + MP3)                     (RSS feed)
```
