"""Python AST symbol indexing using only stdlib ``ast`` module.

Extracts structured symbol metadata from Python source text without
importing, executing, or mutating the source files.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImportRecord:
    """Immutable representation of a single Python import declaration.

    Attributes:
        kind: ``"import"`` or ``"from_import"``.
        module: the source module from which something is imported.
                for ``import X.Y``, the full dotted path (e.g. ``"X.Y"``);
                for ``from A.B import Z``, the source module ``"A.B"``;
                empty string for relative imports with no explicit base
                (``from . import ...``).   The value is *never* ``None`` —
                callers relying on this must not get a special sentinel in
                one branch and ``""`` in another.
        names: list of imported identifiers (immutable tuple) in source order
               (e.g. ``tuple[{name, asname}]``).  For wildcard imports the
               name is ``"*"``, with ``"asname"`` always ``None``.
        level: relative-import depth (``0`` for absolute imports).
        source_path: the file path where this import was found.
        start_line: 1-based line number of the import statement.
        start_col: 0-based column offset of the import keyword within its
                   line, from ``ast.Import.col_offset`` /
                   ``ast.ImportFrom.col_offset``.
    """

    kind: str
    module: str
    names: tuple[tuple[str, str | None], ...]
    level: int
    source_path: str
    start_line: int
    start_col: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PY_SUFFIXES = frozenset({".py", ".pyx", ".pyi", ".pyw"})


def _is_python_file(rel_path: str) -> bool:
    """Check whether *rel_path* looks like a Python source file."""
    return Path(rel_path).suffix.lower() in _PY_SUFFIXES


# ---------------------------------------------------------------------------
# Decorator extraction
# ---------------------------------------------------------------------------

def _extract_decorators(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[str]:
    """Return decorator text fragments (as-is via *ast.unparse* where supported)."""
    decs: list[str] = []
    for d in node.decorator_list:  # type: ignore[attr-defined]
        if isinstance(d, ast.Name):
            decs.append(d.id)
        elif isinstance(d, ast.Attribute):
            try:
                decs.append(ast.unparse(d))
            except (ValueError, RecursionError):
                decs.append("<decorator>")
        elif isinstance(d, ast.Call):
            if isinstance(d.func, ast.Name):
                decs.append(f"{d.func.id}(...)")
            elif isinstance(d.func, ast.Attribute):
                try:
                    decs.append(f"{ast.unparse(d.func)}(...)")
                except (ValueError, RecursionError):
                    decs.append("<decorator>(...)")
            else:
                try:
                    decs.append(ast.unparse(d))
                except (ValueError, RecursionError):
                    decs.append("<decorator>")
        else:
            try:
                decs.append(ast.unparse(d))
            except (ValueError, RecursionError):
                decs.append("<decorator>")
    return decs


# ---------------------------------------------------------------------------
# Docstring extraction
# ---------------------------------------------------------------------------

def _get_docstring(node: ast.stmt) -> str:
    """Return a concise docstring (first line if source > 512 chars)."""
    ds = ast.get_docstring(node)
    if not ds:
        return ""
    first_line = ds.split("\n", 1)[0].strip()
    if len(first_line) <= 512 and first_line == ds.strip():
        return first_line
    # Even a long docstring's first line is useful as concise form
    return first_line


# ---------------------------------------------------------------------------
# Signature extraction
# ---------------------------------------------------------------------------

def _signature_from_node(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Build a human-readable signature string from *node*.

    Renders parameters, defaults, annotations, ``*args``, ``**kwargs`` and
    the return annotation.  Does not execute or import any source code.
    """
    args = node.args
    parts: list[str] = []

    def _arg_str(name: str | None,
                 ann: ast.expr | None = None,
                 default: ast.expr | None = None) -> str:
        if name is None:
            return ""
        s = name
        if ann is not None:
            try:
                s += f": {ast.unparse(ann)}"
            except (ValueError, RecursionError):
                pass
        if default is not None:
            try:
                s += f"={ast.unparse(default)}"
            except (ValueError, RecursionError):
                pass
        return s

    # Positional-only args  (-- Python 3.8+)
    for a in args.posonlyargs:
        parts.append(_arg_str(a.arg, a.annotation, None))
    if args.posonlyargs:
        parts.append("/")

    # Regular positional / keyword args — last *n_defaults* have defaults
    n_args = len(args.args)
    n_defaults = len(args.defaults)
    default_offset = max(n_args - n_defaults, 0) if n_defaults else 0
    for i, a in enumerate(args.args):
        dflt_node = None
        if n_defaults > 0 and i >= default_offset:
            dflt_node = args.defaults[i - default_offset]
        parts.append(_arg_str(a.arg, a.annotation, dflt_node))

    # *args
    if args.vararg is not None:
        s = f"*{args.vararg.arg}"
        if args.vararg.annotation is not None:
            try:
                s += f": {ast.unparse(args.vararg.annotation)}"
            except (ValueError, RecursionError):
                pass
        parts.append(s)

    # keyword-only args (need bare "*" separator if no *args present)
    kw_only = args.kwonlyargs
    kw_defaults = args.kw_defaults or []
    has_vararg = args.vararg is not None
    if not has_vararg and kw_only:
        parts.append("*")
    for i, a in enumerate(kw_only):
        dflt_node = None
        if i < len(kw_defaults) and kw_defaults[i] is not None:
            dflt_node = kw_defaults[i]
        parts.append(_arg_str(a.arg, a.annotation, dflt_node))

    # **kwargs
    if args.kwarg is not None:
        s = f"**{args.kwarg.arg}"
        if args.kwarg.annotation is not None:
            try:
                s += f": {ast.unparse(args.kwarg.annotation)}"
            except (ValueError, RecursionError):
                pass
        parts.append(s)

    sig_params = ", ".join(parts)
    sig_full = f"({sig_params})"

    # Return annotation
    if node.returns is not None:
        try:
            sig_full += f" -> {ast.unparse(node.returns)}"
        except (ValueError, RecursionError):
            pass

    return sig_full


# ---------------------------------------------------------------------------
# Module line helpers
# ---------------------------------------------------------------------------

def _module_end_line(tree: ast.Module) -> int:
    """Return best-guess end line for a Module node (which may lack it)."""
    max_line = 0
    for node in ast.walk(tree):
        el = getattr(node, "end_lineno", None)
        if el is not None and el > max_line:
            max_line = el
    return max(max_line, 1)


# ---------------------------------------------------------------------------
# AST visitor — symbol collection
# ---------------------------------------------------------------------------

class _Visitor(ast.NodeVisitor):
    """AST visitor that collects symbols with parent tracking."""

    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.symbols: list[dict] = []
        # Each entry on *stack* represents the scope at the point of a class or
        # function visit.  Stored as ``(qualified_name, index_into_symbols)`` so
        # that nested scopes can reference their parent by index + 1 (DB id).
        self.stack: list[tuple[str, int]] = []

    @property
    def _parent_qname(self) -> str | None:
        """Qualified name of the immediate enclosing (non-module) scope."""
        for qname, idx in reversed(self.stack):
            if idx != -1:  # skip module sentinel
                return qname
        return None

    @property
    def _parent_idx(self) -> int | None:
        """Index (into self.symbols) of the direct parent symbol, or None."""
        for qname, idx in reversed(self.stack):
            if idx != -1:  # skip module sentinel
                return idx
        return None

    def _parent_is_class(self) -> bool:
        """True when the direct enclosing scope is a class."""
        pidx = self._parent_idx
        if pidx is not None and 0 <= pidx < len(self.symbols):
            return self.symbols[pidx].get("kind") == "class"
        return False

    # -- Module ────────────────────────────────────────────────────────
    def visit_Module(self, node: ast.Module) -> None:
        module_name = Path(self.rel_path).stem
        end_ln = _module_end_line(node)
        self.stack.append((module_name, -1))  # sentinel index for module

        self.symbols.append({
            "kind": "module",
            "name": module_name,
            "qualified_name": module_name,
            "parent_id": None,
            "signature": "",
            "decorators": [],
            "start_line": 1,
            "end_line": end_ln,
            "docstring": _get_docstring(node),
            "source_path": self.rel_path,
        })

        for child in node.body:
            if isinstance(child, ast.ClassDef):
                self.visit_ClassDef(child)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind_k = ("async_function"
                          if isinstance(child, ast.AsyncFunctionDef)
                          else "function")
                self._visit_func_node(child, kind_k, is_inside_class=False)

        self.stack.pop()

    # -- ClassDef ──────────────────────────────────────────────────────
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qname = (f"{self._parent_qname}.{node.name}"
                 if self._parent_qname else node.name)
        parent_id = self._parent_idx

        # Push scope first so nested items see us via _parent_qname
        cur_idx = len(self.symbols)
        self.stack.append((qname, cur_idx))

        self.symbols.append({
            "kind": "class",
            "name": node.name,
            "qualified_name": qname,
            "parent_id": parent_id,
            "signature": "",
            "decorators": _extract_decorators(node),
            "start_line": node.lineno,
            "end_line": node.end_lineno or 0,
            "docstring": _get_docstring(node),
            "source_path": self.rel_path,
        })

        for child in node.body:
            if isinstance(child, ast.ClassDef):
                self.visit_ClassDef(child)  # nested class
            elif isinstance(child,
                            (ast.FunctionDef, ast.AsyncFunctionDef)):
                kk = ("async_method"
                      if isinstance(child, ast.AsyncFunctionDef)
                      else "method")
                self._visit_func_node(child, kk, is_inside_class=True)

        # Pop only this class's scope entry.
        if (self.stack and self.stack[-1][0] == qname
                and self.stack[-1][1] == cur_idx):
            self.stack.pop()

    # -- Function / AsyncFunction ──────────────────────────────────────
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func_node(node, "function", is_inside_class=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func_node(node, "async_function", is_inside_class=False)

    def _visit_func_node(self,
                         node_ast: ast.FunctionDef | ast.AsyncFunctionDef,
                         kind: str,
                         *,
                         is_inside_class: bool = False) -> None:
        name = node_ast.name
        qname = (f"{self._parent_qname}.{name}"
                 if self._parent_qname else name)

        # Inside a class → method / async_method.
        effective_kind = kind
        if is_inside_class or self._parent_is_class():
            effective_kind = ("async_method"
                              if isinstance(node_ast, ast.AsyncFunctionDef) else "method")

        parent_id = self._parent_idx if self._parent_idx is not None else None

        # Push current scope so children see us as parent.
        cur_idx = len(self.symbols)
        self.stack.append((qname, cur_idx))

        self.symbols.append({
            "kind": effective_kind,
            "name": name,
            "qualified_name": qname,
            "parent_id": parent_id,
            "signature": _signature_from_node(node_ast),
            "decorators": _extract_decorators(node_ast),
            "start_line": node_ast.lineno,
            "end_line": node_ast.end_lineno or 0,
            "docstring": _get_docstring(node_ast),
            "source_path": self.rel_path,
        })

        for child in node_ast.body:
            if isinstance(child, ast.ClassDef):
                self.visit_ClassDef(child)  # nested class in function
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nk = ("async_function"
                      if isinstance(child, ast.AsyncFunctionDef)
                      else "function")
                self._visit_func_node(child, nk, is_inside_class=False)

        # Pop.
        if (self.stack and self.stack[-1][0] == qname
                and self.stack[-1][1] == cur_idx):
            self.stack.pop()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_file_symbols(rel_path: str, source: str) -> list[dict]:
    """Parse *source* text (for file at *rel_path*) and return a list of symbol dicts.

    Returns an empty list on any parse / AST error — never raises.
    """
    if not _is_python_file(rel_path):
        return []

    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return []

    visitor = _Visitor(rel_path)
    try:
        visitor.visit(tree)
    except RecursionError:
        return []
    except Exception:
        return []

    return visitor.symbols


def extract_symbols_for_files(
    files_meta: list[dict],
) -> tuple[list[dict], dict[str, int]]:
    """Extract symbols for a batch of file metadata dicts.

    Each *files_meta* entry must have ``rel_path`` and ``abs_path`` keys.

    Returns:
        ``(symbols_list, stats)`` where *stats* has keys
        ``parsed_ok``, ``parse_errors``, ``total_python``, ``total_symbols``.
    """
    all_symbols: list[dict] = []
    parsed_ok = 0
    parse_errors = 0

    for finfo in files_meta:
        rel = finfo.get("rel_path", "")
        abs_p = finfo.get("abs_path", "")
        if not _is_python_file(rel):
            continue

        try:
            with open(abs_p, "r", encoding="utf-8", errors="replace") as fh:
                source = fh.read()
        except (OSError, IOError, UnicodeDecodeError):
            parse_errors += 1
            continue

        syms = extract_file_symbols(rel, source)
        if syms:
            parsed_ok += 1
        else:
            # Distinguish valid Python with no callable/class from real errors
            try:
                ast.parse(source, filename=rel)
                parsed_ok += 1  # valid but no extracted symbol kind we care about
            except SyntaxError:
                parse_errors += 1

        all_symbols.extend(syms)

    stats_dict = {
        "parsed_ok": parsed_ok,
        "parse_errors": parse_errors,
        "total_python": sum(1 for f in files_meta if _is_python_file(f.get("rel_path", ""))),
        "total_symbols": len(all_symbols),
    }

    return all_symbols, stats_dict


# ---------------------------------------------------------------------------
# Import extraction — public API
# ---------------------------------------------------------------------------


def _make_name_entry(alias_node: ast.alias) -> tuple[str, str | None]:
    """Return an immutable ``(name, asname)`` pair for a single AST *alias* node."""
    return (alias_node.name, alias_node.asname)


def _handle_import(
    stmt_type: str,
    node: ast.Import | ast.ImportFrom,
    rel_path: str,
) -> list[ImportRecord]:
    """Core logic shared by ``ast.Import`` and ``ast.ImportFrom`` handling."""
    start_col = getattr(node, "col_offset", 0)
    if isinstance(node, ast.Import):
        # `import os` — one record per alias; module = the dotted name itself.
        records: list[ImportRecord] = []
        for alias in node.names:
            records.append(
                ImportRecord(
                    kind="import",
                    module=alias.name,
                    names=(_make_name_entry(alias),),
                    level=0,
                    source_path=rel_path,
                    start_line=node.lineno,
                    start_col=start_col,
                )
            )
        return records

    # ImportFrom
    mod_name = node.module  # may be None for ``from . import ...``
    level = getattr(node, "level", 0) or 0
    recs: list[ImportRecord] = []

    if not node.names:
        return recs

    # Preserve source order — do NOT alphabetically sort.
    names_list: tuple[tuple[str, str | None], ...] = tuple(
        _make_name_entry(a) for a in node.names
    )
    mod = mod_name or ""  # empty string for `from . import X`
    if stmt_type == "from_import":
        recs.append(
            ImportRecord(
                kind="from_import",
                module=mod,
                names=names_list,
                level=level,
                source_path=rel_path,
                start_line=node.lineno,
                start_col=start_col,
            )
        )

    return recs


def extract_file_imports(rel_path: str, source: str) -> list[ImportRecord]:
    """Parse *source* text (for file at *rel_path*) and collect import declarations.

    Returns an empty list on any parse / AST error — never raises.
    Imports are collected by walking the AST so that imports inside functions,
    classes, ``if``/``try`` blocks, etc. are all discovered.

    Results are **sorted by ``(start_line, start_col)``** (stable) so that
    imports appear in exact lexical source-order regardless of
    ``ast.walk()`` traversal direction.  When two distinct import statements
    share the same physical line, ``start_col`` distinguishes them by their
    column offset from the AST.  Aliases from the same AST node keep their
    original relative order because Python's :py:func:`sorted` is stable.
    """
    if not _is_python_file(rel_path):
        return []

    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return []

    result: list[ImportRecord] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            extracted = _handle_import(
                stmt_type="import" if isinstance(node, ast.Import) else "from_import",
                node=node,
                rel_path=rel_path,
            )
            result.extend(extracted)

    return sorted(result, key=lambda r: (r.start_line, r.start_col))


def extract_imports_for_files(
    files_meta: list[dict],
) -> tuple[list[ImportRecord], dict[str, int]]:
    """Extract imports for a batch of file metadata dicts.

    Each *files_meta* entry must have ``rel_path`` and ``abs_path`` keys.

    Returns:
        ``(import_records, stats)`` where *stats* has keys
        ``parsed_ok``, ``parse_errors``, ``total_python``, ``total_imports``.
    """
    all_imports: list[ImportRecord] = []
    parsed_ok = 0
    parse_errors = 0

    for finfo in files_meta:
        rel = finfo.get("rel_path", "")
        abs_p = finfo.get("abs_path", "")
        if not _is_python_file(rel):
            continue

        try:
            with open(abs_p, "r", encoding="utf-8", errors="replace") as fh:
                source = fh.read()
        except (OSError, IOError, UnicodeDecodeError):
            parse_errors += 1
            continue

        imps = extract_file_imports(rel, source)
        if imps or _can_parse(source):
            parsed_ok += 1
        else:
            parse_errors += 1

        all_imports.extend(imps)

    return all_imports, {
        "parsed_ok": parsed_ok,
        "parse_errors": parse_errors,
        "total_python": sum(1 for f in files_meta if _is_python_file(f.get("rel_path", ""))),
        "total_imports": len(all_imports),
    }


def _can_parse(source: str) -> bool:
    """Check whether *source* is syntactically valid Python."""
    try:
        ast.parse(source)
        return True
    except SyntaxError:
        return False
