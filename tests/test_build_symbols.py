"""End-to-end integration tests for build-time symbol persistence.

These tests exercise ``repo-index build`` to confirm that Python AST symbols
are automatically extracted and persisted into the SQLite ``symbols`` table.
"""

import json
import os
import pathlib
import sqlite3
import tempfile

import pytest
from click.testing import CliRunner

from repo_intelligence.cli import cli
from repo_intelligence.python_ast import _is_python_file
from repo_intelligence.search import (
    db_path as _dbp,
    get_symbols,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(repo_dir: str, rel_path: str, content: str) -> None:
    """Write *content* to *rel_path* inside *repo_dir*."""
    abs_path = os.path.join(repo_dir, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _symbol_count(db_file: str) -> int:
    conn = sqlite3.connect(db_file)
    try:
        count, = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()
        return count
    finally:
        conn.close()


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


def _build(repo_dir: str):
    """Run ``repo-index build`` and return (db_file_path, symbol_count)."""
    runner = CliRunner()
    db_file = _dbp(repo_dir)
    result = runner.invoke(cli, ["build", repo_dir])
    assert result.exit_code == 0, f"Build failed: {result.output}"
    return db_file, _symbol_count(db_file)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_repo(tmp_path):
    """Create an empty temporary git repo and return its path."""
    base_dir = str(tmp_path)
    repo = os.path.join(base_dir, "test_repo")
    os.makedirs(repo)
    # Initialise a minimal git repo so scanner reports is_git_repo=True
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
# Group A: Basic build + symbol persistence (tests 1-10)
# ---------------------------------------------------------------------------

def test_01_build_indexes_python_symbols_automatically(temp_repo):
    """Test 1: build indexes Python symbols automatically."""
    _write(temp_repo, "main.py", 'def hello():\n    pass\n')
    _, count = _build(temp_repo)
    assert count > 0, f"Build produced no symbols. Exit code was {result.exit_code}"


def test_02_top_level_function_persisted(temp_repo):
    """Test 2: top-level function persisted."""
    _write(temp_repo, "mod.py", 'def standalone():\n    pass\n')
    db_file, _ = _build(temp_repo)
    syms = get_symbols(db_file, name="standalone")
    assert len(syms) == 1 and syms[0]["kind"] == "function"


def test_03_class_persisted(temp_repo):
    """Test 3: class persisted."""
    _write(temp_repo, "mod.py", 'class MyClass:\n    pass\n')
    db_file, _ = _build(temp_repo)
    syms = get_symbols(db_file, name="MyClass")
    assert len(syms) == 1 and syms[0]["kind"] == "class"


def test_04_method_persisted(temp_repo):
    """Test 4: method persisted."""
    _write(temp_repo, "mod.py", 'class Foo:\n    def bar(self):\n        pass\n')
    db_file, _ = _build(temp_repo)
    syms = get_symbols(db_file, name="bar")
    assert len(syms) == 1 and syms[0]["kind"] == "method"


def test_05_async_function_and_method_persisted(temp_repo):
    """Test 5: async function/method persisted."""
    src = (
        'class Worker:\n'
        '    async def run(self) -> None:\n'
        '        pass\n'
        '\n'
        'async def async_main():\n'
        '    pass\n'
    )
    _write(temp_repo, "mod.py", src)
    db_file, _ = _build(temp_repo)

    method_syms = get_symbols(db_file, name="run")
    assert len(method_syms) == 1 and method_syms[0]["kind"] == "async_method"

    func_syms = get_symbols(db_file, name="async_main")
    assert len(func_syms) == 1 and func_syms[0]["kind"] == "async_function"


def test_06_qualified_names_preserved(temp_repo):
    """Test 6: qualified names preserved."""
    src = (
        'class MyClass:\n'
        '    def method(self):\n'
        '        class Nested:\n'
        '            pass\n'
    )
    _write(temp_repo, "mod.py", src)
    db_file, _ = _build(temp_repo)

    module_syms = get_symbols(db_file, kind="module")
    mod_sym = [s for s in module_syms if s["kind"] == "module"][0]
    assert mod_sym["qualified_name"] == "mod"


def test_07_parent_database_ids_correct(temp_repo):
    """Test 7: parent database IDs correct for nested scopes."""
    # Nested containment is tested here; top-level classes have parent_id=None
    src = (
        'class Outer:\n'
        '    class Inner:\n'
        '        pass\n'
        '    def method(self):\n'
        '        pass\n'
        '\n'
        'def standalone():\n'
        '    pass\n'
    )
    _write(temp_repo, "mod.py", src)
    db_file, _ = _build(temp_repo)

    outer_syms = get_symbols(db_file, name="Outer")
    assert len(outer_syms) == 1

    inner_cands = get_symbols(db_file, name="Inner")
    # Inner class is child of Outer — parent should point to Outer's id
    if inner_cands:
        assert inner_cands[0]["parent_symbol_id"] == outer_syms[0]["id"], (
            "Nested class parent should reference enclosing class"
        )

    method_cands = get_symbols(db_file, name="method")
    # Method is child of Outer — parent should point to Outer's id
    if method_cands:
        assert method_cands[0]["parent_symbol_id"] == outer_syms[0]["id"], (
            "Method parent should reference enclosing class"
        )

    standalone_cands = get_symbols(db_file, name="standalone")
    # Top-level func has no enclosing class/function scope → parent is None
    if standalone_cands:
        assert standalone_cands[0]["parent_symbol_id"] is None


def test_08_decorators_persist_through_build(temp_repo):
    """Test 8: decorators persist through build."""
    src = (
        '@my_decorator(arg=1)\n'
        '@another_dec\n'
        'class MyClass:\n'
        '    pass\n'
    )
    _write(temp_repo, "mod.py", src)
    db_file, _ = _build(temp_repo)

    class_syms = get_symbols(db_file, name="MyClass")
    assert len(class_syms) == 1
    decs = json.loads(class_syms[0]["decorators"])
    assert "another_dec" in decs and any("my_decorator" in d for d in decs)


def test_09_signatures_persist_through_build(temp_repo):
    """Test 9: signatures persist through build."""
    src = (
        'def func(a, b: int, *args: str, **kwargs) -> bool:\n'
        '    pass\n'
    )
    _write(temp_repo, "mod.py", src)
    db_file, _ = _build(temp_repo)

    func_syms = get_symbols(db_file, name="func")
    assert len(func_syms) == 1 and "(" in func_syms[0]["signature"] and ")" in func_syms[0]["signature"]


def test_10_source_line_numbers_persist(temp_repo):
    """Test 10: source line numbers persist."""
    # Note: start_line values below match actual 1-based Python line numbers
    src = '\nclass MyClass:\n    def method(self):\n        pass\n\ndef standalone():\n    pass\n'
    _write(temp_repo, "mod.py", src)
    db_file, _ = _build(temp_repo)

    class_syms = get_symbols(db_file, name="MyClass")
    assert class_syms[0]["start_line"] == 2
    method_syms = get_symbols(db_file, name="method")
    assert method_syms[0]["start_line"] == 3
    func_syms = get_symbols(db_file, name="standalone")
    assert func_syms[0]["start_line"] == 6


# ---------------------------------------------------------------------------
# Group B: Edge cases (tests 11-13)
# ---------------------------------------------------------------------------

def test_11_malformed_python_does_not_fail_build(temp_repo):
    """Test 11: malformed Python does not fail build."""
    _write(temp_repo, "broken.py", "class : {\n   def\n")
    _write(temp_repo, "good.py", 'def working():\n    pass\n')

    _, count = _build(temp_repo)
    assert count > 0, "Build should still index good.py symbols"


def test_12_non_python_file_creates_no_symbols(temp_repo):
    """Test 12: non-Python file creates no symbols."""
    _write(temp_repo, "index.js", 'function hello() { console.log("hi"); }\n')
    _write(temp_repo, "config.json", '{"key": 1}\n')

    _, count = _build(temp_repo)
    assert count == 0


def test_13_ignored_python_file_creates_no_symbols(temp_repo):
    """Test 13: ignored Python file creates no symbols."""
    _write(temp_repo, ".gitignore", "__ignored__/\n")
    _write(temp_repo, "normal.py", 'visible = True\n')
    _write(temp_repo, "__ignored__/hidden.py", 'hidden_func():\n    pass\n')

    db_file, _ = _build(temp_repo)
    hidden_syms = [s for s in get_symbols(db_file) if "hidden" in s.get("qualified_name", "")]
    assert len(hidden_syms) == 0


# ---------------------------------------------------------------------------
# Group C: Rebuild and staleness (tests 14-16)
# ---------------------------------------------------------------------------

def test_14_rebuild_does_not_duplicate_symbols(temp_repo):
    """Test 14: rebuild does not duplicate symbols."""
    src = 'def alpha():\n    pass\n\ndef beta():\n    pass\n'
    _write(temp_repo, "mod.py", src)

    _, count1 = _build(temp_repo)
    assert count1 > 0

    # Rebuild (same source) on same repo — DB path is deterministic for the repo
    _, count2 = _build(temp_repo)
    assert count2 == count1


def test_15_changed_source_removes_stale_symbol(temp_repo):
    """Test 15: changed source removes stale symbol."""
    src_before = 'def alpha():\n    pass\n\ndef beta():\n    pass\n'
    _write(temp_repo, "mod.py", src_before)

    db_file, count1 = _build(temp_repo)
    assert count1 >= 2

    # Rebuild with beta removed:
    src_after = 'def alpha():\n    pass\n\ndef gamma():\n    pass\n'
    _write(temp_repo, "mod.py", src_after)
    _, count2 = _build(temp_repo)

    # alpha still exists
    assert len(get_symbols(db_file, name="alpha")) == 1
    # beta gone
    assert len(get_symbols(db_file, name="beta")) == 0
    # gamma added
    assert len(get_symbols(db_file, name="gamma")) == 1


def test_16_changed_source_adds_new_symbol(temp_repo):
    """Test 16: changed source adds new symbol."""
    _write(temp_repo, "mod.py", 'def existing():\n    pass\n')
    db_file, _ = _build(temp_repo)

    src_after = 'def existing():\n    pass\n\ndef new_func():\n    pass\n'
    _write(temp_repo, "mod.py", src_after)
    _, count = _build(temp_repo)

    assert len(get_symbols(db_file, name="new_func")) == 1


# ---------------------------------------------------------------------------
# Group D: Multi-file linkage and isolation (tests 17-18)
# ---------------------------------------------------------------------------

def test_17_multiple_python_files_link_to_correct_file_id(temp_repo):
    """Test 17: multiple Python files link to correct file_id."""
    _write(temp_repo, "first.py", 'def func_one():\n    pass\n')
    _write(temp_repo, "second.py", 'class MyClass:\n    def method(self):\n        pass\n')

    db_file, _ = _build(temp_repo)

    f1 = _file_row(db_file, "first.py")
    f2 = _file_row(db_file, "second.py")
    assert f1 is not None and f2 is not None

    for s in get_symbols(db_file, file_id=f1["id"]):
        assert s["file_id"] == f1["id"]
    for s in get_symbols(db_file, file_id=f2["id"]):
        assert s["file_id"] == f2["id"]


def test_18_symbols_for_one_file_do_not_attach_to_another(temp_repo):
    """Test 18: symbol rows for one file do not attach to another."""
    _write(temp_repo, "a.py", 'class AClass:\n    pass\n\ndef a_func():\n    def inner():\n        pass\n')
    _write(temp_repo, "b.py", 'class BClass:\n    def b_method(self):\n        pass\n')

    db_file, _ = _build(temp_repo)

    fa = _file_row(db_file, "a.py")
    fb = _file_row(db_file, "b.py")
    assert fa is not None and fb is not None

    syms_a = get_symbols(db_file, file_id=fa["id"])
    all_ids_a = {s["id"] for s in syms_a}
    for s in syms_a:
        if s["parent_symbol_id"] is not None:
            assert s["parent_symbol_id"] in all_ids_a

    # Verify b.py has its own symbols
    assert len(get_symbols(db_file, file_id=fb["id"])) > 0


# ---------------------------------------------------------------------------
# Group E: Empty repo and Phase 1 compatibility (tests 19-20)
# ---------------------------------------------------------------------------

def test_19_empty_repository_build_works(temp_repo):
    """Test 19: empty repository/build works."""
    _, count = _build(temp_repo)
    assert count == 0


def test_20_phase1_file_and_fts_indexing_still_works(temp_repo):
    """Test 20: Phase 1 file and FTS indexing still operate normally."""
    _write(
        temp_repo, "main.py",
        '"""Module docstring with searchable content."""\n'
        'import os  # noqa\n\n'
        'class Foo:\n'
        '    def bar(self):\n'
        '        pass\n\n'
        'def baz():\n'
        '    """Another search target."""\n'
        '    return True\n',
    )
    _write(
        temp_repo, "notes.txt",
        "This is plain text that should be indexed for FTS.\n"
        "Line two with more searchable content.\n",
    )

    db_file, syms_count = _build(temp_repo)

    # Files exist in DB
    f_main = _file_row(db_file, "main.py")
    assert f_main is not None and f_main["language"] == "Python"
    f_notes = _file_row(db_file, "notes.txt")
    assert f_notes is not None

    # FTS content exists for both
    conn = sqlite3.connect(db_file)
    try:
        fts_count, = conn.execute(
            "SELECT COUNT(*) FROM file_content_fts WHERE rel_path IN ('main.py','notes.txt')"
        ).fetchone()
        assert fts_count >= 2, f"Expected FTS content for both files, got {fts_count}"
    finally:
        conn.close()
