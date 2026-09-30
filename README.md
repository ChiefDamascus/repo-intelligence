# Repository Intelligence

A reusable local repository indexing and navigation tool. It scans a codebase, produces a file inventory with language classification and SHA-256 integrity hashes, persists all data to an external SQLite database, and provides FTS5 full-text search over indexed text content. Designed to support coding agents and developers exploring unfamiliar repositories.

## Current Status

This repository represents **Phase 1** of the `repo-intelligence` project. Phase 1 covers foundational file scanning, indexing, and text search. Advanced capabilities such as AST/symbol analysis, semantic code understanding, and LLM integration are planned for future phases (see *Limitations* below).

## Project Purpose

`repo-intelligence` provides a local, offline-first mechanism to:

- Recursively scan any repository directory
- Classify files by language based on file extension
- Capture git metadata (branch, commit hash, dirty status)
- Persist all metadata to a SQLite database with FTS5 search indexing
- Query the index via CLI or programmatically

## Installation

```bash
pip install -e ".[dev]"
```

Requires Python 3.11+. For production-only use:

```bash
pip install .
```

Development dependencies include `pytest`.

## CLI Usage

The `repo-index` console entry point (installed by `pyproject.toml`) provides four subcommands:

### Build an index

```bash
repo-index build <repository>
```

Scans the given directory tree and creates (or recreates) the SQLite search index. The index is stored in an **external** directory by default, derived deterministically from the repository's absolute path:

- Default location: `<XDG_DATA_HOME or ~/.local/share>/repo-intelligence/<sha256-abbr-of-repo-path>/index.db`
  (e.g. `~/.local/share/repo-intelligence/a1b2c3d4e5f6a7b8/index.db`)

The subdirectory name is a 16-character SHA-256 prefix of the repository's absolute path, guaranteeing uniqueness without cluttering the working tree. Use `--index-path` to override:

```bash
repo-index build /path/to/repo                       # default: external deterministic location
repo-index build /path/to/repo --index-path /tmp.idx  # custom external file
```

The build process:
1. Walks the directory tree recursively
2. Skips excluded directories (see below)
3. Classifies each file by language via extension mapping
4. Computes SHA-256 hashes for text files (empty string for binary/large files)
5. Captures git state (branch, commit hash, dirty/clean flag) when the target is a git repository
6. Stores all data in a fresh SQLite database with FTS5 full-text search

Additional `--ignore-dirs NAME` options can be supplied to exclude extra directory names beyond the defaults.

### Show indexing statistics

```bash
repo-index stats [db_path] [--json]
```

Displays total file count, total bytes, language breakdown, and git metadata. When no `db_path` is given, the database at the current directory's computed external location is used (same hash derivation as `build`). Output can be rendered as valid JSON with `--json`.

### List indexed files

```bash
repo-index files [db_path] [--lang LANG] [--path PREFIX] [--limit N] [--offset N] [--json]
```

Lists files from the index with optional filtering:

| Filter | Description |
|---|---|
| `--lang LANG` | Filter by language name; **case-insensitive** (e.g., `Python`, `python`, `PYTHON`) |
| `--path PREFIX` | Filter by repository-relative path prefix (e.g., `tests/` matches all files under the `tests/` directory). Trailing slash is optional. |
| `--limit N` | Maximum results (default 100) |
| `--offset N` | Pagination offset (default 0) |

Examples:

```bash
repo-index files                                    # all indexed files
repo-index files --lang python                      # only Python files
repo-index files --path tests/                      # only files under tests/
repo-index files --lang python --path src/          # combined filter
repo-index files --json                             # JSON output
```

### Full-text search over indexed content

```bash
repo-index search-files <query> [db_path] [--json]
```

Searches the FTS5 text index. Two modes:

| Mode | Syntax | Behavior |
|---|---|---|
| FTS5 MATCH | `repo-index search-files "def main"` | Searches indexed file **contents** |
| Path glob | `repo-index search-files "like:*.py"` | Searches within file **paths** via SQLite LIKE |

Binary files and files exceeding 10 MB are intentionally excluded from text indexing. A nonexistent query returns cleanly with exit code 0 (no match, never error).

## Index Storage

- **Default location**: `<XDG_DATA_HOME or ~/.local/share>/repo-intelligence/<repo-sha256-abbr>/index.db` — stored in a deterministic external directory outside the working tree.
- **External / override**: Use `--index-path <path>` during `build` to store the database at any location of your choosing.
- **Read-only input**: The indexed target repository is never modified by indexing operations; only files are read.
- **Cleanup**: To remove a repo's index, delete its subdirectory under `$XDG_DATA_HOME/repo-intelligence/` (or `~/.local/share/repo-intelligence/`).

## Safety and Exclusions

The scanner skips the following directories by default:

| Category | Directory patterns |
|---|---|
| Version control / envs | `.git`, `.venv`, `venv` |
| Dependency trees | `node_modules` |
| Build artifacts | `dist`, `build`, `target`, `.tox`, `.eggs` |
| Python caches | `__pycache__` |

Additional exclusions:

- **File-level** exclusions parse and apply patterns from the target's `.gitignore` file (supports exact names, `*.glob` patterns, directory suffixes like `logs/`, root-relative `/path`, `**/wildcard`). Note: full Git ignore specification edge cases may not all be covered.
- **Binary files**: detected by null-byte check in the first 8 KB and by extension whitelist (`.png`, `.jpg`, `.pdf`, `.zip`, `.so`, `.dll`, etc.) — these are still indexed for metadata but their content is **not** text-indexed.
- **Large files**: files exceeding 10 MB are treated as binary and excluded from text indexing.
- **Secrets**: no automatic detection or stripping of `.env` / secret file contents at this time; only file-level name matching can exclude them via `--ignore-dirs`.

## Git Metadata

When the scanned directory is a git repository, the following metadata is captured:

- `repo_name`: basename of the repository root
- `current_branch`: output of `git rev-parse --abbrev-ref HEAD`
- `commit_hash`: output of `git rev-parse --verify HEAD`
- `is_dirty`: true if `git status --porcelain` reports uncommitted changes
- `is_git_repo`: boolean flag

Non-git directories can still be indexed; all git metadata fields will simply be null/false. The index is stored outside the working tree, so nothing needs to be added to `.gitignore`.

## Search Behavior

- FTS5 full-text search applies **only** to files that are not binary and not larger than 10 MB.
- File inventory queries (`files` subcommand) and text searches (`search-files` subcommand) are separate: listing uses the `files` table (language, path, size, sha256); searching uses the `file_content_fts` virtual table.
- The `--lang` filter on the `files` command is case-insensitive; the `--path` filter matches files whose `rel_path` starts with the given prefix followed by `/`.

## Limitations (Phase 1)

This phase intentionally omits advanced analysis features. The following are **not** implemented:

| Category | Not yet implemented |
|---|---|
| Code structure | No AST or symbol indexing; no language-aware parsing |
| Call analysis | No caller/callee graph; no import/reference graph |
| Test mapping | No semantic test-to-production code mapping |
| Documentation | No automatic DeepWiki / documentation generation |
| Semantic search | No embeddings or vector-based similarity search |
| Integration | No MCP or external API integration |
| LLM | No LLM integration at any layer |
| Code understanding | No semantic code analysis beyond file classification |
| Git ignore edge cases | gitignore support covers common patterns but may not implement every edge case of the full Git specification |

## Future Phases (Planned)

Phase 2+ capabilities intended for the roadmap:

- Python AST and general symbol indexing
- Import / reference graph construction
- Caller / callee call graphs
- Semantic test-to-production mapping
- DeepWiki automated documentation generation
- Embeddings and vector similarity search
- LLM-powered code analysis and summarization
- MCP / API integration for external tool compatibility

## Programmatic Use

```python
from repo_intelligence.scanner import scan_repo
from repo_intelligence.search import (
    create_database,
    db_path as get_db_path,
    insert_file_metadata,
    insert_text_content,
    search_files,
    get_stats,
    get_file_list,
)

# Scan a repository (returns file list and metadata)
result = scan_repo("/path/to/repo")

# Create database at the external deterministic location
db_dir = get_db_path("/path/to/repo")
create_database(db_dir)

# Index files manually in full (or use CLI `repo-index build`)
import sqlite3, os
conn = sqlite3.connect(db_dir)
repo_info = result["repo_info"]
from repo_intelligence.search import insert_repo_info
insert_repo_info(conn, **repo_info)

for f_entry in result["files"]:
    insert_file_metadata(
        conn, f_entry["rel_path"], f_entry["lang"],
        f_entry["size"], f_entry["sha256"]
    )
    if not f_entry["is_binary"]:
        with open(f_entry["abs_path"], "r", encoding="utf-8", errors="replace") as fh:
            insert_text_content(conn, f_entry["rel_path"], fh.read())
conn.commit()

# Query
matches = search_files(db_dir, "import json")
stats = get_stats(db_dir)
files = get_file_list(db_dir, lang_filter="Python")
conn.close()
```

## License

MIT
