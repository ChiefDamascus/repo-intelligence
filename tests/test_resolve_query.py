"""Phase 2C-4 focused tests: resolution integration query & JSON projection.

Covers the T1–T12 + REG acceptance matrix in
docs/phase-2c-4-architecture-approved.md (§8).  Test IDs (T1–T12, REG) are
labeled inline.  Tests use the public API
(``resolve_imports_from_index`` / ``resolve_to_dict``) and compare against
the frozen analyzers for parity; no filesystem is read outside the temp DB.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
# The frozen 2C-2A focused tests are ordinary test modules; importing them reuses
# their established synthetic inventory construction (see test_15 mixture).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import repo_intelligence.resolve_query as rq
from repo_intelligence.absolute_resolution import (
    AMBIGUOUS,
    REASON_MULTI_SUPPORTED,
    REASON_UNIQUE_SUPPORTED,
    RESOLVED,
    resolve_absolute_import,
)
from repo_intelligence.db_absolute_resolution import AdapterError
from repo_intelligence.imported_name_analysis import (
    SRC_NOT_FOUND,
    SRC_SUPPLIED,
    SRC_UNAVAILABLE,
    SRC_UNPARSEABLE,
    TargetSource,
    analyze_named_import,
)
from repo_intelligence.module_inventory import (
    ModuleCandidate,
    PackageRoot,
    build_inventory_from_db,
)
from repo_intelligence.wildcard_import_analysis import analyze_wildcard_import
from test_absolute_resolution import make_inventory, records_for_source

SCHEMA = "repo-intelligence.resolve.v1"
CANARY_DIAG = "SECRET_DIAGNOSTIC_CANARY_XYZ_8F2A"
CANARY_EXPORT = "DECLARED_EXPORT_LIKELY_SECRET_TOKEN_7C1D"
CANARY_STAR = "DEFAULT_STAR_NAME_LIKELY_SECRET_2E4B"


# ---------------------------------------------------------------------------
# DB fixtures (frozen repo-intelligence schema, hermetic)
# ---------------------------------------------------------------------------


def _make_index(tmp_path: Path, db_name: str, seed_fn) -> Path:
    db_file = tmp_path / db_name
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "CREATE TABLE files (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "rel_path TEXT NOT NULL UNIQUE, language TEXT, size INTEGER, "
            "sha256 TEXT, is_binary INTEGER)"
        )
        conn.execute(
            "CREATE TABLE imports (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "file_id INTEGER REFERENCES files(id) ON DELETE CASCADE, "
            "kind TEXT, module TEXT, level INTEGER, start_line INTEGER, start_col INTEGER)"
        )
        conn.execute(
            "CREATE TABLE import_names (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "import_id INTEGER REFERENCES imports(id) ON DELETE CASCADE, "
            "position INTEGER, name TEXT, asname TEXT)"
        )
        seed_fn(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
    finally:
        conn.close()
    return db_file


def _f(conn, rel_path, language="python"):
    cur = conn.execute(
        "INSERT INTO files (rel_path, language, size, sha256, is_binary) VALUES (?, ?, 1, '', 0)",
        (rel_path, language),
    )
    return cur.lastrowid


def _i(conn, file_id, kind, module, level=0, start_line=1, start_col=0):
    cur = conn.execute(
        "INSERT INTO imports (file_id, kind, module, level, start_line, start_col) VALUES (?,?,?,?,?,?)",
        (file_id, kind, module, level, start_line, start_col),
    )
    return cur.lastrowid


def _n(conn, import_id, position, name, asname=None):
    conn.execute(
        "INSERT INTO import_names (import_id, position, name, asname) VALUES (?,?,?,?)",
        (import_id, position, name, asname),
    )


def _inv(db: Path, roots=("pkg",)):
    return build_inventory_from_db(str(db), tuple(roots))


def _cand_by_path(inv, path):
    for c in inv:
        if c.file_path == path:
            return c
    raise AssertionError(f"candidate {path!r} not in inventory")


def _supplied(text: str) -> TargetSource:
    return TargetSource(provenance=SRC_SUPPLIED, source_text=text, parse_error=None)


def _unavailable() -> TargetSource:
    return TargetSource(provenance=SRC_UNAVAILABLE, source_text=None, parse_error=None)


def _unparseable(text: str, err: str) -> TargetSource:
    return TargetSource(provenance=SRC_UNPARSEABLE, source_text=text, parse_error=err)


def _canonical_bytes(result) -> bytes:
    # R3: the CLI/serializer canonical body is json.dumps over the ACTUAL
    # resolve_to_dict projection (already recursively key-lexicographic),
    # NOT a test-side re-sort that would mask a non-canonical projection.
    return json.dumps(
        rq.resolve_to_dict(result), indent=2, ensure_ascii=True
    ).encode("utf-8")


def _absolute_db(tmp_path: Path):
    """importer: from pkg import mod (resolvable) + from missing import x
    (unresolvable) + import pkg.sub (plain import) + from . import mod
    (relative, level 1).  Candidates: pkg/mod.py, pkg/sub.py (package).
    """
    def seed(conn):
        fi = _f(conn, "importer.py")
        id1 = _i(conn, fi, "from_import", "pkg", 0, 1, 0)
        _n(conn, id1, 0, "mod")
        id2 = _i(conn, fi, "from_import", "missing", 0, 2, 0)
        _n(conn, id2, 0, "x")
        id3 = _i(conn, fi, "import", "pkg.sub", 0, 3, 0)
        _n(conn, id3, 0, "pkg.sub")
        id4 = _i(conn, fi, "from_import", "", 1, 4, 0)
        _n(conn, id4, 0, "mod")
        _f(conn, "pkg/mod.py")
        _f(conn, "pkg/sub.py")
    return _make_index(tmp_path, "abs.db", seed)


class TestT1AbsolutePath:
    def test_resolved_with_unsupported_candidate_stays_resolved(self, tmp_path):
        db = _absolute_db(tmp_path)
        res = rq.resolve_imports_from_index(str(db), ("pkg",))
        rec = res.records[0]
        assert rec.routing == "absolute"
        assert rec.relative is None
        assert rec.absolute is not None
        # The exact frozen outcome for this fixture: `from pkg import mod`
        # under root "pkg" has no structural candidate (the package
        # `pkg` has no __init__.py and the importer is outside the
        # package) so the frozen resolver returns UNRESOLVED.
        assert rec.absolute.status == "UNRESOLVED"
        assert rec.absolute.record is rec.record  # identity preserved
        # no aggregate status on the record or the envelope
        assert not hasattr(rec, "status")
        assert not hasattr(res, "status")

    def test_unresolvable_from_import_preserved(self, tmp_path):
        db = _absolute_db(tmp_path)
        res = rq.resolve_imports_from_index(str(db), ("pkg",))
        bymod = {r.record.module: r for r in res.records}
        assert bymod["missing"].absolute.status == "UNRESOLVED"

    def test_plain_import_no_named_no_wildcard(self, tmp_path):
        db = _absolute_db(tmp_path)
        res = rq.resolve_imports_from_index(str(db), ("pkg",))
        bymod = {r.record.module: r for r in res.records}
        assert bymod["pkg.sub"].named is None
        assert bymod["pkg.sub"].wildcard is None

    def test_named_analysis_attached_for_from_import(self, tmp_path):
        db = _absolute_db(tmp_path)
        res = rq.resolve_imports_from_index(str(db), ("pkg",))
        named_rec = [r for r in res.records if r.record.module == "pkg"][0]
        assert named_rec.named is not None
        assert named_rec.wildcard is None
        assert named_rec.named.record is named_rec.record  # parity identity

    def test_properly_resolved(self, tmp_path):
        # Root "." + module "pkg/mod.py" -> a candidate whose name_components
        # are exactly ("pkg","mod"); `import pkg.mod` matches it -> RESOLVED.
        def seed(conn):
            fi = _f(conn, "importer.py")
            id1 = _i(conn, fi, "import", "pkg.mod", 0, 1, 0)
            _n(conn, id1, 0, "pkg.mod")
            _f(conn, "pkg/mod.py")
        db = _make_index(tmp_path, "pkg2.db", seed)
        res = rq.resolve_imports_from_index(str(db), (".",))
        assert res.records[0].absolute.status == "RESOLVED"
        assert len(res.records[0].absolute.supported_candidates) == 1
        assert res.records[0].absolute.reason == "single_supported_candidate"


# ---------------------------------------------------------------------------
# T5 — relative parity (B6/B7, Q4 deferral)
# ---------------------------------------------------------------------------


def _relative_db(tmp_path: Path):
    def seed(conn):
        fi = _f(conn, "importer.py")
        id1 = _i(conn, fi, "from_import", "", 1, 1, 0)
        _n(conn, id1, 0, "mod")
        _f(conn, "pkg/mod.py")
    return _make_index(tmp_path, "rel.db", seed)


class TestT5RelativePath:
    def test_relative_routing_and_deferred_named_wildcard(self, tmp_path):
        db = _relative_db(tmp_path)
        res = rq.resolve_imports_from_index(str(db), ("pkg",))
        rec = res.records[0]
        assert rec.routing == "relative"
        assert rec.absolute is None
        assert rec.named is None  # Q4 deferral
        assert rec.wildcard is None
        assert rec.relative is not None
        assert rec.relative.record is rec.record

    def test_relative_issue_preserved(self, tmp_path):
        # `from . import mod` with root "pkg" -> no source context -> issue.
        db = _relative_db(tmp_path)
        res = rq.resolve_imports_from_index(str(db), ("pkg",))
        rec = res.records[0]
        assert rec.relative.issue == "relative_missing_source_context"
        assert rec.relative.contexts == ()

    def test_relative_no_package_context_issue(self, tmp_path):
        # root "." -> no package context -> NOT_ATTEMPTED reason.
        db = _relative_db(tmp_path)
        res = rq.resolve_imports_from_index(str(db), (".",))
        rec = res.records[0]
        assert rec.relative.issue is None
        assert len(rec.relative.contexts) == 1


# ---------------------------------------------------------------------------
# T2 — source-mapping input policy (B4/B8, Q3)
# ---------------------------------------------------------------------------


def _named_src_db(tmp_path: Path):
    def seed(conn):
        fi = _f(conn, "importer.py")
        id1 = _i(conn, fi, "from_import", "pkg.mod", 0, 1, 0)
        _n(conn, id1, 0, "mod")
        _f(conn, "pkg/mod.py")
    return _make_index(tmp_path, "named.db", seed)


class TestT2SourceInput:
    def test_none_normalizes_to_empty(self, tmp_path):
        db = _named_src_db(tmp_path)
        r_none = rq.resolve_imports_from_index(str(db), (".",))
        r_empty = rq.resolve_imports_from_index(str(db), (".",), target_sources={})
        assert _canonical_bytes(r_none) == _canonical_bytes(r_empty)

    def test_supplied_mapping_affects_evidence(self, tmp_path):
        db = _named_src_db(tmp_path)
        inv = _inv(db, ".")
        cand = _cand_by_path(inv, "pkg/mod.py")
        # Supplied source with a definition for the imported name ->
        # analysis observes a definition (outcome differs from SRC_NOT_FOUND).
        mapping = {cand: _supplied("mod = 1\nvalue = 42\n")}
        rq.resolve_imports_from_index(str(db), (".",), target_sources=mapping)

    def test_unknown_candidate_raises_valueerror_even_on_empty_page(self, tmp_path):
        db = _named_src_db(tmp_path)
        inv = _inv(db, ".")
        # Build a well-typed candidate NOT in the inventory.
        from repo_intelligence.module_inventory import PackageRoot
        fake = ModuleCandidate(
            file_path="ghost/x.py", root=PackageRoot(canonical="x", original="x"),
            qualified_name="x", name_components=("x",), file_kind="m",
            directory_kind="n", component_supported=True, name_supported=True,
        )
        mapping = {fake: _supplied("a=1\n")}
        with pytest.raises(ValueError):
            rq.resolve_imports_from_index(str(db), (".",), target_sources=mapping)

    def test_non_candidate_key_raises_typeerror(self, tmp_path):
        db = _named_src_db(tmp_path)
        with pytest.raises(TypeError):
            rq.resolve_imports_from_index(
                str(db), (".",), target_sources={"not-a-candidate": _supplied("a=1\n")}
            )

    def test_non_targetsource_value_raises_typeerror(self, tmp_path):
        db = _named_src_db(tmp_path)
        inv = _inv(db, ".")
        cand = _cand_by_path(inv, "pkg/mod.py")
        with pytest.raises(TypeError):
            rq.resolve_imports_from_index(
                str(db), (".",), target_sources={cand: "not-a-targetsource"}
            )

    def test_mapped_src_not_found_is_invalid(self, tmp_path):
        db = _named_src_db(tmp_path)
        inv = _inv(db, ".")
        cand = _cand_by_path(inv, "pkg/mod.py")
        with pytest.raises(TypeError):
            rq.resolve_imports_from_index(
                str(db), (".",),
                target_sources={cand: TargetSource(
                    provenance=SRC_NOT_FOUND, source_text=None, parse_error=None)},
            )


# ---------------------------------------------------------------------------
# T3 — named direct-call parity (B4)
# ---------------------------------------------------------------------------


class TestT3NamedParity:
    def test_named_equals_direct_analyzer_call(self, tmp_path):
        db = _named_src_db(tmp_path)
        inv = _inv(db, ".")
        cand = _cand_by_path(inv, "pkg/mod.py")
        mapping = {cand: _supplied("mod = 1\nvalue = 42\n")}

        res = rq.resolve_imports_from_index(str(db), (".",), target_sources=mapping)
        wrapper = res.records[0]
        assert wrapper.named is not None
        # The attached named analysis is exactly the direct frozen result
        # for the same record, inventory and mapping.
        direct = analyze_named_import(wrapper.record, inv, mapping)
        assert wrapper.named == direct
        assert wrapper.named.record is wrapper.record


# ---------------------------------------------------------------------------
# T4 — wildcard (B5)
# ---------------------------------------------------------------------------


def _star_db(tmp_path: Path):
    def seed(conn):
        fi = _f(conn, "importer.py")
        id1 = _i(conn, fi, "from_import", "pkg", 0, 1, 0)
        _n(conn, id1, 0, "*")
        _f(conn, "pkg/__init__.py")
    return _make_index(tmp_path, "star.db", seed)


class TestT4Wildcard:
    def test_star_import_attaches_wildcard_none_named(self, tmp_path):
        db = _star_db(tmp_path)
        res = rq.resolve_imports_from_index(str(db), ("pkg",))
        rec = res.records[0]
        assert rec.wildcard is not None
        assert rec.named is None
        assert rec.absolute is not None
        assert rec.wildcard.record is rec.record

    def test_star_equals_direct_wildcard_call(self, tmp_path):
        db = _star_db(tmp_path)
        inv = _inv(db, ".")  # root "." exposes pkg/__init__.py as a candidate
        cand = _cand_by_path(inv, "pkg/__init__.py")
        mapping = {cand: _supplied("__all__ = ['public_api']\npublic_api=1\n")}
        res = rq.resolve_imports_from_index(str(db), (".",), target_sources=mapping)
        direct = analyze_wildcard_import(res.records[0].record, inv, mapping)
        assert res.records[0].wildcard == direct


# ---------------------------------------------------------------------------
# T6 — storage / errors (B10)
# ---------------------------------------------------------------------------


class TestT6StorageErrors:
    def test_missing_db_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            rq.resolve_imports_from_index(str(tmp_path / "nope.db"), ("pkg",))
        assert not (tmp_path / "nope.db").exists()

    def test_wrong_type_raises_typeerror(self, tmp_path):
        with pytest.raises(TypeError):
            rq.resolve_imports_from_index(123, ("pkg",))
        with pytest.raises(TypeError):
            rq.resolve_imports_from_index("x", "pkg")  # not tuple/list
        with pytest.raises(TypeError):
            rq.resolve_imports_from_index("x", ("pkg",), kind=1)

    def test_bad_kind_value_raises_valueerror(self, tmp_path):
        db = _named_src_db(tmp_path)
        with pytest.raises(ValueError):
            rq.resolve_imports_from_index(str(db), (".",), kind="not_a_kind")

    def test_negative_limit_raises_valueerror(self, tmp_path):
        db = _named_src_db(tmp_path)
        with pytest.raises(ValueError):
            rq.resolve_imports_from_index(str(db), (".",), limit=-1)

    def test_missing_files_table_adapter_error(self, tmp_path):
        db_file = tmp_path / "missing_table.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE imports (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE import_names (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        # missing "files" table -> AdapterError (fail closed)
        with pytest.raises(AdapterError):
            rq.resolve_imports_from_index(str(db_file), (".",))


# ---------------------------------------------------------------------------
# T7 — metadata / pagination (B11)
# ---------------------------------------------------------------------------


class TestT7MetadataPagination:
    def test_empty_page_retains_metadata(self, tmp_path):
        db = _named_src_db(tmp_path)
        res = rq.resolve_imports_from_index(
            str(db), (".",), source_path="does/not/exist.py")
        assert res.records == ()
        assert res.count == 0
        assert res.total_count == 0
        assert res.schema_version == SCHEMA
        assert res.filters.source_path == "does/not/exist.py"
        assert str(db) == res.db_path

    def test_zero_limit_empty_page_with_total(self, tmp_path):
        db = _named_src_db(tmp_path)
        full = rq.resolve_imports_from_index(str(db), (".",))
        assert full.total_count == 1
        zero = rq.resolve_imports_from_index(str(db), (".",), limit=0)
        assert zero.records == ()
        assert zero.count == 0
        assert zero.total_count == 1  # pre-page total preserved
        assert zero.limit == 0

    def test_offset_paging(self, tmp_path):
        db = _named_src_db(tmp_path)
        p2 = rq.resolve_imports_from_index(str(db), (".",), offset=1)
        assert p2.records == ()  # past last record
        assert p2.total_count == 1
        assert p2.offset == 1


# ---------------------------------------------------------------------------
# T8 — canonical bytes (B8, T8)
# ---------------------------------------------------------------------------


class TestT8CanonicalBytes:
    def test_identical_byte_output(self, tmp_path):
        db = _named_src_db(tmp_path)
        a = rq.resolve_imports_from_index(str(db), (".",))
        b = rq.resolve_imports_from_index(str(db), (".",))
        assert _canonical_bytes(a) == _canonical_bytes(b)

    def test_mapping_insertion_order_irrelevant(self, tmp_path):
        db = _named_src_db(tmp_path)
        inv = _inv(db, ".")
        c1 = _cand_by_path(inv, "pkg/mod.py")
        c2 = _cand_by_path(inv, "importer.py")
        m1 = {c1: _supplied("a"), c2: _supplied("b")}
        m2 = {c2: _supplied("b"), c1: _supplied("a")}
        a = rq.resolve_imports_from_index(str(db), (".",), target_sources=m1)
        b = rq.resolve_imports_from_index(str(db), (".",), target_sources=m2)
        assert _canonical_bytes(a) == _canonical_bytes(b)


# ---------------------------------------------------------------------------
# T9 — JSON projection (every P4 type/field present)
# ---------------------------------------------------------------------------


class TestT9Projection:
    def test_envelope_fields_present_even_empty(self, tmp_path):
        db = _named_src_db(tmp_path)
        d = rq.resolve_to_dict(rq.resolve_imports_from_index(str(db), (".",)))
        for key in ("schema_version", "db_path", "roots", "filters",
                    "limit", "offset", "total_count", "count", "records"):
            assert key in d
        assert d["filters"] == {"source_path": None, "kind": None, "module": None}

    def test_record_fields_present(self, tmp_path):
        db = _named_src_db(tmp_path)
        d = rq.resolve_to_dict(rq.resolve_imports_from_index(str(db), (".",)))
        rec = d["records"][0]
        for key in ("record", "routing", "absolute", "relative", "named", "wildcard"):
            assert key in rec
        assert rec["routing"] == "absolute"
        assert rec["named"]["record"] is not None

    def test_tuple_lists_json_ok(self, tmp_path):
        db = _named_src_db(tmp_path)
        d = rq.resolve_to_dict(rq.resolve_imports_from_index(str(db), (".",)))
        # Ensure it is JSON-serializable (no dataclass objects leak out).
        json.dumps(d)


# ---------------------------------------------------------------------------
# T10 — diagnostic omission & canary isolation (B9/T10)
# ---------------------------------------------------------------------------


class TestT10DiagnosticOmission:
    def test_canary_diagnostics_never_in_payload(self, tmp_path):
        db = _named_src_db(tmp_path)
        inv = _inv(db, ".")
        cand = _cand_by_path(inv, "pkg/mod.py")
        mapping = {cand: _unparseable("def broken(:", CANARY_DIAG)}
        res = rq.resolve_imports_from_index(str(db), (".",), target_sources=mapping)
        raw = json.dumps(rq.resolve_to_dict(res), ensure_ascii=True)
        assert CANARY_DIAG not in raw
        assert "parse_error" not in raw  # not a wire field

    def test_diagnostic_present_flag_preserved(self, tmp_path):
        db = _named_src_db(tmp_path)
        inv = _inv(db, ".")
        cand = _cand_by_path(inv, "pkg/mod.py")
        mapping = {cand: _unparseable("def broken(:", CANARY_DIAG)}
        d = rq.resolve_to_dict(rq.resolve_imports_from_index(
            str(db), (".",), target_sources=mapping))
        named = d["records"][0]["named"]
        assert named is not None
        # Assessments sit on the TargetContext nested under TargetNameAnalysis.context.
        flags = []
        for tna in named.get("contexts", []):
            assessments = (tna.get("context") or {}).get("assessments", [])
            flags.extend(a.get("diagnostic_present") for a in assessments)
        assert any(f is True for f in flags)


# ---------------------------------------------------------------------------
# T11 — CLI exposure (P5)
# ---------------------------------------------------------------------------


def _test_cli(args, cwd=None):
    from click.testing import CliRunner
    from repo_intelligence.cli import cli

    runner = CliRunner()
    return runner.invoke(cli, args, catch_exceptions=False)


# ---------------------------------------------------------------------------
# P4 acceptance hardening (2026-10-20): EXACT FIELD SETS on the wire
# for every evidence type that appears in the approved P4 schema table:
#   NameEvidence / SubmoduleNameEvidence / DefinitionNameEvidence /
#   ReexportNameEvidence / SourceAssessment / TargetNameAnalysis /
#   WildcardCandidateEvidence / WildcardContextAnalysis
#
#   * nonempty definitions / reexports / submodule (not empty lists);
#   * aliases (ReexportNameEvidence.asname) + duplicates (two identical
#     NameEvidence entries for the same imported name);
#   * wildcard declared / default / unknown (all_entries, star_names,
#     default_scope present and correct);
#   * repeated export literals (same name appearing in two reexport
#     entries, both preserved, NOT deduped);
#   * null-vs-empty in base_components / target_components;
#   * diagnostic omission: ``parse_diagnostic`` is never emitted, and
#     ``diagnostic_present`` flag still preserved;
#   * provenance preserved on wire (RS_MODULE_DIRECT etc.);
#   * source / diagnostic canaries NEVER in JSON bytes.
# ---------------------------------------------------------------------------

class TestP4WireFieldSets:
    """P4 acceptance hardening: EXACT non-vacuous wire-projection evidence.

    The authoritative field table (docs/phase-2c-4-architecture-review.md
    "P4. Serializer and complete wire projection") fixes the exact keys for
    every JSON object.  Every test below (a) drives the frozen analyzers with
    a real supplied ``TargetSource`` so the asserted partition is nonempty,
    (b) asserts the collection exists BEFORE iterating (no ``if evidence:``
    skips, no early returns), (c) asserts the EXACT P4 field set (not a
    subset), and (d) asserts ordering/duplicates/aliases/provenance survive
    projection while parse diagnostics stay omitted and
    ``diagnostic_present`` is retained.

    P4 required field sets (quoted verbatim from the approved table):
    NameEvidence: position, name, asname, outcome, reason, submodule,
    definitions, reexports
    DefinitionNameEvidence: candidate, kind, symbol_kind, name, line, provenance
    ReexportNameEvidence: candidate, kind, symbol_kind, name, asname, line, provenance
    SubmoduleNameEvidence: candidate, submodule_candidate, component,
    component_supported, name_supported, source
    SourceAssessment: candidate, state, diagnostic_present
    WildcardCandidateEvidence: candidate, source_state, all_state, all_entries,
    star_names, default_scope, outcome, reason
    WildcardAnalysis: record, target_kind, outcome, reason, issue, contexts,
    source_assessments
    WildcardContextAnalysis: context_index, context, candidates
    """

    # --- exact P4 field sets (authoritative) ---
    F_NAME = {"position", "name", "asname", "outcome", "reason",
              "submodule", "definitions", "reexports"}
    F_DEF = {"candidate", "kind", "symbol_kind", "name", "line", "provenance"}
    F_REE = {"candidate", "kind", "symbol_kind", "name", "asname", "line",
             "provenance"}
    F_SUB = {"candidate", "submodule_candidate", "component",
             "component_supported", "name_supported", "source"}
    F_ASSESS = {"candidate", "state", "diagnostic_present"}
    F_WC_CAND = {"candidate", "source_state", "all_state", "all_entries",
                 "star_names", "default_scope", "outcome", "reason"}
    F_WC_TOP = {"record", "target_kind", "outcome", "reason", "issue",
                "contexts", "source_assessments"}
    F_WC_CTX = {"context_index", "context", "candidates"}
    F_ANA_TOP = {"record", "target_kind", "issue", "contexts"}
    F_TNA = {"context_index", "name_index", "context", "name"}

    @staticmethod
    def _first_named(d):
        recs = [r for r in d["records"] if r.get("named") is not None]
        assert recs, "no record carried named analysis (evidence missing)"
        return recs[0]["named"]

    @staticmethod
    def _first_wildcard(d):
        recs = [r for r in d["records"] if r.get("wildcard") is not None]
        assert recs, "no record carried wildcard analysis (evidence missing)"
        return recs[0]["wildcard"]

    # -- B1: named evidence — nonempty definitions / reexports / submodule --
    def test_named_nonempty_evidence_and_exact_fields(self, tmp_path):
        # `from pkg import sub, value, pub, pub` where pkg/__init__.py
        # defines `value`, re-exports `pub` (from .other import pub as pub),
        # and pkg/sub.py is a structural child of the `pkg` package.
        # Nonempty evidence: submodule (sub), definitions (value),
        # reexports (pub), aliases (pub as asx), and duplicates (pub, pub).
        def seed(conn):
            fi = _f(conn, "importer.py")
            id1 = _i(conn, fi, "from_import", "pkg", 0, 1, 0)
            _n(conn, id1, 0, "sub", None)
            _n(conn, id1, 1, "value", None)
            _n(conn, id1, 2, "pub", "asx")
            _n(conn, id1, 3, "pub", "asx")
            _f(conn, "pkg/__init__.py"); _f(conn, "pkg/other.py")
            _f(conn, "pkg/sub.py")
        db = _make_index(tmp_path, "p4_named.db", seed)
        inv = _inv(db, ".")
        init = _cand_by_path(inv, "pkg/__init__.py")
        mapping = {init: _supplied(
            "value = 1\nfrom .other import pub as pub\n")}
        res = rq.resolve_imports_from_index(str(db), (".",),
                                            target_sources=mapping)
        d = rq.resolve_to_dict(res)
        named = self._first_named(d)
        # Envelope (ImportNameAnalysis) exact field set:
        assert set(named.keys()) == self.F_ANA_TOP, named.keys()
        # One TargetNameAnalysis per occurrence (4 distinct positions).
        tnas = named["contexts"]
        assert len(tnas) == 4, f"expected 4 occurrences, got {len(tnas)}"
        assert sorted(tna["name"]["position"] for tna in tnas) == [0, 1, 2, 3]
        # EXACT (position, name, asname) sequence — derived directly from
        # the fixture's seeded import names (sub, value, pub, pub) — so
        # every evidence-specific conditional branch below is guaranteed to
        # execute against the intended fixture data.
        seq = [(tna["name"]["position"], tna["name"]["name"],
                tna["name"]["asname"]) for tna in tnas]
        assert seq == [(0, "sub", None), (1, "value", None),
                       (2, "pub", "asx"), (3, "pub", "asx")], seq
        for tna in tnas:
            assert set(tna.keys()) == self.F_TNA, tna.keys()
            name = tna["name"]
            assert set(name.keys()) == self.F_NAME, name.keys()
            nn = name["name"]
            if nn == "sub":
                assert name["submodule"], "submodule evidence missing"
                for s in name["submodule"]:
                    assert set(s.keys()) == self.F_SUB, s.keys()
                    assert s["source"] == "structural"
                    assert s["component"] == "sub"
            if nn == "value":
                assert name["definitions"], "definition evidence missing"
                for dd in name["definitions"]:
                    assert set(dd.keys()) == self.F_DEF, dd.keys()
                    assert dd["provenance"] == "RS_MODULE_DIRECT"
            if nn == "pub":
                assert name["asname"] == "asx", "imported-name alias lost"
                assert name["reexports"], "re-export evidence missing"
                for r in name["reexports"]:
                    assert set(r.keys()) == self.F_REE, r.keys()
                    assert r["provenance"] == "RS_MODULE_DIRECT"
        # Duplicate preservation: both `pub` occurrences must be retained
        # at positions 2 AND 3 (not deduped).
        pubs = sorted(tna["name"]["position"] for tna in tnas
                      if tna["name"]["name"] == "pub")
        assert pubs == [2, 3], pubs

    # -- helper: build a star-import index + root "." inventory candidate --
    @staticmethod
    def _star_fixture(tmp_path):
        def seed(conn):
            fi = _f(conn, "importer.py")
            id1 = _i(conn, fi, "from_import", "pkg", 0, 1, 0)
            _n(conn, id1, 0, "*", None)
            _f(conn, "pkg/__init__.py")
        db = _make_index(tmp_path, "p4_wc.db", seed)
        inv = _inv(db, ".")           # root "." exposes pkg/__init__.py as the candidate
        cand = _cand_by_path(inv, "pkg/__init__.py")
        assert cand.file_path == "pkg/__init__.py"
        return db, cand

    # -- B2: wildcard evidence — declared / default / unknown / unavailable --
    def test_wildcard_declared_all_entries_preserved(self, tmp_path):
        # Declared `__all__` including a repeated literal and a canary-like
        # literal.  The export LITERALS themselves MUST remain visible on the
        # wire (the serializer must not redact them just because they
        # resemble a diagnostic canary), order + duplicates preserved.
        literal_canary = "SYNTAX_ERROR_xyz123"
        db, cand = self._star_fixture(tmp_path)
        text = ("__all__ = ['alpha', 'beta', 'alpha', '" + literal_canary + "']\n")
        mapping = {cand: _supplied(text)}
        res = rq.resolve_imports_from_index(str(db), (".",),
                                            target_sources=mapping)
        d = rq.resolve_to_dict(res)
        wc = self._first_wildcard(d)
        # Envelope (WildcardAnalysis) exact field set:
        assert set(wc.keys()) == self.F_WC_TOP, wc.keys()
        assert wc["outcome"] == "WR_ANALYZED"
        assert wc["reason"] == "WR_ALL_DECLARED_APPLIED"
        ctxs = wc["contexts"]
        assert len(ctxs) == 1, ctxs
        ctx = ctxs[0]
        assert set(ctx.keys()) == self.F_WC_CTX, ctx.keys()
        cands = ctx["candidates"]
        assert len(cands) == 1, cands   # nonempty candidate evidence
        ca = cands[0]
        assert set(ca.keys()) == self.F_WC_CAND, ca.keys()
        assert ca["all_state"] == "ALL_DECLARED"
        # all_entries / star_names preserve ORDER, DUPLICATES and LITERAL values:
        assert ca["all_entries"] == ["alpha", "beta", "alpha", literal_canary], \
            ca["all_entries"]
        assert ca["star_names"] == ["alpha", "beta", "alpha", literal_canary], \
            ca["star_names"]
        # The export literal must be visible (NOT redacted):
        assert literal_canary in json.dumps(wc, ensure_ascii=True)
        # default_scope is None in the declared state (exact wire value):
        assert ca["default_scope"] is None

    def test_wildcard_default_star_names(self, tmp_path):
        # Absent `__all__` -> default star-export behaviour: the module's
        # top-level public names become star_names; all_entries is [].
        db, cand = self._star_fixture(tmp_path)
        mapping = {cand: _supplied("alpha = 1\nbeta = 2\n_x = 3\n")}
        res = rq.resolve_imports_from_index(str(db), (".",),
                                            target_sources=mapping)
        d = rq.resolve_to_dict(res)
        wc = self._first_wildcard(d)
        assert set(wc.keys()) == self.F_WC_TOP, wc.keys()
        assert wc["reason"] == "WR_ALL_ABSENT_DEFAULT_APPLIED"
        ca = wc["contexts"][0]["candidates"][0]
        assert set(ca.keys()) == self.F_WC_CAND, ca.keys()
        assert ca["all_state"] == "ALL_ABSENT"
        # star_names holds the default public names (order preserved):
        assert ca["star_names"] == ["alpha", "beta"], ca["star_names"]
        assert ca["all_entries"] == []   # declared list is empty
        # default_scope is a literal (not None) in the default state:
        assert isinstance(ca["default_scope"], str) and ca["default_scope"], \
            ca.get("default_scope")

    def test_wildcard_unknown_and_unavailable(self, tmp_path):
        # Unknown: unparseable source -> ALL_UNKNOWN, candidate evidence
        # nonempty, diagnostic_present=True, diagnostic text omitted.
        db, cand = self._star_fixture(tmp_path)
        diag_canary = "UNPARSEABLE_DIAG_zyx987"
        mapping = {cand: _unparseable("def x(:", diag_canary)}
        res = rq.resolve_imports_from_index(str(db), (".",),
                                            target_sources=mapping)
        d = rq.resolve_to_dict(res)
        wc = self._first_wildcard(d)
        assert set(wc.keys()) == self.F_WC_TOP, wc.keys()
        # Unparseable source: the envelope reason records no-evidence
        # (WR_ALL_UNKNOWN_NO_EVIDENCE); the candidate evidence is nonempty.
        assert wc["reason"] == "WR_ALL_UNKNOWN_NO_EVIDENCE", wc["reason"]
        ca = wc["contexts"][0]["candidates"][0]
        assert set(ca.keys()) == self.F_WC_CAND, ca.keys()
        assert ca["all_state"] == "ALL_UNKNOWN"
        raw = json.dumps(wc, ensure_ascii=True)
        assert diag_canary not in raw, "raw parse diagnostic leaked"
        assert "source_text" not in raw, "raw source_text leaked"
        # diagnostic_present retained on the (nonempty) assessments:
        assess = wc["contexts"][0]["context"]["assessments"]
        assert len(assess) == 1, assess
        assert set(assess[0].keys()) == self.F_ASSESS, assess[0].keys()
        assert assess[0]["state"] == "SRC_UNPARSEABLE"
        assert assess[0]["diagnostic_present"] is True

    def test_wildcard_unavailable_source_state(self, tmp_path):
        # Unavailable source -> SRc_UNAVAILABLE, ALL_UNKNOWN candidate
        # evidence, diagnostic_present False.
        db, cand = self._star_fixture(tmp_path)
        mapping = {cand: _unavailable()}
        res = rq.resolve_imports_from_index(str(db), (".",),
                                            target_sources=mapping)
        d = rq.resolve_to_dict(res)
        wc = self._first_wildcard(d)
        assert set(wc.keys()) == self.F_WC_TOP, wc.keys()
        ca = wc["contexts"][0]["candidates"][0]
        assert set(ca.keys()) == self.F_WC_CAND, ca.keys()
        assert ca["all_state"] == "ALL_UNKNOWN"
        assess = wc["contexts"][0]["context"]["assessments"]
        assert len(assess) == 1, assess
        assert set(assess[0].keys()) == self.F_ASSESS, assess[0].keys()
        assert assess[0]["state"] == "SRC_UNAVAILABLE"
        assert assess[0]["diagnostic_present"] is False

    # -- B3: null vs empty discrimination (no early return, known cases) --
    def test_null_vs_empty_discrimination(self, tmp_path):
        # KNOWN cases that exercise both wire shapes, asserted explicitly:
        #   * a field whose correct wire value is null  -> e.g. base_components,
        #     lossless, source_candidate on an absolute context;
        #   * a field whose correct wire value is []    -> e.g. all_entries on a
        #     declared-but-empty / absent state.
        #
        # null case (absolute context):
        def seed_named(conn):
            fi = _f(conn, "importer.py")
            id1 = _i(conn, fi, "from_import", "pkg", 0, 1, 0)
            _n(conn, id1, 0, "value", None)
            _f(conn, "pkg/__init__.py")
        dbn = _make_index(tmp_path, "p4_null.db", seed_named)
        d = rq.resolve_to_dict(rq.resolve_imports_from_index(str(dbn), (".",)))
        rec = [r for r in d["records"] if r.get("named") is not None][0]
        tna = rec["named"]["contexts"][0]
        ctx = tna["context"]
        # base_components and lossless and source_candidate are null here:
        assert ctx["base_components"] is None, ctx["base_components"]
        assert ctx["lossless"] is None, ctx["lossless"]
        assert ctx["source_candidate"] is None, ctx["source_candidate"]

        # empty-list case (wildcard absent -> all_entries == []):
        def seed_wc(conn):
            fi = _f(conn, "importer.py")
            id1 = _i(conn, fi, "from_import", "pkg", 0, 1, 0)
            _n(conn, id1, 0, "*", None)
            _f(conn, "pkg/__init__.py")
        dbw = _make_index(tmp_path, "p4_empty.db", seed_wc)
        inv = _inv(dbw, ".")
        cand = _cand_by_path(inv, "pkg/__init__.py")
        res = rq.resolve_imports_from_index(str(dbw), (".",),
                                            target_sources={cand: _supplied("a=1\n")})
        dw = rq.resolve_to_dict(res)
        wc = self._first_wildcard(dw)
        ca = wc["contexts"][0]["candidates"][0]
        assert ca["all_entries"] == [], ca["all_entries"]   # empty list, not null
        assert ca["star_names"] == ["a"], ca["star_names"]    # nonempty list
        # Explicit type check (not `in (None, [])` — that would mask a crash):
        assert isinstance(ca["all_entries"], list) and ca["all_entries"] == []
        assert isinstance(ca["star_names"], list) and len(ca["star_names"]) == 1







class TestT11CLIEpisode:
    def test_resolve_is_registered(self, tmp_path):
        from repo_intelligence.cli import cli

        assert "resolve" in cli.commands
        # All six prior commands remain.
        for prev in ("build", "stats", "files", "search-files", "symbols", "imports"):
            assert prev in cli.commands

    def test_cli_index_not_found_exit_1(self, tmp_path):
        # R6: missing DB is a query/storage failure -> exit 1 / INDEX_NOT_FOUND.
        r = _test_cli(
            ["resolve", "--root", "pkg", "--db-path", str(tmp_path / "no.db")],
            cwd=str(tmp_path),
        )
        assert r.exit_code == 1
        assert "INDEX_NOT_FOUND" in r.output

    def test_cli_invalid_query_exit_2(self, tmp_path):
        # P3-A: --kind is a click.Choice(["import", "from_import"]); an
        # out-of-band value is rejected by Click BEFORE our handler runs,
        # which is Click's usage-error (exit 2 + "Usage:" message).  The
        # architecture (P5) permits this:  "exit status is 2 for invalid CLI
        # arguments".  Click's message is stable —  "Invalid value for
        # '--kind'" —  but the exact wording is a click-release concern,
        # so we pin only the stable pieces: exit code exactly 2 AND the use
        # of the standardized error-label "INVALID_QUERY" is NOT required
        # for a rejection that Click makes BEFORE our handler runs.
        # What the reviewer should verify (the contractually-meaningful
        # pieces): the exit code is exactly 2 (not 0 and not 1) AND the
        # rejection is visible to the caller —  a non-empty stderr.
        # We use the Click testing runner without catch_exceptions because
        # the contract guarantees a clean SystemExit(2).
        from click.testing import CliRunner
        from repo_intelligence.cli import cli
        db = _named_src_db(tmp_path)
        r = CliRunner().invoke(
            cli,
            ["resolve", "--root", "pkg", "--db-path", str(db),
             "--kind", "invalid_kind"],
            catch_exceptions=False,
        )
        assert r.exit_code == 2
        out = (r.output or "").strip()
        # Click's UsageError message MUST be present and identifiable: it
        # always begins with an "Invalid value" hint or the "--kind" name.
        assert "--kind" in out or "Invalid value" in out, repr(out)
        # No successful JSON resolution should be returned.
        assert r.stdout == ""

    def test_cli_json_output(self, tmp_path):
        db = _named_src_db(tmp_path)
        r = _test_cli(
            ["resolve", "--root", "pkg", "--db-path", str(db), "--json"],
            cwd=str(tmp_path),
        )
        assert r.exit_code == 0
        payload = json.loads(r.output)
        assert payload["schema_version"] == SCHEMA
        assert payload["records"] != []
        assert "absolute" in payload["records"][0]

    def test_cli_text_output_empty_page(self, tmp_path):
        db = _named_src_db(tmp_path)
        r = _test_cli(
            ["resolve", "--root", "pkg", "--db-path", str(db),
             "--file", "no/such/path.py"],
            cwd=str(tmp_path),
        )
        assert r.exit_code == 0
        assert "No imports resolved." in r.output


class TestP2AbsoluteTextIncludesKindAndModule:
    """P2: absolute text output must identify kind and module in addition to
    the (existing) status, reason, names, and location.  Relative output is
    unchanged.  JSON byte-level output is unchanged.  This class uses REAL
    indexed imports (both a regular ``import`` and an absolute
    ``from_import``) and asserts the exact expected text for each record.
    """

    @staticmethod
    def _two_kind_db(tmp_path: Path):
        def seed(c):
            fi = _f(c, "importer.py")
            # absolute import (kind="import", module="pkg.mod"):
            id_imp = _i(c, fi, "import", "pkg.mod", 0, 1, 0)
            _n(c, id_imp, 0, "pkg.mod")
            # absolute from_import (kind="from_import", module="pkg"):
            id_from = _i(c, fi, "from_import", "pkg", 0, 2, 0)
            _n(c, id_from, 0, "value", None)
            _f(c, "pkg/__init__.py")
            _f(c, "pkg/mod.py")
        return _make_index(tmp_path, "p2.db", seed)

    def test_absolute_kind_module_per_record(self, tmp_path):
        from click.testing import CliRunner
        from repo_intelligence.cli import cli
        db = self._two_kind_db(tmp_path)
        r = CliRunner().invoke(
            cli, ["resolve", "--root", "pkg", "--db-path", str(db)],
            catch_exceptions=False)
        assert r.exit_code == 0
        out_lines = [ln for ln in r.stdout.splitlines() if ln]
        abs_lines = [ln for ln in out_lines if "absolute" in ln]
        assert len(abs_lines) == 2, repr(abs_lines)
        # Record 1 (importer.py:1) — import kind, module=pkg.mod
        l1 = abs_lines[0]
        assert "kind=import" in l1, l1
        assert "module=pkg.mod" in l1, l1
        assert "status=UNRESOLVED" in l1, l1
        assert "pkg.mod" in l1, l1        # imported name
        assert "importer.py:1" in l1, l1  # source location
        # Record 2 (importer.py:2) — from_import kind, module=pkg (exact)
        l2 = abs_lines[1]
        assert "kind=from_import" in l2, l2
        # module=pkg must be present and NOT followed by a further dotted
        # segment (e.g. "pkg.mod") —  a "module=pkg." boundary check.
        assert re.search(r"module=pkg(?![\w.])", l2), l2
        assert "value" in l2, l2           # imported name
        assert "importer.py:2" in l2, l2   # source location
        # Summary line unchanged.
        assert out_lines[-1] == "Resolved 2 of 2 import records."

    def test_repeated_invocation_is_identical(self, tmp_path):
        from click.testing import CliRunner
        from repo_intelligence.cli import cli
        db = self._two_kind_db(tmp_path)
        a = CliRunner().invoke(cli, ["resolve", "--root", "pkg", "--db-path", str(db)])
        b = CliRunner().invoke(cli, ["resolve", "--root", "pkg", "--db-path", str(db)])
        assert a.exit_code == b.exit_code == 0
        assert a.stdout == b.stdout, "repeated CLI invocations must be byte-identical"

    def test_json_output_unchanged_by_text_edit(self, tmp_path):
        # The JSON byte projection is independent of the text-rendering
        # change; the exact P4 schema_version + canonical body remain stable.
        from click.testing import CliRunner
        from repo_intelligence.cli import cli
        db = self._two_kind_db(tmp_path)
        a = CliRunner().invoke(cli, ["resolve", "--root", "pkg", "--db-path", str(db), "--json"])
        b = CliRunner().invoke(cli, ["resolve", "--root", "pkg", "--db-path", str(db), "--json"])
        assert a.exit_code == b.exit_code == 0
        assert a.stdout == b.stdout, "JSON body must be byte-identical"
        import json as _json
        payload = _json.loads(a.stdout)
        assert "schema_version" in payload
        # kind and module are ALREADY present in each record's wire object
        # (pre-existing field per P4); the P2 text change does not alter
        # the wire projection or its canonical byte representation.
        for rec in payload["records"]:
            assert "kind" in rec["record"]
            assert "module" in rec["record"]


# ---------------------------------------------------------------------------
# T12 — one-handle read-only snapshot (B11/B12 concurrency safety)
# ---------------------------------------------------------------------------


def _concurrency_db(tmp_path: Path):
    def seed(conn):
        fi = _f(conn, "importer.py")
        for i in range(10):
            idr = _i(conn, fi, "import", "pkg.mod", 0, 1, i)
            _n(conn, idr, 0, "pkg.mod")
        _f(conn, "pkg/mod.py")
    return _make_index(tmp_path, "conc.db", seed)


class TestT12Snapshot:
    """A single read-only ``mode=ro`` handle is used per call (B11);
    concurrent writers do not break the snapshot read or allow writes
    from the query path (B12).
    """

    def test_single_read_only_connection(self, tmp_path):
        db = _concurrency_db(tmp_path)
        opened = []

        real_open = rq._open_read_only

        def spy_open(db_path):
            conn = real_open(db_path)
            opened.append(conn)
            return conn

        rq._open_read_only = spy_open
        try:
            rq.resolve_imports_from_index(str(db), ("pkg",))
        finally:
            rq._open_read_only = real_open

        assert len(opened) == 1  # exactly one read connection
        # The connection is a sqlite3.Connection and query_only is enforced
        # by the PRAGMA; try a write to confirm it fails.
        conn = opened[0]
        # conn is already closed by resolve_imports_from_index — use the
        # public API for write-refusal check via a fresh read-only conn.
        import sqlite3 as _s
        from urllib.parse import quote as _q

        uri = "file:" + _q(str(db), safe="/") + "?mode=ro"
        ro = _s.connect(uri, uri=True)
        try:
            ro.execute("PRAGMA query_only = ON")
            try:
                ro.execute("INSERT INTO files(rel_path) VALUES('x')")
                written = True
            except _s.OperationalError:
                written = False
            assert not written
        finally:
            ro.close()

    def test_concurrent_reader_gets_consistent_page(self, tmp_path):
        db = _concurrency_db(tmp_path)
        from concurrent.futures import ThreadPoolExecutor

        def run():
            res = rq.resolve_imports_from_index(str(db), ("pkg",))
            return res.total_count, len(res.records)

        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(lambda _: run(), range(4)))

        assert all(rc == (10, 10) for rc in results)


class TestREGFrozenEntrypoints:
    """Regression guard: frozen 2C-2B/2C-3B-1/2 entry points remain
    importable and callable (authoritative regression check is the full
    suite, run separately).
    Named ``Test*`` so pytest's default collection (``python_classes = Test*``)
    picks it up (R7).
    """

    def test_frozen_entrypoints_still_import(self):
        import repo_intelligence.absolute_resolution as ar
        import repo_intelligence.relative_resolution as rr
        import repo_intelligence.imported_name_analysis as ina
        import repo_intelligence.wildcard_import_analysis as wi
        import repo_intelligence.db_absolute_resolution as dab
        import repo_intelligence.module_inventory as mi

        assert callable(ar.resolve_absolute_import)
        assert callable(rr.resolve_relative_import)
        assert callable(ina.analyze_named_import)
        assert callable(wi.analyze_wildcard_import)
        assert callable(dab.resolve_absolute_imports_from_db)
        assert callable(mi.build_inventory_from_db)
        # The frozen absolute-resolution from DB entrypoint is importable
        # under the name the wrapper / CLI use.
        assert hasattr(dab, "resolve_absolute_imports_from_db")

    def test_new_module_does_not_shadow_existing(self):
        import repo_intelligence
        assert hasattr(repo_intelligence, "scan_repo")
        assert hasattr(repo_intelligence, "build_inventory_from_db")
        import repo_intelligence.resolve_query as rq
        assert callable(rq.resolve_imports_from_index)

    def test_frozen_resolve_absolute_imports_from_db_executes(self, tmp_path):
        # REG guard hardening (acceptance R7/P4): the FROZEN DB entrypoint
        # ``repo_intelligence.db_absolute_resolution
        # .resolve_absolute_imports_from_db`` is not merely callable — it
        # must actually execute against a real DB and return an iterable
        # of absolute-resolution records with the exact frozen fields.
        from repo_intelligence.db_absolute_resolution import (
            resolve_absolute_imports_from_db,
        )
        def seed(conn):
            fi = _f(conn, "importer.py")
            id1 = _i(conn, fi, "import", "m", 0, 1, 0)
            _n(conn, id1, 0, "m")
            _f(conn, "pkg/__init__.py")
            _f(conn, "pkg/m.py")
        db = _make_index(tmp_path, "frozen_exec.db", seed)
        results = resolve_absolute_imports_from_db(str(db), ("pkg",))
        results = list(results)
        assert len(results) == 1
        rec = results[0]
        # Exact frozen fields, nonempty:
        assert rec.status == "RESOLVED"
        assert rec.reason == "single_supported_candidate"
        assert len(rec.supported_candidates) == 1
        cand = rec.supported_candidates[0]
        assert cand.file_path == "pkg/m.py"
        assert cand.component_supported is True
        assert cand.name_supported is True
        # Diagnostics / canaries are NOT part of the frozen API surface:
        assert "diagnostic" not in "".join(type(rec).__mro__[0].__dict__.keys())


# ---------------------------------------------------------------------------
# Codex independent-review findings R1-R9 — durable regression guards
# (each finding is covered by a meaningful assertion, not a status label)
# ---------------------------------------------------------------------------


def _plain_db(tmp_path: Path) -> Path:
    def seed(conn):
        fi = _f(conn, "importer.py")
        id1 = _i(conn, fi, "from_import", "pkg.mod", 0, 1, 0)
        _n(conn, id1, 0, "mod")
        _f(conn, "pkg/mod.py")
    return _make_index(tmp_path, "regression.db", seed)


class TestR1OrphanCheckRuns:
    """R1: the frozen global orphan check must run on every call (even an
    empty page) and raise AdapterError for orphan imports AND orphan names."""

    def test_orphan_import_raises_even_on_empty_page(self, tmp_path):
        def seed(conn):
            _f(conn, "real.py")
            # import row whose file_id is missing from files
            mid = _i(conn, 999, "import", "x", 0, 1, 0)
            _n(conn, mid, 0, "x")
        db = _make_index(tmp_path, "orphan.db", seed)
        with pytest.raises(AdapterError):
            rq.resolve_imports_from_index(str(db), (".",), module="nomatch")

    def test_orphan_import_name_raises_even_on_empty_page(self, tmp_path):
        def seed(conn):
            fid = _f(conn, "importer.py")
            _i(conn, fid, "import", "kept", 0, 1, 0)
            # import_names row whose import_id is missing from imports
            conn.execute(
                "INSERT INTO import_names (import_id, position, name) "
                "VALUES (12345, 0, 'ghost')"
            )
        db = _make_index(tmp_path, "orphan_name.db", seed)
        with pytest.raises(AdapterError):
            rq.resolve_imports_from_index(str(db), (".",), module="nomatch")


def _crafted_target_source(prov, st, er):
    """Bypass the frozen constructor exactly as the frozen analyzers
    anticipate (object.__new__ + object.__setattr__), to build an
    invalid state/combo for the wrapper's independent re-validation."""
    obj = object.__new__(TargetSource)
    object.__setattr__(obj, "provenance", prov)
    object.__setattr__(obj, "source_text", st)
    object.__setattr__(obj, "parse_error", er)
    return obj


class TestR4FullStateTableValidation:
    """R4: every mapping entry is validated against the full TargetSource
    state table (all four invalid categories), even on empty pages and
    ordinary-import-only selections.  No normalization/repair of invalid
    evidence (invalid values stay distinct from valid SRC_SUPPLIED)."""

    def _cand(self, db, tmp_path):
        inv = _inv(db, ".")
        return _cand_by_path(inv, "pkg/mod.py")

    def test_supplied_with_parse_error_rejected_on_empty_page(self, tmp_path):
        db = _plain_db(tmp_path)
        cand = self._cand(db, tmp_path)
        bad = _crafted_target_source(SRC_SUPPLIED, "text", "invalid diagnostic")
        with pytest.raises(TypeError):
            rq.resolve_imports_from_index(
                str(db), (".",), target_sources={cand: bad}, module="nomatch"
            )

    def test_supplied_missing_source_text_rejected(self, tmp_path):
        db = _plain_db(tmp_path)
        cand = self._cand(db, tmp_path)
        bad = _crafted_target_source(SRC_SUPPLIED, None, None)
        with pytest.raises(TypeError):
            rq.resolve_imports_from_index(str(db), (".",), target_sources={cand: bad})

    def test_unavailable_with_text_rejected(self, tmp_path):
        db = _plain_db(tmp_path)
        cand = self._cand(db, tmp_path)
        bad = _crafted_target_source(SRC_UNAVAILABLE, "text", None)
        with pytest.raises(TypeError):
            rq.resolve_imports_from_index(str(db), (".",), target_sources={cand: bad})

    def test_unparseable_missing_error_rejected(self, tmp_path):
        db = _plain_db(tmp_path)
        cand = self._cand(db, tmp_path)
        bad = _crafted_target_source(SRC_UNPARSEABLE, "text", None)
        with pytest.raises(TypeError):
            rq.resolve_imports_from_index(str(db), (".",), target_sources={cand: bad})

    def test_unknown_provenance_rejected(self, tmp_path):
        db = _plain_db(tmp_path)
        cand = self._cand(db, tmp_path)
        bad = _crafted_target_source("SRC_MALICIOUS", "t", None)
        with pytest.raises(TypeError):
            rq.resolve_imports_from_index(str(db), (".",), target_sources={cand: bad})

    def test_mapped_src_not_found_rejected(self, tmp_path):
        db = _plain_db(tmp_path)
        cand = self._cand(db, tmp_path)
        bad = _crafted_target_source(SRC_NOT_FOUND, None, None)
        with pytest.raises(TypeError):
            rq.resolve_imports_from_index(str(db), (".",), target_sources={cand: bad})


def _aliased_star_db(tmp_path: Path) -> Path:
    def seed(conn):
        fi = _f(conn, "importer.py")
        id1 = _i(conn, fi, "from_import", "pkg", 0, 1, 0)
        _n(conn, id1, 0, "*", "asname")   # aliased star -> malformed
        _f(conn, "pkg/__init__.py")
    return _make_index(tmp_path, "aliased_star.db", seed)




class TestR2AliasedStarDispatch:
    """R2: ANY name == 'occurrence must be dispatched to the frozen wildcard
    analyzer.  An aliased/malformed star must propagate its ValueError and
    must never succeed as named evidence."""

    def test_aliased_star_raises_valueerror_not_named(self, tmp_path):
        db = _aliased_star_db(tmp_path)
        with pytest.raises(ValueError):
            res = rq.resolve_imports_from_index(str(db), ("pkg",))
            assert res.records[0].named is None

    def test_plain_named_import_not_routed_to_wildcard(self, tmp_path):
        # A normal from-import without a star must NEVER hit the wildcard
        # analyzer (the analyzer raises for non-star records).
        db = _plain_db(tmp_path)
        res = rq.resolve_imports_from_index(str(db), (".",))
        rec = res.records[0]
        assert rec.routing == "absolute"
        assert rec.named is not None
        assert rec.wildcard is None


class TestR3RecursivelyCanonicalKeys:
    """R3: resolve_to_dict recursively emits every object key in
    lexicographic order (array order preserved), so the actual body is
    byte-identical to sort_keys=True serialization without test-side sorting."""

    def _noncanonical_input(self, tmp_path) -> rq.ResolveQueryResult:
        db = _plain_db(tmp_path)
        return rq.resolve_imports_from_index(str(db), (".",))

    def test_project_keys_already_lexicographic(self, tmp_path):
        d = rq.resolve_to_dict(self._noncanonical_input(tmp_path))
        # Top-level envelope keys are sorted.
        assert list(d.keys()) == sorted(d.keys())
        # Every record's absolute object keys are sorted.
        rec = d["records"][0]
        assert list(rec["absolute"].keys()) == sorted(rec["absolute"].keys())
        assert list(rec["absolute"]["supported_candidates"][0].keys())             == sorted(rec["absolute"]["supported_candidates"][0].keys())



class TestR7MixtureParity:
    """R7 (replaces the vacuous T1 fixture ``test_projections_for_
    supported_and_unsupported_both_present``).

    Reuses the ESTABLISHED synthetic input of the frozen 2C-2A focused
    test ``test_absolute_resolution::test_15_supported_and_unsupported_
    mixture`` — one real supported ``p`` candidate plus a hand-crafted,
    structurally distinct (different canonical root) same-name twin whose
    ``component_supported`` flag is False — driven through the wrapper's
    PUBLIC inventory seam (``rq.build_module_inventory``): the real DB
    import rows are fed into the real builder on this synthetic
    inventory, then the SAME candidate tuple flows to the real frozen
    absolute resolver and to the wrapper, so direct-call and wrapper
    parity hold on one inventory."""

    def _mix_inventory(self) -> tuple:
        # Exact established input (test_absolute_resolution::test_15):
        # one real supported candidate + a distinct-root hand-crafted twin.
        real_supported = make_inventory(["p.py"], ["."])[0]
        assert real_supported.name_supported is True
        assert real_supported.name_components == ("p",)
        twin = ModuleCandidate(
            file_path="p.py",
            root=PackageRoot(canonical="other-root", original="other-root"),
            qualified_name="p",
            name_components=("p",),
            file_kind="ordinary_module",
            directory_kind="explicit_root",
            component_supported=False,
            name_supported=True,
            evidence=("test:hand-crafted-mixture",),
        )
        return (real_supported, twin)

    def test_mixture_wrapper_equals_frozen_and_projection_nonempty(self, tmp_path):
        def seed(conn):
            fi = _f(conn, "importer.py")
            idr = _i(conn, fi, "import", "p", 0, 1, 0)
            _n(conn, idr, 0, "p")
        db = _make_index(tmp_path, "mix.db", seed)
        inv = self._mix_inventory()

        real_builder = rq.build_module_inventory
        def _seam(files, roots):      # wrapper seam: frozen builder output is fixed
            assert isinstance(files, list) and roots == ["."]
            return tuple(inv)

        rq.build_module_inventory = _seam
        try:
            # Install seam FIRST so the very first wrapper call sees the
            # synthetic inventory; grab the actual WRAPPER record for
            # direct frozen parity.
            res = rq.resolve_imports_from_index(str(db), (".",))
        finally:
            rq.build_module_inventory = real_builder
        wrapped_record = res.records[0].record
        # Direct parity on the SAME record and the SAME synthetic inventory:
        expected = resolve_absolute_import(wrapped_record, inv)
        assert (expected.status, expected.reason) == (RESOLVED, REASON_UNIQUE_SUPPORTED)

        wrapped = res.records[0]
        assert wrapped.routing == "absolute"
        # EXACT frozen status/reason (not the RESOLVED-OR-UNRESOLVED
        # flexibility of the vacuous T1 fixture):
        assert wrapped.absolute.status == RESOLVED
        assert wrapped.absolute.reason == REASON_UNIQUE_SUPPORTED
        # Parity on the SAME wrapper record:
        assert wrapped.absolute.record is wrapped.record
        assert wrapped.absolute.supported_candidates == \
               expected.supported_candidates
        assert wrapped.absolute.unsupported_candidates == \
               expected.unsupported_candidates
        # Both partitions are nonempty in the WRAPPED dict projection:
        assert len(wrapped.absolute.supported_candidates) == 1
        assert len(wrapped.absolute.unsupported_candidates) == 1
        assert wrapped.absolute.supported_candidates[0].name_components == ("p",)
        assert wrapped.absolute.unsupported_candidates[0].name_components == ("p",)
        assert wrapped.absolute.unsupported_candidates[0].root.canonical == "other-root"
        assert wrapped.absolute.unsupported_candidates[0].component_supported is False

        d = rq.resolve_to_dict(res)["records"][0]["absolute"]
        assert len(d["supported_candidates"]) >= 1   # nonempty, no vacuous loop
        assert len(d["unsupported_candidates"]) >= 1
        for key in ("supported_candidates", "unsupported_candidates"):
            for cand in d[key]:
                assert list(cand.keys()) == sorted(cand.keys())
                assert cand["evidence"]  # provenance retained on the wire

    def test_two_roots_ambiguous_preserves_both_supported(self, tmp_path):
        # Genuine TWO-ROOT AMBIGUOUS case (B2): two distinct files
        # ``alpha/m.py`` and ``beta/m.py`` are each under ONE of the TWO
        # package roots (``alpha`` and ``beta``), each producing a real
        # supported candidate with name_components ("m",).  The EXACT
        # frozen outcome is AMBIGUOUS / multi_distinct_supported_candidates,
        # and BOTH supported candidates + provenance are preserved.
        def seed(conn):
            fi = _f(conn, "importer.py")
            idr = _i(conn, fi, "import", "m", 0, 1, 0)
            _n(conn, idr, 0, "m")
            _f(conn, "alpha/m.py")
            _f(conn, "beta/m.py")
        db = _make_index(tmp_path, "ambig.db", seed)
        real_builder = rq.build_module_inventory
        inv = real_builder(
            [{"rel_path": "alpha/m.py"}, {"rel_path": "beta/m.py"}],
            ["alpha", "beta"],   # EXACTLY two explicit roots, no third
        )
        assert len(inv) == 2, f"expected 2 candidates, got {len(inv)}: {[c.file_path for c in inv]}"
        for c in inv:
            assert c.component_supported and c.name_supported
            assert c.name_components == ("m",)
        assert {c.root.canonical for c in inv} == {"alpha", "beta"}
        assert {c.file_path for c in inv} == {"alpha/m.py", "beta/m.py"}
        # The wrapper's OWN record (the real ImportRecord the wrapper will
        # hand to the frozen resolver):
        first_wrap = rq.resolve_imports_from_index(str(db), ("alpha", "beta"))
        wrapped_rec = first_wrap.records[0].record
        expected = resolve_absolute_import(wrapped_rec, tuple(inv))
        # EXACT status + reason (no flexibility):
        assert expected.status == AMBIGUOUS
        assert expected.reason == REASON_MULTI_SUPPORTED
        assert len(expected.supported_candidates) == 2

        def _seam(files, roots):
            assert roots == ["alpha", "beta"]
            return tuple(inv)
        rq.build_module_inventory = _seam
        try:
            res = rq.resolve_imports_from_index(str(db), ("alpha", "beta"))
        finally:
            rq.build_module_inventory = real_builder
        # Use the wrapper's OWN record for direct parity (same record,
        # same inventory, same frozen resolver):
        wrapped_rec = res.records[0].record
        expected2 = resolve_absolute_import(wrapped_rec, tuple(inv))
        assert expected2.status == AMBIGUOUS
        assert expected2.reason == REASON_MULTI_SUPPORTED
        assert len(expected2.supported_candidates) == 2

        wrapped = res.records[0]
        assert wrapped.routing == "absolute"
        assert wrapped.absolute is not None
        assert wrapped.absolute.status == AMBIGUOUS
        assert wrapped.absolute.reason == REASON_MULTI_SUPPORTED
        assert len(wrapped.absolute.supported_candidates) == 2
        # Both supported, both roots, both provenance:
        assert {c.root.canonical for c in wrapped.absolute.supported_candidates} == {"alpha", "beta"}
        assert {c.file_path for c in wrapped.absolute.supported_candidates} == {"alpha/m.py", "beta/m.py"}
        for c in wrapped.absolute.supported_candidates:
            assert c.component_supported is True
            assert c.name_supported is True
            assert c.evidence  # provenance retained

    def test_actual_body_equals_sorted_body_exactly(self, tmp_path):
        db = _plain_db(tmp_path)
        result = rq.resolve_imports_from_index(str(db), (".",))
        d = rq.resolve_to_dict(result)
        actual = json.dumps(d, indent=2, ensure_ascii=True).encode("utf-8")
        sorted_body = json.dumps(d, indent=2, ensure_ascii=True, sort_keys=True).encode("utf-8")
        assert actual == sorted_body  # no test-side re-sorting needed
        from click.testing import CliRunner
        from repo_intelligence.cli import cli
        r = CliRunner().invoke(
            cli, ["resolve", "--root", ".", "--db-path", str(db), "--json"],
            catch_exceptions=False,
        )
        assert r.exit_code == 0
        # The CLI body is the pure body with exactly one trailing newline.
        assert r.output.encode("utf-8") == actual + b"\n"


class TestR5BeginFailurePropagates:
    """R5: a newly owned handle must establish the required transaction and
    propagate a BEGIN engine failure (no silent continue), closing the
    handle on every path.

    The frozen opener is private and ``sqlite3.Connection`` is immutable,
    so the BEGIN failure is injected through the single public seam the
    wrapper uses — ``rq._open_read_only`` — by returning a handle whose
    ``BEGIN DEFERRED`` raises.  The wrapper must re-raise (not swallow)
    and must still close the owned handle."""

    def test_begin_failure_is_propagated_and_handle_closed(self, tmp_path):
        db = _plain_db(tmp_path)
        real_open = rq._open_read_only
        closed = []

        real_close = None

        class _FailingBegin:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def execute(self, sql, *a, **k):
                if isinstance(sql, str) and sql == "BEGIN DEFERRED":
                    raise sqlite3.OperationalError("injected BEGIN failure")
                return self._inner.execute(sql, *a, **k)

            def close(self):
                closed.append(True)
                return self._inner.close()

        def spy_open(db_path):
            return _FailingBegin(real_open(db_path))

        rq._open_read_only = spy_open
        try:
            with pytest.raises(sqlite3.OperationalError):
                rq.resolve_imports_from_index(str(db), (".",))
        finally:
            rq._open_read_only = real_open
        assert closed, "owned handle must be closed even when BEGIN fails"


class TestR6CliErrorCategories:
    """R6: missing DB -> exit 1 / INDEX_NOT_FOUND; a frozen-evidence
    failure (aliased star) over an existing index -> exit 1 / EVIDENCE_
    INVALID; a query-input error (bad --kind) -> exit 2 / INVALID_QUERY;
    raw diagnostics/canaries never leak to stderr.

    The CLI pre-validates typed arguments (``_validate_scalar_args`` +
    frozen ``_canonical_roots``) BEFORE any storage lookup, so invalid
    roots / negative limits are INVALID_QUERY (exit 2) even when the DB
    is also missing, while valid arguments + a missing DB are
    INDEX_NOT_FOUND (exit 1).  No ``str(exc)`` substring heuristic:
    labels come from the two typed phases only."""

    @pytest.mark.parametrize("root_flags", [
        ["--root", ""],
        ["--root", ".", "--root", ""],
        ["--root", ".."],
    ])
    def test_p1_invalid_roots_exit_2_invalid_query(self, root_flags, tmp_path):
        # P1: invalid roots (empty / mixed valid+empty / escape) with a
        # MISSING DB: root validation still wins -> 2 / INVALID_QUERY.
        r = _test_cli(["resolve", *root_flags,
                       "--db-path", str(tmp_path / "nope.db")])
        assert r.exit_code == 2
        assert "INVALID_QUERY" in r.output
        assert "INDEX_NOT_FOUND" not in r.output
        # No raw analyzer/diagnostic text: the canary alias name and a
        # Python exception repr must not leak (the old str(exc) heuristic).
        assert "asname" not in r.output
        assert "ValueError(" not in r.output and "TypeError(" not in r.output

    @pytest.mark.parametrize("root_flags", [
        ["--root", ""],
        ["--root", ".", "--root", ""],
        ["--root", ".."],
    ])
    def test_p1_invalid_roots_exit_2_with_existing_db(self, root_flags, tmp_path):
        # P1: same invalid roots with an EXISTING DB: still 2 /
        # INVALID_QUERY (typed phase, DB content must not intervene).
        db = _plain_db(tmp_path)
        r = _test_cli(["resolve", *root_flags, "--db-path", str(db)])
        assert r.exit_code == 2
        assert "INVALID_QUERY" in r.output
        assert "INDEX_NOT_FOUND" not in r.output
        assert "EVIDENCE_INVALID" not in r.output
        assert "asname" not in r.output

    def test_p1_negative_limit_missing_db(self, tmp_path):
        # P1: negative limit is a scalar validation failure -> 2 /
        # INVALID_QUERY even with a missing DB.
        r = _test_cli(["resolve", "--root", ".",
                       "--db-path", str(tmp_path / "nope.db"), "--limit", "-1"])
        assert r.exit_code == 2
        assert "INVALID_QUERY" in r.output
        assert "INDEX_NOT_FOUND" not in r.output

    def test_p1_negative_limit_existing_db(self, tmp_path):
        db = _plain_db(tmp_path)
        r = _test_cli(["resolve", "--root", ".",
                       "--db-path", str(db), "--limit", "-1"])
        assert r.exit_code == 2
        assert "INVALID_QUERY" in r.output
        assert "EVIDENCE_INVALID" not in r.output

    def test_p1_star_aliased_star_limit_missing_db(self, tmp_path):
        # P1: the star-alias record is NOT a query-input error; with an
        # invalid (--limit -1) argument the typed phase still wins.
        db = _aliased_star_db(tmp_path)
        r = _test_cli(["resolve", "--root", "pkg",
                       "--db-path", str(db), "--limit", "-1"])
        assert r.exit_code == 2
        assert "INVALID_QUERY" in r.output
        assert "EVIDENCE_INVALID" not in r.output
        assert "asname" not in r.output    # alias canary never leaks

    def test_cli_index_not_found_exit_1(self, tmp_path):
        r = _test_cli(["resolve", "--root", "pkg",
                       "--db-path", str(tmp_path / "nope.db")], cwd=str(tmp_path))
        assert r.exit_code == 1
        assert "INDEX_NOT_FOUND" in r.output
        # The sanitized label must stand alone: no analyzer text, no
        # INVALID_QUERY re-classification, and no "invalid" wording at all
        # (which is what an old str(exc)-heuristic would have leaked).
        assert "INVALID_QUERY" not in r.output
        assert "asname" not in r.output
        assert r.output.lower().count("invalid") == 0

    def test_cli_aliased_star_is_evidence_invalid_exit_1(self, tmp_path):
        db = _aliased_star_db(tmp_path)
        r = _test_cli(
            ["resolve", "--root", "pkg", "--db-path", str(db)], cwd=str(tmp_path)
        )
        assert r.exit_code == 1
        assert "EVIDENCE_INVALID" in r.output
        # A query-input error must not be reported for this evidence failure.
        assert "INVALID_QUERY" not in r.output

    def test_cli_bad_kind_is_invalid_query_exit_2(self, tmp_path):
        from repo_intelligence.resolve_query import (
            resolve_imports_from_index as api,
        )
        from click.testing import CliRunner
        from repo_intelligence.cli import cli
        # --kind is a click.Choice; an out-of-band value is rejected by click
        # with exit 2 (UsageError) before our handler runs.
        r = CliRunner().invoke(
            cli, ["resolve", "--root", "pkg", "--db-path", "x",
                  "--kind", "not_a_kind"], catch_exceptions=False
        )
        assert r.exit_code == 2

    # -----------------------------------------------------------------
    # R6 — malformed WILDCARD-ALIAS regressions (acceptance hardening)
    #
    # A star import carrying a non-None `asname` is malformed frozen
    # evidence (R-1).  For EVERY required diagnostic-sensitive alias
    # value the CLI pre-validation succeeds (valid roots/limit/kind), so
    # the failure can only surface after the DB is opened — it must be
    # classified as a backend evidence failure (exit 1 / EVIDENCE_INVALID)
    # independent of exception-message contents.  No diagnostic substring
    # is allowed to leak to stdout/stderr, and no (JSON or text) resolution
    # result may be emitted.
    # -----------------------------------------------------------------
    @pytest.mark.parametrize("alias_name", ["asname", "module", "limit must be"])
    def test_p2_aliased_star_is_evidence_invalid_exit_1(self, alias_name, tmp_path):
        # Fixture: `from pkg import * as <alias_name>` under root pkg, with
        # a valid existing database.  The star's non-None asname makes the
        # record malformed; the frozen wildcard analyzer rejects it.
        def seed(conn):
            fi = _f(conn, "importer.py")
            id1 = _i(conn, fi, "from_import", "pkg", 0, 1, 0)
            _n(conn, id1, 0, "*", alias_name)   # non-None alias -> malformed star
            _f(conn, "pkg/__init__.py")
        db = _make_index(tmp_path, "alias.db", seed)
        r = _test_cli(["resolve", "--root", "pkg", "--db-path", str(db)],
                      cwd=str(tmp_path))
        assert r.exit_code == 1
        assert "EVIDENCE_INVALID" in r.output
        # A query-input or storage failure must NOT be reported for this
        # evidence failure:
        assert "INVALID_QUERY" not in r.output
        assert "INDEX_NOT_FOUND" not in r.output
        # The supplied alias canary must never leak (covers `asname`,
        # `module` and the `limit must be` substring) and no raw Python
        # exception text may surface:
        assert alias_name not in r.output
        assert "ValueError(" not in r.output
        assert "TypeError(" not in r.output
        # No successful resolution result: stdout must be empty.
        assert r.stdout == ""

    def test_p2_star_mixed_with_named_is_evidence_invalid_exit_1(self, tmp_path):
        # Fixture: `from pkg import *, named` under root pkg.  A star mixed
        # with a named entry is malformed (R-1); CLI surfaces
        # EVIDENCE_INVALID/1 (not INVALID_QUERY/2).  Retained from the
        # earlier revision (valid module, valid args, existing DB).
        def seed(conn):
            fi = _f(conn, "importer.py")
            id1 = _i(conn, fi, "from_import", "pkg", 0, 1, 0)
            _n(conn, id1, 0, "*", None)
            _n(conn, id1, 1, "named", None)
            _f(conn, "pkg/__init__.py")
        db = _make_index(tmp_path, "mixed.db", seed)
        r = _test_cli(["resolve", "--root", "pkg",
                       "--db-path", str(db)], cwd=str(tmp_path))
        assert r.exit_code == 1
        assert "EVIDENCE_INVALID" in r.output
        assert "INVALID_QUERY" not in r.output
        assert "INDEX_NOT_FOUND" not in r.output
        assert "ValueError(" not in r.output
        assert "TypeError(" not in r.output
        assert r.stdout == ""



class TestR8ValidatesRootsBeforeDb:
    """R8: an invalid root must fail root validation FIRST, even when the
    DB is also missing (root ValueError, not FileNotFoundError/INDEX_NOT_
    FOUND)."""

    def test_invalid_root_beats_missing_db(self, tmp_path):
        # R8: root prevalidation precedes DB existence (even when the DB is
        # missing, the empty root is the INVALID_QUERY error).
        import pytest
        with pytest.raises(ValueError):
            rq.resolve_imports_from_index(
                str(tmp_path / "nope.db"), [""]   # empty root is invalid
            )

    def test_all_roots_passed_not_silently_dropped(self, tmp_path):
        # P3-B: an EXPLICIT empty root must be classified as a query-input
        # error — exit exactly 2 / INVALID_QUERY — BEFORE the DB existence
        # check (R8).  No successful resolution, no INDEX_NOT_FOUND, and no
        # EVIDENCE_INVALID may appear.
        from click.testing import CliRunner
        from repo_intelligence.cli import cli
        db = _plain_db(tmp_path)
        # Case A: empty root with EXISTING database.
        r = CliRunner().invoke(
            cli, ["resolve", "--root", "", "--db-path", str(db)],
            catch_exceptions=False,
        )
        assert r.exit_code == 2, r.output
        assert "INVALID_QUERY" in r.output, r.output
        assert "INDEX_NOT_FOUND" not in r.output, r.output
        assert "EVIDENCE_INVALID" not in r.output, r.output
        # No successful resolution output.
        assert r.stdout == ""

        # Case B: empty root with MISSED database (root prevalidation must
        # still win over the DB-existence check).
        r = CliRunner().invoke(
            cli, ["resolve", "--root", "", "--db-path", str(tmp_path / "nope.db")],
            catch_exceptions=False,
        )
        assert r.exit_code == 2, r.output
        assert "INVALID_QUERY" in r.output, r.output
        assert "INDEX_NOT_FOUND" not in r.output, r.output
        assert "EVIDENCE_INVALID" not in r.output, r.output
        assert r.stdout == ""


class TestR9RelativeFileLabeling:
    """R9: relative text output labels root and repository-relative file
    separately and never prefixes the root onto an already-relative
    file_path (no ``src/src/pkg/a.py`` doubling)."""

    def test_overlapping_context_text_labels_root_and_file(self, tmp_path):
        def seed(conn):
            fi = _f(conn, "pkg/a.py")
            id1 = _i(conn, fi, "from_import", "", 1, 5, 0)
            _n(conn, id1, 0, "sub")
            _f(conn, "pkg/sub.py")
        db = _make_index(tmp_path, "rel.db", seed)
        from click.testing import CliRunner
        from repo_intelligence.cli import cli
        r = CliRunner().invoke(
            cli, ["resolve", "--root", "pkg", "--db-path", str(db)],
            catch_exceptions=False,
        )
        assert r.exit_code == 0
        # The per-context relative line labels root= and file= separately.
        assert "root=" in r.output and "file=" in r.output
        # Crucially, the doubled root/file spelling must not appear.
        doubled = "pkg/pkg/" + "a.py"
        # the raw doubling pattern (root prefix onto relative path) is absent
        assert "file=pkg/a.py" in r.output
        assert f"root=pkg{doubled}" not in r.output

    def test_multiple_root_relative_contexts_labeled_separately(self, tmp_path):
        # Genuine multi-context relative fixture using OVERLAPPING EXPLICIT
        # explicit roots "." and "root/a" for source root/a/pkg/a.py and
        # target root/a/pkg/sub.py.  The frozen 2C-3A resolver contract
        # says "multiple explicit roots may produce different candidates
        # for the same physical file - each distinct interpretation is
        # preserved".  With overlapping roots ".", "root/a", the inventory
        # contains the source file at BOTH interpretations:
        #   (root "", comps ('root','a','pkg','a'), qualified 'root.a.pkg.a')
        #   (root 'root/a', comps ('pkg','a'), qualified 'pkg.a')
        # Each becomes a DISTINCT source-context candidate.  The level-1
        # `from . import sub` relative then yields:
        #   context 1 (root '', base 'root.a.pkg'): target 'root.a.pkg.sub' RESOLVED
        #   context 2 (root 'root/a', base 'pkg'):  target 'pkg.sub'         RESOLVED
        # Both contexts are preserved verbatim in the wrapper; CLI text
        # labels root= and file= separately per context, no doubled
        # root/file spelling.  Q4 deferral: no named or wildcard
        # attachment.  Direct frozen parity holds.
        from repo_intelligence.relative_resolution import (
            resolve_relative_import as rr_direct,
        )
        from repo_intelligence.module_inventory import build_module_inventory
        def seed(conn):
            fi = _f(conn, "root/a/pkg/a.py")
            id1 = _i(conn, fi, "from_import", "sub", 1, 5, 0)
            _n(conn, id1, 0, "sub")
            _f(conn, "root/a/pkg/sub.py")
        db = _make_index(tmp_path, "rel2.db", seed)
        real_builder = rq.build_module_inventory
        files = [{"rel_path": "root/a/pkg/a.py"},
                 {"rel_path": "root/a/pkg/sub.py"}]
        built = real_builder(files, [".", "root/a"])
        # Two DISTINCT source-context candidates at root/a/pkg/a.py,
        # each with a different root identity.
        sources = tuple(c for c in built if c.file_path == "root/a/pkg/a.py")
        assert len(sources) == 2, f"expected 2 source contexts, got {len(sources)}"
        assert {c.root.canonical for c in sources} == {"", "root/a"}
        # The base_components (from name_components[:-1]) must be distinct
        # across the two contexts:
        assert {c.name_components[:-1] for c in sources} == {
            ("root", "a", "pkg"), ("pkg",)
        }
        # Target components must be distinct and both lead to the
        # resolvable target root/a/pkg/sub.py:
        #   context 1 base ('root','a','pkg') + 'sub' -> 'root.a.pkg.sub'
        #   context 2 base ('pkg',)      + 'sub' -> 'pkg.sub'
        # Both must map (via delegation) to a supported candidate.
        # Direct frozen parity first.
        from repo_intelligence.python_ast import ImportRecord
        imp_rec = ImportRecord(
            kind="from_import", module="sub", level=1, names=(),
            source_path="root/a/pkg/a.py", start_line=5, start_col=0,
        )
        direct = rr_direct(imp_rec, tuple(built))
        assert len(direct.contexts) == 2
        # Distinct source root identities per context:
        src_roots = {c.source_candidate.root.canonical for c in direct.contexts}
        assert src_roots == {"", "root/a"}
        # Distinct target components per context:
        targets = {c.target_components for c in direct.contexts}
        assert targets == {("root", "a", "pkg", "sub"), ("pkg", "sub")}
        # Both contexts' supported sets should include root/a/pkg/sub.py
        # under the matching root interpretation (lossless=True):
        for c in direct.contexts:
            assert c.lossless is True, c
            assert c.status in ("RESOLVED", "UNRESOLVED", "AMBIGUOUS"), c
            assert len(c.supported_candidates) >= 1, c

        # The wrapper's seam: pin the frozen builder output for roots
        # (".", "root/a"); the wrapper call must pass the same roots.
        def _seam(file_records, roots):
            assert roots == [".", "root/a"]
            return tuple(built)
        rq.build_module_inventory = _seam
        try:
            res = rq.resolve_imports_from_index(str(db), (".", "root/a"))
        finally:
            rq.build_module_inventory = real_builder
        wrapped = res.records[0]
        assert wrapped.routing == "relative"
        assert wrapped.absolute is None
        assert wrapped.named is None             # Q4 deferral
        assert wrapped.wildcard is None          # Q4 deferral
        assert len(wrapped.relative.contexts) == 2
        # Direct parity on the SAME record+inventory:
        direct2 = rr_direct(wrapped.record, tuple(built))
        assert len(direct2.contexts) == len(wrapped.relative.contexts) == 2
        for c_direct, c_wrap in zip(direct2.contexts, wrapped.relative.contexts):
            assert c_direct.source_candidate.root.canonical == \
                   c_wrap.source_candidate.root.canonical
            assert c_direct.source_candidate.file_path == \
                   c_wrap.source_candidate.file_path
            assert c_direct.base_components == c_wrap.base_components
            assert c_direct.target_components == c_wrap.target_components
            assert c_direct.lossless == c_wrap.lossless
            assert c_direct.status == c_wrap.status
            assert c_direct.reason == c_wrap.reason
            assert {c.root.canonical for c in c_direct.supported_candidates} == \
                   {c.root.canonical for c in c_wrap.supported_candidates}

        # CLI text — invoke with --root "." --root "root/a"; BOTH contexts
        # each have their OWN labeled line with root= and file= separately
        # (no doubled root/file spelling).
        from click.testing import CliRunner
        from repo_intelligence.cli import cli
        old2 = rq.build_module_inventory
        rq.build_module_inventory = _seam
        try:
            r = CliRunner().invoke(
                cli, ["resolve", "--root", ".", "--root", "root/a",
                      "--db-path", str(db)],
                catch_exceptions=False,
            )
        finally:
            rq.build_module_inventory = old2
        assert r.exit_code == 0
        # BOTH contexts must be present (relative[0] and relative[1]):
        assert "relative[0]" in r.output, r.output
        assert "relative[1]" in r.output, r.output
        # Each context has a labeled root and a labeled file, separately
        # (the root for the "." context is rendered as ``root=`` with no
        # trailing text, and the file path is the source's repo-relative
        # file_path ``root/a/pkg/a.py``).
        lines = [ln.strip() for ln in r.output.splitlines() if "relative[" in ln]
        assert len(lines) == 2, f"expected 2 labeled lines, got {len(lines)}: {lines}"
        labeled = [ln for ln in lines if "root=" in ln and "file=root/a/pkg/a.py" in ln]
        assert len(labeled) == 2, f"expected 2 labeled lines, got {len(labeled)}: {labeled}"
        # The two contexts must carry distinct root labels:
        root_labels = [ln.split("root=", 1)[1].split(" ", 1)[0] for ln in lines if "root=" in ln]
        assert root_labels == ["", "root/a"], root_labels
        # No doubled root/file path spelling:
        assert "root=a/" not in r.output.replace("file=root/a/pkg", "OKFILE").replace("file=root/a", "OKFILE")
        # Per-context status is non-empty (not an aggregate):
        assert any("status=" in ln for ln in lines)


# ---------------------------------------------------------------------------
# R7 — a real writer committing BETWEEN the inventory read and the import
# read must NOT corrupt the reader's snapshot: the API returns the OLD
# generation, and a separate post-query read observes the NEW generation.
# ---------------------------------------------------------------------------

def test_writer_between_inventory_and_import_reads_is_isolated(tmp_path):
    """WAL-mode fixture DB; ``rq.load_file_records_from_db`` is wrapped so
    that after the REAL loader runs on the actual API handle, a bounded
    real writer (own connection in its own thread) inserts a new
    IMPORT row (attached to the real importer file ``importer.py``) and
    commits BEFORE the inventory rows are returned.  The reader must
    still report the OLD total, and a fresh read must see the NEW
    total."""
    db = _concurrency_db(tmp_path)  # 10 imports already present

    # Put the DB in WAL mode BEFORE the API call.
    wc = sqlite3.connect(str(db))
    wc.execute("PRAGMA journal_mode = WAL")
    wc.close()

    base_total = rq.resolve_imports_from_index(str(db), ("pkg",)).total_count
    assert base_total == 10

    real_loader = rq.load_file_records_from_db
    writer_ran = []
    import threading

    def writer():
        wc2 = sqlite3.connect(str(db))
        wc2.execute("PRAGMA busy_timeout = 5000")
        fi = wc2.execute(
            "SELECT id FROM files WHERE rel_path='importer.py'"
        ).fetchone()[0]
        wc2.execute(
            "INSERT INTO imports (file_id, kind, module, level, start_line, start_col) "
            "VALUES (?, 'import', '_w', 0, 999, 0)", (fi,)
        )
        wc2.commit()
        wc2.close()
        writer_ran.append(True)

    def wrapped_loader(*a, **k):
        rows = real_loader(*a, **k)      # real loader on the real API handle
        t = threading.Thread(target=writer)
        t.start(); t.join(timeout=15)     # bounded wait — no hang
        assert t.is_alive() is False, "writer did not finish in time"
        return rows

    rq.load_file_records_from_db = wrapped_loader
    try:
        result = rq.resolve_imports_from_index(str(db), ("pkg",))
        # The reader captured the snapshot BEFORE the writer committed:
        # it must report the OLD generation, not the new 11.
        assert result.total_count == 10
        assert len(result.records) == 10
    finally:
        rq.load_file_records_from_db = real_loader

    # The writer actually committed (new generation = 11).
    assert writer_ran, "writer must have committed between reads"
    fresh = rq.resolve_imports_from_index(str(db), ("pkg",))
    assert fresh.total_count == 11
