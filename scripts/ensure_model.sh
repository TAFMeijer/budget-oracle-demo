#!/bin/bash
# Make sure the local model is actually answerable: server listening, model resident.
#
# LM Studio is already a macOS login item, but that only launches the app — it does not
# guarantee the OpenAI-compatible server is listening, nor that a model is loaded. Without
# both, the assistant comes up fine and then fails every question.
#
# Safe to run repeatedly — but only because it probes before it acts. `lms server start` is
# NOT idempotent on LM Studio 0.4.x: run against a server that is already listening, it
# restarts that server, and every generation in flight dies with "Client disconnected". On
# this script's 10-minute schedule that killed roughly one question in fifteen (16 Sep 2026).
# So the server is started only when nothing answers on the port, and the model is only
# loaded when it is genuinely absent (reloading 15.6 GB for no reason is not free either).
set -uo pipefail
cd "$(dirname "$0")/.."

export PATH="$HOME/.lmstudio/bin:$PATH"

# Settings come from .env; an environment variable of the same name overrides (the tests use this).
MODEL=${LLM_MODEL:-$(awk -F= '/^LLM_MODEL=/{print $2; exit}' .env 2>/dev/null | tr -d '"' | xargs)}
BASE_URL=${OPENAI_BASE_URL:-$(awk -F= '/^OPENAI_BASE_URL=/{print $2; exit}' .env 2>/dev/null | xargs)}
PORT=$(printf '%s' "$BASE_URL" | sed -nE 's|.*:([0-9]+)(/.*)?$|\1|p')
: "${MODEL:?LLM_MODEL not set in .env}"
: "${PORT:=1234}"

# How long the model may sit unused before LM Studio unloads it. The default is an hour, which
# means the first question after a quiet afternoon pays a full reload. Six hours keeps it
# resident across a working day; lower it if you want the RAM back sooner.
TTL=${LM_MODEL_TTL:-21600}

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
listening() { curl -s --max-time 5 -o /dev/null "http://localhost:$PORT/v1/models"; }

if ! listening; then
    say "server not answering on port $PORT — starting it"
    # At login the app may still be starting, so give it a while before giving up.
    for i in $(seq 1 30); do
        lms server start >/dev/null 2>&1 && break
        say "waiting for LM Studio to accept commands ($i/30)…"
        sleep 10
    done
    for _ in $(seq 1 10); do            # the port comes up a moment after the command returns
        listening && break
        sleep 1
    done
    if ! listening; then
        say "ERROR: server is not answering on port $PORT"
        exit 1
    fi
    say "server listening on port $PORT"
fi

if lms ps --json 2>/dev/null | grep -q "\"modelKey\":\"$MODEL\""; then
    exit 0                                  # already resident, nothing to do and nothing to log
fi

say "loading $MODEL (ttl ${TTL}s)…"
if lms load "$MODEL" --ttl "$TTL" -y >/dev/null 2>&1; then
    say "loaded $MODEL"
else
    say "ERROR: could not load $MODEL"
    exit 1
fi
