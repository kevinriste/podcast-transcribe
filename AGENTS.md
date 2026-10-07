# AGENTS.md

This file provides comprehensive guidance for AI agents and assistants working in this repository.

## Public Repository Standard

This repository is public and must stay generic and portable — free of the maintainer's private specifics so anyone can clone and run it. Nothing private lives in tracked files: every private or machine-specific value is supplied by a gitignored file or an environment variable. The following are gitignored, each with a committed `*.example.*` template using generic placeholders:

- **Content-source identities** — specific publications/blogs/newsletters/podcasts/authors, their feed URLs, and source-specific routing/classification. Pipeline code stays source-agnostic and config-driven. Generic *platforms* (Substack, Beehiiv) are fine — detected structurally.
- **Personal feed identity** — feed titles, descriptions, cover art, landing-page copy, and domains for the maintainer's own feeds.
- **Infrastructure & secrets** — real domains/hostnames, tokens/keys, account emails, cloud project IDs, credential filenames, absolute home paths. Scripts use `$HOME`/repo-relative paths and read secrets/locations from `.env`; ports default to `localhost:PORT`.
- **Active config & state** — real `*.yaml`/`*.json` config and runtime state; each has a committed `*.example.*` template.

Machine-specific values live in a gitignored root `.env` (copy from `.env.example`). Personal one-off/scratch scripts are not committed. The server runs `main` directly with these gitignored files — no private branch.

### Branch Conventions
Design/planning docs and specs under `docs/superpowers/` are encouraged on feature branches but **must be deleted before merging to `main`**.

---

## What This Project Does

Converts incoming emails (Substack/Beehiiv newsletters, web links, YouTube videos), RSS feeds, and blog archives into podcast episodes using Google Cloud TTS or Gemini TTS, published via Dropcaster. Runs every 20 minutes via cron (`process-caller.sh` → `process.sh`).

---

## Commands & Development Workflow

There are 6 independent uv-managed Python subprojects: `imap/`, `rss/`, `archive/`, `prepare-text/`, `text-to-speech/`, and `shared/`. Always `cd` into the subproject directory first.

```bash
# Install dependencies for all subprojects
cd imap && uv sync
cd rss && uv sync
cd archive && uv sync
cd prepare-text && uv sync
cd text-to-speech && uv sync
cd shared && uv sync

# Run pipeline components individually
cd imap && uv run python3 parse_email.py
cd rss && uv run python3 check-rss.py
cd archive && uv run python3 check-archive.py
cd prepare-text && uv run python3 prepare_text.py
cd text-to-speech && uv run python3 text_to_speech.py

# One-off audition: render a text file with a chosen Gemini TTS engine + voice
# into the feed, annotated (synchronous, not batch)
cd text-to-speech && uv run python3 audition.py "<file.txt>" --engine gemini-3.1-flash --voice Callirrhoe

# Lint and format (run from subproject or root)
uv run ruff check .
uv run ruff check --fix .
uv run ruff format .

# Type checking (run from subproject directory)
uv run basedpyright
```

Tests are script-style `test_*.py` files next to the code in each subproject (no pytest dependency, no CI). Run one with `uv run python3 test_x.py` from its subproject directory; a test that fails raises `AssertionError`. Also do manual validation for anything that depends on live services or config.

```bash
# Run every test script in every subproject
for d in imap rss archive prepare-text text-to-speech shared; do (cd $d && for t in test_*.py; do uv run python3 "$t" >/dev/null || echo "FAIL $d/$t"; done); done
```

---

## Linting & Type Checking Standards

Root `pyproject.toml` defines shared `ruff` and `basedpyright` configs; subproject `pyproject.toml` files extend them.

- **ruff**: ALL rules enabled except CPY (copyright) and specific complexity/style rules (`C901`, `PLR0911-PLR2004`, `LOG015`, `TRY300`, `COM812`, `E501`). Docstring rules (D) enabled with `D213`/`D203` ignored. Preview mode on. Target Python 3.12, line length 120.
- **ruff format**: Enabled, line length 120. `E501` is not linted — the formatter handles wrapping.
- **basedpyright**: `typeCheckingMode = "all"`, Python 3.12.8. Zero errors across all subprojects. Untyped library boundaries must be narrowed with `isinstance()`, `str()`, or `getattr()` — **never use `cast()`**.

---

## Pipeline Architecture & Execution Flow

Orchestrated by `process-caller.sh` (cron wrapper with timestamped logging) → `process.sh`.

```
IMAP (parse_email.py)   ─┐
RSS (check-rss.py)       ├─► prepare-text/text-input-raw/ ─► prepare_text.py ─► text-input-cleaned/
Archive (check-archive) ─┘                                        │
                                                                  ▼
                                                      text_to_speech.py (TTS)
                                                                  │
                                                                  ▼
                                                      dropcaster-docker/audio/ ─► Dropcaster (Docker)
```

### 1. `imap/parse_email.py` (Intake)
Fetches unseen Gmail messages. Three intake modes based on email subject:
- **Default (newsletters)**: Extracts body text, detects platform (Substack, Beehiiv), and extracts the canonical source URL.
  - For HTML-body sources (Substack, etc.), extracts body from HTML rather than lossy plain text to prevent anchor word-joins and preserve structure.
  - Recognized platforms route through structural extractor (`shared/podcast_shared/structural_extract.py`), walking HTML into an ordered `Block` tree.
  - Quotes are marked with `BLOCKQUOTE_MARKER`. Embedded content (tweets, images, videos, link cards, footnotes) is parsed into `ASIDE_MARKER` blocks.
  - Content images and tweet media are described via OpenAI Responses API vision (`shared/podcast_shared/describe.py`), gated by `EMBED_VISION` (`1`=enabled, `0`=disabled) and `EMBED_DROP_TYPES`. Vision replies `DECORATIVE` for page chrome (logos, icons, dividers, banners), and those images are dropped.
  - Vision failures are classified. An image the API refuses (a 400/422 whose code or param names the image) falls back to caption/alt text at once and is reported in a Gotify alert. A URL the API can't download is retried 3 times (5 s and 20 s waits) and then defers only that email. Everything else counts as an outage: bad key or model (401/403/404), any other invalid-request 400, or connection/5xx errors after 3 attempts. An outage defers the email and trips a per-run circuit. Once the circuit trips, or 5 minutes of vision time have been spent in the run, later emails are skipped until the next run: they stay unseen, no attempt is counted and no alert is sent, but their 24-hour clock starts. So an outage alerts once per email only when that email is actually tried, and a long outage can't hold back emails indefinitely.
  - Deferred emails live in `imap/vision-deferrals.json` (atomic writes), keyed by IMAP UID and checked against Message-ID. Descriptions already obtained are cached, including those from a skipped run, so a retry only asks for the missing ones. A deferred email is retried at most hourly. Gotify alerts once when an email is first deferred. After 24 hours it publishes, with caption/alt text for any image that still fails, plus a second alert. Entries for emails no longer unseen are pruned.
  - Publisher-specific link extraction and scraping rules are configured in `imap/sources.yaml` (`sources.example.yaml`).
- **`link`**: Fetches full article via Playwright + trafilatura. URLs matching configured authenticated domains route to the authenticated scraper (`http://localhost:3002/fetch`), others to the general scraper (`http://localhost:3001/fetch`).
- **`youtube`**: Downloads audio directly via `yt-dlp` using non-HLS audio format and Android player client (`bestaudio[protocol!=m3u8][protocol!=m3u8_native]/bestaudio/best`, `{"youtube": {"player_client": ["android"]}}`). Writes ID3 tags directly, bypassing the TTS pipeline.
- Prepends `META_` headers and writes raw text files to `prepare-text/text-input-raw/`.

### 2. `rss/check-rss.py` (Intake)
Polls feeds configured in `rss/feeds.yaml` (`feeds.example.yaml`).
- Modes: `content` (parses `entry.content`), `description` (uses entry summary for podcast-like feeds), or `full_scraper` (fetches full article via local authenticated scraper, verified by per-feed check phrases; on failure sends Gotify alert and keeps GUID for retry).
- Tracks processed GUIDs in `rss/feed-guids/<FeedTitle>.txt`.
- Writes text files with `META_` headers to `prepare-text/text-input-raw/`.

### 3. `archive/check-archive.py` (Intake)
Scheduled intake that walks a blog/archive one post per day from `archive/posts.json` (gitignored), tracking state in `state.json`.
- Source display name is configured in `archive/source.yaml` (`source.example.yaml`).
- Optional `content_selector` (CSS selector for the post body element) scopes extraction to that element (`archive/article_extract.py`). Whole-page trafilatura can pick a sidebar over a very short post; within the element, trafilatura's output is used when it covers the element's text, else the element's own text, else image title/alt text. A selector that matches nothing raises (Gotify alert) instead of publishing junk.
- When a post has sufficient comments, generates a multi-voice "Highlights From The Comments" companion episode (`archive/comment_briefing.py` using OpenAI Responses API model specified in `COMMENT_BRIEFING_MODEL`, default `gpt-5-mini`).
- Writes to `prepare-text/text-input-raw/`.

### 4. `prepare-text/prepare_text.py` (Filtering & Cleaning)
Processes raw files from `prepare-text/text-input-raw/` according to rules in `prepare-text/filters.yaml` (`filters.example.yaml`):
- **Filtering Actions**:
  - `skip`: Skips text synthesis entirely.
  - `notify`: Dispatches a Gotify push alert (can be gated by Gemini via `llm_check`).
  - `podly_process`: Whitelists the episode in Podly and skips local TTS. The episode is matched by GUID, then download URL, then exact title within the feed whose name matches `from`. Connection settings come only from `PODLY_URL`/`PODLY_USERNAME`/`PODLY_PASSWORD`. If the enable fails, the file is still filtered, the recorded reason says it failed, and a Gotify alert asks you to enable it manually.
  - Match fields: `from`, `title`, `source_url`, `source_kind`, `source_name`, `intake_type`, `guid`. Unknown filter keys are rejected at load time.
- **Cleaning**:
  - Applies general cleaning steps (URL stripping, bracket removal, whitespace collapse, Beehiiv footer/anchor cleanup).
  - Executes regex text removals (`text_removals`) and substitutions (`text_replacements`).
  - `archive-comments` episodes bypass content-mutating cleaning steps to preserve load-bearing speaker tags.
- Prepends an author/title header unless the body already leads with its byline, and writes a `LISTENING_TIME_MARKER` line where that intro ends (`shared/podcast_shared/listening_time.py`).
- Archives raw and cleaned files under `prepare-text/text-input-archive/`. Writes output to `prepare-text/text-input-cleaned/`.

### 5. `text-to-speech/text_to_speech.py` (Synthesis & ID3 Tagging)
Reads `prepare-text/text-input-cleaned/*.txt`, parses `META_` headers, and routes synthesis according to `text-to-speech/narrators.yaml` (`narrators.example.yaml`):
- **Engines**:
  - `wavenet` (default): Chunks into 3–5 kB segments and calls Google Cloud TTS (`en-US-Wavenet-F`) synchronously. If the article contains at least one `BLOCKQUOTE_MARKER`, it is rendered multi-voice (narrator + deterministically assigned quote voices from `multivoice.py`). If quote-free, markers are stripped for single-voice reading.
  - `gemini-flash` (`gemini-2.5-flash-preview-tts`), `gemini-pro` (`gemini-2.5-pro-preview-tts`), `gemini-3.1-flash` (`gemini-3.1-flash-tts-preview`): Chunks into 8–12 kB segments and submits as a Gemini Batch API job (~50% audio token discount). Parked in `text-to-speech/batch-pending/` with a state JSON. Finished batch jobs are collected on subsequent runs; failed batch jobs send a Gotify alert and fall back to WaveNet.
  - Comment episodes: Rendered multi-voice via Google Cloud TTS using `comment_voices` from `narrators.yaml` (narrator voice, quote voice pool, aside voice).
- **Listening time**: Every TTS episode (not YouTube) announces "Listening time: 2 minutes, 30 seconds." right after its author/title intro. The intro and body are synthesized separately (split at `LISTENING_TIME_MARKER`; no marker means the announcement goes first), and the quoted time is the whole episode's length, the announcement included, divided by `LISTENING_SPEED`. Comment and multi-voice episodes announce in the narrator voice. Gemini batch episodes record their intro chunk count in the batch state and announce with one synchronous Gemini call in the same voice when collected, falling back to WaveNet if that call fails.
- **Summaries**:
  - Generated via Gemini `gemini-3.1-flash-lite` (`SUMMARY_MODEL`) in 2–3 concise sentences.
- **ID3 Tags & Descriptions**:
  - Title format: `<from>- <base36_timestamp>- <title>` (or `<title>` if from is missing).
  - HTML description format:
    ```html
    <summary>
    <br/><br/>
    Title: <title>
    <br/><br/>
    Via: <IntakeType>
    <br/><br/>
    Source: <a href="<source_url>"><display_text></a>
    ```
    (For Beehiiv sources, `display_text` uses `META_SOURCE_NAME` with URL in `href`).
  - Sets file modification time (`mtime`) explicitly from `META_PUB_DATE` or filename `YYYYMMDD-HHMMSS` prefix to ensure deterministic publication dates in Dropcaster.
- **Feed Routing**:
  - Default: Topical feed (`dropcaster-docker/audio/`).
  - Evergreen: Long-form / backlog episodes routed to `dropcaster-docker/audio/<evergreen_dir>/` based on `evergreen_feed` rules in `narrators.yaml` (by whole source or word count threshold).

- **Failures**: Google TTS calls retry transient errors (503/500/429/timeouts) with backoff, under a 5-minute total deadline per request, so a hung call can't hold the run lock. A file that still fails stays in `text-input-cleaned/` for the next run. The other files are still processed, each Gemini batch job is collected on its own, and the feeds still regenerate; the run then exits nonzero so `process-caller.sh` sends a Gotify alert. If the failure is a service outage (Google Cloud TTS or Gemini: 5xx, 429, connection errors), the remaining files or batch jobs are left for the next run instead of each waiting out its own retries, and outages never count toward the limit below. A file or batch job that fails 3 runs in a row for any other reason (counted in `text-to-speech/tts-strikes.json`) is moved to `text-to-speech/tts-failed/`, with one alert. Episodes are exported to `<name>.mp3.partial`, tagged and dated, then renamed into place, so Dropcaster never publishes a half-written or untagged file; a `.partial` left by a killed run is deleted at the start of the next.

### 6. Dropcaster & Retention
- **Dropcaster (Docker)**: Runs `dropcaster` to regenerate `audio/index.rss` and `audio/evergreen/index.rss` whenever audio files change.
  - Template: `dropcaster-docker/dropcaster/templates/channel.rss.erb`.
  - RSS titles derive from ID3 title tags if present, otherwise filename.
- **Retention**:
  - Prior to Dropcaster execution, topical audio files older than `PODCAST_RETENTION_WEEKS` (default 8) are moved to `dropcaster-docker/audio-archive/` (moved, never deleted).
  - The evergreen feed directory (`audio/evergreen/`) is excluded from retention cuts and accumulates continuously.

---

## Shared Module (`shared/podcast_shared`)

Configured as an editable path dependency (`{ path = "../shared", editable = true }`) across all subprojects. Edits in `shared/` are immediately active.

Key exports:
- `get_gemini_client`: Singleton Gemini client initialized from `GEMINI_API_KEY`.
- `generate_summary`: Generates 2–3 sentence summary using `gemini-3.1-flash-lite`.
- `send_gotify_notification`: Push alerts via Gotify (intentionally fails open without swallowing errors).
- `split_metadata`: Parses `META_` header key-value lines from raw text.
- `apply_id3_tags`: Writes ID3 tags (TIT2, TT3 description, WXXX source URL) via Mutagen.
- `set_file_pub_date`, `pub_date_from_filename`: File `mtime` management for Dropcaster sorting.
- `BLOCKQUOTE_MARKER`, `ASIDE_MARKER`: Canonical marker prefixes for quotes and embed asides.
- `structural_extract.py`: Offline parser turning HTML into structured `Block` trees.
- `aside_render.py`: Renders embedded blocks into meta-narrator spoken asides.
- `describe.py`: OpenAI vision descriptions for images and tweets.
- `openai_routing.py`: Picks the OpenAI key per model (share vs noshare project) and sends billed noshare calls at the Flex tier with a standard-tier fallback.
- `podly.py`: Podly API client for remote post whitelisting and processing.

---

## Environment Variables

Configured in gitignored root `.env` (template in `.env.example`):

| Variable | Description |
| :--- | :--- |
| `GMAIL_PODCAST_ACCOUNT` | Gmail address polled for incoming podcast emails. |
| `GMAIL_PODCAST_ACCOUNT_APP_PASSWORD` | App-specific password for Gmail IMAP access. |
| `GEMINI_API_KEY` | Gemini API key for summaries, LLM filter checks, and Gemini TTS. |
| `GOOGLE_APPLICATION_CREDENTIALS` | Absolute path to Google Cloud service account JSON for WaveNet TTS. |
| `OPENAI_API_KEY` | OpenAI API key for comment briefings and embed image vision descriptions. |
| `OPENAI_API_KEY_NOSHARE` | (Optional) Key for a separate OpenAI project with data sharing off; `NOSHARE_MODELS` use it and are billed. |
| `NOSHARE_MODELS` | (Optional) Comma-separated models routed to `OPENAI_API_KEY_NOSHARE` (default `gpt-6-luna`). |
| `OPENAI_FLEX` | Set `0` to stop sending billed noshare calls at the Flex tier (default on; a Flex 429 or timeout falls back to the standard tier). |
| `OPENAI_FLEX_TIMEOUT` | (Optional) Seconds a Flex request may take before the standard-tier fallback (default `900`; vision uses 60 to stay inside its per-run time budget). |
| `COMMENT_BRIEFING_MODEL` | (Optional) Model for comment briefing summaries (default `gpt-5-mini`). |
| `EMBED_VISION` | Set `0` to disable OpenAI vision descriptions of embed images (default enabled). |
| `EMBED_VISION_MODEL` | (Optional) Model for vision descriptions (default `gpt-6-luna`). |
| `EMBED_DROP_TYPES` | (Optional) Comma-separated embed types to drop (e.g. `video,card`). |
| `GOTIFY_SERVER` | Base URL of Gotify push server (`https://gotify.example.com`). |
| `GOTIFY_TOKEN` | Application token for Gotify notifications. |
| `PODLY_URL` | Base URL for Podly server (`http://localhost:5001`). |
| `PODLY_USERNAME` | Username for Podly authentication. |
| `PODLY_PASSWORD` | Password for Podly authentication. |
| `PODCAST_DOMAIN_PRIMARY` | Primary domain for Dropcaster RSS feed URLs. Required: `process.sh` fails before regenerating feeds if it is empty. |
| `PODCAST_DOMAIN_SECONDARY` | Secondary domain for feed mirrors. |
| `PODCAST_RETENTION_WEEKS` | Weeks of audio to keep in topical feed before archiving (default `8`). |
| `LISTENING_SPEED` | Playback speed the spoken listening time is quoted at (default `1.0`). |
| `TZ` | Timezone for log timestamps (default `UTC`). |
| `LOG_DIR` | Per-run execution log directory for `process-caller.sh`. |
| `GMAIL_PRIMARY_ACCOUNT`, `CF_TOKEN`, `CF_ACCOUNT_ID` | Cloudflare DNS and email for ACME certificate renewal. |

---

## Key Paths & State Directories

- **Intake raw inputs**: `prepare-text/text-input-raw/`
- **Cleaned text inputs**: `prepare-text/text-input-cleaned/`
- **Input text archive**: `prepare-text/text-input-archive/`
- **RSS GUID history**: `rss/feed-guids/<FeedTitle>.txt`
- **Gemini batch jobs in-flight**: `text-to-speech/batch-pending/`
- **TTS token usage stats**: `text-to-speech/stats/YYYY-MM-DD.json`
- **Published audio**: `dropcaster-docker/audio/`
- **Evergreen feed audio**: `dropcaster-docker/audio/evergreen/`
- **Archived audio**: `dropcaster-docker/audio-archive/`
- **Vision deferrals** (emails awaiting image descriptions): `imap/vision-deferrals.json`

---

## Common Administrative Tasks

- **Reprocess an RSS item**: Edit `rss/feed-guids/<FeedTitle>.txt` and revert/delete the latest GUID.
- **Audition a Gemini TTS voice**:
  ```bash
  cd text-to-speech && uv run python3 audition.py "<file.txt>" --engine gemini-3.1-flash --voice Callirrhoe
  ```
- **Force reinstating editable shared module**:
  ```bash
  uv sync --reinstall-package podcast-shared
  ```
- **Deploying a new instance**:
  1. `cp .env.example .env` and populate secrets.
  2. Copy each `*.example.*` file to its corresponding active file:
     - `rss/feeds.example.yaml` → `rss/feeds.yaml`
     - `imap/sources.example.yaml` → `imap/sources.yaml`
     - `archive/source.example.yaml` → `archive/source.yaml`
     - `prepare-text/filters.example.yaml` → `prepare-text/filters.yaml`
     - `text-to-speech/narrators.example.yaml` → `text-to-speech/narrators.yaml`
     - `dropcaster-docker/audio/channel.example.yml` → `dropcaster-docker/audio/channel.yml`
  3. Place `posts.json` in `archive/` if running archive intake.
  4. For the TLS reverse proxy, copy `nginx-proxy-certbot-docker/.env.example` to `nginx-proxy-certbot-docker/.env`. Compose fills `${PODCAST_DOMAIN_PRIMARY}`, `${GMAIL_PRIMARY_ACCOUNT}`, `${CF_TOKEN}` and `${CF_ACCOUNT_ID}` from that file, not from the root `.env` (which Compose can't parse). Without it, a recreated container gets empty values.
