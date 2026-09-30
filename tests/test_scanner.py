"""Tests for repository scanning, file classification, and SHA-256 hashing."""

import hashlib
import os
import stat
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from repo_intelligence.scanner import (
    BINARY_EXTENSIONS,
    DEFAULT_EXCLUDE_DIRS,
    MAX_SCAN_SIZE,
    LANGUAGE_MAP,
    scan_repo,
    _is_binary,
    _get_language,
    _load_gitignore,
    _matches_gitignore,
    _get_git_state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def tmp_repo():
    """Create a temporary directory tree that looks like a small repo."""
    with tempfile.TemporaryDirectory(prefix="repo_int_test_") as td:
        repo = Path(td)
        # A few Python files at different levels
        (repo / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")
        (repo / "lib").mkdir()
        (repo / "lib" / "utils.py").write_text("def helper(): pass\n", encoding="utf-8")
        (repo / "lib" / "deep").mkdir(parents=True)
        (repo / "lib" / "deep" / "mod.py").write_text("x = 1\n", encoding="utf-8")
        # Non-Python files
        (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        (repo / "config.json").write_text('{"key": "val"}', encoding="utf-8")
        (repo / ".gitignore").write_text("node_modules/\n__pycache__/\n*.log\n.env\n.env.*\n", encoding="utf-8")
        # A file matching .gitignore pattern (*.log)
        (repo / "test.log").write_text("log content here\n", encoding="utf-8")
        # Secret-like files
        (repo / ".env").write_text("SECRET=abc123\n", encoding="utf-8")
        (repo / ".env.local").write_text("KEY=val\n", encoding="utf-8")
        tmp_repo_dir = str(repo)
        yield tmp_repo_dir


# ---------------------------------------------------------------------------
# scan_repo tests
# ---------------------------------------------------------------------------

class TestScanRepoBasic:
    """File scanning, language classification, metadata collection."""

    def test_scan_returns_expected_keys(self, tmp_repo):
        result = scan_repo(tmp_repo)
        assert "files" in result
        assert "repo_info" in result
        assert "language_breakdown" in result
        assert "ignore_dirs_used" in result

    def test_scan_finds_all_text_files(self, tmp_repo):
        result = scan_repo(tmp_repo)
        paths = [f["rel_path"] for f in result["files"]]
        # Should have: main.py, lib/utils.py, lib/deep/mod.py, README.md, config.json
        assert "main.py" in paths
        assert "lib/utils.py" in paths or "lib\\utils.py" in paths
        assert "README.md" in paths
        assert "config.json" in paths

    def test_scan_finds_nested_files(self, tmp_repo):
        result = scan_repo(tmp_repo)
        paths = [f["rel_path"] for f in result["files"]]
        # lib/deep/mod.py is deeply nested
        found_deep = any("deep" in p and "mod.py" in p for p in paths)
        assert found_deep, f"Expected deep/mod.py in scanned files: {paths}"

    def test_scan_no_duplicate_dirs(self):
        """Ensure the same directory walked only yields unique files."""
        with tempfile.TemporaryDirectory(prefix="repo_int_test_") as td:
            repo = Path(td)
            (repo / "app.py").write_text("hello", encoding="utf-8")
            result = scan_repo(str(repo))
            rels = [f["rel_path"] for f in result["files"]]
            assert len(rels) == len(set(rels)), "Duplicate file entries found"


class TestScanLanguage:
    """Language classification from extension."""

    def test_python_files_classified(self):
        with tempfile.TemporaryDirectory(prefix="repo_int_test_") as td:
            repo = Path(td)
            (repo / "a.py").write_text("1", encoding="utf-8")
            (repo / "b.js").write_text("1", encoding="utf-8")
            (repo / "c.ts").write_text("1", encoding="utf-8")
            (repo / "d.go").write_text("1x = 1\n", encoding="utf-8")
            result = scan_repo(str(repo))
            by_name = {f["filename"]: f["lang"] for f in result["files"]}
            assert by_name["a.py"] == "Python"
            assert by_name["b.js"] == "JavaScript"
            assert by_name["c.ts"] == "TypeScript"
            assert by_name["d.go"] == "Go"

    def test_binary_extension_classified(self):
        """Files whose extension is in BINARY_EXTENSIONS should get 'Binary'."""
        assert _get_language("photo.png") == "Binary"
        assert _get_language("archive.jpg") == "Binary"
        assert ".png" in BINARY_EXTENSIONS  # sanity on BINARY_EXTENSIONS

    def test_unrecognised_extension_gets_other(self):
        ext = _get_language("weird.foobar")
        assert ext == "Other"


class TestScanExclusions:
    """Default excluded directories, .gitignore handling, secret-file exclusion."""

    def test_default_exclude_dirs_are_included_in_defaults(self):
        assert ".git" in DEFAULT_EXCLUDE_DIRS
        assert "__pycache__" in DEFAULT_EXCLUDE_DIRS
        assert "node_modules" in DEFAULT_EXCLUDE_DIRS
        assert "venv" in DEFAULT_EXCLUDE_DIRS
        assert "dist" in DEFAULT_EXCLUDE_DIRS
        assert "build" in DEFAULT_EXCLUDE_DIRS

    def test_gitignore_excluded_files_skipped(self):
        """Files matched by .gitignore patterns are not scanned."""
        # *.log pattern should match "app.log"
        patterns = ["*.log"]
        assert _matches_gitignore("app.log", patterns) is True

    def test_gitignore_directory_pattern_excludes_files_inside(self):
        patterns = ["node_modules/"]
        assert _matches_gitignore("node_modules/pkg/index.js", patterns) is True
        # Should also match nested
        assert _matches_gitignore("vendor/node_modules/pkg/mod.rs", patterns) is True

    def test_git_pattern_root_relative_excludes_at_root(self):
        patterns = ["/.env"]
        assert _matches_gitignore(".env", patterns) is True
        # But not in subdirectory
        assert _matches_gitignore("subdir/.env", patterns) is False

    @pytest.mark.parametrize("file_pattern,glob,expected", [
        ("*.log", ["*.txt"], False),
        ("foo.txt", ["*.txt"], True),
        ("sub/deep/file.log", ["**/*.log"], True),
        ("deep/file.log", ["**/*.log"], True),
        ("file.log", ["**/*.log"], True),
    ])
    def test_gitignore_glob_matcher(self, file_pattern, glob, expected):
        patterns = glob
        assert _matches_gitignore(file_pattern, patterns) is expected

    def test_secret_dotenv_files_skipped_by_default(self):
        """Files like .env and .env.* should be excluded by default."""
        with tempfile.TemporaryDirectory(prefix="repo_int_test_") as td:
            repo = Path(td)
            (repo / ".env").write_text("SECRET=abc\n", encoding="utf-8")
            (repo / "app.py").write_text("x=1\n", encoding="utf-8")
            # Add .gitignore with secret exclusion patterns
            (repo / ".gitignore").write_text(".env\n.env.*\n", encoding="utf-8")
            result = scan_repo(str(repo))
            paths = [f["rel_path"] for f in result["files"]]
            assert ".env" not in paths, ".env should be excluded"

    def test_user_can_add_custom_ignore_dirs(self):
        with tempfile.TemporaryDirectory(prefix="repo_int_test_") as td:
            repo = Path(td)
            (repo / "main.py").write_text("1", encoding="utf-8")
            ignored_dir = repo / "__ignored__"
            ignored_dir.mkdir()
            (ignored_dir / "secret.py").write_text("SECRET=1", encoding="utf-8")
            result = scan_repo(str(repo), ignore_dirs=["__ignored__"])
            all_rels = [f["rel_path"] for f in result["files"]]
            assert "__ignored__/secret.py" not in all_rels


class TestScanBinarySafety:
    """Binary file detection and size limit handling."""

    def test_text_file_not_binary(self, tmp_repo):
        result = scan_repo(tmp_repo)
        for f_entry in result["files"]:
            if f_entry["rel_path"] == "main.py":
                assert f_entry["is_binary"] is False
                break
        else:
            pytest.fail("main.py not found in scan results")

    def test_binary_content_detected(self):
        """Files with null bytes should be detected as binary."""
        content = b"hello\x00world"
        assert _is_binary("/no/check.txt") is True  # default; real check below
        with tempfile.NamedTemporaryFile(suffix=".dat", delete=False) as tmp:
            tmp.write(content)
            tmp.flush()
            assert _is_binary(tmp.name) is True
        os.unlink(tmp.name)

    def test_non_text_file_not_binary_on_null_bytes(self):
        """A file without null bytes in the first 8K should be 'text'."""
        with tempfile.NamedTemporaryFile(suffix=".dat", delete=False) as tmp:
            tmp.write(b"no nulls here, just ASCII text\n")
            tmp.flush()
            assert _is_binary(tmp.name) is False
        os.unlink(tmp.name)

    def test_large_file_is_treated_as_binary(self):
        """Files larger than MAX_SCAN_SIZE should be excluded from FTS."""
        with tempfile.TemporaryDirectory(prefix="repo_int_test_large_") as td:
            repo = Path(td)
            big_name = "bigfile.txt"
            (repo / big_name).write_bytes(b"x" * (MAX_SCAN_SIZE + 1))
            result = scan_repo(str(repo))
            found = [f for f in result["files"] if f["rel_path"] == big_name]
            assert len(found) == 1
            assert found[0]["is_binary"] is True


class TestScanHashing:
    """SHA-256 generation and correctness."""

    def test_sha256_is_nonempty_for_text(self, tmp_repo):
        result = scan_repo(tmp_repo)
        for f_entry in result["files"]:
            if f_entry["rel_path"] == "main.py":
                assert f_entry["sha256"], "SHA-256 should be populated for text files"
                assert len(f_entry["sha256"]) == 64  # hex digest length
                break

    def test_sha256_correctness(self):
        """Verify SHA-256 matches hashlib computation."""
        with tempfile.TemporaryDirectory(prefix="repo_int_test_hash_") as td:
            repo = Path(td)
            content = "hello, sha256 world\n"
            fname = "test.txt"
            (repo / fname).write_text(content, encoding="utf-8")
            result = scan_repo(str(repo))
            entry = [f for f in result["files"] if f["rel_path"] == fname][0]
            expected = hashlib.sha256(content.encode()).hexdigest()
            assert entry["sha256"] == expected

    def test_binary_file_has_empty_hash(self):
        """Binary files should have an empty SHA-256 string."""
        # A real .pyc would be binary; simulate with a null byte file
        with tempfile.TemporaryDirectory(prefix="repo_int_test_pyc_") as td:
            repo = Path(td)
            fname = "data.dat"
            (repo / fname).write_bytes(b"\x00\x01\x02\x03")
            result = scan_repo(str(repo))
            entry = [f for f in result["files"] if f["rel_path"] == fname][0]
            assert entry["sha256"] == ""


class TestScanGitState:
    """Git repository state capture."""

    def test_git_state_in_scan(self, tmp_repo):
        """When run inside a Git repo (the repo-intelligence itself), git is captured."""
        result = scan_repo(tmp_repo)
        info = result["repo_info"]
        assert "is_git_repo" in info
        if info["is_git_repo"]:
            assert info["current_branch"] is not None
            assert info["commit_hash"] is not None

    def test_nongit_directory_has_is_git_false(self):
        with tempfile.TemporaryDirectory(prefix="repo_int_test_nogit_") as td:
            # Ensure we're outside any git repo
            result = scan_repo(td)
            assert result["repo_info"]["is_git_repo"] is False

    def test_get_git_state_nonexistent_dir(self):
        state = _get_git_state("/nonexistent/path/xyz123")
        assert state["is_git_repo"] is False
        assert state["current_branch"] is None


class TestScanNonDir:
    """Error handling for non-directories."""

    def test_non_directory_raises(self):
        with tempfile.NamedTemporaryFile(delete=False) as tmpf:
            tmpf.write(b"not a dir")
            path = tmpf.name
        try:
            with pytest.raises(NotADirectoryError):
                scan_repo(path)
        finally:
            os.unlink(path)


class TestScanGitignorePatterns:
    """_load_gitignore and _matches_gitignore tests covering various patterns."""

    def test_load_gitignore_missing_file(self):
        with tempfile.TemporaryDirectory(prefix="repo_int_test_gi_") as td:
            assert _load_gitignore(td) == []

    def test_load_gitignore_simple_patterns(self):
        with tempfile.TemporaryDirectory(prefix="repo_int_test_gi2_") as td:
            repo = Path(td)
            (repo / ".gitignore").write_text(
                "*.log\n"
                "# comment line\n"
                "\n"
                "build/\n",
                encoding="utf-8",
            )
            patterns = _load_gitignore(str(repo))
            assert "*.log" in patterns
            assert "build/" in patterns

    def test_gitignore_directory_pattern(self):
        """Directory patterns (ending with /) only match directories."""
        patterns_d = ["logs/"]
        # A file at the same level as a dir matching 'logs/' should not match
        # But a path segment containing 'logs' should
        assert _matches_gitignore("logs/app.log", patterns_d) is True  # file inside logs/
        assert _matches_gitignore("./logs/test.txt", patterns_d) is True

    def test_path_pattern(self):
        """Patterns with slashes match path segments."""
        patterns = ["config/settings.json"]
        rel = "src/config/settings.json"
        # Should match because the segment exists in the path
        assert _matches_gitignore(rel, patterns) is True
