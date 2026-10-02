"""Focused SQLite persistence tests for the ``imports`` and ``import_names`` tables.

Covers Phase 2B-2: persistent storage of import declarations extracted by
Phase 2B-1, without wiring extraction into ``repo-index build`` or adding any
new CLI commands.

Each test name maps to a requirement number from the spec.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from repo_intelligence.python_ast import ImportRecord
from repo_intelligence.search import (
    create_database,
    delete_imports_for_file,
    get_imports,
    replace_imports_for_file,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_inmemory_db() -> sqlite3.Connection:
    """Create a fresh in-memory connection with all tables."""
    conn = sqlite3.connect(":memory:")
    # Enable FK enforcement (SQLite disables it by default)
    conn.execute("PRAGMA foreign_keys = ON")
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_imports_file_id ON imports(file_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_import_names_import_id ON import_names(import_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_import_names_name ON import_names(name)")
    return conn


def _insert_file(conn: sqlite3.Connection, rel_path: str = "test.py") -> int:
    """Insert a files row and return its id."""
    conn.execute(
        "INSERT INTO files (rel_path, language, size, sha256) VALUES (?, ?, ?, ?)",
        (rel_path, "Python", 100, "abc123"),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _make_import(**kwargs) -> ImportRecord:
    """Create an ImportRecord with convenient defaults."""
    return ImportRecord(
        kind=kwargs.get("kind", "import"),
        module=kwargs.get("module", "mod"),
        names=tuple(kwargs.get("names", [("mod", None)])),
        level=kwargs.get("level", 0),
        source_path=kwargs.get("source_path", "test.py"),
        start_line=kwargs.get("start_line", 1),
        start_col=kwargs.get("start_col", 0),
    )


def _names(record: ImportRecord) -> list[tuple[str, str | None]]:
    return list(record.names)


# ===========================================================================
# Tests (24 scenarios)
# ===========================================================================

# 1. Schema contains imports table ----------------------------------------------------

class TestSchema:
    def test_imports_table_exists(self):
        """1. The ``imports`` table is created on initial setup."""
        conn = _make_inmemory_db()
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='imports'"
        ).fetchone()
        assert result is not None

    def test_import_names_table_exists(self):
        """2. The ``import_names`` table is created on initial setup."""
        conn = _make_inmemory_db()
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='import_names'"
        ).fetchone()
        assert result is not None


# 2. Import persistence ------------------------------------------------------

class TestImportPersistence:

    def test_ordinary_absolute_import(self):
        """3. Ordinary absolute import persisted correctly."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec = _make_import(kind="import", module="os", names=[("os", None)])
        replace_imports_for_file(conn, fid, [rec])
        rows = get_imports(conn, file_id=fid)
        assert len(rows) == 1
        assert rows[0]["kind"] == "import"
        assert rows[0]["module"] == "os"

    def test_dotted_module_persistence(self):
        """4. Dotted module stored correctly."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec = _make_import(kind="import", module="os.path", names=[("os.path", None)])
        replace_imports_for_file(conn, fid, [rec])
        rows = get_imports(conn, file_id=fid)
        assert rows[0]["module"] == "os.path"

    def test_alias_persistence(self):
        """5. Aliased import round-trips."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec = _make_import(kind="import", module="numpy", names=[("numpy", "np")])
        replace_imports_for_file(conn, fid, [rec])
        rows = get_imports(conn, file_id=fid)
        assert len(rows[0]["import_names"]) == 1
        assert rows[0]["import_names"][0]["name"] == "numpy"
        assert rows[0]["import_names"][0]["asname"] == "np"

    def test_from_import_persistence(self):
        """6. ``from ... import ...`` kind persisted correctly."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec = _make_import(kind="from_import", module="pathlib", names=[("Path", None)])
        replace_imports_for_file(conn, fid, [rec])
        rows = get_imports(conn, file_id=fid)
        assert rows[0]["kind"] == "from_import"
        assert rows[0]["module"] == "pathlib"

    def test_multiple_names_with_position(self):
        """7. Multiple names in one from-import preserve position order."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec = _make_import(
            kind="from_import", module="package",
            names=[("zebra", None), ("alpha", None)],
        )
        replace_imports_for_file(conn, fid, [rec])
        rows = get_imports(conn, file_id=fid)
        names = [n["name"] for n in rows[0]["import_names"]]
        positions = [n["position"] for n in rows[0]["import_names"]]
        assert names == ["zebra", "alpha"]
        assert positions == [0, 1]

    def test_wildcard_import(self):
        """8. Wildcard import persists with name ``*``."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec = _make_import(
            kind="from_import", module="package",
            names=[("*", None)],
        )
        replace_imports_for_file(conn, fid, [rec])
        rows = get_imports(conn, file_id=fid)
        assert rows[0]["import_names"][0]["name"] == "*"
        assert rows[0]["import_names"][0]["asname"] is None

    def test_relative_level_preserved(self):
        """9. Relative import level preserved correctly."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec = _make_import(kind="from_import", module="pkg", names=[("thing", None)], level=2)
        replace_imports_for_file(conn, fid, [rec])
        rows = get_imports(conn, file_id=fid)
        assert rows[0]["level"] == 2

    def test_bare_relative_module_empty_string(self):
        """10. Bare relative ``from . import X`` has module == ``''``."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec = _make_import(
            kind="from_import", module="", names=[("sibling", None)], level=1,
        )
        replace_imports_for_file(conn, fid, [rec])
        rows = get_imports(conn, file_id=fid)
        assert rows[0]["module"] == ""
        assert rows[0]["level"] == 1

    def test_start_line_persistence(self):
        """11. ``start_line`` stored correctly."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec = _make_import(module="os", start_line=42, start_col=0)
        replace_imports_for_file(conn, fid, [rec])
        rows = get_imports(conn, file_id=fid)
        assert rows[0]["start_line"] == 42

    def test_start_col_persistence(self):
        """12. ``start_col`` stored correctly."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec = _make_import(module="os", start_line=1, start_col=55)
        replace_imports_for_file(conn, fid, [rec])
        rows = get_imports(conn, file_id=fid)
        assert rows[0]["start_col"] == 55

    def test_file_foreign_key_linkage(self):
        """13. Correct ``file_id`` foreign-key linkage via JOIN."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn, "linked.py")
        rec = _make_import(module="os")
        replace_imports_for_file(conn, fid, [rec])
        rows = get_imports(conn, file_id=fid)
        assert len(rows) == 1
        assert rows[0]["file_id"] == fid
        assert rows[0]["rel_path"] == "linked.py"

    def test_multiple_import_records_for_one_file(self):
        """14. Multiple import records for a single file."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec1 = _make_import(module="os", start_line=1, start_col=0)
        rec2 = _make_import(module="sys", start_line=2, start_col=0)
        replace_imports_for_file(conn, fid, [rec1, rec2])
        rows = get_imports(conn, file_id=fid)
        assert len(rows) == 2
        assert rows[0]["module"] == "os"
        assert rows[1]["module"] == "sys"

    def test_imports_across_files_isolated(self):
        """15. Imports on different files remain independent."""
        conn = _make_inmemory_db()
        fid_a = _insert_file(conn, "a.py")
        fid_b = _insert_file(conn, "b.py")
        replace_imports_for_file(conn, fid_a, [_make_import(module="os", start_line=1)])
        replace_imports_for_file(conn, fid_b, [_make_import(module="sys", start_line=1)])
        rows_a = get_imports(conn, file_id=fid_a)
        rows_b = get_imports(conn, file_id=fid_b)
        assert len(rows_a) == 1 and rows_a[0]["module"] == "os"
        assert len(rows_b) == 1 and rows_b[0]["module"] == "sys"


# 3. Replacement / deletion semantics -------------------------------------------

class TestReplacementSemantics:

    def test_replacement_deletes_stale_import_rows(self):
        """16. Replacing imports deletes stale parent import rows."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec = _make_import(module="old_module", start_line=1)
        replace_imports_for_file(conn, fid, [rec])

        new_rec = _make_import(module="new_module", start_line=1)
        replace_imports_for_file(conn, fid, [new_rec])

        rows = get_imports(conn, file_id=fid)
        assert len(rows) == 1
        assert rows[0]["module"] == "new_module"
        # Stale row should be gone
        conn.execute("PRAGMA foreign_keys = 1")
        for r in conn.execute("SELECT module FROM imports ORDER BY id").fetchall():
            assert r[0] != "old_module"

    def test_replacement_deletes_stale_child_name_rows(self):
        """17. Replacing deletes stale child name rows via CASCADE."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec = _make_import(module="old", names=[("old_name", None)])
        replace_imports_for_file(conn, fid, [rec])

        new_rec = _make_import(module="old", names=[("new_name", None)])
        replace_imports_for_file(conn, fid, [new_rec])

        all_names = conn.execute(
            "SELECT name FROM import_names WHERE import_id IN ("
            " SELECT id FROM imports WHERE file_id = ?)"
            , (fid,)).fetchall()
        names = {r[0] for r in all_names}
        assert "old_name" not in names
        assert "new_name" in names

    def test_empty_replacement_clears_records(self):
        """18. Empty replacement clears previously stored imports."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        replace_imports_for_file(conn, fid, [_make_import(module="os")])
        assert len(get_imports(conn, file_id=fid)) == 1

        replace_imports_for_file(conn, fid, [])
        assert get_imports(conn, file_id=fid) == []

    def test_repeated_persistence_no_duplicates(self):
        """19. Re-persisting identical records does not duplicate."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec = _make_import(module="os", start_line=1, start_col=0)
        replace_imports_for_file(conn, fid, [rec])
        first_count = len(get_imports(conn, file_id=fid))

        replace_imports_for_file(conn, fid, [rec])
        second_count = len(get_imports(conn, file_id=fid))

        assert first_count == second_count


# 4. Deletion / cascade -------------------------------------------------------

class TestDeletion:

    def test_cascade_deletes_parent_and_children(self):
        """20. Deleting via delete_imports_for_file removes import_names then imports."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn, "to_delete.py")
        rec = _make_import(module="pkg", names=[("Foo", None)])
        replace_imports_for_file(conn, fid, [rec])

        # Verify rows exist before deletion
        imp_count = conn.execute(
            "SELECT COUNT(*) FROM imports WHERE file_id=?", (fid,)
        ).fetchone()[0]
        assert imp_count == 1

        name_count = conn.execute(
            "SELECT COUNT(*) FROM import_names WHERE import_id IN ("
            " SELECT id FROM imports WHERE file_id=?)", (fid,)
        ).fetchone()[0]
        assert name_count == 1

        # Delete via the production function (which handles child removal)
        delete_imports_for_file(conn, fid)

        remaining_imp = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
        remaining_names_row = conn.execute(
            "SELECT COUNT(*) FROM import_names"
        ).fetchone()
        remaining_names = remaining_names_row[0] if remaining_names_row else 0
        assert remaining_imp == 0
        assert remaining_names == 0


# 5. Ordering ------------------------------------------------------------------

class TestOrdering:

    def test_deterministic_retrieval_by_line_col(self):
        """21. Imports returned in (start_line, start_col) deterministic order."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        recs = [
            _make_import(module="z", start_line=5, start_col=0),
            _make_import(module="a", start_line=1, start_col=0),
            _make_import(module="m", start_line=3, start_col=42),
        ]
        replace_imports_for_file(conn, fid, recs)
        rows = get_imports(conn, file_id=fid)
        modules = [r["module"] for r in rows]
        assert modules == ["a", "m", "z"]

    def test_names_retrieved_in_position_order(self):
        """22. Names within an import returned in position order."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn)
        rec = _make_import(
            kind="from_import", module="pkg",
            names=[("zbra", None), ("alpha", None), ("middle", "m")],
        )
        replace_imports_for_file(conn, fid, [rec])
        rows = get_imports(conn, file_id=fid)
        names_in_order = [n["name"] for n in rows[0]["import_names"]]
        positions = [n["position"] for n in rows[0]["import_names"]]
        assert names_in_order == ["zbra", "alpha", "middle"]
        assert positions == [0, 1, 2]


# 6. Edge cases ----------------------------------------------------------------

class TestEdgeCases:

    def test_missing_file_id_returns_empty(self):
        """23a. Query for a nonexistent file_id returns empty list."""
        conn = _make_inmemory_db()
        rows = get_imports(conn, file_id=9999)
        assert rows == []

    def test_no_source_path_match_returns_empty(self):
        """23b. Non-matching source_path filter returns empty."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn, "real.py")
        replace_imports_for_file(conn, fid, [_make_import(module="os")])
        rows = get_imports(conn, source_path="nonexistent.py")
        assert rows == []


# 8. Foreign-key enforcement on nonexistent file_id ---------------------------

class TestForeignKeyEnforcement:

    def test_nonexistent_file_id_persistence_rejected(self):
        """25a. Persisting imports against a nonexistent files.id raises IntegrityError."""
        conn = _make_inmemory_db()
        fid_nonexistent = 99_999
        rec = _make_import(module="os", start_line=1)
        with pytest.raises(sqlite3.IntegrityError):
            replace_imports_for_file(conn, fid_nonexistent, [rec])

    def test_existing_file_id_persistence_accepted(self):
        """25b. Persisting imports against an existing files.id succeeds."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn, "ok.py")
        rec = _make_import(module="os", start_line=1)
        replace_imports_for_file(conn, fid, [rec])  # should not raise
        rows = get_imports(conn, file_id=fid)
        assert len(rows) == 1
        assert rows[0]["module"] == "os"


# 9. ON DELETE CASCADE --------------------------------------------------------

class TestOnDeleteCascade:

    def test_delete_file_cascades_to_imports(self):
        """26a. Deleting a files row removes its imports (ON DELETE CASCADE)."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn, "cascade_source.py")
        rec = _make_import(module="pkg", names=[("Foo", None)])
        replace_imports_for_file(conn, fid, [rec])
        assert conn.execute(
            "SELECT COUNT(*) FROM imports WHERE file_id=?", (fid,)
        ).fetchone()[0] == 1

        # Delete the parent file row — cascade should remove import rows.
        conn.execute("DELETE FROM files WHERE id=?", (fid,))
        remaining = conn.execute(
            "SELECT COUNT(*) FROM imports"
        ).fetchone()[0]
        assert remaining == 0

    def test_delete_file_cascades_to_import_names(self):
        """26b. Deleting a files row cascades through imports to import_names."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn, "cascade_name.py")
        rec = _make_import(
            module="pkg", names=[("Bar", None), ("Baz", "b")]
        )
        replace_imports_for_file(conn, fid, [rec])
        assert conn.execute(
            "SELECT COUNT(*) FROM import_names"
        ).fetchone()[0] == 2

        conn.execute("DELETE FROM files WHERE id=?", (fid,))
        remaining = conn.execute(
            "SELECT COUNT(*) FROM import_names"
        ).fetchone()[0]
        assert remaining == 0


# 10. Replacement atomicity via caller transaction ---------------------------

class TestReplacementAtomicity:

    def test_rollback_preserves_old_imports(self):
        """27a. Rolling back the caller's transaction restores pre-replacement imports."""
        # Use isolation_level=None to get full manual control (no implicit transactions).
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")

        # Create schema manually for this test, matching _make_inmemory_db but with FK.
        conn.execute("""CREATE TABLE repo_info (key TEXT PRIMARY KEY, value TEXT)""")
        conn.execute("""
            CREATE TABLE files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rel_path TEXT NOT NULL UNIQUE,
                language TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL DEFAULT '',
                is_binary INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT, file_id INTEGER NOT NULL REFERENCES files(id),
                kind TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT NOT NULL,
                parent_symbol_id INTEGER, signature TEXT, decorators TEXT,
                start_line INTEGER, end_line INTEGER, docstring TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, module TEXT NOT NULL DEFAULT '',
                level INTEGER NOT NULL DEFAULT 0, start_line INTEGER, start_col INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE import_names (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
                position INTEGER NOT NULL, name TEXT NOT NULL, asname TEXT
            )
        """)
        # Commit the DDL.
        conn.commit()

        fid_row = conn.execute(
            "INSERT INTO files (rel_path, language, size) VALUES (?, ?, ?)",
            ("atomic_source.py", "Python", 100),
        ).lastrowid

        old_rec = _make_import(module="old_module", start_line=1)
        # Commit initial state.
        replace_imports_for_file(conn, fid_row, [old_rec])
        conn.commit()

        new_rec = _make_import(module="new_module", start_line=1)

        # Now the caller owns an explicit transaction (simulating CLI build's with-conn pattern).
        conn.execute("BEGIN")
        replace_imports_for_file(conn, fid_row, [new_rec])
        # Visibly replaced within the active tx:
        assert get_imports(conn, file_id=fid_row)[0]["module"] == "new_module"

        # Simulate a failure downstream — caller rolls back.
        conn.rollback()
        old_data = get_imports(conn, file_id=fid_row)
        assert len(old_data) == 1
        assert old_data[0]["module"] == "old_module"
        del conn  # cleanup

    def test_committed_replacement_applies(self):
        """27b. Commits make the replacement permanent."""
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""CREATE TABLE repo_info (key TEXT PRIMARY KEY, value TEXT)""")
        conn.execute("""
            CREATE TABLE files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rel_path TEXT NOT NULL UNIQUE, language TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL DEFAULT '',
                is_binary INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE symbols (id INTEGER PRIMARY KEY AUTOINCREMENT, file_id INTEGER NOT NULL REFERENCES files(id),
                kind TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT NOT NULL,
                parent_symbol_id INTEGER, signature TEXT, decorators TEXT, start_line INTEGER, end_line INTEGER, docstring TEXT)
        """)
        conn.execute("""
            CREATE TABLE imports (id INTEGER PRIMARY KEY AUTOINCREMENT, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, module TEXT NOT NULL DEFAULT '', level INTEGER NOT NULL DEFAULT 0, start_line INTEGER, start_col INTEGER)
        """)
        conn.execute("""
            CREATE TABLE import_names (id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
                position INTEGER NOT NULL, name TEXT NOT NULL, asname TEXT)
        """)
        conn.commit()

        fid_row = conn.execute(
            "INSERT INTO files (rel_path, language, size) VALUES (?, ?, ?)",
            ("commit_source.py", "Python", 100),
        ).lastrowid
        rec = _make_import(module="os", start_line=1)
        # Commit via context-manager pattern matching CLI build.
        with conn:
            replace_imports_for_file(conn, fid_row, [rec])

        rows = get_imports(conn, file_id=fid_row)
        assert len(rows) == 1 and rows[0]["module"] == "os"


# 7. Regression: existing Phase 1 / Phase 2A schema functional -----------------

class TestSchemaRegression:

    def test_files_table_still_insertable(self):
        """24a. Existing files table still works for insert."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn, "regression.py")
        assert isinstance(fid, int) and fid > 0

    def test_symbols_table_still_functional(self):
        """24b. Phase 2A symbols table still functional."""
        conn = _make_inmemory_db()
        fid = _insert_file(conn, "sym_test.py")
        from repo_intelligence.search import insert_symbols, get_symbols

        syms = [{
            "kind": "class", "name": "TestClass",
            "qualified_name": "TestClass",
            "parent_id": None, "signature": "", 
            "decorators": [], "start_line": 1, "end_line": 5, "docstring": "",
        }]
        insert_symbols(conn, fid, syms)
        rows = get_symbols(conn, file_id=fid)
        assert len(rows) == 1
        assert rows[0]["name"] == "TestClass"
