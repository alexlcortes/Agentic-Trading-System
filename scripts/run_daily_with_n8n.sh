#!/bin/zsh
# Starts a local n8n instance just long enough for run_daily.py's
# human-override webhook call to reach it, then stops it. n8n only needs to
# be up for the few minutes around one daily run — not 24/7 — so this
# treats it as a scoped dependency of the trading run, started fresh and
# torn down after, the same way a test harness starts/stops a database.
#
# This does NOT build or activate the n8n workflow — that's a one-time,
# interactive step done via the browser UI (see HUMAN_OVERRIDE_SETUP.md).
# n8n persists workflows/credentials/active-state in ~/.n8n across restarts,
# so `n8n start` here just resumes whatever was already built and activated.
#
# If n8n never becomes healthy (not installed, port in use, etc.), this
# still runs run_daily.py — the human-override feature fails safe to
# "declined" on a missing/unreachable webhook (see agents/human_override.py),
# so a broken n8n setup degrades to today's behavior, it never blocks a run.

set -uo pipefail

cd "$(dirname "$0")/.."

N8N_PORT="${N8N_PORT:-5678}"
N8N_LOG="logs/n8n.log"
N8N_STARTUP_TIMEOUT=60

mkdir -p logs

# The ntfy Actions header (built from $execution.resumeUrl inside the n8n
# workflow) needs the phone to be able to reach this Mac directly — that
# only works if n8n generates its webhook/resume URLs using the LAN IP,
# not "localhost". Detected fresh on every run since a home-network DHCP
# lease can change the IP over time. Falls back to localhost (same
# behavior as an unconfigured override — fails safe to "declined" if the
# phone can't reach it) if no LAN interface is found.
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo localhost)"
export N8N_WEBHOOK_URL="http://${LAN_IP}:${N8N_PORT}/"
export N8N_SECURE_COOKIE=false
echo "[run_daily_with_n8n] using N8N_WEBHOOK_URL=$N8N_WEBHOOK_URL" >>"$N8N_LOG"

n8n start >>"$N8N_LOG" 2>&1 &
N8N_PID=$!

cleanup() {
    if kill -0 "$N8N_PID" 2>/dev/null; then
        echo "[run_daily_with_n8n] stopping n8n (pid $N8N_PID)" >>"$N8N_LOG"
        kill "$N8N_PID" 2>/dev/null
        wait "$N8N_PID" 2>/dev/null
    fi
}
trap cleanup EXIT

echo "[run_daily_with_n8n] started n8n (pid $N8N_PID), waiting for it to be ready..." >>"$N8N_LOG"
elapsed=0
until curl -sf "http://localhost:${N8N_PORT}/healthz" >/dev/null 2>&1; do
    if ! kill -0 "$N8N_PID" 2>/dev/null; then
        echo "[run_daily_with_n8n] n8n process exited early — check $N8N_LOG" >>"$N8N_LOG"
        break
    fi
    sleep 2
    elapsed=$((elapsed + 2))
    if [ "$elapsed" -ge "$N8N_STARTUP_TIMEOUT" ]; then
        echo "[run_daily_with_n8n] n8n not healthy after ${N8N_STARTUP_TIMEOUT}s — proceeding without it" >>"$N8N_LOG"
        break
    fi
done

/Users/neuromancer/.local/bin/uv run python run_daily.py
