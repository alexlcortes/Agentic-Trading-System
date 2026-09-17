#!/bin/zsh
# Starts n8n as THIS script's own process (via exec, not backgrounded with
# `&`) so launchd tracks n8n directly — avoids relying on undefined
# process-group survival behavior for a detached background child.
#
# Paired with the com.agentictradingsystem.n8n-stop launchd job, which
# sends SIGTERM to this job's tracked process later the same day without
# unloading it, so it's still scheduled to fire again the next weekday.

cd "$(dirname "$0")/.."

N8N_PORT="${N8N_PORT:-5678}"
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo localhost)"
export N8N_WEBHOOK_URL="http://${LAN_IP}:${N8N_PORT}/"
export N8N_SECURE_COOKIE=false

exec n8n start
