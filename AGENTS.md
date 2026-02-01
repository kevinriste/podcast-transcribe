## Project Summary
- Goal: Convert incoming emails/RSS into podcast episodes with TTS, and publish via Dropcaster.
- Inputs: IMAP email (plain, link, youtube), RSS feeds.
- Outputs: MP3 files in `dropcaster-docker/audio`, RSS in `dropcaster-docker/audio/index.rss`.

## Key Flow
- `process-caller.sh` is the cron entrypoint; it timestamps logs and calls `process.sh`.
- `process.sh` orchestrates IMAP, RSS, TTS, Dropcaster, cleanup.
- IMAP parsing: `imap/parse_email.py`
  - "link" subject: fetch full article via local scraper and write text input.
  - "youtube" subject: download audio via yt-dlp.
  - Other emails: treat as newsletter; Beehiiv uses the “Read Online” link for source URL; Substack uses link extraction.
  - Metadata is prepended to text input: `META_TITLE`, `META_SOURCE_URL`, `META_SOURCE_KIND`, `META_SOURCE_NAME`.
- RSS parsing: `rss/check-rss.py`
  - Writes text input + metadata and tracks GUIDs in `rss/feed-guids/`.
  - Uses local scraper at `http://localhost:3002/fetch`.

## Summaries + Descriptions
- Summary model: `gpt-5-mini`.
- Summary length: 2–3 sentences.
- Description format (Google TTS path):
  - Summary
  - `Title: <title>`
  - `Source: <a href="...">...</a>`
  - Uses `<br/><br/>` separators.
- Beehiiv link text uses `META_SOURCE_NAME` (URL still in href).

## Dropcaster
- Template: `dropcaster-docker/dropcaster/templates/channel.rss.erb` (no overrides).
- RSS titles come from ID3 title when present; otherwise filename.

## Audio Retention
- Weekly cutoff before Dropcaster: files older than 8 weeks moved to `dropcaster-docker/audio-archive`.
- Logic lives in `process.sh`.

## YouTube Notes
- yt-dlp configured to prefer non-HLS audio and Android client:
  - `format`: `bestaudio[protocol!=m3u8][protocol!=m3u8_native]/bestaudio/best`
  - `extractor_args`: `{"youtube": {"player_client": ["android"]}}`
  - retries enabled.
- If SABR/HLS warnings reappear, consider changing player client or adding a PO token.

## Useful Paths
- Text inputs: `text-to-speech/text-input`
- Empty inputs: `text-to-speech/text-input-empty-files`
- Oversized inputs: `text-to-speech/text-input-too-big`
- Input text archive: `text-to-speech/input-text-archive`
- RSS GUID tracking: `rss/feed-guids/`
- Audio output: `dropcaster-docker/audio`
- Archive: `dropcaster-docker/audio-archive`

## Env Vars
- `GMAIL_PODCAST_ACCOUNT`, `GMAIL_PODCAST_ACCOUNT_APP_PASSWORD`
- `OPENAI_API_KEY`
- `GOTIFY_SERVER`, `GOTIFY_TOKEN`
- `PODCAST_DOMAIN_PRIMARY`, `PODCAST_DOMAIN_SECONDARY`
- `GOOGLE_APPLICATION_CREDENTIALS` (Google TTS)
- `GMAIL_PRIMARY_ACCOUNT`, `CF_TOKEN`, `CF_ACCOUNT_ID` (nginx-proxy + ACME)

## Common Tasks
- Reprocess RSS entry: edit `rss/feed-guids/<FeedTitle>.txt` to older GUID.
- Run RSS parser: `cd rss && /home/flog99/.local/bin/uv run python3 check-rss.py`
- Run IMAP parser: `cd imap && /home/flog99/.local/bin/uv run python3 parse_email.py`
