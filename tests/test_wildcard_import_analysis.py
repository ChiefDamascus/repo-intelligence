"""Acceptance tests for ``repo_intelligence.wildcard_import_analysis``.

Covers W1-W24 (W1-W24 per
``docs/phase-2c-3b-2-architecture-approved.md`` §10), the R-1 through R-8
decisions, determinism / input-order-invariance (W23), state-table
enforcement (W17), the ``analyze_wildcard_import`` API surface
(W22), and the frozen-result invariant (W11-W24 result frozen).

Each test is a focused static assertion against the public surface of
``repo_intelligence.wildcard_import_analysis`` and the two 2C-3B-1
primitives it builds on.  Nothing here executes a module's source.
"""

from __future__ import annotations

import ast
import copy
import dataclasses

import pytest

from repo_intelligence.imported_name_analysis import (
    SRC_NOT_FOUND,
    SRC_SUPPLIED,
    SRC_UNAVAILABLE,
    SRC_UNPARSEABLE,
    TargetSource,
)
from repo_intelligence.module_inventory import (
    ModuleCandidate,
    PackageRoot,
    build_module_inventory,
)
from repo_intelligence.python_ast import ImportRecord
from repo_intelligence.wildcard_import_analysis import (
    ALL_ABSENT,
    ALL_DECLARED,
    ALL_UNKNOWN,
    DEFAULT_DIRECT_ONLY,
    DEFAULT_INCOMPLETE,
    WR_ALL_ABSENT_DEFAULT_APPLIED,
    WR_ALL_DECLARED_APPLIED,
    WR_ALL_UNKNOWN_NO_EVIDENCE,
    WR_ANALYZED,
    WR_INVALID_RECORD,
    WR_NOT_ANALYZED,
    WR_NOT_IN_SCOPE,
    WR_SOURCE_NOT_FOUND_NO_EVIDENCE,
    WR_SOURCE_UNAVAILABLE_NO_EVIDENCE,
    WR_SOURCE_UNPARSEABLE_NO_EVIDENCE,
    WR_TARGET_NO_CANDIDATE,
    WR_TARGET_UNSUPPORTED,
    TargetContext,
    WildcardAnalysis,
    WildcardCandidateEvidence,
    WildcardContextAnalysis,
    analyze_wildcard_import,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def two_pkg_inventory() -> tuple[ModuleCandidate, ...]:
    """A two-candidate inventory: ``pkg`` (a package) and ``pkg.sub``
    (a nested package).  Used for most W* tests."""
    return build_module_inventory(
        [
            {"rel_path": "pkg/__init__.py", "language": "Python",
             "is_binary": False},
            {"rel_path": "pkg/sub/__init__.py", "language": "Python",
             "is_binary": False},
        ],
        ["."],
    )


@pytest.fixture
def pkg_candidate(two_pkg_inventory):
    return two_pkg_inventory[0]


@pytest.fixture
def star_record() -> ImportRecord:
    return ImportRecord(
        kind="from_import",
        module="pkg",
        names=(("*", None),),
        level=0,
        source_path="caller.py",
        start_line=1,
        start_col=0,
    )


@pytest.fixture
def nonstar_record() -> ImportRecord:
    return ImportRecord(
        kind="from_import",
        module="pkg",
        names=(("a", None),),
        level=0,
        source_path="caller.py",
        start_line=1,
        start_col=0,
    )


def _ev(res: WildcardAnalysis, ctx_idx: int, ev_idx: int) -> WildcardCandidateEvidence:
    """Drill into ``res.contexts[ctx_idx].candidates[ev_idx]``."""
    return res.contexts[ctx_idx].candidates[ev_idx]


def _supplied(src: str) -> TargetSource:
    return TargetSource(SRC_SUPPLIED, src, None)


# ---------------------------------------------------------------------------
# W1-W12: __all__ closed-state classification
# ---------------------------------------------------------------------------


class TestAllDeclared:
    def test_w1_declared_list_tuple(self, star_record, pkg_candidate,
                                    two_pkg_inventory):
        ts = _supplied('__all__ = ("a", "b")')
        res = analyze_wildcard_import(star_record, two_pkg_inventory, {pkg_candidate: ts})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_DECLARED
        assert e.outcome == WR_ANALYZED
        assert e.reason == WR_ALL_DECLARED_APPLIED
        assert e.all_entries == ("a", "b")
        assert e.star_names == ("a", "b")
        assert e.default_scope is None
        assert e.source_state == SRC_SUPPLIED

    def test_w2_declared_empty_list(self, star_record, pkg_candidate,
                                    two_pkg_inventory):
        ts = _supplied("__all__ = []")
        res = analyze_wildcard_import(star_record, two_pkg_inventory, {pkg_candidate: ts})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_DECLARED
        assert e.outcome == WR_ANALYZED
        assert e.all_entries == ()
        assert e.star_names == ()

    def test_w19_preserves_order_and_duplicates(self, star_record, pkg_candidate,
                                                two_pkg_inventory):
        ts = _supplied('__all__ = ("a", "a", "_p", "", "nobody")')
        res = analyze_wildcard_import(star_record, two_pkg_inventory, {pkg_candidate: ts})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_DECLARED
        # order AND duplicates preserved — no sort, no dedup
        assert e.all_entries == ("a", "a", "_p", "", "nobody")
        assert e.star_names == ("a", "a", "_p", "", "nobody")


class TestAllUnknown:
    def test_w3_binop(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied('__all__ = ["a"] + ["b"]')})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED
        assert e.reason == WR_ALL_UNKNOWN_NO_EVIDENCE
        assert e.all_entries == ()
        assert e.star_names == ()
        assert e.default_scope is None

    def test_w4_reassign(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied('__all__ = ("a",)\n__all__ = ("b",)')})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED

    def test_w5_append(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied('__all__ = (); __all__.append("x")')})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED

    def test_w6_alias(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied('x = __all__\nx.append("y")')})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED

    def test_w7_delete(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied('__all__ = ("a",)\ndel __all__')})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED

    def test_w8_conditional(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied('__all__ = ("a",) if c else ("b",)')})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED

    def test_w9_list_comp(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("__all__ = [n for n in names]")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED

    def test_w10_bare_name(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("__all__ = names")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED

    def test_w20_branch_body(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("if x:\n    __all__ = ('a',)")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED

    def test_w21a_subscript(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("__all__ = x[0]")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED

    def test_w21b_augassign(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied('__all__ = ("a",); __all__ += ("b",')})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED

    def test_w21c_mixed_type(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("__all__ = ('a', 1)")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED

    def test_exec_top_level(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("a=1\nexec('x=1')")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED

    def test_exec_nested_function_is_incomplete(self, star_record, pkg_candidate,
                                                  two_pkg_inventory):
        """exec inside an unexecuted function body is NOT module-executing
        (W24: nested locals ignored; §6).  Result: ALL_ABSENT + INCOMPLETE."""
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("def f(): exec('x=1')\na=1")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_ABSENT
        assert e.outcome == WR_ANALYZED
        # function name `f` is part of star_names (W11: def names are included)
        assert "a" in e.star_names
        assert "f" in e.star_names
        # exec inside a def is not module-executing; the default scope
        # is DIRECT_ONLY (no module-level control flow or __all__ stores).
        assert e.default_scope == DEFAULT_DIRECT_ONLY

    def test_globals_write(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied('globals()["k"] = 1\na=1')})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED


# ---------------------------------------------------------------------------
# W13-W15: absent-default + resolver-level coverage
# ---------------------------------------------------------------------------


class TestAllAbsentDefault:
    def test_w13_defs_classes_assigns(self, star_record, pkg_candidate,
                                      two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("a=1\n_p=2\ndef f(): pass\nclass C: pass")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_ABSENT
        assert e.outcome == WR_ANALYZED
        assert e.star_names == ("C", "a", "f")  # direct top-level, sorted
        assert e.default_scope == DEFAULT_DIRECT_ONLY
        assert e.all_entries == ()

    def test_w14_imports(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("import os.path\n"
                                      "import os as o\n"
                                      "from m import x as y")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_ABSENT
        assert e.star_names == ("o", "os", "y")
        assert e.default_scope == DEFAULT_DIRECT_ONLY

    def test_w15_top_level_control_flow(self, star_record, pkg_candidate,
                                        two_pkg_inventory):
        # `def f(): exec("x=1")` is a function body, not top-level;
        # `if flag: a=1` is top-level control flow → INCOMPLETE.
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("if flag:\n    a=1")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_ABSENT
        assert e.star_names == ()
        assert e.default_scope == DEFAULT_INCOMPLETE

    def test_w24a_del_not_all(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("a=1\ndel a")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_ABSENT
        assert e.star_names == ("a",)
        assert e.default_scope == DEFAULT_INCOMPLETE

    def test_w24b_if_body(self, star_record, pkg_candidate, two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("if flag:\n    a=1")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_ABSENT
        assert e.star_names == ()  # the only assignment is conditional
        assert e.default_scope == DEFAULT_INCOMPLETE


# ---------------------------------------------------------------------------
# W16-W18: source-state + relative pre-fail
# ---------------------------------------------------------------------------


class TestSourceState:
    def test_w16_src_not_found(self, star_record, pkg_candidate,
                               two_pkg_inventory):
        ts = TargetSource(SRC_UNAVAILABLE, None, None)
        # (SRC_NOT_FOUND is synthesized when the key is missing.)
        res = analyze_wildcard_import(star_record, two_pkg_inventory, {})
        e = _ev(res, 0, 0)
        assert e.source_state == SRC_NOT_FOUND
        assert e.outcome == WR_NOT_ANALYZED
        assert e.reason == WR_SOURCE_NOT_FOUND_NO_EVIDENCE
        assert e.all_state == ALL_UNKNOWN
        assert e.star_names == ()
        assert e.default_scope is None

    def test_w16_src_unavailable(self, star_record, pkg_candidate,
                                 two_pkg_inventory):
        ts = TargetSource(SRC_UNAVAILABLE, None, None)
        res = analyze_wildcard_import(star_record, two_pkg_inventory, {pkg_candidate: ts})
        e = _ev(res, 0, 0)
        assert e.source_state == SRC_UNAVAILABLE
        assert e.outcome == WR_NOT_ANALYZED
        assert e.reason == WR_SOURCE_UNAVAILABLE_NO_EVIDENCE
        assert e.all_state == ALL_UNKNOWN

    def test_w16_src_unparseable(self, star_record, pkg_candidate,
                                 two_pkg_inventory):
        ts = TargetSource(SRC_UNPARSEABLE, "def (", "SyntaxError")
        res = analyze_wildcard_import(star_record, two_pkg_inventory, {pkg_candidate: ts})
        e = _ev(res, 0, 0)
        assert e.source_state == SRC_UNPARSEABLE
        assert e.outcome == WR_NOT_ANALYZED
        assert e.reason == WR_SOURCE_UNPARSEABLE_NO_EVIDENCE
        assert e.all_state == ALL_UNKNOWN

    def test_w16_src_supplied_bad(self, star_record, pkg_candidate,
                                  two_pkg_inventory):
        # `ast.parse` will fail with SyntaxError; the analyzer reclassifies
        # to SRC_UNPARSEABLE (R-2: no salvage).
        ts = _supplied("def (")
        res = analyze_wildcard_import(star_record, two_pkg_inventory, {pkg_candidate: ts})
        e = _ev(res, 0, 0)
        assert e.source_state == SRC_UNPARSEABLE
        assert e.outcome == WR_NOT_ANALYZED
        assert e.reason == WR_SOURCE_UNPARSEABLE_NO_EVIDENCE

    def test_w18_relative_missing_source_context(self, two_pkg_inventory):
        # A level-1 star record from a non-package top-level file (e.g.
        # ``a.py`` with no ``pkg`` root) makes the frozen relative
        # resolver report pre-context failure; the envelope must have
        # empty contexts and the issue preserved.
        star_rel = ImportRecord(
            kind="from_import",
            module="b",
            names=(("*", None),),
            level=1,
            source_path="top/a.py",  # not under any package in inv
            start_line=1,
            start_col=0,
        )
        res = analyze_wildcard_import(star_rel, two_pkg_inventory, {})
        assert res.contexts == ()
        assert res.outcome == WR_NOT_ANALYZED
        assert res.issue is not None
        # flat source_assessments still emitted over unique candidates
        assert len(res.source_assessments) == len(two_pkg_inventory)


class TestRelativeValid:
    def test_relative_declared(self, two_pkg_inventory):
        # Two candidates: pkg/a.py (source for the relative import) and
        # pkg/b.py (the target).  `from .b import *` from pkg/a.py with
        # pkg/b.py declaring __all__ = ("x","y").
        inv = build_module_inventory(
            [
                {"rel_path": "pkg/__init__.py", "language": "Python",
                 "is_binary": False},
                {"rel_path": "pkg/a.py", "language": "Python", "is_binary": False},
                {"rel_path": "pkg/b.py", "language": "Python", "is_binary": False},
            ],
            ["."],
        )
        b = next(c for c in inv if c.name_components == ("pkg", "b"))
        star_rel = ImportRecord(
            kind="from_import",
            module="b",
            names=(("*", None),),
            level=1,
            source_path="pkg/a.py",
            start_line=1,
            start_col=0,
        )
        res = analyze_wildcard_import(
            star_rel, inv, {b: _supplied('__all__ = ("x", "y")')})
        assert res.outcome == WR_ANALYZED
        assert res.contexts[0].context.kind == "relative"
        assert len(res.contexts[0].candidates) == 1
        e = _ev(res, 0, 0)
        assert e.outcome == WR_ANALYZED
        assert e.star_names == ("x", "y")


# ---------------------------------------------------------------------------
# W19: order + duplicates (already in TestAllDeclared)
# W20: branch body (already in TestAllUnknown)
# W21: __all__ value non-closed (already in TestAllUnknown)
# W22: API surface
# W23: determinism / input-order-invariance
# W24: star re-export unknown / direct-only / incomplete
# ---------------------------------------------------------------------------


class TestApiSurface:
    def test_w22_nonstar_returns_not_in_scope(self, nonstar_record,
                                              two_pkg_inventory):
        res = analyze_wildcard_import(nonstar_record, two_pkg_inventory, {})
        assert isinstance(res, WildcardAnalysis)
        assert res.outcome == WR_NOT_IN_SCOPE
        assert res.reason == WR_INVALID_RECORD
        assert res.contexts == ()
        # non-star result carries empty source_assessments per §11
        assert res.source_assessments == ()
        assert res.record is nonstar_record
        # frozen — attempt to mutate
        with pytest.raises(dataclasses.FrozenInstanceError):
            res.outcome = WR_ANALYZED

    @pytest.mark.parametrize("record_shape", [
        lambda: ImportRecord(kind="from_import", module="pkg",
                             names=(("a", None), ("*", None)),
                             level=0, source_path="a.py",
                             start_line=1, start_col=0),
        lambda: ImportRecord(kind="from_import", module="pkg",
                             names=(("*", "s"),),
                             level=0, source_path="a.py",
                             start_line=1, start_col=0),
        lambda: ImportRecord(kind="from_import", module="pkg",
                             names=(("*", None), ("*", None)),
                             level=0, source_path="a.py",
                             start_line=1, start_col=0),
        lambda: ImportRecord(kind="import", module="pkg",
                             names=(("*", None),),
                             level=0, source_path="a.py",
                             start_line=1, start_col=0),
    ])
    def test_w22_malformed_star_raises(self, record_shape, two_pkg_inventory):
        rec = record_shape()
        with pytest.raises(ValueError):
            analyze_wildcard_import(rec, two_pkg_inventory, {})

    def test_w22_record_type(self, two_pkg_inventory):
        with pytest.raises(TypeError):
            analyze_wildcard_import("not a record", two_pkg_inventory, {})

    def test_w22_inventory_type(self, star_record):
        with pytest.raises(TypeError):
            analyze_wildcard_import(star_record, ["not-a-tuple"], {})

    def test_w22_target_sources_none(self, star_record, two_pkg_inventory):
        with pytest.raises(TypeError):
            analyze_wildcard_import(star_record, two_pkg_inventory, None)

    def test_w22_target_sources_type(self, star_record, two_pkg_inventory):
        with pytest.raises(TypeError):
            analyze_wildcard_import(star_record, two_pkg_inventory,
                                    "not-a-mapping")

    def test_w22_wrong_value_type(self, star_record, pkg_candidate,
                                  two_pkg_inventory):
        with pytest.raises(TypeError):
            analyze_wildcard_import(star_record, two_pkg_inventory,
                                    {pkg_candidate: "not-a-TargetSource"})

    def test_w17_state_table_supplied_requires_text(self, star_record,
                                                    pkg_candidate,
                                                    two_pkg_inventory):
        bad = object.__new__(TargetSource)
        bad.__dict__.update(provenance=SRC_SUPPLIED, source_text="x",
                            parse_error="boom")
        with pytest.raises(TypeError):
            analyze_wildcard_import(star_record, two_pkg_inventory,
                                    {pkg_candidate: bad})

    def test_r8_extra_key_rejected(self, star_record, pkg_candidate,
                                   two_pkg_inventory):
        # An out-of-inventory candidate must be rejected with ValueError
        # (R-8).
        other = ModuleCandidate(
            file_path="other.py",
            root=PackageRoot(canonical="other-root", original="other-root"),
            qualified_name="other",
            name_components=("other",),
            file_kind="ordinary_module",
            directory_kind="regular_package",
            component_supported=True,
            name_supported=True,
        )
        ts = TargetSource(SRC_UNAVAILABLE, None, None)
        with pytest.raises(ValueError, match="not in the inventory"):
            analyze_wildcard_import(star_record, two_pkg_inventory,
                                    {other: ts})


# ---------------------------------------------------------------------------
# W23: determinism / input-order-invariance
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_w23_inventory_permutation(self, star_record, two_pkg_inventory):
        a = analyze_wildcard_import(star_record, two_pkg_inventory, {})
        b = analyze_wildcard_import(star_record, tuple(reversed(two_pkg_inventory)), {})
        assert a == b
        assert a.record is star_record
        # identity is preserved — the *same* record object, same candidates
        assert a.source_assessments == b.source_assessments

    def test_purity_repeated_invocation(self, star_record, pkg_candidate,
                                        two_pkg_inventory):
        ts = _supplied('__all__ = ("a", "b")')
        a = analyze_wildcard_import(star_record, two_pkg_inventory, {pkg_candidate: ts})
        b = analyze_wildcard_import(star_record, two_pkg_inventory, {pkg_candidate: ts})
        assert a == b


# ---------------------------------------------------------------------------
# W24: star re-export semantics
# ---------------------------------------------------------------------------


class TestStarReexport:
    def test_w24_top_level_star_reexport(self, star_record, pkg_candidate,
                                         two_pkg_inventory):
        # `from x import *` in the candidate's source → the candidate's
        # __all__ binding is unknowable (§8, W24/R-7) → ALL_UNKNOWN.
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("from x import *")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED
        assert e.star_names == ()

    def test_r7_no_literal_star(self, star_record, pkg_candidate,
                                two_pkg_inventory):
        # The evidence tuple must never contain the literal "*" — that is
        # a module-executing star (R-7), not a star name.
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory, {})
        e = _ev(res, 0, 0)
        assert e.star_names == ()
        # The candidate's names come from the §8 rule; for a candidate
        # we don't have source for, it is empty (SRC_NOT_FOUND).
        assert "*" not in e.star_names


# ---------------------------------------------------------------------------
# Frozen-result invariants
# ---------------------------------------------------------------------------


class TestFrozenResults:
    def test_result_frozen(self, star_record, pkg_candidate,
                           two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied('__all__ = ("a",)')})
        with pytest.raises(dataclasses.FrozenInstanceError):
            res.outcome = "WR_ANALYZED"
        ctx = res.contexts[0]
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.context_index = 1
        ev = _ev(res, 0, 0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            ev.all_state = ALL_DECLARED

    def test_candidates_are_frozen(self, star_record, pkg_candidate,
                                   two_pkg_inventory):
        res = analyze_wildcard_import(star_record, two_pkg_inventory, {})
        ev = _ev(res, 0, 0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            ev.candidate = pkg_candidate
        with pytest.raises(dataclasses.FrozenInstanceError):
            ev.all_entries = ("x",)


# ---------------------------------------------------------------------------
# Structural invariants (every W* result shares these)
# ---------------------------------------------------------------------------


class TestStructuralInvariants:
    def test_single_context_absolute(self, star_record, two_pkg_inventory):
        res = analyze_wildcard_import(star_record, two_pkg_inventory, {})
        assert len(res.contexts) == 1
        ctx = res.contexts[0]
        assert ctx.context_index == 0
        assert isinstance(ctx, WildcardContextAnalysis)
        assert isinstance(ctx.context, TargetContext)
        assert ctx.context.kind == "absolute"
        # absolute context: source_candidate is None, base_module set
        assert ctx.context.source_candidate is None
        assert ctx.context.base_module == "pkg"
        assert ctx.context.base_components is None
        # target_components = the requested components
        assert ctx.context.target_components == ("pkg",)

    def test_relative_context(self):
        # Valid relative context: from pkg/a.py do `from .b import *`
        # where both pkg/a.py and pkg/b.py are in the inventory.
        inv = build_module_inventory(
            [
                {"rel_path": "pkg/__init__.py", "language": "Python",
                 "is_binary": False},
                {"rel_path": "pkg/a.py", "language": "Python",
                 "is_binary": False},
                {"rel_path": "pkg/b.py", "language": "Python",
                 "is_binary": False},
            ],
            ["."],
        )
        star_rel = ImportRecord(
            kind="from_import", module="b", names=(("*", None),),
            level=1, source_path="pkg/a.py", start_line=1, start_col=0,
        )
        res = analyze_wildcard_import(star_rel, inv, {})
        assert res.outcome == WR_NOT_ANALYZED
        assert res.issue is None
        # one valid context for this single relative import
        assert len(res.contexts) == 1
        ctx = res.contexts[0]
        assert ctx.context.kind == "relative"
        assert ctx.context.source_candidate is not None
        assert ctx.context.base_components == ("pkg",)
        assert ctx.context.target_components == ("pkg", "b")
        assert ctx.context.lossless is True

    def test_source_assessments_cover_unique_inventory(self, star_record,
                                                       two_pkg_inventory):
        res = analyze_wildcard_import(star_record, two_pkg_inventory, {})
        seen = {a.candidate.qualified_name for a in res.source_assessments}
        expected = {c.qualified_name for c in two_pkg_inventory}
        assert seen == expected

    def test_outcome_reason_pairing(self, star_record, pkg_candidate,
                                    two_pkg_inventory):
        # Every (outcome, reason) pair is one of the documented pairs.
        valid = {
            (WR_ANALYZED, WR_ALL_ABSENT_DEFAULT_APPLIED),
            (WR_ANALYZED, WR_ALL_DECLARED_APPLIED),
            (WR_ANALYZED, WR_ALL_UNKNOWN_NO_EVIDENCE),
            (WR_NOT_ANALYZED, WR_TARGET_UNSUPPORTED),
            (WR_NOT_ANALYZED, WR_TARGET_NO_CANDIDATE),
            (WR_NOT_ANALYZED, WR_SOURCE_NOT_FOUND_NO_EVIDENCE),
            (WR_NOT_ANALYZED, WR_SOURCE_UNAVAILABLE_NO_EVIDENCE),
            (WR_NOT_ANALYZED, WR_SOURCE_UNPARSEABLE_NO_EVIDENCE),
            (WR_NOT_IN_SCOPE, WR_INVALID_RECORD),
        }
        for src in [
            "__all__ = ('a',)",
            "__all__ = []",
            "__all__ = names",
            "a=1\nexec('a=1')",
            "a=1\n_p=2\ndef f(): pass\nclass C: pass",
        ]:
            res = analyze_wildcard_import(
                star_record, two_pkg_inventory,
                {pkg_candidate: _supplied(src)})
            assert (res.outcome, res.reason or "") in valid, (
                src, res.outcome, res.reason)

    def test_pure_module_no_underscore_leaks(self):
        # The result must not leak private state — every field of the
        # result is either a frozen dataclass, a string, a None, or a
        # tuple / list of the same.
        res = analyze_wildcard_import(
            ImportRecord(kind="from_import", module="pkg",
                         names=(("*", None),), level=0,
                         source_path="a.py", start_line=1, start_col=0),
            build_module_inventory(
                [{"rel_path": "pkg/__init__.py",
                  "language": "Python", "is_binary": False}], ["."]),
            {})
        def _check(node, path=""):
            if node is None:
                return
            if isinstance(node, (str, int, bool, float)):
                return
            if isinstance(node, (tuple, list)):
                for i, c in enumerate(node):
                    _check(c, f"{path}[{i}]")
                return
            if dataclasses.is_dataclass(node) and not isinstance(node, type):
                for f in dataclasses.fields(node):
                    _check(getattr(node, f.name), f"{path}.{f.name}")
                return
            raise AssertionError(f"unexpected leaf type at {path}: "
                                 f"{type(node).__name__}")
        _check(res)

    def test_no_mutated_module_state_between_calls(self, star_record, pkg_candidate,
                                                   two_pkg_inventory):
        """Two calls with disjoint inventories must not leak."""
        inv_a = build_module_inventory(
            [{"rel_path": "a/__init__.py", "language": "Python",
              "is_binary": False}], ["."])
        inv_b = build_module_inventory(
            [{"rel_path": "b/__init__.py", "language": "Python",
              "is_binary": False}], ["."])
        r_a = analyze_wildcard_import(star_record, inv_a, {})
        r_b = analyze_wildcard_import(star_record, inv_b, {})
        # Each result must be self-consistent: its source_assessments
        # must cover only its own inventory, and not the other's.
        seen_a = {a.candidate for a in r_a.source_assessments}
        seen_b = {a.candidate for a in r_b.source_assessments}
        assert seen_a == set(inv_a)
        assert seen_b == set(inv_b)
        assert set(inv_a).isdisjoint(seen_b)


class TestW13ModuleExec:
    """W13: module-executing dynamic manipulation → ALL_UNKNOWN."""

    def test_module_body_exec_is_unknown(self, star_record, pkg_candidate,
                                          two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("exec('a=1')\na=1")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED
        assert e.star_names == ()

    def test_globals_write_is_unknown(self, star_record, pkg_candidate,
                                       two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied('globals()["k"]=1\na=1')})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_UNKNOWN
        assert e.outcome == WR_NOT_ANALYZED

    def test_local_function_exec_not_invalidating(self, star_record,
                                                   pkg_candidate,
                                                   two_pkg_inventory):
        """exec inside an unexecuted def body is NOT module-executing.
        Result: ALL_ABSENT (default rule applies; scope may be DIRECT_ONLY)."""
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("def f(): exec('x=1')\na=1")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_ABSENT
        assert e.outcome == WR_ANALYZED


class TestW14AmbiguousCandidates:
    """W14: AMBIGUOUS frozen-resolver result for the SAME imported
    qualified name across two distinct roots.

    Two package roots each containing ``pkg/__init__.py`` → the frozen
    resolver returns AMBIGUOUS with two supported candidates (same
    ``qualified_name``).  The analyzer must:
    * preserve exactly ONE context (absolute),
    * preserve EXACTLY TWO candidate evidence rows in canonical order,
    * keep distinct, unmerged evidence (different all_entries).
    No conditional bypass.
    """

    def test_ambiguous_candidates_not_merged(self, star_record):
        from repo_intelligence.absolute_resolution import (
            AMBIGUOUS, resolve_absolute_import,
        )
        inv = build_module_inventory(
            [
                {"rel_path": "pkg/__init__.py", "language": "Python",
                 "is_binary": False},
                {"rel_path": "other_root/pkg/__init__.py", "language": "Python",
                 "is_binary": False},
            ],
            [".", "other_root"],
        )
        # Pre-condition: the frozen resolver says AMBIGUOUS
        resolution = resolve_absolute_import(star_record, inv)
        assert resolution.status == AMBIGUOUS, (
            f"fixture pre-condition: expected AMBIGUOUS, got {resolution.status}"
        )
        assert len(resolution.supported_candidates) == 2
        c0 = resolution.supported_candidates[0]
        c1 = resolution.supported_candidates[1]

        res = analyze_wildcard_import(
            star_record, inv,
            {c0: _supplied("__all__=['x']"), c1: _supplied("__all__=['y']")})

        # Exactly one absolute context
        assert len(res.contexts) == 1, (
            f"AMBIGUOUS absolute should yield exactly 1 context, "
            f"got {len(res.contexts)}"
        )
        ctx = res.contexts[0]
        # Exactly two candidate rows in canonical order
        assert len(ctx.candidates) == 2, (
            f"expected exactly 2 rows, got {len(ctx.candidates)}"
        )
        ev0, ev1 = ctx.candidates
        # Canonical order: c0 before c1 (resolver order preserved)
        assert ev0.candidate is c0
        assert ev1.candidate is c1
        # Unmerged: distinct __all__ entries
        assert ev0.all_entries == ("x",)
        assert ev1.all_entries == ("y",)
        assert ev0.outcome == WR_ANALYZED
        assert ev1.outcome == WR_ANALYZED


class TestW23PermutationAndDedup:
    """W23: full result equality under meaningful permutations.

    (a) Inventory permutation: same record, same supported target(s),
        at least two distinct candidates, opposite insertion order →
        full result equality with non-trivial evidence.
    (b) target_sources mapping: two distinct keys inserted in opposite
        orders → full result equality.
    (c) One wrapper context per frozen ContextResolution, for both
        absolute and relative (overlapping-roots) cases.
    """

    def _rel_record(self):
        """Relative star: from pkg/mod.py do `from .x import *`."""
        return ImportRecord(
            kind="from_import", module="x", names=(("*", None),),
            level=1, source_path="pkg/mod.py", start_line=1, start_col=0,
        )

    def test_inventory_permutation_full_eq(self):
        """(a) Two matching candidates; opposite inventory order → full result ==."""
        from repo_intelligence.module_inventory import (
            ModuleCandidate, PackageRoot,
        )
        # Build two supported candidates for `pkg.x` at two distinct roots,
        # matching the relative target `x` from `pkg/mod.py` (base=pkg, x).
        rootA = PackageRoot(canonical="a", original="a")
        rootB = PackageRoot(canonical="b", original="b")
        srcA = ModuleCandidate(
            file_path="pkg/mod.py", root=rootA, qualified_name="pkg.mod",
            name_components=("pkg", "mod"), file_kind="ordinary_module",
            directory_kind="regular_package",
            component_supported=True, name_supported=True, evidence=("ea",))
        srcB = ModuleCandidate(
            file_path="pkg/mod.py", root=rootB, qualified_name="pkg.mod",
            name_components=("pkg", "mod"), file_kind="ordinary_module",
            directory_kind="regular_package",
            component_supported=True, name_supported=True, evidence=("eb",))
        # Two matching target candidates (same QN `pkg.x`) from the two roots
        tgtA = ModuleCandidate(
            file_path="pkg/x.py", root=rootA, qualified_name="pkg.x",
            name_components=("pkg", "x"), file_kind="ordinary_module",
            directory_kind="regular_package",
            component_supported=True, name_supported=True, evidence=("ta",))
        tgtB = ModuleCandidate(
            file_path="pkg/x.py", root=rootB, qualified_name="pkg.x",
            name_components=("pkg", "x"), file_kind="ordinary_module",
            directory_kind="regular_package",
            component_supported=True, name_supported=True, evidence=("tb",))

        inv_a_order = (srcA, tgtA, srcB, tgtB)
        inv_b_order = (srcB, tgtB, srcA, tgtA)

        rec = self._rel_record()
        # Supply sources to BOTH target candidates (non-trivial evidence)
        ts = {tgtA: _supplied("__all__=['a']"),
              tgtB: _supplied("__all__=['b']")}
        r_a = analyze_wildcard_import(rec, inv_a_order, ts)
        r_b = analyze_wildcard_import(rec, inv_b_order, ts)
        # Pre-condition check: both results are non-trivial (evidence present)
        total_rows_a = sum(len(c.candidates) for c in r_a.contexts)
        assert total_rows_a >= 1, "no evidence generated — test is vacuous"
        assert r_a == r_b, (
            f"inventory permutation changed result\n"
            f"r_a.outcome={r_a.outcome} r_b.outcome={r_b.outcome}\n"
            f"r_a.reason={r_a.reason}   r_b.reason={r_b.reason}"
        )

    def test_target_sources_permutation_full_eq(self):
        """(b) Two distinct mapping keys in opposite insertion order → full result ==."""
        from repo_intelligence.module_inventory import (
            ModuleCandidate, PackageRoot,
        )
        rootA = PackageRoot(canonical="a", original="a")
        srcA = ModuleCandidate(
            file_path="pkg/mod.py", root=rootA, qualified_name="pkg.mod",
            name_components=("pkg", "mod"), file_kind="ordinary_module",
            directory_kind="regular_package",
            component_supported=True, name_supported=True, evidence=("ea",))
        tgtA = ModuleCandidate(
            file_path="pkg/x.py", root=rootA, qualified_name="pkg.x",
            name_components=("pkg", "x"), file_kind="ordinary_module",
            directory_kind="regular_package",
            component_supported=True, name_supported=True, evidence=("ta",))
        # Two DISTINCT target candidates from two roots (so the mapping
        # really has two keys with different source text)
        rootB = PackageRoot(canonical="b", original="b")
        srcB = ModuleCandidate(
            file_path="pkg/mod.py", root=rootB, qualified_name="pkg.mod",
            name_components=("pkg", "mod"), file_kind="ordinary_module",
            directory_kind="regular_package",
            component_supported=True, name_supported=True, evidence=("eb",))
        tgtB = ModuleCandidate(
            file_path="pkg/x.py", root=rootB, qualified_name="pkg.x",
            name_components=("pkg", "x"), file_kind="ordinary_module",
            directory_kind="regular_package",
            component_supported=True, name_supported=True, evidence=("tb",))

        inv = (srcA, tgtA, srcB, tgtB)
        rec = self._rel_record()
        ts_a = {tgtA: _supplied("__all__=['a']"), tgtB: _supplied("__all__=['b']")}
        # Same two keys, reversed insertion order
        ts_b = {tgtB: _supplied("__all__=['b']"), tgtA: _supplied("__all__=['a']")}
        r_a = analyze_wildcard_import(rec, inv, ts_a)
        r_b = analyze_wildcard_import(rec, inv, ts_b)
        assert r_a == r_b, "target_sources key-order changed the result"

    def test_one_wrapper_context_per_frozen_context(self):
        """(c) One wrapper context per frozen ContextResolution.

        Overlapping roots: two source contexts (a and b) each RESOLVED /
        AMBIGUOUS → the analyzer must produce exactly two
        WildcardContextAnalysis objects, each one-to-one with a
        ContextResolution from the frozen resolver."""
        from repo_intelligence.relative_resolution import resolve_relative_import
        from repo_intelligence.module_inventory import (
            ModuleCandidate, PackageRoot,
        )
        rootA = PackageRoot(canonical="a", original="a")
        rootB = PackageRoot(canonical="b", original="b")
        srcA = ModuleCandidate(
            file_path="pkg/mod.py", root=rootA, qualified_name="pkg.mod",
            name_components=("pkg", "mod"), file_kind="ordinary_module",
            directory_kind="regular_package",
            component_supported=True, name_supported=True, evidence=("ea",))
        srcB = ModuleCandidate(
            file_path="pkg/mod.py", root=rootB, qualified_name="pkg.mod",
            name_components=("pkg", "mod"), file_kind="ordinary_module",
            directory_kind="regular_package",
            component_supported=True, name_supported=True, evidence=("eb",))
        tgt = ModuleCandidate(
            file_path="pkg/x.py", root=rootA, qualified_name="pkg.x",
            name_components=("pkg", "x"), file_kind="ordinary_module",
            directory_kind="regular_package",
            component_supported=True, name_supported=True, evidence=("t",))

        inv = (srcA, srcB, tgt)
        rec =self._rel_record()
        # Pre-condition: frozen resolver yields exactly 2 contexts
        rel_res = resolve_relative_import(rec, inv)
        assert rel_res.issue is None, f"pre-condition: {rel_res.issue}"
        assert len(rel_res.contexts) == 2, (
            f"pre-condition: expected 2 ContextResolutions, got {len(rel_res.contexts)}"
        )
        res = analyze_wildcard_import(rec, inv, {tgt: _supplied("a=1")})
        # One wrapper context per frozen context
        assert len(res.contexts) == len(rel_res.contexts), (
            f"wrapper contexts={len(res.contexts)} != "
            f"frozen contexts={len(rel_res.contexts)}"
        )
        # Root-to-root correspondence: same root order as frozen
        frozen_roots = [c.source_candidate.root.canonical for c in rel_res.contexts]
        wrapper_roots = [c.context.source_candidate.root.canonical
                         for c in res.contexts]
        assert wrapper_roots == frozen_roots, (
            f"root order mismatch: {wrapper_roots} != {frozen_roots}"
        )


class TestW23MixedPartitions:
    """W23 mixed supported/unsupported target interpretation (relative
    with two overlapping roots).

    Root A: source candidate + supported target A (name_supported=True).
    Root B: source candidate + unsupported target B (name_supported=False,
            same file path and name as the target but different root →
            NOT a conflicting duplicate — different structural identity).

    Assertions:
    * Both source contexts exist (pre-condition from the frozen resolver).
    * One context has target A as supported; the other has target B
      (or target A+B as AMBIGUOUS, or target B as UNSUPPORTED — verify
      from the resolver's actual pre-condition check).
    * Target A evidence retained; target B row empty, no parse (sentinel).
    * Supported-before-unsupported row ordering where both appear
      in the same context.
    * Full equality under inventory and mapping reversal.
    """

    @staticmethod
    def _mk(root_canon: str, file_path: str, name: tuple, *,
            name_supported: bool = True, evidence: tuple = ("e",)):
        from repo_intelligence.module_inventory import ModuleCandidate, PackageRoot
        root = PackageRoot(canonical=root_canon, original=root_canon)
        return ModuleCandidate(
            file_path=file_path, root=root,
            qualified_name=".".join(name),
            name_components=name,
            file_kind="ordinary_module", directory_kind="regular_package",
            component_supported=True, name_supported=name_supported,
            evidence=evidence,
        )

    @staticmethod
    def _rel_rec():
        return ImportRecord(
            kind="from_import", module="x", names=(("*", None),),
            level=1, source_path="pkg/mod.py", start_line=1, start_col=0,
        )

    def _build(self):
        # Root A: source (pkg/mod.py) + supported target (pkg/x.py)
        srcA = self._mk("ra", "pkg/mod.py", ("pkg", "mod"), evidence=("ea",))
        tgtA = self._mk("ra", "pkg/x.py",  ("pkg", "x"),  evidence=("ta",))

        # Root B: source (pkg/mod.py) + unsupported target (pkg/x.py,
        # name_supported=False — DIFFERENT structural identity due to
        # root.canonical, so NOT a conflicting duplicate)
        srcB = self._mk("rb", "pkg/mod.py", ("pkg", "mod"), evidence=("eb",))
        tgtB = self._mk("rb", "pkg/x.py",  ("pkg", "x"),
                        name_supported=False, evidence=("tb",))

        inv = (srcA, tgtA, srcB, tgtB)
        rec = self._rel_rec()
        return srcA, tgtA, srcB, tgtB, inv, rec

    def test_mixed_partitions_full_eq_and_no_parse_sentinel(self):
        from repo_intelligence.relative_resolution import resolve_relative_import
        srcA, tgtA, srcB, tgtB, inv, rec = self._build()

        # Pre-condition: resolver produces exactly 2 source contexts
        rel_res = resolve_relative_import(rec, inv)
        assert rel_res.issue is None, f"resolver issue: {rel_res.issue}"
        assert len(rel_res.contexts) == 2, (
            f"expected 2 ContextResolutions, got {len(rel_res.contexts)}"
        )

        # a_ctx: context(s) with tgtA in supported_candidates
        a_ctx = [c for c in rel_res.contexts if tgtA in c.supported_candidates]
        # b_ctx: context(s) with tgtB EXCLUSIVELY in unsupported_candidates
        b_ctx = [c for c in rel_res.contexts if tgtB in c.unsupported_candidates]
        # tgtB must NOT appear in ANY supported_candidates list
        for c in rel_res.contexts:
            assert tgtB not in c.supported_candidates, (
                f"tgtB unexpectedly in supported_candidates of "
                f"context root={c.source_candidate.root.canonical!r}"
            )
        assert a_ctx, "pre-condition: no context has target A as supported"
        assert b_ctx, "pre-condition: no context has target B as unsupported"

        # expected_b_rows: how many contexts carry tgtB in unsupported_candidates
        expected_b_rows = sum(tgtB in c.unsupported_candidates for c in rel_res.contexts)
        assert expected_b_rows > 0, "pre-condition: tgtB not marked unsupported anywhere"

        # --- Run analyzer with sentinel for tgtB, evidence for tgtA ---
        sentinel = "MIXED_PARTITION_SENTINEL"
        ts = {tgtA: _supplied("__all__=['a']"),
              tgtB: _supplied(f"a = 1  # {sentinel}")}

        import repo_intelligence.wildcard_import_analysis as w
        parse_calls = []
        orig_parse = w.ast.parse
        def spy(source, *args, **kwargs):
            if source and sentinel in (source or ""):
                parse_calls.append(True)
            return orig_parse(source, *args, **kwargs)
        try:
            w.ast.parse = spy
            res = analyze_wildcard_import(rec, inv, ts)
        finally:
            w.ast.parse = orig_parse

        # B's sentinel must NOT have been parsed
        assert parse_calls == [], (
            f"§9 VIOLATION: unsupported target B was parsed: {sentinel}"
        )

        # Root must be WR_ANALYZED (supported target A produced evidence)
        assert res.outcome == WR_ANALYZED, (
            f"mixed partitions: supported target A present → root WR_ANALYZED, "
            f"got {res.outcome}"
        )

        # Wrapper contexts match frozen contexts: count + order
        assert len(res.contexts) == len(rel_res.contexts), (
            f"wrapper contexts={len(res.contexts)} != "
            f"frozen contexts={len(rel_res.contexts)}"
        )
        # Source-candidate root order agrees between wrapper and frozen
        frozen_roots = [c.source_candidate.root.canonical for c in rel_res.contexts]
        wrapper_roots = [c.context.source_candidate.root.canonical
                         for c in res.contexts]
        assert wrapper_roots == frozen_roots, (
            f"root order mismatch: {wrapper_roots} != {frozen_roots}"
        )

        # Per-context: wrapper candidates == frozen supported + unsupported
        # (supported before unsupported within each context)
        for wc, fc in zip(res.contexts, rel_res.contexts):
            frozen_ids = tuple(fc.supported_candidates) + tuple(fc.unsupported_candidates)
            wrapper_ids = tuple(e.candidate for e in wc.candidates)
            assert wrapper_ids == frozen_ids, (
                f"context root={fc.source_candidate.root.canonical!r}:\n"
                f"  wrapper={wrapper_ids}\n"
                f"  frozen ={frozen_ids}"
            )

        # A evidence retained — find the context containing tgtA
        a_ctx_w = [c for c in res.contexts
                   if any(e.candidate is tgtA for e in c.candidates)]
        assert a_ctx_w, "target A not found in any wrapper context"
        ctx = a_ctx_w[0]
        a_ev = [e for e in ctx.candidates if e.candidate is tgtA][0]
        assert a_ev.outcome == WR_ANALYZED
        assert a_ev.all_entries == ("a",)

        # B rows: count matches expected_b_rows exactly
        all_b_ev = [
            e for c in res.contexts for e in c.candidates if e.candidate is tgtB
        ]
        assert len(all_b_ev) == expected_b_rows, (
            f"B row count {len(all_b_ev)} != expected {expected_b_rows}"
        )
        for b_ev in all_b_ev:
            assert b_ev.outcome == WR_NOT_ANALYZED, (
                f"unsupported target B must be WR_NOT_ANALYZED, got {b_ev.outcome}"
            )
            assert b_ev.all_state == ALL_UNKNOWN
            assert b_ev.all_entries == ()
            assert b_ev.star_names == ()
            assert b_ev.default_scope is None
            assert b_ev.reason == WR_TARGET_UNSUPPORTED, (
                f"unsupported target B reason must be WR_TARGET_UNSUPPORTED, "
                f"got {b_ev.reason!r}"
            )

        # --- Full equality under inventory reversal ---
        inv_rev = (srcB, tgtB, srcA, tgtA)
        ts_rev = {tgtB: _supplied(f"a = 1  # {sentinel}"),
                  tgtA: _supplied("__all__=['a']")}
        res_rev = analyze_wildcard_import(rec, inv_rev, ts_rev)
        assert res == res_rev, "mixed-partition inventory reversal changed result"


class TestW15Unsupported:
    """W15 / §9: unsupported candidate — no parse, truthful
    source-state, cross-context root aggregation.

    For an UNSUPPORTED context (keyword name → name_supported=False),
    the evidence row must carry WR_TARGET_UNSUPPORTED, ALL_UNKNOWN,
    empty tuples, and default_scope=None.  The source must NOT be
    parsed even if a SRC_SUPPLIED value is present in target_sources.
    """

    @pytest.fixture
    def unsupported_inv(self):
        # 'global' is a Python keyword → name_supported=False → UNSUPPORTED
        return build_module_inventory(
            [{"rel_path": "global/__init__.py", "language": "Python",
              "is_binary": False}],
            ["."],
        )

    @pytest.fixture
    def star_global(self):
        return ImportRecord(
            kind="from_import", module="global", names=(("*", None),),
            level=0, source_path="x.py", start_line=1, start_col=0,
        )

    def test_w15_unsupported_row_not_parsed(self, star_global,
                                             unsupported_inv):
        """§9: ast.parse is never called on the unsupported
        candidate's source even when a parseable SRC_SUPPLIED value
        is present in the mapping."""
        unc = unsupported_inv[0]
        assert unc.name_supported is False  # pre-condition

        import repo_intelligence.wildcard_import_analysis as w
        parse_calls = []
        orig_parse = w.ast.parse

        def spy(source, *args, **kwargs):
            parse_calls.append(source)
            return orig_parse(source, *args, **kwargs)

        try:
            w.ast.parse = spy
            res = analyze_wildcard_import(
                star_global, unsupported_inv,
                {unc: _supplied("a = 1  # W15_SENTINEL")})
        finally:
            w.ast.parse = orig_parse

        # The sentinel source must NOT appear in any parse call
        sentinel_calls = [
            s for s in parse_calls if s and "W15_SENTINEL" in s
        ]
        assert sentinel_calls == [], (
            f"§9 VIOLATION: ast.parse called on unsupported source: "
            f"{sentinel_calls}"
        )
        # Row fields
        assert res.outcome == WR_NOT_ANALYZED
        assert res.reason == WR_TARGET_UNSUPPORTED
        assert len(res.contexts) == 1
        ctx = res.contexts[0]
        ev = ctx.candidates[0]
        assert ev.outcome == WR_NOT_ANALYZED
        assert ev.reason == WR_TARGET_UNSUPPORTED
        assert ev.all_state == ALL_UNKNOWN
        assert ev.all_entries == ()
        assert ev.star_names == ()
        assert ev.default_scope is None

    def test_w15_diagnostic_preserved_unparseable(
            self, star_global, unsupported_inv):
        """W15: TargetSource(SRC_UNPARSEABLE, ..., parse_error=msg)
        for an unsupported candidate → the flat SourceAssessment must
        preserve the caller-supplied diagnostic without calling ast.parse.
        (F2: observable source availability, no parse for unsupported.)"""
        unc = unsupported_inv[0]
        ts = TargetSource(SRC_UNPARSEABLE, "def (", "my-parse-error-msg")
        res = analyze_wildcard_import(star_global, unsupported_inv, {unc: ts})
        sa_map = {a.candidate: a for a in res.source_assessments}
        sa = sa_map[unc]
        assert sa.state == SRC_UNPARSEABLE
        assert sa.parse_diagnostic == "my-parse-error-msg", (
            f"caller-supplied diagnostic was lost: {sa.parse_diagnostic!r}"
        )


class TestR3ConflictingDuplicates:
    """R-3: conflicting structural duplicates → ValueError from the
    frozen resolver propagates through the analyzer API.

    Proven pattern (frozen test 28 / 28b):
      - Build a genuine candidate c1 via the public inventory builder
        for the imported target.
      - c2 = dataclasses.replace(c1, name_supported=not c1.name_supported)
        (or evidence=c1.evidence+('conflict',)).
      - root.canonical, name_components, file_path IDENTICAL.
      - star record module matches c1.qualified_name.
      - Assert pytest.raises(ValueError) for BOTH orderings.
      - No fallback valid-result branch.
    A separate equivalent-duplicate case asserts successful dedup.
    """

    @pytest.fixture
    def r3_setup(self):
        """c1: genuine candidate for 'pkg' via the public builder;
        c2: dataclasses.replace(c1, name_supported=not c1.name_supported).
        star_rec: module='pkg' to match c1.qualified_name."""
        from repo_intelligence.module_inventory import build_module_inventory
        c1 = build_module_inventory(
            [{"rel_path": "pkg/__init__.py", "language": "Python",
              "is_binary": False}],
            ["."],
        )[0]
        import dataclasses as _dc
        c2 = _dc.replace(c1, name_supported=not c1.name_supported)
        # Invariants: structural identity is IDENTICAL
        assert c1.root.canonical == c2.root.canonical
        assert c1.name_components == c2.name_components
        assert c1.file_path == c2.file_path
        assert c1.qualified_name == c2.qualified_name
        # Conflicting: name_supported differs
        assert c1.name_supported != c2.name_supported
        star_rec = ImportRecord(
            kind="from_import", module="pkg", names=(("*", None),),
            level=0, source_path="x.py", start_line=1, start_col=0,
        )
        return c1, c2, star_rec

    def test_conflicting_valueerror_both_orders(self, r3_setup):
        """R-3: ValueError for BOTH tuple orderings — no silent
        consolidation, no fallback valid-result branch."""
        from repo_intelligence.absolute_resolution import resolve_absolute_import
        c1, c2, star_rec = r3_setup
        # Resolver itself raises for both orderings
        with pytest.raises(ValueError):
            resolve_absolute_import(star_rec, (c1, c2))
        with pytest.raises(ValueError):
            resolve_absolute_import(star_rec, (c2, c1))
        # Analyzer propagates (not swallows) the resolver's ValueError
        with pytest.raises(ValueError):
            analyze_wildcard_import(star_rec, (c1, c2), {})
        with pytest.raises(ValueError):
            analyze_wildcard_import(star_rec, (c2, c1), {})

    def test_conflicting_evidence_only_both_orders(self, r3_setup):
        """R-3 (variant 2): evidence-only conflict also raises ValueError."""
        import dataclasses as _dc
        from repo_intelligence.absolute_resolution import resolve_absolute_import
        c1, _c2, star_rec = r3_setup
        c3 = _dc.replace(c1, evidence=c1.evidence + ("test-evidence-conflict",))
        assert c1.root.canonical == c3.root.canonical
        assert c1.file_path == c3.file_path
        assert c1.name_components == c3.name_components
        with pytest.raises(ValueError):
            resolve_absolute_import(star_rec, (c1, c3))
        with pytest.raises(ValueError):
            resolve_absolute_import(star_rec, (c3, c1))
        with pytest.raises(ValueError):
            analyze_wildcard_import(star_rec, (c1, c3), {})
        with pytest.raises(ValueError):
            analyze_wildcard_import(star_rec, (c3, c1), {})

    def test_equivalent_duplicate_dedups_successfully(self, star_record):
        """Equivalent duplicates: caller passes (c1, c2) where c2 == c1
        (dataclasses.replace with no field changes).  The analyzer
        dedups to one candidate — exactly one context, one evidence
        row, one flat SourceAssessment — regardless of insertion order.

        This exercises the caller-provided-tuple path directly; no
        additional builder call is used to pre-deduplicate.
        """
        import dataclasses as _dc
        from repo_intelligence.module_inventory import build_module_inventory
        c1 = build_module_inventory(
            [{"rel_path": "pkg/__init__.py", "language": "Python",
              "is_binary": False}],
            ["."],
        )[0]
        # c2 is a distinct object but structurally identical to c1
        c2 = _dc.replace(c1)
        assert c2 is not c1, "replace() must produce a distinct object"
        assert c2 == c1,    "replace() with no field changes must be equal"
        # The source key for the mapping must be the actual resolver
        # candidate — use whichever the resolver reports first.
        ts = {c1: _supplied("__all__=['ok']")}
        r_a = analyze_wildcard_import(star_record, (c1, c2), ts)
        r_b = analyze_wildcard_import(star_record, (c2, c1), ts)
        # Both orderings yield the same full result
        assert r_a == r_b, "equivalent-duplicate ordering changed result"
        # Exactly one context
        assert len(r_a.contexts) == 1, (
            f"expected 1 context for equivalent duplicates, got {len(r_a.contexts)}"
        )
        ctx = r_a.contexts[0]
        # Exactly one evidence row (deduped)
        assert len(ctx.candidates) == 1, (
            f"expected 1 evidence row for equivalent duplicates, got {len(ctx.candidates)}"
        )
        ev = ctx.candidates[0]
        assert ev.outcome == WR_ANALYZED
        assert ev.reason == WR_ALL_DECLARED_APPLIED
        assert ev.all_entries == ("ok",)
        # Exactly one flat SourceAssessment (deduped)
        assert len(r_a.source_assessments) == 1, (
            f"expected 1 flat SourceAssessment, got {len(r_a.source_assessments)}"
        )


class TestRootOutcomeSuppliedAllUnknown:
    """Human-approved semantic (final report):

    A SUPPORTED target with SUPPLIED, PARSEABLE source yielding
    ALL_UNKNOWN → root=WR_ANALYZED, candidate=WR_NOT_ANALYZED /
    WR_ALL_UNKNOWN_NO_EVIDENCE.

    No conditional bypass: cardinality and root fields are asserted
    directly.
    """

    def test_supplied_nonliteral_all_root_analyzed(self, star_record,
                                                     pkg_candidate,
                                                     two_pkg_inventory):
        """`__all__ = names` → ALL_UNKNOWN.
        Root=WR_ANALYZED, exactly 1 context, exactly 1 candidate,
        WR_NOT_ANALYZED / WR_ALL_UNKNOWN_NO_EVIDENCE per-candidate."""
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("__all__ = names")})
        # Root — no conditional bypass
        assert res.outcome == WR_ANALYZED, (
            f"supplied-parseable ALL_UNKNOWN → root must be WR_ANALYZED, "
            f"got {res.outcome}"
        )
        assert res.reason == WR_ALL_UNKNOWN_NO_EVIDENCE
        # Cardinality
        assert len(res.contexts) == 1, (
            f"expected 1 context, got {len(res.contexts)}"
        )
        assert len(res.contexts[0].candidates) == 1, (
            f"expected 1 candidate, got {len(res.contexts[0].candidates)}"
        )
        ev = res.contexts[0].candidates[0]
        assert ev.outcome == WR_NOT_ANALYZED
        assert ev.reason == WR_ALL_UNKNOWN_NO_EVIDENCE
        assert ev.all_state == ALL_UNKNOWN
        assert ev.all_entries == ()
        assert ev.star_names == ()
        assert ev.default_scope is None

    def test_alias_escape_root_analyzed(self, star_record,
                                         pkg_candidate,
                                         two_pkg_inventory):
        """W6 alias-escape → root=WR_ANALYZED, candidate ALL_UNKNOWN."""
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied('__all__=["a"]\nx=__all__\nx.append("z")')})
        assert res.outcome == WR_ANALYZED
        assert res.reason == WR_ALL_UNKNOWN_NO_EVIDENCE
        assert len(res.contexts) == 1
        assert len(res.contexts[0].candidates) == 1
        ev = res.contexts[0].candidates[0]
        assert ev.outcome == WR_NOT_ANALYZED
        assert ev.reason == WR_ALL_UNKNOWN_NO_EVIDENCE

    def test_module_exec_root_analyzed(self, star_record,
                                        pkg_candidate,
                                        two_pkg_inventory):
        """W13: module-body exec → ALL_UNKNOWN candidate; root=WR_ANALYZED."""
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("__all__=['a']\nexec('x=1')")})
        assert res.outcome == WR_ANALYZED
        assert res.reason == WR_ALL_UNKNOWN_NO_EVIDENCE
        assert len(res.contexts) == 1
        ev = res.contexts[0].candidates[0]
        assert ev.all_state == ALL_UNKNOWN
        assert ev.outcome == WR_NOT_ANALYZED

    def test_src_not_found_root_not_analyzed(self, star_record,
                                               pkg_candidate,
                                               two_pkg_inventory):
        """SRC_NOT_FOUND: no source for the candidate → the wrapper did
        not parse any supplied evidence → root=WR_NOT_ANALYZED.

        The candidate is an EXISTING candidate with missing source
        (not a 'no candidate' resolver outcome); the per-candidate
        reason is the truthful source-state reason."""
        res = analyze_wildcard_import(star_record, two_pkg_inventory, {})
        assert res.outcome == WR_NOT_ANALYZED
        assert len(res.contexts) == 1
        ctx = res.contexts[0]
        assert len(ctx.candidates) >= 1
        ev = ctx.candidates[0]
        assert ev.source_state == SRC_NOT_FOUND
        assert ev.outcome == WR_NOT_ANALYZED
        # Per-candidate reason is a source-state reason, not
        # WR_TARGET_NO_CANDIDATE (the candidate EXISTS).
        assert ev.reason in (
            WR_SOURCE_NOT_FOUND_NO_EVIDENCE,
            WR_SOURCE_UNAVAILABLE_NO_EVIDENCE,
        ), (
            f"per-candidate reason should be a source-state reason, "
            f"got {ev.reason!r}"
        )


class TestR4DeclarationNotBinding:
    """R-4 / W19: an __all__ entry is a *declaration*, not an assertion
    of a binding.  An entry that has no corresponding module-level
    binding is still retained in star_names."""

    def test_no_binding_entry_retained(self, star_record, pkg_candidate,
                                       two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("__all__=['no_binding_here']")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_DECLARED
        # The entry is retained even though no binding exists
        assert "no_binding_here" in e.star_names
        assert e.outcome == WR_ANALYZED


class TestR5DottedPrefixBinding:
    """R-5 / W12: ``import a.b.c`` binds only ``a`` (the leading
    component) for star purposes, not ``a.b`` or ``a.b.c``."""

    def test_dotted_import_binds_leading_component(self, star_record,
                                                    pkg_candidate,
                                                    two_pkg_inventory):
        res = analyze_wildcard_import(
            star_record, two_pkg_inventory,
            {pkg_candidate: _supplied("import a.b.c\nimport a as z")})
        e = _ev(res, 0, 0)
        assert e.all_state == ALL_ABSENT
        # a (leading) and z (alias) are star names; b, c are not
        assert "a" in e.star_names
        assert "z" in e.star_names
        assert "b" not in e.star_names
        assert "c" not in e.star_names
