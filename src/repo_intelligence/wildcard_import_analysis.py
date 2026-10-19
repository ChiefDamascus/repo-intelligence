"""Phase 2C-3B-2: wildcard ``from X import *`` static evidence analysis.

Analyzes one star-import :class:`ImportRecord` against the frozen Phase
2C-1 module inventory plus caller-supplied source text, producing
candidate-scoped evidence about which names the star ``__all__`` rule
*declares*, under the approved architecture
(``docs/phase-2c-3b-2-architecture-approved.md``):

* **Pure** — no filesystem, database, network or runtime-import access.
  Source text is only ever read from the caller-supplied
  ``target_sources`` mapping; parsing is the analyzer's duty.
* **Deterministic** — resolver context order and resolver candidate
  order are preserved as-is; ``source_assessments`` are canonical; the
  output is permutation-invariant under inventory and ``target_sources``
  insertion order for the same record identity and valid evidence.
* **Conservative** — static evidence is distinguished from runtime
  bindings at every field; ``WR_ANALYZED`` means static analysis
  completed against supplied evidence and never asserts a confirmed
  runtime binding.
* **Frozen-compatibility** — the frozen absolute and relative resolvers
  are consumed verbatim (public surface only); their statuses, reasons,
  partitions, provenance and ``ValueError`` on conflicting duplicate
  candidates are preserved on the returned result.
* **R-1…R-8** — single approved decisions: non-star →
  ``WR_NOT_IN_SCOPE`` return, malformed star → ``ValueError``; parse
  failure → ``ALL_UNKNOWN``, no salvage; conflicting duplicates
  propagate the resolver's ``ValueError``; declaration ⇄ binding is
  never cross-checked; ``import a.b.c`` binds ``a`` only; no salvage;
  module-executing star imports invalidate → ``ALL_UNKNOWN``; extra
  ``target_sources`` keys not in the inventory → ``ValueError``.

``TargetContext`` / ``SourceAssessment`` / ``TargetSource`` are reused
verbatim from 2C-3B-1.  All new types are ``@dataclass(frozen=True)``.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass

from repo_intelligence.absolute_resolution import (
    AMBIGUOUS,
    RESOLVED,
    resolve_absolute_import,
)
from repo_intelligence.imported_name_analysis import (
    SRC_NOT_FOUND,
    SRC_SUPPLIED,
    SRC_UNAVAILABLE,
    SRC_UNPARSEABLE,
    SourceAssessment,
    TARGET_ABSOLUTE,
    TARGET_INDETERMINATE,
    TARGET_RELATIVE,
    TargetContext,
    TargetSource,
)
from repo_intelligence.module_inventory import (
    ModuleCandidate,
    PackageRoot,
)
from repo_intelligence.python_ast import ImportRecord
from repo_intelligence.relative_resolution import (
    resolve_relative_import,
)

__all__ = [
    # source states (inherited verbatim from 2C-3B-1, re-exported for
    # the star API's own state table)
    "SRC_SUPPLIED", "SRC_NOT_FOUND", "SRC_UNAVAILABLE", "SRC_UNPARSEABLE",
    # __all__ states (new)
    "ALL_ABSENT", "ALL_DECLARED", "ALL_UNKNOWN",
    # default_scope codes (new)
    "DEFAULT_DIRECT_ONLY", "DEFAULT_INCOMPLETE",
    # outcomes (new WR_* namespace, consistent with 2C-3B-1 NAME_*)
    "WR_ANALYZED", "WR_NOT_ANALYZED", "WR_NOT_IN_SCOPE",
    # reasons (new)
    "WR_TARGET_NO_CANDIDATE", "WR_TARGET_UNSUPPORTED",
    "WR_ALL_ABSENT_DEFAULT_APPLIED", "WR_ALL_DECLARED_APPLIED",
    "WR_ALL_UNKNOWN_NO_EVIDENCE", "WR_SOURCE_UNPARSEABLE_NO_EVIDENCE",
    "WR_SOURCE_NOT_FOUND_NO_EVIDENCE", "WR_SOURCE_UNAVAILABLE_NO_EVIDENCE",
    "WR_INVALID_RECORD",
    # target kinds (inherited from 2C-3B-1, re-exported)
    "TARGET_ABSOLUTE", "TARGET_RELATIVE", "TARGET_INDETERMINATE",
    # result / evidence classes (new)
    "WildcardCandidateEvidence", "WildcardContextAnalysis",
    "WildcardAnalysis",
    # public API
    "analyze_wildcard_import",
]


# ---------------------------------------------------------------------------
# __all__ states (new)
# ---------------------------------------------------------------------------

ALL_ABSENT = "ALL_ABSENT"
ALL_DECLARED = "ALL_DECLARED"
ALL_UNKNOWN = "ALL_UNKNOWN"

# ---------------------------------------------------------------------------
# default_scope codes (new; None otherwise)
# ---------------------------------------------------------------------------

DEFAULT_DIRECT_ONLY = "DIRECT_ONLY"
DEFAULT_INCOMPLETE = "INCOMPLETE"

# ---------------------------------------------------------------------------
# Outcomes (new WR_* namespace)
# ---------------------------------------------------------------------------

WR_ANALYZED = "WR_ANALYZED"
WR_NOT_ANALYZED = "WR_NOT_ANALYZED"
WR_NOT_IN_SCOPE = "WR_NOT_IN_SCOPE"

# ---------------------------------------------------------------------------
# Reasons (new)
# ---------------------------------------------------------------------------

WR_TARGET_NO_CANDIDATE = "WR_TARGET_NO_CANDIDATE"
WR_TARGET_UNSUPPORTED = "WR_TARGET_UNSUPPORTED"
WR_ALL_ABSENT_DEFAULT_APPLIED = "WR_ALL_ABSENT_DEFAULT_APPLIED"
WR_ALL_DECLARED_APPLIED = "WR_ALL_DECLARED_APPLIED"
WR_ALL_UNKNOWN_NO_EVIDENCE = "WR_ALL_UNKNOWN_NO_EVIDENCE"
WR_SOURCE_UNPARSEABLE_NO_EVIDENCE = "WR_SOURCE_UNPARSEABLE_NO_EVIDENCE"
WR_SOURCE_NOT_FOUND_NO_EVIDENCE = "WR_SOURCE_NOT_FOUND_NO_EVIDENCE"
WR_SOURCE_UNAVAILABLE_NO_EVIDENCE = "WR_SOURCE_UNAVAILABLE_NO_EVIDENCE"
WR_INVALID_RECORD = "WR_INVALID_RECORD"


# ---------------------------------------------------------------------------
# Result / evidence dataclasses (immutable, exact contract hierarchy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WildcardCandidateEvidence:
    """One unit per target candidate, independent of outcome state.

    * ``source_state`` records the caller-derived source state (one of the
      ``SRC_*`` inherited from 2C-3B-1), observable even when the
      candidate was not analyzed (W15/W16 analogue).
    * ``all_state`` records the §6 closed-grammar classification
      (``ALL_ABSENT`` / ``ALL_DECLARED`` / ``ALL_UNKNOWN``).
    * ``all_entries`` is the declared literal occurrences exactly as
      declared (order + duplicates preserved); it is ``()`` for
      ``ALL_ABSENT`` and ``ALL_UNKNOWN``.  It is a declaration, not a
      binding (R-4).
    * ``star_names`` is the syntactic-name evidence emitted under this
      API: for ``ALL_DECLARED`` it is the declared occurrences in
      declared order; for ``ALL_ABSENT`` it is the §8 star-default
      concrete names, deduplicated and sorted; for ``ALL_UNKNOWN`` (and
      for W15/W16) it is ``()`` — no salvage, no best-effort fallback.
    * ``default_scope`` is ``DEFAULT_DIRECT_ONLY`` (the default rule ran
      on a syntactically closed module), ``DEFAULT_INCOMPLETE``
      (a syntactically visible module-executing construct is present
      that the analyzer does not prove safe, so the default is not a
      complete view of runtime exports), or ``None`` (not applicable —
      declared, unknown state, or source not available).
    * ``outcome``/``reason`` are the per-candidate codes per the §7
      outcome contract.
    """

    candidate: ModuleCandidate
    source_state: str
    all_state: str
    all_entries: tuple[str, ...]
    star_names: tuple[str, ...]
    default_scope: str | None
    outcome: str
    reason: str


@dataclass(frozen=True)
class WildcardContextAnalysis:
    """One unit per resolver context.

    ``context_index`` is 0-based within the frozen resolver's own context
    sequence (absolute → always 0; relative → one per preserved
    ``ContextResolution`` interpretation).  ``context`` is the *frozen*
    :class:`TargetContext` — verbatim from the 2C-3B-1 inheritance
    (statuses, reasons, issue strings and candidate partitions are
    never re-invented).  ``candidates`` carries the per-candidate
    evidence, ordered first by the frozen resolver's supported partition
    (resolver order preserved), then by its unsupported partition
    (resolver order preserved).  A supported candidate's row is always
    positioned before any unsupported candidate's row for the same
    context — the two partitions are never interleaved or merged
    (canonical ordering invariant from §10).
    """

    context_index: int
    context: TargetContext
    candidates: tuple[WildcardCandidateEvidence, ...]


@dataclass(frozen=True)
class WildcardAnalysis:
    """Top-level result of :func:`analyze_wildcard_import`.

    Cardinality and outcome invariants (see §5/§7/§9 of the approved
    architecture for the full contract):

    * ``WR_NOT_IN_SCOPE`` (well-typed non-star record) →
      ``contexts == ()``, ``source_assessments == ()``,
      ``reason == WR_INVALID_RECORD``, ``issue == None``,
      ``target_kind == TARGET_INDETERMINATE``.
    * Relative pre-context failure (e.g. a frozen
      ``REL_MISSING_SOURCE_CONTEXT`` issue) → ``contexts == ()``,
      ``source_assessments == ()``, ``issue`` preserved verbatim,
      ``outcome == WR_NOT_ANALYZED``,
      ``reason == WR_TARGET_NO_CANDIDATE``.
    * Otherwise → one ``WildcardContextAnalysis`` per frozen resolver
      context (absolute: one context at ``context_index==0``; relative:
      one per frozen ``ContextResolution`` interpretation, indexed in
      frozen order), and a flat ``source_assessments`` tuple holding one
      ``SourceAssessment`` per **unique** inventory candidate, sorted by
      the public candidate key ``(root.canonical, qualified_name,
      file_path, name_components)`` after frozen structural
      deduplication.

    * ``WR_NOT_ANALYZED`` at the root means: (a) the target has no
      supported candidate, or (b) every supported candidate's
      ``__all__`` state / source state is an ``ALL_UNKNOWN`` /
      unsupported state (no evidence).  ``reason`` is the *root* code
      for that case.
    * ``WR_ANALYZED`` at the root means: at least one candidate carried
      an ``ALL_ABSENT`` or ``ALL_DECLARED`` state with syntactically
      valid evidence (``WR_ALL_ABSENT_DEFAULT_APPLIED`` or
      ``WR_ALL_DECLARED_APPLIED`` per candidate).
    """

    record: ImportRecord
    target_kind: str
    outcome: str
    reason: str | None
    issue: str | None
    contexts: tuple[WildcardContextAnalysis, ...]
    source_assessments: tuple[SourceAssessment, ...]


# ---------------------------------------------------------------------------
# Internal helpers — static, pure, no I/O anywhere
# ---------------------------------------------------------------------------


def _canonical_key(c: ModuleCandidate):
    """The canonical public candidate key used to sort
    ``source_assessments`` (§10: input order is not output order)."""
    return (
        c.root.canonical,
        c.qualified_name,
        c.file_path,
        c.name_components,
    )


def _not_in_scope_result(record: ImportRecord) -> "WildcardAnalysis":
    """The R-1 well-typed non-star record envelope (returned, not
    raised): root ``WR_NOT_IN_SCOPE`` / ``WR_INVALID_RECORD`` /
    ``contexts == ()`` / ``TARGET_INDETERMINATE`` (§11)."""
    return WildcardAnalysis(
        record=record,
        target_kind=TARGET_INDETERMINATE,
        outcome=WR_NOT_IN_SCOPE,
        reason=WR_INVALID_RECORD,
        issue=None,
        contexts=(),
        source_assessments=(),
    )


# ---------------------------------------------------------------------------
# TargetSource state-table validation (mirrors the 2C-3B-1 public
# contract; §4/W17 — validated before any routing decision).  A
# hand-built (``object.__new__``) value bypasses ``__post_init__`` and
# must still be rejected here.  This never imports 3B-1 private helpers.
# ---------------------------------------------------------------------------


def _validate_target_source(value: TargetSource) -> None:
    """Validate a :class:`TargetSource` against the inherited state table.

    The state table is authoritative (2C-3B-1 ``TargetSource``
    contract):

    * ``SRC_SUPPLIED`` — ``source_text`` is a ``str`` and
      ``parse_error`` is ``None``;
    * ``SRC_UNAVAILABLE`` — both ``source_text`` and ``parse_error``
      are ``None``;
    * ``SRC_UNPARSEABLE`` — ``source_text`` is a ``str`` and
      ``parse_error`` is a ``str`` (possibly empty).

    ``SRC_NOT_FOUND`` is synthesized only from a missing mapping key and
    is never a valid value here.  Raises ``TypeError`` on any violation.
    """
    if not isinstance(value, TargetSource):
        raise TypeError(
            "target_sources values must be TargetSource; "
            f"got {type(value).__name__}"
        )
    prov = value.provenance
    st = value.source_text
    er = value.parse_error
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
    elif prov == SRC_UNPARSEABLE:
        if not isinstance(st, str):
            raise TypeError(
                "TargetSource(state=SRC_UNPARSEABLE) requires a str source_text"
            )
        if not isinstance(er, str):
            raise TypeError(
                "TargetSource(state=SRC_UNPARSEABLE) requires a str "
                "parse_error (possibly empty)"
            )
    else:  # SRC_NOT_FOUND or any other value: not a valid mapped state
        raise TypeError(
            "TargetSource.provenance must be SRC_SUPPLIED, "
            "SRC_UNAVAILABLE or SRC_UNPARSEABLE; "
            f"got {prov!r}"
        )


def _star_shape(record: ImportRecord) -> str:
    """Classify the record into ``'star'``, ``'non_star'`` or
    raise ``ValueError`` for a malformed star (§11 / R-1).

    A valid star is exactly one name entry ``("*", None)`` under
    ``kind == "from_import"``.  A ``"import"``-kind star is
    malformed (R-1).  Any star carrying an ``asname``, any star mixed
    with other names, multiple stars, or a non-tuple ``names`` shape is
    malformed → ``ValueError``.  A record with well-typed names and
    no star at all is a well-typed non-star record → ``'non_star'``
    (returned to the caller, not raised).
    """
    names = record.names
    if not isinstance(names, tuple) or not names or not all(
        isinstance(entry, tuple) and len(entry) == 2
        and isinstance(entry[0], str)
        and (entry[1] is None or isinstance(entry[1], str))
        for entry in names
    ):
        # A malformed names shape under a star claim is a contract
        # error (defensive; R-1 "malformed star").
        if any(
            isinstance(entry, tuple) and len(entry) == 2 and entry[0] == "*"
            for entry in names
            if isinstance(names, tuple)
        ) or record.kind not in ("import", "from_import"):
            raise ValueError(
                "malformed star import record: expected exactly one "
                '("*", None) name entry under kind="from_import"'
            )
        return "non_star"
    star_entries = [e for e in names if e[0] == "*"]
    if not star_entries:
        return "non_star"
    if record.kind != "from_import":
        raise ValueError(
            'a star import must come under kind="from_import"; '
            f'got kind={record.kind!r} (R-1 malformed star)'
        )
    if len(star_entries) != 1 or len(names) != 1:
        raise ValueError(
            "a star import record must contain exactly one name "
            'entry equal to ("*", None); '
            f"got names={names!r} (mixed/multi star is malformed, R-1)"
        )
    if star_entries[0][1] is not None:
        raise ValueError(
            "a star import name must have asname=None; "
            f"got asname={star_entries[0][1]!r} (R-1 malformed star)"
        )
    return "star"


# ---------------------------------------------------------------------------
# Source-state resolution for one candidate (parsing is the analyzer's
# duty — R-2; no salvage on failure)
# ---------------------------------------------------------------------------


def _candidate_source(
    candidate: ModuleCandidate,
    target_sources: Mapping[ModuleCandidate, TargetSource],
) -> tuple[str, str | None, str | None]:
    """Return ``(source_state, parse_diagnostic, source_text)`` for one
    candidate.  A missing key synthesizes ``SRC_NOT_FOUND`` (§4).  Values
    are already validated against the 2C-3B-1 state table at the
    analyzer boundary before routing, so a valid state is guaranteed
    here; a ``SRC_SUPPLIED`` value that fails ``ast.parse`` is
    reclassified ``SRC_UNPARSEABLE`` (R-2 — parse is the analyzer's
    obligation; the caller need not pre-parse).
    """
    if candidate not in target_sources:
        return SRC_NOT_FOUND, None, None
    ts = target_sources[candidate]
    if ts.provenance == SRC_SUPPLIED:
        try:
            ast.parse(ts.source_text)
        except SyntaxError as exc:
            return SRC_UNPARSEABLE, (str(exc) or ""), ts.source_text
        return SRC_SUPPLIED, None, ts.source_text
    if ts.provenance == SRC_UNPARSEABLE:
        return SRC_UNPARSEABLE, ts.parse_error, ts.source_text
    return SRC_UNAVAILABLE, None, None


# ---------------------------------------------------------------------------
# §6 closed `__all__` grammar
# ---------------------------------------------------------------------------


def _classify_all(tree) -> tuple[str, tuple[str, ...]]:
    """Classify a parsed module's ``__all__`` under the §6 closed grammar.

    Returns ``(all_state, entries)``:

    * Exactly one direct module-body ``__all__`` (``Assign`` with a
      single ``Name('__all__')`` target or ``AnnAssign`` target and a
      non-``None`` value) whose value is a list/tuple literal of string
      constants, with no other direct ``__all__`` writes in the body and
      no nested/global/nonlocal/branch-body ``__all__`` store in the
      whole tree → ``ALL_DECLARED`` with the entries (order and
      duplicates preserved).
    * More than one direct write, or a value that is not a closed literal
      list/tuple of ``ast.Constant(str)`` elements, or any nested /
      global / nonlocal / branch-body ``__all__`` store in the tree, or
      any dynamic-namespace invalidation → ``ALL_UNKNOWN`` with ``()``
      (R-6, no salvage).
    * No ``__all__`` statement at all (including in branch bodies) and
      no invalidation → ``ALL_ABSENT`` with ``()`` (the §8 default rule
      may then apply).
    """
    _BRANCH = (ast.If, ast.For, ast.While, ast.Match, ast.Try, ast.TryStar,
               ast.With)

    def _module_scope_branch_has_all_store() -> bool:
        """True if any module-scope (non-function/class) branch contains
        a ``Name("__all__")`` in Store context.  Function/class bodies
        are skipped — those are §6 local bindings."""
        def _scan(stmts) -> bool:
            for s in stmts:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                if isinstance(s, _BRANCH):
                    for n in ast.walk(s):
                        if isinstance(n, ast.Name) and n.id == "__all__" \
                                and isinstance(n.ctx, ast.Store):
                            return True
                if isinstance(s, ast.Match):
                    for case in s.cases:
                        if _scan(case.body):
                            return True
            return False
        return _scan(tree.body)

    direct_assignments = [
        stmt for stmt in tree.body
        if (
            isinstance(stmt, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "__all__"
                for t in stmt.targets
            )
        ) or (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == "__all__"
        )
    ]
    if len(direct_assignments) == 0:
        # A branch-body (or nested) store of ``__all__`` — ``if x:
        # __all__ = (...)`` — is module-executing behaviour the analyzer
        # does not infer (§6 W20, W4) → not closed.
        if _module_scope_branch_has_all_store():
            return ALL_UNKNOWN, ()
        return ALL_ABSENT, ()
    if len(direct_assignments) > 1:
        return ALL_UNKNOWN, ()
    stmt = direct_assignments[0]
    if isinstance(stmt, ast.AnnAssign):
        # ``__all__: T = <expr>`` (non-None value) or ``__all__: T``
        # (annotation only — no value; not a closed assignment).
        if stmt.value is None:
            return ALL_UNKNOWN, ()
        value = stmt.value
    else:
        # ``__all__ = <expr>`` or multi-target like ``__all__, x = a, b``
        # (the latter is a valid store of ``__all__`` into a
        # non-scalar container — not a closed literal).
        if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name) \
                or stmt.targets[0].id != "__all__":
            return ALL_UNKNOWN, ()
        value = stmt.value
    if not isinstance(value, (ast.List, ast.Tuple)):
        return ALL_UNKNOWN, ()
    entries: list[str] = []
    for el in value.elts:
        if not (isinstance(el, ast.Constant) and isinstance(el.value, str)):
            return ALL_UNKNOWN, ()
        entries.append(el.value)
    if _module_scope_branch_has_all_store():
        return ALL_UNKNOWN, ()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Global, ast.Nonlocal))
            and "__all__" in node.names
        ):
            return ALL_UNKNOWN, ()
    return ALL_DECLARED, tuple(entries)


# ---------------------------------------------------------------------------
# Dynamic-namespace invalidation (§6 / §8; yields ALL_UNKNOWN, no fallback)
# ---------------------------------------------------------------------------


def _find_module_executing_stars(tree) -> bool:
    """True if the module contains a module-executing star import — a
    ``from <mod> import *`` statement at the **module scope**: directly
    in the module body, or inside a module body's control-flow branch
    (``if`` / ``for`` / ``while`` / ``with`` / ``match`` / ``try``).
    Function and class bodies do not run at module-import time, so a
    star import inside them is NOT module-executing (handled as
    ``INCOMPLETE`` by §8)."""
    _BRANCH = (ast.If, ast.For, ast.While, ast.With, ast.Match,
               ast.Try, ast.TryStar)
    _SKIP = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

    def _star_names(n):
        return any(a.name == "*" for a in getattr(n, "names", ()))

    def _check_stmts(stmts) -> bool:
        for s in stmts:
            if isinstance(s, _SKIP):
                continue
            if isinstance(s, ast.ImportFrom) and _star_names(s):
                return True
            if isinstance(s, _BRANCH):
                if _check_stmts(s.body):
                    return True
                if hasattr(s, 'orelse') and _check_stmts(s.orelse):
                    return True
                if isinstance(s, (ast.Try, ast.TryStar)):
                    for handler in s.handlers:
                        if _check_stmts(handler.body):
                            return True
                if isinstance(s, ast.Match):
                    for case in s.cases:
                        if _check_stmts(case.body):
                            return True
        return False

    return _check_stmts(tree.body)


def _find_module_executing_exprs(tree) -> bool:
    """True if the module contains a module-executing ``exec(...)`` /
    ``eval(...)`` call at the **module scope** — directly in the body
    or inside a module body's control-flow branch.  Calls inside
    *function or class bodies* do not run at module-import time and
    are NOT module-executing (handled as ``INCOMPLETE`` by §8)."""
    _BRANCH = (ast.If, ast.For, ast.While, ast.With, ast.Match,
               ast.Try, ast.TryStar)
    _SKIP = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

    def _has_exec_eval_call(n) -> bool:
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id in ("exec", "eval"):
            return True
        return False

    def _check_stmts(stmts) -> bool:
        for s in stmts:
            if isinstance(s, _SKIP):
                continue
            if isinstance(s, _BRANCH):
                if _check_stmts(s.body):
                    return True
                if hasattr(s, 'orelse') and _check_stmts(s.orelse):
                    return True
                if isinstance(s, (ast.Try, ast.TryStar)):
                    for handler in s.handlers:
                        if _check_stmts(handler.body):
                            return True
                if isinstance(s, ast.Match):
                    for case in s.cases:
                        if _check_stmts(case.body):
                            return True
            # check direct children of this module-scope stmt
            for n in ast.iter_child_nodes(s):
                if _has_exec_eval_call(n):
                    return True
        return False

    return _check_stmts(tree.body)


def _has_dynamic_invalidation(tree) -> bool:
    """True if the module contains a construct that invalidates
    declared/absent certainty at the **module scope** (R-6, §6/§8).

    Invalidation constructs:

    * a **module-executing** ``exec(...)`` / ``eval(...)`` call
      (directly in the module body or in a branch body — NOT inside
      an unexecuted function/class body);
    * a **module-executing** star import (``from m import *``) — R-7;
    * a ``globals()`` / ``locals()`` / ``vars()`` store at module scope
      (a dynamic namespace write);
    * a top-level ``del __all__`` / ``__all__ += ...``;
    * a nested-scope ``global`` / ``nonlocal`` declaration naming
      ``__all__`` (a write explicitly reaching the module level);
    * a module-scope store to an attribute target containing ``__all__``
      (e.g. ``m.__all__ = [...]``);
    * an alias-escape read of the module ``__all__`` (``x = __all__``)
      or a second module-scope store.

    §6: function-local or class-local ``__all__`` bindings do NOT
    invalidate a valid direct module-level declaration.  Similarly,
    ``exec`` / ``eval`` in an unexecuted function/class body is not a
    module-executing statement (handled as ``INCOMPLETE`` by §8).
    """
    if _find_module_executing_stars(tree):
        return True
    if _find_module_executing_exprs(tree):
        return True
    # nested-scope global/nonlocal reaching the module level
    for node in ast.walk(tree):
        if isinstance(node, (ast.Global, ast.Nonlocal)) and "__all__" in node.names:
            return True
    # module-scope: attribute target containing __all__, or globals/locals/vars write
    _NS_FUNCS = {"globals", "locals", "vars"}
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # attribute target
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Attribute) and t.attr == "__all__":
                    return True
        if isinstance(stmt, ast.AugAssign):
            target = stmt.target
            if isinstance(target, ast.Attribute) and target.attr == "__all__":
                return True
        if isinstance(stmt, ast.Delete):
            for t in stmt.targets:
                if (isinstance(t, ast.Name) and t.id == "__all__") \
                        or (isinstance(t, ast.Attribute) and t.attr == "__all__"):
                    return True
        # globals()/locals()/vars() write — check all sub-expressions
        for n in ast.walk(stmt):
            if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Call):
                inner = n.value
                if isinstance(inner.func, ast.Name) and inner.func.id in _NS_FUNCS:
                    if isinstance(n.ctx, (ast.Store, ast.Del)):
                        return True
    # module-scope branch bodies for __all__ stores
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, (ast.If, ast.For, ast.While, ast.With,
                             ast.Match, ast.Try, ast.TryStar)):
            for n in ast.walk(stmt):
                if isinstance(n, ast.Name) and n.id == "__all__" \
                        and isinstance(n.ctx, (ast.Store, ast.AugStore, ast.Del)):
                    return True
                if isinstance(n, ast.Attribute) and n.attr == "__all__" \
                        and isinstance(n.ctx, (ast.Store, ast.AugStore, ast.Del)):
                    return True
    # alias-escape: module-scope Load of __all__, or second Store
    body_all_uses: list = []

    def _scan_module_scope(stmts):
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for n in ast.walk(s):
                if isinstance(n, ast.Name) and n.id == "__all__":
                    body_all_uses.append(n)
                elif isinstance(n, ast.Attribute) and n.attr == "__all__":
                    body_all_uses.append(n)
            if isinstance(s, (ast.If, ast.For, ast.While, ast.With,
                              ast.Match, ast.Try, ast.TryStar)):
                _scan_module_scope(s.body)
                if hasattr(s, 'orelse'):
                    _scan_module_scope(s.orelse)
                if isinstance(s, (ast.Try, ast.TryStar)):
                    for handler in s.handlers:
                        _scan_module_scope(handler.body)
                if isinstance(s, ast.Match):
                    for case in s.cases:
                        _scan_module_scope(case.body)

    _scan_module_scope(tree.body)
    store_count = sum(
        1 for n in body_all_uses
        if isinstance(n.ctx, (ast.Store, ast.AugStore))
    )
    if store_count > 1:
        return True
    if any(isinstance(n.ctx, ast.Load) for n in body_all_uses):
        return True
    return False


def _contains_star(names) -> bool:
    for n in names:
        if getattr(n, "name", None) == "*":
            return True
    return False


def _has_all_store_beyond_assignment(tree) -> bool:
    """Detect ``__all__`` handling beyond a single closed literal
    assignment (W5 ``__all__.append(...)``, W7 ``del __all__``, W6
    alias-escape ``x = __all__; x.append(...)``): any ``Name("__all__")``
    read (Load) or an additional store outside the lone assignment
    statement.

    The single closed literal assignment itself (exactly one
    ``Store``-context occurrence) is allowed; anything beyond it is
    dynamic manipulation → invalidate to ``ALL_UNKNOWN``.
    """
    occurrences = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "__all__"
    ]
    stores = [n for n in occurrences if isinstance(n.ctx, ast.Store)]
    if len(stores) > 1:
        return True
    if any(isinstance(n.ctx, ast.Load) for n in occurrences):
        return True
    return False


# ---------------------------------------------------------------------------
# §8 star-default rule (only for ALL_ABSENT)
# ---------------------------------------------------------------------------


def _star_default(tree) -> tuple[tuple[str, ...], str]:
    """Apply the §8 star-default rule to a module with no (valid)
    ``__all__``.  Returns ``(star_names, default_scope)``.

    ``star_names`` is the deduplicated, code-point-sorted tuple of
    concrete direct module-body names (``function``/``class``/
    ``def``/``async def`` names and single-Name assignment targets,
    plain ``import x[.y]`` leading component and ``import x as z``
    alias).  ``*`` is **never** emitted.  ``default_scope`` is
    ``DIRECT_ONLY`` when no syntactically visible module-executing
    binding construct falls outside the rule; ``INCOMPLETE`` when it
    does (still bounded, direct-only evidence; ``*``-free).
    """
    names: set[str] = set()
    incomplete = False

    def _add(name) -> None:
        if name and not name.startswith("_"):
            names.add(name)

    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _add(stmt.name)
            continue
        if isinstance(stmt, ast.Assign):
            if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                _add(stmt.targets[0].id)
            else:
                incomplete = True
            continue
        if isinstance(stmt, ast.AnnAssign):
            if stmt.value is not None and isinstance(stmt.target, ast.Name):
                _add(stmt.target.id)
            else:
                # annotation without a value (``x: int``) or a non-Name
                # target — not a binding for star names; flag incomplete
                incomplete = True
            continue
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == "*":
                    incomplete = True
                    continue
                if alias.asname is not None:
                    _add(alias.asname)
                else:
                    _add(alias.name.split(".")[0])
            continue
        if isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if alias.name == "*":
                    incomplete = True
                    continue
                _add(alias.asname if alias.asname is not None else alias.name)
            continue
        # Any other top-level statement (control flow, loops, Del,
        # If/For/While/match, with, try, Raise, bare Expr, module
        # calls, etc.) is a module-executing construct outside the
        # direct-only rules → bound the evidence, mark INCOMPLETE.
        incomplete = True
    return tuple(sorted(names)), (DEFAULT_INCOMPLETE if incomplete else DEFAULT_DIRECT_ONLY)


# ---------------------------------------------------------------------------
# Per-candidate evidence (frozen candidate; never invents a candidate)
# ---------------------------------------------------------------------------


def _evidence_for_candidate(
    candidate: ModuleCandidate,
    target_sources: Mapping[ModuleCandidate, TargetSource],
) -> WildcardCandidateEvidence:
    """Static, conservative evidence for one target candidate.

    The source state derives from the caller's ``TargetSource`` (or a
    synthesized ``SRC_NOT_FOUND`` for a missing key, §4).  Parsing is
    the analyzer's duty (R-2): a ``SRC_SUPPLIED`` text that fails
    ``ast.parse`` reclassifies to ``SRC_UNPARSEABLE`` / ``ALL_UNKNOWN``
    with no salvage.  Only ``ALL_ABSENT`` and ``ALL_DECLARED`` yield
    name evidence — ``ALL_UNKNOWN``, ``SRC_UNPARSEABLE``,
    ``SRC_UNAVAILABLE`` and ``SRC_NOT_FOUND`` emit no name evidence and
    no default fallback (W16, R-2, R-6).
    """
    source_state, parse_diagnostic, text = _candidate_source(
        candidate, target_sources
    )
    if source_state in (SRC_NOT_FOUND, SRC_UNAVAILABLE, SRC_UNPARSEABLE):
        reason = {
            SRC_NOT_FOUND: WR_SOURCE_NOT_FOUND_NO_EVIDENCE,
            SRC_UNAVAILABLE: WR_SOURCE_UNAVAILABLE_NO_EVIDENCE,
            SRC_UNPARSEABLE: WR_SOURCE_UNPARSEABLE_NO_EVIDENCE,
        }[source_state]
        return WildcardCandidateEvidence(
            candidate=candidate,
            source_state=source_state,
            all_state=ALL_UNKNOWN,
            all_entries=(),
            star_names=(),
            default_scope=None,
            outcome=WR_NOT_ANALYZED,
            reason=reason,
        )
    tree = ast.parse(text)
    if _has_dynamic_invalidation(tree):
        # Dynamic namespace manipulation invalidates declared/absent
        # certainty: no name evidence, no default fallback (§6/§8, R-6).
        return WildcardCandidateEvidence(
            candidate=candidate,
            source_state=SRC_SUPPLIED,
            all_state=ALL_UNKNOWN,
            all_entries=(),
            star_names=(),
            default_scope=None,
            outcome=WR_NOT_ANALYZED,
            reason=WR_ALL_UNKNOWN_NO_EVIDENCE,
        )
    all_state, entries = _classify_all(tree)
    if all_state == ALL_DECLARED:
        return WildcardCandidateEvidence(
            candidate=candidate,
            source_state=SRC_SUPPLIED,
            all_state=ALL_DECLARED,
            all_entries=entries,
            star_names=entries,
            default_scope=None,
            outcome=WR_ANALYZED,
            reason=WR_ALL_DECLARED_APPLIED,
        )
    if all_state == ALL_UNKNOWN:
        # §6 closed grammar failed (conditional, reassignment, mutation,
        # alias-escape, non-literal, nested store) → no salvage, no
        # default fallback (R-6).
        return WildcardCandidateEvidence(
            candidate=candidate,
            source_state=SRC_SUPPLIED,
            all_state=ALL_UNKNOWN,
            all_entries=(),
            star_names=(),
            default_scope=None,
            outcome=WR_NOT_ANALYZED,
            reason=WR_ALL_UNKNOWN_NO_EVIDENCE,
        )
    # ALL_ABSENT: §8 star-default rule (no dynamic invalidation present).
    star_names, default_scope = _star_default(tree)
    return WildcardCandidateEvidence(
        candidate=candidate,
        source_state=SRC_SUPPLIED,
        all_state=ALL_ABSENT,
        all_entries=(),
        star_names=star_names,
        default_scope=default_scope,
        outcome=WR_ANALYZED,
        reason=WR_ALL_ABSENT_DEFAULT_APPLIED,
    )


# ---------------------------------------------------------------------------
# Context-level target-context reconstruction (inherits 2C-3B-1 metadata)
# ---------------------------------------------------------------------------


def _assessments_for(
    candidates: tuple[ModuleCandidate, ...],
    target_sources: Mapping[ModuleCandidate, TargetSource],
    supported_set: frozenset | None = None,
) -> tuple[SourceAssessment, ...]:
    """One :class:`SourceAssessment` per candidate, preserving the
    2C-3B-1 observable source availability (F2).

    When *supported_set* is not None, candidates outside it are
    assessed with state-only lookup (no ``ast.parse``) to honour
    §9: unsupported candidates are never parsed.
    """
    out: list[SourceAssessment] = []
    for c in candidates:
        if supported_set is not None and c not in supported_set:
            # Unsupported candidate — report state without parsing (§9).
            state, diagnostic = _source_state_unparsed(c, target_sources)
            out.append(
                SourceAssessment(candidate=c, state=state, parse_diagnostic=diagnostic)
            )
        else:
            state, diagnostic, _text = _candidate_source(c, target_sources)
            out.append(
                SourceAssessment(candidate=c, state=state, parse_diagnostic=diagnostic)
            )
    return tuple(out)


def _source_state_unparsed(
    candidate: ModuleCandidate,
    target_sources: Mapping[ModuleCandidate, TargetSource],
) -> tuple[str, str | None]:
    """State-only source check: return (state, diagnostic) without
    calling ``ast.parse``.  Used for unsupported candidates where §9
    forbids parsing.  Caller-supplied ``parse_error`` for
    ``SRC_UNPARSEABLE`` is preserved (F2: observable availability)."""
    if candidate not in target_sources:
        return SRC_NOT_FOUND, None
    ts = target_sources[candidate]
    if ts.provenance == SRC_UNAVAILABLE:
        return SRC_UNAVAILABLE, None
    if ts.provenance == SRC_UNPARSEABLE:
        return SRC_UNPARSEABLE, ts.parse_error
    # SRC_SUPPLIED: report as-is (no parse) — the caller knows this
    # candidate is unsupported and must not parse it.  No diagnostic
    # is generated because parsing is forbidden by §9.
    return SRC_SUPPLIED, None


def _context_from_absolute(
    record: ImportRecord,
    inventory: tuple[ModuleCandidate, ...],
    target_sources: Mapping[ModuleCandidate, TargetSource],
) -> tuple[TargetContext, tuple[WildcardCandidateEvidence, ...], str | None]:
    """Build the single absolute-context envelope.

    Returns the (context, candidates, root_issue) triple; on a resolver
    pre-context failure (``NOT_ATTEMPTED`` invalid-kind/level or an
    unsupported envelope) the context carries the frozen status/reason
    and ``candidates`` is ``()``.
    """
    resolution = resolve_absolute_import(record, inventory)
    assessments = _assessments_for(resolution.supported_candidates, target_sources)
    context = TargetContext(
        kind=TARGET_ABSOLUTE,
        source_candidate=None,
        base_module=record.module if isinstance(record.module, str) else "",
        base_components=None,
        target_components=resolution.requested_components,
        lossless=None,
        status=resolution.status,
        reason=resolution.reason,
        supported_candidates=resolution.supported_candidates,
        unsupported_candidates=resolution.unsupported_candidates,
        assessments=assessments,
    )
    # Supported candidates get evidence; unsupported candidates get a
    # conservative W15-style row (WR_NOT_ANALYZED / WR_TARGET_UNSUPPORTED,
    # ALL_UNKNOWN, empty evidence, default_scope=None) and are never
    # parsed.  Partition order is preserved: supported then unsupported,
    # per §5/§9 "supported rows precede unsupported rows".
    support_ev = tuple(
        _evidence_for_candidate(c, target_sources)
        for c in resolution.supported_candidates
    )
    unsupported_ev = tuple(
        WildcardCandidateEvidence(
            candidate=c,
            source_state=SRC_NOT_FOUND,
            all_state=ALL_UNKNOWN,
            all_entries=(),
            star_names=(),
            default_scope=None,
            outcome=WR_NOT_ANALYZED,
            reason=WR_TARGET_UNSUPPORTED,
        )
        for c in resolution.unsupported_candidates
    )
    return context, support_ev + unsupported_ev, None


def _contexts_for_relative(
    record: ImportRecord,
    inventory: tuple[ModuleCandidate, ...],
    target_sources: Mapping[ModuleCandidate, TargetSource],
) -> tuple[tuple[WildcardContextAnalysis, ...], str | None]:
    """Build one context analysis per frozen ``ContextResolution``.

    A relative pre-context failure (``RelativeResolution.issue`` set)
    returns empty contexts and preserves the original issue verbatim
    (W18, D17/F5 envelope).
    """
    rel = resolve_relative_import(record, inventory)
    if rel.issue is not None:
        return (), rel.issue
    out: list[WildcardContextAnalysis] = []
    for idx, ctx in enumerate(rel.contexts):
        target_context = TargetContext(
            kind=TARGET_RELATIVE,
            source_candidate=ctx.source_candidate,
            base_module=None,
            base_components=ctx.base_components,
            target_components=ctx.target_components,
            lossless=ctx.lossless,
            status=ctx.status,
            reason=ctx.reason,
            supported_candidates=ctx.supported_candidates,
            unsupported_candidates=ctx.unsupported_candidates,
            assessments=_assessments_for(ctx.supported_candidates, target_sources),
        )
        support_ev = tuple(
            _evidence_for_candidate(c, target_sources)
            for c in ctx.supported_candidates
        )
        unsupported_ev = tuple(
            WildcardCandidateEvidence(
                candidate=c,
                source_state=SRC_NOT_FOUND,
                all_state=ALL_UNKNOWN,
                all_entries=(),
                star_names=(),
                default_scope=None,
                outcome=WR_NOT_ANALYZED,
                reason=WR_TARGET_UNSUPPORTED,
            )
            for c in ctx.unsupported_candidates
        )
        out.append(
            WildcardContextAnalysis(
                context_index=idx,
                context=target_context,
                candidates=support_ev + unsupported_ev,
            )
        )
    return tuple(out), None


# ---------------------------------------------------------------------------
# Root outcome derivation (§7 "declared once")
# ---------------------------------------------------------------------------


def _root_outcome(
    issue: str | None,
    contexts: tuple[WildcardContextAnalysis, ...],
) -> tuple[str, str | None]:
    """Derive the root ``(outcome, reason)`` from the context set.

    §7 Outcome semantics (declared once):

    - ``WR_ANALYZED`` — **root-level**: the analyzer wrapper ran against
      the supplied evidence; it does NOT assert resolver success.
    - ``WR_NOT_ANALYZED`` — target not analyzable (no analyzable
      candidate with a parseable source, or all contexts
      UNSUPPORTED/UNRESOLVED) or a pre-context failure.

    Root logic (cross-context aggregation; no early return on
    UNSUPPORTED):

    - Pre-context failure (issue set) →
      ``WR_NOT_ANALYZED`` / ``WR_TARGET_NO_CANDIDATE`` (W18).
    - Scan ALL contexts.  ``UNSUPPORTED`` contexts do NOT cause a
      premature early return — they are a fallback reason only.
    - If ANY context (non-UNSUPPORTED) produced a ``WR_ANALYZED``
      candidate → root ``WR_ANALYZED`` with the dominant reason
      (DECLARED > ABSENT > ALL_UNKNOWN).
    - If all candidates are absent / no source:
      * any UNSUPPORTED context/candidate present →
        ``WR_NOT_ANALYZED`` / ``WR_TARGET_UNSUPPORTED``
      * otherwise →
        ``WR_NOT_ANALYZED`` / ``WR_TARGET_NO_CANDIDATE``
    """
    if issue is not None:
        return WR_NOT_ANALYZED, WR_TARGET_NO_CANDIDATE

    # §9: UNSUPPORTED contexts still allow other contexts to contribute
    # evidence.  We scan ALL contexts here; UNSUPPORTED does not cause
    # a premature early return.  It is only a fallback reason when no
    # other context produced evidence.
    any_unsupported_ctx = False
    has_analyzed = False
    has_declared = False
    has_absent = False
    all_no_source = True
    has_unsupported_candidate = False

    for ctx in contexts:
        if ctx.context.status == "UNSUPPORTED":
            any_unsupported_ctx = True
            # No candidate in an UNSUPPORTED context can have been
            # analyzed, but record that unsupported candidates exist.
            if ctx.candidates:
                has_unsupported_candidate = True
            continue
        for ev in ctx.candidates:
            if ev.outcome == WR_ANALYZED:
                has_analyzed = True
                all_no_source = False
                if ev.reason == WR_ALL_DECLARED_APPLIED:
                    has_declared = True
                elif ev.reason == WR_ALL_ABSENT_DEFAULT_APPLIED:
                    has_absent = True
            elif ev.reason in (
                WR_ALL_UNKNOWN_NO_EVIDENCE,
                WR_SOURCE_UNPARSEABLE_NO_EVIDENCE,
            ):
                all_no_source = False
            elif ev.outcome == WR_NOT_ANALYZED and ev.reason == WR_TARGET_UNSUPPORTED:
                has_unsupported_candidate = True

    if all_no_source and not has_analyzed:
        # No candidate had a supplied parseable source.
        if any_unsupported_ctx or has_unsupported_candidate:
            return WR_NOT_ANALYZED, WR_TARGET_UNSUPPORTED
        return WR_NOT_ANALYZED, WR_TARGET_NO_CANDIDATE

    # Wrapper ran against supplied evidence → root WR_ANALYZED.
    if has_declared:
        return WR_ANALYZED, WR_ALL_DECLARED_APPLIED
    if has_absent:
        return WR_ANALYZED, WR_ALL_ABSENT_DEFAULT_APPLIED
    if has_analyzed:
        return WR_ANALYZED, WR_ALL_ABSENT_DEFAULT_APPLIED

    # Wrapper ran but all analyzable candidates are ALL_UNKNOWN / unparseable.
    return WR_ANALYZED, WR_ALL_UNKNOWN_NO_EVIDENCE


# ---------------------------------------------------------------------------
# Routing decision (mirrors the 2C-3B-1 ``_routing_signal`` surface)
# ---------------------------------------------------------------------------


_ABSOLUTE_KINDS = {"import", "from_import"}


def _routing_signal(record: ImportRecord) -> str:
    """Absolute vs. relative routing for a star import record.

    Mirrors the 2C-3B-1 rule: ``from_import`` with a non-bool
    ``level >= 1`` → relative; everything else → absolute (a
    ``kind == "import"`` star is malformed by R-1 and is rejected
    before routing, so it never reaches this path).
    """
    level = record.level
    level_ok = (
        isinstance(level, int)
        and not isinstance(level, bool)
        and level >= 1
    )
    if level_ok and record.kind == "from_import":
        return TARGET_RELATIVE
    return TARGET_ABSOLUTE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_wildcard_import(
    record: ImportRecord,
    inventory: tuple[ModuleCandidate, ...],
    target_sources: Mapping[ModuleCandidate, TargetSource],
) -> WildcardAnalysis:
    """Analyze one ``from X import *`` record against a frozen inventory.

    See the module docstring and the approved architecture
    (``docs/phase-2c-3b-2-architecture-approved.md``) for the full
    contract:

    Args:
        record: a well-typed :class:`ImportRecord` — either a valid star
            record (exactly one ``("*", None)`` name entry under
            ``kind="from_import"``), or a well-typed non-star record
            (returned as ``WR_NOT_IN_SCOPE`` / ``WR_INVALID_RECORD``,
            ``contexts=()`` per R-1/§11).  A malformed star (star with
            alias, mixed names, multiple stars, ``kind != "from_import"``
            for the star) raises ``ValueError``.  A non
            :class:`ImportRecord` value raises ``TypeError``.
        inventory: a *tuple* of :class:`ModuleCandidate` values — the
            exact type is required (§4: wrong type → ``TypeError``).
            Elements are frozen candidates; their internal state is not
            mutated; the returned result preserves their identity verbatim.
        target_sources: a REQUIRED :class:`Mapping` whose keys are
            inventory candidates and whose values are :class:`TargetSource`
            objects validated against the 2C-3B-1 state table (§4, R-8:
            an extra key not in the inventory → ``ValueError``; any
            non-:class:`TargetSource` value or a field violating the
            state table → ``TypeError``).  ``None`` → ``TypeError``.
            A missing key synthesizes ``SRC_NOT_FOUND``.

    Returns:
        A frozen :class:`WildcardAnalysis` — see its docstring for the
        cardinality and outcome invariants (per-candidate
        :class:`WildcardCandidateEvidence` rows inside each context,
        a root-level outcome/reason, and a canonical flat
        ``source_assessments`` tuple over unique inventory candidates).

    Raises:
        TypeError: if any argument has the wrong Python type.
        ValueError: if the record is a malformed star, or if
            ``target_sources`` keys identify a candidate not present
            in the inventory (R-8), or if the frozen propagation rule
            hits a conflicting duplicate candidate
            (``ValueError`` propagated — R-3).
    """
    # 1. record type (R-1 / §4 wrong-type rule).
    if not isinstance(record, ImportRecord):
        raise TypeError(
            f"record must be an ImportRecord; got {type(record).__name__}"
        )
    # 2. inventory must be a tuple of ModuleCandidate (§4).
    if not isinstance(inventory, tuple):
        raise TypeError(
            f"inventory must be a tuple; got {type(inventory).__name__}"
        )
    for c in inventory:
        if not isinstance(c, ModuleCandidate):
            raise TypeError(
                "inventory must contain only ModuleCandidate values; "
                f"got {type(c).__name__}"
            )
    # 3. target_sources is required and must be a Mapping with
    #    ModuleCandidate keys and TargetSource values (§4).
    if target_sources is None:
        raise TypeError(
            "target_sources must be a Mapping[ModuleCandidate, "
            "TargetSource]; pass {} when no candidate has caller-supplied "
            "source"
        )
    if not isinstance(target_sources, Mapping):
        raise TypeError(
            "target_sources must be a Mapping[ModuleCandidate, "
            f"TargetSource]; got {type(target_sources).__name__}"
        )
    inv_by_identity = {id(c) for c in inventory}
    for k in target_sources.keys():
        if not isinstance(k, ModuleCandidate):
            raise TypeError(
                "target_sources keys must be ModuleCandidate values "
                f"(identifying an inventory candidate); got "
                f"{type(k).__name__}"
            )
        if id(k) not in inv_by_identity and k not in inventory:
            # R-8: extra key not in the inventory is an explicit ValueError
            # (not silently inherited / resolved).
            raise ValueError(
                "target_sources keys that do not identify an inventory "
                "candidate are rejected (R-8 / §9): "
                f"{k!r} is not in the inventory"
            )
    for v in target_sources.values():
        if not isinstance(v, TargetSource):
            raise TypeError(
                "target_sources values must be TargetSource; "
                f"got {type(v).__name__}"
            )
        _validate_target_source(v)  # validate state table before routing (§4)
    # 4. star shape (R-1: returns 'star', returns 'non_star', or raises).
    star_kind = _star_shape(record)
    if star_kind == "non_star":
        return _not_in_scope_result(record)
    # 5. routing and context construction.
    signal = _routing_signal(record)
    if signal == TARGET_ABSOLUTE:
        context, candidates, _issue = _context_from_absolute(
            record, inventory, target_sources
        )
        contexts = (
            WildcardContextAnalysis(
                context_index=0, context=context, candidates=candidates
            ),
        )
        issue: str | None = None
    else:
        contexts, issue = _contexts_for_relative(
            record, inventory, target_sources
        )
    outcome, reason = _root_outcome(issue, contexts)
    # 6. flat source_assessments over unique inventory candidates,
    #    sorted by the public candidate key (§10: input order is not
    #    output order; canonical sort gives permutation-invariance).
    unique: dict = {}
    for c in inventory:
        unique[_canonical_key(c)] = c
    ordered_candidates = tuple(unique[k] for k in sorted(unique))
    # §9: unsupported candidates must not be parsed.  Collect all
    # supported candidates across all contexts so the flat assessments
    # know which to skip.
    _supported_all: set = set()
    for ctx_analysis in contexts:
        _supported_all.update(ctx_analysis.context.supported_candidates)
    _supported_frozen = frozenset(_supported_all)
    assessments = _assessments_for(ordered_candidates, target_sources,
                                   supported_set=_supported_frozen)
    return WildcardAnalysis(
        record=record,
        target_kind=signal,
        outcome=outcome,
        reason=reason,
        issue=issue,
        contexts=contexts,
        source_assessments=assessments,
    )


