"""Telegram gateway for Claude Code (Claude Agent SDK) — Claude Code is the only backend.

Architecture:
  config    — env/settings (no secrets or deployment identity in code)
  sessions  — chat_id -> Claude session_id persistence
  progress  — live tool ticker
  format    — markdown prep for Telegram
  deliver   — rich → telegramify MDV2 → HTML → plain (never drop)
  backend   — ClaudeSDKClient adapter + always-on stream pump
  bridge    — per-chat lanes: ordered writer, stream routing, watchdog, handlers
  app       — python-telegram-bot Application wiring
"""

__version__ = "2.1.0"
