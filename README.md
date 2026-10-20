# Repository Intelligence

A local, offline-first command-line tool for exploring Python repositories. Repo Intelligence builds a **searchable catalog** of a repository — its files, and its Python functions, classes, methods, and import statements — into a local SQLite index you can query from the `repo-index` command line.

You install the tool, build an index of a target repository, and then query that index with `repo-index` subcommands. It is a **local, offline-first code-exploration tool**: it does not require network access, and it is **not** an AI assistant, chat interface, or autonomous coding agent.

Typical flow:

```bash
repo-index build /path/to/repo     # create the index (one-time / after changes)
cd /path/to/repo                   # run query commands from the indexed directory
repo-index stats                    # inspect what was captured
repo-index files --lang python      # list indexed Python files
repo-index symbols --kind function  # find functions
repo-index search-files "def main"  # full-text search of indexed content
repo-index imports --kind from_import  # list import statements
```

For a guided walkthrough of your first run, see [Your First Five Minutes](#your-first-five-minutes-with-repo-intelligence). For the full per-command reference, see [CLI Usage](#cli-usage).

## Current Status

`repo-intelligence` implements Phase 1 (file scanning, indexing, and text search) plus the static-analysis milestones through Phase 2C-3B-2. The following capabilities are already present and queryable:

- File inventory, language classification, and SHA-256 hashing (Phase 1)
- FTS5 full-text search over indexed text content (Phase 1)
- Python AST symbol extraction and persistence (Phase 2A)
- Python import / imported-name extraction and persistence (Phase 2B)
- Deterministic Python module inventory and package-root mapping (Phase 2C-1)
- Pure, deterministic absolute-import resolution (Phase 2C-2A) and its read-only database-backed adapter (Phase 2C-2B)
- Pure relative-import module-target resolution (Phase 2C-3A)
- Named imported-item static evidence analysis (Phase 2C-3B-1)
- Wildcard imported-item and `__all__` static evidence analysis (Phase 2C-3B-2)

These are **static, programmatic** analyses performed against supplied inputs; they do not execute, import, or load target code. Extraction of symbols and import statements surfaces via `repo-index symbols` and `repo-index imports`; the module-target resolvers and the named-imported-item and wildcard static-evidence analyses are not yet exposed as a subcommand — they are exposed as module-qualified Python APIs (e.g. `repo_intelligence.wildcard_import_analysis.analyze_wildcard_import`, `repo_intelligence.imported_name_analysis.analyze_named_import`), not CLI commands. The static-evidence analyses return conservative static evidence against the supplied module inventory and source text, not a confirmed runtime binding. Embeddings, vector/semantic search, documentation generation, LLM integration, and MCP integration are **not** implemented (see *Limitations* below).

## Project Purpose

`repo-intelligence` provides a local, offline-first repository intelligence system to:

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

## Your First Five Minutes with Repo Intelligence

A practical run-through on a small Python repository. We'll index a hypothetical project at `/projects/my-application` and then query it. (This is separate from the directory you installed Repo Intelligence from: `/projects/my-application` is the **target** you analyze, not the tool's source.)

**Prerequisites**

- Python **3.11 or newer** (check with `python --version`).
- A project directory to index — here, `/projects/my-application`.

**1. Install the tool (preferably in a virtual environment)**

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# run this from the directory containing Repo Intelligence's own source
pip install .
```

After this, the `repo-index` command is available.

**2. Build the index of the target repository**

```bash
repo-index build /projects/my-application
```

This walks the target directory (read-only), reads its source files, and writes **one SQLite database file** that holds the index. By default that file lives at `<XDG_DATA_HOME or ~/.local/share>/repo-intelligence/<repo-id>/index.db` — outside the target repository; pass `--index-path` to write it somewhere else (e.g. into the target). It does **not** rewrite, edit, or delete the target's source files. See [Index Storage](#index-storage) and [Safety and Exclusions](#safety-and-exclusions).

**3. Move into the project (so the query commands find that index)**

```bash
cd /projects/my-application
```

Query subcommands use the database of the **current directory** when no `--db-path` is given, so running them from the same directory you indexed makes them find the right database.

**4. Look at what was captured**

```bash
repo-index stats
```

Shows the file count, total bytes, language breakdown, and git metadata.

**5. Find Python code and text**

```bash
repo-index files --lang python          # list indexed Python files
repo-index symbols --kind function     # list functions
repo-index symbols --kind class        # list classes
repo-index search-files "def main"     # full-text search of indexed content
repo-index search-files "like:*.py"    # match file paths with a glob
```

**6. Inspect imports**

```bash
repo-index imports --kind from_import   # from-import statements
repo-index imports --module os          # imports of a specific module
```

**7. Rebuild after the source changes**

```bash
repo-index build /projects/my-application
```

Rebuilding refreshes the index (it recreates the database) so it reflects the current source. You only need to rebuild when the target repository has changed.

That's the core loop: **build → cd → query**, and rebuild when the source changes. For the full options of every command, see [CLI Usage](#cli-usage).

## Quick Reference

All six `repo-index` commands:

| Command | What it does | Common options |
|---|---|---|
| `repo-index build <repo>` | Scan a directory and create/refresh the SQLite index (read-only on the target) | `--index-path`, `--ignore-dirs` |
| `repo-index stats` | Show index statistics (counts, bytes, languages, git metadata) | `--db-path`, `--json` |
| `repo-index files` | List indexed files (inventory) | `--lang`, `--path`, `--limit`, `--offset`, `--json` |
| `repo-index search-files <query>` | Full-text search of indexed content, or `like:glob` path match | `--db-path`, `--json` |
| `repo-index symbols` | List extracted Python symbols (functions, classes, methods) | `--name`, `--kind`, `--file`, `--limit`, `--json` |
| `repo-index imports` | List extracted import / from-import statements and names | `--module`, `--name`, `--kind`, `--file`, `--limit`, `--json` |

**Essential terms**

- **Index** — the SQLite database the tool builds from a target. It stores copies of indexed text, file metadata, symbols, and imports.
- **Target repository** — the directory you point `repo-index build` at. The tool only *reads* it.
- **Index database** — the single `.db` file that holds the index. By default it lives at `<XDG_DATA_HOME or ~/.local/share>/repo-intelligence/<repo-id>/index.db`; set `--index-path` to change it.
- **Symbol** — a named definition extracted from Python source (function, class, or method).
- **Import statement** — an `import` or `from … import …` statement, plus the names it imports.
- **Full-text search** — FTS5 search over the *content* of text files (not vector/semantic search) — see [Search Behavior](#search-behavior).

**Indexing does not modify your source code.** `repo-index build` reads the target's source files and writes/recreates the **index database** file. By default that file is stored at `<XDG_DATA_HOME or ~/.local/share>/repo-intelligence/<repo-id>/index.db` (outside the target); `--index-path` controls exactly where the database write goes. It does not rewrite, edit, or move the target's source files. Rebuild recreates the same database file.

> **Sensitive content:** the index stores copies of the text it indexes, which may contain credentials, personal data, confidential or third-party copyrighted material. Secret detection/redaction is **not** implemented. Configure the target's `.gitignore` before indexing (`--ignore-dirs` excludes directory names, not individual secret files), index only material you are authorized to process, and protect the database — by default under `<XDG_DATA_HOME or ~/.local/share>/repo-intelligence/`, or wherever `--index-path` points. See [Safety and Exclusions](#safety-and-exclusions).

## CLI Usage

The `repo-index` console entry point (installed by `pyproject.toml`) provides six subcommands: `build`, `stats`, `files`, `search-files`, `symbols`, and `imports`. Each is documented in detail below; see the [Quick Reference](#quick-reference) for a one-line summary of each.

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
repo-index stats [--db-path PATH] [--json]
```

Displays total file count, total bytes, language breakdown, and git metadata. When no `--db-path` is given, the database for the **current directory** is used (same hash derivation as `build`); point `--db-path` explicitly at the target's database when you run it from elsewhere. Output can be rendered as valid JSON with `--json`.

### List indexed files

```bash
repo-index files [--db-path PATH] [--lang LANG] [--path PREFIX] [--limit N] [--offset N] [--json]
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
repo-index search-files QUERY [--db-path PATH] [--json]
```

Searches the FTS5 text index. Two modes:

| Mode | Syntax | Behavior |
|---|---|---|
| FTS5 MATCH | `repo-index search-files "def main"` | Searches indexed file **contents** |
| Path glob | `repo-index search-files "like:*.py"` | Searches within file **paths** via SQLite LIKE |

Binary files and files exceeding 10 MB are intentionally excluded from text indexing. A nonexistent query returns cleanly with exit code 0 (no match, never error).

### List indexed symbols

```bash
repo-index symbols [--db-path PATH] [--name NAME] [--qualified-name QNAME] [--kind KIND] [--file PATH] [--limit N] [--offset N] [--json]
```

Lists symbols extracted from Python sources during `build`. The `--name`, `--qualified-name`, `--kind` (e.g. `class`, `function`, `method`), and `--file` filters are applied as exact-match predicates. Output can be rendered as valid JSON with `--json`.

### List indexed import statements

```bash
repo-index imports [--db-path PATH] [--file PATH] [--module MODULE] [--name NAME] [--kind KIND] [--limit N] [--offset N] [--json]
```

Lists import / from-import statements and their imported names as extracted during `build`. The `--module` and `--name` filters match the module and an imported name respectively (exact match, not the alias); `--kind` matches the import kind (e.g. `import`, `from_import`). Output can be rendered as valid JSON with `--json`.

> Note: the absolute-import and relative-import module-target resolvers and the named-imported-item and wildcard static-evidence analyses are **programmatic** (module-qualified Python) APIs; they are not yet wired to a `repo-index` subcommand.

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

- **File-level** exclusions parse and apply patterns from the target's `.gitignore` file (supports exact names, `*.glob` patterns like `.env`, `.env.*`, `*.secret`, `*.key`, directory suffixes like `logs/`, root-relative `/path`, and `**/wildcard` patterns covering nested directories). Note: full Git ignore-spec edge cases (e.g. double-negation re-inclusions like `!**/important.txt`) are not guaranteed to be covered — see `Limitations` for how to handle such files safely. For secret and confidential material, prefer excluding the file or directory outright (a `.gitignore` pattern or an `--ignore-dirs NAME` entry for directory names) rather than relying on wildcard `*.glob` matching.
- **Binary files**: detected by null-byte check in the first 8 KB and by extension whitelist (`.png`, `.jpg`, `.pdf`, `.zip`, `.so`, `.dll`, etc.) — these are still indexed for metadata but their content is **not** text-indexed.
- **Large files**: files exceeding 10 MB are treated as binary and excluded from text indexing.
- **Sensitive content:** The index stores copies of indexed text and may contain credentials, personal data, confidential information, or third-party copyrighted material. Secret detection and redaction are not implemented. Configure the target repository's `.gitignore` before indexing; `--ignore-dirs` excludes directory names, not individual secret files. Index only material you are authorized to process, protect the database, and review its contents before sharing it or providing it to an external service.

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

## Limitations

This project intentionally omits several advanced analysis and integration features. The following are **not** implemented:

| Category | Not yet implemented |
|---|---|
| Static analysis | AST, symbol, and import analysis is **Python-only**; other languages are classified by extension but not parsed |
| Resolution | Absolute-/relative-import module-target resolvers and the named-imported-item and wildcard static-evidence analyses are programmatic (module-qualified Python) APIs only; no `repo-index` subcommand |
| Call analysis | No caller/callee graph; no language-wide reference graph |
| Test mapping | No test-to-production code mapping |
| Documentation | No automatic repository documentation generation |
| Semantic search | No embeddings or vector-based similarity search (search is FTS5 full-text, not vector similarity) |
| Integration | No MCP or external API integration |
| LLM | No LLM integration at any layer |
| Code understanding | No cross-file semantic inference beyond the deterministic static analyses listed above |
| Git ignore edge cases | gitignore support covers common patterns, but is not a drop-in for the full Git ignore specification. In particular, **double-negation re-inclusions** (e.g. `**/keep.txt` after `**`) are not guaranteed to override an earlier exclusion. If a `.gitignore` relies on re-inclusion patterns to protect sensitive files, confirm by running `repo-index build` and then `repo-index search-files` or inspecting the DB. Safer approach: place the sensitive file outside the indexed directory, or use `--ignore-dirs NAME` for a directory to be excluded outright. |

## Future Phases (Planned)

Roadmap capabilities that remain **unimplemented**:

- Call / reference graphs beyond the deterministic import-resolution analyses
- Automated repository documentation generation
- Embeddings and vector similarity search
- Cross-language static analysis (AST/symbol/import analysis is currently Python-only)
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

This project's original code is available under the Zero-Clause BSD license (0BSD); see [LICENSE](LICENSE). Third-party materials remain subject to their respective licenses. This license does not grant rights to third-party trademarks or to repositories indexed by the tool.
