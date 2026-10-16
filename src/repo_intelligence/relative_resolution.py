"""Phase 2C-3A: pure relative-import module-target resolver.

Resolves the explicit module target of a Python relative import against
a frozen Phase 2C-1 module inventory, yielding one independent
ContextResolution per distinct source-package interpretation.

Guarantees:
* Pure - read-only over both inputs; never executes import, never reads
  or writes the filesystem or any database, never inspects runtime
  import paths.
* Deterministic - contexts are canonically ordered and independent of
  inventory, root, or duplicate insertion order; equivalent
  source-context duplicates are consolidated to a deterministic
  canonical representative.
* Component-identity preserving - the lossless structural-identity
  guard is applied before delegation to the frozen absolute resolver;
  a target whose components cannot be re-split from its dotted spelling
  is reported UNSUPPORTED / REL_LOSSLESS_UNSAFE and never matched.
* Delegating - for safely encodable targets the frozen
  resolve_absolute_import is invoked; the original relative ImportRecord
  is preserved by identity on the returned RelativeResolution.
* No aggregate status - each ContextResolution is self-contained; a
  pre-context failure yields an empty contexts tuple plus the
  applicable top-level issue.

Analyzing individual imported names (as attributes, variables, classes,
or submodules) is intentionally out of scope for Phase 2C-3A and
reserved for Phase 2C-3B.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from repo_intelligence.absolute_resolution import (
    AMBIGUOUS,
    NOT_ATTEMPTED,
    RESOLVED,
    UNSUPPORTED,
    UNRESOLVED,
    _canonical_order_key,
    _split_target,
    _structural_identity,
    _substantive_attrs,
    resolve_absolute_import,
)
from repo_intelligence.module_inventory import (
    ModuleCandidate,
    _split_rel_path,
)
from repo_intelligence.python_ast import ImportRecord

# ---------------------------------------------------------------------------
# New relative-import reason codes (Phase 2C-3A, R2 spec section 10)
# ---------------------------------------------------------------------------

REL_INVALID_KIND = "relative_invalid_kind"
REL_INVALID_LEVEL = "relative_invalid_level"
REL_NOT_RELATIVE = "relative_not_relative"
REL_MALFORMED_SUFFIX = "relative_malformed_suffix"
REL_INVALID_SOURCE_PATH = "relative_invalid_source_path"
REL_MISSING_SOURCE_CONTEXT = "relative_missing_source_context"
REL_NO_PACKAGE_CONTEXT = "relative_no_package_context"
REL_TRAVERSAL_BEYOND_TOP = "relative_traversal_beyond_top_level"
REL_LOSSLESS_UNSAFE = "relative_lossless_unsafe"

_ALL_REASONS: tuple[str, ...] = (
    REL_INVALID_KIND,
    REL_INVALID_LEVEL,
    REL_NOT_RELATIVE,
    REL_MALFORMED_SUFFIX,
    REL_INVALID_SOURCE_PATH,
    REL_MISSING_SOURCE_CONTEXT,
    REL_NO_PACKAGE_CONTEXT,
    REL_TRAVERSAL_BEYOND_TOP,
    REL_LOSSLESS_UNSAFE,
)

__all__ = list(_ALL_REASONS) + [
    "ContextResolution",
    "RelativeResolution",
    "resolve_relative_import",
]


# ---------------------------------------------------------------------------
# Result models (Phase 2C-3A, R2 spec section 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextResolution:
    """Immutable resolution outcome for one source-package context.

    Attributes:
        source_candidate: The complete original source
            :class:`ModuleCandidate` (root, file kind, directory kind,
            name components, support flags and evidence); equivalent
            duplicates are consolidated to the deterministic canonical
            representative, and conflicting substantive evidence raises
            :class:`ValueError`.
        base_components: Derived package-base component tuple
            (``name_components[:-1]`` for ordinary modules,
            ``name_components`` for package initializers); empty for
            root-level ordinary modules.
        target_components: Explicit module-target component tuple
            actually matched
            (``base_components[:n-(L-1)] + suffix_components``), or
            ``None`` when no target was derivable (pre-context failure,
            no package context, or traversal beyond top level).  Preserved
            verbatim even when the encoding is structurally unsafe
            (``REL_LOSSLESS_UNSAFE``).
        lossless: ``True`` when the target's components round-trip
            losslessly from dotted spelling (and the frozen absolute
            resolver was consulted); ``False`` when the encoding is
            unsafe (no delegation, no matching); ``None`` when no target
            was derivable.
        status: One of :data:`repo_intelligence.absolute_resolution.ALL_STATUSES`.
        reason: A frozen ``REASON_*`` code when the frozen absolute
            resolver produced the outcome, or a new ``REL_*`` code for
            relative-import specific failures.
        supported_candidates: Frozen resolver's supported-candidate
            partition (canonical order) or ``()``.
        unsupported_candidates: Frozen resolver's
            unsupported-candidate partition (canonical order) or ``()``.
    """

    source_candidate: ModuleCandidate
    base_components: tuple[str, ...]
    target_components: tuple[str, ...] | None
    lossless: bool | None
    status: str
    reason: str
    supported_candidates: tuple[ModuleCandidate, ...]
    unsupported_candidates: tuple[ModuleCandidate, ...]


@dataclass(frozen=True)
class RelativeResolution:
    """Immutable outcome of resolving a single relative ``ImportRecord``.

    Invariants:
        * ``result.record is record`` - the original record is never
          modified or reconstructed.
        * ``contexts`` is canonically ordered and independent of
          inventory, root, or duplicate insertion order;
          source-context multiplicity is preserved.
        * ``issue`` is populated only when processing terminates
          before any context can be established; if ``contexts`` is
          non-empty then ``issue is None``.
        * There is no aggregate resolution status.
    """

    record: ImportRecord
    contexts: tuple[ContextResolution, ...]
    issue: str | None


# ---------------------------------------------------------------------------
# Source-context deduplication (Phase 2C-3A, R2 spec section 11)
# ---------------------------------------------------------------------------


def _dedupe_source_contexts(
    candidates: tuple[ModuleCandidate, ...],
) -> tuple[ModuleCandidate, ...]:
    """Deterministically consolidate equivalent source-context duplicates.

    Candidates sharing a structural identity (see the frozen
    :func:`repo_intelligence.absolute_resolution._structural_identity`)
    are grouped.  Within a group, if candidates disagree on any
    substantive attribute (see
    :func:`repo_intelligence.absolute_resolution._substantive_attrs`) a
    :class:`ValueError` is raised rather than selecting an arbitrary
    winner; otherwise they are consolidated to the deterministic
    canonical representative (the lexicographically smallest diagnostic
    :attr:`repo_intelligence.PackageRoot.original` spelling).

    Output is canonically ordered (see the frozen
    :func:`repo_intelligence.absolute_resolution._canonical_order_key`).
    Never mutates *candidates*.
    """
    ordered = sorted(candidates, key=_canonical_order_key)
    groups: dict[tuple, list[ModuleCandidate]] = {}
    for cand in ordered:
        groups.setdefault(_structural_identity(cand), []).append(cand)

    deduped: list[ModuleCandidate] = []
    for _key, group in groups.items():
        if len(group) > 1:
            substantive = {_substantive_attrs(c) for c in group}
            if len(substantive) > 1:
                raise ValueError(
                    "Conflicting duplicate source-context candidates "
                    "share the structural identity but differ on "
                    "substantive attributes; refusing to silently "
                    "discard candidate evidence."
                )
            deduped.append(min(group, key=lambda c: c.root.original))
        else:
            deduped.append(group[0])

    return tuple(sorted(deduped, key=_canonical_order_key))


# ---------------------------------------------------------------------------
# Public API (Phase 2C-3A, R2 spec section 4)
# ---------------------------------------------------------------------------


def resolve_relative_import(
    record: ImportRecord,
    inventory: Iterable[ModuleCandidate],
) -> RelativeResolution:
    """Pure, deterministic resolution of one relative ``ImportRecord``.

    Args:
        record: An :class:`ImportRecord` (Phase 2B contract).  Synthetic
            or malformed records are classified with an explicit
            ``REL_*`` reason code (carried on ``issue``); they are never
            silently reinterpreted.
        inventory: A finite iterable of
            :class:`ModuleCandidate`, typically the frozen
            ``build_module_inventory`` tuple.  Materialized and
            validated once, before processing.

    Returns:
        A frozen :class:`RelativeResolution`.

    Raises:
        TypeError: When *record* is not an :class:`ImportRecord`, or
            when *inventory* is not an iterable of
            :class:`ModuleCandidate`.
        ValueError: When source-context candidates sharing a structural
            identity disagree on substantive attributes (conflicting
            duplicate evidence), or when a conflict among matching-target
            candidates is propagated from the frozen absolute resolver.

    Invariants:
        * ``result.record is record`` - the original record is never
          modified; the derived absolute record used for delegation is
          internal-only and never exposed.
        * No aggregate status; a per-context result tuple.
        * If ``contexts`` is non-empty, ``issue is None``.
    """

    # --- 1. Outer type checks (R2 spec section 5, step 1) ----------------
    if not isinstance(record, ImportRecord):
        raise TypeError(
            f"record must be an ImportRecord; got {type(record).__name__}"
        )
    try:
        candidates: tuple[ModuleCandidate, ...] = tuple(inventory)
    except TypeError:
        raise TypeError(
            f"inventory must be a sequence of ModuleCandidate; "
            f"got {type(inventory).__name__}"
        ) from None
    for cand in candidates:
        if not isinstance(cand, ModuleCandidate):
            raise TypeError(
                "inventory must contain only ModuleCandidate objects; "
                f"got {type(cand).__name__}"
            )

    # --- 2. Kind validation (R2 spec section 5, step 2) -------------------
    kind = record.kind
    if not isinstance(kind, str) or kind not in ("import", "from_import"):
        return RelativeResolution(
            record=record, contexts=(), issue=REL_INVALID_KIND
        )

    # --- 3. Level validation (R2 spec section 5, step 3) ------------------
    # Negative, boolean, or non-integer level -> REL_INVALID_LEVEL.
    # An ordinary `import` with a positive level is likewise malformed.
    level = record.level
    level_ok = (
        isinstance(level, int)
        and not isinstance(level, bool)
        and level >= 0
    )
    if (not level_ok) or (kind == "import" and level > 0):
        return RelativeResolution(
            record=record, contexts=(), issue=REL_INVALID_LEVEL
        )

    # --- 4. Scope check (R2 spec section 5, step 4) ------------------------
    # A valid level-zero record is absolute, not relative.
    if level == 0:
        return RelativeResolution(
            record=record, contexts=(), issue=REL_NOT_RELATIVE
        )

    # --- 5. Suffix validation (R2 spec section 5, step 5) -----------------
    # Non-string module -> REL_MALFORMED_SUFFIX.
    # Non-empty but not a valid dotted target -> REL_MALFORMED_SUFFIX.
    # Empty string (e.g. `from . import x`) is a valid, empty suffix.
    raw_suffix = record.module
    if not isinstance(raw_suffix, str):
        return RelativeResolution(
            record=record, contexts=(), issue=REL_MALFORMED_SUFFIX
        )
    suffix_components = _split_target(raw_suffix)
    if raw_suffix != "" and suffix_components == ():
        return RelativeResolution(
            record=record, contexts=(), issue=REL_MALFORMED_SUFFIX
        )

    # --- 6. Source path validation (R2 spec section 5, step 6) ------------
    # Must conform to frozen `_split_rel_path` canonical rules.
    source_path = record.source_path
    if not isinstance(source_path, str):
        return RelativeResolution(
            record=record, contexts=(), issue=REL_INVALID_SOURCE_PATH
        )
    try:
        _split_rel_path(source_path)
    except ValueError:
        return RelativeResolution(
            record=record, contexts=(), issue=REL_INVALID_SOURCE_PATH
        )

    # --- 7. Source-context lookup (R2 spec section 5, step 7) -------------
    # Collect all candidates whose indexed file matches source_path.
    # Multiple explicit roots may produce different candidates for the
    # same physical file - each distinct interpretation is preserved.
    raw_source_contexts = tuple(
        c for c in candidates if c.file_path == source_path
    )
    if not raw_source_contexts:
        return RelativeResolution(
            record=record, contexts=(), issue=REL_MISSING_SOURCE_CONTEXT
        )

    # Consolidate equivalent duplicates; raise ValueError on conflicting
    # substantive attributes (mirrors frozen resolver F2 behavior).
    try:
        source_contexts = _dedupe_source_contexts(raw_source_contexts)
    except ValueError:
        raise

    # --- 8. Per-context processing (R2 spec section 5, step 8) ------------
    # Derive base, apply traversal BEFORE suffix concatenation, validate
    # losslessness, delegate when safe.
    contexts: list[ContextResolution] = []

    for src in source_contexts:
        # Package-context derivation (R2 spec section 6).
        if src.file_kind == "package_initializer":
            base = src.name_components
        else:
            base = src.name_components[:-1]

        n = len(base)

        # Empty base: root-level ordinary module has no package context.
        if n == 0:
            contexts.append(
                ContextResolution(
                    source_candidate=src,
                    base_components=(),
                    target_components=None,
                    lossless=None,
                    status=NOT_ATTEMPTED,
                    reason=REL_NO_PACKAGE_CONTEXT,
                    supported_candidates=(),
                    unsupported_candidates=(),
                )
            )
            continue

        # Traversal beyond top level.
        if level > n:
            contexts.append(
                ContextResolution(
                    source_candidate=src,
                    base_components=base,
                    target_components=None,
                    lossless=None,
                    status=UNSUPPORTED,
                    reason=REL_TRAVERSAL_BEYOND_TOP,
                    supported_candidates=(),
                    unsupported_candidates=(),
                )
            )
            continue

        # Traversal: remove the last (level - 1) base components.
        kept = base[: n - (level - 1)]
        target_components = kept + suffix_components

        # Lossless structural-identity guard (R2 spec section 8).
        # Every component must be non-empty, and the dotted spelling
        # must round-trip to exactly the same component tuple.
        dotted = ".".join(target_components)
        if _split_target(dotted) != target_components:
            contexts.append(
                ContextResolution(
                    source_candidate=src,
                    base_components=base,
                    target_components=target_components,
                    lossless=False,
                    status=UNSUPPORTED,
                    reason=REL_LOSSLESS_UNSAFE,
                    supported_candidates=(),
                    unsupported_candidates=(),
                )
            )
            continue

        # Delegate to the frozen absolute resolver (R2 spec section 9).
        derived = ImportRecord(
            kind="from_import",
            module=dotted,
            names=record.names,
            level=0,
            source_path=record.source_path,
            start_line=record.start_line,
            start_col=record.start_col,
        )
        resolution = resolve_absolute_import(derived, candidates)

        contexts.append(
            ContextResolution(
                source_candidate=src,
                base_components=base,
                target_components=target_components,
                lossless=True,
                status=resolution.status,
                reason=resolution.reason,
                supported_candidates=resolution.supported_candidates,
                unsupported_candidates=resolution.unsupported_candidates,
            )
        )

    return RelativeResolution(
        record=record,
        contexts=tuple(contexts),
        issue=None,
    )
