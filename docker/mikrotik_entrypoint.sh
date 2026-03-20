#!/bin/sh
# docker/mikrotik_entrypoint.sh — Entrypoint for the MikroTik Matrix Bot.
#
# Starts bot.py and restarts it on transient exit with exponential back-off.
# sshd from the base image is intentionally not started — this bot connects
# outbound to routers via the RouterOS API and does not require an inbound
# SSH gateway.
#
# Signal contract
# ---------------
# SIGTERM / SIGINT → forward to bot → wait → clean exit 0.
# bot exit         → restart with exponential back-off (2 s → 60 s cap).

set -eu

PYTHON=/home/bot/.venv/bin/python3
BOT_PID=""

# ---------------------------------------------------------------------------
# Signal handler
# ---------------------------------------------------------------------------

cleanup() {
    echo "[entrypoint] Signal received — stopping bot"
    [ -n "$BOT_PID" ] && kill -TERM "$BOT_PID" 2>/dev/null || true
    wait "$BOT_PID" 2>/dev/null || true
    echo "[entrypoint] Clean exit"
    exit 0
}

trap cleanup TERM INT

# ---------------------------------------------------------------------------
# Start the bot
# ---------------------------------------------------------------------------

start_bot() {
    $PYTHON /home/bot/app/bot.py &
    BOT_PID=$!
    echo "[entrypoint] bot.py started (PID ${BOT_PID})"
}

start_bot

# ---------------------------------------------------------------------------
# Monitor loop — poll every 5 s (POSIX sh has no wait -n)
# ---------------------------------------------------------------------------

BOT_BACKOFF=2

while true; do
    sleep 5

    if ! kill -0 "$BOT_PID" 2>/dev/null; then
        echo "[entrypoint] bot.py exited — restarting in ${BOT_BACKOFF}s"
        sleep "$BOT_BACKOFF"
        BOT_BACKOFF=$(( BOT_BACKOFF * 2 > 60 ? 60 : BOT_BACKOFF * 2 ))
        start_bot
        BOT_BACKOFF=2
    fi
done
