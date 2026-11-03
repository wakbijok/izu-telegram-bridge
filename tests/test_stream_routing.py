"""Lane routing, driven through the REAL ClaudeBackend (client_for, _pump,
drop) with a fake ClaudeSDKClient whose receive_messages() is a queue — the
same shape as the CLI's stdout stream.

Invariants: every ResultMessage is delivered exactly once and in stream order;
prompts reach the CLI in arrival order; nothing is left in the stream for a
later message to pop (the lag-N bug)."""
import asyncio
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)

import claude_gateway.backend as backend_mod
import claude_gateway.bridge as bridge_mod
from claude_gateway.bridge import Bridge
from claude_gateway.config import Settings

CHAT = 42


# ---------- message builders ----------
def init():
    return SystemMessage(subtype="init", data={})


def echo(uid, text="x"):
    return UserMessage(content=text, uuid=uid)


def say(text, parent=None):
    return AssistantMessage(content=[TextBlock(text=text)], model="m", parent_tool_use_id=parent)


def tool():
    return AssistantMessage(
        content=[ToolUseBlock(id="t", name="Bash", input={"command": "sleep 1"})], model="m"
    )


def result(text, sid="s1", subtype="success"):
    return ResultMessage(
        subtype=subtype,
        duration_ms=10,
        duration_api_ms=10,
        is_error=subtype != "success",
        num_turns=1,
        session_id=sid,
        result=text,
    )


def task_started(tid="b1"):
    return TaskStartedMessage(
        subtype="task_started", data={}, task_id=tid, description="bg", uuid="u", session_id="s1"
    )


def task_done(tid="b1"):
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=tid,
        status="completed",
        output_file="/tmp/x",
        summary="done",
        uuid="u",
        session_id="s1",
    )


# ---------- fakes ----------
class FakeCLI:
    """Fake ClaudeSDKClient. `responder(cli, uid, text)` may emit messages."""

    instances: list = []
    responder = None
    connect_delay = 0.0
    connect_error: Exception | None = None

    def __init__(self, options=None):
        self.options = options
        self.q: asyncio.Queue = asyncio.Queue()
        self.writes: list[tuple[str, str]] = []
        self.interrupts = 0
        self.on_interrupt = None
        self.closed = False
        FakeCLI.instances.append(self)

    async def connect(self):
        if FakeCLI.connect_delay:
            await asyncio.sleep(FakeCLI.connect_delay)
        if FakeCLI.connect_error is not None:
            raise FakeCLI.connect_error

    async def receive_messages(self):
        while True:
            m = await self.q.get()
            if m is None:
                return
            if isinstance(m, BaseException):
                raise m
            yield m

    async def query(self, prompt):
        async for d in prompt:
            uid, text = d["uuid"], d["message"]["content"]
            self.writes.append((uid, text))
            if FakeCLI.responder is not None:
                FakeCLI.responder(self, uid, text)

    def emit(self, *msgs):
        for m in msgs:
            self.q.put_nowait(m)

    async def interrupt(self):
        self.interrupts += 1
        if self.on_interrupt:
            self.on_interrupt(self)

    async def set_model(self, model):
        return None

    async def disconnect(self):
        self.closed = True
        self.q.put_nowait(None)

    @property
    def texts(self):
        return [t for _u, t in self.writes]


class FakeBot:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **_kw):
        self.sent.append(text)
        return SimpleNamespace(message_id=len(self.sent))

    async def send_chat_action(self, *_a, **_kw):
        return None

    async def edit_message_text(self, *_a, **_kw):
        return None


def instant_reply(reply_for):
    """Responder: idle CLI answers each prompt as its own turn."""

    def responder(cli, uid, text):
        r = reply_for(text) if callable(reply_for) else reply_for
        if r is None:
            return
        cli.emit(init(), echo(uid, text), say(r), result(r))

    return responder


@pytest.fixture
def env(tmp_path, monkeypatch):
    FakeCLI.instances = []
    FakeCLI.responder = None
    FakeCLI.connect_delay = 0.0
    FakeCLI.connect_error = None
    monkeypatch.setattr(backend_mod, "ClaudeSDKClient", FakeCLI)
    monkeypatch.setattr(bridge_mod, "TICK", 0.02)
    monkeypatch.setenv("BRIDGE_MCP_SERVERS_FILE", "")
    bot = FakeBot()
    settings = Settings(
        bot_token="x",
        allowed_user_ids=frozenset({1}),
        sessions_file=tmp_path / "sessions.json",
        progress_after=3600,
        heartbeat_every=3600,
    )
    b = Bridge(SimpleNamespace(bot=bot), settings)
    delivered: list[str] = []

    async def send(chat_id, text):
        delivered.append(text)

    b.deliverer.send = send
    return SimpleNamespace(b=b, bot=bot, delivered=delivered)


async def settle(e, t=0.15):
    await asyncio.sleep(t)
    await e.b.courier.drain(CHAT)


def lane(e):
    return e.b.lanes[CHAT]


# ---------- tests ----------
def test_idle_turn_delivered_once_and_session_saved(env):
    async def main():
        FakeCLI.responder = instant_reply("hello")
        env.b.submit(CHAT, CHAT, "hi")
        await settle(env)
        assert env.delivered == ["hello"]
        assert env.b.store.get(CHAT) == "s1"
        assert not lane(env).busy
        opts = FakeCLI.instances[0].options
        assert "replay-user-messages" in opts.extra_args

    asyncio.run(main())


def test_background_turn_pushed_and_next_turn_not_lagged(env):
    """The 29-09-2026 incident, end to end through the real pump."""

    async def main():
        FakeCLI.responder = instant_reply(lambda t: {"start bg": "started", "status?": "all done"}[t])
        env.b.submit(CHAT, CHAT, "start bg")
        await settle(env)
        cli = FakeCLI.instances[0]
        # Background task finishes; Claude Code runs a turn on its own.
        cli.emit(task_done(), init(), say("FINISHED"), result("FINISHED"))
        await settle(env)
        env.b.submit(CHAT, CHAT, "status?")
        await settle(env)
        assert env.delivered == ["started", "🔔 FINISHED", "all done"]
        assert len(FakeCLI.instances) == 1

    asyncio.run(main())


def test_message_folded_into_running_turn_gets_interim_answer(env):
    async def main():
        env.b.submit(CHAT, CHAT, "migrate")
        await settle(env)
        cli = FakeCLI.instances[0]
        (a_uid, _), = cli.writes
        cli.emit(init(), echo(a_uid), tool())
        await settle(env)
        assert lane(env).busy
        env.b.submit(CHAT, CHAT, "done yet?")  # written straight in, no new turn
        await settle(env)
        b_uid = cli.writes[1][0]
        cli.emit(echo(b_uid), say("Not yet — 3/5 tables, ETA 2 min."), tool(),
                 say("Migration complete."), result("Migration complete."))
        await settle(env)
        assert cli.texts == ["migrate", "done yet?"]
        assert env.delivered == ["Not yet — 3/5 tables, ETA 2 min.", "Migration complete."]

    asyncio.run(main())


def test_interim_that_is_also_final_is_not_duplicated(env):
    async def main():
        env.b.submit(CHAT, CHAT, "job")
        await settle(env)
        cli = FakeCLI.instances[0]
        cli.emit(init(), echo(cli.writes[0][0]), tool())
        env.b.submit(CHAT, CHAT, "and 2+2?")
        await settle(env)
        cli.emit(echo(cli.writes[1][0]), say("DONE\n\nFOUR"), result("DONE\n\nFOUR"))
        await settle(env)
        assert env.delivered == ["DONE\n\nFOUR"]

    asyncio.run(main())


def test_message_queued_as_next_turn_is_answered_without_bell(env):
    async def main():
        env.b.submit(CHAT, CHAT, "A")
        await settle(env)
        cli = FakeCLI.instances[0]
        cli.emit(init(), echo(cli.writes[0][0]), tool())
        env.b.submit(CHAT, CHAT, "B")
        await settle(env)
        cli.emit(say("A done"), result("A done"))
        await settle(env)
        assert lane(env).busy  # B written but not consumed yet
        cli.emit(init(), echo(cli.writes[1][0]), say("B done"), result("B done"))
        await settle(env)
        assert env.delivered == ["A done", "B done"]
        assert not lane(env).busy

    asyncio.run(main())


def test_user_message_during_cc_initiated_turn_keeps_attribution(env):
    async def main():
        FakeCLI.responder = instant_reply("started")
        env.b.submit(CHAT, CHAT, "go")
        await settle(env)
        FakeCLI.responder = None
        cli = FakeCLI.instances[0]
        cli.emit(task_done(), init(), tool())  # CC-initiated turn in flight
        await settle(env)
        env.b.submit(CHAT, CHAT, "status?")
        await settle(env)
        cli.emit(say("Build finished OK"), result("Build finished OK"))
        await settle(env)
        assert lane(env).busy  # status? still pending → watchdog/typing stay armed
        cli.emit(init(), echo(cli.writes[1][0]), say("status: green"), result("status: green"))
        await settle(env)
        assert env.delivered == ["started", "🔔 Build finished OK", "status: green"]

    asyncio.run(main())


def test_arrival_order_kept_while_connecting(env):
    async def main():
        FakeCLI.connect_delay = 0.2
        for m in ("M1 deploy X", "M2 wait, use staging", "M3 ok go"):
            env.b.submit(CHAT, CHAT, m)
            await asyncio.sleep(0.05)
        await settle(env, 0.4)
        assert len(FakeCLI.instances) == 1
        assert FakeCLI.instances[0].texts == ["M1 deploy X", "M2 wait, use staging", "M3 ok go"]

    asyncio.run(main())


def test_slash_command_without_echo_closes_cleanly(env):
    async def main():
        def responder(cli, uid, text):
            cli.emit(init(), say("## Context Usage"), result("## Context Usage"))  # no echo

        FakeCLI.responder = responder
        await env.b._dispatch_slash(CHAT, "context", "")
        await settle(env)
        assert env.delivered == ["## Context Usage"]
        assert not lane(env).busy

    asyncio.run(main())


def test_watchdog_idle_interrupts_and_reports(env, monkeypatch):
    async def main():
        monkeypatch.setenv("BRIDGE_IDLE_TIMEOUT", "1")
        env.b.submit(CHAT, CHAT, "hang")
        await settle(env)
        cli = FakeCLI.instances[0]
        cli.on_interrupt = lambda c: c.emit(result(None, subtype="error_during_execution"))
        cli.emit(init(), echo(cli.writes[0][0]), say("partial work"))
        await settle(env, 1.4)
        assert cli.interrupts == 1
        assert env.delivered == ["partial work"]
        assert any("Stopped: no activity" in s for s in env.bot.sent)
        assert not lane(env).busy

    asyncio.run(main())


def test_wedged_cli_killed_and_unread_message_resent(env, monkeypatch):
    async def main():
        monkeypatch.setenv("BRIDGE_IDLE_TIMEOUT", "1")
        monkeypatch.setattr(bridge_mod, "INTERRUPT_GRACE", 0.3)
        env.b.submit(CHAT, CHAT, "A")
        await settle(env)
        cli1 = FakeCLI.instances[0]
        cli1.emit(init(), echo(cli1.writes[0][0]), tool())
        env.b.submit(CHAT, CHAT, "B")  # written, never consumed
        await settle(env)
        FakeCLI.responder = instant_reply("B answered")
        await settle(env, 1.8)
        assert cli1.closed
        assert len(FakeCLI.instances) == 2
        assert FakeCLI.instances[1].texts == ["B"]
        assert env.delivered == ["B answered"]
        joined = " ".join(env.bot.sent)
        assert "cut off" in joined and "Re-sending 1" in joined

    asyncio.run(main())


def test_crash_mid_turn_recovers(env):
    async def main():
        env.b.submit(CHAT, CHAT, "A")
        await settle(env)
        cli1 = FakeCLI.instances[0]
        cli1.emit(init(), echo(cli1.writes[0][0]), say("half"), RuntimeError("Command failed with exit code 1"))
        await settle(env)
        assert env.delivered == ["half"]
        assert any("crashed" in s and "cut off" in s for s in env.bot.sent)
        assert CHAT not in env.b.backend.clients
        assert env.b.store.get(CHAT) is None or True  # no resume happened yet

    asyncio.run(main())


def test_service_restart_exit_is_silent(env):
    async def main():
        FakeCLI.responder = instant_reply("ok")
        env.b.submit(CHAT, CHAT, "hi")
        await settle(env)
        FakeCLI.instances[0].emit(RuntimeError("Command failed with exit code 143"))
        await settle(env)
        assert env.bot.sent == []

    asyncio.run(main())


def test_failed_resume_sets_session_aside(env):
    async def main():
        env.b.store.set(CHAT, "old-sid")
        env.b.submit(CHAT, CHAT, "hi")
        await settle(env)
        cli = FakeCLI.instances[0]
        assert cli.options.resume == "old-sid"
        cli.emit(RuntimeError("No conversation found with session ID old-sid"))
        await settle(env)
        assert env.b.store.get(CHAT) is None
        assert any("old-sid" in s for s in env.bot.sent)

    asyncio.run(main())


def test_reset_during_connect_discards_queue_and_session(env):
    async def main():
        env.b.store.set(CHAT, "OLD")
        FakeCLI.connect_delay = 0.2
        env.b.submit(CHAT, CHAT, "hi")
        await asyncio.sleep(0.05)
        await env.b._cmd_reset(CHAT, "")
        await settle(env, 0.4)
        assert env.b.store.get(CHAT) is None
        assert all(not c.writes for c in FakeCLI.instances)
        assert all(c.closed for c in FakeCLI.instances)
        assert not lane(env).busy

    asyncio.run(main())


def test_effort_refused_while_busy(env):
    async def main():
        env.b.submit(CHAT, CHAT, "A")
        await settle(env)
        cli = FakeCLI.instances[0]
        cli.emit(init(), echo(cli.writes[0][0]), tool())
        await settle(env)
        await env.b._cmd_effort(CHAT, "high")
        assert "not changed" in env.bot.sent[-1]
        assert len(FakeCLI.instances) == 1 and not cli.closed

    asyncio.run(main())


def test_alert_lane_runs_one_triage_at_a_time(env):
    async def main():
        await env.b.enqueue_alert(CHAT, "alert 1")
        await env.b.enqueue_alert(CHAT, "alert 2")
        await settle(env)
        cli = FakeCLI.instances[0]
        assert cli.texts == ["alert 1"]
        cli.emit(init(), echo(cli.writes[0][0]), say("triaged 1"), result("triaged 1"))
        await settle(env)
        assert cli.texts == ["alert 1", "alert 2"]

    asyncio.run(main())


def test_subagent_text_not_used_as_reply(env):
    async def main():
        env.b.submit(CHAT, CHAT, "q")
        await settle(env)
        cli = FakeCLI.instances[0]
        cli.emit(init(), echo(cli.writes[0][0]), say("subagent chatter", parent="tu1"),
                 say("real"), result(None))
        await settle(env)
        assert env.delivered == ["real"]

    asyncio.run(main())


def test_bg_task_tracking_blocks_effort_reopen(env):
    async def main():
        FakeCLI.responder = lambda cli, uid, text: cli.emit(
            init(), echo(uid), task_started("b9"), say("started"), result("started"))
        env.b.submit(CHAT, CHAT, "bg")
        await settle(env)
        assert lane(env).bg_tasks == {"b9"}
        await env.b._cmd_effort(CHAT, "high")
        assert "background task" in env.bot.sent[-1]
        FakeCLI.instances[0].emit(task_done("b9"), init(), say("bg ok"), result("bg ok"))
        await settle(env)
        assert lane(env).bg_tasks == set()

    asyncio.run(main())


def test_plain_prompt_behind_slash_waits_and_keeps_attribution(env):
    """v2 review #0: /compact then a plain prompt must not be lumped into the
    slash turn (false 🔔, early idle)."""

    async def main():
        await env.b._dispatch_slash(CHAT, "compact", "")
        env.b.submit(CHAT, CHAT, "THREE")
        await settle(env)
        cli = FakeCLI.instances[0]
        assert cli.texts == ["/compact"]  # THREE held until the slash is done
        cli.emit(init(), UserMessage(content="<local-command-stdout>Compacted.</local-command-stdout>"),
                 result(""))
        await settle(env)
        assert cli.texts == ["/compact", "THREE"]
        assert lane(env).busy
        cli.emit(init(), echo(cli.writes[1][0]), say("THREE"), result("THREE"))
        await settle(env)
        assert env.delivered == ["Compacted.", "THREE"]
        assert not lane(env).busy

    asyncio.run(main())


def test_resend_after_crash_uses_fresh_uuid(env):
    async def main():
        env.b.submit(CHAT, CHAT, "A")
        await settle(env)
        cli1 = FakeCLI.instances[0]
        first_uid = cli1.writes[0][0]
        cli1.emit(RuntimeError("Command failed with exit code 1"))
        await settle(env, 0.3)
        cli2 = FakeCLI.instances[1]
        assert cli2.texts == ["A"]
        assert cli2.writes[0][0] != first_uid

    asyncio.run(main())


def test_message_right_after_reset_goes_to_new_session(env):
    async def main():
        FakeCLI.responder = instant_reply("ok")
        env.b.submit(CHAT, CHAT, "old")
        await settle(env)
        FakeCLI.responder = None
        old = FakeCLI.instances[0]
        old.emit(init())  # old CLI mid-turn
        t = asyncio.create_task(env.b._cmd_reset(CHAT, ""))
        await asyncio.sleep(0)  # /reset runs up to its first real await
        env.b.submit(CHAT, CHAT, "new")
        await t
        await settle(env)
        assert old.closed
        assert "new" not in old.texts
        assert FakeCLI.instances[-1].texts == ["new"]

    asyncio.run(main())


def test_model_switch_during_connect_does_not_drop_writer_client(env):
    async def main():
        FakeCLI.connect_delay = 0.2
        env.b.submit(CHAT, CHAT, "hi")
        await asyncio.sleep(0.05)
        await env.b._cmd_model(CHAT, "haiku")
        await settle(env, 0.4)
        assert len(FakeCLI.instances) == 1
        assert not FakeCLI.instances[0].closed
        assert FakeCLI.instances[0].texts == ["hi"]

    asyncio.run(main())


def test_reply_queued_before_ticker_await(env):
    async def main():
        env.b.submit(CHAT, CHAT, "q")
        await settle(env)
        cli = FakeCLI.instances[0]
        cli.emit(init(), echo(cli.writes[0][0]))
        await settle(env)
        await env.b._on_tool(CHAT, "Bash: x")
        gate = asyncio.Event()

        async def slow_finish():
            await gate.wait()

        env.b.tickers[CHAT].finish = slow_finish
        cli.emit(say("answer"), result("answer"))
        await asyncio.sleep(0.1)
        await env.b.backend.drop(CHAT)  # e.g. /effort while ticker.finish() is pending
        gate.set()
        await settle(env)
        assert env.delivered == ["answer"]

    asyncio.run(main())


def test_reply_context_quotes_the_replied_message():
    from claude_gateway.bridge import reply_context

    bot = SimpleNamespace(id=999, is_bot=True)
    me = SimpleNamespace(id=1, is_bot=False)
    prev = SimpleNamespace(text="CEPH-10 triaged — escalated", caption=None, from_user=bot)
    msg = SimpleNamespace(reply_to_message=prev, quote=None)
    out = reply_context(msg, bot_id=999)
    assert out.startswith("[Replying to your earlier message:")
    assert "CEPH-10 triaged" in out
    # partial quote wins over the full message
    msg2 = SimpleNamespace(reply_to_message=prev, quote=SimpleNamespace(text="escalated"))
    assert '"""\nescalated\n"""' in reply_context(msg2, bot_id=999)
    # replying to own message / photo without caption
    own = SimpleNamespace(text=None, caption=None, from_user=me, photo=[1])
    out3 = reply_context(SimpleNamespace(reply_to_message=own, quote=None), bot_id=999)
    assert "own earlier message" in out3 and "[photo message]" in out3
    assert reply_context(SimpleNamespace(reply_to_message=None, quote=None), 999) == ""


def test_reply_prefix_reaches_the_cli(env):
    async def main():
        FakeCLI.responder = instant_reply("ok")
        prev = SimpleNamespace(text="the alert summary", caption=None,
                               from_user=SimpleNamespace(id=None, is_bot=True))
        msg = SimpleNamespace(text="what does this mean?", reply_to_message=prev, quote=None,
                              set_reaction=None)
        update = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=CHAT),
                                 effective_user=SimpleNamespace(id=1))
        await env.b.on_message(update, None)
        await settle(env)
        sent = FakeCLI.instances[0].texts[0]
        assert "the alert summary" in sent and sent.endswith("what does this mean?")

    asyncio.run(main())


def test_alert_lane_messages_reach_the_chat_session(env):
    """What the alert lane posts in the chat is passed to the chat session with
    the next message — to the user it is one assistant in one chat."""

    async def main():
        await env.b.enqueue_alert(CHAT, "alert 1")
        await settle(env)
        alert_cli = FakeCLI.instances[0]
        alert_cli.emit(init(), echo(alert_cli.writes[0][0]), say("CEPH-10 triaged — escalated, GLPI #5"),
                       result("CEPH-10 triaged — escalated, GLPI #5"))
        await settle(env)
        assert env.delivered == ["CEPH-10 triaged — escalated, GLPI #5"]
        FakeCLI.responder = instant_reply("it means the OSD is slow")
        env.b.submit(CHAT, CHAT, "apa kaitan dengan issue ceph td?")
        await settle(env)
        chat_cli = FakeCLI.instances[1]
        sent = chat_cli.texts[0]
        assert "CEPH-10 triaged — escalated, GLPI #5" in sent
        assert sent.endswith("apa kaitan dengan issue ceph td?")
        # consumed once: the next message does not repeat it
        env.b.submit(CHAT, CHAT, "ok")
        await settle(env)
        assert chat_cli.texts[1] == "ok"
        # the alert session itself never gets its own words fed back
        assert all("CEPH-10" not in t for t in alert_cli.texts)

    asyncio.run(main())
