"""Phase 2C-3B-1: named imported-item static evidence analysis.

Analyzes the *imported names* of one ``from X import a, b as c`` record
against a frozen Phase 2C-1 module inventory plus optional caller-supplied
source text, yielding deterministic, candidate-scoped, immutable evidence.

Guarantees (contract of the approved Phase 2C-3B-1 scope; see
``docs/phase-2c-3b-1-approved-contract.md``):

* **Pure** — no filesystem, database, network or runtime-import access.
  Source text is only ever read from the caller-supplied
  ``target_sources`` mapping; the analyzer never re-reads a file to
  verify caller-supplied content.
* **Deterministic** — results are canonically ordered; candidate
  multiplicity is preserved and never converted into context
  multiplicity; evidence from different candidates is never merged into
  a single binding.
* **Conservative** — ``NAME_OK`` means static analysis completed against
  the supplied inventory/source and never asserts a confirmed runtime
  binding.
* **Frozen-compatibility** — the frozen absolute and relative resolvers
  are consumed verbatim; their statuses, reasons, issue strings and
  candidate partitions are preserved on the returned result.
* **F1** — structural child evidence requires an individually supported
  ``file_kind == "package_initializer"`` parent candidate and exact
  structural identity (canonical root + tuple equality on
  ``name_components + (name,)``).
* **F2** — candidate-scoped source availability is observable (via
  :class:`SourceAssessment`) even when evidence tuples are empty.
* **F3** — a bounded AST assignment pass (this module only) recognizes
  unambiguous module-scope ``Assign``/``AnnAssign`` bindings.
* **F4** — nested imports never suppress independently valid structural
  child evidence.
* **F5** — one absolute ``NOT_ATTEMPTED`` context is emitted regardless
  of the frozen reason; empty imported-name collections are rejected
  deterministically via :class:`ValueError`.
* **F6** — this module is the only new production file; the sibling
  test file is the only new test file.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass

from repo_intelligence.absolute_resolution import (
    NOT_ATTEMPTED,
    UNSUPPORTED,
    UNRESOLVED,
    _dedupe_matches,
    resolve_absolute_import,
)
from repo_intelligence.module_inventory import ModuleCandidate
from repo_intelligence.python_ast import ImportRecord
from repo_intelligence.relative_resolution import (
    REL_INVALID_KIND,
    REL_INVALID_LEVEL,
    REL_NOT_RELATIVE,
    RelativeResolution,
    resolve_relative_import,
)

__all__ = [
    # source states
    "SRC_SUPPLIED", "SRC_NOT_FOUND", "SRC_UNAVAILABLE", "SRC_UNPARSEABLE",
    # re-export scope codes
    "RS_MODULE_DIRECT", "RS_MODULE_CONDITIONAL", "RS_CLASS_BODY",
    "RS_FUNCTION_BODY", "RS_UNRESOLVED",
    # reasons
    "NAME_OK", "NAME_IMPORT_FORM", "NAME_TARGET_NO_CANDIDATE",
    "NAME_TARGET_UNSUPPORTED", "NAME_TARGET_NOT_ATTEMPTED",
    "NAME_INVALID_KIND", "NAME_OK_NO_EVIDENCE",
    # outcomes
    "OUTCOME_ANALYZED", "OUTCOME_NOT_ANALYZED", "OUTCOME_NOT_IN_SCOPE",
    # target kinds
    "TARGET_ABSOLUTE", "TARGET_RELATIVE", "TARGET_MIXED_ROUTING",
    "TARGET_INDETERMINATE",
    # result / evidence classes
    "TargetSource", "SourceAssessment", "NameEvidence",
    "DefinitionNameEvidence", "ReexportNameEvidence",
    "SubmoduleNameEvidence", "TargetNameAnalysis", "TargetContext",
    "ImportNameAnalysis",
    # public API
    "analyze_named_import",
]

# ---------------------------------------------------------------------------
# Source-state codes (F2)
# ---------------------------------------------------------------------------

SRC_SUPPLIED = "SRC_SUPPLIED"
SRC_NOT_FOUND = "SRC_NOT_FOUND"
SRC_UNAVAILABLE = "SRC_UNAVAILABLE"
SRC_UNPARSEABLE = "SRC_UNPARSEABLE"

# ---------------------------------------------------------------------------
# Re-export scope codes (F3, section 7 of the contract)
# ---------------------------------------------------------------------------

RS_MODULE_DIRECT = "RS_MODULE_DIRECT"
RS_MODULE_CONDITIONAL = "RS_MODULE_CONDITIONAL"
RS_CLASS_BODY = "RS_CLASS_BODY"
RS_FUNCTION_BODY = "RS_FUNCTION_BODY"
RS_UNRESOLVED = "RS_UNRESOLVED"

# ---------------------------------------------------------------------------
# Reasons and outcomes (contract section "Reasons")
# ---------------------------------------------------------------------------

NAME_OK = "NAME_OK"
NAME_IMPORT_FORM = "NAME_IMPORT_FORM"
NAME_TARGET_NO_CANDIDATE = "NAME_TARGET_NO_CANDIDATE"
NAME_TARGET_UNSUPPORTED = "NAME_TARGET_UNSUPPORTED"
NAME_TARGET_NOT_ATTEMPTED = "NAME_TARGET_NOT_ATTEMPTED"
NAME_INVALID_KIND = "NAME_INVALID_KIND"
NAME_OK_NO_EVIDENCE = "NAME_OK_NO_EVIDENCE"

OUTCOME_ANALYZED = "ANALYZED"
OUTCOME_NOT_ANALYZED = "NOT_ANALYZED"
OUTCOME_NOT_IN_SCOPE = "NOT_IN_SCOPE"

# ---------------------------------------------------------------------------
# Target kinds (ImportNameAnalysis.target_kind)
# ---------------------------------------------------------------------------

TARGET_ABSOLUTE = "absolute"
TARGET_RELATIVE = "relative"
TARGET_MIXED_ROUTING = "mixed_routing"
TARGET_INDETERMINATE = "indeterminate"

_MODULE_CONTAINER = (list, tuple, frozenset)


# ---------------------------------------------------------------------------
# Result / evidence dataclasses (immutable, exact contract hierarchy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceAssessment:
    """Immutable candidate-scoped source availability assessment (F2).

    Records source availability (and a parse diagnostic) for one target
    candidate even when no definition/re-export evidence exists, so an
    empty evidence tuple never silently implies the source was analyzed.

    Attributes:
        candidate: The :class:`ModuleCandidate` being assessed.
        state: One of :data:`SRC_SUPPLIED`, :data:`SRC_NOT_FOUND`,
            :data:`SRC_UNAVAILABLE`, :data:`SRC_UNPARSEABLE`.
        parse_diagnostic: ``None`` when no parse was attempted; a string
            (possibly empty) diagnostic for :data:`SRC_UNPARSEABLE`.
    """

    candidate: ModuleCandidate
    state: str
    parse_diagnostic: str | None = None


@dataclass(frozen=True)
class SubmoduleNameEvidence:
    """F1-eligible structural child-module evidence (F4).

    Attributes:
        candidate: The *individually supported package-initializer*
            parent candidate.
        submodule_candidate: The :class:`ModuleCandidate` whose
            ``name_components == candidate.name_components + (name,)``
            under the same canonical root (case-sensitive).
        component: The imported name (e.g. ``"sub"``).
        component_supported: ``submodule_candidate.component_supported``
            (support flags preserved; unsupported children are never
            presented as confirmed importable modules).
        name_supported: ``submodule_candidate.name_supported``.
        source: Always ``"structural"`` under this contract.
    """

    candidate: ModuleCandidate
    submodule_candidate: ModuleCandidate
    component: str
    component_supported: bool
    name_supported: bool
    source: str = "structural"


@dataclass(frozen=True)
class DefinitionNameEvidence:
    """A single module-scope name binding in one candidate's source (F3).

    Attributes:
        candidate: The :class:`ModuleCandidate` supplying the source.
        kind: Evidence kind — ``"module_attribute"`` for assignments /
            annotations, ``"module_class"`` / ``"module_function"`` /
            ``"module_async_function"`` for direct definitions.
        symbol_kind: ``"assignment"``, ``"annotated_assignment"``,
            ``"class"``, ``"function"``, ``"async_function"``.
        name: The syntactically bound name (matches the original
            imported name, not any alias).
        line: 1-based source line of the binding.
        provenance: :data:`RS_MODULE_DIRECT` (direct module-body
            evidence; the only scope that contributes under F3).
    """

    candidate: ModuleCandidate
    kind: str
    symbol_kind: str
    name: str
    line: int
    provenance: str = RS_MODULE_DIRECT


@dataclass(frozen=True)
class ReexportNameEvidence:
    """A single module-scope import binding in one candidate's source.

    Attributes:
        candidate: The :class:`ModuleCandidate` supplying the source.
        kind: Always ``"module_import"`` under this contract.
        symbol_kind: ``"import"`` or ``"from_import"``.
        name: The syntactically bound name (matching rules from the
            contract: ``import x`` binds ``x``; ``import x.y`` binds
            ``x``; ``from x import y`` binds ``y``; aliases bind the
            alias).
        asname: The source-side alias, preserved separately from the
            outer record alias; ``None`` when the source had no alias.
        line: 1-based source line of the import statement.
        provenance: :data:`RS_MODULE_DIRECT` or
            :data:`RS_MODULE_CONDITIONAL` (only these two scopes
            contribute as module re-export evidence).
    """

    candidate: ModuleCandidate
    kind: str
    symbol_kind: str
    name: str
    asname: str | None
    line: int
    provenance: str = RS_MODULE_DIRECT


@dataclass(frozen=True)
class NameEvidence:
    """One unit per ORIGINAL ``record.names`` occurrence.

    Candidate multiplicity is carried *inside* the three evidence
    tuples (which hold one entry per target candidate); this object is
    never split into per-candidate copies.

    Attributes:
        position: 0-based index into the original ``record.names``.
        name: Original imported name.
        asname: Original alias (``None`` when absent); matching is
            always done against ``name``.
        outcome: :data:`OUTCOME_ANALYZED`, ``OUTCOME_NOT_ANALYZED`` or
            :data:`OUTCOME_NOT_IN_SCOPE`.
        reason: One of the ``NAME_*`` reason codes.
        submodule: tuple of :class:`SubmoduleNameEvidence` (F1/F4).
        definitions: tuple of :class:`DefinitionNameEvidence` (F3).
        reexports: tuple of :class:`ReexportNameEvidence`.
    """

    position: int
    name: str
    asname: str | None
    outcome: str
    reason: str
    submodule: tuple = ()
    definitions: tuple = ()
    reexports: tuple = ()


@dataclass(frozen=True)
class TargetContext:
    """Resolver metadata for one resolution context.

    * Absolute routing: one context for the whole record.
    * Relative routing: one context per relative source-package
      interpretation (frozen relative resolver semantics preserved).

    Attributes:
        kind: :data:`TARGET_ABSOLUTE`, :data:`TARGET_RELATIVE` or
            :data:`TARGET_MIXED_ROUTING`.
        source_candidate: The relative source-package candidate (or
            ``None`` for the absolute context).
        base_module: Dotted requested module as-is; ``""`` when absent.
        base_components: Base component tuple for relative contexts;
            ``None`` for the absolute context.
        target_components: Matched target components or ``None`` when no
            target was derivable (optional where the frozen resolver
            permits ``None``).
        lossless: :data:`bool | None` (relative path only).
        status: A frozen resolver status from :data:`ALL_STATUSES`.
        reason: A frozen reason code (``REASON_*`` or ``REL_*``), or
            the original record-level failure reason.
        supported_candidates: Frozen supported partition (canonical
            order).
        unsupported_candidates: Frozen unsupported partition (canonical
            order).
        assessments: tuple of :class:`SourceAssessment`, one per target
            candidate — observable even when evidence tuples are empty
            (F2).
    """

    kind: str
    source_candidate: ModuleCandidate | None
    base_module: str | None
    base_components: tuple[str, ...] | None
    target_components: tuple[str, ...] | None
    lossless: bool | None
    status: str
    reason: str
    supported_candidates: tuple[ModuleCandidate, ...]
    unsupported_candidates: tuple[ModuleCandidate, ...]
    assessments: tuple[SourceAssessment, ...]


@dataclass(frozen=True)
class TargetNameAnalysis:
    """One unit per (resolver context, original name occurrence).

    Exactly one unit per resolver-context \u00d7 original
    ``record.names`` occurrence; candidate multiplicity remains inside
    :attr:`NameEvidence` tuples, never converted into new contexts.

    Attributes:
        context_index: Index of the owning context in the per-context
            context list (canonical order).
        name_index: Original index into ``record.names`` (duplicates and
            their indices preserved; aliases preserved).
        context: The owning :class:`TargetContext`.
        name: The :class:`NameEvidence` for this occurrence.
    """

    context_index: int
    name_index: int
    context: TargetContext
    name: NameEvidence


@dataclass(frozen=True)
class ImportNameAnalysis:
    """Top-level result of :func:`analyze_named_import`.

    Attributes:
        record: The original :class:`ImportRecord` — identity preserved
            (``result.record is record``).
        target_kind: :data:`TARGET_ABSOLUTE`, :data:`TARGET_RELATIVE` or
            :data:`TARGET_MIXED_ROUTING`.
        issue: Original failure issue when pre-resolution fails (e.g. a
            relative ``REL_*`` issue) or for record-level rejections;
            ``None`` on the normal analyzed path.
        contexts: A FLAT tuple of :class:`TargetNameAnalysis` units, one
            per (resolver context, original ``record.names`` occurrence)
            pair, ordered first by ``context_index`` then by
            ``name_index``.  One absolute context yields one unit per
            occurrence; each relative source-package interpretation is a
            separate context and yields one unit per occurrence.

    Cardinality: for ``C`` resolver contexts and ``N`` original name
    occurrences, ``len(contexts) == C * N`` and every entry is a
    :class:`TargetNameAnalysis` (never a nested tuple).
    """

    record: ImportRecord
    target_kind: str
    issue: str | None
    contexts: tuple[TargetNameAnalysis, ...]


@dataclass(frozen=True)
class TargetSource:
    """Immutable caller-supplied source text for one target candidate.

    The public input contract for :func:`analyze_named_import` —
    ``target_sources`` is a ``Mapping[ModuleCandidate, TargetSource]``
    where every value carries an explicit source state:

    * :data:`SRC_SUPPLIED` — ``source_text`` is a ``str``,
      ``parse_error`` is ``None``.
    * :data:`SRC_UNAVAILABLE` — both ``source_text`` and ``parse_error``
      are ``None``.
    * :data:`SRC_UNPARSEABLE` — ``source_text`` is a ``str`` (possibly
      empty) and ``parse_error`` is a ``str`` (possibly empty).

    A missing mapping key is synthesized as :data:`SRC_NOT_FOUND`; the
    caller cannot supply that state directly.  All three fields
    (``provenance`` state, ``source_text``, ``parse_error``) are
    required on every mapping value and are validated at the analyzer
    boundary before the record is routed or any target is matched.

    Attributes:
        provenance: REQUIRED source-state enum — one of
            :data:`SRC_SUPPLIED`, :data:`SRC_UNAVAILABLE`,
            :data:`SRC_UNPARSEABLE`.  :data:`SRC_NOT_FOUND` is not
            permitted here (it is synthesized from a missing key).
        source_text: ``str`` or ``None`` per the state table.
        parse_error: ``str`` or ``None`` per the state table.
    """

    provenance: str
    source_text: str | None
    parse_error: str | None

    def __post_init__(self) -> None:
        # The state table is authoritative: an invalid state OR an
        # invalid combination of state/text/parse_error cannot be
        # constructed, so downstream code can trust the fields.
        prov = self.provenance
        if prov not in (SRC_SUPPLIED, SRC_UNAVAILABLE, SRC_UNPARSEABLE):
            raise TypeError(
                "TargetSource.provenance must be SRC_SUPPLIED, "
                "SRC_UNAVAILABLE or SRC_UNPARSEABLE; "
                f"got {prov!r}"
            )
        st, er = self.source_text, self.parse_error
        if prov == SRC_SUPPLIED:
            if not isinstance(st, str):
                raise TypeError(
                    "TargetSource(state=SRC_SUPPLIED) requires a str source_text"
                )
            if er is not None:
                raise TypeError(
                    "TargetSource(state=SRC_SUPPLIED) must have parse_error=None"
                )
        elif prov == SRC_UNAVAILABLE:
            if st is not None or er is not None:
                raise TypeError(
                    "TargetSource(state=SRC_UNAVAILABLE) must have "
                    "source_text=None and parse_error=None"
                )
        else:  # SRC_UNPARSEABLE
            if not isinstance(st, str):
                raise TypeError(
                    "TargetSource(state=SRC_UNPARSEABLE) requires a str source_text"
                )
            if not isinstance(er, str):
                raise TypeError(
                    "TargetSource(state=SRC_UNPARSEABLE) requires a str "
                    "parse_error (possibly empty)"
                )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _canonical_candidate_order(cands: tuple[ModuleCandidate, ...]) -> tuple[ModuleCandidate, ...]:
    """Sort candidates in the same canonical order the frozen resolvers
    use (root.canonical, qualified_name, file_path, name_components).
    Never mutates the input tuple.
    """

    def _key(c: ModuleCandidate) -> tuple:
        return (
            c.root.canonical,
            c.qualified_name,
            c.file_path,
            c.name_components,
        )

    return tuple(sorted(cands, key=_key))


def _child_evidence(
    parent: ModuleCandidate,
    name: str,
    inventory: tuple[ModuleCandidate, ...],
) -> tuple[SubmoduleNameEvidence, ...]:
    """F1-eligible structural child evidence for a single parent
    candidate under the imported name *name*.

    Eligibility (F1):
    * ``parent.file_kind == "package_initializer"`` and
      ``parent.component_supported and parent.name_supported`` (an
      individual supported package initializer);
    * ordinary ``.py`` module parents never qualify automatically;
    * children are found by exact structural identity:
      ``child.name_components == parent.name_components + (name,)``
      with ``child.root.canonical == parent.root.canonical`` and
      case-sensitive matching;
    * a child without an indexed initializer is never fabricated.

    Every canonically ordered, structurally distinct matching child is
    preserved with its support flags.  Candidates that share one
    structural identity but disagree on substantive attributes follow
    the frozen resolvers' conflict behavior (``ValueError``); equivalent
    duplicates are consolidated to the canonical representative.
    """
    if parent.file_kind != "package_initializer":
        return ()
    if not (parent.component_supported and parent.name_supported):
        return ()
    expected = parent.name_components + (name,)
    matches = [
        c
        for c in inventory
        if c.name_components == expected
        and c.root.canonical == parent.root.canonical
        # C3-E3 contract: children must be ordinary_module or
        # package_initializer; synthetic namespace_directory
        # candidates are excluded from structural child evidence.
        and c.file_kind in ("ordinary_module", "package_initializer")
    ]
    if not matches:
        return ()
    # Reuse the frozen resolvers' duplicate consolidation/conflict
    # semantics rather than inventing a first-child winner.
    deduped = _dedupe_matches(matches)
    return tuple(
        SubmoduleNameEvidence(
            candidate=parent,
            submodule_candidate=child,
            component=name,
            component_supported=child.component_supported,
            name_supported=child.name_supported,
            source="structural",
        )
        for child in deduped
    )


def _inventory_child_index(
    inventory: tuple[ModuleCandidate, ...],
) -> dict[tuple, list[ModuleCandidate]]:
    """Build a lookup ``name_components -> list[candidates]`` for the
    inventory.  Used only for F1 child lookup.  Never mutates input.
    """
    bucket: dict[tuple, list[ModuleCandidate]] = {}
    for c in inventory:
        bucket.setdefault(tuple(c.name_components), []).append(c)
    return bucket


_VALID_SRC_STATES = (SRC_SUPPLIED, SRC_UNAVAILABLE, SRC_UNPARSEABLE)


def _target_source_state(ts: TargetSource) -> tuple[str, str | None]:
    """Validate a caller-supplied :class:`TargetSource` against the
    contract's state table.

    Branches EXPLICITLY on ``ts.provenance`` (the required state enum).
    Returns ``(state, parse_diagnostic)``.  Every malformed combination
    raises a deliberate :class:`TypeError`; callers never see a
    partially-interpreted state.
    """
    prov = ts.provenance
    if not isinstance(prov, str) or prov not in _VALID_SRC_STATES:
        raise TypeError(
            "TargetSource.provenance must be one of ["
            f"{', '.join(_VALID_SRC_STATES)}]; got {prov!r}"
        )
    st, er = ts.source_text, ts.parse_error
    if prov == SRC_SUPPLIED:
        if st is None:
            raise TypeError(
                "TargetSource(state=SRC_SUPPLIED) requires a str source_text"
            )
        if not isinstance(st, str):
            raise TypeError(
                "TargetSource(state=SRC_SUPPLIED).source_text must be str; "
                f"got {type(st).__name__}"
            )
        if er is not None:
            raise TypeError(
                "TargetSource(state=SRC_SUPPLIED) must have parse_error=None"
            )
        return SRC_SUPPLIED, None
    if prov == SRC_UNAVAILABLE:
        if st is not None:
            raise TypeError(
                "TargetSource(state=SRC_UNAVAILABLE) must have source_text=None"
            )
        if er is not None:
            raise TypeError(
                "TargetSource(state=SRC_UNAVAILABLE) must have parse_error=None"
            )
        return SRC_UNAVAILABLE, None
    # SRC_UNPARSEABLE
    if st is None:
        raise TypeError(
            "TargetSource(state=SRC_UNPARSEABLE) requires a str source_text"
            " (possibly empty)"
        )
    if not isinstance(st, str):
        raise TypeError(
            "TargetSource(state=SRC_UNPARSEABLE).source_text must be str; "
            f"got {type(st).__name__}"
        )
    if er is None or not isinstance(er, str):
        raise TypeError(
            "TargetSource(state=SRC_UNPARSEABLE) requires a str parse_error"
            " (possibly empty)"
        )
    return SRC_UNPARSEABLE, er


def _parse_source_state(
    candidate: ModuleCandidate,
    target_sources: Mapping[ModuleCandidate, TargetSource],
) -> tuple[str, str | None]:
    """Compute the (state, parse_diagnostic) pair for one candidate.

    * key absent in *target_sources* -> (SRC_NOT_FOUND, None)
    * value is a :class:`TargetSource` -> validated via
      :func:`_target_source_state`; a :data:`SRC_SUPPLIED` value is
      then attempted to parse, and an unexpected :class:`SyntaxError`
      becomes :data:`SRC_UNPARSEABLE` with an observable diagnostic.
    * any other value raises a deliberate :class:`TypeError`.
    """
    if candidate not in target_sources:
        return SRC_NOT_FOUND, None
    ts = target_sources[candidate]
    if not isinstance(ts, TargetSource):
        raise TypeError(
            "target_sources values must be TargetSource; "
            f"got {type(ts).__name__}"
        )
    state, diagnostic = _target_source_state(ts)
    if state == SRC_SUPPLIED:
        text = ts.source_text
        try:
            ast.parse(text)
        except SyntaxError as exc:
            return SRC_UNPARSEABLE, (str(exc) or "")
    return state, diagnostic


def _module_direct_defs(
    source: str,
    occurrences: tuple[tuple[str, str | None], ...],
    candidate: ModuleCandidate,
) -> dict[int, tuple[DefinitionNameEvidence, ...]]:
    """F3 bounded pass: unambiguous module-scope
    Assign / AnnAssign / ClassDef / FunctionDef / AsyncFunctionDef
    bindings matching each occurrence.  Nested (function-body or
    class-body) bindings are NOT module-scope and do not contribute
    (D3, D6 rule).  Only direct module-body statements are scanned.
    Returns a mapping ``occurrence_index -> tuple[evidence, ...]``.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict[int, list[DefinitionNameEvidence]] = {}
    for pos, (name, _alias) in enumerate(occurrences):
        out[pos] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and isinstance(tgt.id, str):
                    for pos, (name, _a) in enumerate(occurrences):
                        if name == tgt.id:
                            out[pos].append(
                                DefinitionNameEvidence(
                                    candidate=candidate,
                                    kind="module_attribute",
                                    symbol_kind="assignment",
                                    name=name,
                                    line=stmt.lineno,
                                    provenance=RS_MODULE_DIRECT,
                                )
                            )
        elif isinstance(stmt, ast.AnnAssign):
            # F3 bounded pass: an annotation-only ``sub: int`` does NOT
            # bind a value named ``sub``.  Require ``value is not None``
            # (``sub: int = 1`` is the positive case) before emitting
            # binding evidence.
            if (
                isinstance(stmt.target, ast.Name)
                and isinstance(stmt.target.id, str)
                and stmt.value is not None
            ):
                nm = stmt.target.id
                for pos, (name, _a) in enumerate(occurrences):
                    if name == nm:
                        out[pos].append(
                            DefinitionNameEvidence(
                                candidate=candidate,
                                kind="module_attribute",
                                symbol_kind="annotated_assignment",
                                name=name,
                                line=stmt.lineno,
                                provenance=RS_MODULE_DIRECT,
                            )
                        )
        elif isinstance(stmt, ast.ClassDef):
            for pos, (name, _a) in enumerate(occurrences):
                if name == stmt.name:
                    out[pos].append(
                        DefinitionNameEvidence(
                            candidate=candidate,
                            kind="module_class",
                            symbol_kind="class",
                            name=name,
                            line=stmt.lineno,
                            provenance=RS_MODULE_DIRECT,
                        )
                    )
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async_function" if isinstance(stmt, ast.AsyncFunctionDef) else "function"
            for pos, (name, _a) in enumerate(occurrences):
                if name == stmt.name:
                    out[pos].append(
                        DefinitionNameEvidence(
                            candidate=candidate,
                            kind=f"module_{kind}",
                            symbol_kind=kind,
                            name=name,
                            line=stmt.lineno,
                            provenance=RS_MODULE_DIRECT,
                        )
                    )
    return {k: tuple(v) for k, v in out.items()}


# Scope classification walk: returns (scope_code, line) for each
# import statement in the tree, keyed by node id.


def _import_scopes(
    source: str,
    occurrences: tuple[tuple[str, str | None], ...],
    candidate: ModuleCandidate,
) -> dict[int, tuple[ReexportNameEvidence, ...]]:
    """Classify every import statement in *source* by scope and record
    a :class:`ReexportNameEvidence` for each occurrence name bound at
    module scope (direct or conditional), per the contract's
    binding rules for aliases and dotted names.

    Scope codes:
    * Direct module body -> RS_MODULE_DIRECT  (contributes)
    * Any of the standard compound conditional (if / try / with / for /
      while / match) with the import as a descendant of the body ->
      RS_MODULE_CONDITIONAL  (contributes)
    * Class body -> RS_CLASS_BODY  (does not contribute)
    * Function / async-function body -> RS_FUNCTION_BODY  (does not)
    * Other (e.g. comprehension body) -> RS_UNRESOLVED (does not)
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict[int, list[ReexportNameEvidence]] = {i: [] for i in range(len(occurrences))}

    _CONDITIONALS = (
        ast.If,
        ast.While,
        ast.Try,
        ast.TryStar,
        ast.With,
        ast.AsyncWith,
        ast.For,
        ast.AsyncFor,
        ast.Match,
    )

    # Manual walk: depth-first with a stack of enclosing scope codes.
    def _record(node: ast.stmt, scope: str) -> None:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            return
        line = node.lineno
        for pos, (name, _a) in enumerate(occurrences):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    # Contract binding rules: ``import x`` binds x;
                    # ``import x.y`` binds x (top component only); an
                    # alias always binds the alias (``import other.sub
                    # as sub`` binds ``sub``, never ``other``).
                    bound = (
                        alias.asname
                        if alias.asname is not None
                        else alias.name.split(".", 1)[0]
                    )
                    if bound == name:
                        out[pos].append(
                            ReexportNameEvidence(
                                candidate=candidate,
                                kind="module_import",
                                symbol_kind="import",
                                name=name,
                                asname=alias.asname,
                                line=line,
                                provenance=scope,
                            )
                        )
            else:  # ImportFrom
                for alias in node.names:
                    # A wildcard import never establishes a syntactically
                    # named '*' binding — skip it (no wildcard inference).
                    if alias.name == "*":
                        continue
                    # from x import y binds y; from x import y as z binds z.
                    bound = alias.asname if alias.asname else alias.name
                    if bound == name:
                        out[pos].append(
                            ReexportNameEvidence(
                                candidate=candidate,
                                kind="module_import",
                                symbol_kind="from_import",
                                name=name,
                                asname=alias.asname,
                                line=line,
                                provenance=scope,
                            )
                        )

    def _walk(node: ast.AST, scope: str) -> None:
        if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef, ast.FunctionDef)):
            new_scope = (
                RS_FUNCTION_BODY
                if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
                else RS_CLASS_BODY
            )
            for child in ast.iter_child_nodes(node):
                # Skip docstring for functions (does not affect imports)
                _walk(child, new_scope)
            return
        if isinstance(node, _CONDITIONALS):
            # All direct children under a module-body conditional keep
            # the module scope: we walk children with the *current* scope
            # (RS_MODULE_DIRECT / RS_MODULE_CONDITIONAL inherited), and
            # the statement itself is module-scoped if the enclosing
            # scope was module-direct.  To reflect this, we mark every
            # statement inside a top-level conditional as
            # RS_MODULE_CONDITIONAL and let nested function/class/
            # conditional bodies further narrow the scope down.
            child_scope = (
                scope if scope in (RS_MODULE_DIRECT, RS_MODULE_CONDITIONAL)
                else scope
            )
            if scope == RS_MODULE_DIRECT or scope == RS_MODULE_CONDITIONAL:
                child_scope = RS_MODULE_CONDITIONAL
            for child in ast.iter_child_nodes(node):
                _walk(child, child_scope)
            return
        if isinstance(node, ast.stmt):
            _record(node, scope)
        for child in ast.iter_child_nodes(node):
            _walk(child, scope)

    for stmt in tree.body:
        _walk(stmt, RS_MODULE_DIRECT)
    return {k: tuple(v) for k, v in out.items()}


def _validate_record(record: object) -> ImportRecord:
    """Raise on structurally invalid records; return the record on
    success.

    F5 requires that kind failures (including non-string kinds, unknown
    kinds, relative invalid kinds, relative invalid levels) be routed
    through the frozen resolvers, not pre-empted at this boundary.  We
    therefore only check that ``record`` IS an :class:`ImportRecord`
    and that ``record.names`` is a non-empty tuple of (str, str|None)
    pairs — the named-import multiplicity invariant — rather than
    re-implementing the frozen resolvers' kind rules.
    """
    if not isinstance(record, ImportRecord):
        raise TypeError(
            f"record must be an ImportRecord; got {type(record).__name__}"
        )
    # F5 multiplicity invariant: names must be a non-empty tuple of
    # (str, str|None) pairs.  (The frozen resolvers are permissive of
    # non-tuple record.names and would otherwise produce a
    # non-deterministic NameEvidence for the caller's mutable lists.)
    if not isinstance(record.names, tuple):
        raise TypeError(
            "record.names must be a tuple of (name, asname) pairs; "
            f"got {type(record.names).__name__}"
        )
    if not record.names:
        raise ValueError(
            "record.names must not be empty: the named-import analyzer "
            "requires at least one imported name or alias to analyze"
        )
    for entry in record.names:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ValueError(
                "each record.names entry must be a (name, asname) pair; "
                f"got {type(entry).__name__}"
            )
        nm, al = entry
        if not isinstance(nm, str):
            raise ValueError(
                "record.names name must be str; "
                f"got {type(nm).__name__}"
            )
        if al is not None and not isinstance(al, str):
            raise ValueError(
                "record.names asname must be str or None; "
                f"got {type(al).__name__}"
            )
    return record


# ---------------------------------------------------------------------------
# Public API (approx. 180 lines)
# ---------------------------------------------------------------------------


def _routing_signal(record: ImportRecord) -> str:
    """Decide which routing path (absolute / relative / mixed) applies.

    A frozen absolute resolver is consulted UNLESS the record is a
    valid relative record (``from_import`` with level >= 1 and a
    valid int level).  Any invalid-kind / invalid-level / negative
    record is still routed through the ABSOLUTE resolver, so that the
    frozen ``UNSUPPORTED/REASON_INVALID_LEVEL`` and
    ``NOT_ATTEMPTED/REASON_INVALID_RECORD_KIND`` envelopes are
    preserved verbatim (frozen-absolute-compatibility).

    Rules:
    * ``kind == "from_import"`` and ``level`` is a non-bool int and
      ``level >= 1`` → relative routing.
    * ``kind == "import"`` and ``level`` is a non-bool int and
      ``level >= 1`` → mixed routing (an absolute-routing signal that
      will be evaluated against the relative resolver).
    * All other cases (including non-string kinds, invalid kinds,
      negative levels, bool levels, kind="import" level=0,
      kind="from_import" level=0) → absolute routing; the frozen
      absolute resolver classifies the record (F5 / D15 / frozen
      invalid envelopes).
    """
    level = record.level
    level_ok = (
        isinstance(level, int)
        and not isinstance(level, bool)
        and level >= 1
    )
    if level_ok:
        if record.kind == "from_import":
            return TARGET_RELATIVE
        if record.kind == "import":
            return TARGET_MIXED_ROUTING
    return TARGET_ABSOLUTE


def _assessments_for(
    candidates: tuple[ModuleCandidate, ...],
    target_sources: Mapping,
) -> tuple[SourceAssessment, ...]:
    """F2: one :class:`SourceAssessment` per candidate, so that source
    availability remains observable even when no evidence was found.
    """
    return tuple(
        SourceAssessment(
            candidate=c,
            state=st,
            parse_diagnostic=diagnostic,
        )
        for c, (st, diagnostic) in (
            (c, _parse_source_state(c, target_sources))
            for c in candidates
        )
    )


def _evidence_for_occurrence(
    name: str,
    parent_candidates: tuple[ModuleCandidate, ...],
    inventory: tuple[ModuleCandidate, ...],
    target_sources: Mapping,
) -> tuple[SubmoduleNameEvidence, ..., tuple[DefinitionNameEvidence, ...], tuple[ReexportNameEvidence, ...]]:
    """Per-occurrence candidate-scoped evidence.

    For each target candidate (individually):
    * structural child evidence (F1/F4) — one entry per structural
      child, ordered canonically;
    * definitions / re-exports — only when the candidate's source was
      actually supplied AND parseable, one entry per syntactic binding
      that matches the imported name.

    Returns ``(submodules, definitions, reexports)`` — each a tuple
    whose entries are per-candidate (candidate multiplicity is
    preserved inside the tuple, never promoted to new contexts).
    """
    sub: list[SubmoduleNameEvidence] = []
    defs: list[DefinitionNameEvidence] = []
    reex: list[ReexportNameEvidence] = []
    for parent in parent_candidates:
        child_ev = _child_evidence(parent, name, inventory)
        sub.extend(child_ev)
        state, _diag = _parse_source_state(parent, target_sources)
        if state != SRC_SUPPLIED:
            continue
        text = target_sources[parent].source_text
        occurrences = ((name, None),)
        defs.extend(_module_direct_defs(text, occurrences, parent).get(0, ()))
        scope_ev = _import_scopes(text, occurrences, parent).get(0, ())
        # Only module-direct and module-conditional scopes contribute
        # as re-export evidence (other scopes are excluded by the
        # contract).
        reex.extend(
            e for e in scope_ev
            if e.provenance in (RS_MODULE_DIRECT, RS_MODULE_CONDITIONAL)
        )
    return tuple(sub), tuple(defs), tuple(reex)


def _per_candidate_contexts_for_absolute(
    record: ImportRecord,
    resolution,
    inventory: tuple[ModuleCandidate, ...],
    target_sources: Mapping,
    target_kind: str,
) -> tuple[TargetContext, NameEvidence, tuple[TargetNameAnalysis, ...]]:
    """Build one absolute-resolver context plus its per-occurrence
    evidence and the corresponding :class:`TargetNameAnalysis` list.

    Returns:
    * one :class:`TargetContext` (resolver metadata);
    * a list of :class:`NameEvidence` (one per original occurrence) —
      in the order of ``record.names``;
    * a list of :class:`TargetNameAnalysis` (one per occurrence) with
      all ``context_index == 0`` and ``name_index == i``.
    """
    names_in_order = list(record.names)
    assessments = _assessments_for(
        resolution.supported_candidates, target_sources
    )
    base_module = record.module if isinstance(record.module, str) else ""
    target_components = resolution.requested_components
    status = resolution.status
    reason = resolution.reason
    # Outcome/reason mapping per the D-matrix (F5 consistent):
    # D15: a kind="import" record keeps its ACTUAL frozen resolver
    # fields/status/reason/partitions (the absolute resolver classifies
    # it); only the per-occurrence NAME layer is overridden to
    # NOT_IN_SCOPE / NAME_IMPORT_FORM with empty evidence.
    d15_import_form = (record.kind == "import")
    if status in ("RESOLVED", "AMBIGUOUS"):
        outcome, name_reason = OUTCOME_ANALYZED, NAME_OK
    elif status == UNSUPPORTED:
        outcome, name_reason = OUTCOME_NOT_ANALYZED, NAME_TARGET_UNSUPPORTED
    elif status == UNRESOLVED:
        outcome, name_reason = OUTCOME_NOT_ANALYZED, NAME_TARGET_NO_CANDIDATE
    elif status == NOT_ATTEMPTED:
        if "invalid_record_kind" in reason or "invalid_kind" in reason:
            outcome, name_reason = OUTCOME_NOT_IN_SCOPE, NAME_INVALID_KIND
        elif "relative_no_package_context" in reason:
            outcome, name_reason = OUTCOME_NOT_ANALYZED, NAME_TARGET_NOT_ATTEMPTED
        else:
            outcome, name_reason = OUTCOME_NOT_ANALYZED, NAME_TARGET_NOT_ATTEMPTED
    else:
        outcome, name_reason = OUTCOME_NOT_ANALYZED, NAME_TARGET_NOT_ATTEMPTED

    name_evidence: list[NameEvidence] = []
    units: list[TargetNameAnalysis] = []
    context = TargetContext(
        kind=target_kind,
        source_candidate=None,
        base_module=base_module,
        base_components=None,
        target_components=target_components,
        lossless=None,
        status=status,
        reason=reason,
        supported_candidates=resolution.supported_candidates,
        unsupported_candidates=resolution.unsupported_candidates,
        assessments=assessments,
    )
    for i, (name, asname) in enumerate(names_in_order):
        # D15 name layer: kind="import" records are out of scope for
        # named-item analysis regardless of the frozen resolver status.
        if d15_import_form:
            outcome = OUTCOME_NOT_IN_SCOPE
            name_reason = NAME_IMPORT_FORM
        if outcome == OUTCOME_NOT_ANALYZED or outcome == OUTCOME_NOT_IN_SCOPE:
            sub_t, def_t, reex_t = (), (), ()
        else:
            sub_t, def_t, reex_t = _evidence_for_occurrence(
                name,
                resolution.supported_candidates,
                inventory,
                target_sources,
            )
        ne = NameEvidence(
            position=i,
            name=name,
            asname=asname,
            outcome=outcome,
            reason=name_reason,
            submodule=sub_t,
            definitions=def_t,
            reexports=reex_t,
        )
        name_evidence.append(ne)
        units.append(
            TargetNameAnalysis(
                context_index=0, name_index=i, context=context, name=ne
            )
        )
    return context, name_evidence, units


def _per_candidate_contexts_for_relative(
    record: ImportRecord,
    rel: RelativeResolution,
    inventory: tuple[ModuleCandidate, ...],
    target_sources: Mapping,
    target_kind: str,
) -> tuple[TargetContext, list[NameEvidence], tuple[TargetNameAnalysis, ...], tuple]:
    """Build one relative-context envelope per frozen
    :class:`ContextResolution`, each with its per-occurrence evidence
    and the corresponding :class:`TargetNameAnalysis` list.

    Returns one :class:`TargetContext` and one list of
    :class:`TargetNameAnalysis` per frozen context.
    """
    names_in_order = list(record.names)
    base_module = record.module if isinstance(record.module, str) else ""
    contexts_out: list[TargetContext] = []
    units_out: tuple[TargetNameAnalysis, ...] = ()

    for ctx_idx, ctx in enumerate(rel.contexts):
        assessments = _assessments_for(
            ctx.supported_candidates, target_sources
        )
        status = ctx.status
        reason = ctx.reason
        if status in ("RESOLVED", "AMBIGUOUS"):
            outcome, name_reason = OUTCOME_ANALYZED, NAME_OK
        elif status == UNSUPPORTED:
            outcome, name_reason = OUTCOME_NOT_ANALYZED, NAME_TARGET_UNSUPPORTED
        elif status == UNRESOLVED:
            outcome, name_reason = OUTCOME_NOT_ANALYZED, NAME_TARGET_NO_CANDIDATE
        elif status == NOT_ATTEMPTED:
            if "invalid_kind" in reason:
                outcome, name_reason = OUTCOME_NOT_IN_SCOPE, NAME_INVALID_KIND
            elif "relative_no_package_context" in reason:
                outcome, name_reason = OUTCOME_NOT_ANALYZED, NAME_TARGET_NOT_ATTEMPTED
            elif "relative_invalid_kind" in reason:
                outcome, name_reason = OUTCOME_NOT_IN_SCOPE, NAME_INVALID_KIND
            else:
                outcome, name_reason = OUTCOME_NOT_ANALYZED, NAME_TARGET_NOT_ATTEMPTED
        else:
            outcome, name_reason = OUTCOME_NOT_ANALYZED, NAME_TARGET_NOT_ATTEMPTED
        tc = TargetContext(
            kind=target_kind,
            source_candidate=ctx.source_candidate,
            # C2: relative contexts carry base_module=None; the frozen
            # base_components/target_components hold the structure.
            base_module=None,
            base_components=ctx.base_components,
            target_components=ctx.target_components,
            lossless=ctx.lossless,
            status=status,
            reason=reason,
            supported_candidates=ctx.supported_candidates,
            unsupported_candidates=ctx.unsupported_candidates,
            assessments=assessments,
        )
        units = []
        for i, (name, asname) in enumerate(names_in_order):
            if outcome in (OUTCOME_NOT_ANALYZED, OUTCOME_NOT_IN_SCOPE):
                sub_t, def_t, reex_t = (), (), ()
            else:
                sub_t, def_t, reex_t = _evidence_for_occurrence(
                    name,
                    ctx.supported_candidates,
                    inventory,
                    target_sources,
                )
            ne = NameEvidence(
                position=i, name=name, asname=asname,
                outcome=outcome, reason=name_reason,
                submodule=sub_t, definitions=def_t, reexports=reex_t,
            )
            units.append(
                TargetNameAnalysis(
                    context_index=ctx_idx, name_index=i, context=tc, name=ne
                )
            )
        contexts_out.append(tc)
        units_out = (*units_out, *units)
    return contexts_out[0] if contexts_out else None, \
        [u.name for u in units_out], \
        units_out, \
        tuple(contexts_out)


def _target_contexts_per_resolution(
    record: ImportRecord,
    resolution,
    inventory: tuple[ModuleCandidate, ...],
    target_sources: Mapping,
    target_kind: str,
):
    """Uniform entry-point for either absolute or relative routing."""
    if target_kind == TARGET_ABSOLUTE:
        ctx, _ne, units = _per_candidate_contexts_for_absolute(
            record, resolution, inventory, target_sources, target_kind
        )
        return [ctx], units
    # relative / mixed
    _unused, _ne_l, units, contexts = _per_candidate_contexts_for_relative(
        record, resolution, inventory, target_sources, target_kind
    )
    return list(contexts), units


def analyze_named_import(
    record: object,
    inventory: object,
    target_sources: object = None,
) -> ImportNameAnalysis:
    """Analyze the *imported names* of one ``ImportRecord``.

    Args:
        record: A Phase 2B :class:`ImportRecord` — must carry non-empty
            ``names`` and a string ``kind``.  The original object's
            identity is preserved on the returned result
            (``result.record is record``).
        inventory: A Phase 2C-1 inventory — any sequence of
            :class:`ModuleCandidate`.  The order is treated as caller
            input; the result is canonically ordered.
        target_sources: A ``Mapping[ModuleCandidate, TargetSource]`` —
            one entry per candidate whose source text the caller wants
            the analyzer to inspect.  Missing key synthesizes
            :data:`SRC_NOT_FOUND`.  Each :class:`TargetSource` encodes
            one of :data:`SRC_SUPPLIED` / :data:`SRC_UNAVAILABLE` /
            :data:`SRC_UNPARSEABLE` per the state table; the analyzer
            never re-reads the filesystem to verify caller content.

    Returns:
        A frozen :class:`ImportNameAnalysis`.  The ``contexts`` tuple is
        flat and ordered by ``(context_index, name_index)``; its length
        is ``C * N`` where ``C`` is the number of resolver contexts
        (absolute: 1, relative: one per source-package interpretation)
        and ``N`` is the number of original ``record.names`` entries.
        Every entry is a :class:`TargetNameAnalysis` (never a nested
        tuple).

    Raises:
        TypeError: If ``record`` is not an :class:`ImportRecord`, or if
            ``inventory`` is not an iterable of
            :class:`ModuleCandidate`, or if any key of
            ``target_sources`` is not a :class:`ModuleCandidate`, or if
            any value of ``target_sources`` is not a :class:`TargetSource`
            (or its fields violate the state table).
        ValueError: If ``record.names`` contains a non-tuple entry, or a
            tuple with a non-string name/alias (defensive against
            mutable callers), or if the frozen resolver raises
            :class:`ValueError` for conflicting duplicate candidates.
    """
    rec = _validate_record(record)
    # Inventory validation: accept any sequence, verify each element.
    inv_seq = tuple(inventory) if hasattr(inventory, "__iter__") else \
        (inventory,)
    # Coerce to tuple of ModuleCandidate; raise deliberately.
    # (Framing: "a key not identifying an inventory candidate" is
    # deliberately a TypeError.)
    for c in inv_seq:
        if not isinstance(c, ModuleCandidate):
            raise TypeError(
                "inventory must contain only ModuleCandidate objects; "
                f"got {type(c).__name__}"
            )
    # C: target_sources is REQUIRED and must be a Mapping.  Explicit
    # None is malformed (not silently converted to {}).
    if target_sources is None:
        raise TypeError(
            "target_sources is required and must be a "
            "Mapping[ModuleCandidate, TargetSource] (pass {} when no "
            "candidate has caller-supplied source)"
        )
    if not isinstance(target_sources, Mapping):
        raise TypeError(
            "target_sources must be a Mapping[ModuleCandidate, TargetSource]; "
            f"got {type(target_sources).__name__}"
        )
    for k in target_sources.keys():
        if not isinstance(k, ModuleCandidate):
            raise TypeError(
                "target_sources keys must be ModuleCandidate objects "
                f"(identifying an inventory candidate); got {type(k).__name__}"
            )
    # B: validate EVERY mapping value against the state table up front,
    # even for keys that may not match any target.
    for v in target_sources.values():
        if not isinstance(v, TargetSource):
            raise TypeError(
                "target_sources values must be TargetSource; "
                f"got {type(v).__name__}"
            )
        _target_source_state(v)
    # C: strict membership — a key must be the SAME candidate object
    # (or value-equal to one) in the inventory.  Frozen dataclass
    # equality covers substantive flags, so a candidate differing only
    # on a support flag is NOT the same candidate and is rejected.
    inv_by_identity = {id(c) for c in inv_seq}
    for k in target_sources.keys():
        if id(k) not in inv_by_identity and k not in inv_seq:
            raise TypeError(
                "target_sources keys must identify a ModuleCandidate "
                "present in the inventory"
            )

    target_kind = _routing_signal(rec)

    # --- D (Codex): positive-level invalid-kind records are relative ------
    # ``kind`` outside {"import", "from_import"} with level >= 1 must
    # preserve the frozen REL_INVALID_KIND pre-context issue and
    # contexts=(); a positive-level ``kind="import"`` is a malformed
    # ordinary import (frozen absolute UNSUPPORTED/invalid_relative_
    # import_level) and is NOT the relative routing signal — so it is
    # routed through the (non-mixed) relative resolver, which records
    # ``relative_invalid_kind`` at step 1.
    positive_level = (
        isinstance(rec.level, int)
        and not isinstance(rec.level, bool)
        and rec.level >= 1
    )
    invalid_kind = rec.kind not in ("import", "from_import")
    if positive_level and invalid_kind:
        resr = resolve_relative_import(rec, inv_seq)
        issue = resr.issue if resr.issue is not None else "relative_invalid_kind"
        return ImportNameAnalysis(
            record=rec, target_kind=TARGET_RELATIVE, issue=issue, contexts=()
        )

    # --- Routing dispatch --------------------------------------------------
    if target_kind == TARGET_ABSOLUTE or \
            (rec.kind == "import" and positive_level):
        # Absolute resolver classifies: valid absolute records, empty
        # targets (F5), and malformed import-kind levels (level 1 / -1 /
        # True / '1' -> UNSUPPORTED/invalid_relative_import_level).
        resolution = resolve_absolute_import(rec, inv_seq)
        contexts, units = _target_contexts_per_resolution(
            rec, resolution, inv_seq, target_sources, TARGET_ABSOLUTE
        )
    else:
        resolution = resolve_relative_import(rec, inv_seq)
        # Relative pre-context failures (issue is not None) preserve
        # the original issue and contexts=() — no per-occurrence
        # envelope is fabricated (D17 / F5 envelope).
        if resolution.issue is not None:
            return ImportNameAnalysis(
                record=rec,
                target_kind=target_kind,
                issue=resolution.issue,
                contexts=(),
            )
        contexts, units = _target_contexts_per_resolution(
            rec, resolution, inv_seq, target_sources, target_kind
        )

    return ImportNameAnalysis(
        record=rec,
        target_kind=target_kind,
        issue=None,
        contexts=tuple(units),
    )


