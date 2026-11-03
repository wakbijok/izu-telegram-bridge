"""Gateway settings from environment. Secrets stay in .env / EnvironmentFile."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


# Drop API-billing env vars so subscription OAuth / machine login wins.
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = REPO_ROOT / ".env"
_load_dotenv(_ENV_FILE)


def repo_path(value: str) -> Path:
    """Env-supplied file path: ~ expanded, relative paths from the repo root."""
    p = Path(value).expanduser()
    return p if p.is_absolute() else REPO_ROOT / p


# Deployment identity lives in env / local files, never in code.
USER_NAME = os.environ.get("BRIDGE_USER_NAME", "").strip() or "the user"
COMPANION_BOT = os.environ.get("BRIDGE_COMPANION_BOT", "").strip()  # e.g. @my_other_bot


def _load_system_prompt() -> str:
    """BRIDGE_SYSTEM_PROMPT > BRIDGE_SYSTEM_PROMPT_FILE > generic default."""
    inline = os.environ.get("BRIDGE_SYSTEM_PROMPT", "").strip()
    if inline:
        return inline
    path = os.environ.get("BRIDGE_SYSTEM_PROMPT_FILE", "").strip()
    if path:
        return repo_path(path).read_text(encoding="utf-8").strip()  # missing file = loud failure
    return _DEFAULT_SYSTEM_PROMPT


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_user_ids: frozenset[int]
    workdir: str = field(default_factory=lambda: str(Path.home()))
    media_dir: Path = field(default_factory=lambda: Path.home() / ".tg-media")
    model: str = "claude-fable-5"
    fallback_model: str = "claude-sonnet-4-6"
    alert_model: str = ""  # empty = alert lane follows `model`
    effort: str = "medium"
    permission_mode: str = "default"
    turn_timeout: int = 0  # 0 = unlimited interactive
    alert_turn_timeout: int = 600
    progress_after: int = 60
    heartbeat_every: int = 240
    file_lines: int = 30
    tg_limit: int = 3900
    rich_mode: str = "always"  # always | auto | off
    rich_max_chars: int = 32000
    ticker_throttle: float = 2.5
    ticker_tail: int = 6
    sessions_file: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent.parent / "data" / "sessions.json"
    )
    system_prompt: str = ""

    @staticmethod
    def from_env() -> "Settings":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        allowed_raw = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "")
        allowed = frozenset(int(x) for x in allowed_raw.split(",") if x.strip())
        if not token or not allowed:
            raise SystemExit("missing TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_ID")
        workdir = os.path.expanduser(os.environ.get("BRIDGE_CWD", str(Path.home())))
        media = Path(os.environ.get("BRIDGE_MEDIA_DIR", os.path.join(workdir, ".tg-media"))).expanduser()
        prompt = _load_system_prompt()
        return Settings(
            bot_token=token,
            allowed_user_ids=allowed,
            workdir=workdir,
            media_dir=media,
            model=os.environ.get("BRIDGE_MODEL", "claude-fable-5"),
            fallback_model=os.environ.get("BRIDGE_FALLBACK_MODEL", "claude-sonnet-4-6"),
            alert_model=os.environ.get("BRIDGE_ALERT_MODEL", "").strip(),
            effort=os.environ.get("BRIDGE_EFFORT", "medium"),
            permission_mode=os.environ.get("PERMISSION_MODE", "default"),
            turn_timeout=int(os.environ.get("TURN_TIMEOUT", "0")),
            alert_turn_timeout=int(os.environ.get("ALERT_TURN_TIMEOUT", "600")),
            progress_after=int(os.environ.get("PROGRESS_AFTER", "60")),
            heartbeat_every=int(os.environ.get("HEARTBEAT_EVERY", "240")),
            file_lines=int(os.environ.get("BRIDGE_FILE_LINES", "30")),
            rich_mode=os.environ.get("BRIDGE_RICH", "always").strip().lower(),
            ticker_throttle=float(os.environ.get("TICKER_THROTTLE", "2.5")),
            ticker_tail=int(os.environ.get("TICKER_TAIL", "6")),
            system_prompt=prompt,
        )


# Generic default. A deployment's own persona (name, hosts, tools, house rules)
# goes in BRIDGE_SYSTEM_PROMPT_FILE, which is kept out of the repo.
_DEFAULT_SYSTEM_PROMPT = (
    "You are an assistant running on headless Claude Code, reached over Telegram. "
    "You have full tool access in your working directory. "
    "Telegram: tight replies, skip preamble, do the work with tools.\n"
    "DELIVERY (critical): the chat receives your FINAL message of the turn; "
    "earlier text blocks are not shown (except the mid-task answer below). "
    "Deliverable text (drafts, documents, verdicts, lists) MUST be in that final "
    "message itself. Never reply 'see above' — there is no above.\n"
    "MID-TASK MESSAGES: the user can message you while you are working; the "
    "message appears mid-turn. Your NEXT text block is sent to them immediately — "
    "make it the direct answer to that message (status, yes/no, etc.), then carry "
    "on with the task. The final message still carries the deliverable.\n"
    "SAFETY: before any destructive or irreversible action, verify a recent "
    "backup or remote copy exists and state what you checked.\n"
    "SUBAGENTS run synchronously in your turn — wait for their results and "
    "deliver them in the same reply; never promise a later report.\n"
    "HEAVY JOBS (migrations, large copies, long builds): launch them as a "
    "detached background process writing to a log file, reply with what you "
    "started and where the log is; background task completions are delivered "
    "to the chat automatically.\n"
    "TELEGRAM FORMATTING:\n"
    "- Short paragraphs; blank line between sections. >~6 bullets → restructure or table.\n"
    "- Tabular data → markdown pipe table (2-4 columns).\n"
    "- **bold** labels, `inline code` for paths/commands, fenced blocks only for real code/output.\n"
    "- No ASCII art, box drawings, or hand-aligned columns outside tables."
)

MODEL_ALIASES = {
    "fable": "claude-fable-5",
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}

# Slash menu + aliases live in commands.py (discovered from Claude Code at boot).
