#!/bin/bash

FIRST_LOG_DATE=$(TZ='America/Chicago' date +%FT%T.%3N%:z)
RUN_LOG="/home/flog99/process-log-runs/${FIRST_LOG_DATE}.log"

RUN_LOG="$RUN_LOG" bash /home/flog99/dev/podcast-transcribe/process.sh $FIRST_LOG_DATE 2>&1
exit_code=${PIPESTATUS[0]}
if [ "$exit_code" -ne 0 ]; then
    debug_message="Podcast Transcribe error: process.sh exit code $exit_code"
    if [ -f "$RUN_LOG" ]; then
        debug_output=$(RUN_LOG="$RUN_LOG" python3 - <<'PY'
import os

path = os.environ.get("RUN_LOG", "")
data = open(path, "rb").read()
had_nul = b"\x00" in data
data = data.replace(b"\x00", b"\\0")
text = data.decode("utf-8", errors="replace")
if had_nul:
    text = "[NUL bytes replaced]\\n" + text
print(text, end="")
PY
)
    else
        debug_output="Run log missing: $RUN_LOG"
    fi
    echo "Main--Exit code error- sending notification with Gotify"
    # Send message to Gotify, don't display anything if successful
    DEBUG_TITLE="$debug_message" RUN_LOG="$RUN_LOG" python3 - <<'PY' | \
        curl --silent --show-error -H "Content-Type: application/json" \
            --data-binary @- "$GOTIFY_SERVER/message?token=$GOTIFY_TOKEN" > /dev/null
import json
import os

title = os.environ.get("DEBUG_TITLE", "")
path = os.environ.get("RUN_LOG", "")
data = open(path, "rb").read()
had_nul = b"\x00" in data
data = data.replace(b"\x00", b"\\0")
message = data.decode("utf-8", errors="replace")
if had_nul:
    message = "[NUL bytes replaced]\\n" + message
print(json.dumps({"title": title, "message": message, "priority": 9}))
PY
fi
exit "$exit_code"
