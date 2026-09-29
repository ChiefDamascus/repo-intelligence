"""Repository Intelligence - scanning, indexing, and semantic search for codebases."""

from repo_intelligence.scanner import scan_repo
from repo_intelligence.search import (
    create_database,
    db_path,
    get_file_list,
    get_stats,
    insert_file_metadata,
    insert_repo_info,
    insert_text_content,
    search_files,
)

__all__ = [
    "scan_repo",
    "db_path",
    "create_database",
    "insert_repo_info",
    "insert_file_metadata",
    "insert_text_content",
    "search_files",
    "get_file_list",
    "get_stats",
]