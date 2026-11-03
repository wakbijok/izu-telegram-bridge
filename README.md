# izu-telegram-bridge

A Telegram gateway for **Claude Code**. Chat with a long-lived, headless Claude Code session from your phone: it keeps its context across messages, runs tools in your working directory, streams its progress, and tells you when background work finishes — without you having to ask.

Built on the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) and [python-telegram-bot](https://python-telegram-bot.org/). Claude Code is the only backend; there is no model routing layer.

## Features

- **Persistent session per chat.** One Claude Code CLI per chat, resumed across restarts (session ids in `data/sessions.json`).
- **Always-on stream.** The bridge reads Claude Code's output for the session's whole life, so turns Claude Code starts on its own (e.g. when a `run_in_background` task finishes) are delivered immediately, marked 🔔. Replies never lag one message behind.
- **Message while it works.** A message sent mid-task is folded into the running turn (like typing into the interactive CLI) instead of interrupting it; the answer to it is sent right away and the task carries on.
- **Exact attribution.** Every prompt carries a UUID that the CLI echoes back (`--replay-user-messages`) when it consumes it, so each result is matched to the turn that produced it and delivered exactly once, in order.
- **Live progress.** A tool-activity ticker, typing indicator, and heartbeat messages on long tasks.
- **Watchdog.** Inactivity and hard-cap timeouts interrupt a stuck turn; a CLI that doesn't respond is restarted and unread messages are re-sent once.
- **Rich delivery.** Tables via Bot API rich messages, falling back to MarkdownV2 → HTML → plain text; long output is split or sent as a file. Nothing is dropped.
- **Slash commands.** Gateway natives (`/help /stop /reset /model /effort`) plus an allowlist of Claude Code slashes proven to work headless (`/compact`, `/context`, `/review`, …).
- **Photos and documents** are saved locally and handed to Claude Code to read.
- **Optional alert lane.** An authenticated webhook listener turns Grafana alerts, Keep incidents, GLPI tickets and Nextcloud Talk mentions into triage turns in your chat, one at a time.

## Requirements

- Python 3.12+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Claude Code authentication: a Claude subscription token (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`) or an existing `claude` login on the machine. `ANTHROPIC_API_KEY` is deliberately ignored so API billing is never used by accident.

## Quick start

```bash
git clone <this repo> izu-telegram-bridge && cd izu-telegram-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env   # fill in the bot token and your user id
.venv/bin/python bridge.py
```

To run it as a service, see `deploy/claude-telegram-bridge.service` (a systemd user unit).

## Configuration

All configuration is environment variables (`.env`, or the unit's `EnvironmentFile`). See [`.env.example`](.env.example) for the full list. The important ones:

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token (required) |
| `TELEGRAM_ALLOWED_USER_ID` | Comma-separated Telegram user ids allowed to use the bot (required) |
| `BRIDGE_CWD` | Claude Code working directory |
| `BRIDGE_MODEL` / `BRIDGE_EFFORT` | Model and thinking effort (changeable in chat with `/model`, `/effort`) |
| `BRIDGE_ALERT_MODEL` | Default model for the alert lane (empty = follow `BRIDGE_MODEL`), so triage turns don't spend the chat model's quota |
| `PERMISSION_MODE` | `default` asks for tool approval with Telegram buttons; `bypassPermissions` runs tools without asking |
| `BRIDGE_HARD_TIMEOUT` / `BRIDGE_IDLE_TIMEOUT` | Watchdog caps (seconds) |
| `BRIDGE_SYSTEM_PROMPT_FILE` | Your own persona / house rules (otherwise a generic default) |
| `BRIDGE_MCP_SERVERS_FILE` | Extra MCP servers as a JSON object `{"name": {"command": …, "args": […]}}` |

### Keeping your deployment out of the repo

Code carries no deployment identity. Put anything specific to you in `config/*.local.*` files (ignored by git) and point env vars at them:

- `config/persona.local.md`: the system prompt (who the assistant is, your hosts, tools, safety rules)
- `config/mcp.local.json`: MCP servers, e.g. a credential broker so the agent requests actions rather than reading secret values
- `config/alert-incident.local.md`, `config/alert-ticket.local.md`, `config/mention.local.md`: extra instructions appended to alert-lane prompts (`ALERT_INCIDENT_HINTS_FILE`, `ALERT_TICKET_HINTS_FILE`, `MENTION_HINTS_FILE`; `{incident_id}` / `{ticket_id}` / `{room_token}` / `{message_id}` are substituted, after validation)

## Chat commands

| Command | Action |
|---|---|
| `/help` | What this bot can do, current model and effort |
| `/stop` | Interrupt the running task (queued messages still run) |
| `/reset` | Drop the session and start fresh |
| `/model opus\|sonnet\|haiku\|fable` | Switch model |
| `/effort low\|medium\|high\|xhigh\|max` | Change thinking effort (reopens the session, context kept) |
| `/compact`, `/context`, `/usage`, `/review`, … | Forwarded to Claude Code (headless-proven allowlist) |

## Alert lane (optional)

Set `ALERT_PORT` and `ALERT_TOKEN` to start a small HTTP listener:

| Endpoint | Payload |
|---|---|
| `POST /alert` | Grafana unified-alerting webhook |
| `POST /incident` | Keep incident-dispatch workflow JSON |
| `POST /ticket` | GLPI ticket assignment (from your own poller) or followup webhook |
| `POST /mention` | Nextcloud Talk mention / DM / reply-to-me (from your own Talk relay); the turn drafts a reply and waits for your ok |
| `GET /healthz` | Liveness |

Requests need `Authorization: Bearer $ALERT_TOKEN`; bodies over 256 KiB get `413`. Alert turns run in their own Claude Code session, separate from your chat, and are told to triage and recommend, never to take destructive action on their own. Payload text (alert labels, incident names, ticket content, room messages) is passed to the model as a nonce-delimited JSON data block marked as untrusted; ids (Keep incident id, GLPI ticket id, Talk room token / message id) are validated and a payload with a malformed id is ignored.

| Variable | Purpose |
|---|---|
| `ALERT_PORT` / `ALERT_TOKEN` | Listener port and bearer token (both required to enable the lane) |
| `ALERT_BIND` | Listen address, default `0.0.0.0`. Use `127.0.0.1` unless your sources post from other hosts |
| `ALERT_CHAT_ID` | Chat that receives alert turns (default: the first allowed user) |
| `BRIDGE_ALERT_MODEL` | Alert-lane model (see above) |

## How it works

```
Telegram ──► handler ──► Lane.outbox ──► writer ──► Claude Code CLI (stdin, uuid-tagged)
                                                            │
Telegram ◄── Courier ◄── stream routing ◄── pump ◄──────────┘ (stdout, whole session life)
                              ▲
                      lane loop: typing, heartbeat, watchdog
```

- **Lane** (per chat; alerts use a separate lane): ordered outbox, one writer, turn bookkeeping.
- **Pump** (per CLI): reads every message the CLI emits, including turns nobody asked for.
- **Routing**: `init` opens a turn, prompt echoes attribute it, `ResultMessage` closes it and is delivered. Slash commands aren't echoed, so they run one at a time.
- **Courier** (per chat): one sequential sender, so replies keep stream order.

## Security notes

- Only user ids in `TELEGRAM_ALLOWED_USER_ID` can talk to the bot.
- `bypassPermissions` gives the model unrestricted tool use on the host. Use it only on a machine and account you are prepared to hand to an agent; prefer `default` otherwise.
- Keep `.env` and `config/*.local.*` at mode 600. They are git-ignored; never commit them.
- The alert listener binds `ALERT_BIND` (default `0.0.0.0`) on `ALERT_PORT`. Prefer `127.0.0.1` when every source is local; otherwise put it behind a firewall or reverse proxy. Use a long random `ALERT_TOKEN`.
- **Alert lane + `bypassPermissions` = outside input to a shell-capable agent.** Alert turns run with the same permission mode as the chat. Anyone who can get text into an alert source (a Grafana label or annotation, a Keep incident name, a GLPI ticket or followup, a Talk message to the account) can put words in front of an agent that can run shell commands, without asking you. The data-block framing and prompt rules reduce prompt-injection risk; they do not remove it. Only enable the alert lane under `bypassPermissions` if you trust everyone who can write to those sources, or run it in `default` mode.
- The bridge's own secrets (`TELEGRAM_BOT_TOKEN`, `ALERT_TOKEN`) are blanked in the Claude Code CLI's environment. Read/Grep/Glob are auto-approved in `default` mode and are not path-restricted, so files the account can read (including `.env`) remain reachable.

## Tests

```bash
.venv/bin/python -m pytest -q tests
```

The routing tests drive the real stream pump through a fake Claude Code CLI stream.

## License

[MIT](LICENSE)

### Running a second instance

Settings, sessions and persona files are read relative to the checkout (`.env`, `data/`, `config/*.local.*`, all gitignored), so a second bot runs from a `git worktree` of this repo:

```bash
git worktree add --detach ../my-second-bridge main
python3 -m venv ../my-second-bridge/.venv && ../my-second-bridge/.venv/bin/pip install -r requirements.txt
```

Give it its own `.env` (bot token, allowed user, `ALERT_PORT`) and its own systemd unit. To update it, run `git -C ../my-second-bridge checkout --detach main` and restart that unit.
