"""Loads bot configuration from a .env file (if present) and the environment.

Keeping this separate means telegram_bot.py never has to know or care
whether settings came from a real environment variable or a local .env file.
"""
from __future__ import annotations

import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # no-op if there's no .env file
except ImportError:
    pass  # python-dotenv is optional; plain env vars still work without it


BOT_TOKEN: str | None = os.environ.get("TELEGRAM_BOT_TOKEN")
DEFAULT_DB_PATH: str | None = os.environ.get("ACCESS_DB_PATH")

# The owner is the one person with full control (switch database files, view
# schema, invite/remove other searchers). Everyone else the owner invites via
# /adduser can only run searches — see access_search/members.py.
_owner_raw = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
OWNER_ID: int | None = int(_owner_raw) if _owner_raw else None
