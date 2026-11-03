"""Claude Code backend — the only model executor this gateway shells to."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
)
from claude_gateway.config import Settings, repo_path
from claude_gateway.progress import describe_tool_use
from claude_gateway.sessions import SessionStore

log = logging.getLogger("claude-gateway.backend")

# Auto-approved in gated mode. NOTE: Read/Grep/Glob are unrestricted by path
# (they can read .env and other secrets in reach); scoping them is an open
# owner decision, deliberately left unchanged here.
AUTO_TOOLS = ["Read", "Grep", "Glob"]

# The bridge's own secrets (read by config.py / alert_webhook.py). The Claude
# Code CLI and everything it spawns (Bash, MCP servers) must never see them.
# CLAUDE_CODE_OAUTH_TOKEN is NOT here: it is the CLI's own login.
BRIDGE_SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "ALERT_TOKEN")


def env_overrides(environ=None) -> dict[str, str]:
    """ClaudeAgentOptions.env that scrubs the bridge's secrets from the CLI.
    The SDK builds the child env as {**os.environ, **options.env}: it can
    override keys but never drop them, so each bridge secret present is
    blanked — the CLI sees the name with an empty value, never the value."""
    src = os.environ if environ is None else environ
    return {k: "" for k in BRIDGE_SECRET_ENV if k in src}


def load_mcp_servers() -> dict:
    path = os.environ.get("BRIDGE_MCP_SERVERS_FILE", "").strip()
    if not path:
        return {}
    data = json.loads(repo_path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("BRIDGE_MCP_SERVERS_FILE must hold a JSON object")
    return data


class ClaudeBackend:
    """One persistent ClaudeSDKClient per session key (chat or alert).

    Every client gets a *pump*: one task that reads the CLI's output stream for
    the client's whole life and hands each message to ``on_message``. Reading
    only inside a user turn (receive_response) desyncs the chat: Claude Code
    runs turns on its own when a background task finishes, and each unread
    ResultMessage makes the next user turn pop a stale reply ("lag-N").

    Opening and closing a key's client is serialized by a per-key lock, so two
    callers can never start two CLIs on one session. Stream callbacks only fire
    for the key's *current* client; a client that was dropped on purpose is
    silent (the caller of drop() owns the consequences).
    """

    def __init__(
        self,
        settings: Settings,
        store: SessionStore,
        *,
        on_tool: Callable[[int, str], Awaitable[None]] | None = None,
        on_message: Callable[[int, Any], Awaitable[None]] | None = None,
        on_pump_end: Callable[[int, BaseException | None], Awaitable[None]] | None = None,
    ):
        self.settings = settings
        self.store = store
        self.on_tool = on_tool
        self.on_message = on_message
        self.on_pump_end = on_pump_end
        self.clients: dict[int, ClaudeSDKClient] = {}
        self.pumps: dict[int, asyncio.Task] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self.freshly_resumed: dict[int, bool] = {}
        self.effort_by_key: dict[int, str] = {}
        self.model_by_key: dict[int, str] = {}
        self._approver_factory = None  # set by Bridge for gated mode

    def set_approver_factory(self, factory) -> None:
        self._approver_factory = factory

    def effort_for(self, session_key: int) -> str:
        return self.effort_by_key.get(session_key, self.settings.effort)

    def model_for(self, session_key: int) -> str:
        # Alert-lane keys are negative (enqueue_alert submits -chat_id). The alert
        # lane is an executor: give it its own default so triage turns never burn
        # the chat lane's premium-model quota. A runtime /model still wins.
        if session_key in self.model_by_key:
            return self.model_by_key[session_key]
        if session_key < 0 and self.settings.alert_model:
            return self.settings.alert_model
        return self.settings.model

    def _lock(self, session_key: int) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def client_for(self, session_key: int) -> ClaudeSDKClient:
        cl = self.clients.get(session_key)
        if cl is not None:
            return cl
        async with self._lock(session_key):
            return await self._open(session_key)

    async def _open(self, session_key: int) -> ClaudeSDKClient:
        cl = self.clients.get(session_key)
        if cl is not None:
            return cl
        effort = self.effort_for(session_key)
        model = self.model_for(session_key)
        opts: dict = dict(
            model=model,
            fallback_model=self.settings.fallback_model,
            effort=effort,
            system_prompt=self.settings.system_prompt,
            cwd=self.settings.workdir,
            include_partial_messages=False,
            # Default SDK limit is 1MB per JSON line; long tool results / resumed
            # transcripts blow past it and kill the turn with a silent-looking hang.
            max_buffer_size=int(
                os.environ.get("BRIDGE_MAX_BUFFER_SIZE", str(16 * 1024 * 1024))
            ),
            # Echo each prompt (with the uuid we sent) when the CLI consumes it:
            # the bridge's only reliable way to attribute a ResultMessage to the
            # message(s) it answers, incl. ones folded into a running turn.
            extra_args={"replay-user-messages": None},
            # Bridge secrets (bot token, alert token) stay out of the CLI's env.
            env=env_overrides(),
        )
        # Extra MCP servers (e.g. a credential broker so the agent requests
        # actions, never secret values) come from a local JSON file:
        # {"name": {"command": "...", "args": [...], "env": {...}}}
        mcp = load_mcp_servers()
        if mcp:
            opts["mcp_servers"] = mcp
        saved = self.store.get(session_key)
        if saved:
            opts["resume"] = saved
            self.freshly_resumed[session_key] = True
            log.info("resuming session %s for key %s", saved, session_key)

        if self.settings.permission_mode == "bypassPermissions":
            opts["permission_mode"] = "bypassPermissions"
        else:
            opts["permission_mode"] = self.settings.permission_mode
            opts["allowed_tools"] = AUTO_TOOLS
            if self._approver_factory:
                opts["can_use_tool"] = self._approver_factory(session_key)

        async def _on_pre_tool(inp, tool_use_id, ctx):  # noqa: ANN001, ARG001
            if self.on_tool is not None:
                await self.on_tool(
                    session_key,
                    describe_tool_use(
                        str(inp.get("tool_name", "?")), inp.get("tool_input") or {}
                    ),
                )
            # Subagents run synchronously: the reply to a message should carry
            # the subagent's result rather than a promise of a later report.
            if str(inp.get("tool_name")) in ("Agent", "Task"):
                ti = dict(inp.get("tool_input") or {})
                if ti.get("run_in_background") is not False:
                    ti["run_in_background"] = False
                    return {"hookSpecificOutput": {
                        "hookEventName": "PreToolUse", "updatedInput": ti}}
            return {}

        async def _on_subagent_start(inp, tool_use_id, ctx):  # noqa: ANN001, ARG001
            if self.on_tool is not None:
                await self.on_tool(session_key, "Agent: delegated task started")
            return {}

        opts["hooks"] = {
            "PreToolUse": [HookMatcher(hooks=[_on_pre_tool])],
            "SubagentStart": [HookMatcher(hooks=[_on_subagent_start])],
        }
        cl = ClaudeSDKClient(options=ClaudeAgentOptions(**opts))
        await cl.connect()
        self.clients[session_key] = cl
        self.pumps[session_key] = asyncio.create_task(self._pump(session_key, cl))
        log.info(
            "opened Claude session key=%s model=%s effort=%s",
            session_key,
            model,
            effort,
        )
        return cl

    async def set_effort(self, session_key: int, effort: str) -> str:
        """Persist effort and reopen client (CC has no headless /effort slash)."""
        self.effort_by_key[session_key] = effort
        async with self._lock(session_key):
            # Keep resume id so context continues; drop live client only.
            await self._drop(session_key, forget_saved=False)
            await self._open(session_key)
        return effort

    async def set_model_name(
        self, session_key: int, model: str, *, allow_reopen: bool = True
    ) -> str:
        cl = self.clients.get(session_key)
        if cl is None:
            # Nothing live to reopen: record it, and apply to a client that is
            # connecting right now (lock) instead of dropping it.
            self.model_by_key[session_key] = model
            async with self._lock(session_key):
                cl = self.clients.get(session_key)
            if cl is not None:
                await cl.set_model(model)
            return model
        if cl is not None:
            try:
                await cl.set_model(model)
                self.model_by_key[session_key] = model
                return model
            except Exception:  # noqa: BLE001
                if not allow_reopen:
                    raise
                log.warning("set_model failed; reopening client", exc_info=True)
        self.model_by_key[session_key] = model
        async with self._lock(session_key):
            await self._drop(session_key, forget_saved=False)
            await self._open(session_key)
        return model

    async def _pump(self, session_key: int, cl: ClaudeSDKClient) -> None:
        err: BaseException | None = None
        try:
            async for msg in cl.receive_messages():
                if self.on_message is None or self.clients.get(session_key) is not cl:
                    continue
                try:
                    await self.on_message(session_key, msg)
                except Exception:  # noqa: BLE001
                    log.exception("on_message failed key=%s", session_key)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            err = e
            log.warning("stream for key=%s failed: %s", session_key, e)
        else:
            log.warning("stream for key=%s ended (CLI exited)", session_key)
        current = self.clients.get(session_key) is cl
        if current:
            # Forget it first so the next write reopens a fresh client.
            self.clients.pop(session_key, None)
            if self.pumps.get(session_key) is asyncio.current_task():
                self.pumps.pop(session_key, None)
            if self.on_pump_end is not None:
                try:
                    await self.on_pump_end(session_key, err)
                except Exception:  # noqa: BLE001
                    log.exception("on_pump_end failed key=%s", session_key)
        try:
            await cl.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def drop(self, session_key: int, *, forget_saved: bool = False) -> None:
        async with self._lock(session_key):
            await self._drop(session_key, forget_saved=forget_saved)

    async def _drop(self, session_key: int, *, forget_saved: bool = False) -> None:
        pump = self.pumps.pop(session_key, None)
        if pump is not None and pump is not asyncio.current_task() and not pump.done():
            pump.cancel()
        cl = self.clients.pop(session_key, None)
        self.freshly_resumed.pop(session_key, None)
        if forget_saved:
            self.store.set(session_key, None)
            self.effort_by_key.pop(session_key, None)
            self.model_by_key.pop(session_key, None)
        if cl is not None:
            try:
                await cl.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def interrupt(self, session_key: int) -> bool:
        cl = self.clients.get(session_key)
        if cl is None:
            return False
        await cl.interrupt()
        return True
