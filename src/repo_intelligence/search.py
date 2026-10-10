"""SQLite FTS5 search database for indexed repository files."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

# External base directory for all index databases (XDG_DATA_HOME compliant)
def _default_db_base():
    """Return the external base directory, respecting XDG_DATA_HOME when set."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, "repo-intelligence")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "repo-intelligence")


def db_path(repo_dir: str) -> str:
    """Return the path to the index database file for a given repository.

    The database is stored in an *external* directory under ``~/.local/share/repo-intelligence/``
    (or wherever :envvar:`XDG_DATA_HOME` points) using a deterministic subdirectory
    derived from the repository's absolute path.  This keeps indexed artifacts out of
    the working tree while guaranteeing the same repo always maps to the same location.

    Args:
        repo_dir: Path to the repository root.

    Returns:
        Absolute path to ``<external_base>/<repo-sha256-abbrev>/index.db``.
    """
    abs_repo = os.path.abspath(repo_dir)
    # Use the first 16 hex chars of the SHA-256 digest as a deterministic,
    # collision-resistant subdirectory name — short enough to avoid nesting issues
    # yet unique for practically all repository use-cases.
    repo_hash = hashlib.sha256(abs_repo.encode()).hexdigest()[:16]
    db_dir = os.path.join(_default_db_base(), repo_hash)
    return os.path.join(db_dir, "index.db")


def create_database(db_path_arg: str) -> None:
    """Create the SQLite database at *db_path* with all required tables.

    Tables created:
        - repo_info   : repository metadata (name, branch, hash, ...).
        - files       : per-file metadata (path, language, size, sha256).
        - symbols     : AST-extracted symbol data linked to *files*.
        - file_content_fts : FTS5 table for full-text search on file text content.

    If the database file already exists, this function is a no-op.
    """
    # Normalize parameter name to avoid clash with global path
    db_file = db_path_arg

    # Ensure parent directory exists
    parent = os.path.dirname(db_file)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(db_file)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS repo_info (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                rel_path   TEXT NOT NULL UNIQUE,
                language   TEXT NOT NULL,
                size       INTEGER NOT NULL DEFAULT 0,
                sha256     TEXT NOT NULL DEFAULT '',
                is_binary  INTEGER NOT NULL DEFAULT 0
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS symbols (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id         INTEGER NOT NULL REFERENCES files(id),
                kind            TEXT NOT NULL,
                name            TEXT NOT NULL,
                qualified_name  TEXT NOT NULL,
                parent_symbol_id    INTEGER,
                signature       TEXT,
                decorators      TEXT,
                start_line      INTEGER,
                end_line        INTEGER,
                docstring       TEXT
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS imports (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                kind            TEXT NOT NULL,
                module          TEXT NOT NULL DEFAULT '',
                level           INTEGER NOT NULL DEFAULT 0,
                start_line      INTEGER,
                start_col       INTEGER
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS import_names (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id       INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
                position        INTEGER NOT NULL,
                name            TEXT NOT NULL,
                asname          TEXT
            );
        """)

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_imports_file_id ON imports(file_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_import_names_import_id ON import_names(import_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_import_names_name ON import_names(name)"
        )

        # Drop any stale FTS5 table and recreate fresh
        conn.execute("DROP TABLE IF EXISTS file_content_fts")
        conn.execute("""
            CREATE VIRTUAL TABLE file_content_fts USING fts5(
                rel_path,
                content
            );
        """)
    finally:
        with conn:
            conn.commit()


def insert_repo_info(conn: sqlite3.Connection, **kwargs) -> None:
    """Store repository metadata rows *conn* repo_info table.

    Each keyword-argument becomes a (key, value) pair with *value* encoded as JSON.
    """
    import json
    for k, v in kwargs.items():
        if isinstance(v, (dict, list)):
            conn.execute(
                "INSERT OR REPLACE INTO repo_info (key, value) VALUES (?, ?)",
                (k, json.dumps(v)),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO repo_info (key, value) VALUES (?, ?)",
                (k, str(v) if v is not None else ""),
            )


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Ensure the content_files table exists for FTS5 integration."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            rel_path   TEXT NOT NULL UNIQUE,
            language   TEXT NOT NULL,
            size       INTEGER NOT NULL DEFAULT 0,
            sha256     TEXT NOT NULL DEFAULT '',
            is_binary  INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Ensure symbols table exists with indexes (for existing DBs)
    _ensure_symbols_table(conn)
    # Recreate FTS5 table if schema doesn't match expected
    cursor = conn.execute("PRAGMA table_info(file_content_fts)")
    columns = {row[1] for row in cursor.fetchall() if isinstance(row, (list, tuple)) and len(row) >= 2}
    if "rel_path" not in columns or "content" not in columns:
        conn.execute("DROP TABLE IF EXISTS file_content_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE file_content_fts USING fts5(rel_path, content)"
        )


def _ensure_symbols_table(conn: sqlite3.Connection) -> None:
    """Ensure the symbols table and its indexes exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS symbols (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id         INTEGER NOT NULL REFERENCES files(id),
            kind            TEXT NOT NULL,
            name            TEXT NOT NULL,
            qualified_name  TEXT NOT NULL,
            parent_symbol_id    INTEGER,
            signature       TEXT,
            decorators      TEXT,
            start_line      INTEGER,
            end_line        INTEGER,
            docstring       TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_symbols_file_id ON symbols(file_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_symbols_qualified_name ON symbols(qualified_name)"
    )


def _ensure_imports_tables(conn: sqlite3.Connection) -> None:
    """Ensure the imports/import_names tables and their indexes exist."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS imports (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            kind            TEXT NOT NULL,
            module          TEXT NOT NULL DEFAULT '',
            level           INTEGER NOT NULL DEFAULT 0,
            start_line      INTEGER,
            start_col       INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS import_names (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id       INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
            position        INTEGER NOT NULL,
            name            TEXT NOT NULL,
            asname          TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_imports_file_id ON imports(file_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_import_names_import_id ON import_names(import_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_import_names_name ON import_names(name)"
    )


def insert_file_metadata(conn: sqlite3.Connection, rel_path: str, lang: str, size: int, sha256: str) -> None:
    """Insert *file*' metadata into the files table; upserts on rel_path.

    Args:
        conn: Active SQLite connection.
        rel_path: Relative path of the file within the repo.
        lang: Detected programming language label.
        size: File size in bytes.
        sha256: Hex-encoded SHA-256 digest of the file's contents.
    """
    _ensure_table(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO files (rel_path, language, size, sha256, is_binary)
        VALUES (?, ?, ?, ?, 0)
        """,
        (rel_path, lang, size, sha256),
    )

    return None  # explicit no-value return


def insert_text_content(conn: sqlite3.Connection, rel_path: str, content: str) -> None:
    """Insert *file*'s text content for FTS5 indexing; upserts on rel_path.

    Args:
        conn: Active SQLite connection.
        rel_path: Relative path of the file (must match files.rel_path).
        content: Decoded text to be searchable.
    """
    _ensure_table(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO file_content_fts (rel_path, content)
        VALUES (?, ?)
        """,
        (rel_path, content),
    )


def search_files(db_file: str, query: str) -> list[dict]:
    """Search the FTS5 index for *query* and return matching files as dicts.

    The function supports two prefixes:
        - ``like:<pattern>``  → uses SQLite LIKE with wildcard expansion
        - anything else       → performs an FTS5 MATCH expression

    Args:
        db_file: Path to the indexed database file.
        query: Search query string (optionally prefixed with ``like:``).

    Returns:
        List of dicts ``{rel_path, language, size, sha256}`` for matched files.
    """
    conn = sqlite3.connect(db_file)
    try:
        _ensure_table(conn)

        # Check for `like:` prefix → glob-style pattern matching on rel_path
        if query.lower().startswith("like:"):
            pattern = query[len("like:"):]
            like_pattern = pattern.replace("*", "%")
            cursor = conn.execute(
                """
                SELECT rel_path, language, size, sha256
                FROM files
                WHERE rel_path LIKE ?
                """,
                (like_pattern,),
            )
        else:
            # FTS5 MATCH query - escape special chars but keep operators user can use
            escaped_query = query.replace("'", "''")
            cursor = conn.execute(
                """
                SELECT rel_path, language, size, sha256
                FROM files
                JOIN file_content_fts USING (rel_path)
                WHERE file_content_fts MATCH ?
                """,
                (escaped_query,),
            )

        rows = cursor.fetchall()
        return [
            {
                "rel_path": row[0],
                "language": row[1],
                "size": row[2],
                "sha256": row[3],
            }
            for row in rows
        ]
    finally:
        conn.close()


def get_file_list(db_file: str, lang_filter=None, path_prefix=None, limit=1000, offset=0) -> list[dict]:
    """Return a list of indexed files.

    Args:
        db_file: Path to the database file.
        lang_filter: Optional language name for filtering (e.g., ``"Python"``).
            Matching is case-insensitive.
        path_prefix: Optional repository-relative path prefix for filtering
            (e.g., ``"tests/"`` matches ``"tests/route_search_observer.py"``).
        limit: Maximum number of rows to return (default 1000).
        offset: Row offset for pagination (default 0).

    Returns:
        List of dicts ``{rel_path, language, size, sha256}``.
    """
    conn = sqlite3.connect(db_file)
    try:
        _ensure_table(conn)

        conditions: list[str] = []
        params: list = []

        if lang_filter is not None:
            conditions.append("LOWER(language) = LOWER(?)")
            params.append(lang_filter)
        if path_prefix is not None:
            conditions.append("rel_path LIKE ?")
            params.append(path_prefix.rstrip("/") + "/%")

        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        query = (
            "SELECT rel_path, language, size, sha256 FROM files"
            f"{where_clause}"
            " ORDER BY rel_path LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        cursor = conn.execute(query, params)

        return [
            {
                "rel_path": row[0],
                "language": row[1],
                "size": row[2],
                "sha256": row[3],
            }
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


def get_stats(db_file: str) -> dict:
    """Return aggregated statistics for the indexed repository.

    Returns a dict with keys:
        - repo_name, current_branch, commit_hash, is_dirty, is_git_repo
          (from repo_info table; may be None/False if not set).
        - total_files: total number of indexed files.
        - total_bytes: sum of file sizes.
        - language_breakdown: ``{language: count}`` of all indexed files.
    """
    if not os.path.exists(db_file):
        return {
            "repo_name": "unknown",
            "total_files": 0,
            "total_bytes": 0,
            "language_breakdown": {},
        }

    conn = sqlite3.connect(db_file)
    try:
        _ensure_table(conn)

        # Repo metadata (stored as key-value rows)
        repo_info_rows = conn.execute("SELECT key, value FROM repo_info").fetchall()
        raw_info = {row[0]: row[1] for row in repo_info_rows} if repo_info_rows else {}

        # File stats
        total_files_row = conn.execute("SELECT COUNT(*) FROM files").fetchone()
        total_files = int(total_files_row[0]) if total_files_row else 0

        total_bytes_row = conn.execute("SELECT COALESCE(SUM(size), 0) FROM files").fetchone()
        total_bytes = int(total_bytes_row[0]) if total_bytes_row else 0

        lang_rows = conn.execute(
            "SELECT language, COUNT(*) as cnt FROM files GROUP BY language ORDER BY cnt DESC"
        ).fetchall()
        lang_breakdown = {row[0]: row[1] for row in lang_rows}

        # Merge repo info from raw_info into a dict-like structure
        name_val = raw_info.get("name") or raw_info.get("repo_name", "unknown") if raw_info else "unknown"
        meta = {}
        if raw_info:
            import json
            # Handle JSON-encoded values
            for key in ("branch", "commit_hash"):
                kdbkey = "current_branch" if key == "branch" else key
                val = raw_info.get(kdbkey)
                meta[key] = json.loads(val) if isinstance(val, str) and (val.startswith('"') or val.startswith("{")) else val
            is_dirty_val = raw_info.get("is_dirty", False)
            meta["is_dirty"] = True if is_dirty_val == "True" or is_dirty_val is True else False

        return {
            "repo_name": name_val,
            "total_files": total_files,
            "total_bytes": total_bytes,
            "language_breakdown": lang_breakdown,
            **meta,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Symbol persistence API
# ---------------------------------------------------------------------------


def insert_symbols(conn: sqlite3.Connection, file_id: int, symbols: list[dict]) -> None:
    """Replace all symbols for *file_id* with the provided symbol list.

    Strategy: delete existing rows first, then insert new ones in deterministic
    order (same order as they appear on disk / in AST walk) and map the AST-local
    ``parent_id`` (an index into the original list) to the resulting database
    row IDs after insertion.

    Args:
        conn: Active SQLite connection.
        file_id: Primary key of the associated file row.
        symbols: List of symbol dicts with keys matching the schema
            (*kind*, *name*, *qualified_name*, *parent_id*, *signature*,
            *decorators*, *start_line*, *end_line*, *docstring*).
            Decorator values may be a list of strings; they will be JSON-encoded.
    """
    conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))

    if not symbols:
        return

    # Pre-encode decorators lists → JSON text
    row_data: list[tuple] = []
    for sym in symbols:
        decs = sym.get("decorators")
        decs_json = json.dumps(decs) if isinstance(decs, (list, tuple)) else (decs or "")
        row_data.append((
            file_id,
            sym["kind"],
            sym["name"],
            sym["qualified_name"],
            sym.get("parent_id"),  # raw AST-local index — replaced below
            sym.get("signature") or "",
            decs_json,
            sym.get("start_line"),
            sym.get("end_line"),
            sym.get("docstring") or "",
        ))

    conn.executemany(
        """INSERT INTO symbols
           (file_id, kind, name, qualified_name, parent_symbol_id,
            signature, decorators, start_line, end_line, docstring)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        row_data,
    )

    indexed_ids = [None] * len(row_data)  # AST-index → db-id mapping
    cursor = conn.execute(
        "SELECT id, parent_symbol_id FROM symbols WHERE file_id = ?",
        (file_id,),
    )
    rows_ordered = cursor.fetchall()
    for i in range(len(rows_ordered)):
        indexed_ids[i] = rows_ordered[i][0]

    # Now fix parent_symbol_id using the mapped IDs
    updates: list[tuple[int, int]] = []
    for aidx, (dbid, raw_parent) in enumerate(rows_ordered):
        if raw_parent is not None and isinstance(raw_parent, int) and 0 <= raw_parent < len(indexed_ids):
            mapped_id = indexed_ids[raw_parent]
            if mapped_id is not None:
                updates.append((mapped_id, dbid))

    for parent_id, child_id in updates:
        conn.execute(
            "UPDATE symbols SET parent_symbol_id = ? WHERE id = ?",
            (parent_id, child_id),
        )


def delete_symbols_for_file(conn: sqlite3.Connection, file_id: int) -> None:
    """Remove all symbol rows for the given *file_id*.

    Args:
        conn: Active SQLite connection.
        file_id: Primary key of the associated file row.
    """
    conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))


def get_symbols(
    db_file: str | sqlite3.Connection,
    *,
    file_id: int | None = None,
    kind: str | None = None,
    name: str | None = None,
    qualified_name: str | None = None,
    source_path: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """Query symbol rows from the database.

    Args:
        db_file: Path to a database file **or** an open :class:`sqlite3.Connection`.
        file_id: Filter by symbols.table.file_id (exact match).
        kind: Filter by symbols.kind (exact match).
        name: Filter by symbols.name (exact match).
        qualified_name: Filter by symbols.qualified_name (exact match).
        source_path: Filter by files.rel_path (resolved via JOIN, exact match).
        limit: Maximum number of results (None means no limit).
        offset: Row offset for pagination.

    Returns:
        List of dicts keyed by column name.
    """
    close_early = isinstance(db_file, str)
    conn = sqlite3.connect(db_file) if close_early else db_file
    try:
        # Ensure symbols table exists (for DBs that only used create_database
        # but may lack the CREATE INDEX fallback path).
        _ensure_symbols_table(conn)

        conditions: list[str] = []
        params: list = []

        if file_id is not None:
            conditions.append("sym.file_id = ?")
            params.append(file_id)
        if kind is not None:
            conditions.append("LOWER(sym.kind) = LOWER(?)")
            params.append(kind)
        if name is not None:
            conditions.append("sym.name = ?")
            params.append(name)
        if qualified_name is not None:
            conditions.append("sym.qualified_name = ?")
            params.append(qualified_name)
        if source_path is not None:
            conditions.append("f.rel_path = ?")
            params.append(source_path)

        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        query = (
            "SELECT sym.id, sym.file_id, f.rel_path as rel_path,"
            "  sym.kind, sym.name, sym.qualified_name,"
            "  sym.parent_symbol_id, sym.signature, sym.decorators,"
            "  sym.start_line, sym.end_line, sym.docstring"
            " FROM symbols sym"
            " JOIN files f ON sym.file_id = f.id"
            + where_clause
            + " ORDER BY sym.id ASC"
        )

        if limit is not None:
            query += f" LIMIT {int(limit)}"
        if offset > 0:
            query += f" OFFSET {int(offset)}"

        cursor = conn.execute(query, params)

        rows = cursor.fetchall()
        col_names = [d[0] for d in cursor.description] if cursor.description else []
        return [dict(zip(col_names, row)) for row in rows]
    finally:
        if close_early:
            conn.close()


def get_symbols_for_file_file_id(
    db_file: str | sqlite3.Connection, file_id: int
) -> list[dict]:
    """Convenience wrapper returning symbols for a single *file_id*.

    Args:
        db_file: Path to a database file **or** an open :class:`sqlite3.Connection`.
        file_id: Primary key of the referenced file row.

    Returns:
        List of symbol dicts for that file, in DB-insertion order (ordered by *id*).
    """
    return get_symbols(db_file, file_id=file_id)


# ---------------------------------------------------------------------------
# Import persistence API
# ---------------------------------------------------------------------------


def replace_imports_for_file(
    conn: sqlite3.Connection,
    file_id: int,
    records: list | None = None,
) -> None:
    """Replace all import rows for *file_id* with the provided records.

    Strategy mirrors ``insert_symbols``: delete existing rows first, then
    insert fresh ones in deterministic (source-order) position.

    Args:
        conn: Active SQLite connection.
        file_id: Primary key of the associated file row.
        records: List of :class:`~repo_intelligence.python_ast.ImportRecord`
            objects or ``None`` / empty list to clear stale rows.  Each record
            must have attributes ``kind``, ``module``, ``level``,
            ``start_line``, ``start_col`` and a ``names`` iterable of
            ``(name, asname | None)`` pairs.
    """
    if _is_str_path(conn):
        # When given a real path we open/close the connection ourselves.
        _ensure_imports_tables(conn)  # type: ignore[arg-type]

    conn.execute("DELETE FROM import_names WHERE import_id IN ("
                 "SELECT id FROM imports WHERE file_id = ?)",
                 (file_id,))
    conn.execute("DELETE FROM imports WHERE file_id = ?", (file_id,))

    if not records:
        return

    _ensure_imports_tables(conn)

    row_data: list[tuple] = []
    name_insert_params: list[tuple[int, int, str, str | None]] = []
    for i, rec in enumerate(records):
        kind = getattr(rec, "kind", "") or ""
        mod = getattr(rec, "module", "") or ""
        level = getattr(rec, "level", 0) or 0
        sl = getattr(rec, "start_line", None)
        sc = getattr(rec, "start_col", None)
        row_data.append((file_id, kind, mod, level, sl, sc))

    conn.executemany(
        """INSERT INTO imports
               (file_id, kind, module, level, start_line, start_col)
               VALUES (?, ?, ?, ?, ?, ?)""",
        row_data,
    )

    # Collect import IDs inserted above, in deterministic order.
    cursor = conn.execute(
        "SELECT id FROM imports WHERE file_id = ? ORDER BY id ASC",
        (file_id,),
    )
    imported_ids = [row[0] for row in cursor.fetchall()]

    for idx, dbid in enumerate(imported_ids):
        rec = records[idx]
        names_iter = getattr(rec, "names", ()) or ()
        pos = 0
        for entry in names_iter:
            name_val = entry[0] if isinstance(entry, (list, tuple)) and len(entry) >= 1 else entry
            asname_val = entry[1] if isinstance(entry, (list, tuple)) and len(entry) >= 2 else None
            name_insert_params.append((dbid, pos, name_val, asname_val))
            pos += 1

    if name_insert_params:
        conn.executemany(
            """INSERT INTO import_names
                   (import_id, position, name, asname)
                   VALUES (?, ?, ?, ?)""",
            name_insert_params,
        )


def delete_imports_for_file(conn: sqlite3.Connection, file_id: int) -> None:
    """Remove all import rows and their child names for the given *file_id*.

    Args:
        conn: Active SQLite connection.
        file_id: Primary key of the associated file row.
    """
    conn.execute("DELETE FROM import_names WHERE import_id IN ("
                 "SELECT id FROM imports WHERE file_id = ?)",
                 (file_id,))
    conn.execute("DELETE FROM imports WHERE file_id = ?", (file_id,))


def _is_str_path(obj: object) -> bool:
    """Return True if *obj* looks like a database-path string."""
    return isinstance(obj, str)


def get_imports(
    db_file: str | sqlite3.Connection,
    *,
    file_id: int | None = None,
    kind: str | None = None,
    module: str | None = None,
    source_path: str | None = None,
    name_filter: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """Query import rows from the database, including their ``import_names``.

    Args:
        db_file: Path to a database file **or** an open :class:`sqlite3.Connection`.
        file_id: Filter by imports.file_id (exact match).
        kind: Filter by imports.kind (exact match).
        module: Filter by imports.module (exact match).
        source_path: Filter by files.rel_path (resolved via JOIN, exact match).
        name_filter: Filter by import_names.name (exact match; matched against the
            imported name, not the alias). Implemented as a subquery so an import
            statement with several matching names returns exactly once.
        limit: Maximum number of import rows to return.
        offset: Row offset for pagination (default 0).

    Results are ordered deterministically by the source file's repository-relative
    path, then start_line, start_col, then imports.id as a total-order tie-breaker.

    Returns:
        List of dicts keyed by column name for the ``imports`` table only.
        Each dict adds an ``import_names`` key whose value is a list of
        ``{position, name, asname}`` dicts (ordered by position ASC).
    """
    close_early = _is_str_path(db_file)
    conn = sqlite3.connect(db_file) if close_early else db_file
    try:
        _ensure_imports_tables(conn)

        conditions: list[str] = []
        params: list = []

        if file_id is not None:
            conditions.append("i.file_id = ?")
            params.append(file_id)
        if kind is not None:
            conditions.append("LOWER(i.kind) = LOWER(?)")
            params.append(kind)
        if module is not None:
            conditions.append("i.module = ?")
            params.append(module)
        if source_path is not None:
            conditions.append("f.rel_path = ?")
            params.append(source_path)
        if name_filter is not None:
            # Subquery (not a JOIN) so a multi-name import statement whose
            # statement has several matching names is still returned once.
            conditions.append(
                "i.id IN ("
                "SELECT import_id FROM import_names"
                " WHERE import_names.name = ?)"
            )
            params.append(name_filter)

        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        query = (
            "SELECT i.id, i.file_id, f.rel_path as rel_path, "
            "  i.kind, i.module, i.level, i.start_line, i.start_col"
            " FROM imports i"
            " JOIN files f ON i.file_id = f.id"
            + where_clause
            + " ORDER BY f.rel_path ASC, i.start_line ASC, i.start_col ASC, i.id ASC"
        )

        if limit is not None:
            query += f" LIMIT {int(limit)}"
        if offset > 0:
            query += f" OFFSET {int(offset)}"

        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        col_names = [d[0] for d in cursor.description] if cursor.description else []
        dicts_list = [dict(zip(col_names, row)) for row in rows]

        # Enrich each import row with its name child rows.
        for d in dicts_list:
            nid = d["id"]
            name_cursor = conn.execute(
                "SELECT position, name, asname FROM import_names"
                " WHERE import_id = ? ORDER BY position ASC",
                (nid,),
            )
            d["import_names"] = [
                {"position": r[0], "name": r[1], "asname": r[2]}
                for r in name_cursor.fetchall()
            ]

        return dicts_list
    finally:
        if close_early:
            conn.close()
