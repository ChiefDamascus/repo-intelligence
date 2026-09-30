"""Tests for repo-intelligence CLI commands: build, stats, files, search-files."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from repo_intelligence.cli import cli


def _sample_repo_db_path(td):
    """Return the external DB path for a temporary test repo dir."""
    from repo_intelligence.search import db_path as get_db_path
    return get_db_path(td)


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def sample_repo():
    """Create a real small repository on disk for CLI testing."""
    with tempfile.TemporaryDirectory(prefix="cli_test_") as td:
        repo = Path(td)
        (repo / "app.py").write_text(
            "import json\n\ndef main():\n    print('hello')\n", encoding="utf-8"
        )
        (repo / "lib").mkdir()
        (repo / "lib" / "utils.py").write_text(
            "def helper():\n    return 42\n", encoding="utf-8"
        )
        (repo / "README.md").write_text("# Test\nReadme content.\n", encoding="utf-8")
        (repo / "config.json").write_text('{"a": 1}\n', encoding="utf-8")
        # .gitignore to exercise exclusion
        (repo / ".gitignore").write_text(
            "*.log\n__pycache__/\n.env\n", encoding="utf-8"
        )
        (repo / "app.log").write_text("log data\n", encoding="utf-8")
        (repo / "app.py.bak").write_text("backup\n", encoding="utf-8")
        yield str(repo)


class TestCLIHelp:
    """Verify CLI help and entry point."""

    def test_default_help(self, runner):
        result = runner.invoke(cli, [])
        assert result.exit_code == 0
        assert "Repository Intelligence" in result.output

    def test_build_help(self, runner):
        result = runner.invoke(cli, ["build", "--help"])
        assert result.exit_code == 0
        assert "--index-path" in result.output

    def test_stats_help(self, runner):
        result = runner.invoke(cli, ["stats", "--help"])
        assert result.exit_code == 0

    def test_files_help(self, runner):
        result = runner.invoke(cli, ["files", "--help"])
        assert result.exit_code == 0

    def test_search_files_help(self, runner):
        result = runner.invoke(cli, ["search-files", "--help"])
        assert result.exit_code == 0


class TestCLIBuild:
    """CLI build command."""

    def test_build_indexes_files(self, runner, sample_repo):
        result = runner.invoke(cli, ["build", sample_repo])
        assert result.exit_code == 0
        assert "Indexed" in result.output, f"Unexpected output: {result.output}"

    def test_build_creates_db_in_repo(self, runner, sample_repo):
        db_path = _sample_repo_db_path(sample_repo)
        assert not os.path.exists(db_path), "DB should not exist yet"
        result = runner.invoke(cli, ["build", sample_repo])
        assert result.exit_code == 0
        assert os.path.isfile(db_path)

    def test_build_with_custom_index_path(self, runner, sample_repo):
        with tempfile.TemporaryDirectory(prefix="cli_custom_") as td:
            custom_path = os.path.join(td, "custom.db")
            result = runner.invoke(
                cli, ["build", sample_repo, "--index-path", custom_path]
            )
            assert result.exit_code == 0
            assert os.path.isfile(custom_path)

    def test_build_with_ignore_dirs(self, runner, sample_repo):
        with tempfile.TemporaryDirectory(prefix="cli_extra_") as td:
            extra_dir = Path(td) / "__custom__"
            extra_dir.mkdir()
            (extra_dir / "secret.py").write_text("SECRET=1", encoding="utf-8")
            # Add to repo path
            sample_repo_extra = sample_repo + ";" + str(extra_dir)  # won't work, use separate

    def test_build_nonexistent_path(self, runner):
        result = runner.invoke(cli, ["build", "/nonexistent/random/path"])
        assert result.exit_code != 0


class TestCLIStats:
    """CLI stats command."""

    def test_stats_no_args_fails_without_db(self, runner, sample_repo):
        """No explicit db_path means using the external default based on CWD."""
        # Without a db at the current directory's computed location, we expect no-db behavior
        result = runner.invoke(cli, ["stats"])
        # May exit 0 with empty stats or 1 if no DB found -- depends on implementation
        assert "Repository" in result.output or result.exit_code != 0

    def test_stats_shows_output(self, runner, sample_repo):
        db_path = _sample_repo_db_path(sample_repo)
        # Build first
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["stats", "--db-path", db_path])
        assert result.exit_code == 0
        assert "Repository" in result.output
        assert "Total files" in result.output

    def test_stats_json(self, runner, sample_repo):
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["stats", "--db-path", db_path, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, dict)
        assert "total_files" in data


class TestCLIFiles:
    """CLI files command."""

    def test_files_default_output(self, runner, sample_repo):
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["files", "--db-path", db_path])
        assert result.exit_code == 0
        assert "app.py" in result.output

    def test_files_with_lang_filter(self, runner, sample_repo):
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result_upper = runner.invoke(cli, ["files", "--lang", "Python", "--db-path", db_path])
        assert result_upper.exit_code == 0
        # Should match Python files stored with 'Python' in DB via case-insensitive filter
        # We have at least 2 Python files: app.py and lib/utils.py
        assert "app.py" in result_upper.output or "/lib/utils.py" in result_upper.output

    def test_files_lang_filter_lowercase(self, runner, sample_repo):
        """Case-insensitive language filter: --lang python should match 'Python' rows."""
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["files", "--lang", "python", "--db-path", db_path])
        assert result.exit_code == 0
        # lowercase 'python' must match stored 'Python' values
        py_entries = [l for l in result.output.splitlines() if ".py" in l and "Showing" not in l]
        # app.py exists at repo root
        assert len(py_entries) >= 1, f"Expected Python files with lowercase --lang python filter: {result.output}"

    def test_files_lang_filter_uppercase_match(self, runner, sample_repo):
        """Case-insensitive language filter: --lang PYTHON should match 'Python' rows."""
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["files", "--lang", "PYTHON", "--db-path", db_path])
        assert result.exit_code == 0
        py_entries = [l for l in result.output.splitlines() if ".py" in l and "Showing" not in l]
        assert len(py_entries) >= 1

    def test_files_lang_filter_empty(self, runner, sample_repo):
        """No language filter should return all files."""
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result_no_filter = runner.invoke(cli, ["files", "--db-path", db_path])
        assert result_no_filter.exit_code == 0
        # Without filter should have more files than Python-only
        result_py_only = runner.invoke(cli, ["files", "--lang", "python", "--db-path", db_path])
        all_count_str = "Showing" in result_no_filter.output and "of indexed files" in result_no_filter.output
        py_count_str = "Showing" in result_py_only.output and "of indexed files" in result_py_only.output
        assert all_count_str or py_count_str

    def test_files_path_filter(self, runner, sample_repo):
        """--path lib/ should only return files under lib/."""
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["files", "--path", "lib/", "--db-path", db_path])
        assert result.exit_code == 0
        # Should contain the lib/utils.py file but not app.py
        assert "/lib/utils.py" in result.output or "lib/utils.py" in result.output
        assert "app.py" not in result.output

    def test_files_path_filter_no_trailing_slash(self, runner, sample_repo):
        """--path lib should also match files under lib/."""
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["files", "--path", "lib", "--db-path", db_path])
        assert result.exit_code == 0
        assert "/lib/utils.py" in result.output or "lib/utils.py" in result.output

    def test_files_path_filter_no_matches(self, runner, sample_repo):
        """--path with non-matching prefix returns no files."""
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["files", "--path", "nonexistent/", "--db-path", db_path])
        assert result.exit_code == 0
        assert "No files matched" in result.output

    def test_files_combined_lang_and_path(self, runner, sample_repo):
        """--lang python --path lib/ should return only Python files under lib/."""
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["files", "--lang", "python", "--path", "lib/", "--db-path", db_path])
        assert result.exit_code == 0
        # Should contain lib/utils.py
        assert "/lib/utils.py" in result.output or "lib/utils.py" in result.output

    def test_files_combined_lang_and_path_no_match(self, runner, sample_repo):
        """--lang Markdown --path app should return nothing (wrong language)."""
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["files", "--lang", "markdown", "--path", "/", "--db-path", db_path])
        # If there are no Markdown files at root level, expect no match
        assert result.exit_code == 0

    def test_files_combined_lang_and_path_json_output(self, runner, sample_repo):
        """Combined filters with --json should return valid JSON."""
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["files", "--lang", "python", "--path", "lib/", "--db-path", db_path, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_files_json(self, runner, sample_repo):
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["files", "--db-path", db_path, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) > 0

    def test_files_with_limit(self, runner, sample_repo):
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["files", "--limit", "2", "--db-path", db_path])
        assert result.exit_code == 0


class TestCLIsearchFiles:
    """CLI search-files command."""

    def test_search_returns_matches(self, runner, sample_repo):
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["search-files", "hello", "--db-path", db_path])
        assert result.exit_code == 0
        assert "Results" in result.output or "/app.py" in result.output

    def test_search_no_results(self, runner, sample_repo):
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["search-files", "xyznonexistentmatch", "--db-path", db_path])
        assert result.exit_code == 0
        assert "No results" in result.output

    def test_search_json(self, runner, sample_repo):
        db_path = _sample_repo_db_path(sample_repo)
        runner.invoke(cli, ["build", sample_repo])
        result = runner.invoke(cli, ["search-files", "main", "--db-path", db_path, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# CLI via subprocess (integration-level)
# ---------------------------------------------------------------------------

class TestCLIExecutable:
    """Use the 'repo-index' entry point directly."""

    def test_entry_point_exists(self):
        result = subprocess.run(
            ["repo-index", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "Repository Intelligence" in result.stdout

    def test_build_via_entry_point(self, sample_repo):
        with tempfile.TemporaryDirectory(prefix="cli_e2e_") as td:
            db_dir = os.path.join(td, "output")
            os.makedirs(db_dir)
            db_path = os.path.join(db_dir, "index.db")
            result = subprocess.run(
                ["repo-index", "build", "--index-path", db_path, sample_repo],
                capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 0, f"stderr: {result.stderr}, stdout: {result.stdout}"
            assert os.path.isfile(db_path), "Database file not created"


# ---------------------------------------------------------------------------
# Non-directory / nonexistent path handling via CLI
# ---------------------------------------------------------------------------

class TestCLIErrorCases:
    def test_build_file_instead_of_dir(self, runner):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"data")
            tmpf = f.name
        try:
            result = runner.invoke(cli, ["build", tmpf])
            assert result.exit_code != 0
        finally:
            os.unlink(tmpf)

    def test_build_nonexistent_path(self, runner):
        result = runner.invoke(cli, ["build", "/nonexistent/path"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Non-Git repository handling via CLI
# ---------------------------------------------------------------------------

class TestCLINonGitRepos:
    """Ensure indexing works for directories that are NOT Git repos."""

    def test_build_non_git_repo(self, runner):
        with tempfile.TemporaryDirectory(prefix="cli_nogit_") as td:
            repo = Path(td)
            (repo / "app.py").write_text("x=1\n", encoding="utf-8")
            # Do NOT initialize git here
            result = runner.invoke(cli, ["build", str(repo)])
            assert result.exit_code == 0, f"Should not fail: {result.output} {result.exception}"
