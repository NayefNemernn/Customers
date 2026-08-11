"""Command-line interface.

Usage:
    python -m access_search.cli <file.accdb> "search term" [options]
    python -m access_search.cli <file.accdb> --schema
"""
from __future__ import annotations

import argparse
import json
import sys

from .core import AccessSearchError, connect, fetch_related, get_columns, get_relationships, list_tables, search
from .formatting import format_hit

# Windows' default console codepage (cp1252/cp850, whichever the locale
# picks) can't encode Arabic, Cyrillic, CJK, etc. Since this file's whole
# purpose is searching non-Latin PII, printing a search term or result in
# the "wrong" script must never crash the process — reconfigure stdout/
# stderr to UTF-8 up front. (Modern Windows Terminal renders it correctly;
# legacy conhost may show mojibake instead of the real glyphs, but either
# way the tool keeps working rather than dying with UnicodeEncodeError.)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def print_schema(db_path: str) -> None:
    conn = connect(db_path)
    tables = list_tables(conn)
    print(f"{len(tables)} table(s) in {db_path}\n")
    for t in tables:
        cols = get_columns(conn, t)
        print(f"[{t}]")
        for name, type_name in cols:
            print(f"   {name} ({type_name})")
        print()

    edges = get_relationships(db_path, conn=conn)
    print(f"{len(edges)} relationship(s) detected:")
    for e in edges:
        print(f"   {e.table_a}.{e.col_a}  <->  {e.table_b}.{e.col_b}")
    if not edges:
        print("   (none found — table relationships may not be defined in this file)")


def run_search(
    db_path: str,
    term: str,
    tables=None,
    depth: int = 1,
    limit: int = 25,
    max_related: int = 5,
    as_json: bool = False,
) -> None:
    conn = connect(db_path)
    edges = get_relationships(db_path, conn=conn)
    errors: list = []
    hits = search(conn, term, tables=tables, limit_per_table=limit, errors=errors)

    for err in errors:
        print(f"WARNING: search failed on table {err} — results may be incomplete.", file=sys.stderr)

    if not hits:
        print(f"No matches for '{term}'." + (" (some tables failed — see warnings above)" if errors else ""))
        return

    if as_json:
        out = []
        for hit in hits:
            related = fetch_related(conn, edges, hit.table, hit.row, depth=depth, max_rows=max_related)
            out.append({
                "table": hit.table,
                "matched_columns": hit.matched_columns,
                "row": hit.row,
                "related": related,
            })
        print(json.dumps(out, default=str, indent=2))
        return

    print(f"{len(hits)} match(es) for '{term}':\n")
    for hit in hits:
        related = fetch_related(conn, edges, hit.table, hit.row, depth=depth, max_rows=max_related)
        print(format_hit(hit, related, max_related_rows=max_related))
        print()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="access-search",
        description="Search every cell of an Access database and follow its relationships to linked records.",
    )
    parser.add_argument("db_path", help="Path to the .accdb/.mdb file")
    parser.add_argument("term", nargs="?", help="Text to search for (omit with --schema)")
    parser.add_argument("--table", action="append", dest="tables", help="Limit search to this table (repeatable)")
    parser.add_argument("--depth", type=int, default=1, help="How many relationship hops to follow outward (default: 1)")
    parser.add_argument("--limit", type=int, default=25, help="Max matching rows per table (default: 25)")
    parser.add_argument("--max-related", type=int, default=5, help="Max related rows shown per linked table (default: 5)")
    parser.add_argument("--schema", action="store_true", help="Print tables, columns, and detected relationships, then exit")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of formatted text")
    args = parser.parse_args(argv)

    try:
        if args.schema:
            print_schema(args.db_path)
            return 0
        if not args.term:
            parser.error("a search term is required unless --schema is given")
        run_search(
            args.db_path,
            args.term,
            tables=args.tables,
            depth=args.depth,
            limit=args.limit,
            max_related=args.max_related,
            as_json=args.json,
        )
        return 0
    except AccessSearchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
