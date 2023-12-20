#!/bin/bash

FIRST_LOG_DATE=$(TZ='America/Chicago' date +%FT%T.%3N%:z)

timestamp_output() {
    while IFS= read -r line; do
        if [ -z "${FIRST_LOG_DATE}" ]; then
            echo "$(TZ='America/Chicago' date +%FT%T.%3N%:z) $line"
        else
            echo "$FIRST_LOG_DATE $line"
            export FIRST_LOG_DATE=""
        fi
    done
}

bash /home/flog99/dev/podcast-transcribe/process.sh $FIRST_LOG_DATE 2>&1 | timestamp_output
