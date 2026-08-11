"""Core engine: open an Access file, discover its tables/columns/relationships,
search every cell, and follow relationships to pull back linked records.

Nothing here is Telegram- or CLI-specific — both interfaces import this module.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import pyodbc

# Column types the Access ODBC driver reports that we cannot meaningfully
# text-search (OLE objects, attachments, raw binary blobs).
_BINARY_TYPES = {"LONGBINARY", "VARBINARY", "BINARY", "IMAGE", "OLEOBJECT"}

# Types the driver already reports as text — these can go straight into a
# LIKE clause. Everything else (numbers, dates, booleans, GUIDs) needs
# converting to text first — see _column_condition() for why that conversion
# has to be written carefully.
_TEXT_TYPES = {"VARCHAR", "CHAR", "LONGCHAR", "WCHAR", "VARWCHAR", "LONGVARCHAR", "LONGVARWCHAR", "MEMO"}

# System tables Access maintains internally — never shown to the user.
_SYSTEM_PREFIXES = ("MSys", "~", "USys")

_ACCESS_DRIVER_NAMES = [
    "Microsoft Access Driver (*.mdb, *.accdb)",
]


class AccessSearchError(RuntimeError):
    """Raised for connection/driver problems the caller should show to the user."""


@dataclass
class SearchHit:
    table: str
    row: Dict[str, Any]
    matched_columns: List[str]


@dataclass
class Relationship:
    table_a: str
    col_a: str
    table_b: str
    col_b: str


def find_driver() -> str:
    """Return the installed Access ODBC driver name, or raise a clear error."""
    available = pyodbc.drivers()
    for name in _ACCESS_DRIVER_NAMES:
        if name in available:
            return name
    raise AccessSearchError(
        "No Microsoft Access ODBC driver found. Install the free "
        "'Microsoft Access Database Engine' redistributable (bitness must "
        "match your Python install), then try again. Drivers currently "
        f"visible to pyodbc: {available}"
    )


def connect(db_path: str) -> pyodbc.Connection:
    """Open an .accdb/.mdb file. Raises AccessSearchError with a plain-English
    message on anything a non-technical caller needs to react to."""
    db_path = os.path.abspath(db_path)
    if not os.path.exists(db_path):
        raise AccessSearchError(f"File not found: {db_path}")
    if os.path.splitext(db_path)[1].lower() not in (".accdb", ".mdb"):
        raise AccessSearchError(f"Not an Access file (.accdb/.mdb): {db_path}")

    driver = find_driver()
    conn_str = f"DRIVER={{{driver}}};DBQ={db_path};"
    try:
        return pyodbc.connect(conn_str, autocommit=True)
    except pyodbc.Error as exc:
        raise AccessSearchError(f"Could not open '{db_path}': {exc}") from exc


def list_tables(conn: pyodbc.Connection) -> List[str]:
    """All user tables, excluding Access' internal MSys/USys/~ tables."""
    cur = conn.cursor()
    names = []
    for row in cur.tables(tableType="TABLE"):
        name = row.table_name
        if name.startswith(_SYSTEM_PREFIXES):
            continue
        names.append(name)
    return sorted(names)


def get_columns(conn: pyodbc.Connection, table: str) -> List[Tuple[str, str]]:
    """[(column_name, type_name), ...] for a table, in table order."""
    cur = conn.cursor()
    return [(c.column_name, (c.type_name or "").upper()) for c in cur.columns(table=table)]


def get_searchable_columns(conn: pyodbc.Connection, table: str) -> List[str]:
    """Column names we can search — i.e. everything except binary/OLE/
    attachment fields."""
    return [name for name, type_name in get_columns(conn, table) if type_name not in _BINARY_TYPES]


def _column_condition(name: str, type_name: str) -> str:
    """SQL fragment matching one column against a `?` LIKE parameter.

    Text columns compare directly — LIKE against a NULL text cell just
    evaluates to no-match, which is fine. Non-text columns (numbers, dates,
    ...) need CStr() to compare as text, but CStr(Null) raises "Invalid use
    of Null" and aborts the whole query — on a table with millions of rows
    that's near-guaranteed to hit at least one null cell. IIf(IsNull(...))
    substitutes an empty string before CStr ever sees a null, so the row
    just doesn't match instead of blowing up the search.
    """
    if type_name in _TEXT_TYPES:
        return f"[{name}] LIKE ?"
    return f"CStr(IIf(IsNull([{name}]), '', [{name}])) LIKE ?"


def _relationships_via_adox(db_path: str) -> List[Relationship]:
    """Read designer-defined relationships (foreign keys) straight from the
    ACE engine's schema via ADOX/COM. This is the reliable path: unlike a
    plain SQL SELECT, it doesn't hit Access' "no read permission on
    MSysRelationships" restriction that applies to ordinary ODBC connections
    even for the default Admin user.
    """
    try:
        import win32com.client
    except ImportError:
        return []  # pywin32 not installed — caller falls back to ODBC metadata

    edges: List[Relationship] = []
    try:
        catalog = win32com.client.Dispatch("ADOX.Catalog")
        catalog.ActiveConnection = (
            f"Provider=Microsoft.ACE.OLEDB.12.0;Data Source={os.path.abspath(db_path)};"
        )
        for table in catalog.Tables:
            if table.Type != "TABLE":
                continue
            for key in table.Keys:
                if key.Type != 2:  # adKeyForeign
                    continue
                related_table = key.RelatedTable
                for col in key.Columns:
                    edges.append(Relationship(table.Name, col.Name, related_table, col.RelatedColumn))
    except Exception:
        return []
    return edges


def _relationships_via_odbc(conn: pyodbc.Connection) -> List[Relationship]:
    """Fallback for environments without pywin32: try the MSysRelationships
    system table directly, then ODBC foreign-key metadata. Either can fail
    depending on the driver/file's permissions — that's expected, callers
    treat an empty result as "no relationships detected", not an error.
    """
    edges: List[Relationship] = []

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT szObject, szColumn, szReferencedObject, szReferencedColumn "
            "FROM MSysRelationships"
        )
        for row in cur.fetchall():
            a_table, a_col, b_table, b_col = row[0], row[1], row[2], row[3]
            if a_table and a_col and b_table and b_col:
                edges.append(Relationship(a_table, a_col, b_table, b_col))
        if edges:
            return edges
    except pyodbc.Error:
        pass

    try:
        cur = conn.cursor()
        seen: Set[Tuple[str, str, str, str]] = set()
        for table in list_tables(conn):
            for fk in cur.foreignKeys(table=table):
                key = (fk.fktable_name, fk.fkcolumn_name, fk.pktable_name, fk.pkcolumn_name)
                if key not in seen:
                    seen.add(key)
                    edges.append(Relationship(*key))
    except pyodbc.Error:
        pass

    return edges


def get_relationships(db_path: str, conn: Optional[pyodbc.Connection] = None) -> List[Relationship]:
    """Discover the database's real, designer-defined relationships (the same
    ones shown in Access' Relationships window), so a search can follow them
    outward from a matched row.

    `conn` is optional and only used by the ODBC fallback path — pass the
    connection you already have open to avoid opening a second one.
    """
    edges = _relationships_via_adox(db_path)
    if edges:
        return edges

    owns_conn = conn is None
    if conn is None:
        try:
            conn = connect(db_path)
        except AccessSearchError:
            return []
    try:
        return _relationships_via_odbc(conn)
    finally:
        if owns_conn:
            conn.close()


def search(
    conn: pyodbc.Connection,
    term: str,
    tables: Optional[Sequence[str]] = None,
    limit_per_table: int = 25,
    errors: Optional[List[str]] = None,
) -> List[SearchHit]:
    """Search every (non-binary) column of every table for `term`, matched
    as a case-insensitive substring.

    If a table's query fails outright (a genuinely unsupported field type,
    a locked table, etc.), that table is skipped — but pass a list via
    `errors` to find out about it. Without that, a failed table looks
    identical to a table with no matches, which is exactly the kind of
    silent false negative this tool can't afford to produce.
    """
    hits: List[SearchHit] = []
    target_tables = list(tables) if tables else list_tables(conn)
    like_term = f"%{term}%"
    term_lower = term.lower()

    for table in target_tables:
        try:
            cols = get_columns(conn, table)
            cols = [(n, t) for n, t in cols if t not in _BINARY_TYPES]
            if not cols:
                continue
            where = " OR ".join(_column_condition(n, t) for n, t in cols)
            sql = f"SELECT TOP {int(limit_per_table)} * FROM [{table}] WHERE {where}"
            cur = conn.cursor()
            cur.execute(sql, [like_term] * len(cols))
            colnames = [d[0] for d in cur.description]
            for record in cur.fetchall():
                row = dict(zip(colnames, record))
                matched = [
                    n for n, _ in cols
                    if row.get(n) is not None and term_lower in str(row[n]).lower()
                ]
                hits.append(SearchHit(table=table, row=row, matched_columns=matched))
        except pyodbc.Error as exc:
            if errors is not None:
                errors.append(f"{table}: {exc}")
            continue

    return hits


def _query_by_column_value(
    conn: pyodbc.Connection, table: str, col: str, value: Any, limit: int
) -> List[Dict[str, Any]]:
    sql = f"SELECT TOP {int(limit)} * FROM [{table}] WHERE [{col}] = ?"
    cur = conn.cursor()
    cur.execute(sql, [value])
    colnames = [d[0] for d in cur.description]
    return [dict(zip(colnames, r)) for r in cur.fetchall()]


def fetch_related(
    conn: pyodbc.Connection,
    edges: Sequence[Relationship],
    table: str,
    row: Dict[str, Any],
    depth: int = 1,
    max_rows: int = 10,
    _visited: Optional[Set[Tuple[str, str, Any]]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Given a matched row, follow the relationship graph outward and return
    every linked row, grouped by table name. `depth` controls how many hops
    to follow (1 = directly-linked tables only).
    """
    if _visited is None:
        _visited = set()
    results: Dict[str, List[Dict[str, Any]]] = {}
    if depth <= 0:
        return results

    for edge in edges:
        if edge.table_a == table and edge.col_a in row and row[edge.col_a] is not None:
            other_table, other_col, value = edge.table_b, edge.col_b, row[edge.col_a]
        elif edge.table_b == table and edge.col_b in row and row[edge.col_b] is not None:
            other_table, other_col, value = edge.table_a, edge.col_a, row[edge.col_b]
        else:
            continue

        visit_key = (other_table, other_col, value)
        if visit_key in _visited:
            continue
        _visited.add(visit_key)

        try:
            related_rows = _query_by_column_value(conn, other_table, other_col, value, max_rows)
        except pyodbc.Error:
            continue
        if not related_rows:
            continue

        results.setdefault(other_table, []).extend(related_rows)

        if depth > 1:
            for related_row in related_rows:
                deeper = fetch_related(
                    conn, edges, other_table, related_row, depth - 1, max_rows, _visited
                )
                for k, v in deeper.items():
                    results.setdefault(k, []).extend(v)

    return results
