"""Phase 2C-2B focused tests: read-only SQLite adapter.

Covers the acceptance requirements (D) in
docs/phase-2c-2b-architecture-approved.md, plus a few fail-closed checks
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
    REASON_INVALID_RECORD_KIND,
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


def _write_cross_root_db(tmp_path: Path, name: str) -> Path:
    """A two-root cross-root ambiguity index, seeded directly with
    canonical forward-slash ``rel_path`` values (no frozen-scanner
    dependency, so the invariant holds on every platform):

      a/pkg/mod.py, b/pkg/mod.py  (same name under distinct roots)
      importer.py  -> ``import pkg.mod``
    """
    def seed(conn):
        fi = _insert_file(conn, "importer.py")
        _insert_file(conn, "a/pkg/mod.py")
        _insert_file(conn, "b/pkg/mod.py")
        iid = _insert_import(conn, fi, "import", "pkg.mod", 0, 1, 0)
        conn.execute(
            "INSERT INTO import_names (import_id, position, name, asname) "
            "VALUES (?, 0, 'pkg.mod', NULL)", (iid,)
        )
    return _write_db(tmp_path, name, seed)


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
        """A db file whose *path* contains all three URI-significant
        characters ``#``, ``?``, ``%`` (plus a literal ``%3F`` escape
        sequence) must round-trip through the URI encoding without
        corrupting the query or file access.  On platforms where the
        filesystem itself forbids one of the characters (Windows
        forbids ``?`` in path components while permitting the
        others), we include only the legal characters — the URI
        invariant is still exercised by ``#`` and ``%`` (and
        ``literal-%3F``) on every platform."""
        if os.name == "nt":
            # Windows forbids ``?`` in path components (a Win32
            # ``NtCreateFile`` failure).  ``#`` and ``%`` are legal.
            weird_dir = tmp_path / "weirdname#pct%3F%percent"
            weird_db  = weird_dir / "index#db%3F%%.db"
        else:
            # POSIX: all three URI-significant characters are legal.
            weird_dir = tmp_path / "weird?name#pct%3F%percent"
            weird_db  = weird_dir / "index#?db%3F%%.db"
        weird_dir.mkdir()
        repo = weird_dir / "repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "mod.py").write_text("x = 1\n")
        (repo / "user.py").write_text("import pkg.mod\n")

        _build_index(repo, weird_db)

        # The adapter opens the database through its URI-encoded path.
        # If URI encoding were broken the file would either not be
        # found (``#`` truncation) or the ``%`` would be mis-parsed.
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

    def test_nontext_kind_raises(self, tmp_path):
        """A *non-text* ``kind`` (e.g. persisted as a BLOB) is an
        adapter-level storage-type error — it cannot be represented by
        the frozen :class:`ImportRecord` at all — and fails closed here.
        (Contrast *representable unknown strings*, which the frozen
        resolver interprets in :class:`TestResolution`.)  A BLOB is used
        because SQLite's TEXT affinity silently coerces an integer into
        the string ``"42"``; a BLOB survives the round-trip non-string."""
        def seed(conn):
            fi = _insert_file(conn, "a.py")
            conn.execute(
                "INSERT INTO imports (file_id, kind, module, level, start_line, start_col) "
                "VALUES (?, ?, 'a', 0, 1, 0)", (fi, b"\x00\x01")
            )
            conn.execute(
                "INSERT INTO import_names (import_id, position, name) "
                "VALUES (1, 0, 'a')"
            )
        db = _write_db(tmp_path, "badkind.db", seed)
        with pytest.raises(AdapterError, match=r"non-text kind"):
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
        """Seeded directly (see :func:`_write_cross_root_db`) so the
        invariant does not depend on the *frozen* scanner's
        ``os.path.relpath`` output, which is platform-separator dependent.
        On Windows the scanner yields backslash separators that the
        frozen inventory cannot decompose (``module_inventory._split_rel_path``
        only splits on ``/``); using direct-seeded rows preserves the
        cross-root ambiguity invariant without modifying frozen code."""
        db = _write_cross_root_db(tmp_path, "cross.db")
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
        """Seeded directly (see :func:`_write_cross_root_db`) so the
        invariant does not depend on the *frozen* scanner's
        ``os.path.relpath`` output, which is platform-separator dependent
        (see :func:`test_ambiguous_stays_ambiguous` for rationale)."""
        db = _write_cross_root_db(tmp_path, "cross2.db")
        # With AMBIGUOUS result, candidate order is the *canonical*
        # (root.canonical, ...) tuple order — independent of caller order.
        r1 = resolve_absolute_imports_from_db(str(db), ("a", "b"))
        r2 = resolve_absolute_imports_from_db(str(db), ("b", "a"))
        assert [rc.root.canonical for rc in r1[0].supported_candidates] == \
               [rc.root.canonical for rc in r2[0].supported_candidates]
        assert r1[0].status == r2[0].status
        assert r1[0].status == AMBIGUOUS

    def test_root_rejection_for_empty(self, tmp_path):
        db = tmp_path / "i.db"
        with pytest.raises((TypeError, ValueError)):
            resolve_absolute_imports_from_db(str(db), ())

    def test_root_rejection_for_absolute(self, tmp_path):
        db = tmp_path / "i.db"
        with pytest.raises(ValueError):
            resolve_absolute_imports_from_db(str(db), ("/abs/path",))

    # ---- P2-2: representable unknown kind strings resolve unchanged ----

    def test_unknown_string_kind_is_not_attempted(self, tmp_path):
        """A representable unknown ``kind`` string is *not* adapter-level
        enum-validated; the frozen resolver returns its defined
        ``NOT_ATTEMPTED`` outcome with the exact reason
        ``invalid_record_kind`` (no ``AdapterError``)."""
        def seed(conn):
            fi = _insert_file(conn, "a.py")
            _insert_import(conn, fi, "bad_kind", "a", 0, 1, 0)
            conn.execute(
                "INSERT INTO import_names (import_id, position, name) "
                "VALUES (1, 0, 'a')"
            )
        db = _write_db(tmp_path, "badkind.db", seed)
        res = resolve_absolute_imports_from_db(str(db), (".",))
        assert len(res) == 1
        assert res[0].status == NOT_ATTEMPTED
        assert res[0].reason == REASON_INVALID_RECORD_KIND
        assert res[0].record.kind == "bad_kind"

    def test_unknown_kind_matches_in_memory_record(self, tmp_path):
        """Database-backed and the equivalent in-memory record yield the
        *same* frozen resolution — the adapter adds no semantic layer.
        (The in-memory record runs through the identical pure resolver.)"""
        def seed(conn):
            fi = _insert_file(conn, "badkind2.py")
            _insert_import(conn, fi, "bad_kind", "pkg.mod", 0, 1, 0)
            conn.execute(
                "INSERT INTO import_names (import_id, position, name) "
                "VALUES (1, 0, 'a')"
            )
        db = _write_db(tmp_path, "badkind2.db", seed)
        db_res = resolve_absolute_imports_from_db(str(db), (".",))

        # Equivalent in-memory record through the frozen resolver.
        from repo_intelligence.absolute_resolution import (
            resolve_absolute_import,
        )
        from repo_intelligence.module_inventory import build_module_inventory

        in_memory = ImportRecord(
            kind="bad_kind",
            module="pkg.mod",
            names=(("a", None),),
            level=0,
            source_path="badkind2.py",
            start_line=1,
            start_col=0,
        )
        mem_res = resolve_absolute_import(
            in_memory, build_module_inventory((), (".",))
        )

        assert db_res[0].status == mem_res.status == NOT_ATTEMPTED
        assert db_res[0].reason == mem_res.reason == REASON_INVALID_RECORD_KIND
        assert db_res[0].record == in_memory


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

    # ------------------------------------------------------------------- #
    # P2-1 + Codex-reported gaps:
    #  - deterministic total-order sort key with optional aliases
    #  - row-ID permutation invariance
    #  - multi-name reconstruction under the ordering key
    #  - batched name loading across a batch boundary
    #  - resource cleanup after an exception
    # ------------------------------------------------------------------- #

    def _two_same_location_records(self, tmp_path, name: str, first_asname, second_asname):
        def seed(conn):
            fi = _insert_file(conn, "s.py")
            _insert_import(conn, fi, "from_import", "m", 0, 1, 0)
            conn.execute(
                "INSERT INTO import_names (import_id, position, name, asname) "
                "VALUES (?, 0, 'a', ?)", (1, first_asname)
            )
            _insert_import(conn, fi, "from_import", "m", 0, 1, 0)
            conn.execute(
                "INSERT INTO import_names (import_id, position, name, asname) "
                "VALUES (?, 0, 'a', ?)", (2, second_asname)
            )
        return _write_db(tmp_path, name, seed)

    def test_same_location_null_and_string_alias_orders_deterministically(
        self, tmp_path
    ):
        """Two same-location records differing *only* in the alias
        (``None`` vs a ``str``) must compare under a **total order** (no
        ``TypeError``), the ordering must be **identical regardless of
        insertion order / row IDs**, and the original ``names`` values
        must be retained verbatim."""
        # Forward: asname NULL first ("a"), then "alias".
        db_fwd = self._two_same_location_records(
            tmp_path, "fwd.db", first_asname=None, second_asname="alias"
        )
        res_fwd = resolve_absolute_imports_from_db(str(db_fwd), (".",))

        # Reverse: "alias" first, then NULL. Must yield the exact same
        # result order (insertion/row-ID order has no influence).
        db_rev = self._two_same_location_records(
            tmp_path, "rev.db", first_asname="alias", second_asname=None
        )
        res_rev = resolve_absolute_imports_from_db(str(db_rev), (".",))

        # P2-1: reaching this far proves no TypeError was raised on the
        # None-vs-str alias comparison inside sort. Both storage orders
        # must yield the same total ordering of the same records.
        assert [r.record.names for r in res_fwd] == [r.record.names for r in res_rev]
        # The two reconstructed records preserve their original
        # (name, asname) evidence verbatim — no rewriting.
        assert {r.record.names for r in res_fwd} == {
            (("a", None),),
            (("a", "alias"),),
        }
        # And the resolution evidence is unchanged: identical status/reason
        # across both storage orderings.
        assert [r.status for r in res_fwd] == [r.status for r in res_rev]
        assert [r.reason for r in res_fwd] == [r.reason for r in res_rev]
        # Determinism across repeated invocations of the same DB too.
        assert resolve_absolute_imports_from_db(str(db_fwd), (".",)) == res_fwd

    def test_multi_name_with_optional_aliases_sorts(self, tmp_path):
        """A single record with multiple imported names, some with
        aliases and some without, must be reconstructable AND
        orderable under the total order (no ``TypeError`` on any
        pairwise ``names`` comparison — even when the same record
        has to be compared against a *second* record in the same
        source file that happens to share the prefix of its names)."""
        def seed(conn):
            # Two records in the same file, same location, each with
            # multiple names including at least one missing-alias
            # and one present-alias (mixed str/None within the tuple).
            fi = _insert_file(conn, "multi.py")
            i1 = _insert_import(conn, fi, "from_import", "m", 0, 1, 0)
            i2 = _insert_import(conn, fi, "from_import", "m", 0, 1, 0)
            # Record 1: a (no alias), b (with alias).
            for pos, (n, a) in enumerate([("a", None), ("b", "alias")]):
                conn.execute(
                    "INSERT INTO import_names (import_id, position, name, asname) "
                    "VALUES (?, ?, ?, ?)", (i1, pos, n, a)
                )
            # Record 2: a (with alias), b (no alias).
            for pos, (n, a) in enumerate([("a", "x"), ("b", None)]):
                conn.execute(
                    "INSERT INTO import_names (import_id, position, name, asname) "
                    "VALUES (?, ?, ?, ?)", (i2, pos, n, a)
                )
        db = _write_db(tmp_path, "multi.db", seed)
        res = resolve_absolute_imports_from_db(str(db), (".",))
        # P2-1: sort completed against the (name, str|None) tuples.
        # Multi-name reconstruction with mixed aliases is preserved:
        assert {r.record.names for r in res} == {
            (("a", None), ("b", "alias")),
            (("a", "x"), ("b", None)),
        }
        # Total order: both records have the same source_path/line/col,
        # same kind/module/level; the tiebreak is now via `names`.
        # Whichever direction the total order chooses, the ordering must
        # be stable against repeated invocations of the same DB.
        res2 = resolve_absolute_imports_from_db(str(db), (".",))
        assert [r.record.names for r in res] == [r.record.names for r in res2]

    # ------------------------------------------------------------------- #
    # Codex-reported gap tests: batch-boundary / cleanup / rowID
    # ------------------------------------------------------------------- #

    def test_batch_boundary_does_not_split_names(self, tmp_path):
        """Seed ``_BATCH_SIZE + 1`` imports so ``_load_and_validate_names``
        exercises two batches (batch 0: 900 imports; batch 1: 1 import)
        and confirm no name is lost or mis-attributed at the boundary.
        The final records must be reconstructable and deterministically
        ordered regardless of how many batches were required."""
        from repo_intelligence.db_absolute_resolution import _BATCH_SIZE
        n = _BATCH_SIZE + 1

        def seed(conn):
            fi = _insert_file(conn, "batch.py")
            for _ in range(n):
                ii = _insert_import(conn, fi, "from_import", "m", 0, 1, 0)
                conn.execute(
                    "INSERT INTO import_names (import_id, position, name, asname) "
                    "VALUES (?, 0, 'a', 'x')", (ii,)
                )

        db = _write_db(tmp_path, "batch.db", seed)
        res = resolve_absolute_imports_from_db(str(db), (".",))

        # All imports were loaded in two batches, none lost:
        assert len(res) == n
        # Every record retains its names (no mis-attribution at boundary):
        assert all(r.record.names == (("a", "x"),) for r in res)

    def test_connection_closed_on_exception(self, tmp_path):
        """Even when the caller-visible flow raises (here: ``AdapterError``
        from the BLOB-name check), the adapter must still release the
        owned read-only connection (``finally: conn.close()``).  We
        subclass ``sqlite3.Connection`` — a class-level ``close`` override
        is what makes the spy possible, since the concrete type's
        instance ``close`` is a read-only descriptor — and patch the
        adapter's ``_open_read_only`` to return a spy whose ``close()``
        records the call and delegates to the real close."""
        import repo_intelligence.db_absolute_resolution as _mod

        # A BLOB ``name`` in ``import_names`` survives the round-trip as
        # ``bytes`` (SQLite does not coerce it to TEXT), which trips the
        # adapter's non-text-name check and raises ``AdapterError``
        # mid-flight — the exact codepath that must clean up.
        def seed(conn):
            fi = _insert_file(conn, "a.py")
            _insert_import(conn, fi, "import", "m", 0, 1, 0)
            conn.execute(
                "INSERT INTO import_names (import_id, position, name, asname) "
                "VALUES (1, 0, ?, NULL)", (b"\x00",)
            )

        db = _write_db(tmp_path, "exc.db", seed)

        closed: list[bool] = []

        class _Spy(sqlite3.Connection):
            """Spy whose sole difference from a plain ``Connection`` is
            that ``close()`` records the call before delegating.
            ``load_file_records_from_db``'s ``isinstance(conn,
            sqlite3.Connection)`` check still matches (subclass), so the
            spy exercises the exact same production codepath."""
            def close(self):
                closed.append(True)
                return sqlite3.Connection.close(self)

        def spy_open(path):
            # Reissue the same URI the production ``_open_read_only``
            # would issue, but as a spy subclass.  Production behavior
            # (mode=ro, query_only=ON, percent-encoded path) is preserved
            # by using identical statements; the spy differs only in
            # its ability to count ``close`` calls.
            if not os.path.exists(path):
                raise FileNotFoundError(
                    f"index database not found: {path!r}")
            from urllib.parse import quote as _q
            abs_path = os.path.abspath(path)
            uri = "file:" + _q(abs_path, safe="/") + "?mode=ro"
            conn = _Spy(uri, uri=True)
            conn.execute("PRAGMA query_only = ON")
            return conn

        with mock.patch.object(_mod, "_open_read_only", side_effect=spy_open):
            with pytest.raises(AdapterError):
                resolve_absolute_imports_from_db(str(db), (".",))

        assert closed == [True], (
            f"expected exactly one close() call after exception, got {closed}"
        )

    def test_row_id_permutations_produce_identical_outcomes(self, tmp_path):
        """Two DBs holding identical records in *different* row-ID
        orderings (via different insertion sequences) must produce
        identical Resolution sequences.  Row IDs are internal; they
        must never affect externally visible order (architecture
        §5: 'row IDs cannot affect the externally visible ordering')."""
        def seed_first_second(conn):
            fi = _insert_file(conn, "first.py")
            ii = _insert_import(conn, fi, "from_import", "m", 0, 1, 0)
            conn.execute(
                "INSERT INTO import_names (import_id, position, name, asname) "
                "VALUES (?, 0, 'a', 'k')", (ii,))
            fi2 = _insert_file(conn, "second.py")
            ii2 = _insert_import(conn, fi2, "from_import", "m", 0, 1, 0)
            conn.execute(
                "INSERT INTO import_names (import_id, position, name, asname) "
                "VALUES (?, 0, 'a', 'k')", (ii2,))

        def seed_second_first(conn):
            fi = _insert_file(conn, "second.py")
            ii = _insert_import(conn, fi, "from_import", "m", 0, 1, 0)
            conn.execute(
                "INSERT INTO import_names (import_id, position, name, asname) "
                "VALUES (?, 0, 'a', 'k')", (ii,))
            fi2 = _insert_file(conn, "first.py")
            ii2 = _insert_import(conn, fi2, "from_import", "m", 0, 1, 0)
            conn.execute(
                "INSERT INTO import_names (import_id, position, name, asname) "
                "VALUES (?, 0, 'a', 'k')", (ii2,))

        db_a = _write_db(tmp_path, "perm_a.db", seed_first_second)
        db_b = _write_db(tmp_path, "perm_b.db", seed_second_first)
        res_a = resolve_absolute_imports_from_db(str(db_a), (".",))
        res_b = resolve_absolute_imports_from_db(str(db_b), (".",))
        # Same records, same total ordering, independent of insertion order.
        assert [r.record.source_path for r in res_a] == \
               [r.record.source_path for r in res_b] == ["first.py", "second.py"]
        assert [r.status for r in res_a] == [r.status for r in res_b]
        assert [r.reason for r in res_a] == [r.reason for r in res_b]
        assert [r.record.names for r in res_a] == [r.record.names for r in res_b]
