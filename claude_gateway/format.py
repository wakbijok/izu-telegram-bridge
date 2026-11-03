"""Markdown prep for Telegram delivery (Hermes table/header rules)."""
from __future__ import annotations

import html
import re

TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$")


def has_pipe_table(text: str) -> bool:
    return any(TABLE_SEP_RE.match(line) for line in (text or "").splitlines())


def needs_rich_rendering(text: str) -> bool:
    if not text:
        return False
    if has_pipe_table(text):
        return True
    if re.search(r"(?m)^\s*[-*]\s+\[[ xX]\]\s+", text):
        return True
    if re.search(r"(?m)^<details\b|^</details>|^<summary\b|^</summary>", text):
        return True
    if "$$" in text:
        return True
    return False


def _split_table_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _render_table_as_bullets(block: list[str]) -> str:
    if len(block) < 3:
        return "\n".join(block)
    headers = _split_table_row(block[0])
    if len(headers) < 2:
        return "\n".join(block)
    first = _split_table_row(block[2]) if len(block) > 2 else []
    has_row_label = len(first) == len(headers) + 1
    groups: list[str] = []
    for index, row in enumerate(block[2:], start=1):
        cells = _split_table_row(row)
        if has_row_label:
            heading = cells[0] if cells and cells[0] else f"Row {index}"
            data = cells[1:]
        else:
            heading = next((c for c in cells if c), f"Row {index}")
            data = cells
        if len(data) < len(headers):
            data = data + [""] * (len(headers) - len(data))
        elif len(data) > len(headers):
            data = data[: len(headers)]
        bullets = []
        for header, value in zip(headers, data):
            if not has_row_label and value == heading:
                continue
            bullets.append(f"• {header}: {value}")
        groups.append("\n".join([f"**{heading}**", *bullets]))
    return "\n\n".join(groups)


def convert_table_to_bullets(text: str) -> str:
    if not text or "|" not in text or "-" not in text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and TABLE_SEP_RE.match(lines[i + 1]):
            block = [line, lines[i + 1]]
            j = i + 2
            while j < len(lines) and "|" in lines[j].strip() and lines[j].strip():
                block.append(lines[j])
                j += 1
            out.append(_render_table_as_bullets(block))
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def normalize_headers_for_mdv2(text: str) -> str:
    return re.sub(r"^(#{1,6})\s+(.+)$", r"**\2**", text, flags=re.M)


def prepare_legacy_markdown(text: str) -> str:
    return normalize_headers_for_mdv2(convert_table_to_bullets(text or ""))


def md_to_tg_html(text: str) -> str:
    fences: list[str] = []

    def _stash_fence(m):
        fences.append(m.group(2))
        return f"\x00F{len(fences) - 1}\x00"

    text = prepare_legacy_markdown(text)
    text = re.sub(r"```([a-zA-Z0-9_+-]*)\n?(.*?)```", _stash_fence, text, flags=re.S)
    text = html.escape(text, quote=False)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"^\s*([-*_]){3,}\s*$", "", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"^(\s*)[*•]\s+", r"\1- ", text, flags=re.M)

    def _restore(m):
        return "<pre>" + html.escape(fences[int(m.group(1))].rstrip(), quote=False) + "</pre>"

    return re.sub(r"\x00F(\d+)\x00", _restore, text).strip()


def md_chunks(text: str, limit: int):
    if len(text) <= limit:
        yield text
        return
    parts, cur = [], ""
    for para in text.split("\n\n"):
        candidate = (cur + "\n\n" + para) if cur else para
        if len(candidate) > limit and cur and cur.count("```") % 2 == 0:
            parts.append(cur)
            cur = para
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    for p in parts:
        while len(p) > limit:
            yield p[:limit]
            p = p[limit:]
        yield p
