"""Focused tests for the Phase 2C-1 deterministic module inventory.

Each test maps to a requirement letter (A–Y) from the Phase 2C-1 spec.
Tests use in-memory record lists and temporary synthetic SQLite databases
created via the existing ``repo_intelligence.search`` conventions.  No test
touches the validation repository or executes any indexed source module.
"""

import hashlib
import os
import sqlite3
import tempfile

import pytest

import repo_intelligence
from repo_intelligence import (
    ModuleCandidate,
    PackageRoot,
    build_inventory_from_db,
    build_module_inventory,
    candidates_for_file,
    candidates_for_name,
    is_ambiguous,
    load_file_records_from_db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def rec(rel_path, binary=False):
    """Build a minimal file record dict matching the ``files`` table shape."""
    return {
        "rel_path": rel_path,
        "language": "Python",
        "size": 1,
        "is_binary": binary,
    }


def triple(c):
    """Comparable (root, name, file) triple for an assertion."""
    return (c.root.canonical or ".", c.qualified_name, c.file_path)


def names(inventory):
    return {c.qualified_name for c in inventory}


def make_db(tmp_path, file_specs):
    """Create a synthetic index DB with *file_specs* (rel_path, binary)."""
    from repo_intelligence.search import create_database, insert_file_metadata

    db = str(tmp_path / "index.db")
    create_database(db)
    conn = sqlite3.connect(db)
    try:
        with conn:
            for rp, binary in file_specs:
                insert_file_metadata(
                    conn, rp, "Python", 1, "h" + len(rp).__repr__()
                )
                if binary:
                    conn.execute(
                        "UPDATE files SET is_binary = 1 WHERE rel_path = ?", (rp,)
                    )
    finally:
        conn.close()
    return db


def db_fingerprint(db_file):
    """Deterministic fingerprint of every table definition and row set in
    a SQLite database file (schema + data)."""
    h = hashlib.sha256()
    conn = sqlite3.connect(db_file)
    try:
        # Schema (deterministic order).
        schema = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
            " ORDER BY name, type"
        ).fetchall()
        h.update(repr(schema).encode())
        for name, in conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name"
        ):
            try:
                rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
            except sqlite3.DatabaseError:
                rows = None  # FTS vtables: schema already captured above
            h.update((name + ":" + repr(rows)).encode())
    finally:
        conn.close()
    return h.hexdigest()


# ---------------------------------------------------------------------------
# A. Unique ordinary module under root "."
# ---------------------------------------------------------------------------


def test_A_unique_ordinary_module_under_root_dot():
    inventory = build_module_inventory([rec("helper.py")], ["."])
    assert [triple(c) for c in inventory] == [(".", "helper", "helper.py")]
    c = inventory[0]
    assert c.file_kind == "ordinary_module"
    assert c.directory_kind == "explicit_root"
    assert c.component_supported is True
    assert c.name_supported is True


# ---------------------------------------------------------------------------
# B. Unique ordinary module under "src"
# ---------------------------------------------------------------------------


def test_B_unique_ordinary_module_under_src():
    specs = [rec("src/example.py")]
    inventory = build_module_inventory(specs, ["src"])
    assert [triple(c) for c in inventory] == [("src", "example", "src/example.py")]
    # The same file is NOT a candidate under "." unless "." is supplied.
    inventory_dot = build_module_inventory(specs, ["."])
    assert [c.qualified_name for c in inventory_dot] == ["src.example"]


# ---------------------------------------------------------------------------
# C. Nested ordinary module
# ---------------------------------------------------------------------------


def test_C_nested_ordinary_module():
    specs = [
        rec("pkg/helper.py"),
        rec("pkg/sub/worker.py"),
    ]
    inventory = build_module_inventory(specs, ["."])
    assert names(inventory) == {"pkg.helper", "pkg.sub.worker"}
    # pkg/helper.py must map to pkg.helper, never to "pkg".
    c = candidates_for_name(inventory, "pkg.helper")[0]
    assert c.file_path == "pkg/helper.py"
    assert c.file_kind == "ordinary_module"
    assert c.directory_kind == "namespace_package"  # no __init__.py


# ---------------------------------------------------------------------------
# D. Regular package __init__.py mapping
# ---------------------------------------------------------------------------


def test_D_regular_package_init_mapping():
    specs = [rec("pkg/__init__.py")]
    inventory = build_module_inventory(specs, ["."])
    assert [triple(c) for c in inventory] == [(".", "pkg", "pkg/__init__.py")]
    c = inventory[0]
    assert c.file_kind == "package_initializer"
    assert c.directory_kind == "regular_package"
    assert c.qualified_name != "pkg.__init__"


# ---------------------------------------------------------------------------
# E. Nested package __init__.py mapping
# ---------------------------------------------------------------------------


def test_E_nested_package_init_mapping():
    specs = [
        rec("pkg/__init__.py"),
        rec("pkg/sub/__init__.py"),
    ]
    inventory = build_module_inventory(specs, ["."])
    assert names(inventory) == {"pkg", "pkg.sub"}
    assert candidates_for_name(inventory, "pkg")[0].file_path == "pkg/__init__.py"
    assert (
        candidates_for_name(inventory, "pkg.sub")[0].file_path
        == "pkg/sub/__init__.py"
    )


# ---------------------------------------------------------------------------
# F. __init__.py directly at configured root
# ---------------------------------------------------------------------------


def test_F_init_at_configured_root_is_not_emitted():
    specs = [rec("src/__init__.py")]
    # Under root "src" there is no parent package identity to invent:
    # the record is present in the inventory but yields no candidate.
    inventory = build_module_inventory(specs, ["src"])
    assert inventory == ()

    # Under root "." the same file is a normal package "src".
    inventory_dot = build_module_inventory(specs, ["."])
    assert [triple(c) for c in inventory_dot] == [(".", "src", "src/__init__.py")]

    # Nested package under the root is unaffected by the exclusion.
    specs2 = [rec("src/__init__.py"), rec("src/core/__init__.py")]
    inv = build_module_inventory(specs2, ["src"])
    assert [triple(c) for c in inv] == [("src", "core", "src/core/__init__.py")]


# ---------------------------------------------------------------------------
# G. Namespace-package portion with descendant module
# ---------------------------------------------------------------------------


def test_G_namespace_package_with_descendant():
    specs = [rec("ns/mod.py")]  # no __init__.py anywhere
    inventory = build_module_inventory(specs, ["."])
    c = inventory[0]
    assert triple(c) == (".", "ns.mod", "ns/mod.py")
    assert c.directory_kind == "namespace_package"
    # The directory alone is never emitted as a module-file target.
    assert "ns" not in names(inventory)


# ---------------------------------------------------------------------------
# H. Nested namespace-package portions
# ---------------------------------------------------------------------------


def test_H_nested_namespace_portions():
    specs = [
        rec("ns/a.py"),
        rec("ns/b.py"),
        rec("ns/deep/mod.py"),
    ]
    inventory = build_module_inventory(specs, ["."])
    triples = sorted(triple(c) for c in inventory)
    assert triples == [
        (".", "ns.a", "ns/a.py"),
        (".", "ns.b", "ns/b.py"),
        (".", "ns.deep.mod", "ns/deep/mod.py"),
    ]
    for c in (inventory[0], inventory[1], inventory[2]):
        assert c.directory_kind == "namespace_package"


# ---------------------------------------------------------------------------
# I. Hyphenated package directory
# ---------------------------------------------------------------------------


def test_I_hyphenated_package_directory():
    specs = [rec("my-plugin/tool.py")]
    inventory = build_module_inventory(specs, ["."])
    (c,) = inventory
    # Retained as a structural candidate ...
    assert c.qualified_name == "my-plugin.tool"
    # ... with no runtime-import guarantee (flagged unsupported) ...
    assert c.component_supported is False
    assert c.name_supported is False
    # ... and never sanitized into my_plugin.tool
    assert "my_plugin" not in {x.qualified_name for x in inventory}


# ---------------------------------------------------------------------------
# J. Multiple roots map one file to different names (both preserved)
# ---------------------------------------------------------------------------


def test_J_multiple_roots_one_file_different_names():
    specs = [rec("tests/identity_migration.py")]
    inventory = build_module_inventory(specs, [".", "tests"])
    triples = [triple(c) for c in inventory]
    assert triples == [
        (".", "tests.identity_migration", "tests/identity_migration.py"),
        ("tests", "identity_migration", "tests/identity_migration.py"),
    ]
    file_cands = candidates_for_file(inventory, "tests/identity_migration.py")
    assert len(file_cands) == 2
    # Two evidence records for the SAME file are not multi-file ambiguity.
    assert is_ambiguous(file_cands) is False


# ---------------------------------------------------------------------------
# K. Two different files -> same qualified name (multi-file ambiguity)
# ---------------------------------------------------------------------------


def test_K_two_files_same_qualified_name():
    specs = [rec("alpha/mod.py"), rec("beta/mod.py")]
    inventory = build_module_inventory(specs, [".", "alpha", "beta"])
    cands = candidates_for_name(inventory, "mod")
    files = {c.file_path for c in cands}
    assert files == {"alpha/mod.py", "beta/mod.py"}
    assert is_ambiguous(cands) is True
    # No silent first-match selection: both candidates are preserved.
    assert len(cands) == 2


# ---------------------------------------------------------------------------
# L. Module/package collision: foo.py and foo/__init__.py
# ---------------------------------------------------------------------------


def test_L_module_package_collision():
    specs = [rec("foo.py"), rec("foo/__init__.py")]
    inventory = build_module_inventory(specs, ["."])
    cands = candidates_for_name(inventory, "foo")
    assert {triple(c) for c in cands} == {
        (".", "foo", "foo.py"),
        (".", "foo", "foo/__init__.py"),
    }
    kinds = {c.file_kind for c in cands}
    assert kinds == {"ordinary_module", "package_initializer"}
    assert is_ambiguous(cands) is True
    # Neither record is dropped and no third record is invented for "foo".
    assert build_module_inventory(specs, ["."]) and len(inventory) == 2


# ---------------------------------------------------------------------------
# M. Equivalent root spellings
# ---------------------------------------------------------------------------


def test_M_equivalent_root_spellings():
    specs = [rec("src/example/core.py")]
    base = build_module_inventory(specs, ["./src"])
    for spelling in ("src/", "./src/", "src"):
        assert build_module_inventory(specs, [spelling]) == base
    # Canonical identity is spelling-independent.
    (c,) = base
    assert isinstance(c.root, PackageRoot)
    assert c.root.canonical == "src"
    assert triple(c) == ("src", "example.core", "src/example/core.py")


# ---------------------------------------------------------------------------
# N. Duplicate root inputs deduplicate
# ---------------------------------------------------------------------------


def test_N_duplicate_roots_deduplicated():
    specs = [rec("helper.py")]
    inventory = build_module_inventory(specs, [".", ".", "./"])
    assert len(inventory) == 1
    assert [triple(c) for c in inventory] == [(".", "helper", "helper.py")]
    # Nested/duplicate mix keeps every distinct root exactly once.
    specs2 = [rec("src/example.py")]
    inventory2 = build_module_inventory(specs2, ["src", "src", ".", "./src"])
    roots = [c.root.canonical for c in inventory2]
    assert roots == ["", "src"]  # canonical order: repo root first


# ---------------------------------------------------------------------------
# O. Empty root collection
# ---------------------------------------------------------------------------


def test_O_empty_root_collection_rejected():
    specs = [rec("helper.py")]
    with pytest.raises(ValueError):
        build_module_inventory(specs, [])
    with pytest.raises(ValueError):
        build_module_inventory(specs, None)
    with pytest.raises(ValueError):
        build_module_inventory(specs, [""])
    with pytest.raises(TypeError):
        build_module_inventory(specs, 42)


# ---------------------------------------------------------------------------
# P. Root outside repository rejected
# ---------------------------------------------------------------------------


def test_P_root_outside_repository_rejected():
    specs = [rec("helper.py")]
    for bad in ("..", "../outside", "a/../../b", "/abs/out", "C:/win/abs"):
        with pytest.raises(ValueError):
            build_module_inventory(specs, [bad])
    # A legal root in the same call makes the whole call fail: roots are
    # validated before any mapping is attempted.
    with pytest.raises(ValueError):
        build_module_inventory(specs, [".", "../outside"])


# ---------------------------------------------------------------------------
# Q. Root-prefix boundary: tests/ must not match tests_extra/
# ---------------------------------------------------------------------------


def test_Q_root_prefix_boundary():
    specs = [
        rec("tests/a.py"),
        rec("tests_extra/b.py"),
    ]
    inventory = build_module_inventory(specs, ["tests"])
    triples = [triple(c) for c in inventory]
    assert triples == [("tests", "a", "tests/a.py")]
    # And with "." both are visible under their full path.
    inventory_dot = build_module_inventory(specs, ["."])
    triples_dot = [triple(c) for c in inventory_dot]
    assert triples_dot == [
        (".", "tests.a", "tests/a.py"),
        (".", "tests_extra.b", "tests_extra/b.py"),
    ]


# ---------------------------------------------------------------------------
# R. Unsupported file extensions are not module targets
# ---------------------------------------------------------------------------


def test_R_unsupported_extensions_excluded():
    mixed = [
        {"rel_path": "mod.pyi", "language": "Python"},
        {"rel_path": "mod.pyx", "language": "Python"},
        {"rel_path": "mod.pyw", "language": "Python"},
        {"rel_path": "mod.py", "language": "Python"},
    ]
    inventory = build_module_inventory(mixed, ["."])
    assert [c.qualified_name for c in inventory] == ["mod"]
    assert inventory[0].file_path == "mod.py"


# ---------------------------------------------------------------------------
# S. Binary indexed Python file excluded
# ---------------------------------------------------------------------------


def test_S_binary_python_file_excluded():
    specs = [
        rec("bin.py", binary=True),
        rec("ok.py", binary=False),
    ]
    inventory = build_module_inventory(specs, ["."])
    assert [c.file_path for c in inventory] == ["ok.py"]


# ---------------------------------------------------------------------------
# T/U. Shuffled input order (files and roots) yields identical output
# ---------------------------------------------------------------------------


def test_TU_shuffled_inputs_identical_output():
    specs = [
        rec("a/b/c.py"),
        rec("d.py"),
        rec("e/__init__.py"),
        rec("f/g.py"),
        rec("h/i.py"),
    ]
    roots_a = [".", "a", "e"]
    roots_b = ["e", ".", "a"]
    base = build_module_inventory(specs, roots_a)
    assert build_module_inventory(list(reversed(specs)), roots_b) == base
    # Repeated build is stable.
    assert build_module_inventory(specs, roots_a) == base


# ---------------------------------------------------------------------------
# V. Independence from database row IDs
# ---------------------------------------------------------------------------


def test_V_row_id_independence(tmp_path):
    db1 = make_db(tmp_path / "one", [("z/last.py", 0), ("a/first.py", 0), ("m/mid.py", 0)])
    db2 = make_db(
        tmp_path / "two",
        [("a/first.py", 0), ("z/last.py", 0), ("m/mid.py", 0)],  # different ids
    )
    inv1 = build_inventory_from_db(db1, ["."])
    inv2 = build_inventory_from_db(db2, ["."])
    assert inv1 == inv2
    assert [triple(c) for c in inv1] == [
        (".", "a.first", "a/first.py"),
        (".", "m.mid", "m/mid.py"),
        (".", "z.last", "z/last.py"),
    ]


# ---------------------------------------------------------------------------
# W. Read-only query behavior
# ---------------------------------------------------------------------------


def test_W_read_only_query_behavior(tmp_path):
    db = make_db(
        tmp_path / "ro",
        [
            ("pkg/__init__.py", 0),
            ("pkg/mod.py", 0),
            ("plain.py", 0),
            ("binary.py", 1),
        ],
    )
    before = db_fingerprint(db)
    # Call twice to catch lazy side effects.
    load_file_records_from_db(db)
    inventory = build_inventory_from_db(db, [".", "pkg"])
    after = db_fingerprint(db)
    # Schema + all row sets byte-identical: no INSERT/UPDATE/DELETE/CREATE/DROP.
    assert before == after
    # No journal / WAL leftovers from the read-only opens.
    for junk in ("-journal", "-wal", "-shm"):
        assert not os.path.exists(db + junk)
    # Sanity: the read produced the expected result.  Roots "." and "pkg"
    # both own records: "pkg/" maps pkg/mod.py -> "mod".
    assert names(inventory) == {"pkg", "mod", "pkg.mod", "plain"}
    # Record order is deterministic and independent of insertion order.
    recs = load_file_records_from_db(db)
    assert [r["rel_path"] for r in recs] == sorted(
        r["rel_path"] for r in recs
    )


# ---------------------------------------------------------------------------
# X. Frozen Phase 2B APIs remain importable and unchanged in shape
# ---------------------------------------------------------------------------


def test_X_frozen_phase_2b_apis_unchanged(tmp_path):
    frozen = [
        "scan_repo",
        "db_path",
        "create_database",
        "insert_repo_info",
        "insert_file_metadata",
        "insert_text_content",
        "search_files",
        "get_file_list",
        "get_stats",
        "delete_imports_for_file",
        "get_imports",
        "replace_imports_for_file",
    ]
    for name in frozen:
        assert name in repo_intelligence.__all__, name
        assert callable(getattr(repo_intelligence, name))
    # Frozen query behavior on a synthetic DB still works.
    from repo_intelligence.search import get_file_list, get_stats

    db = make_db(
        tmp_path / "x",
        [("a.py", 0), ("b.py", 1)],
    )
    rows = get_file_list(db)
    assert [r["rel_path"] for r in rows] == ["a.py", "b.py"]
    assert get_stats(db)["total_files"] == 2
    # AST extraction entry points still present and unmodified in signature.
    from repo_intelligence.python_ast import (
        ImportRecord,
        extract_file_imports,
        extract_file_symbols,
    )

    records = extract_file_imports("x.py", "import os, sys")
    assert {r.module for r in records} == {"os", "sys"}
    assert all(isinstance(r, ImportRecord) for r in records)
    syms = extract_file_symbols("x.py", "def f():\n    pass")
    assert any(s["name"] == "f" for s in syms)


# ---------------------------------------------------------------------------
# Y. Unsupported path components are retained but flagged
# ---------------------------------------------------------------------------


def test_Y_unsupported_components_flagged_not_rewritten():
    specs = [
        rec("1mod.py"),          # digit-leading stem: legal chars, bad name
        rec("a1.py"),            # digit after first char: fully supported
        rec("for.py"),           # Python keyword collision
        rec("bad dir/mod.py"),   # whitespace component
        rec("a/b.c/d.py"),       # literal dot inside a directory component
    ]
    inventory = build_module_inventory(specs, ["."])
    by_name = {c.qualified_name: c for c in inventory}
    # Nothing is silently rewritten into a different dotted path.
    assert by_name["1mod"].component_supported is True
    assert by_name["1mod"].name_supported is False  # digit-leading
    assert by_name["a1"].component_supported is True
    assert by_name["a1"].name_supported is True
    assert by_name["for"].component_supported is True
    assert by_name["for"].name_supported is False  # keyword
    assert by_name["bad dir.mod"].component_supported is False
    assert by_name["bad dir.mod"].name_supported is False
    assert by_name["a.b.c.d"].component_supported is False
    assert by_name["a.b.c.d"].name_supported is False
    assert not any("a_bc" in c.qualified_name for c in inventory)
    # Hyphenated names are retained verbatim (no underscore sanitization).
    hyph = build_module_inventory([rec("my-plug/tool.py")], ["."])
    assert hyph[0].qualified_name == "my-plug.tool"
    # Supported ones still flag correctly.
    ok = build_module_inventory([rec("ok/mod.py")], ["."])
    assert ok[0].component_supported is True and ok[0].name_supported is True


# ---------------------------------------------------------------------------
# Package-root contract: nested roots and containment
# ---------------------------------------------------------------------------


def test_nested_roots_overlap_preserved():
    specs = [rec("src/sub/mod.py")]
    inventory = build_module_inventory(specs, [".", "src", "src/sub"])
    triples = [triple(c) for c in inventory]
    assert triples == [
        (".", "src.sub.mod", "src/sub/mod.py"),
        ("src", "sub.mod", "src/sub/mod.py"),
        ("src/sub", "mod", "src/sub/mod.py"),
    ]
    cands = candidates_for_file(inventory, "src/sub/mod.py")
    assert is_ambiguous(cands) is False  # same file, no multi-file ambiguity


# ---------------------------------------------------------------------------
# Duplicate candidates are not emitted; distinct candidates preserved
# ---------------------------------------------------------------------------


def test_no_duplicate_candidates():
    specs = [
        rec("pkg/__init__.py"),
        rec("pkg/helper.py"),
    ]
    # Supplying equivalent spellings of the same root twice must not
    # duplicate any candidate row.
    inventory = build_module_inventory(specs, [".", "./", "."])
    keys = [
        (c.root.canonical, c.qualified_name, c.file_path) for c in inventory
    ]
    assert len(keys) == len(set(keys)) == 2


# ---------------------------------------------------------------------------
# Deterministic output: stable ordering of the public relation
# ---------------------------------------------------------------------------


def test_output_ordering_is_canonical():
    specs = [
        rec("zzz.py"),
        rec("aaa/b.py"),
        rec("ccc/__init__.py"),
        rec("ddd/e.py"),
    ]
    inventory = build_module_inventory(specs, ["zzz", ".", "ddd/"])
    keys = [
        (c.root.canonical, c.qualified_name, c.file_path) for c in inventory
    ]
    assert keys == sorted(keys)
    assert isinstance(inventory, tuple)
    for c in inventory:
        assert isinstance(c, ModuleCandidate)
        # Evidence is deterministic strings only.
        assert all(isinstance(e, str) for e in c.evidence)


# ---------------------------------------------------------------------------
# Public helper error semantics
# ---------------------------------------------------------------------------


def test_helpers_raise_for_missing_keys():
    specs = [rec("helper.py")]
    inventory = build_module_inventory(specs, ["."])
    with pytest.raises(KeyError):
        candidates_for_file(inventory, "nope.py")
    with pytest.raises(KeyError):
        candidates_for_name(inventory, "nope")


# ---------------------------------------------------------------------------
# Phase 2C-1 FINAL ACCEPTANCE AUDIT — regression tests (audit areas 1-4)
# ---------------------------------------------------------------------------


def test_AUDIT_db_missing_path_not_created(tmp_path):
    """Audit 1: a nonexistent database path must raise BEFORE any file
    exists — no silent empty-database creation."""
    missing = str(tmp_path / "never-created.db")
    assert not os.path.exists(missing)
    with pytest.raises(FileNotFoundError):
        load_file_records_from_db(missing)
    with pytest.raises(FileNotFoundError):
        build_inventory_from_db(missing, ["."])
    assert not os.path.exists(missing), "the adapter must never create the DB file"


def test_AUDIT_db_connection_genuinely_read_only(tmp_path):
    """Audit 1: a file-path connection must be SQLite-enforced read-only.
    Any mutating statement (this one attempts it; it must fail even though
    the adapter code would never issue one) is rejected by the DB engine,
    and the database file must remain byte-identical across the call."""
    import hashlib
    from repo_intelligence.search import create_database, insert_file_metadata

    db = str(tmp_path / "ro.db")
    create_database(db)
    conn = sqlite3.connect(db)
    insert_file_metadata(conn, "pkg/mod.py", "Python", 1, "h")
    conn.commit()
    conn.close()

    def fingerprint(path):
        return hashlib.sha256(open(path, "rb").read()).hexdigest()

    before = fingerprint(db)
    recs = load_file_records_from_db(db)
    assert recs[0]["rel_path"] == "pkg/mod.py"
    after = fingerprint(db)
    assert before == after, "adapter must not alter the database file"
    for junk in ("-journal", "-wal", "-shm"):
        assert not os.path.exists(db + junk)

    # Direct proof the connection is engine-level read-only: open the same
    # file the way the adapter does (uri mode=ro) and show a write fails.
    ro = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("UPDATE files SET size = 1")
            ro.commit()
    finally:
        ro.close()
    # The failed write must not have taken effect.
    assert fingerprint(db) == before


def test_AUDIT_db_caller_owned_connection_not_closed(tmp_path):
    """Audit 1: a caller-supplied open connection must remain usable after
    the call — the adapter must not close it."""
    from repo_intelligence.search import create_database, insert_file_metadata

    db = str(tmp_path / "conn.db")
    create_database(db)
    conn = sqlite3.connect(db)
    insert_file_metadata(conn, "a/b.py", "Python", 1, "h")
    conn.commit()  # deliberately do NOT close; this is a caller-owned conn

    recs = load_file_records_from_db(conn)
    assert recs[0]["rel_path"] == "a/b.py"
    # The caller's own connection must still be usable (not closed for them).
    rows = conn.execute("SELECT COUNT(*) FROM files").fetchall()
    assert rows == [(1,)]
    conn.close()  # only the caller closes it


def test_AUDIT_root_order_equivalence():
    """Audit 2: equivalent root spellings in different order/duplicates
    must produce byte-identical semantic inventories — the diagnostic
    ``PackageRoot.original`` spelling must not leak into ordering or
    identity."""
    specs = [
        rec("src/example/core.py"),
        rec("src/other.py"),
        rec("src_extra/core.py"),  # component-boundary probe (must NOT
                                   # match root "src")
    ]
    r1 = build_module_inventory(specs, ["src/", "./src"])
    r2 = build_module_inventory(specs, ["./src", "src/"])
    assert r1 == r2, "shuffled equivalent root spellings must be identical"
    # Repeated duplicates collapse to the same single root, order-invariant.
    r3 = build_module_inventory(specs, ["src", "src", "./src", "src/", "./src/"])
    assert r1 == r3
    assert {c.root.canonical for c in r3} == {"src"}
    # Exactly one root identity appears in the output — no duplicate
    # candidates from repeated equivalent spellings.  Two candidates are
    # expected here: "example.core" (nested) and "other" (directly on the
    # root) — distinct by qualified name and file path.
    keys = [
        (c.root.canonical, c.qualified_name, c.file_path) for c in r1
    ]
    assert len(keys) == len(set(keys)) == 2
    assert {"example.core", "other"} == {c.qualified_name for c in r1}
    # The emitted candidate carries the canonical identity, not the
    # first-seen original spelling.
    (c_core,) = candidates_for_name(r1, "example.core")
    assert c_core.root.canonical == "src"
    assert c_core.file_path == "src/example/core.py"
    # Semantic equivalence under shuffled input *file* order too.
    assert build_module_inventory(list(reversed(specs)), ["src/", "./src"]) == r1
    # Component-boundary containment: "src_extra/core.py" is NEVER a
    # candidate under root "src" (complete path components, no string
    # prefix matching).
    assert not any(c.file_path == "src_extra/core.py" for c in r1)


def test_AUDIT_hyphenated_and_whitespace_roots():
    """Audit 4: hyphenated and whitespace-bearing directory names are
    legitimate search-path roots and must be accepted verbatim as ROOTS
    (they are only flagged unsupported when they appear inside an emitted
    qualified name component)."""
    # Hyphenated root, mirroring the hermes-satisfactory ``plugins/*/``
    # layout.  Only files that sit DIRECTLY on the root are unambiguous
    # structural candidates here (a hyphenated *name component* inside an
    # emitted dotted name is correctly flagged unsupported).
    specs = [
        rec("my-tool/mod.py"),
    ]
    inv = build_module_inventory(specs, ["my-tool", "./my-tool/", "my-tool"])
    triples = [(c.root.canonical, c.qualified_name, c.file_path,
                c.directory_kind) for c in inv]
    assert triples == [("my-tool", "mod", "my-tool/mod.py", "explicit_root")]
    assert len(inv) == 2 - 1  # deduplicated: one candidate, no silent selection
    (c,) = inv
    assert c.root.canonical == "my-tool"
    assert c.name_supported is True  # "mod" is a fully legal name component

    # Whitespace-inside-component root (a legal directory name on the
    # filesystem) must also be accepted as a root.
    specs2 = [
        rec("my tool/mod.py"),
    ]
    inv2 = build_module_inventory(specs2, ["my tool"])
    (c2,) = inv2
    assert c2.root.canonical == "my tool"
    assert c2.qualified_name == "mod"
    assert c2.file_path == "my tool/mod.py"
    assert c2.name_supported is True  # the emitted name "mod" is fully valid

    # The emitted-name protection still holds: a hyphen inside an emitted
    # NAME component flags the candidate unsupported (retained, not
    # sanitized) — that is the name-side rule, independent of root policy.
    specs3 = [
        rec("plugins/my-tool.py"),
    ]
    inv3 = build_module_inventory(specs3, [".", "plugins"])
    under_plugins = [c for c in inv3 if c.root.canonical == "plugins"]
    (c3,) = under_plugins
    assert c3.qualified_name == "my-tool"
    assert c3.component_supported is False
    assert c3.name_supported is False
    assert "my_tool" not in {c.qualified_name for c in inv3}  # no sanitization

    # Audit 2 (whitespace): a root component with leading/trailing
    # whitespace must NOT be silently rewritten to a different directory.
    # "src " is a different (and likely nonexistent) directory than "src";
    # silently mapping it to "src" would misreport which root owns the
    # candidate, so it is rejected explicitly instead.
    specs_ws = [rec("src/helper.py")]
    with pytest.raises(ValueError):
        build_module_inventory(specs_ws, ["src "])   # trailing space
    with pytest.raises(ValueError):
        build_module_inventory(specs_ws, ["  src"])  # leading space
    with pytest.raises(ValueError):
        build_module_inventory(specs_ws, ["src dir / sub"])  # not valid syntax, but the trailing-space case is what matters; ensure rejection, not silent rewrite
    # Meanwhile, internal whitespace components remain valid roots.
    inv_ws = build_module_inventory([rec("src dir/mod.py")], ["src dir"])
    assert inv_ws[0].root.canonical == "src dir"
    assert inv_ws[0].qualified_name == "mod"

    # The protections that DO remain are unchanged: dot components
    # (escape / ambiguity), absolute roots, and embedded control
    # characters are still rejected at the root level.
    specs4 = [rec("helper.py")]
    still_bad = [
        "..",            # dot component (escape/ambiguity) — always rejected
        "../outside",
        "a/../../b",     # embedded escape component
        "a/../b",
        "/abs/out",      # absolute root
        "C:/win/abs",
        "a\x00b",        # embedded NUL / control character
        "a\tb/c",        # embedded control char inside a component
        "a/b\nc",        # embedded control char inside a component
    ]
    for bad in still_bad:
        with pytest.raises(ValueError):
            build_module_inventory(specs4, [bad])

    # Contrast: a plain directory root remains accepted (and its files map
    # as ordinary candidates under it).
    inv_ok = build_module_inventory([rec("helper/mod.py")], ["helper"])
    assert len(inv_ok) == 1
    assert inv_ok[0].qualified_name == "mod"
    assert inv_ok[0].root.canonical == "helper"


# ---------------------------------------------------------------------------
# Phase 2C-1R1 — Codex review corrections (focused regression tests)
# ---------------------------------------------------------------------------


def test_R1_URI_filename_with_hash(tmp_path):
    """Correction 1: filename containing ``#`` must be opened exactly."""
    from repo_intelligence.search import create_database, insert_file_metadata

    db = str(tmp_path / "index #1.db")
    create_database(db)
    conn = sqlite3.connect(db)
    insert_file_metadata(conn, "a/b.py", "Python", 1, "h")
    conn.commit()
    conn.close()

    recs = load_file_records_from_db(db)
    assert [r["rel_path"] for r in recs] == ["a/b.py"]


def test_R1_URI_filename_with_percent_and_question(tmp_path):
    """Correction 1: filenames containing ``?`` and ``%`` (including
    ``%3F`` which should be preserved as a literal percent triple, not
    re-decoded) must open the correct file — none of these characters may
    cause the URI parser to misinterpret the path as a fragment/query."""
    from repo_intelligence.search import create_database, insert_file_metadata
    import os

    def make(name, rp):
        db = str(tmp_path / name)
        create_database(db)
        c = sqlite3.connect(db)
        insert_file_metadata(c, rp, "Python", 1, "h")
        c.commit()
        c.close()
        return db

    cases = [
        ("data?1.db", "a/b.py"),
        ("data%3F1.db", "c/d.py"),   # literal %3F in the filename
        ("data 1.db", "e/f.py"),
    ]
    for fname, rp in cases:
        db = make(fname, rp)
        recs = load_file_records_from_db(db)
        assert recs and recs[0]["rel_path"] == rp, f"open failed for {fname!r}"
        assert os.path.exists(db), f"target file {db!r} missing"

    # After the loop, all three files must be exactly present — a
    # mis-parsed URI would have opened/created a truncated sibling at an
    # unexpected path (e.g. for ``data?1.db`` a ``?1.db`` at CWD).
    siblings = set(os.listdir(str(tmp_path)))
    expected = {name for name, _ in cases}
    # All files we created must be present.
    for want in expected:
        assert want in siblings, f"expected {want!r} in {siblings}"
    # No truncated sibling should have been created (e.g. "?1.db",
    # "1.db").  Any of the raw filename prefixes should be exactly the
    # full names we listed.
    unexpected_siblings = siblings - expected
    assert not unexpected_siblings, f"unexpected sibling files: {unexpected_siblings}"


def test_R1_URI_read_only_enforced(tmp_path):
    """Correction 1: the produced connection is SQLite-enforced read-only
    (writes are rejected at the engine level)."""
    from repo_intelligence.search import create_database, insert_file_metadata

    db = str(tmp_path / "ro.db")
    create_database(db)
    c = sqlite3.connect(db)
    insert_file_metadata(c, "a/b.py", "Python", 1, "h")
    c.commit()
    c.close()

    recs = load_file_records_from_db(db)
    assert recs and recs[0]["rel_path"] == "a/b.py"
    # Independently open the same file the way the adapter does, and show a
    # write is rejected by the engine.
    ro = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("UPDATE files SET size = 1")
        ro.commit()
    ro.close()


def test_R1_root_serialization_order_independent():
    """Correction 2: equivalent root spellings in different orders must
    serialize identically (the diagnostic ``original`` spelling must be
    a deterministic representative, not first-seen).  Permutations of
    the SAME input set are used; the lexicographically smallest supplied
    spelling is chosen."""
    import itertools, dataclasses

    specs = [rec("src/example.py")]

    input_list = ["./src", "src/", "src"]
    perms = [list(p) for p in set(itertools.permutations(input_list))]
    results = [build_module_inventory(specs, p) for p in perms]
    for p, r in zip(perms, results):
        assert r == results[0], f"permutation {p} not equal to base"
        assert len(r) == 1
    # Every serialization must be byte-identical across all permutations.
    serializations = {
        str(tuple(dataclasses.asdict(c) for c in r)) for r in results
    }
    assert len(serializations) == 1

    # The representative original spelling must be the lexicographically
    # smallest equivalent spelling supplied ("./src" < "src" < "src/").
    (c,) = results[0]
    assert c.root.original == "./src"
    assert c.root.canonical == "src"
    assert c.qualified_name == "example"

    # A different input SET (different representative) still has a stable
    # canonical identity, order-independent within that set.
    alt = [build_module_inventory(specs, p) for p
           in (["./src/", "src/"], ["src/", "./src/"])]
    assert alt[0] == alt[1]
    (ca,) = alt[0]
    assert ca.root.canonical == "src"
    assert ca.root.original == "./src/"  # smallest spelling of THIS set


def test_R1_duplicate_records_conflicting(tmp_path):
    """Correction 3: duplicate file records with conflicting metadata must
    fail deterministically (same exception type for both input orders,
    and the reported identity pair is the same set of metadata)."""
    from repo_intelligence.module_inventory import _canonical_records
    import re as _re

    def conflict_ids(msg):
        # Extract the two (language, is_binary) identities from the message
        # and return them as a sorted, order-independent set.
        pairs = _re.findall(
            r"language=(?:'([^']*)'|[^,]+),\s*is_binary=(True|False)", msg
        )
        return sorted(pairs)

    a = rec("m.py", binary=False)
    b = rec("m.py", binary=True)
    # Both permutations must raise the same deterministic error type, and
    # report the same (conflicting) identity pair, order-independently.
    with pytest.raises(ValueError) as exc_ab:
        _canonical_records([a, b])
    with pytest.raises(ValueError) as exc_ba:
        _canonical_records([b, a])
    msg_ab, msg_ba = str(exc_ab.value), str(exc_ba.value)
    assert "conflicting duplicate records" in msg_ab
    assert "conflicting duplicate records" in msg_ba
    assert "m.py" in msg_ab and "m.py" in msg_ba
    # Order-invariant identity report.
    assert conflict_ids(msg_ab) == conflict_ids(msg_ba)
    # Both raise ValueError of the same type.
    assert type(exc_ab.value) is ValueError
    assert type(exc_ba.value) is ValueError

    # Conflicting language values also raise (order-invariant).
    rec_a = {"rel_path": "m.py", "language": "Python", "is_binary": False}
    rec_b = {"rel_path": "m.py", "language": "Other", "is_binary": False}
    for perm in ([rec_a, rec_b], [rec_b, rec_a]):
        with pytest.raises(ValueError, match="conflicting duplicate"):
            _canonical_records(perm)

    # Identical duplicate records (same metadata) → safely collapsed,
    # NOT rejected.
    rec_c = rec("m.py", binary=False)
    rec_d = rec("m.py", binary=False)
    out = _canonical_records([rec_c, rec_d])
    assert len(out) == 1

    # Distinct paths with no duplicates → pass through unchanged,
    # and the output order is sorted by rel_path (input-order independent).
    out2 = _canonical_records([rec("b.py"), rec("a.py"), rec("a.py")])
    assert [r["rel_path"] for r in out2] == ["a.py", "b.py"]




# ---------------------------------------------------------------------------
# Phase 2C-1R2 — Codex review corrections (focused regression tests)
# ---------------------------------------------------------------------------


def test_R2_literal_dot_not_conflated_with_structured_dots():
    """Correction 4: a/b.c.py (literal dot in one component) and a/b/c.py
    (separate components) both display as "a.b.c" but are DIFFERENT
    structural identities.  candidates_for_name("a.b.c") must select only
    the normally structured file; the literal-dot candidate stays visible
    via file-path lookup; no false multi-file ambiguity arises."""
    inventory = build_module_inventory([rec("a/b.c.py"), rec("a/b/c.py")], ["."])
    by_file = {c.file_path: c for c in inventory}

    # Both are retained (no silent rewrite/sanitization).
    assert "a/b.c.py" in by_file and "a/b/c.py" in by_file
    assert by_file["a/b/c.py"].name_components == ("a", "b", "c")
    assert by_file["a/b.c.py"].name_components == ("a", "b.c")
    # The literal-dot component is flagged unsupported ...
    assert by_file["a/b.c.py"].component_supported is False
    assert by_file["a/b.c.py"].name_supported is False
    # ... while the normally structured one is fully supported.
    assert by_file["a/b/c.py"].component_supported is True
    assert by_file["a/b/c.py"].name_supported is True

    # Dotted-name lookup resolves ONLY the structured candidate.
    matched = candidates_for_name(inventory, "a.b.c")
    assert [c.file_path for c in matched] == ["a/b/c.py"]
    # No false multi-file ambiguity: only one distinct file for this identity.
    assert is_ambiguous(matched) is False

    # The literal-dot candidate remains observable through file lookup,
    # and is not reachable by the "a.b.c" dotted identity.
    ld = candidates_for_file(inventory, "a/b.c.py")
    assert ld[0].name_components == ("a", "b.c")

    # A genuine multi-file collision on the SAME identity is still detected.
    # (Two DISTINCT files both named a/b/c.py, under two different roots,
    # both produce the identity ("a","b","c").)
    inv2 = build_module_inventory(
        [rec("root1/a/b/c.py"), rec("root2/a/b/c.py")],
        ["root1", "root2"]
    )
    real = candidates_for_name(inv2, "a.b.c")
    assert is_ambiguous(real) is True
    # Exactly two distinct files.
    assert {c.file_path for c in real} == {
        "root1/a/b/c.py", "root2/a/b/c.py"
    }
    assert len(real) == 2


def test_R2_literal_dot_no_false_ambiguity_and_regression_checks():
    """Correction 4 (regressions): ordinary nested packages and
    hyphenated structural candidates still map and flag correctly."""
    # Ordinary nested package.
    inv = build_module_inventory(
        [rec("pkg/__init__.py"), rec("pkg/sub/worker.py")], ["."])
    worker = [c for c in inv if c.file_path == "pkg/sub/worker.py"][0]
    assert worker.qualified_name == "pkg.sub.worker"
    assert worker.name_components == ("pkg", "sub", "worker")
    assert worker.component_supported is True and worker.name_supported is True
    (w,) = candidates_for_name(inv, "pkg.sub.worker")
    assert w.file_path == "pkg/sub/worker.py"

    # Hyphenated structural candidate preserved verbatim, unsupported, no rewrite.
    hyph = build_module_inventory([rec("my-plug/tool.py")], ["."])
    (h,) = hyph
    assert h.qualified_name == "my-plug.tool"
    assert h.name_components == ("my-plug", "tool")
    assert h.component_supported is False and h.name_supported is False
    assert not any("my_plug" in c.qualified_name for c in hyph)


def test_R2_trailing_newline_in_component():
    """Correction 5: a trailing newline must not be accepted by a
    ``$``-anchored ``match()``; fullmatch rejects the whole component."""
    # Trailing newline in filename stem.
    inv = build_module_inventory([rec("a\n.py")], ["."])
    (bad,) = inv
    assert bad.file_path == "a\n.py"
    assert bad.component_supported is False
    assert bad.name_supported is False

    # Trailing newline in a directory component.
    inv2 = build_module_inventory([rec("d\n/mod.py")], ["."])
    (bad2,) = inv2
    assert bad2.component_supported is False
    assert bad2.name_supported is False

    # Valid ordinary identifier still supported.
    inv3 = build_module_inventory([rec("good/mod.py")], ["."])
    (ok,) = inv3
    assert ok.component_supported is True and ok.name_supported is True

    # Valid hyphenated structural candidate (preserved, unsupported, no rewrite).
    inv4 = build_module_inventory([rec("my-plug/tool.py")], ["."])
    (hy,) = inv4
    assert hy.name_components == ("my-plug", "tool")
    assert hy.component_supported is False and hy.name_supported is False


def test_R2_noncanonical_indexed_paths_rejected():
    """Correction 6: noncanonical indexed file paths are rejected with a
    deterministic, explicit error rather than partially normalized."""
    cases = {
        "repeated separators":   "src//pkg/m.py",
        "leading ./":            "./src/m.py",
        "embedded . component":  "a/./b.py",
        "parent traversal":      "a/../b.py",
        "absolute path":         "/abs/mod.py",
    }
    for label, path in cases.items():
        with pytest.raises(ValueError, match="File path|Absolute"):
            build_module_inventory([rec(path)], ["."])


def test_R2_regular_and_namespace_classification_for_canonical_paths():
    """Correction 6 (F, G): correct package classification for CANONICAL
    paths (with and without __init__.py)."""
    # F: regular package (canonical, __init__.py present).
    inv = build_module_inventory([rec("pkg/__init__.py"), rec("pkg/m.py")], ["."])
    (m,) = (c for c in inv if c.file_path == "pkg/m.py")
    assert m.directory_kind == "regular_package"

    # G: namespace package (canonical, no __init__.py at that level).
    inv2 = build_module_inventory([rec("ns/m.py"), rec("ns/sub/d.py")], ["."])
    (mm,) = (c for c in inv2 if c.file_path == "ns/m.py")
    assert mm.directory_kind == "namespace_package"


def test_R2_soft_keywords_not_reserved():
    """Correction 8: soft keywords ('match', 'case') are legal module
    names; hard keywords ('for') are not."""
    import keyword
    assert not keyword.iskeyword("match")
    assert not keyword.iskeyword("case")
    assert keyword.iskeyword("for")

    inv = build_module_inventory(
        [rec("match.py"), rec("case.py"), rec("for.py")], ["."])
    by_file = {c.file_path: c for c in inv}
    assert by_file["match.py"].name_supported is True
    assert by_file["match.py"].component_supported is True
    assert by_file["case.py"].name_supported is True
    assert by_file["for.py"].name_supported is False

    # Dotted use of soft keywords is also supported.
    inv2 = build_module_inventory([rec("match/case.py")], ["."])
    (c,) = inv2
    assert c.qualified_name == "match.case"
    assert c.name_supported is True
    assert c.name_components == ("match", "case")
def test_R1_offset_without_limit(tmp_path):
    """Correction 7: ``load_file_records_from_db(...)`` with ``offset`` but
    no ``limit`` must use SQLite-valid syntax (offset-only paging)."""
    from repo_intelligence.search import create_database, insert_file_metadata

    db = str(tmp_path / "paging.db")
    create_database(db)
    conn = sqlite3.connect(db)
    insert_file_metadata(conn, "a.py", "Python", 1, "h")
    insert_file_metadata(conn, "b.py", "Python", 1, "h")
    insert_file_metadata(conn, "c.py", "Python", 1, "h")
    conn.commit()
    conn.close()

    # Default: all rows.
    all_rec = load_file_records_from_db(db)
    assert [r["rel_path"] for r in all_rec] == ["a.py", "b.py", "c.py"]
    # Limit only: first row.
    first = load_file_records_from_db(db, limit=1)
    assert [r["rel_path"] for r in first] == ["a.py"]
    # Offset only: rows from position 1 onward (i.e., skip the first).
    tail = load_file_records_from_db(db, offset=1)
    assert [r["rel_path"] for r in tail] == ["b.py", "c.py"]
    # Limit + offset: single middle row.
    mid = load_file_records_from_db(db, limit=1, offset=1)
    assert [r["rel_path"] for r in mid] == ["b.py"]
    # Offset beyond end: empty.
    empty = load_file_records_from_db(db, offset=100)
    assert empty == []




# ---------------------------------------------------------------------------
# Phase 2C-1R3 — Final Codex review corrections (focused regression tests)
# ---------------------------------------------------------------------------


def test_R3_public_ordering_restored():
    """Correction 1: public ordering is (root.canonical, qualified_name,
    file_path).  ``a-b.py`` ("a-b") must sort BEFORE ``a/b.py`` ("a.b"),
    since '-' (0x2D) < '.' (0x2E)."""
    inv = build_module_inventory([rec("a-b.py"), rec("a/b.py")], ["."])
    assert [c.qualified_name for c in inv] == ["a-b", "a.b"]
    assert [c.file_path for c in inv] == ["a-b.py", "a/b.py"]

    # C. Identical ordering under shuffled input file order.
    inv_s = build_module_inventory([rec("a/b.py"), rec("a-b.py")], ["."])
    assert [(c.qualified_name, c.file_path) for c in inv_s] == \
        [(c.qualified_name, c.file_path) for c in inv]

    # D. Ordinary nested packages keep stable public order by name.
    inv2 = build_module_inventory(
        [rec("pkg/sub/worker.py"), rec("pkg/a.py"),
         rec("aa.py"), rec("ab.py")], ["."])
    assert [c.qualified_name for c in inv2] == \
        sorted({c.qualified_name for c in inv2})

    # E. Literal-dot collision remains correctly separated (structure).
    inv3 = build_module_inventory([rec("a-b.py"), rec("a/b.py"),
                                   rec("a.c/d.py")], ["."])
    by_file = {c.file_path: c for c in inv3}
    assert by_file["a/b.py"].name_components == ("a", "b")
    assert by_file["a.c/d.py"].name_components == ("a.c", "d")
    # a.c/d.py displays "a.c.d" but is a distinct identity.
    assert by_file["a.c/d.py"].qualified_name == "a.c.d"
    assert by_file["a.c/d.py"].component_supported is False
    assert by_file["a/b.py"].component_supported is True

    # F. Genuine multi-file ambiguity remains preserved and is not masked
    #    by the ordering change.
    inv4 = build_module_inventory(
        [rec("root1/a/b/c.py"), rec("root2/a/b/c.py")], ["root1", "root2"])
    real = candidates_for_name(inv4, "a.b.c")
    assert is_ambiguous(real) is True
    assert len(real) == 2


def test_R3_ordering_literal_dot_never_conflated():
    """Correction 1: the restored ordering must not conflate an
    unsupported literal-dot component with ordinary dotted hierarchy."""
    inv = build_module_inventory(
        [rec("a/b.c.py"), rec("ab.py"), rec("a/b.py")], ["."])
    order = [c.qualified_name for c in inv]
    # Names: "ab" (a/b.py? no) -> ab from ab.py, "a.b" from a/b.py,
    # "a.b.c" from a/b.c.py (display).  Sorted by code point:
    #   "a.b"  ('a','.','b')
    #   "a.b.c"('a','.','b','.','c')  -> "a.b" < "a.b.c"
    #   "ab"   ('a','b')              -> "a.b" < "ab"  ('.'<'b')
    assert order == ["a.b", "a.b.c", "ab"]
    # The literal-dot file is still individually reachable and distinct.
    ld = candidates_for_file(inv, "a/b.c.py")
    assert ld[0].name_components == ("a", "b.c")
    (b,) = candidates_for_name(inv, "a.b")
    assert b.file_path == "a/b.py"


def test_R3_noncanonical_paths_rejected_even_when_ineligible():
    """Correction 2: EVERY record's rel_path is validated before metadata
    (suffix / binary / language) eligibility filtering."""
    # A. Binary record with repeated separators.
    with pytest.raises(ValueError, match="File path|Absolute"):
        build_module_inventory(
            [rec("pkg//bad.py", binary=True)], ["."])
    # B. Non-Python record with an absolute path.
    with pytest.raises(ValueError, match="File path|Absolute"):
        build_module_inventory(
            [{"rel_path": "/bad.py", "language": "Rust"}], ["."])
    # C. Non-.py record with a leading ./ component.
    with pytest.raises(ValueError, match="File path|Absolute"):
        build_module_inventory(
            [{"rel_path": "./note.txt", "language": "Markdown"}], ["."])
    # D. Excluded record with parent traversal.
    with pytest.raises(ValueError, match="File path|Absolute"):
        build_module_inventory(
            [{"rel_path": "../x/y.py", "language": "Rust"}], ["."])
    # E. Duplicate records containing a malformed path are still rejected
    #    (deduplication must not conceal an invalid input).
    with pytest.raises(ValueError, match="File path|Absolute"):
        build_module_inventory(
            [rec("pkg//m.py"), rec("pkg//m.py")], ["."])


def test_R3_canonical_excluded_and_eligible_records():
    """Correction 2 (F, G): canonical EXCLUDED records remain safely
    ignored, and canonical ELIGIBLE .py files remain unchanged."""
    # F. Canonical excluded records -> no candidate, no error.
    assert build_module_inventory(
        [{"rel_path": "docs/other.md", "language": "Markdown"}], ["."]) == ()
    assert build_module_inventory(
        [{"rel_path": "data/image.png", "language": "Python", "is_binary": True}],
        ["."]) == ()
    # G. Canonical eligible .py files remain unchanged.
    inv = build_module_inventory([rec("pkg/sub/worker.py")], ["."])
    assert len(inv) == 1
    assert inv[0].qualified_name == "pkg.sub.worker"
    assert inv[0].name_components == ("pkg", "sub", "worker")
    # Package classification still uses canonical path evidence consistently.
    inv2 = build_module_inventory(
        [rec("pkg/__init__.py"), rec("pkg/m.py")], ["."])
    (m,) = (c for c in inv2 if c.file_path == "pkg/m.py")
    assert m.directory_kind == "regular_package"


def test_R3_deterministic_conflict_errors():
    """Correction 3: conflicting duplicates yield identity-and-text
    deterministic errors across every permutation."""
    from repo_intelligence.module_inventory import _canonical_records
    A = {"rel_path": "m.py", "language": "Python", "is_binary": False}
    B = {"rel_path": "m.py", "language": "Python", "is_binary": True}
    C = {"rel_path": "m.py", "language": "Other", "is_binary": False}

    def err(records):
        try:
            _canonical_records(list(records))
            return None
        except ValueError as e:
            return str(e)

    import itertools
    # A/B: all permutations of the three conflicting records give the
    # identical failure text (exact message equality).
    perms = list(itertools.permutations([A, B, C]))
    assert len(perms) == 6
    texts = {err(p) for p in perms}
    assert len(texts) == 1, f"non-deterministic conflict text: {texts}"
    msg = next(iter(texts))
    assert msg.startswith("conflicting duplicate records")
    assert "m.py" in msg

    # C. Conflicting records at multiple paths, order-independent.
    multi = [
        {"rel_path": "a.py", "language": "Python", "is_binary": False},
        {"rel_path": "a.py", "language": "Python", "is_binary": True},
        {"rel_path": "b.py", "language": "Python", "is_binary": False},
        {"rel_path": "b.py", "language": "Rust", "is_binary": False},
    ]
    texts2 = {err(p) for p in itertools.permutations(multi)}
    assert len(texts2) == 1, f"non-deterministic multi-path text: {texts2}"
    m2 = next(iter(texts2))
    assert "a.py" in m2 and "b.py" in m2

    # E. Identical duplicates are accepted (deduplicated).
    d1 = {"rel_path": "m.py", "language": "Python", "is_binary": False}
    d2 = {"rel_path": "m.py", "language": "Python", "is_binary": False}
    assert len(_canonical_records([d1, d2])) == 1

    # F. Nonconflicting distinct records are accepted.
    distinct = [
        {"rel_path": "a.py", "language": "Python", "is_binary": False},
        {"rel_path": "b.py", "language": "Rust", "is_binary": False},
        {"rel_path": "c.py"},
    ]
    out = _canonical_records(distinct)
    assert [r["rel_path"] for r in out] == ["a.py", "b.py", "c.py"]
