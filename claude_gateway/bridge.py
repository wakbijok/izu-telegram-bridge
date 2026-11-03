"""Lane orchestration + Telegram handlers (gateway waist).

Each session key (chat_id; -chat_id for the alert lane) is a *lane* over one
long-lived Claude Code CLI:

- Inbound: every message goes into the lane's outbox and ONE writer task
  sends them to the CLI in arrival order, each tagged with our own uuid. An
  idle CLI starts a turn; a busy one folds the message into the running turn
  at the next tool boundary, or queues it as the next turn. Chat lanes write
  while busy; alert lanes wait for idle so triages don't mix.
- Outbound: the backend pump reads the CLI stream for the client's whole life.
  With --replay-user-messages the CLI echoes each prompt (our uuid) when it
  consumes it, so every ResultMessage is attributed to the turn that produced
  it and delivered exactly once, in order: user turns, turns Claude Code
  starts on its own after a background task finishes (🔔), and interim
  answers to messages folded into a running turn.
- A lane loop keeps typing/heartbeat alive while the lane is busy and is the
  watchdog: inactivity or the hard cap → interrupt → if the CLI doesn't
  answer, kill it and recover.
- Replies go through a per-chat courier (one sender per chat) so they keep
  stream order and are never cut short by a client being dropped.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    UserMessage,
)
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ContextTypes

from claude_gateway.backend import ClaudeBackend
from claude_gateway.commands import (
    EFFORT_LEVELS,
    NATIVE,
    build_alias_map,
    build_menu,
    hermes_only_hint,
    is_known_cc_slash,
    resolve_cc_slash,
)
from claude_gateway.config import COMPANION_BOT, MODEL_ALIASES, USER_NAME, Settings
from claude_gateway.deliver import Deliverer
from claude_gateway.progress import TgTicker
from claude_gateway.sessions import SessionStore

log = logging.getLogger("claude-gateway.bridge")


def _one(p: "_Prompt"):
    """The SDK's query() takes an async iterable for raw messages; this is how
    the prompt carries our uuid (echoed back by --replay-user-messages)."""

    async def gen():
        yield {
            "type": "user",
            "message": {"role": "user", "content": p.text},
            "parent_tool_use_id": None,
            "session_id": "default",
            "uuid": p.uid,
        }

    return gen()

TERMINAL_TASK = {"completed", "failed", "stopped", "killed"}
CONNECT_TIMEOUT = 180  # CLI spawn + MCP server init
WRITE_TIMEOUT = 30  # a stdin write that blocks this long means a wedged CLI
INTERRUPT_TIMEOUT = 15  # the interrupt control request itself
INTERRUPT_GRACE = 30  # interrupt sent → no ResultMessage by then → kill the CLI
MAX_ATTEMPTS = 2  # a prompt is written at most twice (original + one recovery)
TICK = 1.0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _benign_exit(err: BaseException | None) -> bool:
    """CLI ended by SIGTERM (service restart) — not a crash worth reporting."""
    if err is None:
        return True
    s = str(err)
    return "exit code 143" in s or "exit code -15" in s


def _bloated(err: str) -> bool:
    e = err.lower()
    return (
        "maximum buffer size" in e
        or "exceeded maximum buffer" in e
        or "json message exceeded" in e
        or "failed to decode json" in e
    )


QUOTE_CAP = 6000
SHARED_NOTES_MAX = 10
SHARED_NOTES_CHARS = 12000


def reply_context(msg, bot_id: int | None) -> str:  # noqa: ANN001
    """Telegram 'reply to' → a prefix telling Claude which message is meant.

    Without it the model only sees the new text. The quoted message may also
    come from a session it doesn't share (e.g. the alert lane), so the quoted
    text itself is passed, not just a pointer."""
    r = getattr(msg, "reply_to_message", None)
    q = getattr(msg, "quote", None)
    quoted = (getattr(q, "text", None) or "").strip()  # user-selected part
    if r is None and not quoted:
        return ""
    if not quoted and r is not None:
        quoted = (r.text or r.caption or "").strip()
        if not quoted:
            kind = next(
                (k for k in ("photo", "document", "voice", "video", "sticker", "audio") if getattr(r, k, None)),
                "non-text",
            )
            quoted = f"[{kind} message]"
    who = "an earlier message"
    u = getattr(r, "from_user", None) if r is not None else None
    if u is not None:
        if bot_id is not None and u.id == bot_id:
            who = "your earlier message"
        elif not u.is_bot:
            who = f"{USER_NAME}'s own earlier message"
    if len(quoted) > QUOTE_CAP:
        quoted = quoted[:QUOTE_CAP] + " …[truncated]"
    return f'[Replying to {who}:\n"""\n{quoted}\n"""]\n\n'


def _join(parts: list[str]) -> str:
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


@dataclass
class _Prompt:
    text: str
    uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    attempts: int = 0
    written_at: float = 0.0
    ready: bool = True  # False while a media download fills it in

    @property
    def is_slash(self) -> bool:
        # Slash commands run before the turn's init and are never echoed,
        # batched or folded into a running turn — they need a lane to themselves.
        return self.text.startswith("/")


@dataclass
class Lane:
    key: int
    chat_id: int
    inject: bool  # write while busy (chat) / wait for idle (alerts)
    hard_cap: int
    outbox: deque = field(default_factory=deque)  # _Prompt, not yet written
    unconsumed: dict = field(default_factory=dict)  # uid -> _Prompt, written, not consumed
    # current Claude Code turn
    turn_open: bool = False
    turn_started: float = 0.0
    turn_prompts: int = 0  # our prompts consumed by this turn
    turn_output: bool = False  # any top-level output yet (for interim answers)
    answer_next: bool = False  # a prompt was folded in mid-turn → ship next text
    parts: list = field(default_factory=list)
    interim: str | None = None
    # watchdog / progress
    last_activity: float = 0.0
    last_input: float = 0.0
    stop_reason: str | None = None
    stop_at: float = 0.0
    progress_sent: bool = False
    last_heartbeat: float = 0.0
    bg_tasks: set = field(default_factory=set)
    writer: asyncio.Task | None = None
    loop_task: asyncio.Task | None = None
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    epoch: int = 0  # bumped by /reset; stale writes/media are discarded

    @property
    def slash_pending(self) -> bool:
        return any(p.is_slash for p in self.unconsumed.values())

    @property
    def working(self) -> bool:
        """Claude Code has something of ours in hand (or is mid-turn)."""
        return self.turn_open or bool(self.unconsumed)

    @property
    def busy(self) -> bool:
        return self.working or bool(self.outbox)

    def reset_turn(self) -> None:
        self.turn_open = False
        self.turn_prompts = 0
        self.turn_output = False
        self.answer_next = False
        self.parts = []
        self.interim = None
        self.stop_reason = None
        self.progress_sent = False


class Courier:
    """One sequential sender per chat: replies keep stream order and are not
    cancelled with the task that produced them."""

    def __init__(self):
        self._queues: dict[int, asyncio.Queue] = {}
        self._workers: dict[int, asyncio.Task] = {}

    def post(self, chat_id: int, fn) -> None:  # noqa: ANN001
        q = self._queues.setdefault(chat_id, asyncio.Queue())
        q.put_nowait(fn)
        w = self._workers.get(chat_id)
        if w is None or w.done():
            self._workers[chat_id] = asyncio.get_running_loop().create_task(
                self._work(chat_id, q)
            )

    async def _work(self, chat_id: int, q: asyncio.Queue) -> None:
        while True:
            fn = await q.get()
            try:
                await fn()
            except Exception:  # noqa: BLE001
                log.exception("delivery to chat %s failed", chat_id)
            finally:
                q.task_done()

    async def drain(self, chat_id: int) -> None:
        q = self._queues.get(chat_id)
        if q is not None:
            await q.join()


class Bridge:
    def __init__(self, app: Application, settings: Settings):
        self.app = app
        self.settings = settings
        self.store = SessionStore(settings.sessions_file)
        self.deliverer = Deliverer(app.bot, settings)
        self.courier = Courier()
        self.tickers: dict[int, TgTicker] = {}
        self.lanes: dict[int, Lane] = {}
        # chat_id -> what other lanes (alerts) said there, not yet seen by the chat session
        self.shared_notes: dict[int, deque] = {}
        # approval id -> (chat the buttons were sent to, future the tool call awaits)
        self.pending: dict[str, tuple[int, asyncio.Future]] = {}
        # Headless-proven allowlist (not a dump of get_server_info).
        self.cc_aliases: dict[str, str] = build_alias_map()
        self.menu: list[tuple[str, str]] = build_menu()
        self.cc_command_names: list[str] = list(self.cc_aliases.values())
        self.backend = ClaudeBackend(
            settings,
            self.store,
            on_tool=self._on_tool,
            on_message=self._on_stream_message,
            on_pump_end=self._on_stream_end,
        )
        self.backend.set_approver_factory(self._approver)

    # ---- lanes ----
    def _lane(self, key: int, chat_id: int) -> Lane:
        lane = self.lanes.get(key)
        if lane is None:
            alert = key < 0
            lane = Lane(
                key=key,
                chat_id=chat_id,
                inject=not alert,
                hard_cap=self.settings.alert_turn_timeout if alert else self._hard_cap(),
            )
            lane.idle.set()
            self.lanes[key] = lane
        return lane

    def _hard_cap(self) -> int:
        """Wall-clock backstop, measured from the latest input (or CC turn start)."""
        if self.settings.turn_timeout > 0:
            return self.settings.turn_timeout
        return max(_env_int("BRIDGE_HARD_TIMEOUT", 3600), 60)

    def submit(self, key: int, chat_id: int, text: str) -> Lane:
        """Queue a prompt for the lane's writer. Synchronous: arrival order is
        fixed here, before any await."""
        lane = self._lane(key, chat_id)
        lane.outbox.append(_Prompt(text))
        lane.idle.clear()
        self._ensure_writer(lane)
        self._ensure_loop(lane)
        return lane

    def _ensure_writer(self, lane: Lane) -> None:
        if lane.writer is None or lane.writer.done():
            lane.writer = asyncio.get_running_loop().create_task(self._writer(lane))

    def _ensure_loop(self, lane: Lane) -> None:
        if lane.loop_task is None or lane.loop_task.done():
            lane.loop_task = asyncio.get_running_loop().create_task(self._lane_loop(lane))

    def _mark_idle_if_done(self, lane: Lane) -> None:
        if not lane.working:
            lane.idle.set()

    async def _writer(self, lane: Lane) -> None:
        connect_failures = 0
        while lane.outbox:
            p = lane.outbox[0]
            if not p.ready:
                break  # on_media restarts the writer once the file is in
            # Alerts and slash commands wait for an idle CLI; nothing is
            # written behind an unfinished slash command.
            if lane.slash_pending or ((not lane.inject or p.is_slash) and lane.working):
                lane.idle.clear()
                self._mark_idle_if_done(lane)
                await lane.idle.wait()
                continue
            epoch = lane.epoch
            try:
                cl = await asyncio.wait_for(
                    self.backend.client_for(lane.key), CONNECT_TIMEOUT
                )
            except Exception as e:  # noqa: BLE001
                connect_failures += 1
                log.warning("key %s: connect failed (%d): %s", lane.key, connect_failures, e)
                if connect_failures == 1:
                    await asyncio.sleep(3)
                    continue
                saved = self.store.get(lane.key)
                if connect_failures == 2 and saved:
                    # The saved session may be unresumable; start fresh but say
                    # which session was set aside so it can be resumed by hand.
                    self.store.set(lane.key, None)
                    self._notice(
                        lane.chat_id,
                        "⚠️ Could not reopen the Claude Code session "
                        f"(<code>{html.escape(saved)}</code>): {html.escape(str(e))[:300]}. "
                        "Starting a fresh session; the old one is untouched on disk.",
                        html_mode=True,
                    )
                    continue
                lost = list(lane.outbox)
                lane.outbox.clear()
                self._notice(
                    lane.chat_id,
                    f"⚠️ Claude Code won't start: {html.escape(str(e))[:300]}. "
                    f"{len(lost)} message(s) not processed — send again later.",
                    html_mode=True,
                )
                break
            connect_failures = 0
            if lane.epoch != epoch:
                # /reset ran while we connected: let its drop finish (it is
                # queued on the key lock ahead of us), then open fresh.
                async with self.backend._lock(lane.key):
                    pass
                continue
            if not lane.outbox or lane.outbox[0] is not p:
                continue
            if not p.is_slash and lane.key == lane.chat_id:
                p.text = self._take_shared_notes(lane) + p.text
            p.attempts += 1
            p.written_at = time.monotonic()
            lane.unconsumed[p.uid] = p
            lane.last_input = lane.last_activity = p.written_at
            lane.idle.clear()
            lane.outbox.popleft()
            self._ensure_loop(lane)
            log.info("key %s: write %s (%d chars, attempt %d)", lane.key, p.uid[:8], len(p.text), p.attempts)
            try:
                await asyncio.wait_for(cl.query(_one(p)), WRITE_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                log.warning("key %s: write failed: %s", lane.key, e)
                lane.unconsumed.pop(p.uid, None)
                if p.attempts < MAX_ATTEMPTS:
                    lane.outbox.appendleft(p)
                else:
                    self._notice(
                        lane.chat_id,
                        "⚠️ Couldn't hand a message to Claude Code — send it again.",
                    )
                if self.backend.clients.get(lane.key) is cl:
                    await self.backend.drop(lane.key)
                    await self._recover(lane, f"write failed: {e}")
        self._mark_idle_if_done(lane)

    async def _lane_loop(self, lane: Lane) -> None:
        """Typing, heartbeat and watchdog while the lane is busy."""
        last_typing = 0.0
        try:
            while lane.busy:
                now = time.monotonic()
                if now - last_typing >= 4.5:
                    last_typing = now
                    try:
                        await self.app.bot.send_chat_action(lane.chat_id, "typing")
                    except Exception:  # noqa: BLE001
                        pass
                if lane.working:
                    await self._watch(lane, now)
                await asyncio.sleep(TICK)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("lane loop key=%s", lane.key)

    async def _watch(self, lane: Lane, now: float) -> None:
        started = max(lane.last_input, lane.turn_started)
        ticker = self.tickers.get(lane.key)
        # heartbeat
        if not lane.progress_sent and now - started >= self.settings.progress_after:
            lane.progress_sent = True
            lane.last_heartbeat = now
            if ticker is None:
                self._notice(lane.chat_id, "⚙️ Working on it — this one takes a while…")
        elif lane.progress_sent and now - lane.last_heartbeat >= self.settings.heartbeat_every:
            lane.last_heartbeat = now
            if ticker is None or now - ticker.last_edit > self.settings.heartbeat_every:
                mins = int((now - started) // 60)
                self._notice(lane.chat_id, f"⚙️ Still working ({mins} min)…")
        # watchdog
        if lane.stop_reason is None:
            idle = _env_int("BRIDGE_IDLE_TIMEOUT", 1200)
            reason = None
            if idle > 0 and now - lane.last_activity >= idle:
                reason = f"no activity for {idle // 60} min"
            elif lane.hard_cap > 0 and now - started >= lane.hard_cap:
                reason = f"ran over {max(lane.hard_cap // 60, 1)} min"
            if reason:
                lane.stop_reason, lane.stop_at = reason, now
                log.error("key %s: watchdog — %s; interrupting", lane.key, reason)
                try:
                    await asyncio.wait_for(self.backend.interrupt(lane.key), INTERRUPT_TIMEOUT)
                except Exception:  # noqa: BLE001
                    log.warning("key %s: interrupt failed", lane.key, exc_info=True)
        elif now - lane.stop_at >= INTERRUPT_GRACE:
            reason = lane.stop_reason
            log.error("key %s: no result %ss after interrupt; killing CLI", lane.key, INTERRUPT_GRACE)
            await self.backend.drop(lane.key)
            await self._recover(lane, f"stopped: {reason}; Claude Code did not respond, restarted it")

    # ---- stream routing (backend pump, current client only) ----
    async def _on_tool(self, key: int, line: str) -> None:
        lane = self.lanes.get(key)
        if lane is None:
            return
        lane.last_activity = time.monotonic()
        t = self.tickers.get(key)
        if t is None and not lane.working:
            return  # stray hook from a client being reset
        if t is None:
            t = TgTicker(
                self.app.bot,
                lane.chat_id,
                throttle=self.settings.ticker_throttle,
                tail=self.settings.ticker_tail,
            )
            if lane.turn_open and lane.turn_prompts == 0 and not lane.unconsumed:
                t.mark = "🔔"  # Claude Code working on its own (background task)
            self.tickers[key] = t
        await t.update(line)

    def _open_turn(self, lane: Lane, now: float) -> None:
        lane.reset_turn()
        lane.turn_open = True
        lane.turn_started = now
        lane.idle.clear()
        self._ensure_loop(lane)

    async def _on_stream_message(self, key: int, msg) -> None:  # noqa: ANN001
        lane = self.lanes.get(key)
        if lane is None:
            return  # e.g. the boot-time command probe
        now = time.monotonic()
        lane.last_activity = now
        if isinstance(msg, (AssistantMessage, ResultMessage)):
            self.backend.freshly_resumed.pop(key, None)  # resume evidently worked

        if isinstance(msg, SystemMessage) and not isinstance(
            msg, (TaskStartedMessage, TaskNotificationMessage, TaskUpdatedMessage)
        ):
            if msg.subtype == "init":
                self._open_turn(lane, now)
            return
        if isinstance(msg, TaskStartedMessage):
            lane.bg_tasks.add(msg.task_id)
            return
        if isinstance(msg, TaskNotificationMessage):
            if msg.status in TERMINAL_TASK:
                lane.bg_tasks.discard(msg.task_id)
            log.info("key %s: task %s %s", key, msg.task_id, msg.status)
            return
        if isinstance(msg, TaskUpdatedMessage):
            status = msg.status or (msg.patch or {}).get("status")
            if status in TERMINAL_TASK:
                lane.bg_tasks.discard(msg.task_id)
            return
        if isinstance(msg, RateLimitEvent):
            if getattr(msg.rate_limit_info, "status", None) == "rejected":
                self._notice(lane.chat_id, "⏳ Rate limited — backing off…")
            return
        if isinstance(msg, UserMessage):
            c = msg.content if isinstance(msg.content, str) else ""
            if "<local-command-stdout>" in c:
                out = c.split("<local-command-stdout>", 1)[1].split("</local-command-stdout>", 1)[0]
                if out.strip():
                    lane.parts.append(out.strip())  # slash output (e.g. /compact)
                return
            p = lane.unconsumed.pop(msg.uuid or "", None) if msg.uuid else None
            if p is not None:
                if not lane.turn_open:
                    self._open_turn(lane, now)
                lane.turn_prompts += 1
                if lane.turn_output:
                    lane.answer_next = True  # folded into a running turn
                log.info("key %s: consumed %s", key, p.uid[:8])
            return
        if isinstance(msg, AssistantMessage):
            if msg.parent_tool_use_id:
                return  # subagent-internal
            if not lane.turn_open:
                self._open_turn(lane, now)
            lane.turn_output = True
            texts = [t for b in msg.content if (t := getattr(b, "text", None))]
            text = _join(texts)
            if not text:
                return
            if lane.answer_next:
                # First text after a message was folded into this turn: that's
                # the answer to it — send now, don't wait for the turn to end.
                lane.answer_next = False
                lane.interim = text
                lane.parts = []
                self._lane_deliver(lane, text)
            else:
                lane.parts.append(text)
            return
        if isinstance(msg, ResultMessage):
            await self._on_result(lane, msg, now)

    async def _on_result(self, lane: Lane, msg: ResultMessage, now: float) -> None:
        sid = getattr(msg, "session_id", None)
        if isinstance(sid, str) and sid:
            self.store.set(lane.key, sid)
        if lane.turn_prompts == 0:
            # Plain prompts are always echoed; a slash command never is. An
            # unechoed turn that ends while one is outstanding was that command
            # (the writer never has more than one in flight).
            for uid, p in list(lane.unconsumed.items()):
                if p.is_slash:
                    del lane.unconsumed[uid]
                    lane.turn_prompts += 1
                    break
        user_turn = lane.turn_prompts > 0
        text = (msg.result or "").strip() or _join(lane.parts)
        if text and text == lane.interim:
            text = ""  # already sent as the interim answer
        stop_reason = lane.stop_reason
        log.info(
            "key %s: result %s error=%s %.0fs (%s)",
            lane.key,
            msg.subtype,
            msg.is_error,
            (msg.duration_ms or 0) / 1000,
            "user" if user_turn else "CC-initiated",
        )
        # Queue the reply before any await: a drop can't lose it after this.
        if text:
            self._lane_deliver(lane, text if user_turn else "🔔 " + text)
        if stop_reason:
            self._notice(
                lane.chat_id,
                f"⌛ Stopped: {stop_reason}. Send a message to continue, or /reset if it keeps sticking.",
            )
        ticker = self.tickers.pop(lane.key, None)
        lane.reset_turn()
        self._mark_idle_if_done(lane)
        if ticker is not None:
            await ticker.finish()

    async def _on_stream_end(self, key: int, err: BaseException | None) -> None:
        """The key's current client died on its own (not a deliberate drop)."""
        lane = self.lanes.get(key)
        if lane is None:
            return
        if _benign_exit(err) and not lane.working:
            return
        if _benign_exit(err) and err is not None:
            return  # service is stopping; prompts die with it
        fresh = self.backend.freshly_resumed.pop(key, None)
        if err is not None and (fresh or _bloated(str(err))):
            saved = self.store.get(key)
            if saved and fresh:
                self.store.set(key, None)
                self._notice(
                    lane.chat_id,
                    f"⚠️ Session <code>{html.escape(saved)}</code> failed on resume; "
                    "starting fresh (old session untouched on disk).",
                    html_mode=True,
                )
        reason = "Claude Code exited" if err is None else f"Claude Code crashed: {err}"
        await self._recover(lane, reason)

    async def _recover(self, lane: Lane, reason: str) -> None:
        """After the lane's client died or was killed: close the turn, re-send
        messages the CLI never read, and tell the user what happened."""
        ticker = self.tickers.pop(lane.key, None)
        if ticker is not None:
            await ticker.finish()
        had_turn = lane.turn_open
        partial = _join(lane.parts)
        lane.reset_turn()
        resend, lost = [], []
        for p in sorted(lane.unconsumed.values(), key=lambda p: p.written_at):
            (resend if p.attempts < MAX_ATTEMPTS else lost).append(p)
        lane.unconsumed.clear()
        if partial:
            self._lane_deliver(lane, partial)
        bits = [f"⚠️ {html.escape(reason)[:400]}."]
        if had_turn:
            bits.append("The running task was cut off — send a message to continue it.")
        if resend:
            bits.append(f"Re-sending {len(resend)} message(s) it hadn't read yet.")
        if lost:
            bits.append(f"{len(lost)} message(s) could not be processed — send them again.")
        if lane.bg_tasks:
            bits.append(f"{len(lane.bg_tasks)} background task(s) died with it.")
            lane.bg_tasks.clear()
        self._notice(lane.chat_id, " ".join(bits), html_mode=True)
        for p in reversed(resend):
            # New uuid: the resumed CLI treats a known uuid as already handled.
            p.uid = str(uuid.uuid4())
            lane.outbox.appendleft(p)
        if lane.outbox:
            self._ensure_writer(lane)
            self._ensure_loop(lane)
        self._mark_idle_if_done(lane)

    # ---- outbound ----
    def _lane_deliver(self, lane: Lane, text: str) -> None:
        if lane.key != lane.chat_id:
            # Another session (the alert lane) is speaking in this chat. Keep
            # what it said so the chat session hears it with the next message:
            # to the user it is one assistant in one chat.
            notes = self.shared_notes.setdefault(lane.chat_id, deque())
            notes.append(text)
            while len(notes) > SHARED_NOTES_MAX or sum(map(len, notes)) > SHARED_NOTES_CHARS:
                notes.popleft()
        self._deliver(lane.chat_id, text)

    def _take_shared_notes(self, lane: Lane) -> str:
        notes = self.shared_notes.pop(lane.chat_id, None) if lane.key == lane.chat_id else None
        if not notes:
            return ""
        body = "\n---\n".join(notes)
        return (
            "[Context: since your last turn, your alert lane (a separate session that "
            f"triages Grafana/Keep/GLPI alerts) posted these messages in this chat. "
            f"{USER_NAME} saw them as coming from you; treat them as your own words.\n"
            f'"""\n{body}\n"""]\n\n'
        )

    def _deliver(self, chat_id: int, text: str) -> None:
        self.courier.post(chat_id, lambda: self.deliverer.send(chat_id, text))

    def _notice(self, chat_id: int, text: str, *, html_mode: bool = False) -> None:
        kw = {"parse_mode": "HTML"} if html_mode else {}
        self.courier.post(chat_id, lambda: self.app.bot.send_message(chat_id, text, **kw))

    # ---- boot ----
    async def refresh_commands_from_claude(self) -> list[tuple[str, str]]:
        """Register the headless-proven menu (optional log of raw CC inventory)."""
        self.cc_aliases = build_alias_map()
        self.menu = build_menu()
        self.cc_command_names = sorted(set(self.cc_aliases.values()))
        # Optional: log how many names CC advertises vs what we expose.
        probe_key = -999001
        try:
            cl = await self.backend.client_for(probe_key)
            info = await cl.get_server_info()
            raw = info.get("commands") if isinstance(info, dict) else []
            advertised = len(raw) if isinstance(raw, list) else 0
            log.info(
                "CC advertises %d command names; exposing %d headless-proven + %d gateway natives",
                advertised,
                len(self.cc_command_names),
                len(NATIVE),
            )
        except Exception:  # noqa: BLE001
            log.warning("optional CC inventory probe failed; menu still allowlisted", exc_info=True)
        finally:
            try:
                await self.backend.drop(probe_key, forget_saved=True)
            except Exception:  # noqa: BLE001
                pass
        return self.menu

    def allowed(self, update: Update) -> bool:
        u = update.effective_user
        return bool(u and u.id in self.settings.allowed_user_ids)

    # ---- handlers ----
    APPROVAL_PREVIEW = 600

    @staticmethod
    def approval_preview(text: str, cap: int = APPROVAL_PREVIEW) -> str:
        """Head + tail of a tool input, with an explicit marker for what is cut,
        so a long input cannot hide its payload past the preview."""
        if len(text) <= cap:
            return text
        head = cap * 2 // 3
        tail = cap - head
        cut = len(text) - head - tail
        return (f"{text[:head]}\n… [truncated {cut} chars] …\n{text[-tail:]}"
                f"\n(total {len(text)} chars)")

    def _approver(self, chat_id: int):
        async def approve(tool_name, input_data, context):  # noqa: ANN001, ARG001
            # Unguessable id: callback data is client-supplied, never trust a counter.
            aid = secrets.token_urlsafe(12)
            fut = asyncio.get_running_loop().create_future()
            target = chat_id if chat_id > 0 else -chat_id
            self.pending[aid] = (target, fut)
            preview = self.approval_preview(str(input_data))
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Allow", callback_data=f"ok:{aid}"),
                        InlineKeyboardButton("⛔ Deny", callback_data=f"no:{aid}"),
                    ]
                ]
            )
            await self.app.bot.send_message(
                target,
                f"🔐 Approve <b>{html.escape(tool_name)}</b>?\n<pre>{html.escape(preview)}</pre>",
                parse_mode="HTML",
                reply_markup=kb,
            )
            allowed = await fut
            if allowed:
                return PermissionResultAllow(updated_input=input_data)
            return PermissionResultDeny(message="Denied via Telegram.")

        return approve

    async def on_message(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self.allowed(update):
            await update.message.reply_text("⛔ Not authorized.")
            return
        text = (update.message.text or "").strip()
        # Hyphenated CC slashes arrive as plain text (Telegram BotCommand charset).
        if text.startswith("/") and not text.startswith("//"):
            head, _, arg = text[1:].partition(" ")
            cmd = head.split("@", 1)[0].lower()
            await self._dispatch_slash(update.effective_chat.id, cmd, arg.strip())
            return
        self._enqueue(update, self._reply_prefix(update.message) + text)

    async def on_media(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self.allowed(update):
            await update.message.reply_text("⛔ Not authorized.")
            return
        msg = update.message
        chat_id = update.effective_chat.id
        # Reserve the message's place in line before the (slow) download.
        lane = self._lane(chat_id, chat_id)
        slot = _Prompt("", ready=False)
        epoch = lane.epoch
        lane.outbox.append(slot)
        lane.idle.clear()
        is_image = bool(msg.photo) or bool(
            msg.document and (msg.document.mime_type or "").startswith("image/")
        )
        try:
            if msg.photo:
                tg_file = await msg.photo[-1].get_file()
                name = f"{msg.chat_id}_{msg.message_id}_{msg.photo[-1].file_unique_id}.jpg"
            else:
                tg_file = await msg.document.get_file()
                ext = Path(msg.document.file_name or "file.bin").suffix or ".bin"
                name = f"{msg.chat_id}_{msg.message_id}_{msg.document.file_unique_id}{ext}"
            self.settings.media_dir.mkdir(parents=True, exist_ok=True)
            path = self.settings.media_dir / name
            await tg_file.download_to_drive(str(path))
        except Exception as e:  # noqa: BLE001
            log.exception("media download failed")
            if slot in lane.outbox:
                lane.outbox.remove(slot)
            self._ensure_writer(lane)
            self._mark_idle_if_done(lane)
            await msg.reply_text(f"⚠️ File download failed: {html.escape(str(e))}")
            return
        caption = (msg.caption or "").strip()
        if is_image:
            prompt = (
                f"[{USER_NAME} sent a photo via Telegram; saved to {path}. "
                "Read it with the Read tool first, then respond to its content.]"
            )
        else:
            orig_name = msg.document.file_name or "unknown"
            prompt = (
                f"[{USER_NAME} sent the file '{orig_name}' via Telegram; saved to {path}. "
                "Read/process it first (use a suitable document skill if needed), "
                "then respond to its content.]"
            )
        if caption:
            prompt += f"\nCaption: {caption}"
        prompt = self._reply_prefix(msg) + prompt
        if lane.epoch != epoch or slot not in lane.outbox:
            return  # /reset discarded it while downloading
        slot.text = prompt  # filled in place: keeps its arrival position
        slot.ready = True
        self._ensure_writer(lane)
        self._ensure_loop(lane)

    def _reply_prefix(self, msg) -> str:  # noqa: ANN001
        try:
            bot_id = self.app.bot.id
        except Exception:  # noqa: BLE001
            bot_id = None
        return reply_context(msg, bot_id)

    def _enqueue(self, update: Update, text: str) -> None:
        chat_id = update.effective_chat.id
        lane = self.lanes.get(chat_id)
        if lane is not None and lane.busy:
            # Fire-and-forget: the ack must never delay or reorder the write.
            asyncio.get_running_loop().create_task(self._ack(update))
        self.submit(chat_id, chat_id, text)

    @staticmethod
    async def _ack(update: Update) -> None:
        try:
            await update.message.set_reaction("👀")
        except Exception:  # noqa: BLE001
            pass

    async def enqueue_alert(self, chat_id: int, prompt: str):
        self.submit(-chat_id, chat_id, prompt)

    async def on_command(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self.allowed(update):
            await update.message.reply_text("⛔ Not authorized.")
            return
        text = update.message.text or ""
        head, _, arg = text.partition(" ")
        cmd = head[1:].split("@", 1)[0].lower()
        await self._dispatch_slash(update.effective_chat.id, cmd, arg.strip())

    async def _dispatch_slash(self, chat_id: int, cmd: str, arg: str) -> None:
        if cmd in NATIVE:
            await getattr(self, f"_cmd_{cmd}")(chat_id, arg)
            return

        hint = hermes_only_hint(cmd)
        if hint:
            await self.app.bot.send_message(chat_id, f"ℹ️ /{cmd} — {hint}")
            return

        real = resolve_cc_slash(cmd, self.cc_aliases)
        if not is_known_cc_slash(real, self.cc_aliases):
            natives = ", ".join("/" + n for n in sorted(NATIVE))
            await self.app.bot.send_message(
                chat_id,
                f"Unknown here: /{cmd}\n"
                f"Gateway only: {natives}\n"
                "Plus headless-proven Claude Code slashes (/help)."
                + (f"\nOther commands may belong to {COMPANION_BOT}." if COMPANION_BOT else ""),
            )
            return
        # Same ordered path as plain messages.
        self.submit(chat_id, chat_id, f"/{real}{(' ' + arg) if arg else ''}")

    async def on_button(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        # Only an allowlisted user, pressing the button in the chat the approval
        # was sent to, decides; anything else leaves the request pending.
        if not self.allowed(update):
            await q.answer("⛔ Not authorized.")
            return
        action, _, aid = (q.data or "").partition(":")
        entry = self.pending.get(aid)
        chat = update.effective_chat
        if entry is None or action not in ("ok", "no"):
            await q.answer("Expired.")
            return
        target, fut = entry
        if chat is None or chat.id != target:
            await q.answer("⛔ Wrong chat.")
            return
        self.pending.pop(aid, None)
        await q.answer()
        if not fut.done():
            fut.set_result(action == "ok")
        await q.edit_message_text("✅ Allowed" if action == "ok" else "⛔ Denied")

    async def _cmd_help(self, chat_id: int, _arg: str):
        natives = " ".join(f"/{n}" for n in sorted(NATIVE))
        cc = " ".join(f"/{c}" for c, _ in self.menu if c not in NATIVE)
        cur_e = self.backend.effort_for(chat_id)
        cur_m = self.backend.model_for(chat_id)
        me = getattr(self.app.bot, "username", None)
        await self.app.bot.send_message(
            chat_id,
            f"<b>Claude Code bot</b>{f' (@{html.escape(me)})' if me else ''}\n"
            f"Now: model=<code>{html.escape(cur_m)}</code> "
            f"effort=<code>{html.escape(cur_e)}</code>\n\n"
            f"<b>Gateway:</b> {html.escape(natives)}\n"
            f"<b>Claude Code (proven headless):</b>\n{html.escape(cc)}\n\n"
            + (f"<b>Not here:</b> <code>/reasoning</code>, <code>/fast</code>, "
               f"<code>/cron</code>… → {html.escape(COMPANION_BOT)}.\n" if COMPANION_BOT else "")
            +
            "Claude thinking budget = <code>/effort low|medium|high|xhigh|max</code> "
            "(gateway applies --effort; not a CC slash headless).\n"
            "Messages sent while Izu is working go straight into the running task; "
            "<code>/stop</code> interrupts it.\n"
            "Menu is <b>allowlist</b>, not all ~90 names CC advertises.",
            parse_mode="HTML",
        )

    def _busy_reason(self, chat_id: int) -> str | None:
        lane = self.lanes.get(chat_id)
        if lane is None:
            return None
        if lane.busy:
            return "a task is running — /stop it (or wait), then try again"
        if lane.bg_tasks:
            return (
                f"{len(lane.bg_tasks)} background task(s) are still running and would "
                "be killed — wait for them, or /reset"
            )
        return None

    async def _cmd_reset(self, chat_id: int, _arg: str):
        lane = self.lanes.get(chat_id)
        dropped = 0
        ticker = self.tickers.pop(chat_id, None)
        if lane is not None:
            dropped = len(lane.outbox) + len(lane.unconsumed)
            lane.epoch += 1
            lane.outbox.clear()
            lane.unconsumed.clear()
            lane.reset_turn()
            lane.bg_tasks.clear()
            lane.idle.set()
        # Drop before any other await so nothing sent after /reset can be
        # written into the old CLI.
        await self.backend.drop(chat_id, forget_saved=True)
        if ticker is not None:
            await ticker.finish()
        extra = f" ({dropped} queued message(s) discarded)" if dropped else ""
        await self.app.bot.send_message(
            chat_id, f"🔄 Session reset — next message starts fresh.{extra}"
        )

    async def _cmd_stop(self, chat_id: int, _arg: str):
        lane = self.lanes.get(chat_id)
        if lane is None or not lane.working:
            await self.app.bot.send_message(chat_id, "Nothing running.")
            return
        try:
            ok = await asyncio.wait_for(self.backend.interrupt(chat_id), INTERRUPT_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            await self.app.bot.send_message(chat_id, f"⚠️ Interrupt failed: {html.escape(str(e))}")
            return
        await self.app.bot.send_message(chat_id, "🛑 Interrupted." if ok else "Nothing running.")

    async def _cmd_model(self, chat_id: int, arg: str):
        if not arg:
            cur = self.backend.model_for(chat_id)
            await self.app.bot.send_message(
                chat_id,
                f"Current model: {cur}\nUsage: /model opus | sonnet | haiku | fable",
            )
            return
        name = MODEL_ALIASES.get(arg.lower(), arg)
        busy = self._busy_reason(chat_id)
        try:
            await self.backend.set_model_name(chat_id, name, allow_reopen=busy is None)
            await self.app.bot.send_message(chat_id, f"🧠 Model → {name}")
        except Exception as e:  # noqa: BLE001
            msg = f"can't switch now: {busy}" if busy else html.escape(str(e))
            await self.app.bot.send_message(chat_id, f"⚠️ {msg}")

    async def _cmd_effort(self, chat_id: int, arg: str):
        """Claude Code effort (thinking budget)."""
        cur = self.backend.effort_for(chat_id)
        if not arg:
            levels = "|".join(EFFORT_LEVELS)
            await self.app.bot.send_message(
                chat_id,
                f"Current effort: {cur}\n"
                f"Usage: /effort {levels}\n"
                "(Claude Code --effort / thinking budget.)",
            )
            return
        level = arg.strip().lower().split()[0]
        synonyms = {
            "none": "low",
            "minimal": "low",
            "min": "low",
            "med": "medium",
            "normal": "medium",
            "think": "high",
            "maxx": "max",
        }
        level = synonyms.get(level, level)
        if level not in EFFORT_LEVELS:
            await self.app.bot.send_message(
                chat_id,
                f"Unknown effort {level}. Use: {' | '.join(EFFORT_LEVELS)}",
            )
            return
        busy = self._busy_reason(chat_id)
        if busy:
            await self.app.bot.send_message(chat_id, f"⏳ Effort not changed: {busy}.")
            return
        try:
            await self.backend.set_effort(chat_id, level)
            await self.app.bot.send_message(
                chat_id,
                f"⚡ Effort → {level} (client reopened; session resume kept)",
            )
        except Exception as e:  # noqa: BLE001
            await self.app.bot.send_message(chat_id, f"⚠️ {html.escape(str(e))}")
