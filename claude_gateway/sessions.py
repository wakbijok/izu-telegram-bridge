"""Persistent chat_id -> Claude session_id map (survive gateway restarts)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("claude-gateway.sessions")


class SessionStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in data.items() if v}
        except Exception:  # noqa: BLE001
            return {}

    def get(self, chat_id: int) -> str | None:
        return self.load().get(str(chat_id))

    def set(self, chat_id: int, session_id: str | None) -> None:
        m = self.load()
        if session_id is None:
            m.pop(str(chat_id), None)
        else:
            m[str(chat_id)] = session_id
        self.path.write_text(json.dumps(m, indent=1), encoding="utf-8")
        log.debug("session store chat=%s sid=%s", chat_id, session_id)
