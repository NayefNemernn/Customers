"""Persisted list of people the owner has invited to search (read-only)
through the bot. Stored as JSON next to this file so it survives restarts
without needing a real database.

Only the owner (config.OWNER_ID) can ever modify this list — enforced in
telegram_bot.py, not here — this module just handles storage.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Dict

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "members.json")
_lock = threading.Lock()


def _load() -> Dict[str, str]:
    if not os.path.exists(_PATH):
        return {}
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(members: Dict[str, str]) -> None:
    tmp_path = _PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(members, f, indent=2, sort_keys=True)
    os.replace(tmp_path, _PATH)  # atomic on Windows too, avoids a half-written file


def list_members() -> Dict[int, str]:
    """{telegram_user_id: label}"""
    with _lock:
        return {int(uid): label for uid, label in _load().items()}


def add_member(user_id: int, label: str = "") -> None:
    with _lock:
        members = _load()
        members[str(user_id)] = label
        _save(members)


def remove_member(user_id: int) -> bool:
    with _lock:
        members = _load()
        if str(user_id) not in members:
            return False
        del members[str(user_id)]
        _save(members)
        return True


def is_member(user_id: int) -> bool:
    with _lock:
        return str(user_id) in _load()
