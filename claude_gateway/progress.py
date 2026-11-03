"""Live activity ticker (Hermes/Talk-style tool progress bubble)."""
from __future__ import annotations

import asyncio
import logging
import time

from telegram.error import RetryAfter

log = logging.getLogger("claude-gateway.progress")


def describe_tool_use(name: str, tool_input: dict) -> str:
    i = tool_input or {}
    detail = ""
    if name in ("Bash", "PowerShell"):
        detail = i.get("description") or (i.get("command") or "")[:90]
    elif name in ("Read", "Write", "Edit", "NotebookEdit"):
        detail = str(i.get("file_path") or i.get("notebook_path") or "")
        detail = detail.replace("\\", "/").split("/")[-1] or detail
    elif name == "Glob":
        detail = i.get("pattern", "")
    elif name == "Grep":
        detail = i.get("pattern", "")
    elif name in ("Agent", "Task"):
        detail = i.get("description", "subagent")
    elif name in ("WebFetch", "WebSearch"):
        detail = (i.get("url") or i.get("query") or "")[:90]
    elif name.startswith("mcp__"):
        name = name.split("__", 2)[-1]
    return f"{name}: {detail}" if detail else name


class TgTicker:
    """Best-effort live ticker; API failure disables it for the turn only."""

    def __init__(self, bot, chat_id: int, *, throttle: float = 2.5, tail: int = 6):
        self.bot = bot
        self.chat_id = chat_id
        self.throttle = throttle
        self.tail = tail
        self.msg_id = None
        self.lines: list[str] = []
        self.count = 0
        self.last_edit = 0.0
        self.dead = False
        self._lock = asyncio.Lock()
        self.mark = "⚙"

    def _body(self, final: bool = False) -> str:
        tail = self.lines[-self.tail :]
        head = (
            f"{self.mark} siap — {self.count} actions:"
            if final
            else f"{self.mark} on it — {self.count} actions so far:"
        )
        return head + "\n" + "\n".join(f"· {l}" for l in tail)

    async def update(self, line: str) -> None:
        if self.dead or not line:
            return
        async with self._lock:
            self.count += 1
            self.lines.append(str(line)[:100])
            now = time.monotonic()
            try:
                if self.msg_id is None:
                    m = await self.bot.send_message(
                        self.chat_id, self._body(), disable_notification=True
                    )
                    self.msg_id = m.message_id
                    self.last_edit = now
                elif now - self.last_edit >= self.throttle:
                    await self.bot.edit_message_text(
                        self._body(), chat_id=self.chat_id, message_id=self.msg_id
                    )
                    self.last_edit = now
            except RetryAfter:
                self.last_edit = now
            except Exception as e:  # noqa: BLE001
                log.info("ticker disabled for chat %s: %s", self.chat_id, e)
                self.dead = True

    async def finish(self) -> None:
        if self.dead or self.msg_id is None:
            return
        async with self._lock:
            try:
                await self.bot.edit_message_text(
                    self._body(final=True), chat_id=self.chat_id, message_id=self.msg_id
                )
            except Exception:  # noqa: BLE001
                pass
