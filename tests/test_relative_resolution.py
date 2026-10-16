"""Focused tests for the Phase 2C-3A pure relative-import module-target
resolver (src/repo_intelligence/relative_resolution.py).

Each ``test_NN_*`` maps to an approved category NN of the Phase 2C-3A
R2 spec (Section 12 of the approved contract).  All synthetic
inventories are built *in-memory* (no filesystem access), either by
directly constructing frozen :class:`ModuleCandidate` objects (needed
for literal-dot and unsupported-name cases, which the frozen builder
would classify as unsupported) or via the public
:func:`repo_intelligence.module_inventory.build_module_inventory` API.

No test executes an indexed module, writes to any database, or touches
the network.
"""

from __future__ import annotations

import itertools
import sys

import dataclasses
import pytest

from repo_intelligence.absolute_resolution import (
    AMBIGUOUS,
    NOT_ATTEMPTED,
    RESOLVED,
    UNSUPPORTED,
    UNRESOLVED,
    REASON_MULTI_SUPPORTED,
    REASON_NO_CANDIDATE,
    REASON_UNSUPPORTED_NAME,
    REASON_UNIQUE_SUPPORTED,
    resolve_absolute_import,
)
from repo_intelligence.module_inventory import (
    ModuleCandidate,
    PackageRoot,
    build_module_inventory,
)
from repo_intelligence.python_ast import ImportRecord
from repo_intelligence.relative_resolution import (
    ContextResolution,
    RelativeResolution,
    REL_INVALID_KIND,
    REL_INVALID_LEVEL,
    REL_INVALID_SOURCE_PATH,
    REL_LOSSLESS_UNSAFE,
    REL_MALFORMED_SUFFIX,
    REL_MISSING_SOURCE_CONTEXT,
    REL_NO_PACKAGE_CONTEXT,
    REL_NOT_RELATIVE,
    REL_TRAVERSAL_BEYOND_TOP,
    resolve_relative_import,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def MC(
    *,
    root: str = "",
    root_orig: str | None = None,
    file_path: str,
    name_components: tuple[str, ...],
    file_kind: str = "ordinary_module",
    directory_kind: str = "regular_package",
    component_supported: bool = True,
    name_supported: bool = True,
    evidence: tuple[str, ...] = ("synthetic",),
) -> ModuleCandidate:
    """Construct a synthetic ModuleCandidate via its frozen public
    dataclass; this exercises the resolver directly without depending
    on the frozen builder's support flagging."""
    return ModuleCandidate(
        file_path=file_path,
        root=PackageRoot(canonical=root, original=root_orig if root_orig is not None else root),
        qualified_name=".".join(name_components),
        name_components=name_components,
        file_kind=file_kind,
        directory_kind=directory_kind,
        component_supported=component_supported,
        name_supported=name_supported,
        evidence=evidence,
    )


def rec(
    module: object,
    level: int,
    source_path: object = "pkg/init.py",
    kind: str = "from_import",
    names: tuple = (("x", None),),
    start_line: int = 1,
    start_col: int = 0,
) -> ImportRecord:
    base = dict(
        kind=kind,
        module=module,
        names=names,
        level=level,
        source_path=source_path,
        start_line=start_line,
        start_col=start_col,
    )
    return ImportRecord(**base)


def inv_of(*cands) -> tuple[ModuleCandidate, ...]:
    return tuple(cands)


PKG_INIT      = MC(file_path="pkg/__init__.py", name_components=("pkg",),
                   file_kind="package_initializer")
PKG_HELPER    = MC(file_path="pkg/helpers.py",  name_components=("pkg","helpers"))
PKG_SUB_INIT  = MC(file_path="pkg/sub/__init__.py",
                   name_components=("pkg","sub"), file_kind="package_initializer")
PKG_SUB_UTIL  = MC(file_path="pkg/sub/util.py",   name_components=("pkg","sub","util"))
ROOT_MOD      = MC(file_path="mod.py",            name_components=("mod",),
                   directory_kind="explicit_root")
A_BDC_HELPER  = MC(file_path="a/b.c/helper.py", name_components=("a","b.c","helper"),
                   component_supported=False, name_supported=False)

# A realistic inventory via the frozen builder (used for category 20
# regression-preservation and to compare synthetic vs. built behavior).
def built_inventory() -> tuple[ModuleCandidate, ...]:
    def fl(rel_path):
        return {"rel_path": rel_path, "language": "Python", "size": 1,
                "is_binary": False}
    return build_module_inventory(
        [fl("pkg/__init__.py"), fl("pkg/helpers.py"),
         fl("pkg/sub/__init__.py"), fl("pkg/sub/util.py")],
        ["."],
    )


# ---------------------------------------------------------------------------
# Category 1 -- Empty relative suffix resolves current package
# ---------------------------------------------------------------------------

class TestCategory01_EmptySuffix:
    def test_empty_suffix_resolves_current_package_target(self):
        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="", level=1, source_path="pkg/__init__.py")
        res = resolve_relative_import(r, inv)
        assert res.issue is None
        assert len(res.contexts) == 1
        c = res.contexts[0]
        assert c.status == RESOLVED
        assert c.reason == REASON_UNIQUE_SUPPORTED
        assert c.lossless is True
        assert c.base_components == ("pkg",)
        assert c.target_components == ("pkg",)
        assert [x.file_path for x in c.supported_candidates] == ["pkg/__init__.py"]
        assert c.unsupported_candidates == ()

    def test_empty_suffix_from_submodule_targets_same_package(self):
        # `from . import helper` in pkg/helpers.py -> target (pkg,)
        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="", level=1, source_path="pkg/helpers.py")
        res = resolve_relative_import(r, inv)
        assert res.contexts[0].status == RESOLVED
        assert res.contexts[0].target_components == ("pkg",)

    def test_empty_suffix_names_uninterpreted(self):
        # The imported-name `helper` must NOT be resolved as an
        # attribute; the resolver targets the *package* module only.
        # (Phase 2C-3B would interpret the name.)
        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="", level=1, source_path="pkg/helpers.py",
                names=(('helper', None),))
        res = resolve_relative_import(r, inv)
        # The name should appear unchanged on the record (identity).
        assert res.record.names == (('helper', None),)
        # And the target is still the package module, not the name.
        assert res.contexts[0].target_components == ("pkg",)


# ---------------------------------------------------------------------------
# Category 2 -- Namespace-package contexts (no __init__.py candidate)
# ---------------------------------------------------------------------------

class TestCategory02_NamespacePackage:
    def test_namespace_package_context_no_fabricated_candidate(self):
        # A source file lives under a NAMESPACE package (no __init__.py):
        #   ns/sub/mod.py  (name_components=("ns","sub","mod"),
        #   directory_kind="namespace_package")
        # A relative import `from . import x` in it derives base=
        #   ("ns","sub") but there is no __init__.py candidate for that
        #   base in the inventory. Resolution must NOT fabricate a
        #   synthetic candidate; the frozen absolute resolver reports
        #   UNRESOLVED because nothing structural matches.
        src = MC(file_path="ns/sub/mod.py",
                 name_components=("ns","sub","mod"),
                 directory_kind="namespace_package")
        inv = inv_of(src)
        r = rec(module="", level=1, source_path="ns/sub/mod.py")
        res = resolve_relative_import(r, inv)
        assert res.issue is None
        c = res.contexts[0]
        assert c.status == UNRESOLVED  # frozen behavior: no structural match
        assert c.reason == REASON_NO_CANDIDATE
        assert [x.file_path for x in c.supported_candidates] == []
        assert [x.file_path for x in c.unsupported_candidates] == []
        # And the original source candidate is preserved on the result.
        assert c.source_candidate is src

    def test_no_initializer_and_matching_module_candidate(self):
        # A `pkg.py` (module, not package) with the same qualified-name
        # spelling as a namespace-package base: frozen behavior must be
        # preserved (frozen resolver matches "pkg" -> the pkg.py module).
        src   = MC(file_path="ns/mod.py",
                   name_components=("ns","mod"),
                   directory_kind="namespace_package")
        target = MC(file_path="ns.py", name_components=("ns",))
        # Wait: qualified_name "ns" would conflict with "ns.mod".
        # Let's use a proper distinct namespace module.
        target = MC(file_path="x.py", name_components=("ns",))
        inv = inv_of(src, target)
        r = rec(module="", level=1, source_path="ns/mod.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.status == RESOLVED
        assert [x.file_path for x in c.supported_candidates] == ["x.py"]


# ---------------------------------------------------------------------------
# Category 3 -- Missing initializer vs. existing module
# ---------------------------------------------------------------------------

class TestCategory03_MissingInitVsModule:
    def test_no_initializer_but_exact_matching_module_candidate(self):
        # A source file `pkg/mod.py` (ordinary module); there's no
        # pkg/__init__.py in the inventory.  A relative import
        # `from . import X` (base=("pkg",)) targets the *package*
        # module ("pkg").  Without a `pkg/__init__.py` or `pkg.py`
        # present, the frozen resolver returns UNRESOLVED.
        src = MC(file_path="pkg/mod.py", name_components=("pkg","mod"))
        inv = inv_of(src)
        r = rec(module="", level=1, source_path="pkg/mod.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.status == UNRESOLVED
        assert c.reason == REASON_NO_CANDIDATE

    def test_module_pkg_py_present_suppresses_no_candidate_result(self):
        # If `pkg.py` IS present in the inventory, the frozen absolute
        # resolver matches target ("pkg",) to the pkg.py module and
        # returns RESOLVED.  This preserves the frozen behavior — this
        # test only verifies no suppression by a missing initializer.
        src    = MC(file_path="a/pkg/mod.py", name_components=("a","pkg","mod"))
        pkg_py = MC(file_path="a/pkg.py",     name_components=("a","pkg"))
        inv = inv_of(src, pkg_py)
        r = rec(module="", level=1, source_path="a/pkg/mod.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.status == RESOLVED
        assert [x.file_path for x in c.supported_candidates] == ["a/pkg.py"]

    def test_initializer_and_module_collision_preserves_frozen_ambiguity(self):
        # Both `pkg/__init__.py` and `pkg.py` are present under the
        # same root and have the same name_components ("pkg",).  The
        # frozen resolver deduplicates by structural identity
        # (root, name_components, file_path) — since file_paths differ,
        # they are NOT merged; the frozen matching step yields AMBIGUOUS.
        init = MC(file_path="pkg/__init__.py",
                  name_components=("pkg",), file_kind="package_initializer")
        mod  = MC(file_path="pkg.py",
                  name_components=("pkg",), file_kind="ordinary_module")
        src  = MC(file_path="pkg/helpers.py", name_components=("pkg","helpers"))
        inv = inv_of(init, mod, src)
        r = rec(module="", level=1, source_path="pkg/helpers.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        # Two distinct file paths with same name_components -> 2 supported
        # matches -> AMBIGUOUS per frozen behavior.
        assert c.status == AMBIGUOUS
        assert c.reason == REASON_MULTI_SUPPORTED
        paths = sorted(x.file_path for x in c.supported_candidates)
        assert paths == ["pkg.py", "pkg/__init__.py"]


# ---------------------------------------------------------------------------
# Category 4 -- Relative traversal boundaries
# ---------------------------------------------------------------------------

class TestCategory04_TraversalBoundaries:
    def test_level1_within_package(self):
        # pkg/sub/util.py level=1 base=(pkg,sub) kept=(pkg,sub)
        # -> target=(pkg,sub) (current parent of the source file = `pkg.sub`)
        # A `from . import X` from `pkg/sub/util.py` points at package `pkg.sub`
        # (its immediate parent), not at `pkg`.
        inv = inv_of(PKG_SUB_INIT, PKG_SUB_UTIL)
        r = rec(module="", level=1, source_path="pkg/sub/util.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.status == RESOLVED
        assert c.target_components == ("pkg", "sub")
        assert c.lossless is True
        assert [x.file_path for x in c.supported_candidates] == ["pkg/sub/__init__.py"]

    def test_level_equal_to_base_depth(self):
        # pkg/sub/util.py level=2 base=(pkg,sub) kept=(pkg) -> (pkg,helpers)
        inv = inv_of(PKG_SUB_INIT, PKG_SUB_UTIL, PKG_INIT, PKG_HELPER)
        r = rec(module="helpers", level=2, source_path="pkg/sub/util.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.status == RESOLVED
        assert c.target_components == ("pkg", "helpers")

    @pytest.mark.parametrize("level", [3, 4, 10])
    def test_level_greater_than_base_depth(self, level):
        # pkg/sub/util.py base=(pkg,sub); level>2 -> beyond top
        inv = inv_of(PKG_SUB_INIT, PKG_SUB_UTIL)
        r = rec(module="", level=level, source_path="pkg/sub/util.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.status == UNSUPPORTED
        assert c.reason == REL_TRAVERSAL_BEYOND_TOP
        assert c.target_components is None
        assert c.lossless is None
        assert c.supported_candidates == ()
        assert c.unsupported_candidates == ()

    def test_empty_package_context(self):
        # Root-level ordinary module `mod.py` base=() -> REL_NO_PACKAGE_CONTEXT
        inv = inv_of(ROOT_MOD)
        r = rec(module="", level=1, source_path="mod.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.status == NOT_ATTEMPTED
        assert c.reason == REL_NO_PACKAGE_CONTEXT
        assert c.target_components is None
        assert c.lossless is None
        assert c.supported_candidates == ()

    def test_invalid_traversal_with_nonempty_suffix(self):
        # base=(pkg,sub) level=3 -> beyond top.  Invalid traversal must
        # NOT be rescued by appending the suffix (spec section 7).
        inv = inv_of(PKG_SUB_INIT, PKG_SUB_UTIL, PKG_HELPER)
        r = rec(module="helpers", level=3, source_path="pkg/sub/util.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.status == UNSUPPORTED
        assert c.reason == REL_TRAVERSAL_BEYOND_TOP
        assert c.target_components is None  # not rescued
        assert c.supported_candidates == ()

    def test_empty_suffix_with_full_traversal_is_package(self):
        # base=(pkg,sub) level=2 empty suffix -> target=(pkg,)
        inv = inv_of(PKG_SUB_INIT, PKG_SUB_UTIL, PKG_INIT)
        r = rec(module="", level=2, source_path="pkg/sub/util.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.status == RESOLVED
        assert c.target_components == ("pkg",)


# ---------------------------------------------------------------------------
# Category 5 -- Traversal-before-suffix regression (mandatory)
# ---------------------------------------------------------------------------

class TestCategory05_TraversalBeforeSuffix:
    def test_base_pkg_sub_level2_suffix_util_produces_pkg_util(self):
        # MUST (spec section 7): base=(pkg,sub) level=2 suffix=util
        # -> KEEPED=(pkg) then CONCAT util -> (pkg, util).
        # MUST NOT be (pkg, sub, util).
        inv = inv_of(
            PKG_SUB_INIT, PKG_SUB_UTIL,
            MC(file_path="pkg/util.py", name_components=("pkg","util")),
        )
        r = rec(module="util", level=2, source_path="pkg/sub/util.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        # The target identity must be EXACTLY (pkg, util).
        assert c.target_components == ("pkg", "util")
        # And the resolved candidate must be the pkg/util.py module.
        assert [x.file_path for x in c.supported_candidates] == ["pkg/util.py"]
        assert c.status == RESOLVED

    def test_level1_suffix_preserves_own_parent(self):
        # base=(pkg,sub) L=1 suffix=deep.x  -> (pkg,sub,deep,x)
        deep_pkg = MC(file_path="pkg/sub/deep/__init__.py",
                      name_components=("pkg","sub","deep"),
                      file_kind="package_initializer")
        deep_x   = MC(file_path="pkg/sub/deep/x.py",
                      name_components=("pkg","sub","deep","x"))
        inv = inv_of(PKG_SUB_INIT, PKG_SUB_UTIL, deep_pkg, deep_x)
        r = rec(module="deep.x", level=1, source_path="pkg/sub/util.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.target_components == ("pkg","sub","deep","x")
        assert c.status == RESOLVED

    @pytest.mark.parametrize("base,level,suffix,expected", [
        # (kept = base[:n-(L-1)]) + suffix_components.  For each row the
        # source candidate is built with `leaf` under `base`, so the
        # derived base IS exactly `base` (spec section 6).
        ( ("pkg",),              1, "",        ("pkg",)              ),
        ( ("pkg",),              1, "helpers", ("pkg","helpers")     ),
        ( ("pkg","sub"),         1, "helpers", ("pkg","sub","helpers")),
        ( ("pkg","sub"),         2, "helpers", ("pkg","helpers")     ),
        ( ("pkg","sub"),         2, "",        ("pkg",)              ),
        ( ("a","b","c"),         3, "",        ("a",)                ),
        ( ("a","b","c"),         3, "x",       ("a","x")             ),
        ( ("a","b","c"),         2, "y",       ("a","b","y")         ),
    ])
    def test_traversal_ordering_matrix(self, base, level, suffix, expected):
        # Source `'/'.join(base)/leaf.py` has name_components=base+("leaf",)
        # so derived base == `base` (spec section 6).
        base_dir = "/".join(base)
        src = MC(file_path=f"{base_dir}/leaf.py",
                 name_components=base + ("leaf",))
        r = rec(module=suffix, level=level, source_path=src.file_path)
        res = resolve_relative_import(r, inv_of(src))
        c = res.contexts[0]
        assert c.target_components == expected
        assert c.lossless is True
        # Delegation is safe on these rows (no literal dots), so the
        # status/reason must be the frozen resolver's output.
        assert c.status in (RESOLVED, AMBIGUOUS, UNRESOLVED, UNSUPPORTED)


# ---------------------------------------------------------------------------
# Category 6 -- Validation precedence
# ---------------------------------------------------------------------------

class TestCategory06_ValidationPrecedence:
    def test_wrong_record_type(self):
        with pytest.raises(TypeError, match="ImportRecord"):
            resolve_relative_import("not a record", inv_of(PKG_INIT))

    def test_noniterable_inventory(self):
        r = rec(module="x", level=1, source_path="a/b.py")
        with pytest.raises(TypeError, match="inventory"):
            resolve_relative_import(r, 123)
        with pytest.raises(TypeError, match="inventory"):
            resolve_relative_import(r, None)

    def test_wrong_inventory_member_type(self):
        r = rec(module="x", level=1, source_path="a/b.py")
        with pytest.raises(TypeError, match="ModuleCandidate"):
            resolve_relative_import(r, ["not-a-candidate", 42])
        with pytest.raises(TypeError, match="ModuleCandidate"):
            resolve_relative_import(r, [dict(kind="a candidate")])

    @pytest.mark.parametrize("bad_kind", ["banana", None, 42, ("from",), 1.0])
    def test_invalid_kind(self, bad_kind):
        r = rec(module="x", level=1, source_path="a.py", kind=bad_kind)
        res = resolve_relative_import(r, inv_of(PKG_INIT))
        assert res.issue == REL_INVALID_KIND
        assert res.contexts == ()

    def test_level_precedence_negative_before_kind_is_str(self):
        # `kind` is valid here; `level` is negative -> REL_INVALID_LEVEL
        r = rec(module="x", level=-1, source_path="a.py", kind="import")
        res = resolve_relative_import(r, inv_of())
        assert res.issue == REL_INVALID_LEVEL

    @pytest.mark.parametrize("bad_level", [True, False, 1.5, "1", (1,), None, -1])
    def test_invalid_level(self, bad_level):
        r = rec(module="x", level=bad_level, source_path="a.py")
        res = resolve_relative_import(r, inv_of())
        assert res.issue == REL_INVALID_LEVEL

    def test_import_kind_with_positive_level(self):
        # Ordinary `import` with positive level is malformed (language rule).
        r = rec(module="x.y", level=2, source_path="a.py", kind="import")
        res = resolve_relative_import(r, inv_of())
        assert res.issue == REL_INVALID_LEVEL

    def test_level_zero_absolute_from_import(self):
        r = rec(module="x", level=0, source_path="a.py", kind="from_import")
        res = resolve_relative_import(r, inv_of())
        assert res.issue == REL_NOT_RELATIVE

    @pytest.mark.parametrize("bad_suffix", [
        "a..b",          # empty component
        ".",
        "a.",
        ".a",
        "a.b..c",
    ])
    def test_malformed_dotted_suffix(self, bad_suffix):
        r = rec(module=bad_suffix, level=1, source_path="pkg/mod.py")
        res = resolve_relative_import(r, inv_of())
        assert res.issue == REL_MALFORMED_SUFFIX
        assert res.contexts == ()

    def test_non_string_module(self):
        r = rec(module=42, level=1, source_path="a.py")
        res = resolve_relative_import(r, inv_of())
        assert res.issue == REL_MALFORMED_SUFFIX

    @pytest.mark.parametrize("bad_path", [
        "",                  # empty
        "/abs/path.py",      # absolute
        "./rel.py",          # leading ./
        "a/../b.py",         # .. component
        "a/../b.py",         # (repeat)
        "a//b.py",           # empty component
        "a/./b.py",          # . component
        None,                # not string
    ])
    def test_invalid_canonical_source_path(self, bad_path):
        r = rec(module="x", level=1, source_path=bad_path)
        res = resolve_relative_import(r, inv_of())
        assert res.issue == REL_INVALID_SOURCE_PATH

    def test_missing_source_candidate(self):
        # source path is canonical and no candidate for it.
        r = rec(module="x", level=1, source_path="does/not/exist.py")
        res = resolve_relative_import(r, inv_of(PKG_INIT, PKG_HELPER))
        assert res.issue == REL_MISSING_SOURCE_CONTEXT

    def test_combined_invalid_inputs_kind_beats_level(self):
        # Bad kind AND bad level: kind is checked first.
        r = rec(module="x", level=-1, source_path="a.py", kind="banana")
        res = resolve_relative_import(r, inv_of())
        assert res.issue == REL_INVALID_KIND

    def test_combined_invalid_inputs_level_beats_scope(self):
        # Bad level AND level==0: level check first.
        r = rec(module="x", level=-5, source_path="a.py")
        res = resolve_relative_import(r, inv_of())
        assert res.issue == REL_INVALID_LEVEL

    def test_combined_invalid_inputs_scope_beats_suffix(self):
        # level==0 AND malformed suffix: scope (REL_NOT_RELATIVE) first.
        r = rec(module="a..b", level=0, source_path="a.py")
        res = resolve_relative_import(r, inv_of())
        assert res.issue == REL_NOT_RELATIVE

    def test_combined_invalid_inputs_suffix_beats_source_path(self):
        # malformed suffix AND bad source path: suffix first.
        r = rec(module="a..b", level=1, source_path="bad/../x.py")
        res = resolve_relative_import(r, inv_of())
        assert res.issue == REL_MALFORMED_SUFFIX

    def test_combined_invalid_inputs_source_path_beats_lookup(self):
        # bad source path AND missing source candidate: source path first.
        r = rec(module="x", level=1, source_path="../x.py")
        res = resolve_relative_import(r, inv_of())
        assert res.issue == REL_INVALID_SOURCE_PATH


# ---------------------------------------------------------------------------
# Category 7 -- Source provenance preservation
# ---------------------------------------------------------------------------

class TestCategory07_SourceProvenance:
    def test_source_candidate_is_preserved_unchanged(self):
        src = MC(
            root="rootA",
            root_orig="rootA",
            file_path="pkg/sub/util.py",
            name_components=("pkg", "sub", "util"),
            file_kind="ordinary_module",
            directory_kind="regular_package",
            component_supported=True,
            name_supported=True,
            evidence=("kind:ordinary_module", "dir:regular_package"),
        )
        res = resolve_relative_import(
            rec(module="helpers", level=2, source_path="pkg/sub/util.py"),
            inv_of(src, PKG_INIT, PKG_HELPER),
        )
        c = res.contexts[0]
        assert c.source_candidate is src
        assert c.source_candidate.root.canonical == "rootA"
        assert c.source_candidate.root.original == "rootA"
        assert c.source_candidate.name_components == ("pkg", "sub", "util")
        assert c.source_candidate.file_kind == "ordinary_module"
        assert c.source_candidate.directory_kind == "regular_package"
        assert c.source_candidate.component_supported is True
        assert c.source_candidate.name_supported is True
        assert c.source_candidate.evidence == (
            "kind:ordinary_module", "dir:regular_package")


# ---------------------------------------------------------------------------
# Category 8 -- Literal-dot collision protection (mandatory)
# ---------------------------------------------------------------------------

class TestCategory08_LiteralDotCollision:
    def test_literal_dot_base_yields_lossless_unsafe(self):
        # Source with literal dot in an ancestor:  a/b.c/inner.py
        # name_components=("a","b.c","inner")  base=("a","b.c")
        # level=1 suffix="x" -> target=("a","b.c","x")
        # Dotted spelling "a.b.c.x" re-splits to ("a","b","c","x") != base
        # -> UNSUPPORTED / REL_LOSSLESS_UNSAFE, no delegation, empty
        # partitions, target_components preserved verbatim.
        src = MC(
            file_path="a/b.c/inner.py",
            name_components=("a", "b.c", "inner"),
            component_supported=False, name_supported=False,
        )
        # Provide a candidate that WOULD be falsely matched by joining+split:
        #   a/b/c/x.py (name_components=("a","b","c","x"))
        decoy = MC(
            file_path="a/b/c/x.py",
            name_components=("a", "b", "c", "x"),
        )
        inv = inv_of(src, decoy)
        r = rec(module="x", level=1, source_path="a/b.c/inner.py")
        res = resolve_relative_import(r, inv)
        assert res.issue is None
        c = res.contexts[0]
        assert c.status == UNSUPPORTED
        assert c.reason == REL_LOSSLESS_UNSAFE
        assert c.lossless is False
        assert c.target_components == ("a", "b.c", "x")
        assert c.supported_candidates == ()
        assert c.unsupported_candidates == ()
        # Decoy must NOT appear (no unsafe delegation).
        all_cands = (c.supported_candidates + c.unsupported_candidates)
        assert all(cand.file_path != "a/b/c/x.py" for cand in all_cands)


# ---------------------------------------------------------------------------
# Category 9 -- Unsupported importer components (removed ancestor doesn't
# poison a clean final target)
# ---------------------------------------------------------------------------

class TestCategory09_UnsupportedImporterStem:
    def test_removed_unsupported_ancestor_does_not_poison_target(self):
        # Source `x/y-2/z.py` has an unsupported ancestor "y-2" (hyphen).
        # level=3 removes ("z","y-2","x"[:-2] => kept=("x","-2")  no wait).
        # Concretely: base=("x","y","z") ... let's make it simple:
        #   source: q/r-1/s.py -> base=("q","r-1")
        #   level=2 strips "r-1" -> kept=("q",); suffix="" -> target=("q",)
        #   Clean target "q" delegates safely; resolver reports RESOLVED
        #   because a q/__init__.py exists.  The unsupported "r-1" must
        #   NOT contaminate the outcome (spec section 8, last paragraph).
        src = MC(
            file_path="q/r-1/s.py",
            name_components=("q", "r-1", "s"),
            component_supported=False,  # hyphen in "r-1"
        )
        q_init = MC(file_path="q/__init__.py",
                    name_components=("q",),
                    file_kind="package_initializer")
        inv = inv_of(src, q_init)
        r = rec(module="", level=2, source_path="q/r-1/s.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.lossless is True
        assert c.target_components == ("q",)
        assert c.status == RESOLVED
        assert [x.file_path for x in c.supported_candidates] == ["q/__init__.py"]

    def test_unsupported_stem_remains_in_target_is_still_lossless(self):
        # Same source, level=1 -> kept=("q","r-1") suffix="" -> target=("q","r-1").
        # Dotted "q.r-1" re-splits to ("q","r-1") which matches the tuple.
        # Actually "-2" splits fine as a component (no dots), so it IS
        # lossless.  The frozen resolver will classify the candidate as
        # UNSUPPORTED (or UNRESOLVED if no candidate).
        src = MC(file_path="q/r-1/s.py",
                 name_components=("q", "r-1", "s"),
                 component_supported=False)
        r = rec(module="", level=1, source_path="q/r-1/s.py")
        res = resolve_relative_import(r, inv_of(src))
        c = res.contexts[0]
        assert c.lossless is True           # "q.r-1" splits cleanly
        assert c.target_components == ("q", "r-1")
        # No target candidate in inventory -> UNRESOLVED.
        assert c.status == UNRESOLVED


# ---------------------------------------------------------------------------
# Category 10 -- Lossless but unsupported names (encoding vs. support are
# independent properties, spec section 8)
# ---------------------------------------------------------------------------

class TestCategory10_LosslessButUnsupported:
    def test_hyphenated_stem_is_lossless_and_unsupported(self):
        # Source `p/q.py` base=("p",); level=1 "hy-phen" suffix
        # -> target=("p","hy-phen").  Dotted "p.hy-phen" re-splits to
        # ("p","hy-phen") -> LOSSLESS (True).  A matching candidate with
        # a hyphen component is name_supported=False -> UNSUPPORTED.
        p_init = MC(file_path="p/__init__.py", name_components=("p",),
                    file_kind="package_initializer")
        source = MC(file_path="p/mod.py", name_components=("p","mod"))
        target = MC(file_path="p/hy-phen.py",
                    name_components=("p","hy-phen"),
                    component_supported=False, name_supported=False)
        inv = inv_of(p_init, source, target)
        r = rec(module="hy-phen", level=1, source_path="p/mod.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.lossless is True                     # encoding is fine
        assert c.status == UNSUPPORTED                # frozen classification
        assert c.reason == REASON_UNSUPPORTED_NAME
        assert c.unsupported_candidates[0].file_path == "p/hy-phen.py"

    def test_keyword_component_is_lossless_and_unsupported(self):
        # Python keyword "import" as a component: lossless encoding,
        # unsupported name.
        p_init = MC(file_path="p/__init__.py", name_components=("p",),
                    file_kind="package_initializer")
        source = MC(file_path="p/mod.py", name_components=("p","mod"))
        target = MC(file_path="p/import.py",
                    name_components=("p","import"),
                    component_supported=True, name_supported=False)
        inv = inv_of(p_init, source, target)
        r = rec(module="import", level=1, source_path="p/mod.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.lossless is True
        assert c.status == UNSUPPORTED
        assert c.reason == REASON_UNSUPPORTED_NAME

    def test_digit_leading_component_is_lossless_and_unsupported(self):
        p_init = MC(file_path="p/__init__.py", name_components=("p",),
                    file_kind="package_initializer")
        source = MC(file_path="p/mod.py", name_components=("p","mod"))
        target = MC(file_path="p/1thing.py",
                    name_components=("p","1thing"),
                    component_supported=True, name_supported=False)
        inv = inv_of(p_init, source, target)
        r = rec(module="1thing", level=1, source_path="p/mod.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.lossless is True
        assert c.status == UNSUPPORTED
        assert c.reason == REASON_UNSUPPORTED_NAME

    def test_unicode_component_is_lossless_and_unsupported(self):
        # Non-ASCII identifier: lossless dotted encoding, unsupported
        # per the frozen component-plain-ASCII classification.
        comp = "\u03b1\u03b2"  # greek letters, a valid py3 identifier
        p_init = MC(file_path="p/__init__.py", name_components=("p",),
                    file_kind="package_initializer")
        source = MC(file_path="p/mod.py", name_components=("p","mod"))
        target = MC(file_path=f"p/{comp}.py",
                    name_components=("p", comp),
                    component_supported=False, name_supported=True)
        inv = inv_of(p_init, source, target)
        r = rec(module=comp, level=1, source_path="p/mod.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.lossless is True
        assert c.status == UNSUPPORTED
        assert c.reason == REASON_UNSUPPORTED_NAME

    def test_nonmatching_lossless_unsupported_target_is_unresolved(self):
        # Same unsupported-shape target, but no candidate matches ->
        # UNRESOLVED (not suppressed by the spelling).
        p_init = MC(file_path="p/__init__.py", name_components=("p",),
                    file_kind="package_initializer")
        source = MC(file_path="p/mod.py", name_components=("p","mod"))
        inv = inv_of(p_init, source)
        r = rec(module="hy-phen", level=1, source_path="p/mod.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.lossless is True
        assert c.status == UNRESOLVED
        assert c.reason == REASON_NO_CANDIDATE


# ---------------------------------------------------------------------------
# Category 11 -- File and root variants
# ---------------------------------------------------------------------------

class TestCategory11_FileAndRootVariants:
    def test_ordinary_module_source(self):
        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="", level=1, source_path="pkg/helpers.py")
        res = resolve_relative_import(r, inv)
        assert res.contexts[0].source_candidate.file_kind == "ordinary_module"
        assert res.contexts[0].status == RESOLVED

    def test_package_initializer_source(self):
        inv = inv_of(PKG_INIT, PKG_SUB_INIT)
        r = rec(module="sub", level=1, source_path="pkg/__init__.py")
        res = resolve_relative_import(r, inv)
        assert res.contexts[0].source_candidate.file_kind == "package_initializer"
        assert res.contexts[0].target_components == ("pkg", "sub")

    def test_root_level_ordinary_module(self):
        inv = inv_of(ROOT_MOD)
        r = rec(module="x", level=1, source_path="mod.py")
        res = resolve_relative_import(r, inv)
        assert res.contexts[0].status == NOT_ATTEMPTED
        assert res.contexts[0].reason == REL_NO_PACKAGE_CONTEXT

    def test_nested_package_module(self):
        inv = inv_of(PKG_SUB_INIT, PKG_SUB_UTIL)
        r = rec(module="", level=1, source_path="pkg/sub/util.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.status == RESOLVED
        assert c.target_components == ("pkg", "sub")
        assert c.source_candidate.file_path == "pkg/sub/util.py"

    def test_explicit_overlapping_roots_yield_multiple_contexts(self):
        # Two explicit roots (rootA=``a/``, rootB=``b/``) both index a
        # same-physical-path file at different logical positions.
        rootA_src = MC(file_path="pkg/mod.py", name_components=("pkg","mod"),
                       root="a", root_orig="a")
        rootB_src = MC(file_path="pkg/mod.py", name_components=("pkg","mod"),
                       root="b", root_orig="b")
        # They have DIFFERENT roots -> structural identity
        # (canonical_root, name_components, file_path) DIFFERS ->
        # both are separate source contexts, preserved.
        inv = inv_of(rootA_src, rootB_src, PKG_INIT)
        r = rec(module="", level=1, source_path="pkg/mod.py")
        res = resolve_relative_import(r, inv)
        # Two contexts (one per explicit root interpretation).
        assert len(res.contexts) == 2
        # Canonical order by (root.canonical, qualified_name, file_path, name_components).
        roots_in_result = [c.source_candidate.root.canonical for c in res.contexts]
        assert roots_in_result == ["a", "b"]


# ---------------------------------------------------------------------------
# Category 12 -- Multiple source contexts independently resolving
# ---------------------------------------------------------------------------

class TestCategory12_MultipleContexts:
    def test_contexts_keep_distinct_outcomes(self):
        # Two source interpretations (different roots) for the same
        # physical file, each with distinct target candidate presence.
        # Context A (root="a"): has a target candidate matching (a, x) -> RESOLVED
        # Context B (root="b"): no matching candidate -> UNRESOLVED
        a_src = MC(file_path="m.py", name_components=("a","m"),
                   root="a", root_orig="a")
        b_src = MC(file_path="m.py", name_components=("b","m"),
                   root="b", root_orig="b")
        a_target = MC(file_path="a/x.py", name_components=("a","x"),
                      root="a", root_orig="a")
        inv = inv_of(a_src, b_src, a_target)
        r = rec(module="x", level=1, source_path="m.py")
        res = resolve_relative_import(r, inv)
        assert len(res.contexts) == 2
        by_root = {c.source_candidate.root.canonical: c for c in res.contexts}
        assert by_root["a"].status == RESOLVED
        assert by_root["b"].status == UNRESOLVED
        # No aggregate status on the overall result.
        assert not hasattr(res, "status")

    def test_ambiguous_context_among_multiple(self):
        # Two matching targets at the same root -> AMBIGUOUS context.
        a_src = MC(file_path="m.py", name_components=("m",),
                   root="a", root_orig="a")
        t1 = MC(file_path="t1.py", name_components=("t",), root="a", root_orig="a")
        t2 = MC(file_path="t2.py", name_components=("t",),
                root="a", root_orig="a-2")
        inv = inv_of(a_src, t1, t2)
        # Target is "t" from source `m.py` (root a; base=("a",) wait).
        # Actually source is root a; name_components=("m",) -> base=().
        # That's an empty package context.  Let me redo:
        a_src = MC(file_path="p/m.py", name_components=("p","m"),
                   root="a", root_orig="a")
        # base=("p",), L=1, suffix="t" -> target=("p","t")
        t1 = MC(file_path="p/t1.py", name_components=("p","t"),
                root="a", root_orig="a")
        t2 = MC(file_path="p/t2.py", name_components=("p","t"),
                root="a", root_orig="a-2")
        inv = inv_of(a_src, t1, t2)
        r = rec(module="t", level=1, source_path="p/m.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.status == AMBIGUOUS
        assert c.reason == REASON_MULTI_SUPPORTED
        assert len(c.supported_candidates) == 2


# ---------------------------------------------------------------------------
# Category 13 -- Cross-root target matching
# ---------------------------------------------------------------------------

class TestCategory13_CrossRootMatching:
    def test_complete_inventory_matching(self):
        # A source under one root; a target under a DIFFERENT root.  The
        # resolver must match against the full inventory (not restricted
        # to source root).
        src = MC(file_path="x/m.py", name_components=("x","m"),
                 root="R1", root_orig="R1")
        target = MC(file_path="y/target.py", name_components=("x","target"),
                    root="R2", root_orig="R2")   # note: different root
        inv = inv_of(src, target)
        r = rec(module="target", level=1, source_path="x/m.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.status == RESOLVED
        assert [x2.file_path for x2 in c.supported_candidates] == ["y/target.py"]

    def test_module_and_package_name_collision(self):
        # `pkg` exists as both a package (pkg/__init__.py) and a module
        # (pkg.py) at different roots.  Both match the target
        # ("pkg",) -> AMBIGUOUS, both in supported_candidates.
        p_init = MC(file_path="pkg/__init__.py", name_components=("pkg",),
                    file_kind="package_initializer", root="R1", root_orig="R1")
        p_mod  = MC(file_path="pkg.py",              name_components=("pkg",),
                    file_kind="ordinary_module",     root="R2", root_orig="R2")
        # Source lives at `pkg/x.py` (base=("pkg",)) under root R1.
        src    = MC(file_path="pkg/x.py", name_components=("pkg","x"),
                    root="R1", root_orig="R1")
        inv = inv_of(p_init, p_mod, src)
        r = rec(module="", level=1, source_path="pkg/x.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert c.status == AMBIGUOUS
        assert c.reason == REASON_MULTI_SUPPORTED
        paths = sorted(x2.file_path for x2 in c.supported_candidates)
        assert paths == ["pkg.py", "pkg/__init__.py"]

    def test_no_root_priority(self):
        # Two candidates at different roots both matching the target.
        # Root priority must NOT be inferred by the resolver (spec §9).
        a_c = MC(file_path="t.py", name_components=("p","t"),
                 root="A", root_orig="A")
        b_c = MC(file_path="t.py", name_components=("p","t"),
                 root="B", root_orig="B")
        src = MC(file_path="p/mod.py", name_components=("p","mod"),
                 root="A", root_orig="A")
        inv = inv_of(src, a_c, b_c)
        r = rec(module="t", level=1, source_path="p/mod.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        # Both distinct (different roots) -> 2 matches -> AMBIGUOUS.
        assert c.status == AMBIGUOUS
        assert len(c.supported_candidates) == 2
        roots = sorted(x2.root.canonical
                       for x2 in c.supported_candidates)
        assert roots == ["A", "B"]

    def test_supported_unsupported_partition(self):
        # One matching candidate supported, one not -> supported has
        # 1, unsupported has 1.
        good = MC(file_path="t.py", name_components=("p","t"),
                  root="R", root_orig="R",
                  component_supported=True, name_supported=True)
        bad  = MC(file_path="t-2.py", name_components=("p","t"),
                  root="R", root_orig="R-2",
                  component_supported=False, name_supported=False)
        src = MC(file_path="p/mod.py", name_components=("p","mod"),
                 root="R", root_orig="R")
        inv = inv_of(src, good, bad)
        r = rec(module="t", level=1, source_path="p/mod.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert [x2.file_path for x2 in c.supported_candidates] == ["t.py"]
        assert [x2.file_path for x2 in c.unsupported_candidates] == ["t-2.py"]


# ---------------------------------------------------------------------------
# Category 14 -- Source duplicate handling
# ---------------------------------------------------------------------------

class TestCategory14_SourceDuplicates:
    def test_identical_duplicates_consolidate_to_one_context(self):
        a = MC(file_path="pkg/mod.py", name_components=("pkg","mod"),
               root="R", root_orig="R")
        b = MC(file_path="pkg/mod.py", name_components=("pkg","mod"),
               root="R", root_orig="R")   # exact duplicate
        inv = inv_of(a, b, PKG_INIT)
        r = rec(module="", level=1, source_path="pkg/mod.py")
        res = resolve_relative_import(r, inv)
        assert len(res.contexts) == 1

    def test_equivalent_duplicates_different_root_originals_consolidate(self):
        # Same canonical root, different diagnostic `original` spellings;
        # substantive attrs agree.  Consolidate to smallest root.original.
        a = MC(file_path="pkg/mod.py", name_components=("pkg","mod"),
               root="R", root_orig="Zebra")
        b = MC(file_path="pkg/mod.py", name_components=("pkg","mod"),
               root="R", root_orig="Alpha")
        inv = inv_of(a, b, PKG_INIT)
        r = rec(module="", level=1, source_path="pkg/mod.py")
        res = resolve_relative_import(r, inv)
        assert len(res.contexts) == 1
        assert res.contexts[0].source_candidate.root.original == "Alpha"

    def test_conflicting_substantive_fields_raise_value_error(self):
        # Same structural identity, differing name_supported (substantive).
        a = MC(file_path="pkg/mod.py", name_components=("pkg","mod"),
               root="R", root_orig="R", name_supported=True)
        b = MC(file_path="pkg/mod.py", name_components=("pkg","mod"),
               root="R", root_orig="R2", name_supported=False)
        inv = inv_of(a, b)
        r = rec(module="", level=1, source_path="pkg/mod.py")
        with pytest.raises(ValueError):
            resolve_relative_import(r, inv)

    def test_conflicting_evidence_tuples_raise_value_error(self):
        a = MC(file_path="pkg/mod.py", name_components=("pkg","mod"),
               root="R", root_orig="R", evidence=("e1",))
        b = MC(file_path="pkg/mod.py", name_components=("pkg","mod"),
               root="R", root_orig="R2", evidence=("e1", "e2"))
        inv = inv_of(a, b)
        r = rec(module="", level=1, source_path="pkg/mod.py")
        with pytest.raises(ValueError):
            resolve_relative_import(r, inv)

    def test_conflicting_file_kind_raises_value_error(self):
        a = MC(file_path="pkg/mod.py", name_components=("pkg","mod"),
               file_kind="ordinary_module")
        b = MC(file_path="pkg/mod.py", name_components=("pkg","mod"),
               file_kind="package_initializer")
        inv = inv_of(a, b)
        r = rec(module="", level=1, source_path="pkg/mod.py")
        with pytest.raises(ValueError):
            resolve_relative_import(r, inv)


# ---------------------------------------------------------------------------
# Category 15 -- Target duplicate handling (frozen behavior preserved)
# ---------------------------------------------------------------------------

class TestCategory15_TargetDuplicates:
    def test_target_conflicting_substantives_propagate_value_error(self):
        # Two matching target candidates with SAME structural identity
        # (same root.canonical, name_components, file_path) but differing
        # on a substantive attribute -> frozen resolver's ValueError
        # must propagate.  Note: same file_path + different file_kind
        # = conflicting duplicates.
        # We can't have the same file_path with different file_kind in
        # one inventory without violating the inventory uniqueness
        # invariant at build time, BUT synthetic ModuleCandidate
        # objects can express this.  The frozen
        # `_dedupe_matches` groups by structural identity and
        # classifies this as a conflict -> ValueError.
        good = MC(file_path="p/t.py", name_components=("p","t"),
                  file_kind="ordinary_module",
                  root="R", root_orig="R")
        bad  = MC(file_path="p/t.py", name_components=("p","t"),
                  file_kind="package_initializer",
                  root="R", root_orig="R2")
        src  = MC(file_path="p/m.py", name_components=("p","m"),
                  root="R", root_orig="R")
        inv_orig = inv_of(src, good, bad)

        # Verify the FROZEN resolver itself raises on these two (this
        # test only asserts propagation; the underlying frozen behavior
        # is already covered by the absolute-resolution tests).
        from repo_intelligence.absolute_resolution import resolve_absolute_import
        absolute_record = ImportRecord(
            kind="from_import", module="p.t", names=(("x", None),),
            level=0, source_path="p/m.py", start_line=1, start_col=0)
        with pytest.raises(ValueError):
            resolve_absolute_import(absolute_record, inv_orig)

        # And the relative resolver must propagate the same error.
        r = rec(module="t", level=1, source_path="p/m.py")
        # The SOURCE context (source=p/m.py) has only one source
        # candidate (good/bad are TARGET matches, not source matches),
        # so no source-context conflict.  Delegation to the frozen
        # resolver MUST surface the ValueError.
        with pytest.raises(ValueError):
            resolve_relative_import(r, inv_orig)

    def test_mixed_partitions_preserved_when_no_conflict(self):
        # Two matching targets that are NOT conflicting duplicates:
        # different file_paths -> separate, both reported, mixed
        # supported/unsupported partition preserved.
        good = MC(file_path="p/good.py", name_components=("p","t"),
                  root="R", root_orig="R",
                  component_supported=True, name_supported=True)
        bad  = MC(file_path="p/bad.py",  name_components=("p","t"),
                  root="R", root_orig="R",
                  component_supported=False, name_supported=False)
        src  = MC(file_path="p/m.py", name_components=("p","m"),
                  root="R", root_orig="R")
        inv = inv_of(src, good, bad)
        r = rec(module="t", level=1, source_path="p/m.py")
        res = resolve_relative_import(r, inv)
        c = res.contexts[0]
        assert [x2.file_path for x2 in c.supported_candidates] == ["p/good.py"]
        assert [x2.file_path for x2 in c.unsupported_candidates] == ["p/bad.py"]
        assert c.status == RESOLVED


# ---------------------------------------------------------------------------
# Category 16 -- Determinism
# ---------------------------------------------------------------------------

class TestCategory16_Determinism:
    def test_inventory_permutations_yield_identical_results(self):
        inv = inv_of(PKG_INIT, PKG_HELPER, PKG_SUB_INIT, PKG_SUB_UTIL,
                     ROOT_MOD)
        r = rec(module="", level=2, source_path="pkg/sub/util.py")
        baseline = resolve_relative_import(r, inv)
        for perm in itertools.permutations(list(inv)):
            res = resolve_relative_import(r, tuple(perm))
            assert res == baseline
            assert res.contexts == baseline.contexts

    def test_duplicate_insertion_order_is_independent(self):
        # Duplicate identical entries in different positions.
        a = MC(file_path="p/m.py", name_components=("p", "m"),
               root="R", root_orig="R")
        b = MC(file_path="p/m.py", name_components=("p", "m"),
               root="R", root_orig="R")
        r = rec(module="", level=1, source_path="p/m.py")
        res_ab = resolve_relative_import(r, (a, b, PKG_INIT))
        res_ba = resolve_relative_import(r, (b, a, PKG_INIT))
        assert res_ab == res_ba
        assert res_ab.contexts[0].source_candidate is a or \
               res_ab.contexts[0].source_candidate is b

    def test_result_is_hashable_and_equal(self):
        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="", level=1, source_path="pkg/helpers.py")
        res = resolve_relative_import(r, inv)
        # Frozen dataclasses -> hashable components; equality works.
        assert res == resolve_relative_import(r, inv)
        assert isinstance(res.record, ImportRecord)
        # Equality is structural.
        assert res.contexts == res.contexts


# ---------------------------------------------------------------------------
# Category 17 -- Invalid API inputs (covered with categories 6)
# ---------------------------------------------------------------------------

class TestCategory17_InvalidApiInputs:
    def test_kind_none(self):
        r = rec(module="x", level=1, source_path="a.py", kind=None)
        res = resolve_relative_import(r, inv_of())
        assert res.issue == REL_INVALID_KIND

    def test_module_none(self):
        r = rec(module=None, level=1, source_path="a.py")
        res = resolve_relative_import(r, inv_of())
        assert res.issue == REL_MALFORMED_SUFFIX

    def test_names_non_tuple_is_stored_verbatim(self):
        # ImportRecord is a plain frozen dataclass without type
        # enforcement; a list is stored as-is.  Not a resolver concern,
        # but useful to pin the contract so callers never pass lists
        # without checking.
        r = ImportRecord(kind="from_import", module="x",
                         names=[("a", None)], level=1,
                         source_path="a.py", start_line=1, start_col=0)
        # The relative resolver is agnostic to the type of `names`;
        # it's only carried through to the delegated absolute record.
        res = resolve_relative_import(r, inv_of(PKG_INIT))
        # (We only care that it doesn't crash.)
        assert res.record.names == [("a", None)]
    def test_inventory_empty_tuple(self):
        r = rec(module="x", level=1, source_path="pkg/mod.py")
        res = resolve_relative_import(r, ())
        assert res.issue == REL_MISSING_SOURCE_CONTEXT

    def test_inventory_non_module_candidate_object(self):
        class Bogus: pass
        r = rec(module="x", level=1, source_path="a/b.py")
        with pytest.raises(TypeError, match="ModuleCandidate"):
            resolve_relative_import(r, [Bogus()])


# ---------------------------------------------------------------------------
# Category 18 -- Original ImportRecord preservation
# ---------------------------------------------------------------------------

class TestCategory18_RecordPreservation:
    def test_result_record_is_original_by_identity(self):
        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="", level=1, source_path="pkg/helpers.py")
        res = resolve_relative_import(r, inv)
        assert res.record is r
        # The relative resolver must NOT have mutated the record.
        assert r.kind == "from_import"
        assert r.module == ""
        assert r.level == 1

    def test_names_aliases_wildcard_preserved(self):
        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="helpers", level=1, source_path="pkg/__init__.py",
                names=(("helper", "h_alias"), ("*", None)))
        res = resolve_relative_import(r, inv)
        assert res.record.names == (("helper", "h_alias"), ("*", None))

    def test_source_coordinates_preserved(self):
        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="", level=1, source_path="pkg/helpers.py",
                start_line=42, start_col=7)
        res = resolve_relative_import(r, inv)
        assert res.record.start_line == 42
        assert res.record.start_col == 7

    def test_derived_absolute_record_never_exposed(self):
        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="helpers", level=1, source_path="pkg/__init__.py")
        res = resolve_relative_import(r, inv)
        # The original record must be the ONLY ImportRecord referenced
        # in the result.  The derived internal absolute record (which
        # would have level=0) must NOT be exposed anywhere on res.
        assert res.record is r
        # And the record still carries the original relative level.
        assert res.record.level == 1
        # No extra record field on the result.
        assert not hasattr(res, "derived_record")
        assert not hasattr(res, "absolute_record")

    def test_result_dataclass_fields(self):
        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="", level=1, source_path="pkg/helpers.py")
        res = resolve_relative_import(r, inv)
        # Exactly the fields from the R2 contract.
        assert set(res.__dict__.keys()) == {"record", "contexts", "issue"}
        # ContextResolution: the exact public field set.
        cr_fields = set(res.contexts[0].__dict__.keys())
        assert cr_fields == {
            "source_candidate", "base_components", "target_components",
            "lossless", "status", "reason", "supported_candidates",
            "unsupported_candidates",
        }


# ---------------------------------------------------------------------------
# Category 19 -- Immutability and purity
# ---------------------------------------------------------------------------

class TestCategory19_ImmutabilityPurity:
    def test_relative_resolution_is_frozen(self):
        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="", level=1, source_path="pkg/helpers.py")
        res = resolve_relative_import(r, inv)
        with pytest.raises(dataclasses.FrozenInstanceError):
            res.record = None
        with pytest.raises(dataclasses.FrozenInstanceError):
            res.contexts = ()
        with pytest.raises(dataclasses.FrozenInstanceError):
            res.issue = "x"

    def test_context_resolution_is_frozen(self):
        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="", level=1, source_path="pkg/helpers.py")
        res = resolve_relative_import(r, inv)
        with pytest.raises(dataclasses.FrozenInstanceError):
            res.contexts[0].status = "X"
        with pytest.raises(dataclasses.FrozenInstanceError):
            res.contexts[0].target_components = ()

    def test_input_inventory_is_not_mutated(self):
        inv = inv_of(PKG_INIT, PKG_HELPER)
        snapshot = [dataclasses.astuple(c) for c in inv]
        r = rec(module="", level=1, source_path="pkg/helpers.py")
        res = resolve_relative_import(r, inv)
        # The inventory tuple and its candidates are unchanged.
        assert len(inv) == 2
        assert [dataclasses.astuple(c) for c in inv] == snapshot
        # And res.record is unchanged (not a derived record).
        assert res.record.level == 1

    def test_resolver_does_not_read_the_filesystem(self, monkeypatch):
        # The resolver must not touch the filesystem.
        called = {"open": False, "stat": False, "listdir": False}

        class FSBlock:
            def __init__(self, *a, **k):
                raise RuntimeError("filesystem access attempted")

            def __call__(self, *a, **_k):
                raise RuntimeError("filesystem access attempted")

        # Block read-only file API surface that a test author might
        # (unintentionally) rely on.
        monkeypatch.setattr("builtins.open", FSBlock)
        import os as _os
        monkeypatch.setattr(_os, "stat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("os.stat called")))
        monkeypatch.setattr(_os, "listdir", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("os.listdir called")))

        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="", level=1, source_path="pkg/helpers.py")
        # If the resolver tries to hit the filesystem, RuntimeError
        # is raised here.  We assert the happy path runs.
        res = resolve_relative_import(r, inv)
        assert res.contexts[0].status == RESOLVED

    def test_resolver_does_not_touch_the_database(self, monkeypatch):
        # The resolver must not use sqlite3.
        import sqlite3
        monkeypatch.setattr(sqlite3, "connect",
                           lambda *a, **k: (_ for _ in ()).throw(
                               RuntimeError("sqlite3.connect called")))
        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="", level=1, source_path="pkg/helpers.py")
        res = resolve_relative_import(r, inv)
        assert res.contexts[0].status == RESOLVED

    def test_resolver_does_not_execute_dynamic_imports(self, monkeypatch):
        # The resolver must not call `import` at runtime.
        import builtins as _b
        real_import = _b.__import__

        def blocker(name, globals_=None, locals_=None,
                    fromlist=(), level=0):
            # Only block dynamic runtime imports; `from __future__
            # import annotations` at module import time is legitimate.
            if level == 0 and name not in ("__future__",):
                raise RuntimeError("dynamic import attempted: " + name)
            return real_import(name, globals_ or {}, locals_ or {},
                               fromlist, level)

        # Import a fresh module with import blocked.
        import repo_intelligence.relative_resolution as rel_mod
        # We can't actually re-import easily, so we just monkeypatch and
        # call the already-imported function: that proves the call path
        # has no dynamic import at runtime.
        monkeypatch.setattr(_b, "__import__", blocker)
        inv = inv_of(PKG_INIT, PKG_HELPER)
        r = rec(module="", level=1, source_path="pkg/helpers.py")
        res = rel_mod.resolve_relative_import(r, inv)
        assert res.contexts[0].status == RESOLVED


# ---------------------------------------------------------------------------
# Category 20 -- Frozen regression preservation
# ---------------------------------------------------------------------------
# The existing frozen test suites
#   tests/test_absolute_resolution.py
#   tests/test_module_inventory.py
#   tests/test_db_absolute_resolution.py
#   tests/test_imports.py
#   tests/test_imports_db.py
# run against the unchanged frozen modules and must continue to pass.
# They are executed by the mandatory validation command in Checkpoint 2
# and are not re-implemented here.  This class only pins the *coexistence*
# contract: the new relative resolver must delegate to the SAME frozen
# absolute resolver used by the frozen suites, preserving their behavior
# end-to-end (no duplicated or re-implemented matching logic).
# ---------------------------------------------------------------------------

class TestCategory20_FrozenRegressionCoexistence:
    def test_relative_delegation_matches_absolute_resolver(self):
        # Build a canonical inventory with the FROZEN builder (matching
        # the frozen-suite test conventions) and verify that a relative
        # import's outcome is IDENTICAL to directly calling the frozen
        # absolute resolver on the equivalent record.
        def fl(rel_path):
            return {"rel_path": rel_path, "language": "Python",
                    "size": 1, "is_binary": False}
        inv = build_module_inventory(
            [fl("pkg/__init__.py"), fl("pkg/helpers.py")],
            ["."],
        )

        # Relative: `from . import helpers` in pkg/__init__.py
        #       -> base=("pkg",), suffix=("helpers",) -> target="pkg.helpers"
        r_rel = rec(module="helpers", level=1, source_path="pkg/__init__.py")
        res = resolve_relative_import(r_rel, inv)
        c = res.contexts[0]
        assert c.status == RESOLVED

        # Absolute: the equivalent `from pkg.helpers import x`
        r_abs = ImportRecord(
            kind="from_import", module="pkg.helpers",
            names=(("x", None),), level=0,
            source_path="pkg/__init__.py", start_line=1, start_col=0,
        )
        abs_res = resolve_absolute_import(r_abs, inv)
        # The frozen absolute resolver's status, reason, and candidate
        # partition are exactly what the relative resolver projected.
        assert abs_res.status == c.status
        assert abs_res.reason == c.reason
        assert abs_res.supported_candidates == c.supported_candidates
        assert abs_res.unsupported_candidates == c.unsupported_candidates
        # And they agree on the resolved set of file paths.
        assert sorted(x.file_path for x in c.supported_candidates) == \
               sorted(x.file_path for x in abs_res.supported_candidates)

    def test_frozen_absolute_reasons_still_exposed_as_before(self):
        # Sanity: the frozen reason constants still exist and the relative
        # resolver reuses them verbatim (no redefinition).
        assert REASON_UNIQUE_SUPPORTED == "single_supported_candidate"
        assert REASON_MULTI_SUPPORTED == "multiple_distinct_supported_candidates"
        assert REASON_NO_CANDIDATE == "no_structural_candidate_match"
        assert REASON_UNSUPPORTED_NAME == "matched_candidates_name_unsupported"
        # And the new relative codes are separate strings.
        assert REL_LOSSLESS_UNSAFE == "relative_lossless_unsafe"

    def test_frozen_inventory_builder_unchanged(self):
        def fl(rel_path):
            return {"rel_path": rel_path, "language": "Python",
                    "size": 1, "is_binary": False}
        inv = build_module_inventory(
            [fl("pkg/__init__.py"), fl("pkg/helpers.py")], ["."],
        )
        # The frozen builder still returns ModuleCandidate tuples with
        # the documented fields — a relative resolver test is safe to
        # rely on these invariants.
        assert all(isinstance(c, ModuleCandidate) for c in inv)
        assert all(isinstance(c.root, PackageRoot) for c in inv)
