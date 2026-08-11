"""Telegram bot front-end: send it any text and it searches your currently
loaded Access database, following relationships to show linked records too.

Access model (deliberately least-privilege — this handles sensitive files):
  - OWNER (config.OWNER_ID): full control. Can /load a different database,
    view /schema, and invite/remove other searchers.
  - MEMBER (anyone the owner adds via /adduser, stored in members.json):
    can only send search terms and read results. Cannot switch files, see
    the schema, or manage the member list.
  - Everyone else: rejected outright, no information disclosed.

Setup is documented in README.md. Run with:
    python -m access_search.telegram_bot
"""
from __future__ import annotations

import itertools
import logging
from typing import Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config, members
from .core import AccessSearchError, connect, fetch_related, get_columns, get_relationships, list_tables, search
from .formatting import chunk_text, format_hit, summarize_hit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("access_search.telegram_bot")

# chat_id -> currently active database file path (owner-controlled, shared
# by everyone who searches through this bot instance)
_active_db: Dict[int, str] = {}

# chat_id -> {"id": int, "db_path": str, "hits": [SearchHit, ...], "edges": [...]}
# Backs the "tap a result to expand" buttons: a search shows a compact list
# first, and expanding a specific match re-queries only that row's related
# records on demand instead of fetching everything up front. The "id" is a
# per-chat monotonic counter stamped into each button's callback_data so a
# stale button from a previous search can't be used to index into (and thus
# return data from) whatever search happens to be cached now.
_last_search: Dict[int, dict] = {}
_search_counter = itertools.count(1)


def _is_owner(user_id: int) -> bool:
    return config.OWNER_ID is not None and user_id == config.OWNER_ID


def _can_search(user_id: int) -> bool:
    return _is_owner(user_id) or members.is_member(user_id)


def _db_for_chat(chat_id: int) -> str | None:
    return _active_db.get(chat_id) or config.DEFAULT_DB_PATH


async def _reject(update: Update, reason: str = "not_allowed") -> None:
    uid = update.effective_user.id
    logger.warning("Rejected user_id=%s username=%s reason=%s", uid, update.effective_user.username, reason)
    if reason == "owner_only":
        await update.message.reply_text("⛔ Only the bot owner can do that.")
    else:
        await update.message.reply_text("⛔ You're not authorized to use this bot.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not _can_search(uid):
        await _reject(update)
        return
    db_path = _db_for_chat(update.effective_chat.id)
    lines = [
        "Send me any text and I'll search the loaded Access database, "
        "including records linked to it through the database's relationships.",
        "",
        f"Current database: {db_path or 'none loaded yet'}",
    ]
    if _is_owner(uid):
        lines += [
            "",
            "Owner commands:",
            "/load <path> — switch to a different .accdb/.mdb file",
            "/schema — list tables, columns, and detected relationships",
            "/adduser <telegram_id> [name] — let someone else search (read-only)",
            "/removeuser <telegram_id> — revoke their access",
            "/listusers — show everyone who currently has access",
        ]
    await update.message.reply_text("\n".join(lines))


async def load_db(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update.effective_user.id):
        await _reject(update, "owner_only")
        return
    if not context.args:
        await update.message.reply_text("Usage: /load C:\\path\\to\\file.accdb")
        return
    path = " ".join(context.args)
    try:
        conn = connect(path)
        conn.close()
    except AccessSearchError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return
    _active_db[update.effective_chat.id] = path
    await update.message.reply_text(f"✅ Now searching: {path}")


async def schema(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update.effective_user.id):
        await _reject(update, "owner_only")
        return
    db_path = _db_for_chat(update.effective_chat.id)
    if not db_path:
        await update.message.reply_text("⚠️ No database loaded. Use /load <path> first.")
        return
    try:
        conn = connect(db_path)
        tables = list_tables(conn)
        lines = [f"{len(tables)} table(s):"]
        for t in tables:
            cols = get_columns(conn, t)
            lines.append(f"\n[{t}]")
            lines.extend(f"   {name} ({type_name})" for name, type_name in cols)
        edges = get_relationships(db_path, conn=conn)
        lines.append(f"\n{len(edges)} relationship(s):")
        lines.extend(f"   {e.table_a}.{e.col_a} <-> {e.table_b}.{e.col_b}" for e in edges)
        text = "\n".join(lines)
    except AccessSearchError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return
    for chunk in chunk_text(text):
        await update.message.reply_text(chunk)


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update.effective_user.id):
        await _reject(update, "owner_only")
        return
    if not context.args:
        await update.message.reply_text("Usage: /adduser <telegram_id> [name]")
        return
    try:
        new_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "That doesn't look like a numeric Telegram ID. They can get theirs from @userinfobot."
        )
        return
    if new_id == config.OWNER_ID:
        await update.message.reply_text("That's already the owner ID.")
        return
    label = " ".join(context.args[1:])
    members.add_member(new_id, label)
    who = f"{new_id} ({label})" if label else str(new_id)
    await update.message.reply_text(f"✅ {who} can now search through this bot (read-only).")


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update.effective_user.id):
        await _reject(update, "owner_only")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeuser <telegram_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a numeric Telegram ID.")
        return
    if members.remove_member(target_id):
        await update.message.reply_text(f"✅ Removed {target_id}.")
    else:
        await update.message.reply_text(f"{target_id} wasn't on the list.")


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update.effective_user.id):
        await _reject(update, "owner_only")
        return
    lines = [f"Owner: {config.OWNER_ID}"]
    current = members.list_members()
    if current:
        lines.append("Invited (search-only):")
        lines.extend(f"   {uid}" + (f" — {label}" if label else "") for uid, label in current.items())
    else:
        lines.append("No one else has been invited.")
    await update.message.reply_text("\n".join(lines))


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not _can_search(uid):
        await _reject(update)
        return
    term = (update.message.text or "").strip()
    if not term:
        return

    db_path = _db_for_chat(update.effective_chat.id)
    if not db_path:
        await update.message.reply_text("⚠️ No database loaded yet. Ask the owner to /load one.")
        return

    await update.message.chat.send_action("typing")
    errors: list = []
    try:
        conn = connect(db_path)
        edges = get_relationships(db_path, conn=conn)
        hits = search(conn, term, limit_per_table=15, errors=errors)
    except AccessSearchError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if errors:
        logger.warning("Search for %r had table failures: %s", term, errors)
        await update.message.reply_text(
            f"⚠️ {len(errors)} table(s) failed to search — results below may be incomplete."
        )

    if not hits:
        await update.message.reply_text(f"No matches for '{term}'.")
        return

    shown = hits[:20]  # keep the list + buttons readable even for very broad terms
    search_id = next(_search_counter)
    _last_search[update.effective_chat.id] = {
        "id": search_id,
        "db_path": db_path,
        "hits": shown,
        "edges": edges,
    }

    header = f"🔎 {len(hits)} match(es) for '{term}'"
    if len(hits) > 20:
        header += " (showing first 20)"
    lines = [header + ":"]
    lines += [f"{i + 1}. {summarize_hit(hit)}" for i, hit in enumerate(shown)]
    for chunk in chunk_text("\n".join(lines)):
        await update.message.reply_text(chunk)

    keyboard = [
        [InlineKeyboardButton(f"View #{i + 1}", callback_data=f"exp:{search_id}:{i}")]
        for i in range(len(shown))
    ]
    await update.message.reply_text(
        "Tap a result to see its full details:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def expand_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = query.from_user.id
    if not _can_search(uid):
        await query.answer("⛔ You're not authorized to use this bot.", show_alert=True)
        return

    try:
        _, sid_str, idx_str = query.data.split(":", 2)
        search_id, idx = int(sid_str), int(idx_str)
    except (ValueError, AttributeError):
        await query.answer()
        return

    state = _last_search.get(query.message.chat.id)
    if not state or state["id"] != search_id:
        await query.answer("This result list is stale — search again.", show_alert=True)
        return
    if idx < 0 or idx >= len(state["hits"]):
        await query.answer("That result is no longer available.", show_alert=True)
        return

    await query.answer()
    hit = state["hits"][idx]
    try:
        conn = connect(state["db_path"])
        related = fetch_related(conn, state["edges"], hit.table, hit.row, depth=1, max_rows=10)
        conn.close()
    except AccessSearchError as exc:
        await query.message.reply_text(f"❌ {exc}")
        return

    text = format_hit(hit, related, full=True)
    for chunk in chunk_text(text):
        await query.message.reply_text(chunk)


def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN (env var or .env file) before starting the bot.")
    if not config.OWNER_ID:
        raise SystemExit(
            "Set TELEGRAM_OWNER_ID (your numeric Telegram user ID, from @userinfobot) "
            "before starting the bot — without it, no one (including you) can use it."
        )

    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("load", load_db))
    app.add_handler(CommandHandler("schema", schema))
    app.add_handler(CommandHandler("adduser", add_user))
    app.add_handler(CommandHandler("removeuser", remove_user))
    app.add_handler(CommandHandler("listusers", list_users))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    app.add_handler(CallbackQueryHandler(expand_result, pattern=r"^exp:\d+:\d+$"))

    logger.info("Bot starting (polling) — owner_id=%s", config.OWNER_ID)
    app.run_polling()


if __name__ == "__main__":
    main()
