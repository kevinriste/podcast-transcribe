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
pipenv install
echo "Main--Run IMAP Parse Emails script"
pipenv run python3 parse_email.py
cd /home/flog99/dev/podcast-transcribe/rss
echo "Main--Install Parse RSS dependencies"
pipenv install
echo "Main--Run Parse RSS script"
pipenv run python3 check-rss.py
cd ..
export GOOGLE_APPLICATION_CREDENTIALS=/home/flog99/dev/podcast-transcribe/EmailPodcast-c69d63681230.json
echo "Main--Copy email text to be in Google, AWS and OpenAI directories"
cp -r text-to-speech/text-input text-to-speech-polly
cp -r text-to-speech/text-input text-to-speech-openai
cd text-to-speech
echo "Main--Remove empty text files"
find ./text-input -size 0 -exec  mv {}  ./text-input-empty-files/ \;
echo "Main--Install Google Text to Speech dependencies"
pipenv install
echo "Main--Run Google Text to Speech script"
pipenv run python3 text_to_speech.py
cd ..
cd dropcaster-docker
echo "Main--Check if podcast files changed"
newHash=$(ls -lhaAgGR --block-size=1 --time-style=+%s ./audio | sed -re 's/^[^ ]* //' | sed -re 's/^[^ ]* //' | tail -n +3 | sha1sum)
if [ -f audio-hash.txt ]; then
    oldHash=$(cat audio-hash.txt)
else
    oldHash=""
fi
if [ "$newHash" != "$oldHash" ]; then
    echo "Main--Run Google Dropcaster"
    start=$(date +%s)
    docker compose --file ./docker-compose-local.yml run dropcaster dropcaster --parallel_type processes --parallel_level 8 --url "https://${PODCAST_DOMAIN_PRIMARY}" > ./new-index.rss
    cp ./new-index.rss ./audio/index.rss
    echo $(ls -lhaAgGR --block-size=1 --time-style=+%s ./audio | sed -re 's/^[^ ]* //' | sed -re 's/^[^ ]* //' | tail -n +3 | sha1sum) > ./audio-hash.txt
    end=$(date +%s)
    printf 'Dropcaster processing time: %.2f minutes\n' $(echo "($end-$start)/60.0" | bc -l)
fi
echo "Main--Send IP to Google DNS"
curl "https://${GOOGLE_DOMAIN_1_KEY}@domains.google.com/nic/update?hostname=${GOOGLE_DOMAIN_1}"
curl "https://${GOOGLE_DOMAIN_2_KEY}@domains.google.com/nic/update?hostname=${GOOGLE_DOMAIN_2}"
echo ""
echo "Main--Clean up Docker"
docker system prune -f
echo "Main--End Script (success)"
