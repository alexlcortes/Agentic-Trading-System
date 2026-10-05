#!/usr/bin/env bash
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
#
# Portable between macOS (launchd) and Linux, e.g. a Raspberry Pi (systemd):
# bash rather than zsh (not installed by default on Raspberry Pi OS), uv and
# n8n found on PATH rather than at Homebrew/home-dir paths, and the LAN IP
# looked up with whichever tool the OS has.

set -uo pipefail

cd "$(dirname "$0")/.."

export N8N_PORT="${N8N_PORT:-5690}"
# n8n also opens a task-runner broker on 127.0.0.1, default 5679 — pin it
# next to N8N_PORT so another local n8n (e.g. a different project's) can't
# collide with it.
export N8N_RUNNERS_BROKER_PORT="${N8N_RUNNERS_BROKER_PORT:-5691}"
N8N_LOG="logs/n8n.log"
# A Raspberry Pi can take well over a minute to boot n8n — override with
# N8N_STARTUP_TIMEOUT=180 in the systemd unit if it keeps timing out.
N8N_STARTUP_TIMEOUT="${N8N_STARTUP_TIMEOUT:-60}"

# Schedulers start jobs with a minimal PATH (launchd's login shell does load
# the profile; systemd does not), so also check where the uv installer puts it.
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"

mkdir -p logs

# The ntfy Actions header (built from $execution.resumeUrl inside the n8n
# workflow) needs the phone to be able to reach this Mac directly — that
# only works if n8n generates its webhook/resume URLs using the LAN IP,
# not "localhost". Detected fresh on every run since a home-network DHCP
# lease can change the IP over time. Falls back to localhost (same
# behavior as an unconfigured override — fails safe to "declined" if the
# phone can't reach it) if no LAN interface is found.
lan_ip() {
    case "$(uname -s)" in
        Darwin) ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null ;;
        # First address `hostname -I` lists — the one on the default
        # interface (eth0/wlan0) on a Pi.
        Linux) hostname -I 2>/dev/null | awk '{print $1}' ;;
    esac
}
LAN_IP="$(lan_ip)"
LAN_IP="${LAN_IP:-localhost}"
export N8N_WEBHOOK_URL="http://${LAN_IP}:${N8N_PORT}/"
export N8N_SECURE_COOKIE=false
echo "[run_daily_with_n8n] using N8N_WEBHOOK_URL=$N8N_WEBHOOK_URL" >>"$N8N_LOG"

N8N_PID=""
cleanup() {
    if [ -n "$N8N_PID" ] && kill -0 "$N8N_PID" 2>/dev/null; then
        echo "[run_daily_with_n8n] stopping n8n (pid $N8N_PID)" >>"$N8N_LOG"
        kill "$N8N_PID" 2>/dev/null
        wait "$N8N_PID" 2>/dev/null
    fi
}
trap cleanup EXIT

if ! command -v n8n >/dev/null 2>&1; then
    echo "[run_daily_with_n8n] n8n not found on PATH — running without it" >>"$N8N_LOG"
else
    n8n start >>"$N8N_LOG" 2>&1 &
    N8N_PID=$!
    echo "[run_daily_with_n8n] started n8n (pid $N8N_PID), waiting for it to be ready..." >>"$N8N_LOG"
fi

elapsed=0
until [ -z "$N8N_PID" ] || curl -sf "http://localhost:${N8N_PORT}/healthz" >/dev/null 2>&1; do
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

"$UV" run python run_daily.py
