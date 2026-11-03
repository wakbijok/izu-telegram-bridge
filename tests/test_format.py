"""Lightweight unit tests for format helpers (no Telegram network)."""
from claude_gateway.format import (
    convert_table_to_bullets,
    md_chunks,
    needs_rich_rendering,
    prepare_legacy_markdown,
)


def test_pipe_table_triggers_rich():
    text = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    assert needs_rich_rendering(text)


def test_table_to_bullets():
    text = "| Name | Val |\n| --- | --- |\n| foo | 1 |\n"
    out = convert_table_to_bullets(text)
    assert "foo" in out
    assert "Val" in out


def test_headers_normalized():
    out = prepare_legacy_markdown("## Title\n\nbody")
    assert "**Title**" in out
    assert "##" not in out


def test_md_chunks_respects_limit():
    text = "para1\n\n" + ("x" * 50)
    parts = list(md_chunks(text, 40))
    assert all(len(p) <= 40 for p in parts)
    assert "".join(parts).replace("\n", "")  # non-empty
