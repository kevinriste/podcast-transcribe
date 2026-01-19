# Podcast transcription service

## Purpose

I created this in order to consume Substack subscriptions in the form of a podcast, because I'm much more effective at listening to podcasts than I am at reading emails.

## How it works

- `process-caller.sh` is the cron entrypoint and adds timestamps to logs.
- `process.sh` orchestrates the pipeline every 20 minutes:
  - IMAP parsing (`imap/parse_email.py`) downloads unseen emails and writes text inputs with metadata headers.
  - RSS parsing (`rss/check-rss.py`) reads `rss/feeds.txt`, fetches new items, and writes text inputs with metadata headers.
  - Google TTS (`text-to-speech/text_to_speech.py`) chunks text, generates MP3s, writes ID3 tags, and moves audio into `dropcaster-docker/audio`.
  - Dropcaster renders the RSS feed when audio changes.
  - Audio older than 8 weeks is archived to `dropcaster-docker/audio-archive` before Dropcaster runs.

Optional scripts:
- `process-openai.sh` runs OpenAI TTS into `dropcaster-docker/audio-openai`.
- `process-aws.sh` runs AWS Polly TTS into `dropcaster-docker/audio-aws`.

## Runtime requirements

- Ubuntu + cron (current schedule: `*/20 * * * * bash -l /home/flog99/dev/podcast-transcribe/process-caller.sh >> /home/flog99/process-log.log 2>&1`)
- Docker + Docker Compose (Dropcaster and nginx-proxy stack).
- Python via `pyenv` + `uv` for IMAP/RSS/Google TTS (pyproject requires >=3.9).
- `pipenv` for the OpenAI/AWS TTS scripts (Pipfiles require Python 3.10).
- Playwright browsers installed for IMAP/RSS fetches.
- `ffmpeg` available on PATH (required by `pydub`).
- Local article scraper services:
  - IMAP link mode: `http://localhost:3001/fetch?url=...`
  - RSS fetcher: `http://localhost:3002/fetch?url=...`

## Paths

- Text inputs: `text-to-speech/text-input`
- Empty inputs: `text-to-speech/text-input-empty-files`
- Oversized inputs: `text-to-speech/text-input-too-big`
- RSS GUID tracking: `rss/feed-guids/`
- Audio output: `dropcaster-docker/audio`
- Archive: `dropcaster-docker/audio-archive`
- Logs (cron): `/home/flog99/process-log.log`

## Summaries and descriptions

- Summary model: `gpt-5-mini` (2–3 sentences).
- Google TTS builds the ID3 description as:
  - Summary
  - `Title: <title>`
  - `Source: <a href="...">...</a>`
  - Separator: `<br/><br/>`
- OpenAI and AWS Polly scripts currently do not add summaries or ID3 tags.

## Environment variables and credentials

Core pipeline:
- `GMAIL_PODCAST_ACCOUNT`, `GMAIL_PODCAST_ACCOUNT_APP_PASSWORD`
- `OPENAI_API_KEY` (summaries + OpenAI TTS)
- `GOTIFY_SERVER`, `GOTIFY_TOKEN`
- `PODCAST_DOMAIN_PRIMARY`, `PODCAST_DOMAIN_SECONDARY`
- `GOOGLE_APPLICATION_CREDENTIALS` (path to the Google TTS service account JSON; `process.sh` exports this)

nginx-proxy + ACME:
- `GMAIL_PRIMARY_ACCOUNT` (DEFAULT_EMAIL for ACME)
- `CF_TOKEN`, `CF_ACCOUNT_ID` (Cloudflare DNS-01 challenge)

AWS Polly:
- AWS credentials configured for the `polly` profile (see `text-to-speech-polly/text_to_speech_polly.py`).

## Manual runs

- IMAP parser: `cd imap && /home/flog99/.local/bin/uv run python3 parse_email.py`
- RSS parser: `cd rss && /home/flog99/.local/bin/uv run python3 check-rss.py`
- Google TTS: `cd text-to-speech && /home/flog99/.local/bin/uv run python3 text_to_speech.py`
- OpenAI TTS: `bash process-openai.sh`
- AWS Polly TTS: `bash process-aws.sh`
