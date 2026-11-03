from claude_gateway.commands import (
    build_alias_map,
    build_menu,
    hermes_only_hint,
    is_known_cc_slash,
    resolve_cc_slash,
    to_tg_name,
)


def test_to_tg_name():
    assert to_tg_name("code-review") == "code_review"
    assert to_tg_name("memory:recall") == "memory_recall"


def test_resolve_and_known():
    aliases = build_alias_map()
    assert resolve_cc_slash("code_review", aliases) == "code-review"
    assert is_known_cc_slash("code-review", aliases)
    assert not is_known_cc_slash("reasoning", aliases)
    assert not is_known_cc_slash("memory:recall", aliases)


def test_menu_has_effort_and_no_reasoning():
    menu = build_menu()
    names = [m[0] for m in menu]
    assert names[:5] == ["help", "stop", "reset", "model", "effort"]
    assert "reasoning" not in names
    assert "clear" in names
    assert "code_review" in names


def test_hermes_hint():
    assert hermes_only_hint("reasoning")
    assert hermes_only_hint("/reasoning".lstrip("/"))
    assert hermes_only_hint("fast")
