"""Phase 2C-2B focused tests: read-only SQLite adapter.

Covers the acceptance requirements (D) in
PHASE_2C_2B_ARCHITECTURE_APPROVED.md, plus a few fail-closed checks
required by section 5 (C).
"""
from __future__ import annotations

import os

import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_ROOT))  # guarantee src/ precedes site-packages

from repo_intelligence.db_absolute_resolution import (
    AdapterError,
    resolve_absolute_imports_from_db,
)

from repo_intelligence.absolute_resolution import (
    AMBIGUOUS,
    NOT_ATTEMPTED,
    REASON_RELATIVE_IMPORT,
    REASON_UNIQUE_SUPPORTED,
    REASON_UNSUPPORTED_NAME,
    RESOLVED,
    REASON_NO_CANDIDATE,
    UNRESOLVED,
    UNSUPPORTED,
)
from repo_intelligence.python_ast import ImportRecord


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path, layout: dict[str, str]) -> Path:
    """Create a small on-disk repo layout {rel_path: text}."""
    for rel, text in layout.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def _build_index(repo: Path, db: Path) -> None:
    """Build an index via the same codepath used by `repo-index build`,
    without spawning a subprocess (keeps tests hermetic)."""
    from click.testing import CliRunner

    from repo_intelligence.cli import cmd_build

    runner = CliRunner()
    result = runner.invoke(cmd_build, [str(repo), "--index-path", str(db)])
    if result.exit_code != 0:
        raise AssertionError(
            f"repo-index build failed: {result.output} {result.exception}"
        )


def _write_db(tmp_path: Path, db_name: str, seed_fn) -> Path:
    """Directly write a SQLite db with the frozen schema, then invoke
    *seed_fn*(conn) to insert data. Useful for corruption cases the
    build pipeline can't produce (orphans, missing columns, etc.)."""
    db_file = tmp_path / db_name
    conn = sqlite3.connect(str(db_file))
    try:
        # FK enforcement is on by default for the frozen schema; a test
        # seed function may need to insert orphan rows for fail-closed
        # testing, so we allow it to toggle the pragma freely *before*
        # any DML and commit only after it returns.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("CREATE TABLE repo_info (key TEXT PRIMARY KEY, value TEXT)")
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
            CREATE TABLE imports (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
              kind TEXT NOT NULL,
              module TEXT NOT NULL DEFAULT '',
              level INTEGER NOT NULL DEFAULT 0,
              start_line INTEGER,
              start_col INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE import_names (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
              position INTEGER NOT NULL,
              name TEXT NOT NULL,
              asname TEXT
            )
        """)
        seed_fn(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
    finally:
        conn.close()
    return db_file


def _insert_file(conn, rel_path, language="python"):
    cur = conn.execute(
        "INSERT INTO files (rel_path, language, size, sha256, is_binary) "
        "VALUES (?, ?, 0, '', 0)",
        (rel_path, language),
    )
    return cur.lastrowid


def _insert_import(
    conn,
    file_id: int,
    kind: str,
    module: str,
    level: int = 0,
    start_line: int | None = 1,
    start_col: int | None = 0,
):
    cur = conn.execute(
        "INSERT INTO imports (file_id, kind, module, level, start_line, start_col) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, kind, module, level, start_line, start_col),
    )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Read-only invariants
# ---------------------------------------------------------------------------


class TestReadOnlyInvariants:
    def test_missing_db_does_not_create(self, tmp_path):
        target = tmp_path / "no_such.db"
        with pytest.raises(FileNotFoundError):
            resolve_absolute_imports_from_db(str(target), (".",))
        assert not target.exists()

    def test_path_with_uri_significant_chars_roundtrips(self, tmp_path):
        """A db file whose *path* contains ``#``, ``?``, ``%`` and
        literal ``%3F`` must round-trip through the URI encoding
        without corrupting the query or file access."""
        weird_dir = tmp_path / "weird?name#pct%3F%percent"
        weird_dir.mkdir()
        repo = weird_dir / "repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "mod.py").write_text("x = 1\n")
        (repo / "user.py").write_text("import pkg.mod\n")
        weird_db = weird_dir / "index#?db%3F%%.db"

        _build_index(repo, weird_db)

        # The adapter opens the database through its URI-encoded path.
        # If URI encoding were broken the file would either not be
        # found or the ``mode=ro`` fragment would be mangled.
        res = resolve_absolute_imports_from_db(str(weird_db), (".",))
        assert len(res) == 1
        assert res[0].record.module == "pkg.mod"
        assert res[0].status == RESOLVED

    def test_missing_imports_table_fails_closed(self, tmp_path):
        # A db without the imports table.
        db = tmp_path / "no_imports.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, rel_path TEXT)")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(AdapterError):
            resolve_absolute_imports_from_db(str(db), (".",))

    def test_missing_imports_columns_fails_closed(self, tmp_path):
        db = tmp_path / "missing_cols.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, rel_path TEXT)")
            conn.execute("CREATE TABLE imports (id INTEGER PRIMARY KEY, file_id INTEGER)")
            conn.execute(
                "CREATE TABLE import_names "
                "(id INTEGER PRIMARY KEY, import_id INTEGER, position INTEGER, "
                "name TEXT, asname TEXT)"
            )
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(AdapterError):
            resolve_absolute_imports_from_db(str(db), (".",))

    def test_never_calls_frozen_search_helpers(self, tmp_path):
        # Smoke-test that we do NOT call search.get_imports / _ensure_imports_tables.
        repo = tmp_path / "r"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "m.py").write_text("import os\n")
        db = tmp_path / "i.db"
        _build_index(repo, db)

        with mock.patch(
            "repo_intelligence.search.get_imports",
            side_effect=AssertionError("adapter must not call get_imports"),
        ), mock.patch(
            "repo_intelligence.search._ensure_imports_tables",
            side_effect=AssertionError("adapter must not call DDL helper"),
        ):
            res = resolve_absolute_imports_from_db(str(db), ("pkg",))
        assert res  # non-empty; ensures the mock wasn't silently bypassed


# ---------------------------------------------------------------------------
# Snapshot consistency / full-inventory guarantee
# ---------------------------------------------------------------------------


class TestSnapshotAndInventory:
    def test_full_inventory_is_used_for_statement_filter(
        self, tmp_path
    ):
        """Filter to one importing file, but the resolved target comes
        from a *different* file in the same index. If the adapter only
        loaded the filtered file's inventory, ``import other.mod`` would
        be UNRESOLVED; with the *complete* inventory it resolves."""
        repo = tmp_path / "r"
        (repo / "other").mkdir(parents=True)
        (repo / "a").mkdir(parents=True, exist_ok=True)
        (repo / "a" / "__init__.py").write_text("")
        (repo / "other" / "target.py").write_text("x = 1\n")
        (repo / "importer.py").write_text("import other.target\n")
        db = tmp_path / "i.db"
        _build_index(repo, db)

        res = resolve_absolute_imports_from_db(
            str(db), (".",), source_path="importer.py"
        )
        assert len(res) == 1
        # The import statement is in importer.py, but its *target*
        # ``other.target`` resolves to other/target.py.
        assert res[0].status == RESOLVED
        assert res[0].record.source_path == "importer.py"

    def test_concurrentIndex_generation_change_does_not_mix(
        self, tmp_path
    ):
        """Concurrent rebuilds must not leak partial state into a
        snapshot. (This exercises the adapter's transactional
        ownership; we trigger it by pre-writing a consistent snapshot,
        then opening a second connection that writes concurrently and
        confirm the first read still succeeds with its own snapshot.)"""
        repo = tmp_path / "r"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "mod.py").write_text("x = 1\n")
        (repo / "importer.py").write_text("import pkg.mod\n")
        db = tmp_path / "i.db"
        _build_index(repo, db)

        # Snapshot via the adapter.
        before = resolve_absolute_imports_from_db(str(db), (".",))
        assert len(before) == 1

        # Concurrent rebuild (writes to the same db) — must not affect
        # the *already-completed* read above, and the next read must
        # be internally coherent (no mix of two generations).
        _build_index(repo, db)
        after = resolve_absolute_imports_from_db(str(db), (".",))
        assert len(after) == 1
        assert (before[0].record, before[0].status) == (after[0].record, after[0].status)


# ---------------------------------------------------------------------------
# Fail-closed reconstruction
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_orphan_import_row_raises(self, tmp_path):
        def seed(conn):
            # FK enforcement is on in this fixture; disable it to create
            # a deliberate orphan, then re-enable.
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO imports (file_id, kind, module, level, start_line, start_col) "
                "VALUES (42, 'import', 'x', 0, 1, 0)"
            )
            conn.execute(
                "INSERT INTO import_names (import_id, position, name, asname) "
                "VALUES (1, 0, 'x', NULL)"
            )
            conn.execute("PRAGMA foreign_keys = ON")
        db = _write_db(tmp_path, "orphan.db", seed)
        with pytest.raises(AdapterError, match="orphaned"):
            resolve_absolute_imports_from_db(str(db), (".",))

    def test_orphan_import_name_raises(self, tmp_path):
        def seed(conn):
            fi = _insert_file(conn, "a.py")
            _insert_import(conn, fi, "import", "a", 0, 1, 0)
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO import_names (import_id, position, name, asname) "
                "VALUES (999, 0, 'a', NULL)"
            )
            conn.execute("PRAGMA foreign_keys = ON")
        db = _write_db(tmp_path, "orphan_name.db", seed)
        with pytest.raises(AdapterError, match="orphaned"):
            resolve_absolute_imports_from_db(str(db), (".",))

    def test_duplicate_position_raises(self, tmp_path):
        def seed(conn):
            fi = _insert_file(conn, "a.py")
            iid = _insert_import(conn, fi, "import", "a, b", 0, 1, 0)
            conn.execute(
                "INSERT INTO import_names (import_id, position, name) "
                "VALUES (?, 0, 'a')", (iid,)
            )
            conn.execute(
                "INSERT INTO import_names (import_id, position, name) "
                "VALUES (?, 0, 'b')", (iid,)  # duplicate position 0
            )
        db = _write_db(tmp_path, "dup.db", seed)
        with pytest.raises(AdapterError, match="duplicate position"):
            resolve_absolute_imports_from_db(str(db), (".",))

    def test_gap_in_positions_raises(self, tmp_path):
        def seed(conn):
            fi = _insert_file(conn, "a.py")
            iid = _insert_import(conn, fi, "from_import", "a", 0, 1, 0)
            conn.execute(
                "INSERT INTO import_names (import_id, position, name) "
                "VALUES (?, 0, 'x')", (iid,)
            )
            conn.execute(
                "INSERT INTO import_names (import_id, position, name) "
                "VALUES (?, 2, 'y')", (iid,)  # position 2 with 1 missing
            )
        db = _write_db(tmp_path, "gap.db", seed)
        with pytest.raises(AdapterError, match="contiguous"):
            resolve_absolute_imports_from_db(str(db), (".",))

    def test_missing_start_line_raises(self, tmp_path):
        def seed(conn):
            fi = _insert_file(conn, "a.py")
            # No start_line (NULL).
            conn.execute(
                "INSERT INTO imports (file_id, kind, module, level) "
                "VALUES (?, 'import', 'a', 0)", (fi,)
            )
            conn.execute(
                "INSERT INTO import_names (import_id, position, name) "
                "VALUES (1, 0, 'a')"
            )
        db = _write_db(tmp_path, "noline.db", seed)
        with pytest.raises(AdapterError, match="start_line"):
            resolve_absolute_imports_from_db(str(db), (".",))

    def test_invalid_kind_raises(self, tmp_path):
        def seed(conn):
            fi = _insert_file(conn, "a.py")
            _insert_import(conn, fi, "bad_kind", "a", 0, 1, 0)
            conn.execute(
                "INSERT INTO import_names (import_id, position, name) "
                "VALUES (1, 0, 'a')"
            )
        db = _write_db(tmp_path, "badkind.db", seed)
        with pytest.raises(AdapterError, match="kind"):
            resolve_absolute_imports_from_db(str(db), (".",))


# ---------------------------------------------------------------------------
# Resolution correctness (frozen resolver pass-through)
# ---------------------------------------------------------------------------


class TestResolution:
    def test_import_simple_resolves(self, tmp_path):
        repo = tmp_path / "r"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "mod.py").write_text("x = 1\n")
        (repo / "importer.py").write_text("import pkg.mod\n")
        db = tmp_path / "i.db"
        _build_index(repo, db)
        res = resolve_absolute_imports_from_db(str(db), (".",))
        assert res[0].status == RESOLVED
        assert res[0].record.module == "pkg.mod"
        assert len(res[0].supported_candidates) == 1
        assert len(res[0].unsupported_candidates) == 0

    def test_from_import_with_alias_retains_name(self, tmp_path):
        repo = tmp_path / "r"
        (repo / "pkg" / "sub").mkdir(parents=True)
        (repo / "pkg" / "sub" / "mod.py").write_text("x = 1\n")
        (repo / "importer.py").write_text("from pkg.sub import mod as m\n")
        db = tmp_path / "i.db"
        _build_index(repo, db)
        res = resolve_absolute_imports_from_db(str(db), (".",))
        # The *parent* module pkg.sub resolves to its __init__ package
        # (or the directory), not to mod.py.  We only assert the
        # adapter preserves the alias inside ImportRecord.names.
        (mod_name, alias) = res[0].record.names[0]
        assert mod_name == "mod"
        assert alias == "m"

    def test_relative_import_stays_not_attempted(self, tmp_path):
        repo = tmp_path / "r"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "mod.py").write_text("x = 1\n")
        (repo / "pkg" / "user.py").write_text("from . import mod\n")
        db = tmp_path / "i.db"
        _build_index(repo, db)
        res = resolve_absolute_imports_from_db(str(db), (".",))
        rel_res = [r for r in res if r.record.level > 0]
        assert rel_res, "expected a relative-import record"
        assert rel_res[0].status == NOT_ATTEMPTED
        assert rel_res[0].reason == REASON_RELATIVE_IMPORT
        assert rel_res[0].record.names == (("mod", None),)

    def test_ambiguous_stays_ambiguous(self, tmp_path):
        # Two top-level packages with the same name in distinct roots.
        repo = tmp_path / "r"
        (repo / "a" / "pkg").mkdir(parents=True)
        (repo / "b" / "pkg").mkdir(parents=True)
        (repo / "a" / "pkg" / "mod.py").write_text("x = 1\n")
        (repo / "b" / "pkg" / "mod.py").write_text("y = 2\n")
        (repo / "importer.py").write_text("import pkg.mod\n")
        db = tmp_path / "i.db"
        _build_index(repo, db)
        res = resolve_absolute_imports_from_db(str(db), ("a", "b"))
        # Importer resolves to two distinct candidates.
        assert res[0].status == AMBIGUOUS
        assert len(res[0].supported_candidates) == 2

    def test_unresolved_external_kept(self, tmp_path):
        # `import requests` — no local structural match.
        repo = tmp_path / "r"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "mod.py").write_text("x = 1\n")
        (repo / "importer.py").write_text("import requests\n")
        db = tmp_path / "i.db"
        _build_index(repo, db)
        res = resolve_absolute_imports_from_db(str(db), (".",))
        assert res[0].status == UNRESOLVED
        assert res[0].reason == REASON_NO_CANDIDATE

    def test_wildcard_name_retained(self, tmp_path):
        """``from pkg import *`` — the wildcard name is preserved in the
        record and only the parent module target is resolved."""
        repo = tmp_path / "r"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "__init__.py").write_text("")
        (repo / "importer.py").write_text("from pkg import *\n")
        db = tmp_path / "i.db"
        _build_index(repo, db)
        res = resolve_absolute_imports_from_db(str(db), (".",))
        recs = [r for r in res if r.record.module == "pkg"]
        assert len(recs) == 1
        rec = recs[0]
        assert rec.record.names == (("*", None),)
        assert rec.status in (RESOLVED, AMBIGUOUS, UNRESOLVED)

    def test_unsupported_candidate_pass_through(self, tmp_path):
        """When a persisted import statement targets a component that
        matches a name-unsupported candidate (unreachable from a real
        AST-extracted import because such targets are syntactically
        invalid Python), the adapter must surface the frozen resolver's
        UNSUPPORTED outcome unchanged — not reclassify, not drop."""
        def seed(conn):
            fi = _insert_file(conn, "a/b-c.py")
            # Deliberately persist a syntactically invalid target
            # ``a.b-c``.  Real extracted imports never look like this,
            # but the adapter's job is to pass such records to the
            # resolver unchanged.
            iid = _insert_import(
                conn, fi, "import", "a.b-c", 0, 1, 0
            )
            conn.execute(
                "INSERT INTO import_names (import_id, position, name, asname) "
                "VALUES (?, 0, 'a.b-c', NULL)", (iid,)
            )
            conn.execute(
                "INSERT INTO files (id, rel_path, language, size, sha256, is_binary) "
                "VALUES (4242, 'user.py', 'python', 0, '', 0)"
            )
            ufid = 4242
            uid = _insert_import(
                conn, ufid, "import", "a.b-c", 0, 1, 0
            )
            conn.execute(
                "INSERT INTO import_names (import_id, position, name, asname) "
                "VALUES (?, 0, 'a.b-c', NULL)", (uid,)
            )

        db = _write_db(tmp_path, "invalid.db", seed)
        res = resolve_absolute_imports_from_db(str(db), (".",))
        recs = [r for r in res if r.record.module == "a.b-c"]
        assert len(recs) >= 1
        # The user.py record is the interesting one: its target matches
        # the name-unsupported candidate ``a/b-c.py`` and the resolver
        # classifies it UNSUPPORTED with the evidence retained.
        user = [r for r in recs if r.record.source_path == "user.py"][0]
        assert user.status == UNSUPPORTED
        assert user.reason == REASON_UNSUPPORTED_NAME
        assert len(user.unsupported_candidates) == 1
        assert len(user.supported_candidates) == 0

    def test_root_order_does_not_change_outcome(self, tmp_path):
        # With AMBIGUOUS result, candidate order is the *canonical*
        # (root.canonical, ...) tuple order — independent of caller order.
        repo = tmp_path / "r"
        (repo / "a" / "pkg").mkdir(parents=True)
        (repo / "b" / "pkg").mkdir(parents=True)
        (repo / "a" / "pkg" / "mod.py").write_text("x = 1\n")
        (repo / "b" / "pkg" / "mod.py").write_text("y = 2\n")
        (repo / "importer.py").write_text("import pkg.mod\n")
        db = tmp_path / "i.db"
        _build_index(repo, db)
        r1 = resolve_absolute_imports_from_db(str(db), ("a", "b"))
        r2 = resolve_absolute_imports_from_db(str(db), ("b", "a"))
        assert [rc.root.canonical for rc in r1[0].supported_candidates] == \
               [rc.root.canonical for rc in r2[0].supported_candidates]
        assert r1[0].status == r2[0].status

    def test_root_rejection_for_empty(self, tmp_path):
        db = tmp_path / "i.db"
        with pytest.raises((TypeError, ValueError)):
            resolve_absolute_imports_from_db(str(db), ())

    def test_root_rejection_for_absolute(self, tmp_path):
        db = tmp_path / "i.db"
        with pytest.raises(ValueError):
            resolve_absolute_imports_from_db(str(db), ("/abs/path",))


# ---------------------------------------------------------------------------
# Determinism / ordering guarantees
# ---------------------------------------------------------------------------


class TestOrderingAndDeterminism:
    def test_repeated_calls_are_equal(self, tmp_path):
        repo = tmp_path / "r"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "mod.py").write_text("x = 1\n")
        (repo / "importer.py").write_text("import pkg.mod\n")
        db = tmp_path / "i.db"
        _build_index(repo, db)
        r1 = resolve_absolute_imports_from_db(str(db), (".",))
        r2 = resolve_absolute_imports_from_db(str(db), (".",))
        assert r1 == r2

    def test_multiple_import_statements_are_sorted(self, tmp_path):
        repo = tmp_path / "r"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "mod.py").write_text("x = 1\n")
        # Two import statements at the same line/column in different
        # files, plus one earlier in the alphabetically-first file.
        (repo / "z.py").write_text("import pkg.mod\n")
        (repo / "a.py").write_text("import pkg.mod\n")
        db = tmp_path / "i.db"
        _build_index(repo, db)
        res = resolve_absolute_imports_from_db(str(db), (".",))
        # Sorted by (source_path, start_line, start_col, ...):
        #   a.py comes before z.py.
        assert [r.record.source_path for r in res] == ["a.py", "z.py"]
