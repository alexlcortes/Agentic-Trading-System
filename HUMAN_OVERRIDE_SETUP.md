# Human override via n8n + ntfy — setup guide

## What this is

When `risk_manager.check_trade` blocks a **buy** for hitting `max_position_pct`
(and only that reason — kill switch, daily loss halt, and
`max_open_positions` stay hard stops with no override path), the trading
run now optionally pauses and sends a push notification asking whether to
bypass the cap for that one trade. Tap **Approve** within the timeout
window (default 10 minutes) and it buys at the originally requested size;
tap **Deny** or let it time out and it holds, exactly like it does today.

The Python side (`agents/human_override.py`, `agents/risk_manager.py`,
`orchestration/graph.py`) is channel-agnostic — it only knows how to POST to
an n8n webhook and wait for a JSON response. It's already built and is a
no-op until you finish the steps below — `ENABLE_HUMAN_OVERRIDE` defaults to
off, and even if it's on, a missing/broken webhook always fails safe to
"declined," never to "approved."

## Why ntfy (and not Discord or Slack)

Three channels got evaluated for the notify+reply step. ntfy won on actually
getting built today; the other two are parked as future upgrades (see
bottom of this doc):

- **Slack** — n8n has first-class native support for this (Interactivity
  webhook, no bot gateway needed), but it's not a channel actually used day
  to day, so an "instant" approval prompt would go unseen in practice.
- **Discord** — the natural fit for actual usage, but a typed-reply design
  needs a bot with a persistent Gateway connection (more infra, less
  certain n8n support), and even the simpler "post links, click one"
  version has a real gotcha: Discord auto-fetches any link to build a
  preview embed, which would silently hit the Approve/Deny URL the instant
  the message posts, unless every link is wrapped in `<angle brackets>` to
  suppress that. Solvable, but more moving parts than ntfy for a first pass.
- **ntfy** — a push notification with real tap-to-fire action buttons (not
  passive links), so there's no auto-fetch risk at all, no bot/app
  registration, and no signature verification to write. Reaches your phone
  even with nothing else open.

## Architecture

```
run_daily.py
   │  one blocking HTTP POST (times out after OVERRIDE_TIMEOUT_SECONDS + 30s)
   ▼
n8n: Webhook (receiver)
   │  validates X-Override-Secret header
   ▼
n8n: HTTP Request node — POST to https://ntfy.sh/<topic>
   │  with Approve/Deny "http" actions pointing at this execution's resume URL
   ▼
n8n: Wait node — "Resume: On Webhook Call", limit = OVERRIDE_TIMEOUT_SECONDS
   ▲                                                    │ (times out if no tap)
   │ tapping Approve/Deny on your phone calls           │
   │ <n8n base>/webhook-waiting/{{$execution.id}}       ▼
   └─────────────────────────────────────────── (resumes with ?approved=true/false)
   ▼
n8n: IF (resumed with data vs timed out) → build {"approved": ..., "reason": ...}
   ▼
n8n: Respond to Webhook — sends it back to the original POST from run_daily.py
```

Only one thing needs to actually block: the single HTTP call `run_daily.py`
makes. n8n owns the waiting/timeout logic natively via the Wait node — no
polling loop anywhere, and no second trigger/webhook needed (unlike the
Slack/Discord designs), because the phone hits the Wait node's resume URL
directly when you tap a button.

## What run_daily.py sends

```json
POST <N8N_OVERRIDE_WEBHOOK_URL>
Headers: X-Override-Secret: <N8N_OVERRIDE_SECRET>

{
  "run_id": "b07d6636f43d4254822ccb2adeff4b0c",
  "ticker": "AAPL",
  "action": "buy",
  "requested_size_pct": 0.02,
  "existing_pct": 0.05,
  "max_position_pct": 0.05,
  "reasoning": "<the portfolio manager's reasoning text>",
  "timeout_seconds": 600
}
```

## What run_daily.py expects back

```json
{
  "approved": true,
  "responder": "ntfy",
  "reason": "approved via push notification"
}
```

Anything else — a non-200 status, malformed JSON, a missing `approved` key,
a dropped connection, or no response within the timeout — is treated as
`approved: false`. There is no code path where a failure defaults to
"approved."

## Hosting: local n8n, started/stopped around each daily run

n8n runs locally on this Mac (no Docker, no cloud account) via a global npm
install — it only needs to be alive for the few minutes around one daily
run, not 24/7, and since `run_daily.py` and n8n share the same machine, the
webhook `run_daily.py` calls never has to leave `localhost`.

**One-time install:**
```
npm install -g n8n
```

**One-time interactive setup** (do this before wiring up the wrapper
script below — building the workflow requires the browser UI):
```
n8n start
```
Open `http://localhost:5678`, create your owner account (email/password —
this is local-only, nothing is sent anywhere), then work through the
"Step-by-step: ntfy setup" and "Step-by-step: n8n workflow" sections below
in that browser tab. When you're done and the workflow is toggled **Active**,
stop this manual instance (Ctrl+C) — n8n persists workflows, credentials,
and active-state to `~/.n8n` (SQLite), so a fresh headless `n8n start` later
resumes right where you left off with no browser needed.

**Daily lifecycle, already wired up:** `scripts/run_daily_with_n8n.sh`
detects the Mac's current LAN IP and exports `N8N_WEBHOOK_URL`/
`N8N_SECURE_COOKIE=false` (same as the manual exports used during the
interactive build below — the script does this automatically so the
resume URLs your phone taps always resolve to a reachable address, even if
DHCP hands out a new IP on a different day), starts `n8n start` in the
background, polls `http://localhost:5678/healthz` until it's up (or gives
up after 60s and proceeds anyway — a missing n8n fails safe to "declined,"
it never blocks the trading run), runs `run_daily.py`, then kills the n8n
process on exit. The launchd plist
(`scripts/launchd/com.agentictradingsystem.daily.plist`) now points at this
wrapper instead of `run_daily.py` directly. **Verified working** (manual
run of the wrapper on 2026-09-17: n8n started, auto-activated the saved
workflow, bound to the LAN IP, and shut down cleanly after `run_daily.py`
exited).

**For the manual/interactive build below**, set the same two env vars
yourself before running `n8n start`, or you'll hit a cookie error (see
Troubleshooting) and phone-unreachable resume URLs:
```
export N8N_WEBHOOK_URL="http://<your-lan-ip>:5678/"
export N8N_SECURE_COOKIE=false
n8n start
```
Get your LAN IP with `ipconfig getifaddr en0` (or `en1`). Access the
**editor** at `http://localhost:5678` in Chrome either way — the env vars
only affect what URLs n8n *generates*, not what address you browse to.

**Don't reload the launchd job until n8n is actually installed and the
workflow is built and active** — the wrapper degrades gracefully (it'll
just skip straight to `run_daily.py` if `n8n` isn't found or never becomes
healthy), but there's no point reloading it before there's a workflow for
it to talk to. When you're ready:
```
launchctl unload ~/Library/LaunchAgents/com.agentictradingsystem.daily.plist
launchctl load ~/Library/LaunchAgents/com.agentictradingsystem.daily.plist
```
(adjust the path if the plist is symlinked/copied somewhere other than
`~/Library/LaunchAgents` — run `launchctl list | grep agentictradingsystem`
to confirm what's currently loaded first).

## Step-by-step: ntfy setup

1. Install the **ntfy** app on your phone (iOS/Android — search "ntfy").
2. Pick a random, unguessable topic name — treat it like a password, e.g.
   `openssl rand -hex 12` → `trading-override-<that string>`. This is your
   only access control on the public `ntfy.sh` server: anyone who knows the
   topic name could publish to or read it, so don't post it anywhere public.
3. In the app, **Subscribe to topic** → enter `https://ntfy.sh/<your-topic>`
   (or just the topic name if it defaults to ntfy.sh).
4. That's it — no account, no API key needed for the public server.

## Step-by-step: n8n workflow

1. **Create a new workflow** in n8n, name it `trading-override`.

2. **Add a Webhook node** (trigger).
   - HTTP Method: `POST`
   - Path: something unguessable, e.g. `override-a1e9f2`
   - Respond: **"Using Respond to Webhook Node"** (not "Immediately") — this
     is what lets the workflow hold the HTTP connection open until the Wait
     node resolves.
   - Copy the **Production URL** it generates — that's your
     `N8N_OVERRIDE_WEBHOOK_URL`.

3. **Add an IF node** right after the webhook to check the secret.
   - Condition: `{{$json.headers['x-override-secret']}}` equals your chosen
     secret string (generate one, e.g. `openssl rand -hex 24` — that's your
     `N8N_OVERRIDE_SECRET`).
   - False branch → **Respond to Webhook** node returning `403` and stop.
     This keeps random internet traffic from ever reaching the notification
     or, worse, the approval path.

4. **Add an HTTP Request node** (true branch) to publish the ntfy notification.
   - Method: `POST`
   - URL: `https://ntfy.sh/<your-topic>`
   - Body (raw text) — the message:
     ```
     {{$json.body.ticker}}: buy {{$json.body.requested_size_pct}} of equity
     (current position: {{$json.body.existing_pct}}, cap: {{$json.body.max_position_pct}})
     {{$json.body.reasoning}}
     ```
   - Headers (add each as a Name/Value row, both in **Expression** mode
     since both contain `{{...}}`):
     - `Title` → `Risk override: {{$json.body.ticker}}`
     - `Actions` → (one line):
       ```
       http, Approve, {{$execution.resumeUrl}}&approved=true, method=GET; http, Deny, {{$execution.resumeUrl}}&approved=false, method=GET
       ```
   - **Use `$execution.resumeUrl`, not a hand-built URL.** n8n's Wait node
     (next step) generates its own resume URL — shown in its node panel as
     "Send it somewhere before getting to this node," reachable via
     `$execution.resumeUrl` in any node that runs *before* the Wait node
     (which this one does). It already includes its own `?signature=...`
     query string for validating the resume call, which is why this
     appends with `&`, not `?` — see Troubleshooting below for what
     happens if you get that wrong.
   - This URL only resolves to something your phone can actually reach if
     n8n was started with `N8N_WEBHOOK_URL` set to the LAN IP (see Hosting
     section above) — otherwise it'll be `localhost`, unreachable from the
     phone.

5. **Add a Wait node.**
   - Resume: **"On Webhook Call"**
   - Limit: **enabled**, 600 seconds (matches `OVERRIDE_TIMEOUT_SECONDS`)
   - When resumed, the query string from the tapped action
     (`?approved=true` / `?approved=false`) is available on the next node as
     `{{$json.query.approved}}`. On timeout, the workflow continues with no
     resume data — branch on that.

6. **Add an IF node** (after Wait) — check whether `$json.query.approved`
   **exists** (use the String-type "exists" operator, since a URL query
   param always arrives as a string):
   - True branch → a **Set** ("Edit Fields") node building
     `{"approved": {{$json.query.approved === 'true'}}, "responder": "ntfy", "reason": "approved via push notification"}`
     (cast `approved` to an actual Boolean field, not a string)
   - False branch (timed out) → a **Set** node building
     `{"approved": false, "reason": "timed out"}`

7. **Add a Respond to Webhook node** after each Set node (two total, one
   per branch). **Set "Respond With" to "First Incoming Item"** — the
   default ("All Incoming Items") wraps the response in a JSON array
   (`[{"approved": true, ...}]`), which `agents/human_override.py` will
   silently treat as malformed and auto-decline, since it expects a plain
   object. This is easy to miss because the workflow still "works" (an
   array with a false `approved` inside just looks like a normal decline)
   — verify the raw response is `{...}`, not `[{...}]`, before trusting a
   test result.

8. **Activate the workflow** (top-right toggle) — the Wait node and Webhook
   trigger only run on an active workflow, not in manual test-execution mode.

## Wiring it up on this side

Once the workflow is built and active (webhook path + secret chosen in
steps 2–3 above), add these to `.env`:

```
ENABLE_HUMAN_OVERRIDE=true
N8N_OVERRIDE_WEBHOOK_URL=http://<your-lan-ip>:5678/webhook/override-a1e9f2
N8N_OVERRIDE_SECRET=<the secret from step 3>
OVERRIDE_TIMEOUT_SECONDS=600
```
Use the LAN IP here, not `localhost` — even though `run_daily.py` and n8n
run on the same Mac, this keeps it consistent with whatever
`N8N_WEBHOOK_URL` n8n itself is using, which is what its Production URL
will actually be bound to.

Then test it deliberately, in this order, before trusting it in a real
daily run:

1. **Isolate the n8n side with curl first** (skip Python entirely):
   ```
   curl -X POST http://<your-lan-ip>:5678/webhook/<your-path> -H "Content-Type: application/json" -H "X-Override-Secret: <your-secret>" -d '{"run_id":"test1","ticker":"AAPL","action":"buy","requested_size_pct":0.02,"existing_pct":0.05,"max_position_pct":0.05,"reasoning":"manual test","timeout_seconds":600}'
   ```
   Keep the `-d` JSON on **one line** — pasting a multi-line quoted string
   into an interactive terminal can get corrupted by the shell (extra
   characters inserted at line breaks), which shows up as a cryptic
   `"Bad control character in string literal"` JSON-parse error from n8n
   that has nothing to do with the workflow itself.
2. **With n8n running manually**: with `ENABLE_HUMAN_OVERRIDE=true`,
   invoke `agents.human_override.request_override(...)` from a Python
   shell with a fake `run_id`/ticker and confirm (a) the push notification
   shows up on your phone, (b) tapping Approve returns
   `{'approved': True, ...}` (a real Python dict, not a list) within a
   couple seconds, and (c) letting it sit past the timeout returns
   `{'approved': False, ...}` on its own without touching your phone.
3. **Through the actual wrapper**: stop any manually-running n8n first
   (`lsof -i :5678` to find it, or Ctrl+C in its terminal — the wrapper
   starts its own instance and needs the port free), then run
   `./scripts/run_daily_with_n8n.sh` by hand once (not via launchd) and
   watch `logs/n8n.log` to confirm n8n started, activated the workflow,
   bound to the LAN IP, and shut down cleanly after `run_daily.py`
   finished. Note: `run_daily.py` aborts immediately if run before 16:00
   local (`CATCHUP_CUTOFF_HOUR`) — that's expected and still validates the
   n8n start/stop lifecycle, just not a real trading cycle.

## Troubleshooting (issues actually hit building this)

- **"Your n8n server is configured to use a secure cookie..." on login** —
  happens when you access n8n via a non-`localhost` address (the LAN IP)
  or via Safari. Fix: `export N8N_SECURE_COOKIE=false` before `n8n start`
  (already done automatically by the wrapper script). Browse the editor at
  `http://localhost:5678` in Chrome regardless.
- **Forgot the owner password** — `n8n start` must be stopped first, then
  `n8n user-management:reset` clears just the login (not your workflows),
  and the next `n8n start` shows the setup screen again to create a new
  owner account.
- **Wait node never resumes after tapping Approve/Deny** — almost
  certainly the URL has two `?` characters in it. `$execution.resumeUrl`
  already includes `?signature=...`; appending another `?approved=true`
  instead of `&approved=true` corrupts the signature n8n uses to validate
  the resume call, so it silently rejects it. Check the actual URL n8n
  sent by opening the stuck execution in the **Executions** tab and
  inspecting the HTTP Request node's output.
- **Response comes back as `[{"approved": ...}]` instead of `{"approved":
  ...}`** — the Respond to Webhook node's "Respond With" is set to "All
  Incoming Items" (wraps in an array). Change it to "First Incoming Item"
  on *both* Respond to Webhook nodes (approved branch and timeout branch).
  This is easy to miss because a declined response still "looks right" —
  it only breaks a genuine approval, since `agents/human_override.py`
  requires a plain dict and treats anything else as malformed → declined.
- **`curl: ... Bad control character in string literal` from n8n** — a
  shell/paste artifact from a multi-line `-d '{...}'` JSON body getting
  corrupted on paste, not a real JSON or workflow problem. Put the JSON
  payload on a single line.
- **Port 5678 already in use** when starting the wrapper script — you
  likely still have a manually-started `n8n start` running from testing.
  `lsof -i :5678` to find the PID, stop it (Ctrl+C in its terminal, or
  `kill <pid>`) before running the wrapper.

## What to double check before relying on this

- **The resume URL must be reachable from your phone**, not just from the
  Mac itself — this is the one thing most likely to silently not work if
  skipped. Test a tap for real before trusting it in production.
- `scripts/run_daily_with_n8n.sh` gives n8n 60s to become healthy before
  giving up and running the trading cycle without it — if your Mac is slow
  to start it (cold boot right after wake, etc.), that's the knob to raise.
- `OVERRIDE_TIMEOUT_SECONDS` on this side and the Wait node's limit in n8n
  should match — if the HTTP client times out before n8n's Wait node
  would, you lose a would-be approval to a client-side timeout.
- Every override attempt — approved, declined, or timed out — is logged as
  a `"type": "human_override"` entry in `logs/trades.jsonl`, so you can
  audit how often this actually gets used and whether the reply channel is
  reliable.
- `~/.n8n` is where your workflow and execution history live — back it up
  if you'd be annoyed to rebuild the workflow from scratch, and don't
  commit it anywhere.
- The ntfy topic name is a bearer secret on the public server — don't paste
  it into a commit, a screenshot, or this repo.

## Known limitation: phone must be on the same LAN as the Mac

The ntfy action buttons hit the Mac's LAN IP (e.g. `192.168.1.168:5678`)
directly, so approving a trade only works while your phone is on the same
Wi-Fi network as this Mac. Off-network (cellular, another Wi-Fi), the
buttons can't reach n8n and any blocked trade just times out to "declined"
— safe, but you lose the ability to actually approve.

**Planned fix: Tailscale.** A free private mesh VPN — install it on both
the Mac and the phone, each gets a stable private IP reachable from
anywhere without exposing anything to the public internet (only your own
logged-in devices can reach it). Swap the LAN IP in the ntfy Actions header
for the Mac's Tailscale IP once set up. Deliberately deferred for now to
get the local version working first; the only change needed later is that
one IP substitution.

## Future upgrade options (parked, not built)

**Discord** — the channel actually worth switching to eventually, since
it's where you actually pay attention. Two designs were scoped and are
worth revisiting:
- *Plain links via a Discord channel webhook* (no bot): same idea as ntfy's
  action buttons, but as two links in a posted message. Requires wrapping
  each link in `<angle brackets>` to stop Discord's own link-preview
  crawler from auto-hitting (and thus auto-resolving) the Approve/Deny URL
  the moment the message posts.
- *Native buttons via Discord Interactions*: real tap-to-approve buttons
  like ntfy's, but requires a Discord Application with Ed25519 signature
  verification on every incoming interaction (needs a Code node with a
  crypto library, plus `NODE_FUNCTION_ALLOW_EXTERNAL` configured in n8n) —
  more correct long-term, more setup.

**Slack** — already the most "native" fit for n8n (built-in Interactivity
webhook support, no bot gateway needed), parked purely because it's not a
channel that gets checked day to day. Worth reconsidering if that changes,
or as a secondary/redundant channel alongside ntfy.
