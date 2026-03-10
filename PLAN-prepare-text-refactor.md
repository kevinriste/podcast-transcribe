# Plan: prepare-text refactor

## Overview

Migrate all post-processing (filtering, text cleaning, text transformation) to a new dedicated script `prepare-text/prepare_text.py`. Intake scripts become dumb writers of raw content. TTS script becomes a dumb reader of cleaned content.

## New subproject: `prepare-text/`

```
prepare-text/
  prepare_text.py              # the new script
  filters.yaml                 # gitignored — active config
  filters.example.yaml         # checked in — documents schema
  stats/                       # daily JSON logs (gitignored), 12-month retention
  text-input-raw/              # intake scripts write here
  text-input-raw-archive/      # permanent archive of originals
  text-input-cleaned/          # output for TTS to read
  text-input-cleaned-archive/  # permanent archive of cleaned versions
  text-input-filtered/         # files that hit a filter (with META_FILTERED_REASON)
  pyproject.toml               # uv project, extends root config
```

## Pipeline flow (process.sh)

```
imap/parse_email.py → rss/check-rss.py → prepare-text/prepare_text.py → text-to-speech/text_to_speech.py → dropcaster
```

No more archive step in process.sh — prepare_text.py owns the full lifecycle.

## Changes to intake scripts

### imap/parse_email.py

- Remove ALL text transformation: URL stripping, empty bracket/paren/angle cleanup, author+subject prepending, Beehiiv markdown-to-plaintext, Beehiiv emphasis marker removal
- Remove Jessica Valenti and JaeHa Kim filters
- Write raw `msg.text` with metadata headers to `prepare-text/text-input-raw/`
- Add `META_INTAKE_TYPE: email` for newsletters, `META_INTAKE_TYPE: link` for link mode
- YouTube path unchanged (bypasses everything, writes directly to audio)

### rss/check-rss.py

- Remove Bill Simmons NFL detection and skip logic
- Remove feed title + entry title prepending for non-NYT feeds
- Write ALL entries (including Bill Simmons with description as content) to `prepare-text/text-input-raw/`
- Add `META_INTAKE_TYPE: rss`

## prepare_text.py — per-file pipeline

All variables use `Final` annotation. Immutable chaining style.

```
for each file in text-input-raw:
    1. Read raw file + parse metadata
    2. Run filters (YAML order; action: notify continues, action: skip stops)
       - If skip: add META_FILTERED_REASON, write to text-input-filtered/, log stats, delete raw, next
       - If notify: send Gotify, continue to next rule
    3. Beehiiv plaintext conversion (if META_SOURCE_KIND: beehiiv)
    4. General cleaning (fixed order, configurable on/off):
       a. URL removal
       b. Legal bracket unwrap [t]he → the
       c. Triple dash removal
       d. Empty bracket/paren/angle removal
       e. Whitespace collapse
       f. Unsubscribe section removal
       g. "View this post on the web" removal
       h. Substack "substacks referenced above" + standalone @ lines
       i. Beehiiv emphasis marker removal (if beehiiv)
       j. End-of-line punctuation insertion
    5. YAML text_removals (declaration order)
    6. YAML text_replacements (declaration order)
    7. Prepend author + title from metadata
    8. Append author + title from metadata
    9. Check too-big (>150k chars) → filtered if over limit
    10. Check empty → filtered if empty
    11. Write cleaned to text-input-cleaned/
    12. Archive raw to text-input-raw-archive/
    13. Archive cleaned to text-input-cleaned-archive/
    14. Log stats
    15. Delete raw from text-input-raw/
```

If anything fails at steps 11-14, raw file survives in text-input-raw for retry next run.

## Config validation (at startup)

Three layers:

1. **Invalid YAML syntax** — crash, process.sh error handling sends Gotify
2. **Valid YAML but invalid schema** (missing required fields, unknown fields, bad regex, action:notify without notify block, etc.) — crash with clear error message
3. **Valid config but rule ordering issue** (skip rule before notify/other rules with overlapping match) — Gotify notification about the conflict, skip affected files (leave in text-input-raw), continue processing everything else

Rule ordering validation: for each `skip` rule, check if any later rule's `match` is a subset of or identical to the skip rule's `match`. If so, the later rule would never fire.

## Filter action types

- `action: skip` (default) — skip the file, move to text-input-filtered/, stop processing further rules
- `action: notify` — send Gotify notification, continue to next rule (does NOT stop processing)

Rules execute in YAML declaration order. A `skip` action terminates rule evaluation for that file.

## YAML config schema

```yaml
# --- Filters ---
filters:
  - match:           # required, at least one field
      <field>:       # from, title, source_url, source_kind, source_name, intake_type
        contains: str        # case-insensitive
        # OR
        not_contains: str    # case-insensitive
    action: skip | notify    # optional, default: skip
    reason: str              # required
    llm_check: str           # optional — freeform prompt sent to Gemini with title+content
    notify:                  # optional (required if action: notify)
      priority: int          # required
      title: str             # required

# --- General cleaning (all default true, only need to specify overrides) ---
general_cleaning:
  <cleaning_key>: bool       # override default
  overrides:                 # optional, per-source overrides
    - match: {same as filter match}
      <cleaning_key>: bool

# Valid cleaning keys:
#   url_removal, triple_dash_removal, legal_bracket_unwrap,
#   empty_bracket_removal, whitespace_collapse, unsubscribe_removal,
#   view_online_removal, substack_refs_removal, standalone_at_removal,
#   beehiiv_plaintext_conversion, beehiiv_emphasis_removal,
#   end_of_line_punctuation

# --- Source-specific text removals ---
text_removals:
  - pattern: str             # required, valid regex
    reason: str              # required
    flags: str | list[str]   # optional: ignorecase, multiline, dotall

# --- Pronunciation / text replacements ---
text_replacements:
  - pattern: str             # required, valid regex
    replacement: str         # required
    reason: str              # required
    flags: str | list[str]   # optional: ignorecase, multiline, dotall
```

## Stats JSON (daily, 12-month retention)

File: `prepare-text/stats/YYYY-MM-DD.json`

```json
{
  "2026-03-09T14:20:03": {
    "file": "20260309-...-Law Dork-....txt",
    "raw_archive": "prepare-text/text-input-raw-archive/20260309-....txt",
    "cleaned_archive": "prepare-text/text-input-cleaned-archive/20260309-....txt",
    "filtered_archive": null,
    "filters_checked": ["Jessica Valenti: only weekly roundup", "K-Culture: skip BTS"],
    "filters_matched": [],
    "text_removals": {"NYT advertisement lines": {"matches": 1}},
    "text_replacements": {},
    "general_cleaning": {"url_removal": {"matches": 14}, "empty_bracket_removal": {"matches": 7}},
    "outcome": "cleaned",
    "chars_before": 8432,
    "chars_after": 7891
  }
}
```

For filtered files: `cleaned_archive` is null, `filtered_archive` has the path, `outcome` is "filtered".

## Changes to text-to-speech/text_to_speech.py

- Remove `clean_text()` entirely
- Read from `prepare-text/text-input-cleaned/` instead of `text-input/`
- Keep: `split_metadata()`, chunking, TTS API calls, MP3 stitching, summary generation, ID3 tag building, file cleanup
- Add `META_INTAKE_TYPE` to the ID3 description
- Remove empty-after-cleaning and too-big holding directory logic (moved to prepare_text.py)

## Changes to process.sh

- Remove archive copy step (prepare_text.py handles it)
- Remove empty file move step
- Add: `cd prepare-text && uv sync && uv run python3 prepare_text.py`
- Update TTS to read from new location

## .gitignore additions

```
prepare-text/filters.yaml
prepare-text/stats/
```

(text-input-raw/, text-input-cleaned/, etc. already covered by **/*.txt)

## Decisions made

- YAML config format
- All cleaning/filtering/transformation in prepare_text.py, nowhere else
- Intake scripts write raw only
- Both before (raw) and after (cleaned) versions archived permanently
- filters.yaml gitignored, filters.example.yaml checked in
- Final from typing on all local variables
- General cleaning defaults all true, only in example YAML (user's YAML only has overrides)
- Daily stats JSON with 12-month retention
- Rule ordering validation: skip before overlapping rules = error, Gotify notification, affected files left in raw
- Bill Simmons: RSS writes description as content, notify rule for NFL, skip rule for all
- Author + title prepended AND appended to content
- Episode description includes META_INTAKE_TYPE (email, rss, link)
- YouTube bypass unchanged
- Hangul left as-is (not removed)

## Text removals (confirmed by user)

- NYT "Advertisement" standalone lines (mid-article)
- NYT "Supported by" standalone line
- NYT "Opinion | ..." title prefix line
- NYT letters-to-editor block
- NYT social media footer
- NYT author bios (per-columnist: Douthat, Klein — not Friedman)
- NYT "Related Content" standalone line
- Substack "reader-supported publication" nag
- Substack "This post is public so feel free to share it."
- Garbage Day "Any typos in this email are on purpose actually"
- Garbage Day "TikTok Video: " embed placeholder
- Garbage Day "Instagram: " embed placeholder
- K-Culture copyright line
- K-Culture unicode small-caps promotional text
- Substack "substacks referenced above" block + standalone @ lines
- "View this post on the web at" text
- Beehiiv plain-text disclaimer

## Text removals NOT added (user declined)

- Garbage Day "P.S. here's {something}" lines
- K-Culture Zoom meet-up promo
