"""Comprehensive tests for Python AST import extraction (Phase 2B-1).

Covers all required import semantics on ``extract_file_imports()`` and
``extract_imports_for_files()`` using the ``ImportRecord`` dataclass.

Each name in an ImportRecord.names tuple is ``(name, asname | None)`` —
an immutable source-order pair inherited directly from the AST alias node.
"""

import tempfile
from pathlib import Path

import pytest

from repo_intelligence.python_ast import (
    ImportRecord,
    extract_file_imports,
    extract_imports_for_files,
)


# ── Fixture helpers ────────────────────────────────────────────────────────


def _write_temp(src: str, suffix: str = ".py") -> Path:
    """Write *src* into a temporary file and return its path."""
    tmp = tempfile.NamedTemporaryFile(
        suffix=suffix, mode="w", delete=False, encoding="utf-8",
    )
    tmp.write(src)
    tmp.close()
    return Path(tmp.name)


# ── Helper to extract (name, asname) pairs ────────────────────────────────


def _names(record: ImportRecord) -> list[str]:
    """Return imported names from a record in iteration order."""
    return [n[0] for n in record.names]


def _asnames(record: ImportRecord) -> list[str | None]:
    """Return asname values from a record in iteration order."""
    return [n[1] for n in record.names]


# ── 1. Simple `import` (Issue-1 corrected: module is never None, always the dotted name) ────────────────────────────────


class TestSimpleImport:

    def test_single_import(self):
        src = "import os\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 1
        rec = result[0]
        assert rec.kind == "import"
        assert rec.module == "os"  # corrected: module is the dotted name, never None
        assert len(rec.names) == 1

    def test_single_import_name(self):
        src = "import os\n"
        result = extract_file_imports("mod.py", src)
        rec = result[0]
        assert _names(rec) == ["os"]
        assert _asnames(rec) == [None]

    def test_simple_import_module_not_none(self):
        """Phase-1-corrected assertion: module is always a str."""
        src = "import os\n"
        result = extract_file_imports("mod.py", src)
        assert isinstance(result[0].module, str) and result[0].module == "os"

    def test_single_import_start_line(self):
        src = "\n\nimport os\n"
        result = extract_file_imports("mod.py", src)
        assert result[0].start_line == 3


# ── 2. Dotted module import (Issue-1 corrected: module holds the dotted name) ────────────


class TestDottedModuleImport:

    def test_dotted_module_os_path(self):
        src = "import os.path\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 1
        rec = result[0]
        assert rec.kind == "import"
        assert rec.module == "os.path"  # corrected: module captures the dotted target

    def test_dotted_module_preserves_name(self):
        """Names contain (dotted_name, alias_or_None)."""
        src = "import os.path\n"
        result = extract_file_imports("mod.py", src)
        assert _names(result[0]) == ["os.path"]


# ── 3. Multiple names (one import statement → multiple records) ─────────────


class TestMultipleNamesInImport:

    def test_import_os_sys(self):
        src = "import os, sys\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 2
        all_names = sorted([_names(r)[0] for r in result])
        assert all_names == ["os", "sys"]


# ── 4. Aliased import ──────────────────────────────────────────────────────


class TestAliasedImport:

    def test_numpy_as_np(self):
        src = "import numpy as np\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 1
        rec = result[0]
        assert _names(rec) == ["numpy"]
        assert _asnames(rec) == ["np"]


# ── 5. Basic `from ... import ...` ────────────────────────────────────────


class TestFromImport:

    def test_basic_from_import(self):
        src = "from pathlib import Path\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 1
        rec = result[0]
        assert rec.kind == "from_import"
        assert rec.module == "pathlib"
        assert _names(rec) == ["Path"]

    def test_from_import_no_alias(self):
        src = "from os import path\n"
        result = extract_file_imports("mod.py", src)
        rec = result[0]
        assert rec.kind == "from_import"
        assert _names(rec) == ["path"]
        assert _asnames(rec) == [None]


# ── 6. Multiple names in a from-import (Issue-3 corrected: source order, not alphabetical) ────────────


class TestMultipleNamesFromImport:

    def test_from_package_import_a_b(self):
        src = "from package import a, b\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 1
        rec = result[0]
        assert rec.kind == "from_import"
        assert rec.module == "package"
        # Source order preserved (also alphabetical here by coincidence)
        assert _names(rec) == ["a", "b"]

    def test_from_package_import_zebra_alpha_source_order(self):
        """Issue-3 fixed: names follow AST source order, not alphabetical."""
        src = "from package import zebra, alpha\n"
        result = extract_file_imports("mod.py", src)
        assert _names(result[0]) == ["zebra", "alpha"]  # source order

    def test_from_package_reverse_cba(self):
        """Source order is deterministic and NOT alphabetical."""
        src = "from pkg import c, b, a\n"
        result = extract_file_imports("mod.py", src)
        assert _names(result[0]) == ["c", "b", "a"]  # preserved


# ── 7. Aliased from-import ────────────────────────────────────────────────


class TestAliasedFromImport:

    def test_from_package_import_thing_as_alias(self):
        src = "from package import thing as alias\n"
        result = extract_file_imports("mod.py", src)
        rec = result[0]
        assert rec.kind == "from_import"
        assert _names(rec) == ["thing"]
        assert _asnames(rec) == ["alias"]

    def test_multiple_aliased_from(self):
        src = "from typing import Dict as Mapping, List\n"
        result = extract_file_imports("mod.py", src)
        names = [n[0] for n in result[0].names]
        assert names == ["Dict", "List"]  # source order preserved


# ── 8. Dotted from-module ────────────────────────────────────────────────


class TestDottedFromModule:

    def test_from_package_submodule_import(self):
        src = "from package.submodule import thing\n"
        result = extract_file_imports("mod.py", src)
        rec = result[0]
        assert rec.kind == "from_import"
        assert rec.module == "package.submodule"

    def test_deeply_dotted_module(self):
        src = "import os.path.join\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 1
        rec = result[0]
        assert rec.kind == "import"
        assert rec.module == "os.path.join"


# ── 9. Wildcard import ────────────────────────────────────────────────────


class TestWildcardImport:

    def test_from_package_import_star(self):
        src = "from package import *\n"
        result = extract_file_imports("mod.py", src)
        rec = result[0]
        assert rec.kind == "from_import"
        assert rec.module == "package"
        assert len(rec.names) == 1
        assert _names(rec) == ["*"]
        assert _asnames(rec) == [None]


# ── 10. Relative level 1 import (`from . import sibling`) ────────────────


class TestRelativeLevel1:

    def test_from_dot_import_sibling(self):
        src = "from . import sibling\n"
        result = extract_file_imports("mod.py", src)
        rec = result[0]
        assert rec.kind == "from_import"
        assert rec.module == ""  # empty for bare `from .`
        assert rec.level == 1
        assert _names(rec) == ["sibling"]

    def test_from_dot_sibling_as_alias(self):
        src = "from . import sibling as s\n"
        result = extract_file_imports("mod.py", src)
        rec = result[0]
        assert rec.level == 1
        assert _names(rec) == ["sibling"]
        assert _asnames(rec) == ["s"]


# ── 11. Relative level 2+ import ─────────────────────────────────────────


class TestRelativeLevel2:

    def test_from_two_dot_import_parent(self):
        src = "from .. import parent_sibling\n"
        result = extract_file_imports("mod.py", src)
        rec = result[0]
        assert rec.level == 2
        assert rec.module == ""
        assert _names(rec) == ["parent_sibling"]

    def test_from_two_dot_package(self):
        src = "from ..package import thing\n"
        result = extract_file_imports("mod.py", src)
        rec = result[0]
        assert rec.level == 2
        assert rec.module == "package"


# ── 12. `from .sibling import name` ──────────────────────────────────────


class TestFromDotSiblingImport:

    def test_from_dot_sibling_import_thing(self):
        src = "from .sibling import thing\n"
        result = extract_file_imports("mod.py", src)
        rec = result[0]
        assert rec.kind == "from_import"
        assert rec.level == 1
        assert rec.module == "sibling"
        assert _names(rec) == ["thing"]


# ── 13. Nested imports ───────────────────────────────────────────────────


class TestNestedImports:

    def test_import_inside_function(self):
        src = "def foo():\n    import json\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 1
        assert _names(result[0]) == ["json"]

    def test_import_inside_async_function(self):
        src = "async def bar():\n    import asyncio\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 1
        assert _names(result[0]) == ["asyncio"]

    def test_import_inside_method(self):
        src = """class C:
    def method(self):
        import hashlib
"""
        result = extract_file_imports("mod.py", src)
        assert len(result) == 1
        assert _names(result[0]) == ["hashlib"]

    def test_import_inside_try_block(self):
        src = "try:\n    import special_module\nexcept ImportError:\n    pass\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 1
        assert _names(result[0]) == ["special_module"]

    def test_import_inside_conditional(self):
        src = "if True:\n    import conditional_mod\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 1
        assert _names(result[0]) == ["conditional_mod"]

    def test_import_inside_loop(self):
        src = "for i in range(3):\n    import loop_mod\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 1
        assert _names(result[0]) == ["loop_mod"]

    def test_import_inside_with(self):
        src = "import builtins\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) >= 1


# ── 14. Imports inside conditional / try / function / class constructs ────


class TestMixedNestedImports:

    def test_top_level_and_nested(self):
        src = """import top_mod

def outer():
    import func_mod

class C:
    def method(self):
        import method_mod
"""
        result = extract_file_imports("mod.py", src)
        all_names = sorted(r.names[0][0] for r in result)
        assert all_names == ["func_mod", "method_mod", "top_mod"]


# ── 15. Deterministic source ordering ────────────────────────────────────


class TestDeterministicOrdering:

    def test_source_order_preserved(self):
        src = """import zebra
import alpha
from pkg import charlie
from mod import beta
"""
        result = extract_file_imports("mod.py", src)
        # First two are `import` statements (in source order: zebra, alpha)
        assert result[0].kind == "import"
        assert _names(result[0]) == ["zebra"]

    def test_valid_multi_import_ordering(self):
        src = "import z; import a\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 2

    def test_multiline_from_import_line_number(self):
        src = """\
from first import A
from second import B
"""
        result = extract_file_imports("mod.py", src)
        from_imps = [r for r in result if r.kind == "from_import"]
        assert len(from_imps) == 2
        assert from_imps[0].start_line == 1
        assert from_imps[1].start_line == 2

    def test_names_order_preserved_within_statement(self):
        """Names within a single ``from X import a, b`` keep source order."""
        src = "from pkg import charlie, alpha, zebra\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 1
        assert _names(result[0]) == ["charlie", "alpha", "zebra"]

    def test_nested_mixed_source_order(self):
        """Nested imports interleaved with top-level statements return file-order.

        The AST traversal (depth-first) would find ``second`` and ``third``
        before ``fourth``, but output must be **sorted by start_line** so the
        order matches source appearance:

            import first       -- line 1
            def outer():       -- line 3
                import second   -- line 4
                if True:        -- line 5
                    import third -- line 6
            import fourth      -- line 8
        """
        src = (
            "import first\n"
            "\n"
            "def outer():\n"
            "    import second\n"
            "    if True:\n"
            "        import third\n"
            "\n"
            "import fourth\n"
        )
        result = extract_file_imports("mod.py", src)
        assert len(result) == 4
        names = [r.names[0][0] for r in result]
        assert names == ["first", "second", "third", "fourth"]

    def test_nested_before_top_level_source_order(self):
        """Imports inside a construct that begins before top-level imports
        must still appear in file-order after sorting."""
        src = (
            "def first_func():\n"
            "    import alpha\n"
            "\n"
            "class Container:\n"
            "    def method(self):\n"
            "        import beta\n"
            "\n"
            "import gamma\n"
        )
        result = extract_file_imports("mod.py", src)
        assert len(result) == 3
        names = [r.names[0][0] for r in result]
        # alpha (line 2) before beta (line 6) before gamma (line 10)
        assert names == ["alpha", "beta", "gamma"]

    def test_stable_sort_preserves_alias_order(self):
        """When aliases share the same line, stable sort keeps their write order."""
        src = "import zebra, alpha\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 2
        # Both on line 1; stable sort preserves definition order within a node
        names = [r.names[0][0] for r in result]
        assert names == ["zebra", "alpha"]


# ── 16. Source line capture ──────────────────────────────────────────────


class TestSourceLineCapture:

    def test_multiline_import_lines(self):
        src = """
x = 1
import first_mod    # line 3
y = 2
import second_mod  # line 5
z = 3
"""
        result = extract_file_imports("mod.py", src)
        assert len(result) == 2
        assert result[0].start_line == 3
        assert result[1].start_line == 5

    def test_from_import_lines(self):
        src = """
from a import A  # line 2
import B         # line 3
"""
        result = extract_file_imports("mod.py", src)
        from_imps = [r for r in result if r.kind == "from_import"]
        assert len(from_imps) >= 1
        assert from_imps[0].start_line == 2


# ── 17. Syntax / parse failure behavior ─────────────────────────────────


class TestParseFailureBehavior:

    def test_syntax_error_returns_empty(self):
        src = "import\nbad syntax"
        result = extract_file_imports("mod.py", src)
        assert result == []

    def test_malformed_from_returns_empty(self):
        src = "from import\nnot valid"
        result = extract_file_imports("mod.py", src)
        assert result == []

    def test_severe_syntax_error(self):
        src = "class { }\ndef "  # Multiple syntax errors
        result = extract_file_imports("mod.py", src)
        assert result == []


# ── 18. No imports ────────────────────────────────────────────────────────


class TestNoImports:

    def test_empty_source(self):
        result = extract_file_imports("mod.py", "")
        assert result == []

    def test_all_constants_no_imports(self):
        src = "x = 1\nclass Foo:\n    pass\ndef bar(): pass\n"
        result = extract_file_imports("mod.py", src)
        assert result == []


# ── Batch extraction tests ────────────────────────────────────────────────


class TestExtractImportsForFiles:

    def test_batch_extracts_all(self):
        p1 = _write_temp("import os\n")
        p2 = _write_temp("from pathlib import Path\n")
        records, stats = extract_imports_for_files([
            {"rel_path": "a.py", "abs_path": str(p1)},
            {"rel_path": "b.py", "abs_path": str(p2)},
        ])
        assert len(records) == 2
        all_names = sorted(r.names[0][0] for r in records)
        assert all_names == ["Path", "os"]

    def test_batch_stats(self):
        p1 = _write_temp("import os\n")
        records, stats = extract_imports_for_files([
            {"rel_path": "mod.py", "abs_path": str(p1)},
        ])
        assert stats["total_python"] >= 1
        assert stats["parsed_ok"] >= 0

    def test_batch_non_python_skipped(self):
        p_txt = _write_temp("data", suffix=".txt")
        records, _ = extract_imports_for_files([
            {"rel_path": "file.txt", "abs_path": str(p_txt)},
        ])
        assert len(records) == 0

    def test_batch_parse_error_file(self):
        p_bad = _write_temp("class {\n\n")
        records, stats = extract_imports_for_files([
            {"rel_path": "bad.py", "abs_path": str(p_bad)},
        ])
        assert stats["parsed_ok"] >= 0


# ── ImportRecord immutability tests (Issue-2 fixed) ─────────────────────


class TestImportRecordType:

    def test_import_record_frozen(self):
        """Top-level frozen dataclass prevents field reassignment."""
        rec = ImportRecord(
            kind="import",
            module="os",
            names=(("os", None),),
            level=0,
            source_path="mod.py",
            start_line=1,
            start_col=0,
        )
        with pytest.raises(AttributeError):
            rec.kind = "x"

    def test_import_record_equality(self):
        r1 = ImportRecord(
            kind="from_import", module="pathlib",
            names=(("Path", None),), level=0, source_path="a.py", start_line=1, start_col=0,
        )
        r2 = ImportRecord(
            kind="from_import", module="pathlib",
            names=(("Path", None),), level=0, source_path="a.py", start_line=1, start_col=0,
        )
        assert r1 == r2

    def test_from_import_record_fields(self):
        rec = ImportRecord(
            kind="from_import",
            module="mymod.submod",
            names=(("Foo", "F"),),
            level=0,
            source_path="x.py",
            start_line=5,
            start_col=42,
        )
        assert rec.kind == "from_import"
        assert rec.module == "mymod.submod"
        assert len(rec.names) == 1
        assert rec.names[0] == ("Foo", "F")


class TestImmutability:
    """Verify that nested contents inside ImportRecord are genuinely immutable."""

    def _make_from_record(self):
        src = "from pkg import a, b\n"
        result = extract_file_imports("mod.py", src)
        return result[0]  # mutable-tuple names

    def test_names_tuple_is_immutable(self):
        """The tuple wrapping names must raise on reassignment."""
        rec = self._make_from_record()
        original = rec.names
        with pytest.raises((TypeError, Exception)):  # FrozenInstanceError is a subclass of Exception
            rec.names = (("x", None),)
        assert rec.names is original

    def test_names_tuple_contents_cannot_be_mutated(self):
        """Tuple elements are (name, asname) str-tuples which cannot be
        mutated — tuples are immutable by construction."""
        rec = self._make_from_record()
        # Reassignment of the entire tuple field is blocked by frozen dataclass.
        with pytest.raises((TypeError, Exception)):
            rec.names = ("x", None),  # reassigning raises FrozenInstanceError

    def test_name_string_is_immutably_small(self):
        """Each name value is a simple Python str — immutable by language."""
        rec = self._make_from_record()
        for entry in rec.names:
            # entry[0] and entry[1] are strs (or None) which are immutable.
            assert isinstance(entry[0], str)

    def test_names_iteration_yields_tuples(self):
        """Iterating names yields 2-tuples, not dicts."""
        src = "from pkg import foo as bar\n"
        result = extract_file_imports("mod.py", src)
        rec = result[0]
        for entry in rec.names:
            assert isinstance(entry, tuple)
            assert len(entry) == 2

    def test_module_is_str_for_simple_import(self):
        """For `import X`, module is the name (never None)."""
        result = extract_file_imports("mod.py", "import os\n")
        rec = result[0]
        assert isinstance(rec.module, str)

    def test_module_is_str_for_dotted_import(self):
        """For `import os.path`, module is the dotted string."""
        result = extract_file_imports("mod.py", "import os.path\n")
        rec = result[0]
        assert isinstance(rec.module, str)
        assert rec.module == "os.path"

    def test_module_is_str_for_from_import(self):
        """For `from pkg import X`, module is the source module."""
        result = extract_file_imports("mod.py", "from pathlib import Path\n")
        rec = result[0]
        assert isinstance(rec.module, str)
        assert rec.module == "pathlib"


# ── Relative import comprehensive tests ──────────────────────────────────


class TestRelativeImportsComprehensive:

    def test_from_dot_pkg_import_thing(self):
        src = "from .pkg import thing\n"
        result = extract_file_imports("mod.py", src)
        rec = result[0]
        assert rec.level == 1
        assert rec.module == "pkg"
        names = [n[0] for n in rec.names]
        assert names == ["thing"]


# ── Same-line import ordering tests (Phase 2B-1: start_col + (line, col) sort) ─

class TestSameLineImportOrdering:

    def test_two_top_level_imports_on_one_physical_line(self):
        """import first; import second — records must appear in source order."""
        src = "import first; import second\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 2
        # Both should be sorted by start_col: 'import first' at col 0, 'import second' at col 14
        assert result[0].start_col < result[1].start_col
        names = [r.names[0][0] for r in result]
        assert names == ["first", "second"]

    def test_multiple_same_line_imports_inside_nested_construct(self):
        """Imports inside a function body on one line must be col-ordered."""
        src = "def f():\n    import nested_a; import nested_b\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 2
        names = [r.names[0][0] for r in result]
        from_first, from_second = result[0], result[1]
        # col_offset of 'import' on right side > col_offset of 'import' on left
        assert from_first.start_col < from_second.start_col
        assert names == ["nested_a", "nested_b"]

    def test_interleaving_same_line_and_different_line_imports(self):
        """Mix of distinct-line and same-line statements must all be in source order."""
        src = """\nimport oneline_1; import oneline_2\nimport after_both\n"""
        result = extract_file_imports("mod.py", src)
        assert len(result) == 3
        names = [r.names[0][0] for r in result]
        assert names == ["oneline_1", "oneline_2", "after_both"]
        # The oneline imports should have col_offset < the after_both import's start_line line offset
        # (after_both is on a new line, so its start_line > oneline_2.start_line)

    def test_start_col_values_distinguish_separate_statements(self):
        """start_col must capture real column offsets to distinguish same-line statements."""
        src = "import first; import second\n"
        result = extract_file_imports("mod.py", src)
        assert result[0].start_line == 1
        assert result[1].start_line == 1
        # Different col_offset values must be present so they can be ordered
        assert result[0].start_col != result[1].start_col
        assert result[0].start_col < result[1].start_col

    def test_same_statement_aliases_preserve_source_order(self):
        """Multiple aliases from ONE import statement preserve alias order even when
        they share the exact same (start_line, start_col)."""
        src = "import zebra, alpha\n"
        result = extract_file_imports("mod.py", src)
        assert len(result) == 2
        # Both records have identical (start_line, start_col) because they come from one AST node
        assert result[0].start_line == result[1].start_line
        assert result[0].start_col == result[1].start_col
        # Stable sort must preserve their definition order (zebra, then alpha)
        names = [r.names[0][0] for r in result]
        assert names == ["zebra", "alpha"]

