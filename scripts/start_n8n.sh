#!/bin/zsh
# Starts n8n as THIS script's own process (via exec, not backgrounded with
# `&`) so launchd tracks n8n directly — avoids relying on undefined
# process-group survival behavior for a detached background child.
#
# Paired with the com.agentictradingsystem.n8n-stop launchd job, which
# sends SIGTERM to this job's tracked process later the same day without
# unloading it, so it's still scheduled to fire again the next weekday.

cd "$(dirname "$0")/.."

export N8N_PORT="${N8N_PORT:-5690}"
# n8n also opens a task-runner broker on 127.0.0.1, default 5679 — pin it
# next to N8N_PORT so another local n8n (e.g. a different project's) can't
# collide with it.
export N8N_RUNNERS_BROKER_PORT="${N8N_RUNNERS_BROKER_PORT:-5691}"
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo localhost)"
export N8N_WEBHOOK_URL="http://${LAN_IP}:${N8N_PORT}/"
export N8N_SECURE_COOKIE=false

exec n8n start
