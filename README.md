# Agentic Trading System

## Scheduling

`run_daily.py` runs once per trading day, shortly after market close, against
the paper Alpaca account. It's scheduled via a macOS `launchd` job rather than
`cron`, because `launchd` catches up a run that was missed while the machine
was asleep or off — `cron` just silently skips it.

The job definition lives at `scripts/launchd/com.agentictradingsystem.daily.plist`
(Mon-Fri, 16:30 local time). `run_daily.py` itself aborts if it's started
before 16:00 local time (`CATCHUP_CUTOFF_HOUR`) — this is what actually
prevents a very late wake (e.g. the machine coming back online well after
midnight) from running a stale catch-up instead of waiting for the next
regularly scheduled day.

### Install

```
cp scripts/launchd/com.agentictradingsystem.daily.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.agentictradingsystem.daily.plist
```

### Uninstall

```
launchctl bootout gui/$(id -u)/com.agentictradingsystem.daily
rm ~/Library/LaunchAgents/com.agentictradingsystem.daily.plist
```

### Logs

- `logs/cron.log` — stdout/stderr of each run (the script's own output)
- `logs/run_daily.log` — structured application log
- `logs/daily_summary.log` — one-line-per-ticker daily outcome summary
- `logs/launchd.log` — launchd-level failures only (should normally stay empty)

## Human override (risk cap bypass)

A buy blocked solely by `max_position_pct` can optionally be escalated to a
human for a timed approve/deny before it's forced to hold — see
`HUMAN_OVERRIDE_SETUP.md`. Off by default (`ENABLE_HUMAN_OVERRIDE=false`);
a missing/unconfigured n8n webhook always fails safe to "declined."

Current channel: **ntfy** push notification with tap-to-approve action
buttons (in progress). **Future upgrade:** Discord, since that's the
channel actually used day to day — two designs (plain webhook links with
Discord's link-preview crawler worked around, or native Interactions
buttons with signature verification) are scoped in
`HUMAN_OVERRIDE_SETUP.md` but not yet built. Slack was also scoped (n8n
has the most native support for it) but parked since it's not checked
regularly.
