"""Deterministic static module inventory over indexed repository files.

Phase 2C-1 only.

Given an *existing* file inventory (repository-relative record dicts in the
shape of the ``files`` table) and an explicitly supplied, non-empty
collection of package roots, this module maps indexed ``.py`` files to
candidate qualified Python module names.

The mapping is purely structural:

- Only the ``.py`` suffix is treated as a module-file target.  ``.pyi``,
  ``.pyx``, and ``.pyw`` files, files whose existing inventory metadata
  marks them as binary, and files of any other language are never emitted
  as candidate modules.
- No import statements are inspected.  No Python module is executed.
  Nothing here claims that any candidate *resolves* at runtime, or that a
  particular candidate is a preferred import target.
- The inventory is defined over *persisted index state*.  It may contain
  records for files that were later deleted from the working tree, and it
  never reads, follows, or re-resolves any of them.

The public relation is::

    (root, qualified_module_name, indexed_file_path)

sorted by ``(root.canonical, qualified_name, file_path)`` using standard
code-point (``ord``) ordering — e.g. uppercase sorts before lowercase,
so ``"A" < "a"``.  SQLite row IDs and source-row insertion order are
never exposed and do not influence the output.

Unsupported path components
---------------------------

A source component (directory name or filename stem) is considered to
contain an *unsupported module-name component* when it contains any
character outside the plain ASCII identifier character set
``[A-Za-z0-9_]`` (with the additional restrictions below).  In that case
the file is still included in the inventory — the candidate qualified name
is retained so the structural relationship is visible to the caller — but
the candidate is flagged with ``component_supported=False`` in
:data:`ModuleCandidate` when any single directory/stem component is
unsupported, and ``name_supported=False`` when the whole dotted name
could never be a legal dotted-identifier module name in an ``import``
statement.

  Component-level unsupported characters:
    * whitespace, any Unicode non-ASCII letter, ``-`` (hyphen),
      ``+``, ``*``, ``.`` (literal dot within a component),
      other punctuation, empty strings.  (Whitespace and hyphens are
      rejected for qualified *names* but are permitted verbatim within a
      valid search-path *root* directory name — see
      :func:`_canonicalize_root`.)
  Name-level (whole qualified name, across all dotted parts) unsupported
    additionally: leading digits in any component (e.g. ``1abc``),
    Python keyword collisions in any component (e.g. ``class``, ``for``).

A name component containing unsupported characters (e.g. a hyphen as in
``my-plugin``, or a space as in ``my tool``) is retained verbatim as a
structural candidate (``component_supported=False``,
``name_supported=False``) and never sanitized (e.g. never rewritten to
``my_plugin``).
"""

from __future__ import annotations

import keyword
import os
import re
import sqlite3
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: File suffix (case-sensitive) that is eligible as a module-file target.
_PY_SUFFIX = ".py"

#: The exact basename that identifies a package initializer file.
_INIT_BASENAME = "__init__.py"

# A plain-component character class for :func:`_component_is_plain`:
# letters, digits, underscores (any order).  Digit-leading names and
# keyword collisions are distinguished at the *name* level by
# :func:`_name_is_supported`.
_COMPONENT_CHARS_RE = re.compile(r"^[A-Za-z0-9_]+$")

# Python reserved *hard* keywords are detected with the standard library
# :func:`keyword.iskeyword`, which covers only true keywords (e.g. ``for``,
# ``class``, ``if``).  Python *soft* keywords (``match``, ``case``) are
# deliberately NOT treated as reserved: they remain usable as ordinary
# identifiers and therefore as legal ``import`` module names (e.g.
# ``import match`` is valid).  No hand-maintained keyword list is used.


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PackageRoot:
    """A canonical, normalized package root.

    Attributes:
        canonical:  Repository-relative POSIX canonical spelling; ``""``
            denotes the repository root (displayed as ``"."`` in
            diagnostics and evidence strings).  No leading ``./``, no
            trailing ``/``, no ``..`` components.
        original:   The deterministic representative caller-supplied
            spelling (diagnostic only; the lexicographically smallest
            spelling among equivalent inputs).  Never used for
            identity or ordering; excluded from equality and hash
            comparisons via ``compare=False``.

    Note:
        Two :class:`PackageRoot` objects are equal when their
        ``canonical`` spellings are equal, regardless of the
        ``original`` spelling.  Hashing is likewise based on ``canonical``
        only.
    """

    canonical: str
    original: str = field(compare=False, repr=False)

    def __hash__(self) -> int:
        return hash(("PackageRoot", self.canonical))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PackageRoot):
            return NotImplemented
        return self.canonical == other.canonical

    def __lt__(self, other: "PackageRoot") -> bool:
        # Explicit ordering by canonical spelling only, so that tuples
        # containing a PackageRoot (e.g. dataclass comparison of
        # ModuleCandidate) never depend on the diagnostic ``original``
        # spelling.
        if not isinstance(other, PackageRoot):
            return NotImplemented
        return self.canonical < other.canonical


@dataclass(frozen=True)
class ModuleCandidate:
    """One entry in the module-inventory relation.

    Attributes:
        file_path:            Repository-relative path of the indexed file
                              (e.g. ``"pkg/sub/worker.py"``).
        root:                 :class:`PackageRoot` under which this file
                              is a candidate (its ``canonical`` spelling
                              is the identity).
        qualified_name:       Dotted candidate module name (e.g.
                              ``"pkg.sub.worker"``).  Retained even when
                              the name cannot legally appear in a Python
                              ``import`` statement; see
                              :data:`name_supported` and
                              :data:`component_supported`.
        name_components:      The *actual* component identity — a tuple of
                              the original path components that the dotted
                              name was derived from (e.g.
                              ``"a.b.c"`` -> ``("a", "b", "c")`` but
                              ``"a.b.c"`` from the literal-dot file
                              ``a/b.c.py`` -> ``("a", "b.c")``).  This
                              preserves filesystem component boundaries
                              and distinguishes a literal dot inside one
                              component from separate module components,
                              so two files whose dotted ``qualified_name``
                              display the same (because of a literal dot
                              in one) are NOT conflated.  Identity,
                              deduplication, and dotted-name lookup are
                              keyed on ``(root, name_components)``; the
                              dotted ``qualified_name`` is a display
                              alias of that identity.
        file_kind:            ``"ordinary_module"`` or
                              ``"package_initializer"``.
        directory_kind:       ``"explicit_root"`` (file sits directly on the
                              configured root, or the candidate directory
                              *is* the configured root),
                              ``"regular_package"`` (containing directory
                              has an explicit ``__init__.py`` record),
                              or ``"namespace_package"`` (containing
                              directory lacks ``__init__.py`` but has
                              indexed ``.py`` descendant evidence).
        component_supported:  ``False`` when any single directory or
                              stem component of the dotted name contains
                              a character outside ``[A-Za-z_]``.
        name_supported:       ``False`` when the qualified name cannot be
                              a legal dotted-identifier import target
                              (any component is not a valid identifier,
                              or any component collides with a Python
                              keyword, or the name starts with a
                              digit-lead component).
        evidence:             Tuple of structural-evidence strings (see
                              the ``evidence`` keys below).
    """

    file_path: str
    root: "PackageRoot"
    qualified_name: str
    name_components: tuple[str, ...]
    file_kind: str
    directory_kind: str
    component_supported: bool = True
    name_supported: bool = True
    evidence: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Normalization helpers (private)
# ---------------------------------------------------------------------------

def _canonicalize_root(root_input: object) -> str:
    """Validate and normalize a single root input.

    Returns the canonical repository-relative POSIX string.
    Raises:
        TypeError:    When *root_input* is not a string.
        ValueError:   When the root is absolute, escapes the repository,
                      is empty, or contains unsupported components.
    """
    if not isinstance(root_input, str):
        raise TypeError(
            f"Root must be a string; got {type(root_input).__name__}"
        )
    if root_input.strip() == "":
        raise ValueError("Root must be a non-empty string; got an empty value")

    # Reject absolute paths explicitly (not part of this API).
    if root_input.startswith(("/", "\\", ".\\")) or ":" in root_input:
        raise ValueError(f"Absolute roots are not permitted: {root_input!r}")

    # "." (or any dot-only/empty-remainder spelling such as "./") means
    # the repository root itself.  The canonical internal spelling for
    # the repository root is the empty string (no path components); it
    # is displayed as "." in diagnostics and evidence strings.  Leading
    # and trailing slashes and empty components (e.g. repeated "/") are
    # collapsed (POSIX semantics), but the *interior* of each path
    # component is preserved verbatim: a component containing internal
    # whitespace (e.g. ``"my tool"``) is a legal search-path directory
    # and must NOT be silently rewritten.  Leading/trailing whitespace
    # *inside* a component would be silently rewritten by naive
    # normalization and is therefore rejected explicitly (see validation
    # loop below).
    parts = [p for p in root_input.split("/") if p not in ("", ".")]
    if not parts:
        return ""

    for part in parts:
        if part in (".", ".."):
            raise ValueError(
                f"Root component {part!r} in {root_input!r} is not allowed"
            )
        if part != part.strip():
            raise ValueError(
                f"Root component {part!r} in {root_input!r} has leading or "
                "trailing whitespace, which would be silently rewritten; "
                "supply the component without such whitespace"
            )
        if any(ord(ch) < 0x20 or ch in "\x7f/:\\" for ch in part):
            raise ValueError(
                f"Root component {part!r} in {root_input!r} contains "
                "unsupported characters"
            )

    canonical = "/".join(parts)
    return canonical


def _canonical_roots(roots: object) -> tuple["PackageRoot", ...]:
    """Validate, normalize, and deduplicate a collection of roots.

    Returns a tuple of :class:`PackageRoot` objects sorted by canonical
    spelling (the repository root ``""`` first, then component-wise).
    Raises:
        TypeError: When *roots* is not an iterable of strings, or is
                   empty.
        ValueError: When any individual root fails canonicalization, or
                    when the collection is empty.
    """
    if roots is None:
        raise ValueError("Root collection must not be None")

    try:
        items = list(roots)
    except TypeError:
        raise TypeError(
            f"Roots must be an iterable of strings; got {type(roots).__name__}"
        ) from None

    if not items:
        raise ValueError("Root collection must be non-empty")

    seen: dict[str, str] = {}  # canonical -> deterministic representative spelling
    for item in items:
        canonical = _canonicalize_root(item)
        existing = seen.get(canonical)
        if existing is None:
            seen[canonical] = item
        elif item < existing:
            # Equivalent spellings normalize to the same canonical
            # identity; keep the lexicographically smallest supplied
            # spelling as the single diagnostic representative so that
            # serialized output is independent of input order.
            seen[canonical] = item

    return tuple(
        PackageRoot(canonical=c, original=seen[c])
        for c in sorted(seen, key=lambda c: (c != "", c))
    )


def _split_rel_path(rel_path: str) -> tuple[str, ...]:
    """Split a repository-relative POSIX path into components.

    Requires a **canonical** repository-relative POSIX spelling:
    no leading ``/``, no leading ``./``, no embedded ``.`` components,
    no double (or trailing) slashes, no ``..`` components.  The
    scanner already emits canonical paths, so this invariant is expected
    to hold for indexed file records.  Noncanonical spellings are
    *rejected* with a deterministic :class:`ValueError` rather than
    partially and inconsistently normalized, which would risk producing
    incorrect module candidates or package classifications.

    Valid unusual filename components (e.g. hyphens, literal dots inside
    one component, spaces, Unicode letters) are preserved verbatim —
    only the structural path format is constrained.
    """
    if not rel_path:
        raise ValueError(
            "File path must be a non-empty repository-relative POSIX path"
        )
    if rel_path.startswith("/"):
        raise ValueError(f"Absolute file path in index: {rel_path!r}")
    raw_parts = rel_path.split("/")
    # Reject repeated/trailing separators (empty components).
    if any(p == "" for p in raw_parts):
        raise ValueError(
            f"File path {rel_path!r} has empty/repeated separators; "
            "indexed records must be canonical"
        )
    # Reject "." and ".." components anywhere (with their position).
    for i, p in enumerate(raw_parts):
        if p == ".":
            raise ValueError(
                "File path " + repr(rel_path)
                + " has a '.' component at index " + str(i)
                + "; indexed records must be canonical"
            )
        if p == "..":
            raise ValueError(
                "File path " + repr(rel_path)
                + " has a '..' component at index " + str(i)
                + "; repository escapes are not allowed"
            )
    parts = tuple(raw_parts)
    if not parts:
        raise ValueError(
            f"File path {rel_path!r} has no components"
        )
    return parts


def _root_parts(canonical: str) -> tuple[str, ...]:
    """Split a canonical root into path components (empty for ``""``)."""
    if canonical == "":
        return ()
    return tuple(canonical.split("/"))


def _is_under_root(file_parts: tuple[str, ...], root_parts: tuple[str, ...]) -> tuple[bool, tuple[str, ...] | None]:
    """Return (under, remainder_parts) — *under* is True when
    *file_parts* starts with *root_parts* (exact component boundary)."""
    if len(root_parts) > len(file_parts):
        return False, None
    if file_parts[:len(root_parts)] != root_parts:
        return False, None
    return True, file_parts[len(root_parts):]


# ---------------------------------------------------------------------------
# Name-support analysis
# ---------------------------------------------------------------------------

def _component_is_plain(part: str) -> bool:
    """True when *part* matches a plain ASCII identifier character class
    (no leading-digit check; use :func:`_name_is_supported` for that).

    Uses :meth:`re.Pattern.fullmatch` so the *entire* component is
    validated; the ``$`` anchor alone in ``match()`` would also accept
    a component with a trailing newline (e.g. ``"abc\\n"``), so it is not
    relied upon.
    """
    return bool(part) and _COMPONENT_CHARS_RE.fullmatch(part) is not None


def _name_is_supported(dotted_name: str, parts: tuple[str, ...]) -> tuple[bool, bool]:
    """Return ``(component_supported, name_supported)`` for a dotted name.

    *component_supported* is False when any single part has a character
    not in ``[A-Za-z0-9_]``.
    *name_supported* is additionally False when any part is a Python
    *hard* keyword (per :func:`keyword.iskeyword`) or starts with a digit
    (making the dotted name invalid as an ``import`` target).  Soft
    keywords such as ``match`` and ``case`` are not treated as reserved.
    """
    comp_ok = all(
        _component_is_plain(p) and p not in (".", "..") for p in parts
    )
    if not comp_ok:
        return False, False
    name_ok = all(
        not p[0].isdigit() and not keyword.iskeyword(p)
        for p in parts
    )
    return True, name_ok


# ---------------------------------------------------------------------------
# Directory-kind classification
# ---------------------------------------------------------------------------

def _classify_directory(
    dir_parts: tuple[str, ...],
    root_canonical: str,
    record_paths: frozenset[str],
) -> str:
    """Classify a directory (identified by *dir_parts* relative to a
    root) against the file inventory.

    ``dir_parts`` is the directory's path relative to the configured
    root; repository-relative record paths are matched against
    *record_paths* after prefixing with *root_canonical*.

    Returns one of:
        ``"explicit_root"``        — the directory is the configured
                                       import root (``dir_parts == ()``).
        ``"regular_package"``      — contains an explicit
                                       ``__init__.py`` record.
        ``"namespace_package"``    — lacks an explicit ``__init__.py``
                                       record but is supported by at
                                       least one ``.py`` descendant.
    """
    if not dir_parts:
        return "explicit_root"

    prefix = "/".join(dir_parts)
    if root_canonical:
        prefix = root_canonical + "/" + prefix

    # Regular package: explicit __init__.py at this directory level.
    if prefix + "/__init__.py" in record_paths:
        return "regular_package"

    # Namespace portion: any descendant .py record under this directory.
    if any(rp.startswith(prefix + "/") and rp.endswith(".py")
           for rp in record_paths):
        return "namespace_package"

    # Defensive fallback for externally supplied record sets that do not
    # include the candidate file itself as evidence.
    return "namespace_package"


# ---------------------------------------------------------------------------
# Core mapping (pure function)
# ---------------------------------------------------------------------------

def build_module_inventory(
    file_records: object,
    roots: object,
) -> tuple[ModuleCandidate, ...]:
    """Map indexed ``.py`` file records to candidate qualified module
    names under the explicitly supplied package roots.

    Args:
        file_records:  An iterable of per-file record dicts.  Each dict
            must contain ``"rel_path"`` (str, repository-relative POSIX
            path).  If the key ``"is_binary"`` is present and truthy,
            the file is excluded regardless of its suffix.
            If ``"language"`` is present and is not ``"Python"``
            (case-insensitive), the file is excluded.
        roots:         A non-empty iterable of root strings (see
            :func:`normalize_roots`).

    Returns:
        A tuple of :class:`ModuleCandidate`, sorted by
        ``(root.canonical, qualified_name, file_path)``.
        Sorted by Unicode code point (``sorted`` default).

    Raises:
        TypeError:  On non-iterable or non-string inputs.
        ValueError: For empty root collections, absolute/escaping roots,
                    or malformed file paths in the index.

    Notes:
        * Deterministic.  Independent of insertion order, SQLite row IDs,
          and filesystem traversal order.
        * Read-only.  No writes of any kind.
        * ``__init__.py`` directly at the configured root is *not*
          emitted: no qualified name can be derived without inventing a
          parent package identity, which is explicitly forbidden.
    """
    normed_roots = _canonical_roots(roots)
    normed_files = _canonical_records(file_records)
    record_paths = frozenset(rec["rel_path"] for rec in normed_files)

    candidates: list[ModuleCandidate] = []

    for root in normed_roots:
        root_parts = _root_parts(root.canonical)

        for rec in normed_files:
            file_path = rec["rel_path"]
            if not file_path.endswith(_PY_SUFFIX):
                continue
            if rec.get("is_binary"):
                continue
            lang = (rec.get("language") or "").strip().lower()
            if lang and lang != "python":
                continue

            parts = _split_rel_path(file_path)
            under, remainder = _is_under_root(parts, root_parts)
            if not under:
                continue
            assert remainder is not None  # guaranteed by _is_under_root

            is_init = parts[-1] == _INIT_BASENAME

            # --- __init__.py directly at the configured root ------------
            # ``root/pkg/__init__.py`` gives remainder ``("pkg",
            # "__init__.py")`` (still a normal package); only
            # ``root/__init__.py`` (remainder ``("__init__.py",)``) has
            # no parent package to name — skip it explicitly.
            if is_init and len(remainder) == 1:
                continue

            # --- Derive the dotted name ---------------------------------
            if is_init:
                # Package initializer: name = all remainder parts except
                # the trailing "__init__.py", i.e. the containing dir.
                name_parts = remainder[:-1]
                file_kind = "package_initializer"
            else:
                # Ordinary module: name = all dir parts of remainder +
                # the filename stem.
                name_parts = remainder[:-1] + (remainder[-1][:-len(_PY_SUFFIX)],)
                file_kind = "ordinary_module"

            if not name_parts:
                # An ordinary module directly at the root with an empty
                # stem is impossible (it would be a dir); skip defensively
                # — a file named ".py" is not a valid module record.
                continue

            dotted = ".".join(name_parts)

            # Component-level support analysis.
            component_supported, name_supported = _name_is_supported(
                dotted, name_parts
            )

            # Directory-kind classification for the parent dir (relative
            # to the same root), evaluated against the full inventory so
            # that __init__.py / descendant .py evidence from *any* root is
            # visible to the classification.
            parent_rel_parts = remainder[:-1]  # dirs above the file
            dir_kind = _classify_directory(
                parent_rel_parts, root.canonical, record_paths
            )

            # --- Evidence strings ---------------------------------------
            evidence: list[str] = [f"root:{root.canonical or '.'}",
                                   f"file_kind:{file_kind}"]
            if dir_kind == "regular_package":
                evidence.append(
                    "dir_kind:regular_package (explicit __init__.py indexed)"
                )
            elif dir_kind == "namespace_package":
                evidence.append(
                    "dir_kind:namespace_package (indexed .py descendant evidence)"
                )
            elif dir_kind == "explicit_root":
                evidence.append(
                    "dir_kind:explicit_root (no package directory above file)"
                )
            evidence.append("static:structural_only (no runtime binding)")

            candidates.append(ModuleCandidate(
                file_path=file_path,
                root=root,
                qualified_name=dotted,
                name_components=tuple(name_parts),
                file_kind=file_kind,
                directory_kind=dir_kind,
                component_supported=component_supported,
                name_supported=name_supported,
                evidence=tuple(evidence),
            ))

    # Approved public ordering contract:
    #   (root.canonical, qualified_name, file_path)
    # ``name_components`` is added only as a final tie-breaker to give a
    # total deterministic order when two DISTINCT file paths share the same
    # (root, qualified_name, file_path) spelling (structurally impossible
    # here, but harmless and future-proof).  It never changes the relative
    # order of the approved public fields (e.g. "a-b" still sorts before
    # "a.b", since "-" (0x2D) < "." (0x2E)).
    sort_key = lambda c: (
        c.root.canonical,
        c.qualified_name,
        c.file_path,
        c.name_components,
    )
    return tuple(sorted(candidates, key=sort_key))


def _canonical_records(
    file_records: object,
) -> list[dict]:
    """Normalize file records into a list sorted by rel_path.

    * Every supplied record's ``rel_path`` is validated against the
      canonical repository-relative POSIX path contract **before** any
      metadata (suffix / binary / language) eligibility filtering and
      before deduplication, so malformed paths cannot be silently skipped
      or concealed by a duplicate.
    * Duplicate records for the same ``rel_path`` with **identical**
      relevant metadata (``language``, ``is_binary``) are deduplicated.
    * Duplicates with **conflicting** relevant metadata are rejected with
      a deterministic :class:`ValueError` whose text is a canonical,
      sorted enumeration of the conflicting identities — identical for
      every input ordering of the same record set (no first-match
      selection, no reliance on dictionary insertion order).
    """
    if file_records is None:
        raise TypeError("file_records must not be None")
    try:
        items = list(file_records)
    except TypeError:
        raise TypeError(
            f"file_records must be an iterable of dicts; got {type(file_records).__name__}"
        ) from None

    # rel_path -> distinct normalized (language, is_binary) identities.
    identities: dict[str, set[tuple[object, object]]] = {}
    first_seen: dict[str, dict] = {}

    for i, rec in enumerate(items):
        if not isinstance(rec, dict):
            raise TypeError(
                f"file_records[{i}] must be a dict; got {type(rec).__name__}"
            )
        rp = rec.get("rel_path")
        if not isinstance(rp, str) or not rp:
            raise ValueError(
                f"file_records[{i}] is missing a valid 'rel_path' string"
            )
        # Correction 2: validate the canonical path for EVERY record,
        # regardless of suffix/binary/language, so noncanonical paths are
        # never partially normalized or silently skipped.
        _split_rel_path(rp)

        lang = rec.get("language")
        lang_norm = lang.strip().lower() if isinstance(lang, str) else None
        bin_flag = bool(rec.get("is_binary", False))
        identities.setdefault(rp, set()).add((lang_norm, bin_flag))
        first_seen.setdefault(rp, rec)

    # Correction 3: deterministic conflict diagnostics.
    conflicting_paths = sorted(
        rp for rp, ids in identities.items() if len(ids) > 1
    )
    if conflicting_paths:
        segments = []
        for rp in conflicting_paths:
            id_parts = ", ".join(
                f"(language={lang!r}, is_binary={bin_flag})"
                for lang, bin_flag in sorted(
                    identities[rp], key=lambda t: (repr(t[0]), t[1])
                )
            )
            segments.append(f"rel_path={rp!r}: {id_parts}")
        raise ValueError(
            "conflicting duplicate records: " + " | ".join(segments)
        )

    return [first_seen[rp] for rp in sorted(first_seen)]


# ---------------------------------------------------------------------------
# Public convenience wrappers
# ---------------------------------------------------------------------------

def candidates_for_file(
    inventory: tuple[ModuleCandidate, ...] | list[ModuleCandidate],
    file_path: str,
) -> tuple[ModuleCandidate, ...]:
    """Return all candidates for *file_path*, preserving inventory order.

    Raises:
        KeyError: When *file_path* is not present in the inventory.
    """
    matched = tuple(c for c in inventory if c.file_path == file_path)
    if not matched:
        raise KeyError(
            f"file_path {file_path!r} is not present in the inventory"
        )
    return matched


def candidates_for_name(
    inventory: tuple[ModuleCandidate, ...] | list[ModuleCandidate],
    qualified_name: str,
) -> tuple[ModuleCandidate, ...]:
    """Return all candidates whose *component identity* is
    ``tuple(qualified_name.split("."))``, preserving order.

    Literal-dot components are excluded from this lookup by identity:
    a candidate whose ``name_components`` contain a literal dot (e.g.
    ``("a", "b.c")`` from ``a/b.c.py``) is *not* matched by
    ``"a.b.c"`` (which is the identity ``("a", "b", "c")`` of the
    normally structured ``a/b/c.py``).  Such candidates remain
    observable in the inventory and via :func:`candidates_for_file`.

    Use :func:`is_ambiguous` on the returned set to determine whether
    more than one distinct file path is present (structural multi-file
    ambiguity).
    """
    identity = tuple(qualified_name.split("."))
    matched = tuple(
        c for c in inventory
        if c.name_components == identity
        and c.qualified_name == qualified_name
    )
    if not matched:
        raise KeyError(
            f"qualified_name {qualified_name!r} is not present in the inventory"
        )
    return matched


def is_ambiguous(
    candidates: tuple[ModuleCandidate, ...] | list[ModuleCandidate],
) -> bool:
    """True when *candidates* contains more than one distinct
    ``file_path`` — structural multi-file ambiguity.

    Multiple evidence records (different roots) for the *same* file
    path do **not** constitute multi-file ambiguity.
    """
    distinct_files = {c.file_path for c in candidates}
    return len(distinct_files) > 1


# ---------------------------------------------------------------------------
# Read-only SQLite adapter (Phase 2C-1 database interface)
# ---------------------------------------------------------------------------

def _quote_uri_path(abs_path: str) -> str:
    """Percent-encode an absolute path for use in a ``file:`` SQLite URI.

    Preserves path separators (``/``) and quotes every other
    URI-significant character in a way safe for the URI authority
    context (``quote`` with ``safe='/'``).  ``#``, ``?``, ``%`` and
    literal percent sequences (e.g. an existing ``%3F``) are all
    encoded so that SQLite's URI parser recovers the exact filesystem
    path.
    """
    from urllib.parse import quote
    return quote(abs_path, safe="/")


def load_file_records_from_db(
    db_file: str | sqlite3.Connection,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """Load file records from an existing index database (read-only).

    Issues a single SELECT over the pre-existing ``files`` table
    (rel_path, language, is_binary), ordered by ``rel_path``.  No
    CREATE, INSERT, UPDATE, DELETE, or DDL statements of any kind are
    executed, and no tables or schema versions are probed or fixed up.

    Args:
        db_file: Path to the database file or an open connection.
        limit:   Maximum rows (default: None — no limit).
        offset:  Row offset (default 0).

    Returns:
        A list of dicts with keys ``rel_path``, ``language`` (when the
        column exists), and ``is_binary`` (defaults to ``False`` when
        the column is absent), sorted by ``rel_path``.

    Raises:
        sqlite3.OperationalError: If the ``files`` table does not
            exist in *db_file* (no tables are created as a result).

    Notes:
        * When a path string is supplied, the database file must already
          exist; it is opened strictly read-only (URI mode ``ro``).  A
          missing path raises ``FileNotFoundError`` and no file is
          created.
        * When an open connection is supplied, it is used as-is and is
          *not* closed by this function.
    """
    if isinstance(db_file, sqlite3.Connection):
        conn = db_file
        close_own = False
    else:
        if not os.path.exists(db_file):
            raise FileNotFoundError(
                f"index database not found: {db_file!r} "
                "(Phase 2C-1 never creates index databases)"
            )
        # ``uri=True`` + ``file:<abs-path>?mode=ro`` gives a SQLite-enforced
        # read-only handle: every write (INSERT/UPDATE/DELETE/DDL) is
        # rejected by the database engine, and a file can never be
        # created by opening.  The absolute path is percent-encoded
        # (via ``urllib.parse.quote``) so URI-significant characters in
        # the filename (``#``, ``?``, ``%``, e.g. literal ``%3F``) are
        # preserved exactly rather than being misparsed as a fragment
        # or query string.  Path separators are left literal.
        abs_path = os.path.abspath(db_file)
        uri = "file:" + _quote_uri_path(abs_path) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        close_own = True
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(files)")]
        if not cols:
            raise sqlite3.OperationalError("no such table: files")
        has_lang = "language" in cols
        has_bin = "is_binary" in cols

        columns = ["rel_path"]
        if has_lang:
            columns.append("language")
        if has_bin:
            columns.append("is_binary")

        query = "SELECT " + ", ".join(columns) + " FROM files ORDER BY rel_path"
        params: list = []
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        if offset > 0:
            # SQLite requires a LIMIT clause whenever OFFSET is present.
            # ``LIMIT -1`` imposes no row cap, so offset-only paging is
            # expressed with SQLite-valid syntax.
            if limit is None:
                query += " LIMIT -1"
            query += " OFFSET ?"
            params.append(int(offset))

        rows = conn.execute(query, params).fetchall()
        records = []
        for row in rows:
            rec: dict = {"rel_path": row[0]}
            pos = 1
            if has_lang:
                rec["language"] = row[pos]
                pos += 1
            rec["is_binary"] = bool(row[pos]) if has_bin else False
            records.append(rec)
        return records
    finally:
        if close_own:
            conn.close()


def build_inventory_from_db(
    db_file: str | sqlite3.Connection,
    roots: object,
) -> tuple[ModuleCandidate, ...]:
    """Convenience: load file records from a database and build the
    module inventory in one call.

    Equivalent to::

        build_module_inventory(load_file_records_from_db(db_file), roots)
    """
    return build_module_inventory(load_file_records_from_db(db_file), roots)
