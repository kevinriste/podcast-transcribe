#!/bin/bash
echo $(TZ=America/Chicago date --iso-8601=seconds)"--Main--Start Script"
cd /home/flog99/dev/podcast-transcribe/imap
echo $(TZ=America/Chicago date --iso-8601=seconds)"--Main--Install IMAP Parse Emails dependencies"
/usr/bin/pipenv install
echo $(TZ=America/Chicago date --iso-8601=seconds)"--Main--Run IMAP Parse Emails script"
/usr/bin/pipenv run python3 parse_email.py
cd ..
export GOOGLE_APPLICATION_CREDENTIALS=/home/flog99/dev/podcast-transcribe/EmailPodcast-c69d63681230.json
echo $(TZ=America/Chicago date --iso-8601=seconds)"--Main--Copy email text to be in Google and AWS directories"
cp -r text-to-speech/text-input text-to-speech-polly
cd text-to-speech
echo $(TZ=America/Chicago date --iso-8601=seconds)"--Main--Install Google Text to Speech dependencies"
/usr/bin/pipenv install
echo $(TZ=America/Chicago date --iso-8601=seconds)"--Main--Run Google Text to Speech script"
/usr/bin/pipenv run python3 text_to_speech.py
cd ..
cd dropcaster-docker
echo $(TZ=America/Chicago date --iso-8601=seconds)"--Main--Check if podcast files changed"
echo $(ls -lhaAgGR --block-size=1 --time-style=+%s ./audio | sed -re 's/^[^ ]* //' | sed -re 's/^[^ ]* //' | tail -n +3 | sha1sum) > ./audio-hash-new.txt
newHash=$(cat audio-hash-new.txt)
oldHash=$(cat audio-hash.txt)
if [ "$newHash" != "$oldHash" ]; then
    echo $(TZ=America/Chicago date --iso-8601=seconds)"--Main--Run Google Dropcaster"
    /usr/local/bin/docker-compose run dropcaster dropcaster --url "https://${PODCAST_DOMAIN_PRIMARY}" > ./new-index.rss
    cp ./new-index.rss ./audio/index.rss
    echo $(ls -lhaAgGR --block-size=1 --time-style=+%s ./audio | sed -re 's/^[^ ]* //' | sed -re 's/^[^ ]* //' | tail -n +3 | sha1sum) > ./audio-hash.txt
fi
echo $(TZ=America/Chicago date --iso-8601=seconds)"--Main--Send IP to Google DNS"
curl "https://${GOOGLE_DOMAIN_1_KEY}@domains.google.com/nic/update?hostname=${GOOGLE_DOMAIN_1}"
curl "https://${GOOGLE_DOMAIN_2_KEY}@domains.google.com/nic/update?hostname=${GOOGLE_DOMAIN_2}"
echo ""
echo $(TZ=America/Chicago date --iso-8601=seconds)"--Main--Clean up Docker"
docker system prune -f
echo $(TZ=America/Chicago date --iso-8601=seconds)"--Main--End Script"
