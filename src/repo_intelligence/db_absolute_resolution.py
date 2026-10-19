"""Phase 2C-2B: read-only database-backed absolute import resolution.

Narrow, deterministic adapter from an *existing* repo-intelligence SQLite
index to the frozen Phase 2C-2A pure resolver
:func:`repo_intelligence.absolute_resolution.resolve_absolute_import`.

Guarantees (per docs/phase-2c-2b-architecture-approved.md):

* **Read-only.**  The index is opened through a SQLite URI with
  ``mode=ro`` plus ``PRAGMA query_only=ON`` (defense in depth); only
  SELECT and read-only PRAGMA statements are ever executed.  No schema
  object is created, no row is written, no derived result is stored.
* **No DDL helper reuse.**  ``repo_intelligence.search.get_imports`` /
  ``_ensure_imports_tables`` (which contain DDL) are never called; the
  import statements and their imported names are read by adapter-only
  SELECT queries with set-based/bounded-batch name loading.
* **Inventory reuse.**  The frozen Phase 2C-1 file-row reader and module
  inventory builder are reused on the same owned connection inside one
  explicit read transaction (consistent snapshot).  The complete file
  inventory is always used, regardless of the optional ``source_path``
  statement filter.
* **Fail-closed reconstruction.**  Missing required tables/columns,
  orphaned rows, duplicate/invalid name positions, or absent/invalid
  ``start_line``/``start_col`` coordinates raise
  :class:`AdapterError`; rows are never silently dropped, repaired, or
  fabricated.
* **Semantic pass-through.**  Reconstructed
  :class:`repo_intelligence.python_ast.ImportRecord` objects are passed
  unchanged to the frozen resolver; the adapter never retries, resolves
  relative levels, or alters statuses/reasons.

This milestone adds no CLI surface, no JSON contract, no MCP endpoint,
no relative-import resolution, and no public database row-ID identity.
"""

from __future__ import annotations

import os
import sqlite3
from urllib.parse import quote

from repo_intelligence.absolute_resolution import (
    Resolution,
    resolve_absolute_import,
)
from repo_intelligence.module_inventory import (
    build_module_inventory,
    load_file_records_from_db,
)

# Frozen Phase 2C-1 root validation is exposed via a private helper;
# the adapter is a sibling module in the same package and is expected
# to reuse it (per the approved architecture, no new root validation
# rules may be introduced here).
from repo_intelligence.module_inventory import _canonical_roots as _validate_roots
from repo_intelligence.python_ast import ImportRecord

#: Upper bound on parameters per single SELECT (SQLite parameter count
#: limit is 999; stay strictly below it).
_BATCH_SIZE = 900


class AdapterError(RuntimeError):
    """A persisted index cannot be losslessly reconstructed.

    Raised for missing required tables/columns, orphaned import or
    import-name rows, duplicate or non-contiguous imported-name
    positions, and absent/invalid required statement coordinates.  This
    is a storage/integrity error and is deliberately distinct from the
    pure resolver's semantic ``UNSUPPORTED``/``NOT_ATTEMPTED`` outcomes.
    The adapter never repairs, migrates, or silently drops data.
    """


# ---------------------------------------------------------------------------
# Read-only connection
# ---------------------------------------------------------------------------


def _open_read_only(db_path: str) -> sqlite3.Connection:
    """Open an existing index in a strictly read-only owned connection.

    The absolute path is percent-encoded exactly as the Phase 2C-1
    read-only opener does (``urllib.parse.quote(path, safe='/')``) so URI
    -significant characters (``#``, ``?``, ``%``, literal ``%3F``)
    survive SQLite's URI parsing.  ``mode=ro`` guarantees the engine
    rejects all writes and can never create the file.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(
            f"index database not found: {db_path!r} "
            "(Phase 2C-2B never creates index databases)"
        )
    abs_path = os.path.abspath(db_path)
    uri = "file:" + quote(abs_path, safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


# ---------------------------------------------------------------------------
# Schema / integrity validation (read-only)
# ---------------------------------------------------------------------------

# table -> required columns (existence checked via sqlite_master)
_REQUIRED_SCHEMA: dict[str, tuple[str, ...]] = {
    "files": ("id", "rel_path"),
    "imports": ("id", "file_id", "kind", "module", "level",
                "start_line", "start_col"),
    "import_names": ("id", "import_id", "position", "name", "asname"),
}


def _validate_schema(conn: sqlite3.Connection) -> None:
    """Fail closed when required tables or columns are absent."""
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    except sqlite3.Error as exc:
        raise AdapterError(f"cannot read schema metadata: {exc}") from exc

    present = {r[0] for r in rows}
    for table, required in _REQUIRED_SCHEMA.items():
        if table not in present:
            raise AdapterError(f"required table '{table}' is absent from the index")
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        have_cols = {r[1].lower() for r in info}
        for col in required:
            if col not in have_cols:
                raise AdapterError(
                    f"required column '{table}.{col}' is absent from the index"
                )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _name_sort_key(name: str, asname: str | None) -> tuple:
    """Comparable key for one imported-name ``(name, asname)`` pair.

    ``asname`` is legitimately ``None`` (no alias) or a ``str``; a raw
    tuple comparison of ``('a', None)`` against ``('a', 'alias')`` would
    raise ``TypeError`` because ``None`` is not ordering-comparable with
    ``str``.  The key tags the alias with a boolean presence flag so that
    the ordering is a **total order** over the whole domain: the absent
    alias sorts deterministically (before any present alias) and present
    aliases compare by their string value.  This key is used only for
    ordering; the original ``names`` tuple on the record is never
    rewritten.
    """
    return (name, (0 if asname is None else 1, asname))


def _record_order_key(record: ImportRecord) -> tuple:
    """Deterministic total-order key for a reconstructed record.

    ``record.names`` (a tuple of ``(str, str | None)`` pairs) is mapped
    through :func:`_name_sort_key` so that missing aliases ``str | None``
    never meet in a raw comparison.  The key matches the approved
    ordering invariants (``source_path, start_line, start_col, kind,
    module, level, names``) and deliberately excludes any SQLite row ID;
    equal records may repeat as equal keys.
    """
    return (
        record.source_path,
        record.start_line,
        record.start_col,
        record.kind,
        record.module,
        record.level,
        tuple(
            _name_sort_key(name, asname) for name, asname in record.names
        ),
    )


def _validate_import_rows(rows: list[tuple]) -> None:
    """Fail closed on absent/invalid required *storage* fields only.

    Checks are deterministic and independent of row order.  Only values
    the frozen :class:`ImportRecord` cannot structurally represent are
    rejected here: non-text ``kind``/``module``, non-integer ``level``,
    and absent/invalid ``start_line``/``start_col``.  A *representable*
    unknown ``kind`` string (e.g. ``bad_kind``) is deliberately NOT enum-
    validated at the adapter: the frozen resolver interprets it and
    returns its defined ``NOT_ATTEMPTED`` / ``invalid_record_kind``
    outcome, and in-memory vs database-backed equality is preserved.
    """
    for i, row in enumerate(rows):
        (_id, _rel_path, kind, module, level, start_line, start_col) = row
        if not isinstance(kind, str):
            raise AdapterError(
                f"imports row {i + 1} has non-text kind {kind!r}"
            )
        if not isinstance(module, str):
            raise AdapterError(
                f"imports row {i + 1} has non-text module {module!r}"
            )
        if not _is_int(level):
            raise AdapterError(
                f"imports row {i + 1} has invalid level {level!r}"
            )
        if not _is_int(start_line) or start_line <= 0:
            raise AdapterError(
                f"imports row {i + 1} has absent/invalid start_line {start_line!r}"
            )
        if not _is_int(start_col) or start_col < 0:
            raise AdapterError(
                f"imports row {i + 1} has absent/invalid start_col {start_col!r}"
            )


def _load_and_validate_names(
    conn: sqlite3.Connection, import_ids: list[int]
) -> dict[int, list[tuple]]:
    """Load imported-name rows for the selected imports in bounded batches.

    Rows are grouped per import ID and the positions are verified to be
    exactly contiguous (no gaps, no duplicates, no negative positions):
    anything else would make lossless reconstruction of the frozen
    ``ImportRecord.names`` tuple impossible, so the adapter fails
    closed instead of guessing or dropping names.
    """
    names: dict[int, list[tuple]] = {}
    seen: dict[int, set[int]] = {}
    for start in range(0, len(import_ids), _BATCH_SIZE):
        batch = import_ids[start:start + _BATCH_SIZE]
        placeholders = ", ".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT import_id, position, name, asname FROM import_names "
            f"WHERE import_id IN ({placeholders}) "
            f"ORDER BY import_id, position",
            batch,
        ).fetchall()
        for import_id, position, name, asname in rows:
            if not _is_int(import_id) or not _is_int(position):
                raise AdapterError(
                    f"import_names has invalid types at position {position!r}"
                )
            if not isinstance(name, str):
                raise AdapterError(
                    f"import_names has non-text name {name!r} for import {import_id}"
                )
            if asname is not None and not isinstance(asname, str):
                raise AdapterError(
                    f"import_names has non-text asname {asname!r} "
                    f"for import {import_id}"
                )
            positions = seen.setdefault(import_id, set())
            if position in positions:
                raise AdapterError(
                    f"import_names has duplicate position {position} "
                    f"for import_id {import_id}"
                )
            positions.add(position)
            names.setdefault(import_id, []).append((name, asname))

    for imp_id in names:
        # Contiguity check: positions must equal exactly 0..n-1 (the
        # persisted convention, per ``replace_imports_for_file``).
        expected = list(range(len(names[imp_id])))
        pos_list = sorted(seen[imp_id])
        if pos_list != expected:
            raise AdapterError(
                f"import_names positions for import_id {imp_id} are not "
                f"contiguous 0..{len(names[imp_id]) - 1}: got {pos_list}"
            )
    return names


def _verify_no_orphans(conn: sqlite3.Connection) -> None:
    """Detect import and import-name orphans explicitly (fail closed).

    The statement SELECT joins ``imports`` to ``files``, so an import row
    whose ``file_id`` is missing from ``files`` would otherwise be
    silently dropped by the INNER JOIN.  Likewise an ``import_names``
    row whose ``import_id`` matches nothing would vanish.  Both cases
    are deterministic storage errors; both are checked globally rather
    than per-selection, and both raise :class:`AdapterError`.
    """
    missing_file_count = conn.execute(
        "SELECT COUNT(*) FROM imports i "
        "LEFT JOIN files f ON f.id = i.file_id WHERE f.id IS NULL"
    ).fetchone()[0]
    if missing_file_count:
        raise AdapterError(
            f"{missing_file_count} orphaned import row(s) reference "
            "file_id values absent from the files table"
        )

    orphan_names = conn.execute(
        "SELECT COUNT(*) FROM import_names ni "
        "LEFT JOIN imports i ON i.id = ni.import_id WHERE i.id IS NULL"
    ).fetchone()[0]
    if orphan_names:
        raise AdapterError(
            f"{orphan_names} orphaned import_names row(s) reference "
            "import_id values absent from the imports table"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_absolute_imports_from_db(
    db_path: str,
    roots: tuple[str, ...] | list[str],
    *,
    source_path: str | None = None,
) -> tuple[Resolution, ...]:
    """Resolve absolute imports from an existing index (read-only).

    Args:
        db_path: Path to an *existing* SQLite index.  Never created; a
            missing path raises :class:`FileNotFoundError`.
        roots: Explicit, non-empty package roots, validated and
            normalized by the frozen Phase 2C-1 rules (``TypeError`` /
            ``ValueError`` on invalid or empty input).  No root guessing
            and no implicit repository-root fallback.
        source_path: Optional exact repository-relative path filter
            applied to import *statements only*; the module inventory is
            always built over the complete file set.

    Returns:
        An immutable tuple of the frozen Phase 2C-2A
        :class:`Resolution` objects, each retaining its original
        :class:`ImportRecord`, status, reason, and supported/unsupported
        candidate partitions.  Records are ordered by
        ``(source_path, start_line, start_col, kind, module, level,
        names)``; row IDs never influence the externally visible order.

    Raises:
        FileNotFoundError:  *db_path* does not exist.
        TypeError / ValueError:  *roots* fail frozen Phase 2C-1 validation.
        AdapterError:  The index lacks required tables/columns, contains
            orphaned or malformed rows, and cannot be losslessly
            reconstructed.
        sqlite3.Error:  Any engine-level read failure (also raised on
            write attempts, which the read-only handle rejects).
    """
    # Approved contract: roots is an explicit, nonempty tuple or list.
    if not isinstance(roots, (tuple, list)):
        raise TypeError(
            f"roots must be a tuple or list of strings; "
            f"got {type(roots).__name__}"
        )

    if not isinstance(db_path, str) or db_path == "":
        raise TypeError(
            f"db_path must be a non-empty string; got {db_path!r}"
        )

    # Fail fast on root validation before touching the file system:
    # the frozen Phase 2C-1 rules (TypeError for non-string items,
    # ValueError for empty/absolute/escape/unsupported components)
    # apply here exactly as they do inside build_module_inventory.
    _validate_roots(roots)

    conn = _open_read_only(db_path)
    read_started = False
    try:
        # One explicit consistent read snapshot across files, imports,
        # and import_names.  (BEGIN DEFERRED: first statement is a read,
        # so the snapshot is taken at the reader.)
        try:
            conn.execute("BEGIN DEFERRED")
        except sqlite3.OperationalError:
            # SQLite may already hold us inside an implicit transaction.
            pass
        else:
            read_started = True

        # --- Schema validation (read-only metadata only) ---------------
        _validate_schema(conn)

        # --- Phase 2C-1: complete file inventory, same snapshot --------
        file_records = load_file_records_from_db(conn)
        inventory = build_module_inventory(file_records, roots)

        # --- Adapter-only statement SELECT (no DDL helper) -------------
        if source_path is None:
            stmt_query = (
                "SELECT i.id, f.rel_path, i.kind, i.module, i.level, "
                "i.start_line, i.start_col "
                "FROM imports AS i JOIN files AS f ON f.id = i.file_id "
                "ORDER BY f.rel_path COLLATE BINARY, i.start_line, "
                "i.start_col, i.kind, i.module, i.level, i.id"
            )
            stmt_params: list = []
        else:
            stmt_query = (
                "SELECT i.id, f.rel_path, i.kind, i.module, i.level, "
                "i.start_line, i.start_col "
                "FROM imports AS i JOIN files AS f ON f.id = i.file_id "
                "WHERE f.rel_path = ? "
                "ORDER BY f.rel_path COLLATE BINARY, i.start_line, "
                "i.start_col, i.kind, i.module, i.level, i.id"
            )
            stmt_params = [source_path]
        rows = conn.execute(stmt_query, stmt_params).fetchall()
        import_ids = [row[0] for row in rows]

        # --- Fail-closed integrity checks ------------------------------
        _validate_import_rows(rows)
        _verify_no_orphans(conn)
        names_by_import = _load_and_validate_names(conn, import_ids)

        # --- Reconstruct frozen ImportRecord objects -------------------
        records: list[ImportRecord] = []
        for imp_id, rel_path, kind, module, level, start_line, start_col in rows:
            name_rows = names_by_import.get(imp_id, [])
            names = tuple(name_rows)
            records.append(
                ImportRecord(
                    kind=kind,
                    module=module,
                    names=names,
                    level=level,
                    source_path=rel_path,
                    start_line=start_line,
                    start_col=start_col,
                )
            )

        # Row-ID-free deterministic total-order ordering; equal records
        # may repeat.  The key maps optional aliases to a total order
        # (see _record_order_key) and never consults SQLite row IDs.
        records.sort(key=_record_order_key)

        # --- Frozen resolver, unchanged (semantic pass-through) --------
        return tuple(resolve_absolute_import(rec, inventory) for rec in records)
    finally:
        if read_started:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
        conn.close()
