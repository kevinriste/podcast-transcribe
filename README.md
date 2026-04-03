# podcast-transcribe

![podcast-transcribe logo](docs/logo.png)

Convert newsletters, articles, and YouTube videos into a personal podcast feed — automatically.

Substack, Beehiiv, and RSS content is fetched, cleaned, synthesized to speech via Google Cloud TTS, and published as a podcast RSS feed. Runs every 20 minutes via cron.

## Pipeline

```mermaid
flowchart TB
    subgraph intake ["Intake"]
        direction TB
        gmail["Gmail IMAP<br/><small>unseen messages</small>"]
        rss_feeds["RSS Feeds<br/><small>NYT, Megaphone</small>"]

        gmail --> email_route{subject?}
        email_route -->|default| newsletters["Newsletter Parser<br/><small>Substack / Beehiiv detection<br/>source URL extraction</small>"]
        email_route -->|"link"| link_fetch["Article Fetcher<br/><small>Playwright + trafilatura<br/>via localhost:3001</small>"]
        email_route -->|"youtube"| yt["yt-dlp<br/><small>download + FFmpeg → MP3</small>"]

        rss_feeds --> rss_parse["RSS Parser<br/><small>feedparser + BeautifulSoup<br/>GUID tracking</small>"]
    end

    subgraph prep ["Prepare Text"]
        direction TB
        raw_files[("text-input-raw/<br/><small>META_ headers + body</small>")]
        filters["YAML Filters<br/><small>skip / notify / LLM check</small>"]
        cleaning["General Cleaning<br/><small>URL removal, whitespace collapse<br/>bracket cleanup, unsubscribe removal<br/>end-of-line punctuation</small>"]
        removals["Text Removals & Replacements<br/><small>YAML-configured regexes<br/>image captions, disclaimers<br/>pronunciation fixes</small>"]
        empty_check{"empty?"}
        cleaned_files[("text-input-cleaned/")]

        raw_files --> filters
        filters -->|matched| filtered[/"filtered/"/]
        filters -->|passed| cleaning
        cleaning --> removals
        removals --> empty_check
        empty_check -->|yes| filtered
        empty_check -->|no| cleaned_files
    end

    subgraph tts ["Text-to-Speech"]
        direction TB
        chunk["Chunk Text<br/><small>3–5k chars at punctuation</small>"]
        gcloud["Google Cloud TTS<br/><small>en-US-Wavenet-F → MP3</small>"]
        stitch["Stitch MP3s<br/><small>pydub AudioSegment</small>"]
        summary["Gemini Summary<br/><small>2–3 sentences</small>"]
        id3["ID3 Tags<br/><small>mutagen: TIT2, TT3, WXXX</small>"]
    end

    subgraph publish ["Publish"]
        direction TB
        audio_dir[("dropcaster-docker/audio/")]
        archive["Archive > 8 weeks"]
        dropcaster["Dropcaster<br/><small>Docker, regenerates index.rss<br/>only when audio changes</small>"]
        feed["Podcast RSS Feed"]

        audio_dir --> archive
        audio_dir --> dropcaster
        dropcaster --> feed
    end

    newsletters --> raw_files
    link_fetch --> raw_files
    rss_parse --> raw_files
    yt -->|"direct MP3 + ID3"| audio_dir

    cleaned_files --> chunk
    chunk --> gcloud
    gcloud --> stitch
    stitch --> summary
    summary --> id3
    id3 --> audio_dir

    style intake fill:#1a1a2e,stroke:#e94560,color:#eee
    style prep fill:#1a1a2e,stroke:#0f3460,color:#eee
    style tts fill:#1a1a2e,stroke:#533483,color:#eee
    style publish fill:#1a1a2e,stroke:#16213e,color:#eee
    style filtered fill:#2d1b1b,stroke:#e94560,color:#e94560
    style feed fill:#0f3460,stroke:#e94560,color:#eee
```

## Sources

| Source | Method | Details |
|--------|--------|---------|
| **Substack/Beehiiv newsletters** | Gmail IMAP | Auto-detected via headers and link patterns |
| **Article links** | Email with subject "link" | Fetched via headless Chromium (Playwright) + trafilatura |
| **YouTube** | Email with subject "youtube" | Audio downloaded via yt-dlp, bypasses TTS pipeline |
| **NYT columns** | RSS via Wayback Machine | Ross Douthat, Ezra Klein |
| **Bill Simmons Podcast** | RSS (Megaphone) | Description extracted, NFL episodes flagged via Gemini LLM |

## Text Format

All intermediate files use a simple plain-text format: `META_` header lines, a blank line, then the content body.

```
META_FROM: Author Name
META_TITLE: Article Title
META_SOURCE_URL: https://...
META_SOURCE_KIND: substack
META_INTAKE_TYPE: email

Article text content starts here...
```

## Processing

**Filters** (YAML-configured in `filters.yaml`): Match on metadata fields with `contains`/`not_contains` operators. Actions: `skip` (discard) or `notify` (Gotify push). Optional `llm_check` for fuzzy matching via Gemini.

**Cleaning** (all enabled by default, per-source overrides): URL removal, triple-dash removal, legal bracket unwrap, empty bracket/paren removal, whitespace collapse, unsubscribe/view-online block removal, Substack refs removal, Beehiiv markdown conversion, end-of-line punctuation for TTS pausing.

**Text removals/replacements**: YAML-configured regexes for image captions, disclaimers, pronunciation fixes (e.g. Keynesian -> Cainzeean).

## Speech Synthesis

- **API**: Google Cloud Text-to-Speech (`texttospeech.TextToSpeechClient`)
- **Voice**: `en-US-Wavenet-F`, MP3 encoding
- **Chunking**: 3,000-5,000 characters, split at punctuation/whitespace boundaries
- **Stitching**: pydub `AudioSegment` concatenation
- **Summary**: Gemini `gemini-3.1-flash-lite-preview` generates a 2-3 sentence description

## Tagging & Publishing

- **ID3 tags** via mutagen (v1 + v2): `TIT2` (title), `TT3` (description with summary + source link), `WXXX` (source URL)
- **Feed generation**: Dropcaster (Ruby, Docker) reads MP3 ID3 tags and generates `index.rss`
- **Lifecycle**: Audio older than 8 weeks is archived weekly

## Project Structure

```
podcast-transcribe/
  imap/              # Email intake (parse_email.py)
  rss/               # RSS intake (check-rss.py)
  prepare-text/      # Filtering + cleaning (prepare_text.py, filters.yaml)
  text-to-speech/    # TTS + tagging (text_to_speech.py)
  shared/            # Shared utilities (podcast_shared/)
  dropcaster-docker/ # Feed generation + audio hosting
  process.sh         # Pipeline orchestration
```

Each subdirectory is an independent Python project managed by [uv](https://github.com/astral-sh/uv).

## Requirements

- Python 3.12+ via pyenv + uv
- Docker + Docker Compose (Dropcaster)
- Playwright browsers (`uv run playwright install`)
- ffmpeg on PATH (pydub dependency)
- Local scraper service at `localhost:3001` (article fetching)
- Gmail app password, Gemini API key, Google Cloud TTS service account, Gotify server
