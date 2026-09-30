"""SQLite FTS5 search database for indexed repository files."""

import hashlib
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
    # Recreate FTS5 table if schema doesn't match expected
    cursor = conn.execute("PRAGMA table_info(file_content_fts)")
    columns = {row[1] for row in cursor.fetchall() if isinstance(row, (list, tuple)) and len(row) >= 2}
    if "rel_path" not in columns or "content" not in columns:
        conn.execute("DROP TABLE IF EXISTS file_content_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE file_content_fts USING fts5(rel_path, content)"
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
