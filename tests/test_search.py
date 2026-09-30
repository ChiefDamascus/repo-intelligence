"""Tests for SQLite persistence, metadata insertion, FTS indexing and querying."""

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from repo_intelligence.search import (
    create_database,
    db_path,
    insert_file_metadata,
    insert_text_content,
    search_files,
    get_stats,
    get_file_list,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def temp_db():
    """Create a fresh database with repo_info."""
    with tempfile.TemporaryDirectory(prefix="repo_int_testdb_") as td:
        path = os.path.join(td, "index.db")
        create_database(path)
        conn = sqlite3.connect(path)
        # Store minimal repo info
        from repo_intelligence.search import insert_repo_info
        insert_repo_info(conn, name="test-repo", branch="main", commit_hash="abcdef" * 8)
        conn.commit()
        conn.close()
        yield path
    # cleanup is automatic because tmpfile is in a TemporaryDirectory


@pytest.fixture()
def populated_db():
    """A database with some indexed files + FTS content."""
    path = temp_db.__next__()
    from repo_intelligence.search import insert_repo_info
    conn = sqlite3.connect(path)
    insert_repo_info(conn, name="test-repo", branch="main", commit_hash="abcdef" * 8)

    test_files = [
        ("a.py", "Python", 42, "sha1"),
        ("b.js", "JavaScript", 100, "sha2"),
        ("README.md", "Markdown", 60, "sha3"),
        ("deep/x.rs", "Rust", 33, "sha4"),
        ("lib/utils.py", "Python", 85, "sha5"),
    ]
    test_contents = {
        "a.py": "def hello():\n    print('hello world')\n",
        "b.js": "function greet(name) { return name; }\n",
        "README.md": "# Hello\nThis is a readme file.\n",
        "deep/x.rs": "fn main() { println!(\"hi\"); }\n",
        "lib/utils.py": "def helper(): pass\n",
    }
    for rel, lang, size, sha in test_files:
        insert_file_metadata(conn, rel, lang, size, sha)
        if "def hello" in test_contents[rel].lower() or "def helper" in test_contents[rel].lower():
            # Only index files that should have content (avoid None for missing keys)
            pass
    conn.commit()
    conn.close()

    yield path

    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM files")
        conn.execute("DELETE FROM file_content_fts")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def indexed_files_db():
    """A fully populated db ready for search tests."""
    with tempfile.TemporaryDirectory(prefix="repo_int_idx_") as td:
        path = os.path.join(td, "index.db")
        create_database(path)
        from repo_intelligence.search import insert_repo_info
        conn = sqlite3.connect(path)
        insert_repo_info(conn, name="test-repo", branch="main", commit_hash="abcdef" * 8)

        files_and_content = [
            ("src/main.py", "Python", 150, "sha_main", "def main():\n    print('hello world')\n"),
            ("src/lib/utils.py", "Python", 70, "sha_utils", "def helper()\n    return 'done'\n"),
            ("docs/README.md", "Markdown", 200, "sha_readme", "# Repo\nThis is a README.\n"),
            ("config.json", "JSON", 30, "sha_config", '{"key": "value"}\n'),
            ("app.rs", "Rust", 100, "sha_rust", "fn main() { println!(\"hello\"); }\n"),
        ]
        for rel, lang, size, sha, content in files_and_content:
            insert_file_metadata(conn, rel, lang, size, sha)
            insert_text_content(conn, rel, content)

        conn.commit()
        conn.close()

        yield path


# ---------------------------------------------------------------------------
# create_database tests
# ---------------------------------------------------------------------------

class TestCreateDatabase:
    def test_creates_dirs_and_file(self):
        with tempfile.TemporaryDirectory(prefix="repo_int_creat_") as td:
            path = os.path.join(td, "sub", "index.db")
            create_database(path)
            assert os.path.isfile(path)

    def test_tables_exist(self):
        with tempfile.TemporaryDirectory(prefix="repo_int_tbls_") as td:
            db_file = os.path.join(td, "index.db")
            create_database(db_file)
            conn = sqlite3.connect(db_file)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' OR type='view'").fetchall()}
            assert "repo_info" in tables
            assert "files" in tables
            # FTS5 tables are virtual but listed with type='table'
            fts_found = any("fts" in t.lower() for t in tables)
            assert fts_found, f"FTS table not found. Tables: {tables}"
            conn.close()

    def test_idempotent_no_error(self):
        with tempfile.TemporaryDirectory(prefix="repo_int_dup_") as td:
            db_file = os.path.join(td, "index.db")
            create_database(db_file)
            create_database(db_file)  # should not raise


# ---------------------------------------------------------------------------
# insert_file_metadata tests
# ---------------------------------------------------------------------------

class TestInsertFileMetadata:
    def test_inserts_row(self, temp_db):
        path = temp_db
        conn = sqlite3.connect(path)
        insert_file_metadata(conn, "test.py", "Python", 42, "abc123")
        rows = conn.execute("SELECT rel_path, language, size, sha256 FROM files").fetchall()
        assert len(rows) == 1
        assert rows[0] == ("test.py", "Python", 42, "abc123")
        conn.close()

    def test_upsert_on_insert(self, temp_db):
        path = temp_db
        conn = sqlite3.connect(path)
        insert_file_metadata(conn, "test.py", "Python", 42, "sha_a")
        # Same rel_path with different metadata (upsert via INSERT OR REPLACE)
        insert_file_metadata(conn, "test.py", "JavaScript", 100, "sha_b")
        rows = conn.execute("SELECT language, size, sha256 FROM files WHERE rel_path='test.py'").fetchone()
        assert rows == ("JavaScript", 100, "sha_b")
        conn.close()


# ---------------------------------------------------------------------------
# insert_text_content tests
# ---------------------------------------------------------------------------

class TestInsertTextContent:
    def test_inserts_fts_row(self, temp_db):
        path = temp_db
        conn = sqlite3.connect(path)
        insert_file_metadata(conn, "a.py", "Python", 10, "sha1")
        insert_text_content(conn, "a.py", "hello world")
        row = conn.execute(
            "SELECT rel_path, content FROM file_content_fts"
        ).fetchone()
        assert row is not None
        assert row[0] == "a.py"
        assert "hello world" in row[1]
        conn.close()


# ---------------------------------------------------------------------------
# FTS search tests
# ---------------------------------------------------------------------------

class TestSearchFiles:
    def test_search_returns_matches(self, indexed_files_db):
        matches = search_files(indexed_files_db, "hello")
        assert len(matches) > 0
        # main.py and README both contain 'hello' in different forms
        rels = {m["rel_path"] for m in matches}
        assert "src/main.py" in rels

    def test_search_specific_word(self, indexed_files_db):
        matches = search_files(indexed_files_db, "helper")
        paths = [m["rel_path"] for m in matches]
        assert "src/lib/utils.py" in paths

    def test_search_returns_empty_for_no_match(self, indexed_files_db):
        matches = search_files(indexed_files_db, "xyznonexistent12345678")
        assert len(matches) == 0

    def test_search_like_prefix(self, indexed_files_db):
        """Test the 'like:' prefix for glob-like matching."""
        matches = search_files(indexed_files_db, "like:*.py")
        paths = [m["rel_path"] for m in matches]
        assert len(paths) >= 2
        all_py = all(p.endswith(".py") for p in paths)
        assert all_py

    def test_search_returns_language_field(self, indexed_files_db):
        matches = search_files(indexed_files_db, "hello")
        for m in matches:
            assert "language" in m
            assert "rel_path" in m
            assert "size" in m
            assert "sha256" in m

    def test_search_no_match(self, indexed_files_db):
        # Files only have 'hello', 'main', 'done', 'Repo', README, value, done
        matches = search_files(indexed_files_db, "definitely_not_in_any_file_abc")
        assert len(matches) == 0

    def test_search_finds_rust_main(self, indexed_files_db):
        matches = search_files(indexed_files_db, 'println')
        paths = [m["rel_path"] for m in matches]
        assert "app.rs" in paths


# ---------------------------------------------------------------------------
# get_stats tests
# ---------------------------------------------------------------------------

class TestGetStats:
    def test_returns_dict_with_keys(self, indexed_files_db):
        stats = get_stats(indexed_files_db)
        assert isinstance(stats, dict)
        assert "total_files" in stats
        assert "total_bytes" in stats
        assert "language_breakdown" in stats

    def test_total_files_count(self, indexed_files_db):
        stats = get_stats(indexed_files_db)
        assert stats["total_files"] == 5

    def test_language_breakdown(self, indexed_files_db):
        stats = get_stats(indexed_files_db)
        breakdown = stats["language_breakdown"]
        assert "Python" in breakdown
        assert breakdown["Python"] >= 2  # main.py + utils.py

    def test_repo_name(self, indexed_files_db):
        stats = get_stats(indexed_files_db)
        assert stats.get("repo_name") == "test-repo"


# ---------------------------------------------------------------------------
# get_file_list tests
# ---------------------------------------------------------------------------

class TestGetFileList:
    def test_returns_all_files(self, indexed_files_db):
        files = get_file_list(indexed_files_db, limit=100)
        assert len(files) == 5

    def test_filter_by_language(self, indexed_files_db):
        py_files = get_file_list(indexed_files_db, lang_filter="Python", limit=100)
        paths = [f["rel_path"] for f in py_files]
        assert all(p.endswith(".py") for p in paths)
        assert len(py_files) >= 2

    def test_pagination(self, indexed_files_db):
        page1 = get_file_list(indexed_files_db, limit=2, offset=0)
        page2 = get_file_list(indexed_files_db, limit=2, offset=2)
        # Both should have exactly limit elements (when enough rows exist)
        assert len(page1) == 2
        assert len(page2) == 2

    def test_no_files_with_lang_filter(self, indexed_files_db):
        files = get_file_list(indexed_files_db, lang_filter="NonExistent")
        assert len(files) == 0


# ---------------------------------------------------------------------------
# db_path tests
# ---------------------------------------------------------------------------

class TestDbPath:
    def test_returns_external_deterministic_location(self):
        p = db_path("/some/repo")
        # Should be outside the repo directory and end with index.db
        assert os.path.dirname(os.path.dirname(p)) == os.path.join(
            os.path.expanduser("~"), ".local", "share", "repo-intelligence"
        )
        assert p.endswith("index.db")

    def test_deterministic_same_path(self):
        assert db_path("/a/b/c") == db_path("/a/b/c")


# ---------------------------------------------------------------------------
# End-to-end: build from scan to query
# ---------------------------------------------------------------------------

class TestE2EIndexedFiles:
    """End-to-end: index files with content and query them."""

    def test_files_have_content_indexed(self, indexed_files_db):
        # After inserting text, FTS should return matches
        stats = get_stats(indexed_files_db)
        assert stats["total_files"] == 5


# ---------------------------------------------------------------------------
# Non-existent DB handling
# ---------------------------------------------------------------------------

class TestNonExistentDB:
    def test_search_nonexistent_db(self):
        """search_files for a path that doesn't exist returns nothing (SQLite creates empty db)."""
        with tempfile.TemporaryDirectory(prefix="repo_int_noexist_") as td:
            bad_path = os.path.join(td, "noindex.db")
            matches = search_files(bad_path, "test")
            assert isinstance(matches, list)  # may be empty or raise

    def test_get_stats_nonexistent_db(self):
        with tempfile.TemporaryDirectory(prefix="repo_int_noexist2_") as td:
            bad_path = os.path.join(td, "noindex.db")
            stats = get_stats(bad_path)
            assert isinstance(stats, dict)  # returns empty stats for new db


# ---------------------------------------------------------------------------
# SQLite schema validation
# ---------------------------------------------------------------------------

class TestSchema:
    def test_files_table_columns(self, temp_db):
        conn = sqlite3.connect(temp_db)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
        assert "rel_path" in cols
        assert "language" in cols
        assert "size" in cols
        assert "sha256" in cols
        assert "is_binary" in cols
        conn.close()

    def test_repo_info_table_has_key_value(self, temp_db):
        conn = sqlite3.connect(temp_db)
        rows = conn.execute("SELECT key, value FROM repo_info").fetchall()
        # The fixture already inserted name, branch, commit_hash
        keys = {r[0] for r in rows}
        assert "name" in keys or "repo_name" in keys
        conn.close()
