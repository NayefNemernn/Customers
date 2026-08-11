"""Shared text formatting for search results — used by both the CLI and the
Telegram bot so results look consistent everywhere.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .core import SearchHit

# Column-name substrings used to guess which field is a person's name/phone
# for the compact one-line summary shown before a result is expanded. Best
# effort only — falls back to a generic field preview when nothing matches.
_NAME_HINTS = ("name", "customer", "client", "contact", "person")
_PHONE_HINTS = ("phone", "mobile", "cell", "tel", "fax")


def _preview_row(row: Dict[str, Any], max_fields: int = 5) -> str:
    parts = []
    for k, v in list(row.items())[:max_fields]:
        parts.append(f"{k}={v}")
    return ", ".join(parts)


def _find_field(row: Dict[str, Any], hints: tuple) -> Optional[str]:
    for k, v in row.items():
        if v is None or str(v).strip() == "":
            continue
        if any(h in k.lower() for h in hints):
            return str(v)
    return None


def summarize_hit(hit: SearchHit) -> str:
    """One short line for a search hit: best-guess name + phone number, so a
    broad search (e.g. a common name) can be scanned quickly before picking
    which match to expand. Falls back to a generic field preview for tables
    that don't look like they have a name/phone column.
    """
    name = _find_field(hit.row, _NAME_HINTS)
    phone = _find_field(hit.row, _PHONE_HINTS)
    if name or phone:
        bits = [b for b in (name, phone) if b]
        return f"[{hit.table}] " + " — ".join(bits)
    return f"[{hit.table}] " + _preview_row(hit.row, max_fields=3)


def format_hit(
    hit: SearchHit,
    related: Dict[str, List[Dict[str, Any]]],
    max_related_rows: int = 5,
    full: bool = False,
) -> str:
    """Render one matched row (plus its linked records).

    `full=False` (default) shows just the matched column(s) — used to keep
    the CLI's normal output compact. `full=True` dumps every field of the
    matched row — used for the Telegram bot's "expand" view, once the user
    has picked a specific match off the compact list.
    """
    lines = [f"[{hit.table}]"]
    if full:
        for k, v in hit.row.items():
            marker = "* " if k in hit.matched_columns else "  "
            lines.append(f"   {marker}{k}: {v}")
    else:
        for col in hit.matched_columns:
            lines.append(f"   - {col}: {hit.row.get(col)}")
        if not hit.matched_columns:
            lines.append(f"   {_preview_row(hit.row)}")

    for rtable, rrows in related.items():
        lines.append(f"   -> {rtable} ({len(rrows)} linked row{'s' if len(rrows) != 1 else ''})")
        for rr in rrows[:max_related_rows]:
            lines.append(f"        {_preview_row(rr, max_fields=8 if full else 5)}")
        if len(rrows) > max_related_rows:
            lines.append(f"        ... and {len(rrows) - max_related_rows} more")

    return "\n".join(lines)


def chunk_text(text: str, max_len: int = 3800) -> List[str]:
    """Split long text on line boundaries so no chunk exceeds max_len
    (Telegram messages cap at 4096 chars; the CLI has no limit but this is
    harmless there too)."""
    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1
        if current_len + line_len > max_len and current:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks
