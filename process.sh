#!/bin/bash

FIRST_LOG_DATE=$1

error_handler() {
    local exit_code=$?
    local debug_message=$(echo "Podcast Transcribe error: '$BASH_COMMAND' with exit code $exit_code")
    local debug_output=$(awk -v date="$FIRST_LOG_DATE" 'print_next {print} $0 ~ date {print; print_next=1}' /home/flog99/process-log.log)

    echo "Main--Exit code error- sending notification with Gotify"
    # Send message to Gotify, don't display anything if successful
    curl --silent --show-error "$GOTIFY_SERVER/message?token=$GOTIFY_TOKEN" -F "title=$debug_message" -F "message=$debug_output" -F "priority=9" > /dev/null

    echo "Main--$debug_message"
    echo "Main--End Script (failure)"

    exit $exit_code
}

# Trap any error signal (ERR) and call the error_handler function
trap error_handler ERR

# Enable the script to exit if any command returns a non-zero status
set -e

echo "Main--Start Script"
export PYENV_ROOT="/home/flog99/.pyenv"
command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
cd /home/flog99/dev/podcast-transcribe/imap
echo "Main--Install IMAP Parse Emails dependencies"
/home/flog99/.local/bin/uv sync
echo "Main--Run IMAP Parse Emails script"
/home/flog99/.local/bin/uv run python3 parse_email.py
echo "Main--Ensure Playwright is up to date"
/home/flog99/.local/bin/uv run playwright install
cd /home/flog99/dev/podcast-transcribe/rss
echo "Main--Install Parse RSS dependencies"
/home/flog99/.local/bin/uv sync
echo "Main--Ensure Playwright is up to date"
/home/flog99/.local/bin/uv run playwright install
echo "Main--Run Parse RSS script"
/home/flog99/.local/bin/uv run python3 check-rss.py
cd ..
export GOOGLE_APPLICATION_CREDENTIALS=/home/flog99/dev/podcast-transcribe/EmailPodcast-c69d63681230.json
echo "Main--Archive a copy of input text files"
mkdir -p text-to-speech/input-text-archive
if compgen -G "text-to-speech/text-input/*.txt" > /dev/null; then
    cp -n text-to-speech/text-input/*.txt text-to-speech/input-text-archive/
fi
cd text-to-speech
echo "Main--Remove empty text files"
find ./text-input -size 0 -exec  mv {}  ./text-input-empty-files/ \;
echo "Main--Install Google Text to Speech dependencies"
/home/flog99/.local/bin/uv sync
echo "Main--Run Google Text to Speech script"
/home/flog99/.local/bin/uv run python3 text_to_speech.py
cd ..
cd dropcaster-docker
echo "Main--Archive audio files older than 8 weeks (weekly cutoff)"
weekly_cutoff=$(date -d "last monday -56 days" +%Y-%m-%d)
archive_dir="./audio-archive"
mkdir -p "$archive_dir"
find ./audio -type f -name "*.mp3" ! -newermt "$weekly_cutoff" -print -exec mv {} "$archive_dir" \;
echo "Main--Check if podcast files changed"
newHash=$(ls -lhaAgGR --block-size=1 --time-style=+%s ./audio | sed -re 's/^[^ ]* //' | sed -re 's/^[^ ]* //' | tail -n +3 | sha1sum)
if [ -f audio-hash.txt ]; then
    oldHash=$(cat audio-hash.txt)
else
    oldHash=""
fi
if [ "$newHash" != "$oldHash" ]; then
    echo "Main--Run Dropcaster"
    start=$(date +%s)
    docker compose --file ./docker-compose-local.yml down --remove-orphans
    docker compose --file ./docker-compose-local.yml build
    docker compose --file ./docker-compose-local.yml run dropcaster dropcaster --parallel_type processes --parallel_level 8 --url "https://${PODCAST_DOMAIN_PRIMARY}" > ./new-index.rss
    cp ./new-index.rss ./audio/index.rss
    ls -lhaAgGR --block-size=1 --time-style=+%s ./audio | sed -re 's/^[^ ]* //' | sed -re 's/^[^ ]* //' | tail -n +3 | sha1sum > ./audio-hash.txt
    end=$(date +%s)
    printf 'Dropcaster processing time: %.2f minutes\n' $(echo "($end-$start)/60.0" | bc -l)
fi
echo "Main--Clean up Docker"
docker container prune -f
docker volume prune -f
echo "Main--End Script (success)"
