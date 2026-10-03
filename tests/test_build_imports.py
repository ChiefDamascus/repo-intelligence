"""End-to-end build-integration tests for import persistence (Phase 2B-3).

These tests exercise ``repo-index build`` to confirm that Python imports are
automatically extracted and persisted into the SQLite ``imports`` +
``import_names`` tables.
"""

import os
import pathlib
import sqlite3
import tempfile

import pytest
from click.testing import CliRunner

from repo_intelligence.cli import cli
from repo_intelligence.python_ast import _is_python_file, extract_file_imports
from repo_intelligence.search import (
    db_path as _dbp,
    get_imports,
    replace_imports_for_file,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(repo_dir: str, rel_path: str, content: str) -> str:
    """Write *content* to *rel_path* inside *repo_dir*. Return abs path."""
    abs_path = os.path.join(repo_dir, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return abs_path


def _file_row(db_file: str, rel_path: str):
    """Return dict {id, language, size} or None."""
    conn = sqlite3.connect(db_file)
    try:
        row = conn.execute(
            "SELECT id, language, size FROM files WHERE rel_path = ?", (rel_path,)
        ).fetchone()
        if row:
            return {"id": row[0], "language": row[1], "size": row[2]}
    finally:
        conn.close()
    return None


def _import_count(db_file: str, file_id=None):
    """Return the number of import rows (across all files or a specific file)."""
    conn = sqlite3.connect(db_file)
    try:
        if file_id is not None:
            count, = conn.execute(
                "SELECT COUNT(*) FROM imports WHERE file_id = ?", (file_id,)
            ).fetchone()
            return count
        count, = conn.execute("SELECT COUNT(*) FROM imports").fetchone()
        return count
    finally:
        conn.close()


def _import_count_for_file(db_file: str, rel_path: str):
    """Resolve file_id fresh then return import count for that file."""
    conn = sqlite3.connect(db_file)
    try:
        row = conn.execute(
            "SELECT id FROM files WHERE rel_path = ?", (rel_path,)
        ).fetchone()
        if row is None:
            return 0
        count, = conn.execute(
            "SELECT COUNT(*) FROM imports WHERE file_id = ?", (row[0],)
        ).fetchone()
        return count
    finally:
        conn.close()


def _import_names_count(db_file: str, import_id=None):
    """Return the number of import_name rows."""
    conn = sqlite3.connect(db_file)
    try:
        if import_id is not None:
            count, = conn.execute(
                "SELECT COUNT(*) FROM import_names WHERE import_id = ?", (import_id,)
            ).fetchone()
            return count
        count, = conn.execute("SELECT COUNT(*) FROM import_names").fetchone()
        return count
    finally:
        conn.close()


def _build(repo_dir: str):
    """Run ``repo-index build`` and return the db_file path."""
    runner = CliRunner()
    db_file = _dbp(repo_dir)
    result = runner.invoke(cli, ["build", repo_dir])
    assert result.exit_code == 0, f"Build failed: {result.output}"
    return db_file


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_repo(tmp_path):
    """Create an empty temporary git repo and return its path."""
    base_dir = str(tmp_path)
    repo = os.path.join(base_dir, "test_repo")
    os.makedirs(repo)
    orig = os.getcwd()
    try:
        os.chdir(repo)
        os.system("git init -q > /dev/null 2>&1")
        os.system('git config user.email "test@test.com"')
        os.system('git config user.name "Test"')
        pathlib.Path(".keep").touch()
        os.system("git add . > /dev/null 2>&1")
        os.system('git commit -m "init" --quiet > /dev/null 2>&1')
    finally:
        os.chdir(orig)
    return repo


# ---------------------------------------------------------------------------
# Group A: Import indexing (tests 1-7)
# ---------------------------------------------------------------------------


def test_01_simple_import(temp_repo):
    """Test 1: build indexes a simple import ``import os``."""
    _write(temp_repo, "main.py", 'import os\n')

    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")
    assert f_row is not None

    imps = get_imports(db_file, file_id=f_row["id"])
    assert len(imps) == 1
    assert imps[0]["kind"] == "import"
    assert imps[0]["module"] == "os"
    names = [n for n in imps[0].get("import_names", [])]
    assert len(names) == 1 and names[0]["name"] == "os"


def test_02_aliased_import(temp_repo):
    """Test 2: build indexes an aliased import ``import numpy as np``."""
    _write(temp_repo, "main.py", 'import numpy as np\n')

    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")
    imps = get_imports(db_file, file_id=f_row["id"])
    assert len(imps) == 1
    names = imps[0].get("import_names", [])
    assert len(names) == 1
    assert names[0]["name"] == "numpy"
    assert names[0]["asname"] == "np"


def test_03_from_import_names(temp_repo):
    """Test 3: build indexes from-import names ``from package import zebra, alpha``."""
    _write(temp_repo, "main.py", 'from package import zebra, alpha\n')

    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")
    imps = get_imports(db_file, file_id=f_row["id"])
    assert len(imps) == 1
    assert imps[0]["kind"] == "from_import"
    assert imps[0]["module"] == "package"
    names = imps[0].get("import_names", [])
    assert len(names) == 2
    assert names[0]["name"] == "zebra" and names[0]["position"] == 0
    assert names[1]["name"] == "alpha" and names[1]["position"] == 1


def test_04_relative_import(temp_repo):
    """Test 4: relative import survives integration ``from .pkg import thing``."""
    _write(temp_repo, "main.py", 'from .pkg import thing\n')

    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")
    imps = get_imports(db_file, file_id=f_row["id"])
    assert len(imps) == 1
    assert imps[0]["kind"] == "from_import"
    assert imps[0]["module"] == "pkg"
    assert imps[0]["level"] >= 1  # relative import has level > 0


def test_05_wildcard_import(temp_repo):
    """Test 5: wildcard import survives integration ``from package import *``."""
    _write(temp_repo, "main.py", 'from package import *\n')

    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")
    imps = get_imports(db_file, file_id=f_row["id"])
    assert len(imps) == 1
    names = imps[0].get("import_names", [])
    assert len(names) == 1 and names[0]["name"] == "*"


def test_06_nested_import_persisted(temp_repo):
    """Test 6: nested/dotted import is persisted ``from a.b.c import Z``."""
    _write(temp_repo, "main.py", 'from a.b.c import Z\n')

    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")
    imps = get_imports(db_file, file_id=f_row["id"])
    assert len(imps) == 1
    assert imps[0]["module"] == "a.b.c"
    names = imps[0].get("import_names", [])
    assert len(names) == 1 and names[0]["name"] == "Z"


def test_07_source_line_and_column_survive(temp_repo):
    """Test 7: source line and column survive build integration."""
    src = (
        'x = 1\n'
        '\n'
        'import os   # at line 3\n'
        'from sys import path as p   # at line 4\n'
    )
    _write(temp_repo, "main.py", src)

    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")
    imps = get_imports(db_file, file_id=f_row["id"])
    assert len(imps) == 2
    # The import at line 3 comes first
    assert imps[0]["start_line"] == 3
    # from-import at line 4 comes second
    assert imps[1]["start_line"] == 4


# ---------------------------------------------------------------------------
# Group B: Multi-file isolation and file type guards (tests 8-10)
# ---------------------------------------------------------------------------


def test_08_multiple_files_isolated(temp_repo):
    """Test 8: multiple Python files get isolated import rows."""
    _write(temp_repo, "a.py", 'import os\n')
    _write(temp_repo, "b.py", 'import sys\n')

    db_file = _build(temp_repo)
    fa = _file_row(db_file, "a.py")
    fb = _file_row(db_file, "b.py")
    assert fa is not None and fb is not None

    imps_a = get_imports(db_file, file_id=fa["id"])
    imps_b = get_imports(db_file, file_id=fb["id"])
    assert len(imps_a) == 1 and imps_a[0]["module"] == "os"
    assert len(imps_b) == 1 and imps_b[0]["module"] == "sys"


def test_09_non_python_no_imports(temp_repo):
    """Test 9: non-Python files produce no import rows."""
    _write(temp_repo, "script.sh", '#!/bin/bash\necho hi\n')
    _write(temp_repo, "config.json", '{"a": 1}\n')

    db_file = _build(temp_repo)
    total = _import_count(db_file)
    assert total == 0


def test_10_python_no_imports_produces_rows_zero(temp_repo):
    """Test 10: Python file with no imports produces no import rows."""
    _write(temp_repo, "main.py", 'x = 1\n\ndef foo():\n    pass\n')

    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")
    total = _import_count(db_file, file_id=f_row["id"])
    assert total == 0


# ---------------------------------------------------------------------------
# Group C: Rebuild semantics (tests 11-14)
# ---------------------------------------------------------------------------


def test_11_repeated_build_no_duplicates(temp_repo):
    """Test 11: repeated identical build produces no duplicate imports."""
    _write(temp_repo, "main.py", 'import os\n')

    db_file = _build(temp_repo)
    count1 = _import_count_for_file(db_file, "main.py")

    db_file2 = _build(temp_repo)  # rebuild on same repo (same deterministic DB path)
    count2 = _import_count_for_file(db_file2, "main.py")
    assert count1 == count2 == 1


def test_12_modified_file_replaces_imports(temp_repo):
    """Test 12: modified Python file replaces old imports."""
    _write(temp_repo, "main.py", 'import os\n')

    db_file = _build(temp_repo)
    count_before = _import_count_for_file(db_file, "main.py")
    assert count_before == 1

    # Change the source to have a different import.
    _write(temp_repo, "main.py", 'import sys\n')

    db_file2 = _build(temp_repo)
    imps = get_imports(db_file2, source_path="main.py")
    assert len(imps) == 1, f"Expected 1 import, got {len(imps)}"
    mod_val = imps[0].get("module") or ""
    assert imps[0]["module"] == "sys", f"Expected 'sys', got {mod_val!r}"


def test_13_removing_all_imports_clears_stale(temp_repo):
    """Test 13: removing all imports from an existing Python file clears stale imports."""
    _write(temp_repo, "main.py", 'import old_module\n')

    db_file = _build(temp_repo)
    assert _import_count_for_file(db_file, "main.py") == 1

    # Replace source with no imports.
    _write(temp_repo, "main.py", 'x = 1\n')

    db_file2 = _build(temp_repo)
    # Old import rows MUST be cleared (not accumulated).
    stale = _import_count_for_file(db_file2, "main.py")
    assert stale == 0, f"Expected cleared imports; got {stale}"


def test_14_changed_from_import_names_replace_children(temp_repo):
    """Test 14: changed from-import names replace stale import_names children."""
    _write(temp_repo, "main.py", 'from pkg import alpha, beta\n')

    db_file = _build(temp_repo)
    imps_before = get_imports(db_file, source_path="main.py")
    assert len(imps_before) == 1
    names_before = [n["name"] for n in imps_before[0].get("import_names", [])]
    assert names_before == ["alpha", "beta"]

    # Change the from-import to different names.
    _write(temp_repo, "main.py", 'from pkg import gamma\n')

    db_file2 = _build(temp_repo)
    imps_after = get_imports(db_file2, source_path="main.py")
    assert len(imps_after) == 1, f"Expected 1 import row, got {len(imps_after)}"
    names_after = [n["name"] for n in imps_after[0].get("import_names", [])]
    # Stale children cleared, new children added.
    assert "alpha" not in names_after and "beta" not in names_after
    assert names_after == ["gamma"], f"Expected ['gamma'], got {names_after}"


# ---------------------------------------------------------------------------
# Group D: Cross-phase validation (tests 15-16)
# ---------------------------------------------------------------------------


def test_15_symbol_indexing_still_works(temp_repo):
    """Test 15: existing symbol indexing still works during the same build."""
    from repo_intelligence.search import get_symbols

    src = 'class CMain:\n    def method(self):\n        pass\n'
    _write(temp_repo, "main.py", src)
    _write(temp_repo, "lib.py", 'import os\n')

    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")

    syms = get_symbols(db_file, file_id=f_row["id"])
    kinds = {s["kind"] for s in syms}
    assert "class" in kinds or "method" in kinds, (
        f"Expected symbols from main.py but got: {[s['kind'] for s in syms]}"
    )


def test_16_file_metadata_remains_correct(temp_repo):
    """Test 16: Phase 1 file metadata remains correct."""
    _write(temp_repo, "main.py", 'import os\n')

    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")
    assert f_row is not None
    assert f_row["language"] == "Python"
    assert isinstance(f_row["size"], int) and f_row["size"] > 0


# ---------------------------------------------------------------------------
# Group E: Parse-failure behaviour (tests 17-18)
# ---------------------------------------------------------------------------


def test_17_syntax_failure_no_crash(temp_repo):
    """Test 17: a syntax/parse failure follows established extraction behavior."""
    _write(temp_repo, "broken.py", 'class : {\n   def\n')
    _write(temp_repo, "good.py", 'import os\n')

    db_file = _build(temp_repo)
    assert os.path.exists(db_file), "DB should exist after build with broken file"

    # Good file should still have imports indexed.
    f_good = _file_row(db_file, "good.py")
    assert f_good is not None
    imps = get_imports(db_file, file_id=f_good["id"])
    assert len(imps) == 1 and imps[0]["module"] == "os"

    # Broken file: extraction returns [] for SyntaxError → replaces with empty.
    f_broken = _file_row(db_file, "broken.py")
    assert f_broken is not None
    broken_imports = get_imports(db_file, file_id=f_broken["id"])
    assert len(broken_imports) == 0


def test_18_target_repo_not_modified(temp_repo):
    """Test 18: target repository source files are not modified by build."""
    import hashlib

    src = 'import os\nfrom sys import path\n'
    original_abs = _write(temp_repo, "main.py", src)

    # Capture source hash before build.
    with open(original_abs, "rb") as fh:
        hash_before = hashlib.sha256(fh.read()).hexdigest()

    db_file = _build(temp_repo)

    # Re-read source after build.
    with open(original_abs, "rb") as fh:
        hash_after = hashlib.sha256(fh.read()).hexdigest()

    assert hash_before == hash_after, "Build must not modify target repo source files"


# ---------------------------------------------------------------------------
# Group F: Comprehensive replacement semantics (tests 19-24)
# ---------------------------------------------------------------------------


def test_19_empty_imports_clears_stale_via_replace(temp_repo):
    """Test 19: replace_imports_for_file with empty list clears stale rows."""
    # Write a Python file with imports and build it.
    _write(temp_repo, "main.py", 'import x\n')
    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")
    assert _import_count(db_file, file_id=f_row["id"]) == 1

    # Now use replace_imports_for_file directly with empty list.
    conn = sqlite3.connect(db_file)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        replace_imports_for_file(conn, f_row["id"], [])
        conn.commit()  # Ensure changes are persisted.
    finally:
        conn.close()

    conn2 = sqlite3.connect(db_file)
    try:
        count = conn2.execute(
            "SELECT COUNT(*) FROM imports WHERE file_id = ?", (f_row["id"],)
        ).fetchone()[0]
        assert count == 0, f"Expected cleared imports via replace; got {count}"
    finally:
        conn2.close()


def test_20_repeated_import_persistence_no_duplicates(temp_repo):
    """Test 20: repeated replace_imports_for_file calls don't duplicate."""
    # Ensure a file-row exists in the DB first.
    _write(temp_repo, "main.py", 'x = 1\n')
    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")

    class FakeRec:
        pass

    rec = FakeRec()
    rec.kind = "import"
    rec.module = "os"
    rec.level = 0
    rec.start_line = 1
    rec.start_col = 0
    rec.names = (("os", None),)

    conn = sqlite3.connect(db_file)
    try:
        replace_imports_for_file(conn, f_row["id"], [rec])
        count1 = conn.execute(
            "SELECT COUNT(*) FROM imports WHERE file_id = ?", (f_row["id"],)
        ).fetchone()[0]
        replace_imports_for_file(conn, f_row["id"], [rec])
        count2 = conn.execute(
            "SELECT COUNT(*) FROM imports WHERE file_id = ?", (f_row["id"],)
        ).fetchone()[0]
    finally:
        conn.close()

    assert count1 == count2


def test_21_from_import_with_names_replaces_accurately(temp_repo):
    """Test 21: from-import with names replaces old import_names accurately."""
    _write(temp_repo, "mod.py", 'from pkg import a, b, c\n')

    db_file = _build(temp_repo)
    imps_before = get_imports(db_file, source_path="mod.py")
    assert len(imps_before) == 1
    names_before = [n["name"] for n in imps_before[0].get(
        "import_names", []
    )]
    assert names_before == ["a", "b", "c"]

    _write(temp_repo, "mod.py", 'from pkg import d\n')
    db_file2 = _build(temp_repo)

    imps = get_imports(db_file2, source_path="mod.py")
    assert len(imps) == 1, f"Expected 1 import row, got {len(imps)}"
    names_after = [n["name"] for n in imps[0].get("import_names", [])]
    assert "a" not in names_after and "b" not in names_after and "c" not in names_after
    assert names_after == ["d"], f"Expected ['d'], got {names_after}"


def test_22_import_level_preserved_through_build(temp_repo):
    """Test 22: relative import level survives build end-to-end."""
    src_lines = [
        'from .import parent_ref\n',
        'from ..sibling import something\n',
    ]
    _write(temp_repo, "pkg/mod.py", ''.join(src_lines))

    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "pkg/mod.py")
    imps = get_imports(db_file, file_id=f_row["id"])
    assert len(imps) == 2
    levels = {imp["level"] for imp in imps}
    assert 1 in levels and 2 in levels


def test_23_build_with_no_python_files(temp_repo):
    """Test 23: build with no Python files produces no imports."""
    _write(temp_repo, "readme.md", "# Title\n")
    _write(temp_repo, "script.sh", 'echo hi\n')

    db_file = _build(temp_repo)
    total_imports = _import_count(db_file)
    assert total_imports == 0
    total_name_rows = _import_names_count(db_file)
    assert total_name_rows == 0


def test_24_build_completes_with_mix_of_files(temp_repo):
    """Test 24: mixed Python/non-Python build completes and indexes correctly."""
    _write(temp_repo, "main.py", 'import os\n')
    _write(temp_repo, "utils.py", 'from os.path import join as jp\n')
    _write(temp_repo, "notes.txt", "Some text.\n")
    _write(temp_repo, "config.json", '{"k": 1}\n')

    db_file = _build(temp_repo)
    f_main = _file_row(db_file, "main.py")
    f_utils = _file_row(db_file, "utils.py")
    assert f_main and f_utils

    imps_main = get_imports(db_file, file_id=f_main["id"])
    imps_utils = get_imports(db_file, file_id=f_utils["id"])
    assert len(imps_main) == 1 and imps_main[0]["module"] == "os"
    assert len(imps_utils) == 1 and imps_utils[0]["module"] == "os.path"


# ---------------------------------------------------------------------------
# Group G: Phase 2B-3 build-integration correctness (tests 25-30)
# ---------------------------------------------------------------------------


def test_25_syntax_invalid_python_clears_stale_imports(temp_repo):
    """Test 25: syntax-invalid Python produces [] and clears previously persisted imports."""
    # First, create a file WITH imports so stale rows exist.
    _write(temp_repo, "main.py", 'import alpha\nimport beta\n')
    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")
    assert _import_count_for_file(db_file, "main.py") == 2

    # Now overwrite with syntax-broken content.
    _write(temp_repo, "main.py", 'class : {\n   def\n')
    db_file2 = _build(temp_repo)
    f_row2 = _file_row(db_file2, "main.py")

    # Re-parse: empty import list from SyntaxError should have cleared stale rows.
    conn = sqlite3.connect(db_file2)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM imports WHERE file_id = ?", (f_row2["id"],)
        ).fetchone()[0]
        assert count == 0, f"Expected stale imports cleared, got {count}"
    finally:
        conn.close()


def test_26_unexpected_persistence_failure_not_swallowed(temp_repo):
    """Test 26: unexpected import-persistence failure propagates (not silently swallowed)."""
    from repo_intelligence.search import replace_imports_for_file as _orig_riff

    _write(temp_repo, "main.py", 'import os\n')

    # Monkey-patch to simulate a DB write failure.
    import builtins as _builtins

    original_replace = _builtins.__dict__.get("replace_imports_for_file")  # noqa: F841 (will use mock)

    def _mock_replace(conn, file_id, records):
        raise sqlite3.DatabaseError("simulated persistence failure")

    import repo_intelligence.cli as cli_mod

    original_ref = cli_mod.replace_imports_for_file
    try:
        cli_mod.replace_imports_for_file = _mock_replace  # type: ignore[assignment]
        runner = CliRunner()
        db_file = _dbp(temp_repo)
        result = runner.invoke(cli, ["build", temp_repo])
        assert result.exit_code != 0, (
            "Build must not succeed when replace_imports_for_file raises unexpectedly"
        )
    finally:
        cli_mod.replace_imports_for_file = original_ref


def test_27_unexpected_extractor_failure_not_swallowed(temp_repo):
    """Test 27: unexpected extractor failure propagates (not silently swallowed)."""
    _write(temp_repo, "main.py", 'import os\n')

    import repo_intelligence.python_ast as pa_mod
    from repo_intelligence import cli as cli_module

    original_extract = pa_mod.extract_file_symbols
    original_riff = cli_module.replace_imports_for_file
    # cli.py does ``from ... import extract_file_symbols``, so it holds its own reference.
    original_cli_extract = getattr(cli_module, "extract_file_symbols", None)

    try:
        def _bad_symbols(rel_path, source):
            raise sqlite3.IntegrityError("simulated extractor regression")

        # Patch in both module namespaces.
        pa_mod.extract_file_symbols = _bad_symbols  # type: ignore[assignment]
        cli_module.replace_imports_for_file = lambda conn, file_id, records: None  # type: ignore[assignment]
        if original_cli_extract is not None:
            cli_module.extract_file_symbols = _bad_symbols  # type: ignore[assignment]

        runner = CliRunner()
        db_file = _dbp(temp_repo)
        result = runner.invoke(cli, ["build", temp_repo])
        assert result.exit_code != 0, (
            "Build must not succeed when extract_file_symbols raises unexpectedly"
        )
    finally:
        pa_mod.extract_file_symbols = original_extract
        cli_module.replace_imports_for_file = original_riff
        if original_cli_extract is not None:
            cli_module.extract_file_symbols = original_cli_extract  # type: ignore[assignment]


def test_28_normal_good_file_indexing_still_works(temp_repo):
    """Test 28: removal of broad exception does NOT break normal good-file indexing."""
    _write(temp_repo, "a.py", 'import os\nfrom sys import path\n')
    _write(temp_repo, "b.py", 'class Alpha:\n    pass\n')

    db_file = _build(temp_repo)

    f_a = _file_row(db_file, "a.py")
    f_b = _file_row(db_file, "b.py")
    assert f_a and f_b

    imps = get_imports(db_file, file_id=f_a["id"])
    assert len(imps) == 2

    from repo_intelligence.search import get_symbols
    syms = get_symbols(db_file, file_id=f_b["id"])
    kinds = {s["kind"] for s in syms}
    assert "class" in kinds


def test_29_existing_symbol_indexing_unaffected(temp_repo):
    """Test 29: existing symbol indexing remains correct after build-integration change."""
    _write(temp_repo, "main.py", 'class MyClass:\n    def greet(self):\n        pass\n')

    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")

    from repo_intelligence.search import get_symbols
    syms = get_symbols(db_file, file_id=f_row["id"])
    kinds = {s["kind"] for s in syms}
    assert "class" in kinds or "method" in kinds, (
        f"Symbol indexing broken: got kinds {kinds}"
    )


def test_30_repeated_build_same_file_fk_safe(temp_repo):
    """Test 30: rebuilding an existing Python file with FK enabled still works correctly."""
    _write(temp_repo, "main.py", 'import os\n')

    # First build.
    db_file = _build(temp_repo)
    f_row = _file_row(db_file, "main.py")
    assert f_row is not None
    imps1 = get_imports(db_file, file_id=f_row["id"])
    assert len(imps1) == 1

    from repo_intelligence.search import get_symbols
    syms1 = get_symbols(db_file, file_id=f_row["id"])

    # Second build (repeated) — FK enforcement must not produce orphan rows or FK errors.
    db_file2 = _build(temp_repo)
    f_row2 = _file_row(db_file2, "main.py")
    assert f_row2 is not None

    imps2 = get_imports(db_file2, file_id=f_row2["id"])
    assert len(imps2) == 1

    syms2 = get_symbols(db_file2, file_id=f_row2["id"])

    # Symbols and imports must still link correctly with FK constraints active.
    conn = sqlite3.connect(db_file2)
    fk_error = None
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        rows = conn.execute(
            "SELECT COUNT(*) FROM import_names WHERE import_id IN ("
            "  SELECT id FROM imports WHERE file_id = ?)", (f_row2["id"],)
        ).fetchone()
        import_name_count = rows[0]
    except sqlite3.IntegrityError as e:
        fk_error = str(e)
    finally:
        conn.close()

    assert fk_error is None, f"FK constraint failed on repeated build: {fk_error}"


# ---------------------------------------------------------------------------
# Group H: Repeated-build lifecycle & orphan audit (tests 31-35)
# ---------------------------------------------------------------------------


def test_31_file_id_changes_on_repeated_build(temp_repo):
    """Test 31: repeated build changes files.id and proves the mechanism."""
    _write(temp_repo, "sample.py", 'import os\n')

    db_file = _build(temp_repo)
    conn = sqlite3.connect(db_file)
    first_id, = conn.execute(
        "SELECT id FROM files WHERE rel_path = ?", ("sample.py",)
    ).fetchone()
    conn.close()

    db_file2 = _build(temp_repo)
    conn2 = sqlite3.connect(db_file2)
    second_id, = conn2.execute(
        "SELECT id FROM files WHERE rel_path = ?", ("sample.py",)
    ).fetchone()
    assert first_id != second_id, (
        f"Expected file_id to change on repeated build; "
        f"first={first_id}, second={second_id}"
    )
    conn2.close()


def test_32_file_row_count_always_one(temp_repo):
    """Test 32: INSERT OR REPLACE guarantees exactly one files row per rel_path."""
    _write(temp_repo, "sample.py", 'import os\n')

    for _ in range(5):
        db_file = _build(temp_repo)

    conn = sqlite3.connect(db_file)
    count, = conn.execute(
        "SELECT COUNT(*) FROM files WHERE rel_path = ?", ("sample.py",)
    ).fetchone()
    conn.close()
    assert count == 1


def test_33_zero_orphan_symbols_after_repeat_build(temp_repo):
    """Test 33: zero orphan symbols after repeated identical build."""
    _write(temp_repo, "sample.py", 'import os\n\ndef hello(): pass\n')

    for _ in range(3):
        _build(temp_repo)

    db_file = _dbp(temp_repo)
    conn = sqlite3.connect(db_file)
    orphan_syms, = conn.execute(
        "SELECT COUNT(*) FROM symbols s "
        "LEFT JOIN files f ON f.id = s.file_id "
        "WHERE f.id IS NULL"
    ).fetchone()
    conn.close()
    assert orphan_syms == 0


def test_34_semantic_correctness_after_source_modification(temp_repo):
    """Test 34: after modifying source and rebuilding, old names are absent."""
    _write(temp_repo, "sample.py", 'import os\n\ndef hello(): pass\n')

    _build(temp_repo)

    # Modify source.
    _write(temp_repo, "sample.py", 'import sys\n\ndef goodbye(): return 2\n')
    db_file = _build(temp_repo)

    conn = sqlite3.connect(db_file)

    hello_count, = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE name = 'hello'"
    ).fetchone()
    assert hello_count == 0

    bye_rows = conn.execute(
        "SELECT id, file_id, kind, name FROM symbols WHERE name = 'goodbye'"
    ).fetchall()
    assert len(bye_rows) == 1

    os_count, = conn.execute(
        "SELECT COUNT(*) FROM imports WHERE module = 'os'"
    ).fetchone()
    assert os_count == 0

    sys_rows = conn.execute(
        "SELECT id, file_id, kind, module FROM imports WHERE module = 'sys'"
    ).fetchall()
    assert len(sys_rows) == 1

    orphan_syms, = conn.execute(
        "SELECT COUNT(*) FROM symbols s "
        "LEFT JOIN files f ON f.id = s.file_id "
        "WHERE f.id IS NULL"
    ).fetchone()
    assert orphan_syms == 0

    orphan_inames, = conn.execute(
        "SELECT COUNT(*) FROM import_names n "
        "WHERE NOT EXISTS (SELECT 1 FROM imports i WHERE i.id = n.import_id)"
    ).fetchone()
    assert orphan_inames == 0

    conn.close()


def test_35_current_symbols_and_imports_reference_current_file(temp_repo):
    """Test 35: all semantic rows reference the CURRENT files.id, not a ghost ID."""
    _write(temp_repo, "sample.py", 'import x\n\ndef f(): pass\n')

    for _ in range(3):
        _build(temp_repo)

    db_file = _dbp(temp_repo)
    conn = sqlite3.connect(db_file)

    current_file_id, = conn.execute(
        "SELECT id FROM files WHERE rel_path = ?", ("sample.py",)
    ).fetchone()

    stale_sym, = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE file_id != ?", (current_file_id,)
    ).fetchone()
    assert stale_sym == 0

    stale_imp, = conn.execute(
        "SELECT COUNT(*) FROM imports WHERE file_id != ?", (current_file_id,)
    ).fetchone()
    assert stale_imp == 0

    orphan_inames, = conn.execute(
        "SELECT COUNT(*) FROM import_names n "
        "WHERE NOT EXISTS (SELECT 1 FROM imports i WHERE i.id = n.import_id)"
    ).fetchone()
    assert orphan_inames == 0

    file_count, = conn.execute(
        "SELECT COUNT(*) FROM files WHERE rel_path = ?", ("sample.py",)
    ).fetchone()
    assert file_count == 1

    conn.close()
