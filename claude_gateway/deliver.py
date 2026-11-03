"""Outbound delivery cascade — Hermes-grade, never drop a reply."""
from __future__ import annotations

import logging

from claude_gateway.config import Settings
from claude_gateway.format import md_chunks, md_to_tg_html, needs_rich_rendering, prepare_legacy_markdown

log = logging.getLogger("claude-gateway.deliver")

try:
    import telegramify_markdown as _tgmd
    from telegramify_markdown import config as _tgmd_config

    _sym = _tgmd_config.get_runtime_config().markdown_symbol
    for _lvl in (
        "heading_level_1",
        "heading_level_2",
        "heading_level_3",
        "heading_level_4",
        "heading_level_5",
        "heading_level_6",
    ):
        setattr(_sym, _lvl, "")
except Exception:  # noqa: BLE001
    _tgmd = None


class Deliverer:
    def __init__(self, bot, settings: Settings):
        self.bot = bot
        self.settings = settings
        self._rich_disabled = False

    def _should_attempt_rich(self, text: str) -> bool:
        mode = self.settings.rich_mode
        if mode in ("0", "false", "off", "no", "never"):
            return False
        if mode in ("1", "true", "on", "always", "yes"):
            return True
        return needs_rich_rendering(text)

    async def _try_send_rich(self, chat_id: int, text: str) -> bool:
        if self._rich_disabled or not text or len(text) > self.settings.rich_max_chars:
            return False
        try:
            await self.bot.do_api_request(
                "sendRichMessage",
                api_kwargs={
                    "chat_id": chat_id,
                    "rich_message": {"markdown": text},
                },
            )
            return True
        except Exception as exc:  # noqa: BLE001
            err = str(exc).lower()
            name = type(exc).__name__.lower()
            capability = (
                name in {"endpointnotfound", "invalidtoken", "attributeerror", "typeerror"}
                or getattr(exc, "error_code", None) == 404
                or (
                    ("method" in err or "endpoint" in err)
                    and ("not found" in err or "does not exist" in err)
                )
                or "no such method" in err
            )
            if capability:
                self._rich_disabled = True
                log.warning("sendRichMessage unavailable — latched off: %s", exc)
            else:
                log.warning("sendRichMessage rejected; legacy fallback: %s", exc)
            return False

    async def send(self, chat_id: int, text: str) -> None:
        text = text or "(done, no text)"
        if self._should_attempt_rich(text):
            if await self._try_send_rich(chat_id, text):
                return

        legacy = prepare_legacy_markdown(text)
        if _tgmd is not None:
            try:
                boxes = await _tgmd.telegramify(
                    legacy,
                    max_message_length=self.settings.tg_limit,
                    min_file_lines=self.settings.file_lines,
                )
                for box in boxes:
                    kind = type(box).__name__
                    if kind == "Text":
                        try:
                            from telegram import MessageEntity as _TGME

                            ents = [
                                _TGME(
                                    type=e.type,
                                    offset=e.offset,
                                    length=e.length,
                                    url=getattr(e, "url", None) or None,
                                    language=getattr(e, "language", None) or None,
                                )
                                for e in (box.entities or [])
                            ]
                            await self.bot.send_message(
                                chat_id, box.text, entities=ents or None
                            )
                        except Exception:  # noqa: BLE001
                            mdv2 = _tgmd.entities_to_markdownv2(box.text, box.entities)
                            await self.bot.send_message(
                                chat_id, mdv2, parse_mode="MarkdownV2"
                            )
                    elif kind in ("File", "Photo"):
                        await self.bot.send_document(
                            chat_id,
                            document=bytes(box.file_data),
                            filename=box.file_name,
                            caption=(getattr(box, "caption_text", None) or "").strip()[:1000]
                            or None,
                        )
                return
            except Exception:  # noqa: BLE001
                log.warning("telegramify delivery failed; degrading", exc_info=True)

        for chunk in md_chunks(legacy, self.settings.tg_limit):
            if _tgmd is not None:
                try:
                    mdv2 = _tgmd.markdownify(chunk)
                    if len(mdv2) <= self.settings.tg_limit:
                        await self.bot.send_message(
                            chat_id, mdv2, parse_mode="MarkdownV2"
                        )
                        continue
                except Exception:  # noqa: BLE001
                    pass
            try:
                await self.bot.send_message(
                    chat_id, md_to_tg_html(chunk), parse_mode="HTML"
                )
            except Exception:  # noqa: BLE001
                await self.bot.send_message(chat_id, chunk)
