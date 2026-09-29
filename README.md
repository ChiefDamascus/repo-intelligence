# Repository Intelligence

A CLI tool for scanning, indexing, and searching codebases. It builds a local SQLite database with FTS5 full-text search over file contents, language classification, SHA-256 file integrity hashes, and git metadata.

## Installation

```bash
pip install -e ".[dev]"
```

Or from source:

```bash
pip install .
```

## CLI Usage

The `repo-index` command provides four subcommands:

### Build an index

Scan a repository directory and create (or refresh) the SQLite search index:

```bash
repo-index build /path/to/repo
```

This will:
- Walk the directory tree, skipping default excluded dirs (`.git`, `node_modules`, `.venv`, `__pycache__`, etc.)
- Classify files by language based on extension
- Compute SHA-256 hashes for text files
- Store git metadata (branch, commit hash, dirty status)
- Index all file contents in an FTS5 virtual table

### Show indexing statistics

```bash
repo-index stats [db_path]
```

Displays total files, total bytes, language breakdown, and git info. If no `db_path` is supplied, the database inside the current directory's `.repo_intelligence/` is used. With `--json`, output is valid JSON:

```bash
repo-index stats --json
```

### List indexed files

```bash
repo-index files [db_path] [--lang LANG] [--limit N] [--offset N]
```

Lists files from the index with optional language filtering and pagination:

```bash
repo-index files --lang Python --limit 50
repo-index files --json
```

### Full-text search

```bash
repo-index search-files <query> [db_path]
```

Searches indexed file contents using FTS5 MATCH syntax or the `like:` prefix for glob patterns:

```bash
repo-index search-files "def main"
repo-index search-files "like:*.py"
repo-index search-files "class *Handler" --json
```

## Configuration

### Default excluded directories

The following are always skipped during scanning:

- `.git`, `node_modules`, `.venv`, `__pycache__`
- `venv`, `dist`, `build`, `target`, `.tox`, `.eggs`

Custom exclusions can be provided to the scanning API programmatically. The CLI currently uses defaults.

### Database location

By default the index lives at `<repo_root>/.repo_intelligence/index.db`. Pass `--index-path` or the positional `[db_path]` argument to override.

## Programmatic use

```python
from repo_intelligence.scanner import scan_repo
from repo_intelligence.search import (
    create_database,
    insert_file_metadata,
    insert_text_content,
    search_files,
    get_stats,
)

# Scan a repository
result = scan_repo("/path/to/repo")

# Create database and index files
import sqlite3, os
db_path = repo_intelligence.search.db_path(repo_dir)
os.makedirs(os.path.dirname(db_path), exist_ok=True)
create_database(db_path)

conn = sqlite3.connect(db_path)
for f_data in result["files"]:
    insert_file_metadata(conn, f_data["rel_path"], f_data["lang"],
                         f_data["size"], f_data["sha256"])
    # ... also insert text content via insert_text_content()
    conn.commit()

# Search
matches = search_files(db_path, "import json")
stats = get_stats(db_path)
```

## License

MIT
