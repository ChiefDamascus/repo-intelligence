"""Repository scanning, file classification, and state detection."""

import hashlib
import os
import subprocess
from pathlib import Path


# Extension-to-language mapping
LANGUAGE_MAP = {
    # Python
    ".py": "Python",
    ".pyx": "Python",
    ".pyi": "Python",
    ".pyw": "Python",
    # JavaScript
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    # TypeScript
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    # Java
    ".java": "Java",
    # Go
    ".go": "Go",
    # Rust
    ".rs": "Rust",
    # C/C++
    ".c": "C",
    ".h": "C Header",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".hpp": "C++ Header",
    ".hh": "C++ Header",
    # Ruby
    ".rb": "Ruby",
    ".erb": "Ruby",
    ".rake": "Ruby",
    # PHP
    ".php": "PHP",
    ".phtml": "PHP",
    # HTML/XML/CSS
    ".html": "HTML",
    ".htm": "HTML",
    ".xml": "XML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "Sass",
    ".less": "Less",
    # Shell
    ".sh": "Shell",
    ".bash": "Bash",
    ".zsh": "Zsh",
    ".fish": "Fish",
    ".bashrc": "Bash",
    ".bash_profile": "Bash",
    # Configuration
    ".toml": "TOML",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".json": "JSON",
    ".ini": "INI",
    ".cfg": "Config",
    ".conf": "Config",
    # SQL
    ".sql": "SQL",
    ".plsql": "SQL",
    # Docker
    ".dockerfile": "Dockerfile",
    # Markdown
    ".md": "Markdown",
    ".rst": "reStructuredText",
    # Other
    ".lua": "Lua",
    ".jl": "Julia",
    ".R": "R",
    ".r": "R",
    ".scm": "Scheme",
    ".clj": "Clojure",
    ".dart": "Dart",
    ".zig": "Zig",
    ".v": "Verilog",
    ".sv": "SystemVerilog",
    ".proto": "Protocol Buffer",
    ".graphql": "GraphQL",
    ".svelte": "Svelte",
    ".vue": "Vue",
}


# Default directories to exclude during scanning
DEFAULT_EXCLUDE_DIRS = frozenset({
    ".git",
    "node_modules",
    ".venv",
    "__pycache__",
    "venv",
    "dist",
    "build",
    "target",
    ".tox",
    ".eggs",
})

# Files considered binary by extension
BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".so", ".dll", ".dylib", ".a", ".o", ".pyc", ".pyo",
    ".woff", ".woff2", ".ttf", ".eot",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv",
    ".ogg", ".wav", ".flac",
})

# Max file size (10MB) to scan; larger files are treated as binary
MAX_SCAN_SIZE = 10 * 1024 * 1024


def _is_binary(file_path: str) -> bool:
    """Detect if a file is binary by checking for null bytes in the first 8K bytes."""
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(8192)
            return b"\x00" in chunk
    except (OSError, IOError):
        return True


def _get_language(filename: str) -> str:
    """Classify a file's language based on its extension."""
    ext = Path(filename).suffix.lower()
    if ext in BINARY_EXTENSIONS:
        return "Binary"
    return LANGUAGE_MAP.get(ext, "Other")


def _load_gitignore(repo_dir: str) -> list[str]:
    """Parse .gitignore file and return list of pattern strings."""
    gitignore_path = os.path.join(repo_dir, ".gitignore")
    patterns = []
    if not os.path.isfile(gitignore_path):
        return patterns

    try:
        with open(gitignore_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue
                # Strip trailing whitespace but keep leading/trailing glob semantics
                patterns.append(line)
    except (OSError, IOError):
        pass

    return patterns


def _matches_gitignore(rel_path: str, gitignore_patterns: list[str], is_dir: bool = False) -> bool:
    """Check if a relative path matches any .gitignore pattern.
    
    Supports:
    - Exact filenames (e.g., "*.txt")
    - Directory patterns with trailing slash (e.g., "logs/")
    - Path segments matching (pattern applies at any directory depth)
    - Root-anchored patterns (leading /, only matches at repo root)
    """
    # Normalize leading ./ that git sometimes produces
    rel_path = rel_path.removeprefix("./")
    parts = rel_path.replace("\\", "/").split("/")

    for pattern in gitignore_patterns:
        if not pattern:
            continue

        # Pattern ends with / => directory-only pattern
        if pattern.endswith("/"):
            dir_prefix = pattern.rstrip("/")
            # Check cumulative prefixes (vendor/node_modules) against dir prefix
            for i in range(len(parts)):
                part_parts = parts[:i + 1]
                segment = "/".join(part_parts)
                if _fnmatch_ignore(segment, dir_prefix):
                    return True
            # Also check individual path parts (handles "node_modules/" finding it at any depth)
            for part in parts:
                if _fnmatch_ignore(part, dir_prefix):
                    return True
            continue

        # Pattern starts with / is a repo-root relative pattern
        if pattern.startswith("/"):
            inner = pattern.lstrip("/")
            # Root-anchored: only match at the first path component (repo root)
            if parts and _fnmatch_ignore(parts[0], inner):
                return True
            continue

        # Pattern starts with ** => match at any depth
        if pattern.startswith("**"):
            inner_pattern = pattern.removeprefix("**")
            if inner_pattern and inner_pattern.startswith("/"):
                inner_pattern = inner_pattern[1:]
            for i in range(len(parts)):
                sub = "/".join(parts[i:])
                if _fnmatch_ignore(sub, inner_pattern):
                    return True
            continue

        # Regular pattern: match basename against the simple glob
        basename = os.path.basename(parts[-1])
        if _fnmatch_ignore(basename, pattern):
            return True
        # Also check full relative path for patterns with slashes
        if "/" in pattern:
            rel_clean = rel_path.replace("\\", "/")
            parts_pattern = pattern.split("/")
            for i in range(len(parts) - len(parts_pattern) + 1):
                segment = "/".join(parts[i:i + len(parts_pattern)])
                if _sequence_match(segment, parts_pattern):
                    return True

    return False


def _fnmatch_ignore(filename: str, pattern: str) -> bool:
    """Simple glob matching similar to fnmatch but without complex extensions."""
    import fnmatch as _fnmatch
    return _fnmatch.fnmatch(filename, pattern)


def _sequence_match(path: str, pattern_parts: list[str]) -> bool:
    """Check if a path matches a dotted (slash-separated) globs pattern."""
    path_parts = path.split("/")
    if len(path_parts) != len(pattern_parts):
        return False
    for pp, pat in zip(path_parts, pattern_parts):
        import fnmatch as _fnmatch
        if not _fnmatch.fnmatch(pp, pat):
            return False
    return True


def _get_git_state(repo_dir: str) -> dict:
    """Capture git state info for the repository at repo_dir."""
    state = {
        "repo_name": os.path.basename(os.path.abspath(repo_dir)),
        "current_branch": None,
        "commit_hash": None,
        "is_dirty": False,
        "is_git_repo": False,
    }

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True,
            cwd=repo_dir, timeout=10,
        )
        if result.returncode != 0:
            return state
        root = result.stdout.strip()

        state["is_git_repo"] = True

        # Branch
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
            cwd=repo_dir, timeout=10,
        )
        if result.returncode == 0:
            state["current_branch"] = result.stdout.strip()

        # Commit hash
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True,
            cwd=repo_dir, timeout=10,
        )
        if result.returncode == 0:
            state["commit_hash"] = result.stdout.strip()

        # Dirty status
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True,
            cwd=repo_dir, timeout=10,
        )
        if result.returncode == 0:
            state["is_dirty"] = bool(result.stdout.strip())

    except (subprocess.TimeoutExpired, OSError):
        pass

    return state


def scan_repo(
    repo_path: str,
    ignore_dirs: list[str] | None = None,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> dict:
    """Scan a repository directory for files, classify them, and capture git state.

    Args:
        repo_path: Path to the repository root directory.
        ignore_dirs: Additional directories to skip beyond defaults.
        include_patterns: Glob patterns of file names to include (empty = all).
        exclude_patterns: Glob patterns of file names to exclude.

    Returns:
        Dictionary with keys:
            - repo_info: git state dict
            - files: list of dicts with file metadata
            - language_breakdown: {language: count}
            - ignore_dirs_used: combined list of ignored directory names
    """
    repo_path = os.path.abspath(repo_path)

    if not os.path.isdir(repo_path):
        raise NotADirectoryError(f"Path is not a directory: {repo_path}")

    # Combine excluded directories
    excluded = set(DEFAULT_EXCLUDE_DIRS)
    if ignore_dirs:
        excluded.update(ignore_dirs)
    ignore_dirs_used = sorted(excluded)

    # Load .gitignore patterns
    gitignore_patterns = _load_gitignore(repo_path)

    repo_info = _get_git_state(repo_path)
    files_list: list[dict] = []
    language_breakdown: dict[str, int] = {}

    for dirpath, dirnames, filenames in os.walk(repo_path):
        # Prune ignored/subdirectories from traversal
        dirnames[:] = [
            d for d in dirnames
            if d not in excluded and not _matches_gitignore(d + "/", gitignore_patterns, is_dir=True)
        ]

        rel_dir = os.path.relpath(dirpath, repo_path)
        if rel_dir == ".":
            rel_dir_prefix = ""
        else:
            rel_dir_prefix = rel_dir + "/"

        for filename in filenames:
            rel_file = rel_dir_prefix + filename

            # Check directory-level .gitignore first
            if _matches_gitignore(rel_file, gitignore_patterns):
                continue

            abs_path = os.path.join(repo_path, rel_file)

            # File-level name pattern checks
            basename_match_include = True
            if include_patterns:
                basename_match_include = any(
                    _fnmatch_ignore(filename, pat) for pat in include_patterns
                )
            if not basename_match_include:
                continue

            if exclude_patterns:
                if any(_fnmatch_ignore(filename, pat) for pat in exclude_patterns):
                    continue
                # Also check pattern with path segments
                rel_clean = rel_file.replace("\\", "/")
                parts_flat = rel_clean.split("/")
                for pat in exclude_patterns:
                    if "/" in pat:
                        pat_parts_flat = pat.split("/")
                        for i in range(len(parts_flat) - len(pat_parts_flat) + 1):
                            segment = "/".join(parts_flat[i:i + len(pat_parts_flat)])
                            if _fnmatch_ignore(segment, pat):
                                basename_match_include = False
                if not basename_match_include:
                    continue

            try:
                file_size = os.path.getsize(abs_path)
            except OSError:
                continue

            # Detect binary
            is_binary_file = (
                _is_binary(abs_path)
                or file_size > MAX_SCAN_SIZE
            )
            content_for_hash = None
            if not is_binary_file:
                try:
                    with open(abs_path, "rb") as f:
                        raw = f.read()
                    content_for_hash = raw.hex()

                    # Read for text content (FTS5 will receive decoded version)
                    try:
                        text_content = raw.decode("utf-8", errors="replace")
                    except Exception:
                        text_content = ""
                except (OSError, IOError):
                    continue
            else:
                text_content = ""

            # sha256 from actual file bytes
            if content_for_hash is not None:
                temp_path = abs_path + ".sha_tmp"
                with open(abs_path, "rb") as f:
                    h = hashlib.sha256()
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        h.update(chunk)
                sha256_hash = h.hexdigest()
            else:
                # Binary or unprocessed file - use size-based pseudo hash for record completeness
                sha256_hash = ""

            lang = _get_language(filename)
            languages_seen = language_breakdown.get(lang, 0) + 1
            language_breakdown[lang] = languages_seen

            files_list.append({
                "rel_path": rel_file,
                "abs_path": abs_path,
                "filename": filename,
                "size": file_size,
                "lang": lang,
                "sha256": sha256_hash,
                "is_binary": is_binary_file,
            })

    # Sort files for deterministic output
    files_list.sort(key=lambda f: f["rel_path"])

    return {
        "repo_info": repo_info,
        "files": files_list,
        "language_breakdown": language_breakdown,
        "ignore_dirs_used": ignore_dirs_used,
    }
