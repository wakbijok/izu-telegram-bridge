"""Tool-approval buttons (who may press them, where) and the CLI's env scrub."""
import asyncio
from types import SimpleNamespace

import pytest

import claude_gateway.backend as backend_mod
from claude_gateway.bridge import Bridge
from claude_gateway.config import Settings

OWNER, STRANGER = 1, 666
CHAT, OTHER_CHAT = 42, 99


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw.get("reply_markup")))
        return SimpleNamespace(message_id=len(self.sent))


@pytest.fixture
def bridge(tmp_path):
    settings = Settings(bot_token="x", allowed_user_ids=frozenset({OWNER}),
                        sessions_file=tmp_path / "sessions.json")
    bot = FakeBot()
    return Bridge(SimpleNamespace(bot=bot), settings), bot


def _press(data: str, user: int, chat: int):
    q = SimpleNamespace(data=data, answers=[], edits=[])

    async def answer(text=None, **_):
        q.answers.append(text)

    async def edit_message_text(text, **_):
        q.edits.append(text)

    q.answer, q.edit_message_text = answer, edit_message_text
    upd = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=user),
                          effective_chat=SimpleNamespace(id=chat))
    return upd, q


def _run_approval(bridge, key, presses):
    """Start an approval on session key, apply presses [(action, user, chat)],
    return (decision or 'pending', pending entry, the button queries)."""
    b, bot = bridge

    async def main():
        task = asyncio.create_task(b._approver(key)("Bash", {"command": "ls"}, None))
        await asyncio.sleep(0.01)
        (aid,) = b.pending
        entry = b.pending[aid]
        qs = []
        for action, user, chat in presses:
            upd, q = _press(f"{action}:{aid}", user, chat)
            await b.on_button(upd, None)
            qs.append(q)
        await asyncio.sleep(0.01)
        if task.done():
            res = task.result()
            return type(res).__name__, entry, aid, qs
        task.cancel()
        return "pending", entry, aid, qs

    return asyncio.run(main())


def test_approval_id_is_random_and_pending_stores_chat(bridge):
    _, entry, aid, _ = _run_approval(bridge, CHAT, [])
    assert len(aid) >= 16 and not aid.startswith("a")
    assert entry[0] == CHAT and isinstance(entry[1], asyncio.Future)
    kb = bridge[1].sent[0][2]
    assert kb.inline_keyboard[0][0].callback_data == f"ok:{aid}"


def test_owner_in_right_chat_allows(bridge):
    res, _, _, qs = _run_approval(bridge, CHAT, [("ok", OWNER, CHAT)])
    assert res == "PermissionResultAllow"
    assert qs[0].edits == ["✅ Allowed"]


def test_alert_lane_approval_targets_positive_chat(bridge):
    res, entry, _, _ = _run_approval(bridge, -CHAT, [("no", OWNER, CHAT)])
    assert entry[0] == CHAT and res == "PermissionResultDeny"


def test_stranger_cannot_decide(bridge):
    res, _, _, qs = _run_approval(bridge, CHAT, [("ok", STRANGER, CHAT)])
    assert res == "pending"
    assert qs[0].edits == [] and "Not authorized" in qs[0].answers[0]


def test_wrong_chat_cannot_decide_then_right_chat_can(bridge):
    res, _, _, qs = _run_approval(bridge, CHAT, [("ok", OWNER, OTHER_CHAT), ("no", OWNER, CHAT)])
    assert res == "PermissionResultDeny"
    assert qs[0].edits == [] and "Wrong chat" in qs[0].answers[0]
    assert qs[1].edits == ["⛔ Denied"]


def test_unknown_or_malformed_callback_is_ignored(bridge):
    b, _ = bridge

    async def main():
        for data in ("ok:nope", "garbage", "", "zz:x"):
            upd, q = _press(data, OWNER, CHAT)
            await b.on_button(upd, None)
            assert q.edits == []

    asyncio.run(main())


def test_preview_shows_head_tail_and_truncation_marker():
    text = "H" * 1000 + "MIDDLE" + "T" * 1000
    out = Bridge.approval_preview(text)
    assert out.startswith("H" * 400)
    assert "T" * 200 in out and out.rstrip().endswith(f"(total {len(text)} chars)")
    assert f"[truncated {len(text) - 600} chars]" in out
    assert Bridge.approval_preview("short") == "short"
    exact = "x" * Bridge.APPROVAL_PREVIEW
    assert Bridge.approval_preview(exact) == exact


# ---------- env scrub ----------
def test_env_overrides_blank_bridge_secrets():
    src = {"TELEGRAM_BOT_TOKEN": "123:abc", "ALERT_TOKEN": "s3cret",
           "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "PATH": "/bin", "HOME": "/h"}
    over = backend_mod.env_overrides(src)
    assert over == {"TELEGRAM_BOT_TOKEN": "", "ALERT_TOKEN": ""}
    # The SDK builds the child env as {**os.environ, **options.env}.
    child = {**src, **over}
    assert "123:abc" not in child.values() and "s3cret" not in child.values()
    assert child["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth" and child["PATH"] == "/bin"
    assert backend_mod.env_overrides({"PATH": "/bin"}) == {}


def test_denylist_covers_every_bridge_secret():
    assert set(backend_mod.BRIDGE_SECRET_ENV) >= {"TELEGRAM_BOT_TOKEN", "ALERT_TOKEN"}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in backend_mod.BRIDGE_SECRET_ENV


def test_opened_client_gets_scrubbed_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALERT_TOKEN", "s3cret")
    monkeypatch.setenv("BRIDGE_MCP_SERVERS_FILE", "")
    seen = {}

    class FakeClient:
        def __init__(self, options):
            seen["env"] = options.env

        async def connect(self):
            pass

        async def receive_messages(self):
            await asyncio.sleep(3600)
            yield None

        async def disconnect(self):
            pass

    monkeypatch.setattr(backend_mod, "ClaudeSDKClient", FakeClient)
    from claude_gateway.sessions import SessionStore
    settings = Settings(bot_token="x", allowed_user_ids=frozenset({1}),
                        sessions_file=tmp_path / "s.json")
    be = backend_mod.ClaudeBackend(settings, SessionStore(settings.sessions_file))

    async def main():
        await be.client_for(CHAT)
        await be.drop(CHAT)

    asyncio.run(main())
    assert seen["env"] == {"TELEGRAM_BOT_TOKEN": "", "ALERT_TOKEN": ""}
