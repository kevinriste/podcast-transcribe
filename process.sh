#!/bin/bash

FIRST_LOG_DATE=$1
RUN_LOG=${RUN_LOG:-}
if [ -n "$RUN_LOG" ]; then
    mkdir -p "$(dirname "$RUN_LOG")"
    timestamp_output() {
        while IFS= read -r line; do
            printf '%s %s\n' "$(TZ="${TZ:-UTC}" date +%FT%T.%3N%:z)" "$line"
        done
    }
    # Mirror stdout/stderr to the per-run log for reliable error reporting.
    exec > >(tee -a "$RUN_LOG" | timestamp_output) 2>&1
fi

# Enable the script to exit if any command returns a non-zero status
set -e

# Resolve the repo root from this script's location (portable, no absolute paths).
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load machine-specific config/secrets (see .env.example).
if [ -f "$REPO_DIR/.env" ]; then set -a; . "$REPO_DIR/.env"; set +a; fi

echo "Main--Start Script"
export PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
cd "$REPO_DIR/imap"
echo "Main--Install IMAP Parse Emails dependencies"
uv sync
echo "Main--Ensure Playwright is up to date"
uv run playwright install
echo "Main--Run IMAP Parse Emails script"
uv run python3 parse_email.py
cd "$REPO_DIR/rss"
echo "Main--Install Parse RSS dependencies"
uv sync
echo "Main--Ensure Playwright is up to date (RSS)"
uv run playwright install
echo "Main--Run Parse RSS script"
uv run python3 check-rss.py
cd "$REPO_DIR/archive"
echo "Main--Install Archive intake dependencies"
uv sync
echo "Main--Run Archive intake script"
uv run python3 check-archive.py
cd "$REPO_DIR/prepare-text"
echo "Main--Install Prepare Text dependencies"
uv sync
echo "Main--Run Prepare Text script"
uv run python3 prepare_text.py
cd "$REPO_DIR/text-to-speech"
echo "Main--Install Google Text to Speech dependencies"
uv sync
echo "Main--Run Google Text to Speech script"
# A TTS failure must not block publishing the episodes that did succeed: remember it,
# regenerate the feeds, then fail the run at the end so the Gotify alert still fires.
tts_status=0
uv run python3 text_to_speech.py || tts_status=$?
cd "$REPO_DIR/dropcaster-docker"
# Retention window for the topical feed, in weeks (default 8). Older audio is
# moved to ./audio-archive. Override via PODCAST_RETENTION_WEEKS in .env.
retention_weeks="${PODCAST_RETENTION_WEEKS:-8}"
echo "Main--Archive audio files older than ${retention_weeks} weeks (weekly cutoff)"
weekly_cutoff=$(date -d "last monday -$((retention_weeks * 7)) days" +%Y-%m-%d)
archive_dir="./audio-archive"
mkdir -p "$archive_dir"
# -maxdepth 1 keeps the archiver on the topical feed only: the evergreen feed
# (./audio/evergreen) is the long-form backlog and is meant to accumulate.
find ./audio -maxdepth 1 -type f -name "*.mp3" ! -newermt "$weekly_cutoff" -print -exec mv {} "$archive_dir" \;
echo "Main--Check if podcast files changed"
newHash=$(ls -lhaAgGR --block-size=1 --time-style=+%s ./audio | sed -re 's/^[^ ]* //' | sed -re 's/^[^ ]* //' | tail -n +3 | sha1sum)
if [ -f audio-hash.txt ]; then
    oldHash=$(cat audio-hash.txt)
else
    oldHash=""
fi
if [ "$newHash" != "$oldHash" ]; then
    # An empty domain would publish enclosure URLs like "https:///x.mp3"; fail the run
    # instead (the old feed and hash stay in place, so the next run retries).
    : "${PODCAST_DOMAIN_PRIMARY:?PODCAST_DOMAIN_PRIMARY is unset; refusing to regenerate feeds}"
    echo "Main--Run Dropcaster"
    start=$(date +%s)
    docker compose down --remove-orphans
    echo "Main--Ensure Dropcaster image is built (no-op container run)"
    docker compose --progress quiet run --rm --remove-orphans dropcaster /bin/true
    docker compose run --rm dropcaster dropcaster --parallel_type processes --parallel_level 8 --url "https://${PODCAST_DOMAIN_PRIMARY}" > ./new-index.rss
    cp ./new-index.rss ./audio/index.rss
    echo "Main--Run Dropcaster (evergreen feed)"
    docker compose run --rm dropcaster dropcaster --parallel_type processes --parallel_level 8 --url "https://${PODCAST_DOMAIN_PRIMARY}/evergreen" evergreen > ./new-index-evergreen.rss
    cp ./new-index-evergreen.rss ./audio/evergreen/index.rss
    ls -lhaAgGR --block-size=1 --time-style=+%s ./audio | sed -re 's/^[^ ]* //' | sed -re 's/^[^ ]* //' | tail -n +3 | sha1sum > ./audio-hash.txt
    end=$(date +%s)
    printf 'Dropcaster processing time: %.2f minutes\n' $(echo "($end-$start)/60.0" | bc -l)
fi
if [ "$tts_status" -ne 0 ]; then
    echo "Main--Text to Speech failed (exit $tts_status); feeds were still regenerated"
    exit "$tts_status"
fi
echo "Main--End Script (success)"
