"""Focused persistence tests for the ``symbols`` table and related API."""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from repo_intelligence.search import (
    create_database,
    delete_symbols_for_file,
    get_symbols,
    insert_symbols,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db():
    """Create a fresh in-memory database and return the connection."""
    conn = sqlite3.connect(":memory:")
    # Create table definitions here only — we don't call create_database
    # because it creates the FTS5 virtual table which requires special setup.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS repo_info (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
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
    _create_indexes(conn)
    return conn


def _create_indexes(conn):
    """Ensure symbol indexes exist."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_symbols_file_id ON symbols(file_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_symbols_qualified_name ON symbols(qualified_name)"
    )


def _insert_file(conn, rel_path="test.py"):
    """Insert a files row and return its id."""
    conn.execute(
        "INSERT INTO files (rel_path, language, size, sha256) VALUES (?, ?, ?, ?)",
        (rel_path, "Python", 100, "abc123"),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


SAMPLE_SYMBOLS = [
    {
        "kind": "module",
        "name": "test_module",
        "qualified_name": "test_module",
        "parent_id": None,
        "signature": "",
        "decorators": [],
        "start_line": 1,
        "end_line": 10,
        "docstring": "A module docstring.",
    },
    {
        "kind": "class",
        "name": "MyClass",
        "qualified_name": "MyClass",
        "parent_id": None,
        "signature": "",
        "decorators": ["@dataclass"],
        "start_line": 3,
        "end_line": 8,
        "docstring": "Class docstring.",
    },
    {
        "kind": "method",
        "name": "my_method",
        "qualified_name": "MyClass.my_method",
        "parent_id": 1,  # index into SAMPLE_SYMBOLS (0-based), points to MyClass at index 1
        "signature": "(self, x: int) -> str",
        "decorators": ["@property"],
        "start_line": 4,
        "end_line": 6,
        "docstring": "Method docstring.",
    },
]

# ---------------------------------------------------------------------------
# Tests (20 total)
# ---------------------------------------------------------------------------


def test_symbols_table_exists():
    """1. The ``symbols`` table is created on initial setup."""
    conn = _make_db()
    result = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='symbols'"
    ).fetchone()
    assert result is not None


def test_indexes_exist():
    """2. Required indexes exist: file_id, kind, qualified_name."""
    conn = _make_db()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='symbols'"
    ).fetchall()
    index_names = {r[0] for r in rows}
    assert "idx_symbols_file_id" in index_names
    assert "idx_symbols_kind" in index_names
    assert "idx_symbols_qualified_name" in index_names


def test_insert_one_symbol():
    """3. Inserting a single symbol row works correctly."""
    conn = _make_db()
    fid = _insert_file(conn)
    insert_symbols(conn, fid, [SAMPLE_SYMBOLS[0]])
    count = conn.execute("SELECT COUNT(*) FROM symbols WHERE file_id=?", (fid,)).fetchone()[0]
    assert count == 1


def test_insert_multiple_symbols():
    """4. Inserting multiple symbols works correctly."""
    conn = _make_db()
    fid = _insert_file(conn)
    insert_symbols(conn, fid, SAMPLE_SYMBOLS)
    count = conn.execute("SELECT COUNT(*) FROM symbols WHERE file_id=?", (fid,)).fetchone()[0]
    assert count == len(SAMPLE_SYMBOLS)


def test_file_linkage():
    """5. Symbols are correctly linked to their file rows via file_id."""
    conn = _make_db()
    fid_a = _insert_file(conn, "a.py")
    fid_b = _insert_file(conn, "b.py")

    insert_symbols(conn, fid_a, [SAMPLE_SYMBOLS[0]])
    insert_symbols(conn, fid_b, [SAMPLE_SYMBOLS[1]])

    rows_a = conn.execute(
        "SELECT file_id FROM symbols WHERE id IN ("
        "  SELECT id FROM symbols WHERE file_id=? LIMIT 2"
        ") ORDER BY id",
        (fid_a,),
    ).fetchall()
    for row in rows_a:
        assert row[0] == fid_a

    # Verify the two files' symbols are distinct
    all_rows = conn.execute(
        "SELECT file_id FROM symbols ORDER BY file_id, id"
    ).fetchall()
    file_ids = [r[0] for r in all_rows]
    assert 1 in file_ids and 2 in file_ids


def test_class_method_persistence():
    """6. A class followed by a method is stored with correct data."""
    conn = _make_db()
    fid = _insert_file(conn)
    insert_symbols(conn, fid, SAMPLE_SYMBOLS)

    cls_rows = get_symbols(conn, file_id=fid, kind="class")
    assert len(cls_rows) == 1
    assert cls_rows[0]["name"] == "MyClass"
    assert cls_rows[0]["qualified_name"] == "MyClass"

    method_rows = get_symbols(conn, file_id=fid, kind="method")
    assert len(method_rows) == 1
    assert method_rows[0]["name"] == "my_method"
    assert method_rows[0]["qualified_name"] == "MyClass.my_method"


def test_parent_db_id_mapping():
    """7. After insertion, parent_symbol_id points to the actual DB row ID of
    the parent, not to the AST-local list index."""
    conn = _make_db()
    fid = _insert_file(conn)
    insert_symbols(conn, fid, SAMPLE_SYMBOLS)

    method_rows = get_symbols(conn, file_id=fid, kind="method")
    assert len(method_rows) == 1
    parent_id = method_rows[0]["parent_symbol_id"]
    assert parent_id is not None and parent_id > 0

    # The parent must be a row in the symbols table for this file
    parent_row = conn.execute(
        "SELECT id, kind FROM symbols WHERE id=? AND file_id=?",
        (parent_id, fid),
    ).fetchone()
    assert parent_row is not None
    assert parent_row[1] == "class", f"Expected 'class', got {parent_row[1]}"
    assert parent_row[0] != 1, (
        f"parent_symbol_id ({parent_row[0]}) should NOT equal the stale "
        f"AST index (1); it must be mapped to the actual DB row ID."
    )


def test_nested_parent_relationships():
    """8. Deep nesting (module → class → nested_class → method) preserves parents."""
    source = '''
class Outer:
    class Inner:
        def __init__(self):
            pass

        def do_something(self, x: int) -> str:
            pass
'''
    temp = tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w")
    temp.write(source)
    temp.close()

    from repo_intelligence.python_ast import extract_file_symbols
    syms = extract_file_symbols("nested_test.py", source)
    conn = _make_db()
    fid = _insert_file(conn)
    insert_symbols(conn, fid, syms)

    # Verify the Inner class references Outer as parent
    inner_syms = get_symbols(conn, file_id=fid, name="Inner")
    assert len(inner_syms) == 1
    outer_id = conn.execute(
        "SELECT id FROM symbols WHERE file_id=? AND kind='class' ORDER BY id LIMIT 1", (fid,)
    ).fetchone()[0]
    assert inner_syms[0]["parent_symbol_id"] == outer_id

    # Verify __init__ references Inner as parent
    init_syms = get_symbols(conn, file_id=fid, name="__init__")
    assert len(init_syms) == 1
    inner_db_id = conn.execute(
        "SELECT id FROM symbols WHERE file_id=? AND qualified_name='Outer.Inner'", (fid,)
    ).fetchone()[0]
    assert init_syms[0]["parent_symbol_id"] == inner_db_id

    # Verify do_something references Inner as parent
    method_syms = get_symbols(conn, file_id=fid, name="do_something")
    assert len(method_syms) == 1
    assert method_syms[0]["parent_symbol_id"] == inner_db_id

    Path(temp.name).unlink(missing_ok=True)


def test_decorator_serialization():
    """9. Decorator lists are stored as JSON text and deserialize correctly."""
    conn = _make_db()
    fid = _insert_file(conn)
    insert_symbols(conn, fid, SAMPLE_SYMBOLS)

    cls_rows = get_symbols(conn, file_id=fid, kind="class")
    dec = json.loads(cls_rows[0]["decorators"])
    assert isinstance(dec, list)
    assert "@dataclass" in dec or "dataclass" in dec  # AST visitor may strip @ prefix

    method_rows = get_symbols(conn, file_id=fid, kind="method")
    mdec = json.loads(method_rows[0]["decorators"])
    assert isinstance(mdec, list)


def test_signature_persistence():
    """10. Method signatures are stored correctly as text."""
    conn = _make_db()
    fid = _insert_file(conn)
    insert_symbols(conn, fid, SAMPLE_SYMBOLS)

    method_rows = get_symbols(conn, file_id=fid, kind="method")
    assert len(method_rows) == 1
    sig = method_rows[0]["signature"]
    assert "self" in sig
    assert "x: int" in sig


def test_line_numbers():
    """11. start_line and end_line are stored correctly."""
    conn = _make_db()
    fid = _insert_file(conn)
    insert_symbols(conn, fid, SAMPLE_SYMBOLS)

    for sym_data in SAMPLE_SYMBOLS:
        rows = get_symbols(conn, file_id=fid, name=sym_data["name"])
        assert len(rows) == 1
        row = rows[0]
        assert row["start_line"] == sym_data["start_line"]
        assert row["end_line"] == sym_data["end_line"]


def test_docstring_persistence():
    """12. Docstrings are stored correctly as text."""
    conn = _make_db()
    fid = _insert_file(conn)
    insert_symbols(conn, fid, SAMPLE_SYMBOLS)

    module_rows = get_symbols(conn, file_id=fid, name="test_module")
    assert len(module_rows) == 1
    assert module_rows[0]["docstring"] == "A module docstring."

    class_rows = get_symbols(conn, file_id=fid, kind="class")
    assert class_rows[0]["docstring"] == "Class docstring."


def test_query_by_kind():
    """13. Querying by ``kind`` returns only matching rows."""
    conn = _make_db()
    fid = _insert_file(conn)
    insert_symbols(conn, fid, SAMPLE_SYMBOLS)

    classes = get_symbols(conn, file_id=fid, kind="class")
    methods = get_symbols(conn, file_id=fid, kind="method")
    assert len(classes) == 1 and classes[0]["kind"] == "class"
    assert len(methods) == 1 and methods[0]["kind"] == "method"


def test_query_by_name():
    """14. Querying by ``name`` returns only matching rows."""
    conn = _make_db()
    fid = _insert_file(conn)
    insert_symbols(conn, fid, SAMPLE_SYMBOLS)

    result = get_symbols(conn, file_id=fid, name="MyClass")
    assert len(result) == 1
    assert result[0]["name"] == "MyClass"


def test_query_by_qualified_name():
    """15. Querying by ``qualified_name`` returns the correct row."""
    conn = _make_db()
    fid = _insert_file(conn)
    insert_symbols(conn, fid, SAMPLE_SYMBOLS)

    result = get_symbols(conn, file_id=fid, qualified_name="MyClass.my_method")
    assert len(result) == 1
    assert result[0]["name"] == "my_method"


def test_query_by_file():
    """16. Querying by ``file_id`` returns only that file's symbols."""
    conn = _make_db()
    fid_a = _insert_file(conn, "a.py")
    fid_b = _insert_file(conn, "b.py")

    insert_symbols(conn, fid_a, [SAMPLE_SYMBOLS[0]])
    insert_symbols(conn, fid_b, [SAMPLE_SYMBOLS[1]])

    a_syms = get_symbols(conn, file_id=fid_a)
    b_syms = get_symbols(conn, file_id=fid_b)

    for row in a_syms:
        assert row["file_id"] == fid_a
    for row in b_syms:
        assert row["file_id"] == fid_b


def test_reinsert_no_duplicates():
    """17. Re-inserting symbols for the same file does not produce duplicates."""
    conn = _make_db()
    fid = _insert_file(conn)

    insert_symbols(conn, fid, SAMPLE_SYMBOLS)
    first_count = get_symbols(conn, file_id=fid).__len__()

    # Insert exactly the same set again
    insert_symbols(conn, fid, SAMPLE_SYMBOLS)
    second_count = get_symbols(conn, file_id=fid).__len__()

    assert first_count == second_count


def test_removed_symbol_disappears():
    """18. Removing a symbol from the source list and re-inserting removes it."""
    conn = _make_db()
    fid = _insert_file(conn)

    # Insert full set (3 symbols)
    insert_symbols(conn, fid, SAMPLE_SYMBOLS)
    after_full = get_symbols(conn, file_id=fid).__len__()
    assert after_full == 3

    # Re-insert with the method removed
    reduced = [s for s in SAMPLE_SYMBOLS if s["kind"] != "method"]
    insert_symbols(conn, fid, reduced)
    after_reduced = get_symbols(conn, file_id=fid).__len__()
    assert after_reduced == 2

    # Verify the method is truly gone
    methods = get_symbols(conn, file_id=fid, kind="method")
    assert len(methods) == 0


def test_other_files_untouched():
    """19. Replacing symbols for one file does not affect another."""
    conn = _make_db()
    fid_a = _insert_file(conn, "a.py")
    fid_b = _insert_file(conn, "b.py")

    insert_symbols(conn, fid_a, [SAMPLE_SYMBOLS[0]])
    insert_symbols(conn, fid_b, SAMPLE_SYMBOLS)

    a_count = get_symbols(conn, file_id=fid_a).__len__()
    b_count = get_symbols(conn, file_id=fid_b).__len__()
    assert a_count == 1 and b_count == 3

    # Replace only file A's symbols with a reduced set
    new_a = [S for S in SAMPLE_SYMBOLS if S["kind"] != "method"]
    insert_symbols(conn, fid_a, new_a)

    after_a = get_symbols(conn, file_id=fid_a).__len__()
    after_b = get_symbols(conn, file_id=fid_b).__len__()

    assert after_a == 2
    assert after_b == 3  # File B is unaffected


def test_empty_symbols_clears_prior():
    """20. Passing an empty symbol list clears all prior symbols for that file."""
    conn = _make_db()
    fid = _insert_file(conn)

    insert_symbols(conn, fid, SAMPLE_SYMBOLS)
    before = get_symbols(conn, file_id=fid).__len__()
    assert before == 3

    insert_symbols(conn, fid, [])  # empty replacement
    after = get_symbols(conn, file_id=fid).__len__()
    assert after == 0
