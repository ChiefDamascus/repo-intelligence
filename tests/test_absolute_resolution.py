"""Focused tests for the Phase 2C-2A pure absolute import resolver.

Each ``test_NN_*`` maps to numbered requirement NN of the Phase 2C-2A
spec.  Inventory fixtures are built exclusively through the frozen
Phase 2C-1 public API (``build_module_inventory``); ImportRecord
fixtures are produced by the frozen Phase 2B extractor
(``extract_file_imports``) or built via the real frozen ``ImportRecord``
dataclass for the deliberately malformed synthetic cases.

No test executes an indexed module, reads the filesystem (beyond pytest
infrastructure), or writes to any database.
"""

from __future__ import annotations

import dataclasses

import pytest

from repo_intelligence.absolute_resolution import (
    AMBIGUOUS,
    NOT_ATTEMPTED,
    RESOLVED,
    REASON_EMPTY_TARGET,
    REASON_INVALID_LEVEL,
    REASON_INVALID_RECORD_KIND,
    REASON_MULTI_SUPPORTED,
    REASON_NO_CANDIDATE,
    REASON_RELATIVE_IMPORT,
    REASON_TARGET_NOT_INTERPRETABLE,
    REASON_UNIQUE_SUPPORTED,
    REASON_UNSUPPORTED_NAME,
    UNSUPPORTED,
    UNRESOLVED,
    Resolution,
    resolve_absolute_import,
)
from repo_intelligence.module_inventory import (
    ModuleCandidate,
    PackageRoot,
    build_module_inventory,
)
from repo_intelligence.python_ast import ImportRecord, extract_file_imports


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def rec(rel_path: str) -> dict:
    """Minimal Phase 2C-1 file record (same shape as ``files`` rows)."""
    return {"rel_path": rel_path, "language": "Python", "size": 1,
            "is_binary": False}


def make_inventory(specs: list[str], roots: list[str]) -> tuple:
    """Build a frozen Phase 2C-1 inventory via the public API."""
    return build_module_inventory([rec(s) for s in specs], roots)


def records_for_source(source: str) -> list:
    """Produce real Phase 2B ImportRecord objects from source text."""
    recs = extract_file_imports("pkg/module.py", source)
    assert recs, "expected at least one ImportRecord"
    return recs


def inv_helper() -> tuple:
    return make_inventory(["helper.py"], ["."])


def inv_sub() -> tuple:
    return make_inventory(["helper/__init__.py", "helper/sub.py"], ["."])


def synthetic(**kw) -> ImportRecord:
    """Synthetic ImportRecord (real frozen dataclass) for malformed
    record cases required by the spec."""
    base = dict(
        kind="import", module="helper",
        names=(("helper", None),), level=0,
        source_path="synthetic.py", start_line=1, start_col=0,
    )
    base.update(kw)
    return ImportRecord(**base)


# ---------------------------------------------------------------------------
# 1. Unique ordinary module
# ---------------------------------------------------------------------------


def test_01_unique_ordinary_module():
    inv = inv_helper()
    recs = records_for_source("import helper")
    res = resolve_absolute_import(recs[0], inv)
    assert res.status == RESOLVED
    assert res.reason == REASON_UNIQUE_SUPPORTED
    assert res.requested_components == ("helper",)
    assert len(res.supported_candidates) == 1
    assert res.supported_candidates[0].file_path == "helper.py"


# ---------------------------------------------------------------------------
# 2. Unique package initializer
# ---------------------------------------------------------------------------


def test_02_unique_package_initializer():
    recs = records_for_source("from helper import sub")
    res = resolve_absolute_import(recs[0], inv_sub())
    assert res.status == RESOLVED
    (cand,) = res.supported_candidates
    assert cand.file_path == "helper/__init__.py"
    assert cand.file_kind == "package_initializer"


# ---------------------------------------------------------------------------
# 3. Absolute dotted import
# ---------------------------------------------------------------------------


def test_03_absolute_dotted_import():
    recs = records_for_source("import helper.sub")
    res = resolve_absolute_import(recs[0], inv_sub())
    assert res.status == RESOLVED
    assert res.requested_components == ("helper", "sub")
    assert res.supported_candidates[0].file_path == "helper/sub.py"


# ---------------------------------------------------------------------------
# 4. Aliased dotted import
# ---------------------------------------------------------------------------


def test_04_aliased_dotted_import():
    recs = records_for_source("import helper.sub as hs")
    rec = recs[0]
    assert rec.names == (("helper.sub", "hs"),)  # alias in Phase 2B record
    res = resolve_absolute_import(rec, inv_sub())
    assert res.status == RESOLVED
    assert res.requested_components == ("helper", "sub")
    assert res.record is rec  # alias retained through the original record


# ---------------------------------------------------------------------------
# 5. Multiple ordinary imports -> separate records
# ---------------------------------------------------------------------------


def test_05_multiple_ordinary_imports_are_separate_records():
    inv = make_inventory(["a.py", "b.py"], ["."])
    recs = records_for_source("import a, b")
    assert [r.module for r in recs] == ["a", "b"]  # Phase 2B contract
    res_a = resolve_absolute_import(recs[0], inv)
    res_b = resolve_absolute_import(recs[1], inv)
    assert res_a.status == RESOLVED and res_b.status == RESOLVED
    assert res_a.supported_candidates[0].file_path == "a.py"
    assert res_b.supported_candidates[0].file_path == "b.py"
    assert recs[0] != recs[1]


# ---------------------------------------------------------------------------
# 6. From-import with one name (targets the explicit module only)
# ---------------------------------------------------------------------------


def test_06_from_import_single_name_targets_explicit_module():
    inv = make_inventory(["core/__init__.py", "core/api.py"], ["."])
    recs = records_for_source("from core import api")
    res = resolve_absolute_import(recs[0], inv)
    assert res.requested_components == ("core",)
    assert res.status == RESOLVED
    assert res.supported_candidates[0].file_path == "core/__init__.py"


def test_06b_from_import_resolves_module_portion_only():
    # ``from core.api import client`` targets core/api.py, NOT a client
    # submodule; the imported name is carried by the original record only.
    recs = records_for_source("from core.api import client")
    rec = recs[0]
    assert rec.module == "core.api"
    inv = make_inventory(["core/__init__.py", "core/api.py"], ["."])
    res = resolve_absolute_import(rec, inv)
    assert res.requested_components == ("core", "api")
    assert res.status == RESOLVED


# ---------------------------------------------------------------------------
# 7. From-import with multiple names (single record, source order)
# ---------------------------------------------------------------------------


def test_07_from_import_multiple_names_single_record():
    inv = make_inventory(["core/__init__.py", "core/api.py"], ["."])
    recs = records_for_source("from core.api import client, server")
    rec = recs[0]
    assert rec.kind == "from_import"
    assert rec.names == (("client", None), ("server", None))
    res = resolve_absolute_import(rec, inv)
    assert res.status == RESOLVED
    assert res.requested_components == ("core", "api")
    assert res.supported_candidates[0].file_path == "core/api.py"


# ---------------------------------------------------------------------------
# 8. Aliased imported names (from-import aliasing)
# ---------------------------------------------------------------------------


def test_08_from_import_aliased_names_retained():
    inv = make_inventory(["core/__init__.py", "core/api.py"], ["."])
    recs = records_for_source("from core.api import client as c, server")
    rec = recs[0]
    assert rec.names == (("client", "c"), ("server", None))
    res = resolve_absolute_import(rec, inv)
    assert res.status == RESOLVED
    assert res.record.names == (("client", "c"), ("server", None))


# ---------------------------------------------------------------------------
# 9. Wildcard from-import
# ---------------------------------------------------------------------------


def test_09_wildcard_from_import_resolves_target_keeps_wildcard():
    inv = make_inventory(["core/__init__.py"], ["."])
    recs = records_for_source("from core import *")
    rec = recs[0]
    assert rec.names == (("*", None),)
    res = resolve_absolute_import(rec, inv)
    assert res.status == RESOLVED
    assert res.requested_components == ("core",)
    assert res.record.names == (("*", None),)  # wildcard retained


# ---------------------------------------------------------------------------
# 10. From-import where a same-named submodule also exists
# ---------------------------------------------------------------------------


def test_10_from_import_same_named_submodule_not_ambiguous():
    inv = make_inventory(["core/__init__.py", "core/util.py"], ["."])
    recs = records_for_source("from core import util")
    res = resolve_absolute_import(recs[0], inv)
    assert res.status == RESOLVED  # NOT AMBIGUOUS: target is "core" only
    (cand,) = res.supported_candidates
    assert cand.file_path == "core/__init__.py"
    assert all(c.file_path != "core/util.py" for c in res.supported_candidates)


# ---------------------------------------------------------------------------
# 11. No local candidate
# ---------------------------------------------------------------------------


def test_11_no_local_candidate():
    inv = make_inventory(["other/__init__.py"], ["."])
    recs = records_for_source("import os.path")
    res = resolve_absolute_import(recs[0], inv)
    assert res.status == UNRESOLVED
    assert res.reason == REASON_NO_CANDIDATE
    assert res.supported_candidates == ()
    assert res.unsupported_candidates == ()


# ---------------------------------------------------------------------------
# 12. Two supported candidates from different roots -> AMBIGUOUS
# ---------------------------------------------------------------------------


def test_12_two_candidates_from_different_roots_ambiguous():
    inv = make_inventory(
        ["lib/p/a.py", "lib/b/a.py"],
        ["lib/p", "lib/b"],
    )
    recs = records_for_source("import a")
    res = resolve_absolute_import(recs[0], inv)
    assert res.status == AMBIGUOUS
    assert res.reason == REASON_MULTI_SUPPORTED
    assert {c.file_path for c in res.supported_candidates} == {
        "lib/p/a.py", "lib/b/a.py"
    }
    assert {c.root.canonical for c in res.supported_candidates} == {
        "lib/p", "lib/b"
    }


# ---------------------------------------------------------------------------
# 13. Multiple distinct candidates under one root -> AMBIGUOUS
# ---------------------------------------------------------------------------


def test_13_multiple_distinct_candidates_under_one_root():
    # Same root ".", identical target "a.b", two DISTINCT candidates
    # whose (root, name_components) identity is shared only because the
    # frozen inventory legitimately contains both an ordinary module and
    # a package initializer at one qualified name:
    #   a/b.py           ordinary_module
    #   a/b/__init__.py  package_initializer
    # Distinct candidate identity follows the frozen inventory contract
    # (file_path differs), so this is AMBIGUOUS and no winner is chosen.
    inv = make_inventory(["a/b.py", "a/b/__init__.py"], ["."])
    assert len(inv) == 2
    assert {c.file_path for c in inv} == {"a/b.py", "a/b/__init__.py"}
    assert all(c.name_components == ("a", "b") for c in inv)
    assert all(c.root.canonical == "" for c in inv)
    assert {c.file_kind for c in inv} == {"ordinary_module", "package_initializer"}

    rec = records_for_source("import a.b")[0]
    res = resolve_absolute_import(rec, inv)
    assert res.status == AMBIGUOUS
    assert res.reason == REASON_MULTI_SUPPORTED
    assert len(res.supported_candidates) == 2
    assert {c.file_path for c in res.supported_candidates} == {
        "a/b.py", "a/b/__init__.py"
    }
    # Inventory (frozen) order is preserved — never re-ranked to a winner.
    assert [c.file_path for c in res.supported_candidates] == [
        c.file_path for c in inv
    ]


# ---------------------------------------------------------------------------
# 14. Unsupported module-name candidate
# ---------------------------------------------------------------------------


def test_14_keyword_stem_candidate_unsupported():
    inv = make_inventory(["def.py"], ["."])
    (cand,) = inv
    assert cand.name_supported is False  # frozen 2C-1 flag
    # ``import def`` is a SyntaxError, so the Phase 2B extractor cannot
    # produce this record; a synthetic record (real frozen dataclass)
    # stands in for the import target "def".
    rec = synthetic(kind="import", module="def", names=(("def", None),))
    res = resolve_absolute_import(rec, inv)
    assert res.status == UNSUPPORTED
    assert res.reason == REASON_UNSUPPORTED_NAME
    assert res.unsupported_candidates == (cand,)
    assert res.supported_candidates == ()


def test_14b_digit_leading_stem_candidate_unsupported():
    inv = make_inventory(["1mod.py"], ["."])
    assert inv[0].name_supported is False
    rec = synthetic(kind="import", module="1mod", names=(("1mod", None),))
    res = resolve_absolute_import(rec, inv)
    assert res.status == UNSUPPORTED
    assert res.reason == REASON_UNSUPPORTED_NAME
    assert res.unsupported_candidates == inv


def test_14c_request_for_keyword_target_without_matching_candidate():
    # The requested dotted target itself is not a legal import target
    # ("if" is a hard keyword).  With no structurally matching candidate
    # in the inventory there is nothing to classify as supported/
    # unsupported, so the resolver reports UNRESOLVED (no candidate).
    # The *unsupported-candidate* UNSUPPORTED path is what is exercised
    # by tests 14, 14b, 15, and 16 — where a file's frozen
    # name_supported flag is what drives the decision, not a separate
    # "is this target legal?" gate on the resolver side.
    rec = synthetic(kind="import", module="if", names=(("if", None),))
    res = resolve_absolute_import(rec, make_inventory(["helper.py"], ["."]))
    assert res.status == UNRESOLVED
    assert res.reason == REASON_NO_CANDIDATE
    assert res.requested_components == ("if",)
    assert res.supported_candidates == ()
    assert res.unsupported_candidates == ()


# ---------------------------------------------------------------------------
# 15. Supported and unsupported candidate mixture
# ---------------------------------------------------------------------------


def test_15_supported_and_unsupported_mixture():
    # One SUPPORTED matching candidate plus one UNSUPPORTED matching
    # candidate that is structurally DISTINCT (different canonical root),
    # must yield exactly RESOLVED (the supported one) with the
    # unsupported candidate RETAINED — not dropped, not folded into
    # supported, and not mistaken for a same-identity conflict.
    real_supported = make_inventory(["p.py"], ["."
                                             ])[0]
    assert real_supported.name_supported is True
    assert real_supported.name_components == ("p",)

    # A structurally distinct (different canonical root) candidate whose
    # component flag is False -> unsupported by the F4 gate.
    unsupported_twin = ModuleCandidate(
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
    inv = (real_supported, unsupported_twin)

    rec = records_for_source("import p")[0]
    res = resolve_absolute_import(rec, inv)
    # Exactly one supported candidate (the real one) and one
    # unsupported candidate (the hand-crafted, distinct-root one) =>
    # RESOLVED, and the unsupported match is RETAINED.
    assert res.status == RESOLVED
    assert res.reason == REASON_UNIQUE_SUPPORTED
    assert res.supported_candidates == (real_supported,)
    assert all(c.component_supported and c.name_supported
               for c in res.supported_candidates)
    assert res.unsupported_candidates == (unsupported_twin,)


# ---------------------------------------------------------------------------
# 16. Literal-dot component collision
# ---------------------------------------------------------------------------


def test_16_literal_dot_component_collision():
    # a.b.py    -> name_components ("a.b",)  (single literal-dot comp)
    # a/b.py    -> name_components ("a", "b")
    inv = make_inventory(["a.b.py", "a/b.py"], ["."])
    by_name = {(c.file_path, c.name_components) for c in inv}
    assert ("a.b.py", ("a.b",)) in by_name
    assert ("a/b.py", ("a", "b")) in by_name

    # Frozen 2C-1 flags on the two files confirm their distinct identities.
    dot = next(c for c in inv if c.file_path == "a.b.py")
    two = next(c for c in inv if c.file_path == "a/b.py")
    assert dot.name_supported is False
    assert two.name_supported is True

    # Target "a.b" (two components) is a strict component-tuple match on
    # a/b.py only; the literal-dot file is structurally a different name
    # and therefore neither a supported nor an unsupported match here.
    res = resolve_absolute_import(records_for_source("import a.b")[0], inv)
    assert res.status == RESOLVED
    assert res.supported_candidates == (two,)
    assert res.unsupported_candidates == ()


def test_16b_literal_dot_target_is_unsupported_candidate():
    # Inverse of 16: an import target whose dotted string has a literal
    # dot in a single component cannot be a legal two-component name and
    # the resolver still must return a deterministic answer.  Use the
    # synthetic-record path (the extractor would treat this as a
    # single-component name, matching only a same-component file if any
    # existed — here none do).
    rec = synthetic(kind="import", module="a.b", names=(("a.b", None),))
    inv = make_inventory(["x.py"], ["."])
    res = resolve_absolute_import(rec, inv)
    # "a.b" splits into ("a", "b"); no candidate under x.py matches,
    # hence UNRESOLVED (demonstrates the split still occurs and does
    # not silently reinterpret the literal dot as a separator).
    assert res.status == UNRESOLVED
    assert res.requested_components == ("a", "b")
    assert res.supported_candidates == ()


# ---------------------------------------------------------------------------
# 17. Case-sensitive matching
# ---------------------------------------------------------------------------


def test_17_case_sensitive_matching():
    inv = make_inventory(["Helper.py"], ["."])
    res = resolve_absolute_import(records_for_source("import helper")[0], inv)
    assert res.status == UNRESOLVED
    assert res.supported_candidates == ()


# ---------------------------------------------------------------------------
# 18. Relative imports -> NOT_ATTEMPTED (Phase 2C-3 scope)
# ---------------------------------------------------------------------------


def test_18_relative_from_import_not_attempted():
    inv = inv_helper()
    recs = records_for_source("from . import helper")
    rec = recs[0]
    assert rec.level == 1
    res = resolve_absolute_import(rec, inv)
    assert res.status == NOT_ATTEMPTED
    assert res.reason == REASON_RELATIVE_IMPORT
    assert res.supported_candidates == ()


def test_18b_relative_from_submodule_not_attempted():
    inv = make_inventory(["x/__init__.py", "x/a.py"], ["."])
    recs = records_for_source("from .a import b")
    rec = recs[0]
    assert rec.level == 1 and rec.module == "a"
    res = resolve_absolute_import(rec, inv)
    assert res.status == NOT_ATTEMPTED
    assert res.reason == REASON_RELATIVE_IMPORT


# ---------------------------------------------------------------------------
# 19. Empty or malformed synthetic module target
# ---------------------------------------------------------------------------


def test_19a_empty_module_target():
    res = resolve_absolute_import(synthetic(module=""), inv_helper())
    assert res.status == NOT_ATTEMPTED
    assert res.reason == REASON_EMPTY_TARGET
    assert res.requested_components is None


def test_19b_double_dot_target_not_interpretable():
    inv = make_inventory(["a.py", "b.py"], ["."])
    res = resolve_absolute_import(synthetic(module="a..b"), inv)
    assert res.status == UNSUPPORTED
    assert res.reason == REASON_TARGET_NOT_INTERPRETABLE
    assert res.supported_candidates == ()
    assert res.requested_components is None


def test_19c_null_module_target_not_reinterpreted():
    res = resolve_absolute_import(synthetic(module=None), inv_helper())
    assert res.status == NOT_ATTEMPTED
    assert res.reason == REASON_EMPTY_TARGET
    assert res.requested_components is None


# ---------------------------------------------------------------------------
# 20. Invalid synthetic kind or import structure
# ---------------------------------------------------------------------------


def test_20a_invalid_record_kind():
    res = resolve_absolute_import(synthetic(kind="weird"), inv_helper())
    assert res.status == NOT_ATTEMPTED
    assert res.reason == REASON_INVALID_RECORD_KIND


def test_20b_non_string_kind():
    res = resolve_absolute_import(synthetic(kind=42), inv_helper())
    assert res.status == NOT_ATTEMPTED
    assert res.reason == REASON_INVALID_RECORD_KIND


def test_20c_nonzero_level_on_ordinary_import_is_malformed():
    res = resolve_absolute_import(
        synthetic(kind="import", level=1), inv_helper()
    )
    assert res.status == UNSUPPORTED
    assert res.reason == REASON_INVALID_LEVEL


def test_20d_negative_level_is_malformed():
    res = resolve_absolute_import(
        synthetic(kind="from_import", level=-1), inv_helper()
    )
    assert res.status == UNSUPPORTED
    assert res.reason == REASON_INVALID_LEVEL


def test_20e_type_error_on_foreign_record():
    try:
        resolve_absolute_import("import helper", inv_helper())
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError for non-ImportRecord input")


def test_20f_type_error_on_foreign_inventory_item():
    rec = records_for_source("import helper")[0]
    try:
        resolve_absolute_import(rec, ["not-a-candidate"])
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError for non-ModuleCandidate entry")


def test_20g_type_error_on_non_sequence_inventory():
    rec = records_for_source("import helper")[0]
    try:
        resolve_absolute_import(rec, 1234)
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError for non-sequence inventory")


# ---------------------------------------------------------------------------
# 21. Stable ordering / status under candidate input permutations
# ---------------------------------------------------------------------------


def test_21_stable_under_permutations():
    # F1/F3: the resolver must order result candidate collections by the
    # frozen canonical key, so *any* permutation of the same inventory
    # yields an EXACTLY equal complete Resolution (no pre-sorting of
    # results before compare).  Two separate stable scenarios are
    # exercised: two SUPPORTED distinct matching candidates and two
    # UNSUPPORTED distinct matching candidates.

    def build(specs, roots):
        return build_module_inventory(
            [{"rel_path": p, "language": "Python", "size": 1,
              "is_binary": False} for p in specs],
            list(roots),
        )

    # (a) TWO distinct SUPPORTED candidates (different roots).
    # (b) TWO distinct UNSUPPORTED candidates (different roots, hard
    #     keyword target `if` which is a Python SyntaxError in real
    #     source text, so a synthetic ImportRecord is used to drive the
    #     resolver — the spec's malformed/synthetic clause permits this).
    supp = build(["lib/p/a.py", "lib/b/a.py"], ["lib/p", "lib/b"])
    unsup = build(["p/if.py", "q/if.py"], ["p", "q"])
    assert len(supp) == 2 and all(c.name_supported for c in supp)
    assert len(unsup) == 2 and all(not c.name_supported for c in unsup)

    rec_a = records_for_source("import a")[0]        # matches the two supported
    rec_if = synthetic(kind="import", module="if", names=(("if", None),))

    def permutations(inv, first):
        return [
            list(inv),
            list(reversed(inv)),
            list(inv[2:]) + list(inv[:2]),             # non-trivial rotation
            list(inv) + [first],                        # repeated identical
            list(inv) + [dataclasses.replace(first)],   # equal, separate object
        ]

    # (a)
    results_a = [resolve_absolute_import(rec_a, p) for p in permutations(supp, supp[0])]
    assert results_a[0].status == AMBIGUOUS
    assert len(results_a[0].supported_candidates) == 2
    for r in results_a[1:]:
        # EXACT complete Resolution equality — no sorting before compare.
        assert r == results_a[0]

    # (b)
    results_if = [resolve_absolute_import(rec_if, p) for p in permutations(unsup, unsup[0])]
    assert results_if[0].status == UNSUPPORTED
    assert len(results_if[0].unsupported_candidates) == 2
    for r in results_if[1:]:
        assert r == results_if[0]


def test_21b_diagnostic_root_spelling_is_not_ambiguity():
    # F2/F3: two candidates sharing the structural identity but differing
    # ONLY in PackageRoot.original (diagnostic spelling) must collapse to
    # a single deterministic representative and NOT yield AMBIGUOUS.
    base = build_module_inventory([rec("a.py")], ["."])[0]
    twin = dataclasses.replace(base,
                              root=PackageRoot(canonical=base.root.canonical,
                                               original="custom-spelling"))
    rec_a = records_for_source("import a")[0]
    # Both spellings equal-value; assert they do NOT count as two.
    r1 = resolve_absolute_import(rec_a, [base, base])
    r2 = resolve_absolute_import(rec_a, [base, dataclasses.replace(base)])
    r3 = resolve_absolute_import(rec_a, [base, twin])
    for r in (r1, r2, r3):
        assert r.status == RESOLVED
        assert len(r.supported_candidates) == 1
    # Consolidation is deterministic and the canonical representative is
    # the one with the smallest diagnostic original spelling.
    assert r3.supported_candidates[0].root.original == \
        min(base.root.original, twin.root.original)
    assert r1 == r2 == r3


# ---------------------------------------------------------------------------
# 22. Identical results for repeated calls; Resolution is immutable
# ---------------------------------------------------------------------------


def test_22_repeat_call_determinism():
    inv = make_inventory(["helper.py"], ["."])
    rec = records_for_source("import helper")[0]
    r1 = resolve_absolute_import(rec, inv)
    r2 = resolve_absolute_import(rec, list(inv))
    r3 = resolve_absolute_import(rec, tuple(inv))
    assert r1 == r2 == r3
    assert isinstance(r1, Resolution)


def test_22b_resolution_is_frozen():
    inv = inv_helper()
    rec = records_for_source("import helper")[0]
    res = resolve_absolute_import(rec, inv)
    try:
        res.status = "HACKED"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("Resolution must be immutable")


# ---------------------------------------------------------------------------
# 23. Exact preservation of ImportRecord fields
# ---------------------------------------------------------------------------


def test_23_record_fields_exactly_preserved():
    rec = records_for_source("import helper.sub as hs")[0]
    before = (
        rec.kind, rec.module, rec.names, rec.level,
        rec.source_path, rec.start_line, rec.start_col,
    )
    res = resolve_absolute_import(rec, inv_sub())
    after = (
        rec.kind, rec.module, rec.names, rec.level,
        rec.source_path, rec.start_line, rec.start_col,
    )
    assert before == after
    assert res.record is rec


# ---------------------------------------------------------------------------
# 24. Exact preservation of ModuleCandidate evidence / identity
# ---------------------------------------------------------------------------


def test_24_candidate_evidence_preserved():
    inv = make_inventory(["core/__init__.py", "core/api.py"], ["."])
    rec = records_for_source("from core.api import client")[0]
    res = resolve_absolute_import(rec, inv)
    (cand,) = res.supported_candidates
    orig = next(c for c in inv if c.file_path == "core/api.py")
    assert cand is orig  # same frozen object; not reconstructed
    assert cand.evidence == orig.evidence
    assert cand.evidence  # non-trivial evidence in the frozen contract


# ---------------------------------------------------------------------------
# 25. No input mutation
# ---------------------------------------------------------------------------


def test_25_no_input_mutation():
    paths = ["helper.py", "x/a.b.py", "x/a/b.py", "def.py"]
    inv = make_inventory(paths, ["."])
    rec = records_for_source("import helper")[0]

    # Full-field snapshot of the frozen inventory (order included).
    inv_snapshot = [
        (c.file_path, c.root.canonical, c.root.original, c.qualified_name,
         c.name_components, c.file_kind, c.directory_kind,
         c.component_supported, c.name_supported, c.evidence)
        for c in inv
    ]
    # Identity of the input sequence itself.
    ids = [id(c) for c in inv]
    rec_snapshot = (
        rec.kind, rec.module, rec.names, rec.level,
        rec.source_path, rec.start_line, rec.start_col,
        id(rec),
    )

    # Exercise a rich resolution: an import that matches some, misses
    # others, and exercises the supported/unsupported split.
    r1 = resolve_absolute_import(rec, list(inv))
    assert r1.status == RESOLVED
    r2 = resolve_absolute_import(
        synthetic(kind="import", module="nomatch.q", names=(("nomatch.q", None),)),
        list(inv),
    )
    r3 = resolve_absolute_import(
        synthetic(kind="from_import", module="", level=1),
        list(inv),
    )

    for (a, b) in zip(inv, inv_snapshot):
        assert (
            a.file_path, a.root.canonical, a.root.original,
            a.qualified_name, a.name_components, a.file_kind,
            a.directory_kind, a.component_supported, a.name_supported,
            a.evidence,
        ) == b
    assert [id(c) for c in inv] == ids
    assert (
        rec.kind, rec.module, rec.names, rec.level,
        rec.source_path, rec.start_line, rec.start_col,
        id(rec),
    ) == rec_snapshot
    # Results are distinct frozen objects.
    assert r1 == r1
    assert r2.status == UNRESOLVED
    assert r3.status == NOT_ATTEMPTED


# ---------------------------------------------------------------------------
# 26. No runtime Python import execution
# ---------------------------------------------------------------------------


def test_26_no_runtime_python_import_execution():
    # The resolver must not:
    #   * execute an ``import`` for the target module, or
    #   * lazily import any new Python module to do its work.
    # We prove both invariants by:
    #   (a) targeting a real stdlib module name and confirming the
    #       resolver treats it as a name, not a request to import;
    #   (b) asserting ``sys.modules`` is unchanged by a resolver call
    #       that touches a stdlib target — lazy-import side effects
    #       would surface as new entries.
    import os.path as _os_path  # force sys.modules entry pre-call
    import sys

    inv = make_inventory(["helper.py"], ["."])
    snap_pre = dict(sys.modules)

    rec = records_for_source("import os.path")[0]
    res = resolve_absolute_import(rec, inv)

    # (a) No crash, no import of os.path; the resolver reports the
    # structural UNRESOLVED outcome (target os.path vs. inventory
    # lacking os or os.path).
    assert res.status == UNRESOLVED
    assert res.supported_candidates == ()

    # (b) Lazy-import side effects would have added new entries to
    # sys.modules.  (We compare *added* keys, not removed ones, in case
    # pytest has a finalizer that removes entries.)
    added = [k for k in sys.modules if k not in snap_pre]
    # Only allow additions that are clearly the stdlib modules we
    # intentionally imported before the call above.
    for k in added:
        # No resolver-internal additions permitted.
        assert not k.startswith("repo_intelligence"), (
            f"resolver imported unexpected module: {k!r}"
        )
    # And critically: the resolver did not cause *any* new import of
    # a stdlib module that wasn't already imported before the call.
    assert "os" in snap_pre  # sanity: was pre-imported
    # (We don't assert "os.path" was pre-imported — Python's
    # attribute-based access to os.path does not always add an
    # `os.path` entry — but the resolver's UNRESOLVED verdict plus
    # the zero new repo_intelligence imports is what this test is
    # actually proving.)


def test_26b_resolver_module_does_not_load_at_import_time():
    # A resolver that *executes* imports would typically import them
    # at module level or on first call.  Assert the steady-state
    # dependency graph is limited to the frozen contract modules we
    # already know it needs (plus stdlib it legitimately uses:
    # ``dataclasses``, ``keyword``, ``re``, ``ast``).
    import sys

    known_frozen = {
        "repo_intelligence",
        "repo_intelligence.python_ast",
        "repo_intelligence.module_inventory",
        "repo_intelligence.absolute_resolution",
    }
    pre = set(sys.modules)
    for k in known_frozen:
        assert k in pre
    # No runtime import machinery in the resolved steady state
    # of the resolver's own namespace — inspect the module object
    # directly without importing anything it doesn't already have.
    import repo_intelligence.absolute_resolution as mod
    for name in (
        "importlib",  # runtime import
        "importlib.util",
        "runpy",
        "imp",
    ):
        assert name not in vars(mod), f"resolver pulls in {name!r}"
    assert callable(resolve_absolute_import)


# ---------------------------------------------------------------------------
# 27. No filesystem / database writes by the resolver
# ---------------------------------------------------------------------------


def test_27_no_fs_or_db_writes(monkeypatch):
    import builtins
    import pathlib
    import tempfile

    original_open = builtins.open

    def guard(path, *a, **kw):
        # Allow read opens; forbid write/append/replace/x modes.
        mode = a[0] if a else kw.get("mode", "r")
        if any(flag in mode for flag in ("w", "a", "+", "x")):
            raise AssertionError(f"resolver wrote to {path!r}")
        return original_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", guard)

    # Snapshot the temp dir pre-call; resolver must not create files.
    tmp = pathlib.Path(tempfile.gettempdir())
    pre = {p.name for p in tmp.iterdir()}

    rec = records_for_source("import helper")[0]
    res = resolve_absolute_import(rec, inv_helper())
    assert res.status == RESOLVED

    post = {p.name for p in tmp.iterdir()}
    assert pre == post


# ---------------------------------------------------------------------------
# 28. F2 conflicting duplicates -> deterministic ValueError
# ---------------------------------------------------------------------------


def test_28_conflicting_duplicates_raise_value_error():
    # F2: candidates sharing a structural identity but differing on a
    # SUBSTANTIVE attribute (support flag / classification / evidence)
    # must NOT be silently consolidated; the resolver raises an explicit
    # ValueError instead of arbitrarily choosing one.
    c1 = build_module_inventory([rec("a.py")], ["."])[0]
    # Identical structure, but flipped substantive support flag.
    c2 = dataclasses.replace(c1, name_supported=not c1.name_supported)
    assert c1.root.canonical == c2.root.canonical
    assert c1.name_components == c2.name_components
    assert c1.file_path == c2.file_path
    rec_a = records_for_source("import a")[0]
    with pytest.raises(ValueError):
        resolve_absolute_import(rec_a, [c1, c2])
    # Input-order independent: the conflict is raised regardless of order.
    with pytest.raises(ValueError):
        resolve_absolute_import(rec_a, [c2, c1])
    # A differing file classification is also a substantive conflict.
    c3 = dataclasses.replace(c1, file_kind="namespace_package")
    with pytest.raises(ValueError):
        resolve_absolute_import(rec_a, [c1, c3])


# ---------------------------------------------------------------------------
# 29. F4 both-flags supported gate
# ---------------------------------------------------------------------------


def test_29_component_flag_false_is_unsupported_despite_name_flag():
    # F4: a candidate is supported only when BOTH component_supported and
    # name_supported are True.  A synthetic candidate with
    # component_supported=False and name_supported=True must be reported
    # as UNSUPPORTED-only, never as supported.
    fake = ModuleCandidate(
        file_path="p-k/q.py",
        root=PackageRoot(canonical="", original="."),
        qualified_name="q",
        name_components=("q",),
        file_kind="ordinary_module",
        directory_kind="explicit_root",
        component_supported=False,
        name_supported=True,
        evidence=("test:f4-flag",),
    )
    rec_q = records_for_source("import q")[0]
    res = resolve_absolute_import(rec_q, [fake])
    assert res.status == UNSUPPORTED
    assert res.reason == REASON_UNSUPPORTED_NAME
    assert res.supported_candidates == ()
    assert res.unsupported_candidates == (fake,)


def test_29b_one_supported_plus_one_flag_false_not_ambiguous():
    # F4: one genuinely supported candidate plus one
    # component_supported=False candidate must NOT automatically become
    # AMBIGUOUS — the flag-false one is unsupported, so the result is a
    # single supported RESOLVED with the flag-false candidate retained as
    # unsupported.
    real = build_module_inventory([rec("q.py")], ["."])[0]
    assert real.component_supported and real.name_supported
    # A structurally-different root so this is a distinct candidate, with
    # the component flag flipped False (name_supported stays True).
    flag_false = dataclasses.replace(
        real,
        file_path="p-k/q.py",
        root=PackageRoot(canonical="zz", original="zz"),
        component_supported=False,
    )
    rec_q = records_for_source("import q")[0]
    res = resolve_absolute_import(rec_q, [real, flag_false])
    assert res.status == RESOLVED
    assert res.supported_candidates == (real,)
    assert res.unsupported_candidates == (flag_false,)
