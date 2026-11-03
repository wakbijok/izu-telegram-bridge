"""Application entry — wire python-telegram-bot to the bridge."""
from __future__ import annotations

import asyncio
import logging

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from claude_gateway.bridge import Bridge
from claude_gateway.config import Settings

log = logging.getLogger("claude-gateway")


async def _post_init(app: Application):
    bridge: Bridge = app.bot_data["bridge"]
    # Discover live Claude Code slash set, then register Telegram menu.
    menu = await bridge.refresh_commands_from_claude()
    await app.bot.set_my_commands([BotCommand(c, d[:256]) for c, d in menu])
    log.info("registered %d Telegram menu commands (CC-discovered)", len(menu))
    for c, d in menu[:12]:
        log.info("  /%s — %s", c, d[:60])

    import alert_webhook

    alert_webhook.start(
        bridge.enqueue_alert,
        asyncio.get_running_loop(),
        sorted(bridge.settings.allowed_user_ids)[0]
        if bridge.settings.allowed_user_ids
        else 0,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = Settings.from_env()
    app = (
        Application.builder()
        .token(settings.bot_token)
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )
    bridge = Bridge(app, settings)
    app.bot_data["bridge"] = bridge
    app.add_handler(CallbackQueryHandler(bridge.on_button))
    app.add_handler(MessageHandler(filters.COMMAND, bridge.on_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, bridge.on_message)
    )
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.ALL, bridge.on_media)
    )
    log.info(
        "claude-gateway v2 starting; allowlist=%s model=%s rich=%s cwd=%s",
        sorted(settings.allowed_user_ids),
        settings.model,
        settings.rich_mode,
        settings.workdir,
    )
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
