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
