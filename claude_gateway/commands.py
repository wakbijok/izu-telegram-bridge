"""Claude Code slash allowlist + Telegram menu (headless-proven only).

Important distinction:
- A companion agent bot (e.g. a Hermes Agent gateway) may own other slashes
  (/reasoning, /fast, /cron, …); this gateway does not implement them and
  points the user there (BRIDGE_COMPANION_BOT).
- Claude Code headless SDK does NOT expose /effort or /model as slashes
  ("isn't available in this environment") — those are gateway-native here.
- get_server_info() lists many skills/plugins; several are unknown/unavailable
  or hang when invoked as /slash in headless mode. Menu is therefore a
  **proven allowlist**, not a dump of all discovered names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Gateway shell — never forwarded to Claude Code.
NATIVE = frozenset({"help", "stop", "reset", "model", "effort"})

# Effort levels accepted by ClaudeAgentOptions / CLI --effort
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Headless-proven Claude Code slashes (probed 2026-07-28 via SDK query).
# Do NOT add items just because get_server_info listed them.
HEADLESS_CC_SLASHES: list[tuple[str, str]] = [
    ("clear", "CC: new empty context (session kept on disk)"),
    ("compact", "CC: summarize conversation to free context"),
    ("context", "CC: show context usage"),
    ("usage", "CC: session / plan usage"),
    ("init", "CC: write CLAUDE.md for a codebase"),
    ("config", "CC: set harness setting key=value"),
    ("debug", "CC: enable debug logging this session"),
    ("code-review", "CC: review working tree diff"),
    ("security-review", "CC: security review of pending changes"),
    ("review", "CC: review a GitHub PR"),
    ("simplify", "CC: cleanup/simplify changed code"),
    ("verify", "CC: verify a change actually works"),
    ("goal", "CC: keep working until goal met"),
    ("batch", "CC: large-scale parallel change"),
    ("loop", "CC: run prompt/slash on an interval"),
    ("reload-skills", "CC: reload skills from disk"),
    ("deep-research", "CC: multi-source research"),
    ("update-config", "CC: configure harness via settings.json"),
    ("insights", "CC: analyze your Claude Code sessions"),
    ("recap", "CC: one-line session recap"),
    ("agents", "CC: manage subagents (may be limited)"),
    ("schedule", "CC: scheduled cloud agents / routines"),
    ("run", "CC: launch/drive project app"),
    ("usage-credits", "CC: configure usage credits"),
]

# Explicitly NOT available as slash in headless (use gateway natives / other bots):
#   /effort /model /help /reasoning  → gateway natives or a companion bot
#   MCP prompt names (server:name) are often UNKNOWN as slash — use tools in chat instead

TG_NAME_RE = re.compile(r"[^a-z0-9_]")


def to_tg_name(cc_name: str) -> str:
    n = (cc_name or "").strip().lower()
    n = n.replace(":", "_").replace("-", "_").replace(" ", "_")
    n = TG_NAME_RE.sub("_", n)
    n = re.sub(r"_+", "_", n).strip("_")
    return n[:32]


@dataclass(frozen=True)
class CcCommand:
    name: str
    description: str
    tg_name: str


def headless_commands() -> list[CcCommand]:
    return [
        CcCommand(name=n, description=d, tg_name=to_tg_name(n))
        for n, d in HEADLESS_CC_SLASHES
    ]


def build_alias_map(cmds: list[CcCommand] | None = None) -> dict[str, str]:
    cmds = cmds or headless_commands()
    m = {c.tg_name: c.name for c in cmds}
    # common aliases
    m.setdefault("code_review", "code-review")
    m.setdefault("security_review", "security-review")
    m.setdefault("deep_research", "deep-research")
    m.setdefault("update_config", "update-config")
    m.setdefault("reload_skills", "reload-skills")
    m.setdefault("usage_credits", "usage-credits")
    m.setdefault("extra_usage", "usage-credits")
    return m


def build_menu(cmds: list[CcCommand] | None = None) -> list[tuple[str, str]]:
    cmds = cmds or headless_commands()
    menu: list[tuple[str, str]] = [
        ("help", "This bot: gateway + Claude Code slash list"),
        ("stop", "Interrupt running Claude turn"),
        ("reset", "Drop client + forget resume id"),
        ("model", "Gateway: model fable|opus|sonnet|haiku"),
        ("effort", "Gateway: effort low|medium|high|xhigh|max"),
    ]
    used = {m[0] for m in menu}
    for c in cmds:
        if c.tg_name in used:
            continue
        menu.append((c.tg_name, (c.description or c.name)[:256]))
        used.add(c.tg_name)
    return menu


def resolve_cc_slash(tg_cmd: str, aliases: dict[str, str] | None = None) -> str:
    aliases = aliases or build_alias_map()
    if tg_cmd in aliases:
        return aliases[tg_cmd]
    return tg_cmd.replace("_", "-")


def is_known_cc_slash(cc_name: str, aliases: dict[str, str] | None = None) -> bool:
    aliases = aliases or build_alias_map()
    known = set(aliases.values()) | {c.name for c in headless_commands()}
    return cc_name in known


def hermes_only_hint(cmd: str) -> str | None:
    """If the user typed a slash that belongs to a companion agent bot
    (Hermes Agent style), explain what to use here instead."""
    from claude_gateway.config import COMPANION_BOT

    there = f" Use {COMPANION_BOT} for that." if COMPANION_BOT else ""
    c = cmd.lower().lstrip("/")
    hints = {
        "reasoning": "Not on this bot. For thinking effort use "
        "/effort low|medium|high|xhigh|max." + there,
        "fast": "Not on this bot. Use /effort low (or medium)." + there,
        "voice": "Not on this bot." + there,
        "title": "Not on this bot." + there,
        "new": "Here use /clear (Claude Code) or /reset (drop the session).",
        "status": "Not on this bot." + there,
        "cron": "Not on this bot." + there,
        "skills": "Use /reload-skills, or ask in chat.",
    }
    return hints.get(c)


# Back-compat names used by older imports/tests
FALLBACK_MENU = build_menu()
FALLBACK_ALIASES = build_alias_map()


def parse_server_commands(raw) -> list[CcCommand]:
    """Keep parser for optional discovery, but production menu uses allowlist."""
    out: list[CcCommand] = []
    if not isinstance(raw, list):
        return out
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            name, desc = item, ""
        elif isinstance(item, dict):
            name = item.get("name") or item.get("command") or ""
            desc = item.get("description") or ""
        else:
            continue
        name = str(name).strip()
        if name.endswith(" (MCP)"):
            name = name[: -len(" (MCP)")].strip()
        if not name:
            continue
        tg = to_tg_name(name)
        if not tg or tg in seen or tg in NATIVE:
            continue
        seen.add(tg)
        out.append(CcCommand(name=name, description=str(desc)[:200], tg_name=tg))
    return out
