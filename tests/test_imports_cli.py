"""Focused CLI tests for ``repo-index imports`` (Phase 2B-4).

Covers:
  - unfiltered listing, filters (file, module, name, kind)
  - combined filter AND semantics
  - aliases, multiple names, name-order preservation
  - wildcard imports, relative import levels, nested imports
  - deterministic ordering
  - pagination (limit/offset) without duplicated import statements
  - human-readable and JSON output
  - empty-result behavior
  - db-path handling (explicit + default external)
  - read-only guarantee (queries do not mutate the indexed repository or DB)

Reuses helper conventions from ``test_symbols_cli.py`` (``_make_temp_repo``,
``_build_and_get_db``) rather than introducing new fixtures.
"""

import json
import os
import pathlib
import tempfile

import pytest
from click.testing import CliRunner

from repo_intelligence.cli import cli


# ---------------------------------------------------------------------------
# Helpers (mirrors test_symbols_cli.py conventions)
# ---------------------------------------------------------------------------

def _write_repo_files(repo_dir: str, files: dict[str, str]) -> None:
    """Write *files* (rel_path → content) inside *repo_dir*."""
    for rel_path, content in files.items():
        abs_path = os.path.join(repo_dir, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as fh:
            fh.write(content)


def _build_and_get_db(repo_dir: str) -> str:
    """Run ``repo-index build`` on *repo_dir* and return the default DB path."""
    from repo_intelligence.search import db_path
    db_file = db_path(repo_dir)
    result = CliRunner().invoke(cli, ["build", repo_dir])
    assert result.exit_code == 0, f"Build failed: {result.output}"
    return db_file


def _make_temp_repo(base_dir: str, files: dict[str, str] | None = None) -> str:
    """Create a minimal repository on disk and return its path."""
    repo = os.path.join(str(base_dir), "imports_repo")
    os.makedirs(repo, exist_ok=True)
    _write_repo_files(repo, files or {})
    return repo


def _snapshot_tree(root: str) -> dict:
    """Snapshot file mtimes under *root* (excluding the DB) for mutation checks."""
    snap = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            abs_p = os.path.join(dirpath, name)
            st = os.stat(abs_p)
            snap[os.path.relpath(abs_p, root)] = (st.st_mtime_ns, st.st_size)
    return snap


# ---------------------------------------------------------------------------
# Fixture: one repository with every persisted import shape we need to test
# ---------------------------------------------------------------------------

# ``main.py`` and ``pkg/mod.py`` carry every import shape: plain, aliased,
# multi-name (non-alphabetical order to prove order preservation), wildcard,
# relative, and a nested (function-body) import.
REPO_FILES = {
    "main.py": (
        "import os\n"
        "import sys\n"
        "import numpy as np\n"
        "from pathlib import Path\n"
        "from package import zeta, alpha as a2\n"
        "from . import sibling\n"
        "from ..foo import Bar\n"
        "from star_pkg import *\n"
        "\n"
        "def body():\n"
        "    import nested_mod\n"
        "    return nested_mod\n"
    ),
    "pkg/mod.py": (
        "import os.path\n"
        "import sys\n"
        "from typing import Dict as Mapping, List\n"
    ),
}


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def imports_repo(tmp_path):
    """Built repository + default DB path covering all import shapes."""
    repo = _make_temp_repo(str(tmp_path), REPO_FILES)
    db_file = _build_and_get_db(repo)
    return repo, db_file


def _invoke(runner, db_file, *args):
    return runner.invoke(cli, ["imports", "--db-path", db_file, *args])


def _json_data(runner, db_file, *args):
    result = _invoke(runner, db_file, *args, "--json")
    assert result.exit_code == 0, result.output
    return result, json.loads(result.output)


# ---------------------------------------------------------------------------
# 1. Unfiltered listing / 17. human-readable / 18. JSON
# ---------------------------------------------------------------------------

class TestUnfilteredListing:
    def test_unfiltered_listing(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1])
        assert result.exit_code == 0
        # 9 imports in main.py, 3 in pkg/mod.py = 12 statements
        assert "Showing 12 of indexed import statements." in result.output

    def test_text_output_is_source_like(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1])
        assert "import os" in result.output
        assert "import numpy as np" in result.output
        assert "from pathlib import Path" in result.output
        assert "from package import zeta, alpha as a2" in result.output
        assert "from star_pkg import *" in result.output

    def test_text_output_shares_location(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1])
        assert "/main.py:" in result.output
        assert "/pkg/mod.py:" in result.output

    def test_import_vs_from_import_distinguished(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1])
        # `import foo` (plain) vs `from foo import bar` (from_import) differ
        # visibly in the text output.
        plain = [l for l in result.output.splitlines() if l.strip().startswith("import ")]
        froms = [l for l in result.output.splitlines() if "from " in l and "import" in l]
        assert plain and froms

    def test_json_output_shape(self, runner, imports_repo):
        _result, data = _json_data(runner, imports_repo[1])
        assert isinstance(data, list)
        assert len(data) == 12
        row = data[0]
        for field in ("source_file", "kind", "module", "level",
                      "imported_names", "start_line", "start_col"):
            assert field in row, f"Missing JSON field: {field}"

    def test_json_no_resolution_fields(self, runner, imports_repo):
        """Phase 2B-4 must not add resolved/dependency fields."""
        _result, data = _json_data(runner, imports_repo[1])
        forbidden = {
            "resolved_file_id", "resolved_path", "target_module",
            "is_local", "dependency_type", "resolved_to",
        }
        for row in data:
            assert not (set(row) & forbidden), "No resolution/dependency fields allowed"


# ---------------------------------------------------------------------------
# 2–6. Filters
# ---------------------------------------------------------------------------

class TestFilters:
    def test_file_filter(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1], "--file", "pkg/mod.py")
        assert result.exit_code == 0
        assert "Showing 3 of" in result.output
        assert "os.path" in result.output
        assert "Dict as Mapping" in result.output
        # main.py imports must not leak in
        assert "pathlib" not in result.output

    def test_file_filter_exact_path_only(self, runner, imports_repo):
        """--file matches the source file exactly (no prefix semantics)."""
        result = _invoke(runner, imports_repo[1], "--file", "pkg")
        assert "No imports found" in result.output

    def test_module_filter(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1], "--module", "numpy")
        assert result.exit_code == 0
        assert "Showing 1 of" in result.output
        assert "import numpy as np" in result.output

    def test_module_filter_dotted(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1], "--module", "os.path")
        assert result.exit_code == 0
        assert "Showing 1 of" in result.output
        assert "/pkg/mod.py" in result.output

    def test_module_filter_relative_represented_persisted(self, runner, imports_repo):
        """Relative import: module stored as 'foo' with level 2 (no synthesized name)."""
        result = _invoke(runner, imports_repo[1], "--module", "foo")
        assert "Showing 1 of" in result.output
        assert "Bar" in result.output

    def test_name_filter(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1], "--name", "Path")
        assert result.exit_code == 0
        assert "Showing 1 of" in result.output
        assert "from pathlib import Path" in result.output

    def test_name_filter_matches_imported_name_not_alias(self, runner, imports_repo):
        """`alpha as a2`: matched by 'alpha', not by its alias 'a2'."""
        result = _invoke(runner, imports_repo[1], "--name", "alpha")
        assert "Showing 1 of" in result.output

        result_alias = _invoke(runner, imports_repo[1], "--name", "a2")
        assert "No imports found" in result_alias.output
        assert "Showing" not in result_alias.output

    def test_kind_filter(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1], "--kind", "from_import")
        assert result.exit_code == 0
        # main.py: 5 from_imports, pkg/mod.py: 1 → 6
        assert "Showing 6 of" in result.output
        for line in result.output.splitlines():
            if not line.strip():
                continue
            stripped = line.strip()
            assert stripped.startswith("from ") or "Showing" in stripped, (
                f"Non-from_import line leaked: {line!r}"
            )

    def test_kind_filter_plain_import(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1], "--kind", "import")
        assert result.exit_code == 0
        # main.py: 3, nested: 1, pkg/mod.py: 2 → 6
        assert "Showing 6 of" in result.output

    def test_kind_filter_case_insensitive(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1], "--kind", "FROM_IMPORT")
        assert result.exit_code == 0
        assert "Showing 6 of" in result.output

    def test_combined_file_and_module(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1],
                         "--file", "pkg/mod.py", "--module", "os.path")
        assert "Showing 1 of" in result.output

        # Same module in a different file → AND excludes it
        result_no = _invoke(runner, imports_repo[1],
                            "--file", "main.py", "--module", "os.path")
        assert "No imports found" in result_no.output

    def test_combined_module_and_name(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1],
                         "--module", "package", "--name", "zeta")
        assert "Showing 1 of" in result.output

        result_no = _invoke(runner, imports_repo[1],
                            "--module", "pathlib", "--name", "zeta")
        assert "No imports found" in result_no.output

    def test_combined_kind_and_name(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1],
                         "--kind", "from_import", "--name", "Bar")
        assert "Showing 1 of" in result.output

        # nested_mod is kind 'import', so from_import AND nested_mod → empty
        result_no = _invoke(runner, imports_repo[1],
                            "--kind", "from_import", "--name", "nested_mod")
        assert "No imports found" in result_no.output

    def test_no_match_empty_result(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1], "--module", "nonexistent_pkg")
        assert result.exit_code == 0
        assert "No imports found" in result.output
        assert "nonexistent_pkg" in result.output  # filter echoed in message


# ---------------------------------------------------------------------------
# 7–12. Import shapes
# ---------------------------------------------------------------------------

class TestImportShapes:
    def test_aliases(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1], "--module", "numpy")
        assert "import numpy as np" in result.output

    def test_multiple_names_from_one_from_statement(self, runner, imports_repo):
        _result, data = _json_data(
            runner, imports_repo[1], "--module", "package", "--json"
        )
        assert len(data) == 1
        names = [e["name"] for e in data[0]["imported_names"]]
        assert names == ["zeta", "alpha"]

    def test_name_order_preserved_not_alphabetical(self, runner, imports_repo):
        """zeta comes before alpha in source → must also come first in output."""
        _result, data = _json_data(
            runner, imports_repo[1], "--module", "package", "--json"
        )
        rows = data[0]["imported_names"]
        assert [r["name"] for r in rows] == ["zeta", "alpha"]
        assert rows[0]["asname"] is None
        assert rows[1]["asname"] == "a2"
        assert [r["position"] for r in rows] == [0, 1]

        result = _invoke(runner, imports_repo[1], "--module", "package")
        assert "zeta, alpha as a2" in result.output

    def test_wildcard_import(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1], "--module", "star_pkg")
        assert "from star_pkg import *" in result.output

    def test_relative_import_level_1(self, runner, imports_repo):
        _result, data = _json_data(
            runner, imports_repo[1], "--name", "sibling"
        )
        assert len(data) == 1
        assert data[0]["level"] == 1
        assert data[0]["module"] == ""

    def test_relative_import_level_2(self, runner, imports_repo):
        _result, data = _json_data(
            runner, imports_repo[1], "--name", "Bar"
        )
        assert data[0]["level"] == 2
        assert data[0]["module"] == "foo"

    def test_nested_import(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1], "--name", "nested_mod")
        assert "Showing 1 of" in result.output
        assert "import nested_mod" in result.output
        # Nested import sits on line 11 of main.py (inside body())
        assert "/main.py:11" in result.output

    def test_all_required_fields_round_trip(self, runner, imports_repo):
        _result, data = _json_data(runner, imports_repo[1], "--module", "package")
        row = data[0]
        assert row["source_file"] == "main.py"
        assert row["kind"] == "from_import"
        assert row["module"] == "package"
        assert row["level"] == 0
        assert row["start_line"] == 5
        assert row["start_col"] == 0
        assert row["imported_names"], "imported_names must be populated"


# ---------------------------------------------------------------------------
# 13. Deterministic ordering
# ---------------------------------------------------------------------------

class TestDeterministicOrdering:
    def test_order_by_file_then_line(self, runner, imports_repo):
        _result, data = _json_data(runner, imports_repo[1])
        keys = [(d["source_file"], d["start_line"], d["start_col"], d["id"])
                for d in data]
        assert keys == sorted(keys), "Result order must be (file, line, col, id)"

    def test_repeated_queries_identical(self, runner, imports_repo):
        first = _json_data(runner, imports_repo[1])[1]
        second = _json_data(runner, imports_repo[1])[1]
        assert first == second

    def test_nested_import_ordered_after_top_level(self, runner, imports_repo):
        _result, data = _json_data(
            runner, imports_repo[1], "--file", "main.py"
        )
        lines = [d["start_line"] for d in data]
        assert lines == sorted(lines)
        assert lines[-1] == 11  # the function-body import is last


# ---------------------------------------------------------------------------
# 14–16. Pagination
# ---------------------------------------------------------------------------

class TestPagination:
    def test_limit(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1], "--limit", "3")
        assert "Showing 3 of" in result.output

    def test_limit_and_offset_window(self, runner, imports_repo):
        first = _json_data(runner, imports_repo[1], "--limit", "4")[1]
        second = _json_data(runner, imports_repo[1], "--limit", "4", "--offset", "4")[1]
        assert len(first) == 4 and len(second) == 4
        all_ids = [d["id"] for d in first] + [d["id"] for d in second]
        assert len(set(all_ids)) == 8, "Pagination windows must not overlap"

    def test_offset_beyond_end(self, runner, imports_repo):
        result = _invoke(runner, imports_repo[1], "--offset", "999")
        assert "No imports found" in result.output

    def test_no_duplicate_import_statements_with_name_filter(self, runner, imports_repo):
        """A multi-name import must not repeat merely because it matches --name.

        `from package import zeta, alpha as a2` matches neither name twice;
        querying one name still yields exactly one statement.
        """
        result = _invoke(runner, imports_repo[1], "--name", "zeta")
        assert "Showing 1 of" in result.output
        assert result.output.count("from package import") == 1

    def test_name_filter_returns_full_statement_with_all_names(self, runner, imports_repo):
        """Filtering by one name of a multi-name statement keeps the whole fact."""
        _result, data = _json_data(runner, imports_repo[1], "--name", "zeta")
        assert len(data) == 1
        names = [e["name"] for e in data[0]["imported_names"]]
        assert names == ["zeta", "alpha"]

    def test_paginate_name_filter_stable(self, runner, imports_repo):
        """--name pagination across files matching one name is disjoint/stable."""
        first = _json_data(runner, imports_repo[1], "--name", "sys",
                           "--limit", "1")[1]
        second = _json_data(runner, imports_repo[1], "--name", "sys",
                            "--limit", "1", "--offset", "1")[1]
        assert len(first) == 1 and len(second) == 1
        assert first[0]["id"] != second[0]["id"]


# ---------------------------------------------------------------------------
# 19–22. db-path handling, empty result, read-only
# ---------------------------------------------------------------------------

class TestDbPathAndSafety:
    def test_invalid_db_path_fails(self, runner):
        result = runner.invoke(
            cli, ["imports", "--db-path", "/nonexistent/dir/index.db"]
        )
        assert result.exit_code == 1
        assert "No index database found" in result.output

    def test_explicit_db_path(self, runner, tmp_path):
        """--db-path pointing at a valid DB works."""
        repo = _make_temp_repo(str(tmp_path), {"only.py": "import json\n"})
        db_file = _build_and_get_db(repo)
        result = _invoke(runner, db_file)
        assert "import json" in result.output

    def test_default_db_path_from_cwd(self, runner, tmp_path, monkeypatch):
        """With no --db-path, the DB is resolved from the CWD (external path)."""
        repo = _make_temp_repo(str(tmp_path / "cwdcase"), {"only.py": "import json\n"})
        _build_and_get_db(repo)
        monkeypatch.chdir(repo)
        result = runner.invoke(cli, ["imports"])
        assert result.exit_code == 0, result.output
        assert "import json" in result.output

    def test_queries_do_not_mutate_repository(self, runner, imports_repo):
        repo, db_file = imports_repo
        before = _snapshot_tree(repo)

        for args in (
            [],
            ["--file", "main.py"],
            ["--module", "package"],
            ["--name", "zeta"],
            ["--kind", "from_import", "--limit", "2", "--offset", "1"],
            ["--json"],
        ):
            result = _invoke(runner, db_file, *args)
            assert result.exit_code == 0

        assert _snapshot_tree(repo) == before, "Querying must not modify the repo"

    def test_queries_do_not_mutate_database(self, runner, imports_repo):
        import sqlite3

        repo, db_file = imports_repo
        conn = sqlite3.connect(db_file)
        counts_before = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("files", "imports", "import_names", "symbols")
        }
        conn.close()

        for _ in range(3):
            _invoke(runner, db_file, "--json")

        conn = sqlite3.connect(db_file)
        counts_after = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("files", "imports", "import_names", "symbols")
        }
        conn.close()
        assert counts_before == counts_after, "Querying must not modify the DB"


# ---------------------------------------------------------------------------
# Query-layer contract for --name (subquery semantics, no duplicate parents)
# ---------------------------------------------------------------------------

class TestNameFilterQueryContract:
    def test_name_filter_no_parent_duplicates(self, tmp_path):
        """Direct get_imports() check: multi-name import, name matched → 1 row."""
        from repo_intelligence.search import get_imports

        repo = _make_temp_repo(
            str(tmp_path),
            {"m.py": "from package import zeta, alpha as a2\n"},
        )
        db_file = _build_and_get_db(repo)
        rows = get_imports(db_file, name_filter="zeta")
        assert len(rows) == 1
        # And the full statement (both names) is still reported.
        assert [e["name"] for e in rows[0]["import_names"]] == ["zeta", "alpha"]

    def test_name_filter_composes_with_module(self, tmp_path):
        from repo_intelligence.search import get_imports

        repo = _make_temp_repo(
            str(tmp_path),
            {
                "a.py": "from package import zeta\n",
                "b.py": "from other import zeta\n",
            },
        )
        db_file = _build_and_get_db(repo)
        rows = get_imports(db_file, name_filter="zeta", module="package")
        assert len(rows) == 1
        assert rows[0]["rel_path"] == "a.py"
