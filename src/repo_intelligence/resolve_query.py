"""Phase 2C-4 — Resolution integration, query / API exposure.

A read-only, deterministic query adapter that composes the three existing
frozen tiers from a single established database snapshot:

* Phase 2C-2A absolute resolution (``resolve_absolute_import``)
* Phase 2C-3A relative resolution (``resolve_relative_import``)
* Phase 2C-3B-1 / 2C-3B-2 named / wildcard analysis (``analyze_named_import`` /
  ``analyze_wildcard_import``)

The new layer is a thin adapter: it performs no re-computation beyond
dispatching frozen records to their already-frozen analysis functions,
validates caller-supplied source mappings against a complete inventory,
applies the approved filters / pagination, and provides a recursive
JSON projection of the immutable result.

The Python result and the JSON body have **no aggregate status or reason**
field. Every record's outcome is preserved exactly as the frozen tier
reports it (per-context, per-partition), as fixed by docs/phase-2c-4-
architecture-approved.md §4 (Q2) and §6 (B1–B14).

The wire projection (``resolve_to_dict``) intentionally omits source text
and parse diagnostics to keep the JSON body stable and portable; it also
retains a boolean ``diagnostic_present`` flag per source assessment so
consumers can tell whether a parse was attempted. Structural identity,
candidate partitions, relative contexts, and declared export literals
(``all_entries`` / ``star_names``) are preserved exactly.

Read-only guarantees:

* A missing ``db_path`` raises :class:`FileNotFoundError`; the file is not
  created.
* A wrong argument type raises :class:`TypeError`; an invalid argument
  value raises :class:`ValueError`.
* The database is opened read-only (``mode=ro`` + ``PRAGMA query_only``)
  on a single owned handle inside one explicit ``BEGIN DEFERRED``
  transaction; no writes occur anywhere.
* Frozen analyzer exceptions propagate without wrapping or partial
  envelope generation (per approved Q5); serialization raises
  ``TypeError`` on unknown types rather than silently using ``str()``
  fallbacks.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from repo_intelligence.absolute_resolution import (
    Resolution,
    resolve_absolute_import,
)
from repo_intelligence.db_absolute_resolution import (
    AdapterError,
    _open_read_only,
    _record_order_key,
    _validate_import_rows,
    _validate_schema,
    _verify_no_orphans,
    _load_and_validate_names,
)
from repo_intelligence.imported_name_analysis import (
    ImportNameAnalysis,
    NameEvidence,
    SourceAssessment,
    ReexportNameEvidence,
    DefinitionNameEvidence,
    TargetContext,
    TargetNameAnalysis,
    SubmoduleNameEvidence,
    TargetSource,
    SRC_NOT_FOUND,
    SRC_SUPPLIED,
    SRC_UNAVAILABLE,
    SRC_UNPARSEABLE,
    analyze_named_import,
)
from repo_intelligence.module_inventory import (
    ModuleCandidate,
    PackageRoot,
    build_module_inventory,
    load_file_records_from_db,
    _canonical_roots,
)
from repo_intelligence.python_ast import ImportRecord
from repo_intelligence.relative_resolution import (
    ContextResolution,
    RelativeResolution,
    resolve_relative_import,
)
from repo_intelligence.wildcard_import_analysis import (
    WildcardAnalysis,
    WildcardCandidateEvidence,
    WildcardContextAnalysis,
    analyze_wildcard_import,
)

__all__ = [
    "ResolveFilters",
    "ResolvedImportRecord",
    "ResolveQueryResult",
    "resolve_imports_from_index",
    "resolve_to_dict",
]
# ---------------------------------------------------------------------------
# P1. Immutable result models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolveFilters:
    """Exact-match query filters (no case folding, no path rewriting)."""

    source_path: str | None
    kind: str | None
    module: str | None


@dataclass(frozen=True)
class ResolvedImportRecord:
    """One wrapped import record with its dispatched frozen tier results.

    ``routing`` is the *wrapper's* dispatch decision ("absolute" or
    "relative"), not an analyzer's ``target_kind``.  Only the absolute
    path may carry ``absolute`` / optionally ``named`` / ``wildcard``;
    only the relative path may carry ``relative``.
    """

    record: ImportRecord
    routing: str
    absolute: Resolution | None
    relative: RelativeResolution | None
    named: ImportNameAnalysis | None
    wildcard: WildcardAnalysis | None


@dataclass(frozen=True)
class ResolveQueryResult:
    """Envelope for one ``resolve_imports_from_index`` query.

    Carries the complete immutable query metadata (schema version, exact
    ``db_path`` string, canonical root records, filters, pagination) so a
    successful empty page still yields a valid envelope, plus the record
    tuple for the selected page.
    """

    schema_version: str
    db_path: str
    roots: tuple[PackageRoot, ...]
    filters: ResolveFilters
    limit: int | None
    offset: int
    total_count: int
    count: int
    records: tuple[ResolvedImportRecord, ...]


# ---------------------------------------------------------------------------
# P3. Internal helpers
# ---------------------------------------------------------------------------

_ROUTING_ABSOLUTE = "absolute"
_ROUTING_RELATIVE = "relative"


def _is_int(value: object) -> bool:
    """True for real integers, excluding bools (approved P3 policy)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _name_sort_key(name: object, asname: object) -> tuple:
    """Total-order key for one ``(name, asname)`` pair (alias presence
    tagged) — used only for canonical ordering, never to rewrite the
    record.  Mirrors the frozen 2C-2B ordering so the row-sort is stable
    and comparable across the whole domain.
    """
    return (name, (0 if asname is None else 1, asname))


def _record_order_key(record: ImportRecord) -> tuple:
    """Frozen, row-ID-free total-order key
    ``(source_path, start_line, start_col, kind, module, level, names)``."""
    return (
        record.source_path,
        record.start_line,
        record.start_col,
        record.kind,
        record.module,
        record.level,
        tuple(_name_sort_key(n, a) for n, a in record.names),
    )


def _has_star(record: ImportRecord) -> bool:
    """True when ANY imported name is the wildcard ``*`` (alias ignored).

    The wrapper's dispatch rule (approved §5): any ``from_import`` absolute
    record with at least one ``*`` occurrence in ``names`` is routed to the
    frozen wildcard analyzer. The analyzer itself validates the star shape
    and propagates its own frozen exception for malformed/aliased stars —
    the wrapper must not pre-filter on alias presence.
    """
    return any(n == "*" for n, _a in record.names)


def _routing(record: ImportRecord) -> str:
    """Wrapper dispatch: ``from_import`` with positive level is relative;
    every other structurally representable record is absolute.  (Approved
    §5 dispatch table; a malformed/unknown record still routes to the
    frozen absolute resolver so its classification is preserved.)"""
    if record.kind == "from_import" and isinstance(record.level, int) \
            and not isinstance(record.level, bool) and record.level > 0:
        return _ROUTING_RELATIVE
    return _ROUTING_ABSOLUTE


def _validate_scalar_args(
    db_path: object,
    roots: object,
    *,
    source_path: object = None,
    kind: object = None,
    module: object = None,
    limit: object = None,
    offset: object = 0,
) -> tuple[list[str], ResolveFilters]:
    """Validate scalar arguments *before* touching the database.

    Raises :class:`TypeError` for wrong argument types and
    :class:`ValueError` for invalid argument values (approved P3).
    Returns ``(roots_list, filters)`` on success.
    """
    if not isinstance(db_path, str) or db_path == "":
        raise TypeError(
            f"db_path must be a non-empty string; got {type(db_path).__name__}"
        )
    if not isinstance(roots, (tuple, list)):
        raise TypeError(
            f"roots must be a tuple or list of strings; got {type(roots).__name__}"
        )
    if len(roots) == 0:
        raise ValueError("roots must be a non-empty sequence of roots")
    for r in roots:
        if not isinstance(r, str):
            raise TypeError(f"roots entries must be str; got {type(r).__name__}")

    for label, value in (
        ("source_path", source_path),
        ("module", module),
        ("kind", kind),
    ):
        if value is not None and not isinstance(value, str):
            raise TypeError(
                f"{label} must be str or None; got {type(value).__name__}"
            )
    if kind is not None and kind not in ("import", "from_import"):
        raise ValueError(f'kind must be "import" or "from_import"; got {kind!r}')

    if limit is not None and not _is_int(limit):
        raise TypeError(f"limit must be an int or None; got {limit!r}")
    if limit is not None and limit < 0:
        raise ValueError(f"limit must be >= 0; got {limit!r}")
    if not _is_int(offset):
        raise TypeError(f"offset must be an int; got {offset!r}")
    if offset < 0:
        raise ValueError(f"offset must be >= 0; got {offset!r}")

    return (
        list(roots),
        ResolveFilters(source_path=source_path, kind=kind, module=module),
    )


#: Caller-valid TargetSource states (a mapped ``SRC_NOT_FOUND`` or any
#: other value is invalid input, never silently repaired).
_VALID_TARGET_SOURCE_STATES = (
    SRC_SUPPLIED,
    SRC_UNAVAILABLE,
    SRC_UNPARSEABLE,
)


def _validate_target_sources(
    target_sources: Mapping | None,
    inventory: tuple[ModuleCandidate, ...],
) -> dict:
    """Normalize + validate the caller source mapping at the wrapper boundary.

    ``None`` becomes ``{}`` (no caller-supplied sources).  A provided
    mapping is snapshotted (never mutated) and every entry is validated:
    keys must be :class:`ModuleCandidate` values equal to inventory
    candidates (else :class:`ValueError`), values must be well-formed
    :class:`TargetSource` (else :class:`TypeError`).  A well-typed key
    missing from the inventory raises ``ValueError`` even on an empty page.
    Returns the snapshot dict to pass unchanged to the analyzers.
    """
    if target_sources is None:
        return {}
    if not isinstance(target_sources, Mapping):
        raise TypeError(
            "target_sources must be a Mapping[ModuleCandidate, TargetSource] "
            f"or None; got {type(target_sources).__name__}"
        )
    inventory = tuple(inventory)
    snapshot: dict = {}
    for key, value in target_sources.items():
        if not isinstance(key, ModuleCandidate):
            raise TypeError(
                "target_sources keys must be ModuleCandidate; "
                f"got {type(key).__name__}"
            )
        if not isinstance(value, TargetSource):
            raise TypeError(
                "target_sources values must be TargetSource; "
                f"got {type(value).__name__}"
            )
        # Full frozen TargetSource state-table validation (R4).  A mapped
        # ``SRC_NOT_FOUND`` or ANY invalid provenance/state/text/parse_error
        # combination is invalid input and is never silently repaired — even
        # on an empty page or an ordinary-import-only selection.  (A value
        # that bypassed the frozen constructor is rejected here; the frozen
        # analyzers perform the identical validation when called directly.)
        prov = value.provenance
        st = value.source_text
        er = value.parse_error
        if prov == SRC_NOT_FOUND:
            raise TypeError(
                "a mapped TargetSource must not have provenance SRC_NOT_FOUND "
                "(that is synthesized by the frozen analyzers, never supplied)"
            )
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
        else:
            raise TypeError(
                "TargetSource.provenance must be SRC_SUPPLIED, SRC_UNAVAILABLE "
                f"or SRC_UNPARSEABLE; got {prov!r}"
            )
        if key not in inventory:
            raise ValueError(
                f"target_sources references candidate {key.qualified_name!r} "
                "which is not in this call's inventory"
            )
        snapshot[key] = value
    return snapshot


def _build_where(filters: ResolveFilters) -> tuple[str, list]:
    """Exact-match ``WHERE`` clause + params for the requested filters.

    ``kind`` and ``module`` are persisted columns; ``source_path`` filters
    on the joined files rel_path. Empty strings remain exact matches.
    """
    clauses: list[str] = []
    params: list = []
    if filters.source_path is not None:
        clauses.append("f.rel_path = ?")
        params.append(filters.source_path)
    if filters.kind is not None:
        clauses.append("i.kind = ?")
        params.append(filters.kind)
    if filters.module is not None:
        clauses.append("i.module = ?")
        params.append(filters.module)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _select_matching_records(
    conn: sqlite3.Connection,
    filters: ResolveFilters,
) -> tuple[tuple[ImportRecord, ...], int]:
    """Load the complete matching statement set with contiguous names.

    Returns ``(records, total_count)`` where ``records`` is the full,
    canonically sorted tuple of reconstructed :class:`ImportRecord`
    objects for all matching statements (not yet paginated), and
    ``total_count == len(records)``.
    """
    where, params = _build_where(filters)
    query = (
        "SELECT i.id, f.rel_path, i.kind, i.module, i.level, "
        "i.start_line, i.start_col "
        "FROM imports AS i JOIN files AS f ON f.id = i.file_id"
        + where
        + " ORDER BY f.rel_path COLLATE BINARY, i.start_line, i.start_col, "
        "i.kind, i.module, i.level, i.id"
    )
    rows = conn.execute(query, params).fetchall()
    import_ids = [row[0] for row in rows]

    _validate_import_rows(rows)
    names_by_import = _load_and_validate_names(conn, import_ids)
    records: list[ImportRecord] = [
        ImportRecord(
            kind=row[2],
            module=row[3],
            names=tuple(names_by_import.get(row[0], ())),
            level=row[4],
            source_path=row[1],
            start_line=row[5],
            start_col=row[6],
        )
        for row in rows
    ]
    records.sort(key=_record_order_key)
    records_tuple = tuple(records)
    return records_tuple, len(records_tuple)


_SCHEMA_VERSION = "repo-intelligence.resolve.v1"


def _wrap_absolute(record: ImportRecord, inventory: tuple[ModuleCandidate, ...], mapping: dict) -> ResolvedImportRecord:
    """Dispatch an absolute-routed record to the frozen tier(s) and wrap.

    Always attaches the frozen absolute :class:`Resolution`. Then, and only
    when the record is a ``from_import``:
    * has a star occurrence → attach frozen wildcard analysis
    * otherwise has non-empty names → attach frozen named analysis
    * else → neither (no fabricated analysis for an ``import`` or empty names)

    A tier conflict or validation error (per the frozen analyzers) propagates
    without producing a partial record.
    """
    absolute = resolve_absolute_import(record, inventory)
    named: ImportNameAnalysis | None = None
    wildcard: WildcardAnalysis | None = None
    if record.kind == "from_import" and record.names:
        if _has_star(record):
            wildcard = analyze_wildcard_import(record, inventory, mapping)
        else:
            named = analyze_named_import(record, inventory, mapping)
    return ResolvedImportRecord(
        record=record,
        routing=_ROUTING_ABSOLUTE,
        absolute=absolute,
        relative=None,
        named=named,
        wildcard=wildcard,
    )


def _wrap_relative(record: ImportRecord, inventory: tuple[ModuleCandidate, ...]) -> ResolvedImportRecord:
    """Dispatch a relative-routed record to the frozen relative tier only.

    Named / wildcard attachment is deferred under Q4 (the approved 2C-4
    scope); relative rows expose the complete frozen :class:`RelativeResolution`
    and nothing else.
    """
    relative = resolve_relative_import(record, inventory)
    return ResolvedImportRecord(
        record=record,
        routing=_ROUTING_RELATIVE,
        absolute=None,
        relative=relative,
        named=None,
        wildcard=None,
    )


def resolve_imports_from_index(
    db_path: str,
    roots: tuple[str, ...] | list[str],
    *,
    target_sources: Mapping[ModuleCandidate, TargetSource] | None = None,
    source_path: str | None = None,
    kind: str | None = None,
    module: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> ResolveQueryResult:
    """Query resolved import records from an existing index (read-only).

    See the module docstring and
    :file:`docs/phase-2c-4-architecture-approved.md` for the full contract.
    This is a single-snapshot, fail-closed, read-only adapter: it opens one
    ``mode=ro`` handle, builds the complete inventory once, selects and
    orders the matching records, wraps each with its frozen tier result(s),
    and returns an immutable :class:`ResolveQueryResult` envelope.
    """
    # 1. Validate scalar arguments before touching the database; build the
    #    canonical roots tuple via the frozen 2C-1 canonicalizer.  R8: this
    #    happens before ``_open_read_only`` so an invalid root combined with
    #    a missing DB fails root validation first (not INDEX_NOT_FOUND).
    roots_list, filters = _validate_scalar_args(
        db_path, roots,
        source_path=source_path, kind=kind, module=module,
        limit=limit, offset=offset,
    )
    # Canonicalize the roots via the frozen canonicalizer.  This is the
    # exact Phase 2C-1 validation; a ValueError/TypeError here is a query-
    # input error (INVALID_QUERY / CLI exit 2), not a DB/storage error.
    roots_tuple: tuple[PackageRoot, ...] = _canonical_roots(roots_list)

    # 2. Open one existing-file read-only connection.
    conn = _open_read_only(db_path)
    try:
        # 3. Begin one explicit read snapshot (BEGIN DEFERRED) before any
        #    schema/data read. Mirrors the Phase 2C-2B pattern. An engine
        #    failure during BEGIN is a query/storage failure: propagate it
        #    without continuing unprotected on an unestablished snapshot
        #    (a newly owned handle must own its transaction).
        conn.execute("BEGIN DEFERRED")

        # 4. Validate required schema + global import/import-name orphan
        #    integrity on *this* handle (fail closed).  The orphan check
        #    is a global storage invariant and is required on every call,
        #    even when the requested page is empty, because the query
        #    completes only when the snapshot is trusted.
        _validate_schema(conn)
        _verify_no_orphans(conn)

        # 5. Build the complete inventory from the same snapshot.
        file_records = load_file_records_from_db(conn)
        inventory = build_module_inventory(file_records, roots_list)

        # 6. Select the complete matching statement set (fail closed on
        #    malformed or orphan name rows even when a filter would hide them
        #    from the chosen page), then apply pagination by row ordering.
        records, total_count = _select_matching_records(conn, filters)

        if offset:
            records = records[offset:]
        if limit is not None and limit >= 0:
            records = records[:limit]

        # 7. Normalize + validate the caller source mapping once against the
        #    complete inventory; never mutate the caller's mapping.
        mapping = _validate_target_sources(target_sources, tuple(inventory))

        # 8. Dispatch each selected record through its frozen tier(s) and
        #    build the immutable wrapper records. A tier conflict or
        #    validation exception propagates without partial return.
        wrapped: list[ResolvedImportRecord] = []
        for rec in records:
            if _routing(rec) == _ROUTING_RELATIVE:
                wrapped.append(_wrap_relative(rec, tuple(inventory)))
            else:
                wrapped.append(_wrap_absolute(rec, tuple(inventory), mapping))

        return ResolveQueryResult(
            schema_version=_SCHEMA_VERSION,
            db_path=db_path,
            roots=roots_tuple,
            filters=filters,
            limit=limit,
            offset=offset,
            total_count=total_count,
            count=len(wrapped),
            records=tuple(wrapped),
        )
    finally:
        # R5: close the owned handle on every path (success, analysis
        # failure, or a BEGIN/transaction failure).  Best-effort ROLLBACK
        # first so a still-open read transaction is released before close.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        conn.close()


# ---------------------------------------------------------------------------
# P4. Serializer and recursive wire projection
# ---------------------------------------------------------------------------


def _project(value: object) -> object:
    """Recursively project a value into a JSON-compatible structure.

    Every key/field is emitted verbatim for the known frozen types, in
    lexicographic order, with tuples converted to arrays and ``None`` to
    ``null``.  Diagnostic / free-form text (``parse_diagnostic``,
    ``parse_error``, ``source_text``) is deliberately **omitted** to keep
    the wire body stable; ``SourceAssessment`` gets a boolean
    ``diagnostic_present`` in its place.  Unknown Python objects raise
    ``TypeError`` rather than falling back to ``str()``/``repr()``.
    """
    if value is None or isinstance(value, (int, float, bool, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_project(v) for v in value]
    if isinstance(value, dict):
        return {k: _project(v) for k, v in value.items()}

    # --- Known frozen / dataclass types ------------------------------------
    if isinstance(value, ResolveQueryResult):
        return _project_resolve_query_result(value)
    if isinstance(value, ResolveFilters):
        return {
            "source_path": _project(value.source_path),
            "kind": _project(value.kind),
            "module": _project(value.module),
        }
    if isinstance(value, ResolvedImportRecord):
        return {
            "record": _project(value.record),
            "routing": value.routing,
            "absolute": _project(value.absolute),
            "relative": _project(value.relative),
            "named": _project(value.named),
            "wildcard": _project(value.wildcard),
        }
    if isinstance(value, ImportRecord):
        return {
            "kind": value.kind,
            "module": value.module,
            "names": [{"name": n, "asname": a} for n, a in value.names],
            "level": value.level,
            "source_path": value.source_path,
            "start_line": value.start_line,
            "start_col": value.start_col,
        }
    if isinstance(value, PackageRoot):
        return {"canonical": value.canonical, "original": value.original}
    if isinstance(value, ModuleCandidate):
        return {
            "file_path": value.file_path,
            "root": _project(value.root),
            "qualified_name": value.qualified_name,
            "name_components": list(value.name_components),
            "file_kind": value.file_kind,
            "directory_kind": value.directory_kind,
            "component_supported": value.component_supported,
            "name_supported": value.name_supported,
            "evidence": list(value.evidence),
        }
    if isinstance(value, Resolution):
        return {
            "record": _project(value.record),
            "requested_module": value.requested_module,
            "requested_components": list(value.requested_components)
            if value.requested_components is not None else None,
            "status": value.status,
            "supported_candidates": [_project(c) for c in value.supported_candidates],
            "unsupported_candidates": [_project(c) for c in value.unsupported_candidates],
            "reason": value.reason,
        }
    if isinstance(value, RelativeResolution):
        return {
            "record": _project(value.record),
            "contexts": [_project(c) for c in value.contexts],
            "issue": value.issue,
        }
    if isinstance(value, ContextResolution):
        return {
            "source_candidate": _project(value.source_candidate),
            "base_components": list(value.base_components),
            "target_components": list(value.target_components)
            if value.target_components is not None else None,
            "lossless": value.lossless,
            "status": value.status,
            "reason": value.reason,
            "supported_candidates": [_project(c) for c in value.supported_candidates],
            "unsupported_candidates": [_project(c) for c in value.unsupported_candidates],
        }
    if isinstance(value, SourceAssessment):
        # Diagnostic text (parse_diagnostic) is intentionally omitted; a
        # boolean presence flag is preserved in its place.
        return {
            "candidate": _project(value.candidate),
            "state": value.state,
            "diagnostic_present": value.parse_diagnostic is not None,
        }
    if isinstance(value, NameEvidence):
        return {
            "position": value.position,
            "name": value.name,
            "asname": value.asname,
            "outcome": value.outcome,
            "reason": value.reason,
            "submodule": [_project(s) for s in value.submodule],
            "definitions": [_project(d) for d in value.definitions],
            "reexports": [_project(r) for r in value.reexports],
        }
    if isinstance(value, SubmoduleNameEvidence):
        return {
            "candidate": _project(value.candidate),
            "submodule_candidate": _project(value.submodule_candidate),
            "component": value.component,
            "component_supported": value.component_supported,
            "name_supported": value.name_supported,
            "source": value.source,
        }
    if isinstance(value, DefinitionNameEvidence):
        return {
            "candidate": _project(value.candidate),
            "kind": value.kind,
            "symbol_kind": value.symbol_kind,
            "name": value.name,
            "line": value.line,
            "provenance": value.provenance,
        }
    if isinstance(value, ReexportNameEvidence):
        return {
            "candidate": _project(value.candidate),
            "kind": value.kind,
            "symbol_kind": value.symbol_kind,
            "name": value.name,
            "asname": value.asname,
            "line": value.line,
            "provenance": value.provenance,
        }
    if isinstance(value, TargetContext):
        return {
            "kind": value.kind,
            "source_candidate": _project(value.source_candidate),
            "base_module": value.base_module,
            "base_components": list(value.base_components)
            if value.base_components is not None else None,
            "target_components": list(value.target_components)
            if value.target_components is not None else None,
            "lossless": value.lossless,
            "status": value.status,
            "reason": value.reason,
            "supported_candidates": [_project(c) for c in value.supported_candidates],
            "unsupported_candidates": [_project(c) for c in value.unsupported_candidates],
            "assessments": [_project(a) for a in value.assessments],
        }
    if isinstance(value, TargetNameAnalysis):
        return {
            "context_index": value.context_index,
            "name_index": value.name_index,
            "context": _project(value.context),
            "name": _project(value.name),
        }
    if isinstance(value, ImportNameAnalysis):
        return {
            "record": _project(value.record),
            "target_kind": value.target_kind,
            "issue": value.issue,
            "contexts": [_project(c) for c in value.contexts],
        }
    if isinstance(value, WildcardCandidateEvidence):
        return {
            "candidate": _project(value.candidate),
            "source_state": value.source_state,
            "all_state": value.all_state,
            "all_entries": list(value.all_entries),
            "star_names": list(value.star_names),
            "default_scope": value.default_scope,
            "outcome": value.outcome,
            "reason": value.reason,
        }
    if isinstance(value, WildcardContextAnalysis):
        return {
            "context_index": value.context_index,
            "context": _project(value.context),
            "candidates": [_project(c) for c in value.candidates],
        }
    if isinstance(value, WildcardAnalysis):
        return {
            "record": _project(value.record),
            "target_kind": value.target_kind,
            "outcome": value.outcome,
            "reason": value.reason,
            "issue": value.issue,
            "contexts": [_project(c) for c in value.contexts],
            "source_assessments": [_project(a) for a in value.source_assessments],
        }

    # TargetSource is a caller-supplied input type only — never in any output.
    if isinstance(value, TargetSource):
        raise TypeError(
            "TargetSource is a caller-supplied input type and cannot appear "
            "in the resolve query wire projection"
        )
    raise TypeError(
        f"cannot serialize {type(value).__name__} in the resolve query wire "
        "projection; the contract fixes the exact recursive field set"
    )


def _project_resolve_query_result(result: ResolveQueryResult) -> dict:
    return {
        "schema_version": result.schema_version,
        "db_path": result.db_path,
        "roots": [_project(r) for r in result.roots],
        "filters": _project(result.filters),
        "limit": result.limit,
        "offset": result.offset,
        "total_count": result.total_count,
        "count": result.count,
        "records": [_project(r) for r in result.records],
    }


def resolve_to_dict(result: ResolveQueryResult) -> dict:
    """Return a fresh, JSON-compatible projection of *result*.

    The projection is pure (no I/O, no globals, no database) and does not
    modify the immutable :class:`ResolveQueryResult`.  Every P4 field is
    emitted, including on empty pages (``records == ()``).  Object keys
    are inserted recursively in lexicographic order (approved P4 canonical
    JSON body); array element order and duplicates are preserved verbatim.
    """
    return _canonicalize_keys(_project_resolve_query_result(result))


def _canonicalize_keys(obj):
    """Recursively insert every object key in lexicographic order (P4).

    Applied to the whole projection so that the canonical JSON body is
    fully determined by the Python result: object keys are ordered
    lexicographically at every nesting level, while arrays preserve their
    element order and duplicates exactly.  This makes
    ``json.dumps(_canonicalized, indent=2, ensure_ascii=True)`` equal to a
    ``sort_keys=True`` serialization without re-sorting the actual CLI body.
    """
    if isinstance(obj, dict):
        return {k: _canonicalize_keys(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_canonicalize_keys(x) for x in obj]
    return obj
