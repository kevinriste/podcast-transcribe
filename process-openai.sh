#!/bin/bash
echo $(TZ=America/Chicago date --iso-8601=seconds)"--OpenAI--Start Script"
export PYENV_ROOT="/home/flog99/.pyenv"
command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
cd /home/flog99/dev/podcast-transcribe/text-to-speech-openai
echo $(TZ=America/Chicago date --iso-8601=seconds)"--OpenAI--Install OpenAI Text to Speech dependencies"
pipenv install
echo $(TZ=America/Chicago date --iso-8601=seconds)"--OpenAI--Run OpenAI Text to Speech script"
pipenv run python3 text_to_speech_openai.py
cd ..
cd dropcaster-docker
echo $(TZ=America/Chicago date --iso-8601=seconds)"--OpenAI--Check if podcast files changed"
echo $(ls -lhaAgGR --block-size=1 --time-style=+%s ./audio-openai | sed -re 's/^[^ ]* //' | sed -re 's/^[^ ]* //' | tail -n +3 | sha1sum) > ./audio-openai-hash-new.txt
newHash=$(cat audio-openai-hash-new.txt)
oldHash=$(cat audio-openai-hash.txt)
if [ "$newHash" != "$oldHash" ]; then
    echo $(TZ=America/Chicago date --iso-8601=seconds)"--OpenAI--Run OpenAI Dropcaster"
    start=$(date +%s)
    docker compose --file ./docker-compose.openai.yml run dropcaster dropcaster --parallel_type processes --parallel_level 8 --url "https://${PODCAST_DOMAIN_SECONDARY}" > ./new-index-openai.rss
    cp ./new-index-openai.rss ./audio-openai/index.rss
    echo $(ls -lhaAgGR --block-size=1 --time-style=+%s ./audio-openai | sed -re 's/^[^ ]* //' | sed -re 's/^[^ ]* //' | tail -n +3 | sha1sum) > ./audio-openai-hash.txt
    end=$(date +%s)
    printf 'OpenAI Dropcaster processing time: %.2f minutes\n' $(echo "($end-$start)/60.0" | bc -l)
fi
echo $(TZ=America/Chicago date --iso-8601=seconds)"--OpenAI--End Script"
