"""Phase 2C-2A: pure absolute import resolver.

Resolves a single :class:`repo_intelligence.python_ast.ImportRecord`
(Phase 2B, frozen) against a frozen Phase 2C-1 module inventory
(``tuple[ModuleCandidate, ...]`` as produced by
:func:`repo_intelligence.module_inventory.build_module_inventory`).

Guarantees:
    * **Pure** — read-only over both inputs; never executes ``import``,
      never reads or writes the filesystem or any database.
    * **Deterministic** — repeated calls with equal inputs return an
      equal :class:`Resolution`; result candidate sequences are always
      canonically ordered and duplicate-equivalent matches are
      consolidated; ordering never selects a "winning" candidate.
    * **Conservative** — only exact, component-based structural matches
      are considered; no namespace-package inference, no candidate
      fabrication, no reinterpretation of malformed records.
"""

from __future__ import annotations

from dataclasses import dataclass

from repo_intelligence.module_inventory import ModuleCandidate
from repo_intelligence.python_ast import ImportRecord

# ---------------------------------------------------------------------------
# Status taxonomy (stable, machine-readable)
# ---------------------------------------------------------------------------

RESOLVED = "RESOLVED"
AMBIGUOUS = "AMBIGUOUS"
UNRESOLVED = "UNRESOLVED"
UNSUPPORTED = "UNSUPPORTED"
NOT_ATTEMPTED = "NOT_ATTEMPTED"

ALL_STATUSES = (RESOLVED, AMBIGUOUS, UNRESOLVED, UNSUPPORTED, NOT_ATTEMPTED)

# ---------------------------------------------------------------------------
# Reason codes (stable, machine-readable)
# ---------------------------------------------------------------------------

REASON_UNIQUE_SUPPORTED = "single_supported_candidate"
REASON_MULTI_SUPPORTED = "multiple_distinct_supported_candidates"
REASON_NO_CANDIDATE = "no_structural_candidate_match"
REASON_UNSUPPORTED_NAME = "matched_candidates_name_unsupported"
REASON_TARGET_NOT_INTERPRETABLE = "requested_module_uninterpretable"
REASON_EMPTY_TARGET = "empty_requested_module"
REASON_RELATIVE_IMPORT = "relative_import_reserved_for_phase_2c_3"
REASON_INVALID_RECORD_KIND = "invalid_record_kind"
REASON_INVALID_LEVEL = "invalid_relative_import_level"


def _split_target(target: object) -> tuple[str, ...]:
    """Split a dotted target string into its components.

    Returns ``()`` when the target is not a str, is empty, or contains
    an empty component (``"a..b"``, ``"."``, ``"a."``, ``".a"``).
    Literal dots are structural separators only when they fall on
    component boundaries; a candidate derived from a literal-dot
    directory name (e.g. ``x/a.b.py`` -> ``("a.b",)``) can never equal
    a two-component target ``("a", "b")`` and so is not conflated.
    """
    if not isinstance(target, str) or target == "":
        return ()
    parts = target.split(".")
    if any(part == "" for part in parts):
        return ()
    return tuple(parts)


def _canonical_order_key(c: ModuleCandidate) -> tuple:
    """Frozen canonical ordering key.

    Exactly
        ``(root.canonical, qualified_name, file_path, name_components)``
    — independent of caller input ordering and of the diagnostic
    ``PackageRoot.original`` spelling.  Used so that both supported and
    unsupported result sequences are deterministically ordered.
    """
    return (
        c.root.canonical,
        c.qualified_name,
        c.file_path,
        c.name_components,
    )


def _structural_identity(c: ModuleCandidate) -> tuple:
    """Structural candidate identity, consistent with the frozen
    inventory contract: canonical root, actual name components, indexed
    file path.  The diagnostic ``PackageRoot.original`` spelling is
    deliberately NOT part of the identity.
    """
    return (c.root.canonical, c.name_components, c.file_path)


def _substantive_attrs(c: ModuleCandidate) -> tuple:
    """Substantive attributes that must agree among candidates sharing
    one structural identity.  ``root.original`` (diagnostic only) is
    intentionally excluded so that equivalent candidates differing only
    in diagnostic spelling are not rejected.
    """
    return (
        c.qualified_name,
        c.file_kind,
        c.directory_kind,
        c.component_supported,
        c.name_supported,
        c.evidence,
    )


def _dedupe_matches(matches: list[ModuleCandidate]) -> tuple[ModuleCandidate, ...]:
    """Deterministically consolidate duplicate matching candidates.

    Candidates sharing a structural identity are grouped and, if they
    agree on all substantive attributes, consolidated to the
    deterministic canonical representative (the lexicographically
    smallest diagnostic ``root.original`` spelling).  If they disagree
    on any substantive attribute the consolidation is ambiguous and the
    caller must not silently discard conflicting evidence, so a
    ``ValueError`` is raised instead of choosing one arbitrarily.

    Output is canonically ordered (see :func:`_canonical_order_key`).
    Never mutates *matches*.
    """
    ordered = sorted(matches, key=_canonical_order_key)
    groups: dict[tuple, list[ModuleCandidate]] = {}
    for cand in ordered:
        groups.setdefault(_structural_identity(cand), []).append(cand)

    deduped: list[ModuleCandidate] = []
    for _key, group in groups.items():
        if len(group) > 1:
            substantive = {_substantive_attrs(c) for c in group}
            if len(substantive) > 1:
                raise ValueError(
                    "Conflicting duplicate module candidates share the "
                    "structural identity but differ on substantive "
                    "attributes; refusing to silently discard candidate "
                    "evidence."
                )
            # Equivalent duplicates: consolidate to the deterministic
            # canonical representative (smallest diagnostic original).
            deduped.append(min(group, key=lambda c: c.root.original))
        else:
            deduped.append(group[0])

    return tuple(sorted(deduped, key=_canonical_order_key))


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """Immutable resolution outcome for one :class:`ImportRecord`.

    Attributes:
        record: The original, unmodified :class:`ImportRecord`.  The
            requested names, aliases, and source positions are carried
            by it; no redundant reconstructed copies are kept here.
        requested_module: The dotted module string the resolver was
            asked to target (``module`` as-is; ``""`` when absent).
        requested_components: The component tuple the match was
            performed on; ``None`` when no match was possible
            (relative import, empty, or uninterpretable target).
        status: One of :data:`ALL_STATUSES`.
        supported_candidates: Matching candidates supported for import
            (``component_supported and name_supported``), in canonical
            order — independent of caller input ordering.
        unsupported_candidates: Matching candidates that are NOT
            supported, retained even when supported candidates also
            exist, in the same canonical order.
        reason: A stable :data:`REASON_*` code explaining the status.
    """

    record: ImportRecord
    requested_module: str
    requested_components: tuple[str, ...] | None
    status: str
    supported_candidates: tuple[ModuleCandidate, ...]
    unsupported_candidates: tuple[ModuleCandidate, ...]
    reason: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _outcome(
    record: ImportRecord,
    requested_module: str,
    requested_components: tuple[str, ...] | None,
    status: str,
    supported: tuple[ModuleCandidate, ...],
    unsupported: tuple[ModuleCandidate, ...],
    reason: str,
) -> Resolution:
    return Resolution(
        record=record,
        requested_module=requested_module,
        requested_components=requested_components,
        status=status,
        supported_candidates=supported,
        unsupported_candidates=unsupported,
        reason=reason,
    )


def resolve_absolute_import(record: object, inventory: object) -> Resolution:
    """Pure, deterministic resolution of one import record.

    Args:
        record: An :class:`ImportRecord` (Phase 2B contract).  Synthetic
            or malformed records are validated and classified with an
            explicit reason code; they are never silently reinterpreted.
        inventory: A Phase 2C-1 inventory — any sequence of
            :class:`ModuleCandidate`, typically the frozen
            ``build_module_inventory`` tuple.  Candidate order is
            preserved in the outcome; ordering never selects a winner.

    Returns:
        A frozen :class:`Resolution`.

    Raises:
        TypeError: If *record* is not an :class:`ImportRecord`, or if
            *inventory* is not a sequence of :class:`ModuleCandidate`.
    """
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

    kind = record.kind

    # --- 1. Structural record validation --------------------------------
    if not isinstance(kind, str) or kind not in ("import", "from_import"):
        return _outcome(
            record,
            record.module if isinstance(record.module, str) else "",
            None,
            NOT_ATTEMPTED, (), (),
            REASON_INVALID_RECORD_KIND,
        )

    # --- 2. Scope boundary: relative imports are Phase 2C-3 -------------
    level = record.level
    level_ok = isinstance(level, int) and not isinstance(level, bool) and level >= 0
    if kind == "import":
        # Ordinary ``import`` statements are absolute by language; a
        # non-zero (or non-int) level is a malformed synthetic record.
        if not level_ok or level > 0:
            return _outcome(
                record,
                record.module if isinstance(record.module, str) else "",
                None, UNSUPPORTED, (), (), REASON_INVALID_LEVEL,
            )
    elif not level_ok:
        return _outcome(
            record,
            record.module if isinstance(record.module, str) else "",
            None, UNSUPPORTED, (), (), REASON_INVALID_LEVEL,
        )
    elif level > 0:
        return _outcome(
            record,
            record.module if isinstance(record.module, str) else "",
            None, NOT_ATTEMPTED, (), (), REASON_RELATIVE_IMPORT,
        )

    # --- 3. Target extraction ------------------------------------------
    target = record.module if isinstance(record.module, str) else ""
    if target == "":
        return _outcome(
            record, "", None, NOT_ATTEMPTED, (), (), REASON_EMPTY_TARGET
        )

    components = _split_target(target)
    if components == ():
        # Non-empty but uninterpretable as a dotted target
        # (e.g. "a..b", non-string already handled).
        return _outcome(
            record, target, None, UNSUPPORTED, (), (),
            REASON_TARGET_NOT_INTERPRETABLE,
        )

    # --- 4. Matching: exact component-tuple identity --------------------
    # First collect *all* structurally matching candidates (both flags
    # together), dedupe/consolidate them — which raises
    # ``ValueError`` on substantive conflicts per F2 — and only then
    # partition by the supported flag (F4).
    all_matches = [
        c for c in candidates if c.name_components == components
    ]
    deduped = _dedupe_matches(all_matches)

    supported = tuple(
        c for c in deduped
        if c.component_supported and c.name_supported
    )
    unsupported = tuple(
        c for c in deduped
        if not (c.component_supported and c.name_supported)
    )

    if supported:
        if len(supported) == 1:
            return _outcome(
                record, target, components, RESOLVED,
                supported, unsupported, REASON_UNIQUE_SUPPORTED,
            )
        return _outcome(
            record, target, components, AMBIGUOUS,
            supported, unsupported, REASON_MULTI_SUPPORTED,
        )
    if unsupported:
        return _outcome(
            record, target, components, UNSUPPORTED,
            (), unsupported, REASON_UNSUPPORTED_NAME,
        )

    # --- 5. No structural match ----------------------------------------
    # The target structure itself may still be a legal import name
    # (e.g. "os.path" against a stdlib-less inventory); report the
    # distinct reason so callers can tell "legal but absent" apart from
    # "structurally unmatchable" (already handled above).
    return _outcome(
        record, target, components, UNRESOLVED, (), (), REASON_NO_CANDIDATE,
    )
