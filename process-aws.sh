#!/bin/bash
echo $(TZ=America/Chicago date --iso-8601=seconds)"--AWS--Start Script"
cd /home/flog99/dev/podcast-transcribe/text-to-speech-polly
echo $(TZ=America/Chicago date --iso-8601=seconds)"--AWS--Install AWS Text to Speech dependencies"
/usr/bin/pipenv install
echo $(TZ=America/Chicago date --iso-8601=seconds)"--AWS--Run AWS Text to Speech script"
/usr/bin/pipenv run python3 text_to_speech_polly.py
cd ..
cd dropcaster-docker
echo $(TZ=America/Chicago date --iso-8601=seconds)"--AWS--Check if podcast files changed"
echo $(ls -lhaAgGR --block-size=1 --time-style=+%s ./audio-aws | sed -re 's/^[^ ]* //' | sed -re 's/^[^ ]* //' | tail -n +3 | sha1sum) > ./audio-aws-hash-new.txt
newHash=$(cat audio-aws-hash-new.txt)
oldHash=$(cat audio-aws-hash.txt)
if [ "$newHash" != "$oldHash" ]; then
    cp audio-aws-hash-new.txt audio-aws-hash.txt
    echo $(TZ=America/Chicago date --iso-8601=seconds)"--AWS--Run AWS Dropcaster"
    cd audio-aws
    /usr/local/bin/docker-compose --file ./docker-compose.aws.yml run dropcaster dropcaster --url "${PODCAST_DOMAIN_SECONDARY}" > ./new-index-aws.rss
    cp ./new-index-aws.rss ./audio-aws/index.rss
fi
echo $(TZ=America/Chicago date --iso-8601=seconds)"--AWS--End Script"
