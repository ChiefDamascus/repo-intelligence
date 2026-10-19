"""Phase 2C-3B-1: focused tests for named imported-item analysis.

Covers the corrected D1-D17 named-import matrix, binding corrections
F1-F6, the public result-cardinality contract, and the explicit
regression cases called out during review.

Deterministic; no new third-party dependencies.
"""
from __future__ import annotations

import ast
import dataclasses

import pytest

from repo_intelligence.absolute_resolution import (
    NOT_ATTEMPTED,
    REASON_EMPTY_TARGET,
    REASON_INVALID_RECORD_KIND,
    REASON_INVALID_LEVEL,
    RESOLVED,
    UNSUPPORTED,
    UNRESOLVED,
)
from repo_intelligence.imported_name_analysis import (
    DefinitionNameEvidence,
    ImportNameAnalysis,
    NameEvidence,
    OUTCOME_ANALYZED,
    OUTCOME_NOT_ANALYZED,
    OUTCOME_NOT_IN_SCOPE,
    NAME_IMPORT_FORM,
    NAME_INVALID_KIND,
    NAME_OK,
    NAME_TARGET_NO_CANDIDATE,
    NAME_TARGET_NOT_ATTEMPTED,
    NAME_TARGET_UNSUPPORTED,
    ReexportNameEvidence,
    RS_CLASS_BODY,
    RS_FUNCTION_BODY,
    RS_MODULE_CONDITIONAL,
    RS_MODULE_DIRECT,
    SourceAssessment,
    SRC_NOT_FOUND,
    SRC_SUPPLIED,
    SRC_UNAVAILABLE,
    SRC_UNPARSEABLE,
    SubmoduleNameEvidence,
    TargetContext,
    TargetNameAnalysis,
    TargetSource,
    TARGET_ABSOLUTE,
    TARGET_MIXED_ROUTING,
    TARGET_RELATIVE,
    analyze_named_import,
)
from repo_intelligence.module_inventory import (
    build_module_inventory,
)
from repo_intelligence.python_ast import ImportRecord


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def rec(
    p: str,
    *,
    kind: str = "from_import",
    module: str = "pkg",
    names=(),
    level: int = 0,
) -> ImportRecord:
    if not names:
        names = (("sub", None),)
    return ImportRecord(
        kind=kind,
        module=module,
        names=tuple(names),
        level=level,
        source_path=p,
        start_line=1,
        start_col=0,
    )


def inv(*paths: str, roots: list[str] | None = None) -> tuple:
    roots = roots if roots is not None else ["."]
    return build_module_inventory(
        [{"rel_path": p, "language": "Python", "is_binary": False} for p in paths],
        roots,
    )


def pick(inventory: tuple, *components: str):
    for c in inventory:
        if tuple(c.name_components) == tuple(components):
            return c
    raise LookupError(components)


def single_unit(res: ImportNameAnalysis, ctx_idx: int = 0, name_idx: int = 0) -> TargetNameAnalysis:
    return res.contexts[ctx_idx * 1 + name_idx]


# ---------------------------------------------------------------------------
# Public result-cardinality contract (Codex acceptance check)
# ---------------------------------------------------------------------------


def test_cardinality_one_context_two_occurrences():
    invd = inv("pkg/__init__.py", "pkg/other.py")
    pkg = pick(invd, "pkg")
    record = rec("a.py", module="pkg", names=(("sub", None), ("other", "oth")))
    res = analyze_named_import(record, invd, {})
    # absolute: C == 1, N == 2 -> len(contexts) == 2
    assert isinstance(res, ImportNameAnalysis)
    assert len(res.contexts) == 2
    for u in res.contexts:
        assert isinstance(u, TargetNameAnalysis)
        assert isinstance(u.name, NameEvidence)
        assert u.context_index == 0
    assert [u.name_index for u in res.contexts] == [0, 1]


def test_cardinality_absolute_two_candidates_two_occurrences_inner_tuples_preserved():
    # two distinct roots -> two candidates for the same target.
    # Each NameEvidence tuple must carry the candidate-scoped entries.
    invd = build_module_inventory(
        [
            {"rel_path": "alpha/pkg/__init__.py", "language": "Python", "is_binary": False},
            {"rel_path": "beta/pkg/__init__.py", "language": "Python", "is_binary": False},
            {"rel_path": "alpha/pkg/sub/__init__.py", "language": "Python", "is_binary": False},
            {"rel_path": "beta/pkg/sub/__init__.py", "language": "Python", "is_binary": False},
        ],
        ["alpha", "beta"],
    )
    record = rec("a.py", module="pkg", names=(("sub", None), ("sub", "S")))
    res = analyze_named_import(record, invd, {})
    assert len(res.contexts) == 2  # one context x two occurrences
    # Each unit: submodule tuple should have ONE child evidence per
    # candidate (so two total: one per candidate).
    for u in res.contexts:
        assert len(u.name.submodule) == 2
        subs = u.name.submodule
        roots = {s.submodule_candidate.root.canonical for s in subs}
        assert roots == {"alpha", "beta"}


def test_cardinality_relative_two_contexts_three_names_six_units():
    # Two DIFFERENT source interpretations of the SAME source file via
    # overlapping package roots (frozen relative resolver matches the
    # exact indexed file_path, then one context per package root).
    invd = build_module_inventory(
        [
            {"rel_path": "a/pkg/__init__.py", "language": "Python", "is_binary": False},
            {"rel_path": "a/pkg/client.py", "language": "Python", "is_binary": False},
            {"rel_path": "a/pkg/sub.py", "language": "Python", "is_binary": False},
            {"rel_path": "b/pkg/__init__.py", "language": "Python", "is_binary": False},
        ],
        [".", "a", "b"],
    )
    record = rec(
        "a/pkg/client.py",
        module="",
        names=(("a", None), ("b", "B"), ("c", None)),
        level=1,
    )
    # Validate the fixture against the frozen resolver first.
    from repo_intelligence.relative_resolution import resolve_relative_import
    frozen = resolve_relative_import(record, invd)
    assert frozen.issue is None
    assert len(frozen.contexts) == 2
    res = analyze_named_import(record, invd, {})
    # 2 frozen contexts x 3 names = 6 flat units.
    assert len(res.contexts) == 6
    assert all(isinstance(u, TargetNameAnalysis) for u in res.contexts)
    keys = [(u.context_index, u.name_index) for u in res.contexts]
    assert keys == [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]
    # Each embedded context must match the corresponding frozen context
    # (flat ordering groups context-0 first, then context-1).
    assert res.contexts[0].context.source_candidate is frozen.contexts[0].source_candidate
    assert res.contexts[3].context.source_candidate is frozen.contexts[1].source_candidate



# ---------------------------------------------------------------------------
# D-matrix named-import behavior (corrected D1-D17)
# ---------------------------------------------------------------------------


def test_D1_absolute_structural_no_source():
    # from pkg import sub ; pkg/__init__.py + pkg/sub/__init__.py child
    invd = inv("pkg/__init__.py", "pkg/sub/__init__.py")
    record = rec("a.py", module="pkg", names=(("sub", None),))
    res = analyze_named_import(record, invd, {})
    assert res.target_kind == TARGET_ABSOLUTE
    assert len(res.contexts) == 1
    unit = res.contexts[0]
    assert unit.name.outcome == OUTCOME_ANALYZED
    assert unit.name.reason == NAME_OK
    assert len(unit.name.submodule) == 1
    # F2: source NOT FOUND is still observable
    assert unit.context.assessments and unit.context.assessments[0].state == SRC_NOT_FOUND


def test_D2_absolute_definition_module_attribute():
    invd = inv("pkg/__init__.py", "pkg/sub/__init__.py")
    pkg = pick(invd, "pkg")
    ts = {pkg: TargetSource(SRC_SUPPLIED, "sub = 1\n", None)}
    res = analyze_named_import(rec("a.py", module="pkg", names=(("sub", None),)), invd, ts)
    unit = res.contexts[0]
    assert unit.name.outcome == OUTCOME_ANALYZED
    assert len(unit.name.definitions) == 1
    d = unit.name.definitions[0]
    assert d.kind == "module_attribute"
    assert d.symbol_kind == "assignment"
    assert d.line == 1
    assert unit.context.assessments[0].state == SRC_SUPPLIED


def test_D3_nested_definition_not_module_scope():
    invd = inv("pkg/__init__.py")
    pkg = pick(invd, "pkg")
    ts = {pkg: TargetSource(SRC_SUPPLIED, "def f():\n    sub = 1\n", None)}
    res = analyze_named_import(rec("a.py", module="pkg", names=(("sub", None),)), invd, ts)
    unit = res.contexts[0]
    assert len(unit.name.definitions) == 0


def test_D4_reexport_direct_module_scope():
    invd = inv("pkg/__init__.py", "pkg/sub/__init__.py")
    pkg = pick(invd, "pkg")
    ts = {pkg: TargetSource(SRC_SUPPLIED, "from . import sub\n", None)}
    res = analyze_named_import(rec("a.py", module="pkg", names=(("sub", None),)), invd, ts)
    unit = res.contexts[0]
    assert len(unit.name.reexports) == 1
    r = unit.name.reexports[0]
    assert r.symbol_kind == "from_import"
    assert r.provenance == RS_MODULE_DIRECT
    assert r.name == "sub"


def test_D5_reexport_conditional_module_scope():
    invd = inv("pkg/__init__.py")
    pkg = pick(invd, "pkg")
    ts = {pkg: TargetSource(SRC_SUPPLIED, "if COND:\n    from . import sub\n", None)}
    res = analyze_named_import(rec("a.py", module="pkg", names=(("sub", None),)), invd, ts)
    unit = res.contexts[0]
    assert len(unit.name.reexports) == 1
    assert unit.name.reexports[0].provenance == RS_MODULE_CONDITIONAL


def test_D6_nested_import_never_suppresses_structural():
    # from pkg import sub ; pkg/__init__.py has `def f(): from . import sub`
    # child pkg/sub/__init__.py exists -> structural evidence retained.
    invd = inv("pkg/__init__.py", "pkg/sub/__init__.py")
    pkg = pick(invd, "pkg")
    ts = {pkg: TargetSource(SRC_SUPPLIED, "def f():\n    from . import sub\n", None)}
    res = analyze_named_import(rec("a.py", module="pkg", names=(("sub", None),)), invd, ts)
    unit = res.contexts[0]
    assert len(unit.name.submodule) == 1          # F4: structural kept
    assert len(unit.name.reexports) == 0          # nested import not module-scope


def test_D8_dotted_import_never_reexports_Y():
    # import os.path binds os, never path
    invd = inv("pkg/__init__.py")
    pkg = pick(invd, "pkg")
    ts = {pkg: TargetSource(SRC_SUPPLIED, "import os.path\n", None)}
    res = analyze_named_import(rec("a.py", module="pkg", names=(("path", None),)), invd, ts)
    unit = res.contexts[0]
    assert len(unit.name.reexports) == 0
    # but 'os' WOULD be bound (control check)
    res_os = analyze_named_import(rec("a.py", module="pkg", names=(("os", None),)), invd, ts)
    assert len(res_os.contexts[0].name.reexports) == 1


def test_D9_alias_recorded_matching_on_original():
    invd = inv("pkg/__init__.py", "pkg/sub/__init__.py")
    pkg = pick(invd, "pkg")
    ts = {pkg: TargetSource(SRC_SUPPLIED, "from . import sub\n", None)}
    res = analyze_named_import(rec("a.py", module="pkg", names=(("sub", "s"),)), invd, ts)
    unit = res.contexts[0]
    assert unit.name.asname == "s"
    assert unit.name.name == "sub"
    assert len(unit.name.reexports) == 1


def test_D10_source_not_found_still_name_ok():
    invd = inv("pkg/__init__.py", "pkg/sub/__init__.py")
    res = analyze_named_import(rec("a.py", module="pkg", names=(("sub", None),)), invd, {})
    unit = res.contexts[0]
    assert unit.name.outcome == OUTCOME_ANALYZED
    assert unit.name.reason == NAME_OK
    assert len(unit.name.submodule) == 1


def test_D13_unsupported_target():
    # A non-module-name dotted segment (i-j) is UNSUPPORTED by the
    # frozen resolver -> NAME_TARGET_UNSUPPORTED.
    invd = build_module_inventory(
        [{"rel_path": "i-j/__init__.py", "language": "Python", "is_binary": False}], ["."]
    )
    record = rec("a.py", module="i-j", names=(("sub", None),))
    res = analyze_named_import(record, invd, {})
    unit = res.contexts[0]
    assert unit.context.status == UNSUPPORTED
    assert unit.name.outcome == OUTCOME_NOT_ANALYZED
    assert unit.name.reason == NAME_TARGET_UNSUPPORTED
    assert len(unit.name.definitions) == 0
    assert len(unit.name.reexports) == 0


def test_D14_unresolved_target():
    invd = inv("other/__init__.py")
    res = analyze_named_import(rec("a.py", module="missing", names=(("sub", None),)), invd, {})
    unit = res.contexts[0]
    assert unit.name.outcome == OUTCOME_NOT_ANALYZED
    assert unit.name.reason == NAME_TARGET_NO_CANDIDATE


def test_D15_import_form_out_of_scope():
    # D15: kind="import" -> NOT_IN_SCOPE / NAME_IMPORT_FORM
    record = rec("a.py", kind="import", module="pkg.sub", names=(("sub", None),))
    res = analyze_named_import(record, inv("pkg/__init__.py", "pkg/sub/__init__.py"), {})
    unit = res.contexts[0]
    assert unit.name.outcome == OUTCOME_NOT_IN_SCOPE
    assert unit.name.reason == NAME_IMPORT_FORM
    assert unit.name.submodule == () and unit.name.definitions == () and unit.name.reexports == ()


def test_D16_relative_single_source_context():
    # relative from pkg import sub (level 1) from src/mod.py
    invd = build_module_inventory(
        [
            {"rel_path": "src/mod.py", "language": "Python", "is_binary": False},
            {"rel_path": "src/pkg/__init__.py", "language": "Python", "is_binary": False},
            {"rel_path": "src/pkg/sub/__init__.py", "language": "Python", "is_binary": False},
        ], ["."]
    )
    record = rec("src/mod.py", module="pkg", names=(("sub", None),), level=1)
    res = analyze_named_import(record, invd, {})
    assert res.target_kind == TARGET_RELATIVE
    assert len(res.contexts) == 1
    unit = res.contexts[0]
    assert unit.name.outcome == OUTCOME_ANALYZED
    assert unit.name.reason == NAME_OK
    assert len(unit.name.submodule) == 1


def test_D17_relative_missing_source_context():
    # source candidate missing -> REL_MISSING_SOURCE_CONTEXT, contexts=()
    invd = build_module_inventory(
        [{"rel_path": "elsewhere/mod.py", "language": "Python", "is_binary": False}], ["."]
    )
    record = rec("src/mod.py", module="pkg", names=(("sub", None),), level=1)
    res = analyze_named_import(record, invd, {})
    assert res.contexts == ()
    assert "missing_source_context" in (res.issue or "")



# ---------------------------------------------------------------------------
# F1-F6 binding corrections
# ---------------------------------------------------------------------------


def test_F1_ordinary_module_is_not_a_package_parent():
    # a "child" that is a plain .py module under an ordinary .py parent
    # must NOT produce structural child evidence.
    invd = inv("pkg.py", "pkg/sub.py")  # pkg.py is an ordinary module, not a package
    res = analyze_named_import(rec("a.py", module="pkg", names=(("sub", None),)), invd, {})
    unit = res.contexts[0]
    assert len(unit.name.submodule) == 0


def test_F4_nested_import_never_suppresses_structural_evidence():
    # child present + nested import in source -> structural retained
    invd = inv("pkg/__init__.py", "pkg/sub/__init__.py")
    pkg = pick(invd, "pkg")
    ts = {pkg: TargetSource(SRC_SUPPLIED, "def f():\n    from . import sub\n", None)}
    res = analyze_named_import(rec("a.py", module="pkg", names=(("sub", None),)), invd, ts)
    assert len(res.contexts[0].name.submodule) == 1
    assert len(res.contexts[0].name.reexports) == 0


def test_F6_pure_analyzer_no_runtime_effects():
    # F6: the analyzer is pure/deterministic and makes NO filesystem,
    # database, network, or runtime import-execution calls. Set up all
    # fixtures BEFORE the traps, patch the I/O surfaces, then analyze
    # twice and assert no forbidden surface was touched.
    import builtins, contextlib, importlib, os, socket, sqlite3
    from pathlib import Path
    from unittest.mock import patch
    invd = inv("pkg/__init__.py", "pkg/sub.py")
    pkg = pick(invd, "pkg")
    record = rec("a.py", module="pkg", names=(("sub", None),))
    text = ('import module_that_must_never_be_loaded as sub\n'
            'sub = open("must_never_exist", "w")\n'
            'exec("raise RuntimeError()")\n'
            'eval("1/0")\n')
    supplied = TargetSource(SRC_SUPPLIED, text, None)
    calls = []
    def deny(name):
        def blocked(*args, **kwargs):
            calls.append(name)
            raise AssertionError("Forbidden runtime operation: " + name)
        return blocked
    surfaces = [
        (builtins, "open"), (os, "open"), (os, "stat"),
        (os, "listdir"), (os, "scandir"), (sqlite3, "connect"),
        (socket, "socket"), (importlib, "import_module"),
        (Path, "open"), (Path, "read_text"), (Path, "read_bytes"),
        (Path, "write_text"), (Path, "write_bytes"),
        (builtins, "eval"), (builtins, "exec"), (builtins, "__import__"),
    ]
    with contextlib.ExitStack() as stack:
        for owner, name in surfaces:
            stack.enter_context(patch.object(owner, name, deny(name)))
        result = analyze_named_import(record, invd, {pkg: supplied})
        repeated = analyze_named_import(record, invd, {pkg: supplied})
    assert calls == []
    assert result == repeated and result.record is record
    assert len(result.contexts[0].name.submodule) == 1
    assert len(result.contexts[0].name.definitions) == 1
    assert len(result.contexts[0].name.reexports) == 1


# ---------------------------------------------------------------------------
# Public API and result card
# ---------------------------------------------------------------------------


def test_result_record_identity_and_frozen():
    invd = inv("pkg/__init__.py", "pkg/sub/__init__.py")
    record = rec("a.py", module="pkg", names=(("sub", None),))
    res = analyze_named_import(record, invd, {})
    assert res.record is record
    # frozen objects
    with pytest.raises(Exception):
        res.contexts = ()  # type: ignore[misc]


def test_target_source_state_matrix():
    invd = inv("pkg/__init__.py", "pkg/sub/__init__.py")
    pkg = pick(invd, "pkg")
    record = rec("a.py", module="pkg", names=(("sub", None),))
    # SRC_SUPPLIED
    r = analyze_named_import(record, invd, {pkg: TargetSource(SRC_SUPPLIED, "a=1", None)})
    assert r.contexts[0].context.assessments[0].state == SRC_SUPPLIED
    # SRC_UNAVAILABLE
    r = analyze_named_import(record, invd, {pkg: TargetSource(SRC_UNAVAILABLE, None, None)})
    assert r.contexts[0].context.assessments[0].state == SRC_UNAVAILABLE
    # SRC_UNPARSEABLE (caller diagnostic preserved, even empty)
    r = analyze_named_import(record, invd, {pkg: TargetSource(SRC_UNPARSEABLE, "bad", "")})
    a = r.contexts[0].context.assessments[0]
    assert a.state == SRC_UNPARSEABLE and a.parse_diagnostic == ""
    # SRC_NOT_FOUND synthesized
    r = analyze_named_import(record, invd, {})
    assert r.contexts[0].context.assessments[0].state == SRC_NOT_FOUND


@pytest.mark.parametrize(
    "kwargs, expect_type_error",
    [
        (dict(provenance="Bogus", source_text=None, parse_error=None), True),
        (dict(provenance=SRC_SUPPLIED, source_text=None, parse_error=None), True),
        (dict(provenance=SRC_SUPPLIED, source_text="x", parse_error="e"), True),
        (dict(provenance=SRC_UNAVAILABLE, source_text="x", parse_error=None), True),
        (dict(provenance=SRC_UNPARSEABLE, source_text=None, parse_error="e"), True),
        (dict(provenance=SRC_UNPARSEABLE, source_text="x", parse_error=None), True),
        (dict(provenance=SRC_SUPPLIED, source_text="x=1", parse_error=None), False),
        (dict(provenance=SRC_UNAVAILABLE, source_text=None, parse_error=None), False),
        (dict(provenance=SRC_UNPARSEABLE, source_text="x", parse_error="err"), False),
    ],
)
def test_target_source_malformed_states(kwargs, expect_type_error):
    if expect_type_error:
        with pytest.raises(TypeError):
            TargetSource(**kwargs)
    else:
        TargetSource(**kwargs)


def test_target_source_none_mapping_rejected():
    invd = inv("pkg/__init__.py")
    record = rec("a.py", module="pkg", names=(("sub", None),))
    with pytest.raises(TypeError):
        analyze_named_import(record, invd, None)


def test_foreign_candidate_key_rejected():
    invd = inv("pkg/__init__.py")
    pkg = pick(invd, "pkg")
    foreign = dataclasses.replace(pkg, name_supported=False)
    record = rec("a.py", module="pkg", names=(("sub", None),))
    with pytest.raises(TypeError):
        analyze_named_import(
            record, invd, {foreign: TargetSource(SRC_UNAVAILABLE, None, None)}
        )


def test_import_form_level_variants_keep_frozen_envelope():
    invd = inv("pkg/__init__.py", "pkg/sub/__init__.py")
    for level in (1, -1, True, "1"):
        r = ImportRecord(
            kind="import", module="pkg.sub", names=(("sub", None),),
            level=level, source_path="a.py", start_line=1, start_col=0,
        )
        res = analyze_named_import(r, invd, {})
        unit = res.contexts[0]
        assert unit.name.outcome == OUTCOME_NOT_IN_SCOPE
        assert unit.name.reason == NAME_IMPORT_FORM
        # frozen absolute UNSUPPORTED/invalid_relative_import_level for
        # malformed import-kind levels (Codex D)
        if level != 0:
            assert unit.context.status == UNSUPPORTED
            assert "invalid_relative_import_level" in (unit.context.reason or "")


def test_positive_level_invalid_kind_keeps_relative_issue():
    invd = inv("pkg/__init__.py")
    r = ImportRecord(
        kind="invalid-kind", module="pkg", names=(("sub", None),),
        level=1, source_path="a.py", start_line=1, start_col=0,
    )
    res = analyze_named_import(r, invd, {})
    assert res.contexts == ()
    assert "relative_invalid_kind" in (res.issue or "")


def test_absolute_empty_module_not_attempted():
    # valid from_import with empty absolute module -> empty_requested_module
    invd = inv("pkg/__init__.py")
    r = ImportRecord(
        kind="from_import", module="", names=(("sub", None),),
        level=0, source_path="a.py", start_line=1, start_col=0,
    )
    res = analyze_named_import(r, invd, {})
    unit = res.contexts[0]
    assert unit.context.status == NOT_ATTEMPTED
    assert "empty_requested_module" in (unit.context.reason or "")
    assert unit.name.outcome == OUTCOME_NOT_ANALYZED
    assert unit.name.reason == NAME_TARGET_NOT_ATTEMPTED


def test_relative_no_package_context_not_attempted():
    # relative from a source that is itself the module (no enclosing
    # package) -> NOT_ATTEMPTED/relative_no_package_context
    invd = build_module_inventory(
        [{"rel_path": "mod.py", "language": "Python", "is_binary": False}], ["."]
    )
    r = ImportRecord(
        kind="from_import", module="pkg", names=(("sub", None),),
        level=1, source_path="mod.py", start_line=1, start_col=0,
    )
    res = analyze_named_import(r, invd, {})
    if res.contexts:
        unit = res.contexts[0]
        assert unit.context.status == NOT_ATTEMPTED
        assert "relative_no_package_context" in (unit.context.reason or "")
        assert unit.name.outcome == OUTCOME_NOT_ANALYZED
        assert unit.name.reason == NAME_TARGET_NOT_ATTEMPTED
    else:
        # pre-context failure envelope also acceptable
        assert res.issue is not None


def test_ann_assign_requires_value_to_bind():
    invd = inv("pkg/__init__.py")
    pkg = pick(invd, "pkg")
    record = rec("a.py", module="pkg", names=(("sub", None),))
    # annotation-only 'sub: int' does NOT bind a value
    ts = {pkg: TargetSource(SRC_SUPPLIED, "sub: int\n", None)}
    res = analyze_named_import(record, invd, ts)
    assert len(res.contexts[0].name.definitions) == 0
    # 'sub: int = 1' DOES bind a value
    ts2 = {pkg: TargetSource(SRC_SUPPLIED, "sub: int = 1\n", None)}
    res2 = analyze_named_import(record, invd, ts2)
    assert len(res2.contexts[0].name.definitions) == 1


def test_relative_context_base_module_is_none():
    invd = build_module_inventory(
        [
            {"rel_path": "src/mod.py", "language": "Python", "is_binary": False},
            {"rel_path": "src/pkg/__init__.py", "language": "Python", "is_binary": False},
            {"rel_path": "src/pkg/sub/__init__.py", "language": "Python", "is_binary": False},
        ], ["."]
    )
    r = ImportRecord(
        kind="from_import", module="pkg", names=(("sub", None),),
        level=1, source_path="src/mod.py", start_line=1, start_col=0,
    )
    res = analyze_named_import(r, invd, {})
    assert res.contexts[0].context.base_module is None


def test_wildcard_import_from_not_a_named_binding():
    # from . import * never establishes a named '*' re-export
    invd = inv("pkg/__init__.py")
    pkg = pick(invd, "pkg")
    ts = {pkg: TargetSource(SRC_SUPPLIED, "from . import *\n", None)}
    record = rec("a.py", module="pkg", names=(("sub", None),))
    res = analyze_named_import(record, invd, ts)
    assert len(res.contexts[0].name.reexports) == 0


def test_import_alias_binds_alias_not_original():
    # import x.y as z binds z, NOT x.y
    invd = inv("pkg/__init__.py")
    pkg = pick(invd, "pkg")
    ts = {pkg: TargetSource(SRC_SUPPLIED, "import os.path as p\n", None)}
    # 'p' is bound
    res = analyze_named_import(rec("a.py", module="pkg", names=(("p", None),)), invd, ts)
    assert len(res.contexts[0].name.reexports) == 1
    # 'os' is NOT bound by the aliased import
    res_os = analyze_named_import(rec("a.py", module="pkg", names=(("os", None),)), invd, ts)
    assert len(res_os.contexts[0].name.reexports) == 0


# ---------------------------------------------------------------------------
# Accepted-coverage durability additions (same authorized test file)
# ---------------------------------------------------------------------------


def test_D11_namespace_package_child_retained():
    # D11: child is an ORDINARY module (pkg/sub.py) whose directory_kind
    # we then replace with 'namespace_package'; the valid initializer
    # parent still yields structural child evidence with metadata retained.
    invd = build_module_inventory(
        [
            {"rel_path": "pkg/__init__.py", "language": "Python", "is_binary": False},
            {"rel_path": "pkg/sub.py", "language": "Python", "is_binary": False},
        ], ["."]
    )
    child = [c for c in invd if c.name_components == ("pkg", "sub")][0]
    assert child.file_kind == "ordinary_module"
    ns_child = dataclasses.replace(child, directory_kind="namespace_package")
    invd2 = tuple(
        ns_child if c.name_components == ("pkg", "sub") else c for c in invd
    )
    record = rec("a.py", module="pkg", names=(("sub", None),))
    res = analyze_named_import(record, invd2, {})
    unit = res.contexts[0]
    assert len(unit.name.submodule) == 1
    assert unit.name.submodule[0].submodule_candidate.name_components == ("pkg", "sub")
    s = unit.name.submodule[0]
    assert s.submodule_candidate is ns_child


def test_D12_unparseable_source_retains_child_evidence():
    # F4/F2: with SRC_UNPARSEABLE (or SRC_UNAVAILABLE) source, a valid
    # structural child is still observed and definitions/reexports empty.
    invd = inv("pkg/__init__.py", "pkg/sub/__init__.py")
    pkg = pick(invd, "pkg")
    record = rec("a.py", module="pkg", names=(("sub", None),))
    for state in (SRC_UNAVAILABLE, SRC_UNPARSEABLE):
        ts = {
            pkg: TargetSource(
                state,
                None if state == SRC_UNAVAILABLE else "bad syntax",
                None if state == SRC_UNAVAILABLE else "err",
            )
        }
        res = analyze_named_import(record, invd, ts)
        unit = res.contexts[0]
        assert unit.name.outcome == OUTCOME_ANALYZED
        assert len(unit.name.submodule) == 1            # structural retained
        assert len(unit.name.definitions) == 0          # no source defs
        assert len(unit.name.reexports) == 0            # no source reexports
        # assessment state observable
        assert unit.context.assessments[0].state == state


def test_direct_class_function_asyncfunction_definitions():
    invd = inv("pkg/__init__.py")
    pkg = pick(invd, "pkg")
    # One occurrence per name (3 units, each with its own definition)
    record = rec("a.py", module="pkg", names=(("A", None), ("f", None), ("g", None)))
    ts = {pkg: TargetSource(SRC_SUPPLIED, "class A:\n    pass\n\ndef f():\n    pass\n\nasync def g():\n    pass\n", None)}
    res = analyze_named_import(record, invd, ts)
    kinds = {}
    for u in res.contexts:
        for d in u.name.definitions:
            kinds[d.name] = (d.kind, d.symbol_kind)
    assert kinds == {
        "A": ("module_class", "class"),
        "f": ("module_function", "function"),
        "g": ("module_async_function", "async_function"),
    }
    # nested locals are excluded
    ts_nested = {pkg: TargetSource(SRC_SUPPLIED, "def f():\n    class A: pass\n", None)}
    res_nested = analyze_named_import(record, invd, ts_nested)
    assert len(res_nested.contexts[0].name.definitions) == 0


def test_conditional_while_import_vs_nested_barrier():
    invd = inv("pkg/__init__.py")
    pkg = pick(invd, "pkg")
    record = rec("a.py", module="pkg", names=(("sub", None),))
    # while-loop module-scope import IS module-conditional (RS_MODULE_CONDITIONAL)
    ts_while = {pkg: TargetSource(SRC_SUPPLIED, "while COND:\n    from . import sub\n", None)}
    res_while = analyze_named_import(record, invd, ts_while)
    assert len(res_while.contexts[0].name.reexports) == 1
    assert res_while.contexts[0].name.reexports[0].provenance == RS_MODULE_CONDITIONAL
    # class-body and function-body imports are NEITHER re-exports
    ts_cb = {pkg: TargetSource(SRC_SUPPLIED, "class C:\n    from . import sub\n", None)}
    res_cb = analyze_named_import(record, invd, ts_cb)
    assert len(res_cb.contexts[0].name.reexports) == 0
    ts_fb = {pkg: TargetSource(SRC_SUPPLIED, "def f():\n    from . import sub\n", None)}
    res_fb = analyze_named_import(record, invd, ts_fb)
    assert len(res_fb.contexts[0].name.reexports) == 0


def test_inventory_permutation_invariance():
    # reordering the inventory must not change the resolved evidence set
    inv_files = [
        {"rel_path": "pkg/__init__.py", "language": "Python", "is_binary": False},
        {"rel_path": "pkg/sub/__init__.py", "language": "Python", "is_binary": False},
        {"rel_path": "other.py", "language": "Python", "is_binary": False},
    ]
    invd_a = build_module_inventory(inv_files, ["."])
    invd_b = build_module_inventory(list(reversed(inv_files)), ["."])
    record = rec("a.py", module="pkg", names=(("sub", None),))
    ra = analyze_named_import(record, invd_a, {})
    rb = analyze_named_import(record, invd_b, {})
    # same child-evidence (by (root,components))
    def skey(r):
        return {(s.submodule_candidate.root.canonical, s.submodule_candidate.name_components)
                for s in r.contexts[0].name.submodule}
    assert skey(ra) == skey(rb)
    assert len(ra.contexts[0].name.submodule) == len(rb.contexts[0].name.submodule)


def test_equivalent_duplicate_candidates_and_conflict():
    # Equivalent (same components, near-same root identity) duplicates
    # collapse to one candidate; a supported/unsupported CONFLICT on the
    # same components is rejected rather than silently resolved.
    invd = inv("pkg/__init__.py", "pkg/sub.py")
    pkg = pick(invd, "pkg")
    # An alternate root identity (original ends in '/') is still the same
    # components and value, exercised to exercise the conflict path.
    alternate = dataclasses.replace(
        pkg, root=dataclasses.replace(pkg.root, original=pkg.root.original + "/")
    )
    record = rec("a.py", module="pkg", names=(("sub", None),))
    sources = {alternate: TargetSource(SRC_SUPPLIED, "sub = 1\n", None)}
    candidates = (*invd, alternate)
    first = analyze_named_import(record, candidates, sources)
    reversed_result = analyze_named_import(record, tuple(reversed(candidates)), sources)
    assert first == reversed_result
    unit = first.contexts[0]
    assert len(unit.context.supported_candidates) == 1
    assert len(unit.name.submodule) == 1
    assert len(unit.name.definitions) == 1
    assert unit.context.assessments[0].state == SRC_SUPPLIED
    # A name-supported/unsupported conflict on the same components is an
    # explicit failure, not a silent pick.
    conflict = dataclasses.replace(pkg, name_supported=False)
    with pytest.raises(ValueError):
        analyze_named_import(record, (*invd, conflict), {})


def test_boundary_case_sensitive_and_literal_dot():
    # case-sensitive name matching: 'Sub' != 'sub'
    invd = inv("pkg/__init__.py", "pkg/sub/__init__.py")
    record_lower = rec("a.py", module="pkg", names=(("sub", None),))
    record_upper = rec("a.py", module="pkg", names=(("Sub", None),))
    assert len(analyze_named_import(record_lower, invd, {}).contexts[0].name.submodule) == 1
    assert len(analyze_named_import(record_upper, invd, {}).contexts[0].name.submodule) == 0
    # literal-dot name 'pkg.sub' is NOT the structural child 'sub'
    record_dotted = rec("a.py", module="pkg", names=(("pkg.sub", None),))
    assert len(analyze_named_import(record_dotted, invd, {}).contexts[0].name.submodule) == 0
    # root boundary: a child in a DIFFERENT root is not evidence
    invd_roots = build_module_inventory(
        [
            {"rel_path": "ra/pkg/__init__.py", "language": "Python", "is_binary": False},
            {"rel_path": "rb/pkg/sub/__init__.py", "language": "Python", "is_binary": False},
        ], ["ra", "rb"]
    )
    rec_r = ImportRecord(kind="from_import", module="pkg", names=(("sub", None),),
                         level=0, source_path="a.py", start_line=1, start_col=0)
    rr = analyze_named_import(rec_r, invd_roots, {})
    # child 'sub' is under root 'rb' but 'pkg' parent is under 'ra'
    # -> no same-root structural child
    assert len(rr.contexts[0].name.submodule) == 0


def test_literal_dot_file_component_collision():
    # A literal dotted directory 'pkg.a' is a SINGLE component ('pkg.a',),
    # and its name is NOT a Python-identifier -> name_supported=False and
    # the frozen resolver reports UNRESOLVED.  Structural 'pkg/a' is the
    # two-component ('pkg','a') target and DOES resolve.  Contrasting the
    # two proves the analyzer distinguishes a literal-dot FILE COMPONENT
    # from a structural path of the same dot-free display string.
    invd_literal = build_module_inventory(
        [
            {"rel_path": "pkg.a/__init__.py", "language": "Python", "is_binary": False},
            {"rel_path": "pkg.a/sub.py", "language": "Python", "is_binary": False},
        ], ["."]
    )
    record = rec("a.py", module="pkg.a", names=(("sub", None),))
    r_lit = analyze_named_import(record, invd_literal, {})
    # The literal 'pkg.a' is one component, not ('pkg','a')
    sc_lit = r_lit.contexts[0].context.supported_candidates[0] if r_lit.contexts[0].context.supported_candidates else None
    if sc_lit is not None:
        assert sc_lit.name_components == ("pkg.a",)
    # F5: this is an unsupported/literal-dot name, so NO structural child
    # may be synthesized from the 'sub' occurrence
    assert len(r_lit.contexts[0].name.submodule) == 0
    # 'pkg/a' structural: two-component target that DOES resolve.
    invd_struct = build_module_inventory(
        [
            {"rel_path": "pkg/a/__init__.py", "language": "Python", "is_binary": False},
            {"rel_path": "pkg/a/sub.py", "language": "Python", "is_binary": False},
        ], ["."]
    )
    r_struct = analyze_named_import(record, invd_struct, {})
    sc_st = r_struct.contexts[0].context.supported_candidates
    assert len(sc_st) == 1
    assert sc_st[0].name_components == ("pkg", "a")
    # and it produces structural child evidence for 'sub'
    assert len(r_struct.contexts[0].name.submodule) == 1


def test_empty_names_rejected_constructed():
    # F5: construct ImportRecord directly with empty names -> ValueError
    try:
        bad = ImportRecord(kind="from_import", module="pkg",
                           names=(), level=0, source_path="a.py",
                           start_line=1, start_col=0)
    except Exception:
        return  # constructor already rejects; nothing to test
    invd = inv("pkg/__init__.py")
    with pytest.raises(ValueError):
        analyze_named_import(bad, invd, {})


def test_deep_immutability_and_input_preservation():
    invd = inv("pkg/__init__.py", "pkg/sub/__init__.py")
    pkg = pick(invd, "pkg")
    record = rec("a.py", module="pkg", names=(("sub", None),))
    ts_map = {pkg: TargetSource(SRC_SUPPLIED, "from . import sub\n", None)}
    # snapshot input identity / values before the call
    record_before = record
    child_before = [c for c in invd if c.name_components == ("pkg", "sub")][0]
    ts_val_before = ts_map[pkg]
    res = analyze_named_import(record, invd, ts_map)
    # deep immutability: every result object is immutable (frozen).
    # dataclasses raise FrozenInstanceError; plain frozen types raise
    # TypeError — either proves the object is immutable.
    import dataclasses as _dc
    for obj in (res, res.contexts[0], res.contexts[0].name,
                res.contexts[0].context, res.contexts[0].name.submodule[0]):
        with pytest.raises(Exception) as excinfo:
            obj.x = 1  # type: ignore[misc]
        assert isinstance(excinfo.value, (_dc.FrozenInstanceError, TypeError)), excinfo.value
    # input preservation: record identity and candidate identity untouched.
    # SubmoduleNameEvidence.candidate is the PARENT; .submodule_candidate is the CHILD.
    assert res.record is record_before
    assert res.contexts[0].name.submodule[0].candidate is pkg
    assert res.contexts[0].name.submodule[0].submodule_candidate is child_before
    assert ts_map[pkg] is ts_val_before
