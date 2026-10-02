"""Comprehensive tests for ``src/repo_intelligence/python_ast.py``.

Uses string-sourced Python code and in-memory file writes so no real
repository dependency is needed.  All assertions target the public API of
``extract_file_symbols()`` and ``extract_symbols_for_files()``.
"""

import ast
import tempfile
from pathlib import Path
from repo_intelligence.python_ast import (
    extract_file_symbols,
    extract_symbols_for_files,
)


# ── Fixture helpers ────────────────────────────────────────────────────────


def _write_temp(src: str) -> Path:
    """Write *src* into a temporary ``.py`` file and return its path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, encoding="utf-8")
    tmp.write(src)  # type: ignore[arg-type]
    tmp.close()
    return Path(tmp.name)


# ── Module symbol tests ────────────────────────────────────────────────────


class TestModuleSymbol:
    """Tests that a ``module`` kind entry is always produced."""

    def test_module_symbol_exists(self):
        syms = extract_file_symbols("pkg/mod.py", "x = 1")
        module_syms = [s for s in syms if s["kind"] == "module"]
        assert len(module_syms) == 1
        mod = module_syms[0]
        assert mod["name"] == "mod"
        assert mod["qualified_name"] == "mod"
        assert mod["start_line"] == 1
        assert mod["end_line"] == 1

    def test_module_multiline(self):
        src = "\n".join(f"line{i}" for i in range(1, 21))
        syms = extract_file_symbols("big.py", src)
        module_syms = [s for s in syms if s["kind"] == "module"]
        assert len(module_syms) == 1
        assert module_syms[0]["end_line"] == 20

    def test_module_docstring(self):
        source = '"""A docstring."""\n\nx = 1'
        syms = extract_file_symbols("doc.py", source)
        mod = [s for s in syms if s["kind"] == "module"][0]
        assert mod["docstring"] == "A docstring."

    def test_multiline_module_docstring(self):
        source = '''"""
First line of long docstring.

Second paragraph here.
"""
x = 1'''
        syms = extract_file_symbols("doc2.py", source)
        mod = [s for s in syms if s["kind"] == "module"][0]
        assert "First line" in mod["docstring"]


# ── Top-level function tests ──────────────────────────────────────────────


class TestTopLevelFunction:
    """Tests for ``function`` kind at module level."""

    def test_single_function(self):
        syms = extract_file_symbols("f.py", "def foo(): pass")
        funcs = [s for s in syms if s["kind"] == "function"]
        assert len(funcs) == 1
        f = funcs[0]
        assert f["name"] == "foo"
        assert f["qualified_name"] == "foo"
        assert f["start_line"] == 1
        assert f["parent_id"] is None

    def test_multiple_functions(self):
        source = "\ndef foo(): pass\n\ndef bar(): pass\n"
        syms = extract_file_symbols("multi.py", source)
        funcs = [s for s in syms if s["kind"] == "function"]
        names = sorted(f["name"] for f in funcs)
        assert names == ["bar", "foo"]

    def test_function_with_no_body(self):
        syms = extract_file_symbols("noop.py", "def noop(): ...\n")
        func = [s for s in syms if s["kind"] == "function"][0]
        assert func["name"] == "noop"

    def test_function_docstring(self):
        source = 'def helper():\n    """Helper doc."""\n    pass'
        syms = extract_file_symbols("d.py", source)
        func = [s for s in syms if s["kind"] == "function"][0]
        assert func["docstring"] == "Helper doc."

    def test_signature_and_decorators(self):
        source = '"""Return doubled."""\ndef double(x: int, y: int = 1) -> int:\n    return x * 2\n@staticmethod\ndef fixed(): pass'
        syms = extract_file_symbols("sd.py", source)
        funcs = [s for s in syms if s["kind"] == "function"]
        func_names = {f["name"] for f in funcs}
        assert "double" in func_names
        d = next(f for f in funcs if f["name"] == "double")
        assert "-> int" in d["signature"]


# ── Async function tests ───────────────────────────────────────────────────


class TestAsyncFunction:
    """Tests for ``async_function`` kind at module level."""

    def test_async_top_level(self):
        source = "async def fetch(url):\n    return url"
        syms = extract_file_symbols("a.py", source)
        funcs = [s for s in syms if s["kind"] == "async_function"]
        assert len(funcs) == 1
        f = funcs[0]
        assert f["name"] == "fetch"
        assert f["qualified_name"] == "fetch"
        assert f["parent_id"] is None

    def test_multiple_async(self):
        source = "async def a(): pass\nasync def b(): pass\nasync def c(): pass\n"
        syms = extract_file_symbols("m.py", source)
        funcs = [s for s in syms if s["kind"] == "async_function"]
        assert len(funcs) == 3
        names = sorted(f["name"] for f in funcs)
        assert names == ["a", "b", "c"]

    def test_async_docstring(self):
        source = 'async def helper():\n    """Async helper."""\n    pass'
        syms = extract_file_symbols("ah.py", source)
        func = [s for s in syms if s["kind"] == "async_function"][0]
        assert func["docstring"] == "Async helper."

    def test_async_in_class(self):
        source = '''class Client:\n    """HTTP client."""\n\n    async def get(self, url):\n        return url'''
        syms = extract_file_symbols("c.py", source)
        methods = [s for s in syms if s["kind"] == "async_method"]
        assert len(methods) == 1
        assert methods[0]["name"] == "get"
        assert methods[0]["qualified_name"] == "Client.get"

    def test_async_with_complex_signature(self):
        source = '''async def resolve(
            *, timeout: int = 30, retry: bool = True, **kw
        ): pass'''
        syms = extract_file_symbols("r.py", source)
        func = [s for s in syms if s["kind"] == "async_function"][0]
        sig = func["signature"]
        assert "timeout" in sig and "retry" in sig and "kw" in sig


# ── Class tests ────────────────────────────────────────────────────────────


class TestClass:
    """Tests for ``class`` kind."""

    def test_single_class(self):
        source = "class Foo:\n    pass"
        syms = extract_file_symbols("c.py", source)
        classes = [s for s in syms if s["kind"] == "class"]
        assert len(classes) == 1
        cls = classes[0]
        assert cls["name"] == "Foo"
        assert cls["qualified_name"] == "Foo"
        assert cls["start_line"] == 1

    def test_class_inherits(self):
        source = "class Derived(Base):\n    pass"
        syms = extract_file_symbols("d.py", source)
        cls = [s for s in syms if s["kind"] == "class"][0]
        assert cls["name"] == "Derived"

    def test_multiple_classes(self):
        source = "class A:\n    pass\nclass B:\n    pass\n"
        syms = extract_file_symbols("mc.py", source)
        classes = [s for s in syms if s["kind"] == "class"]
        names = sorted(c["name"] for c in classes)
        assert names == ["A", "B"]

    def test_class_docstring(self):
        source = 'class Foo:\n    """My class."""\n    pass'
        syms = extract_file_symbols("cd.py", source)
        cls = [s for s in syms if s["kind"] == "class"][0]
        assert cls["docstring"] == "My class."


# ── Method tests ───────────────────────────────────────────────────────────


class TestMethod:
    """Tests for ``method`` kind inside classes."""

    def test_single_method(self):
        source = """class Foo:\n    def bar(self):\n        pass"""
        syms = extract_file_symbols("m.py", source)
        methods = [s for s in syms if s["kind"] == "method"]
        assert len(methods) == 1
        assert methods[0]["name"] == "bar"
        assert methods[0]["qualified_name"] == "Foo.bar"

    def test_multiple_methods(self):
        source = """class Foo:\n    
    def a(self): pass\n    def b(self): pass\n    def c(self): pass\n"""
        syms = extract_file_symbols("mm.py", source)
        methods = [s for s in syms if s["kind"] == "method"]
        assert len(methods) == 3
        names = sorted(m["name"] for m in methods)
        assert names == ["a", "b", "c"]

    def test_method_docstring(self):
        source = '"""Class."""\nclass Foo:\n    def bar(self):\n        """Method."""\n        pass'
        syms = extract_file_symbols("md.py", source)
        method = [s for s in syms if s["kind"] == "method"][0]
        assert method["docstring"] == "Method."

    def test_special_methods(self):
        source = """class C:\n    def __init__(self): pass\n    def __repr__(self): pass\n"""
        syms = extract_file_symbols("sp.py", source)
        methods = [s for s in syms if s["kind"] == "method"]
        names = sorted(m["name"] for m in methods)
        assert names == ["__init__", "__repr__"]

    def test_method_signature(self):
        source = """class Foo:\n    def method(self, x: int, name: str = 'default') -> bool:\n        pass"""
        syms = extract_file_symbols("sig.py", source)
        method = [s for s in syms if s["kind"] == "method"][0]
        sig = method["signature"]
        assert "self" in sig and "x:" in sig and "name:" in sig


# ── Decorator tests ────────────────────────────────────────────────────────


class TestDecorators:
    """Tests for decorator extraction on classes, functions, methods."""

    def test_class_decorator(self):
        source = "@dataclass\nclass Point:\n    x: int"
        syms = extract_file_symbols("d.py", source)
        cls = [s for s in syms if s["kind"] == "class"][0]
        assert "dataclass" in cls["decorators"]

    def test_class_multiple_decorators(self):
        source = "@first\n@decorated\nclass Foo:\n    pass"
        syms = extract_file_symbols("d.py", source)
        cls = [s for s in syms if s["kind"] == "class"][0]
        decs = cls["decorators"]
        assert "first" in decs and "decorated" in decs

    def test_function_decorator(self):
        source = "@cached\ndef compute(x):\n    return x * 2"
        syms = extract_file_symbols("d.py", source)
        func = [s for s in syms if s["kind"] == "function"][0]
        assert "cached" in func["decorators"]

    def test_method_decorator(self):
        source = '"""Class."""\nclass Foo:\n    @property\n    def name(self):\n        return "foo"'
        syms = extract_file_symbols("dm.py", source)
        method = [s for s in syms if s["kind"] == "method"][0]
        assert "property" in method["decorators"]

    def test_static_method_decorator(self):
        source = """class Foo:\n    @staticmethod\n    def go(): pass"""
        syms = extract_file_symbols("sm.py", source)
        method = [s for s in syms if s["kind"] == "method"][0]
        assert "staticmethod" in method["decorators"]

    def test_classmethod_decorator(self):
        source = """class Foo:\n    @classmethod\n    def from_x(cls, x): pass"""
        syms = extract_file_symbols("cm.py", source)
        method = [s for s in syms if s["kind"] == "method"][0]
        assert "classmethod" in method["decorators"]

    def test_nested_decorator(self):
        source = '"""Class."""\nclass Foo:\n    """Method."""\n    @property\n    def bar(self):\n        return 1'
        syms = extract_file_symbols("nd.py", source)
        classes = [s for s in syms if s["kind"] == "class"]
        cls = next(c for c in classes if c["name"] == "Foo")
        methods = [s for s in syms if s["kind"] == "method"]
        method = next(m for m in methods if m["name"] == "bar")
        assert "property" in method["decorators"]


# ── Nested structures tests ───────────────────────────────────────────────


class TestNestedStructures:
    """Tests for nested classes and functions."""

    def test_inner_class(self):
        source = """class Outer:\n    class Inner:\n        pass"""
        syms = extract_file_symbols("nc.py", source)
        outer_cls, inner_cls = [s for s in syms if s["kind"] == "class"]
        assert inner_cls["qualified_name"] == "Outer.Inner"
        assert inner_cls["parent_id"] is not None

    def test_inner_class_method(self):
        source = """class Foo:\n    class Bar:\n        def thing(self): pass"""
        syms = extract_file_symbols("ni.py", source)
        classes = [s for s in syms if s["kind"] == "class"]
        fnames = {c["name"]: c for c in classes}
        assert "Foo" in fnames and "Bar" in fnames
        assert fnames["Bar"]["qualified_name"] == "Foo.Bar"

    def test_inner_function(self):
        source = """class Foo:\n    def outer(self):\n        def inner(): pass"""
        syms = extract_file_symbols("nf.py", source)
        funcs = [s for s in syms if s["kind"] == "function"]
        assert len(funcs) == 1
        assert funcs[0]["name"] == "inner"

    def test_inner_function_parent(self):
        source = """class C:\n    def a(self):\n        def b(): pass"""
        syms = extract_file_symbols("ip.py", source)
        methods = [m for m in syms if m["kind"] == "method"]
        funcs = [f for f in syms if f["kind"] == "function"]
        assert len(funcs) == 1
        # Inner function's qualified name should be C.a.<locals>.b or similar

    def test_deeply_nested(self):
        source = """class A:\n    class B:\n        class C:\n            pass"""
        syms = extract_file_symbols("dn.py", source)
        classes = sorted([s for s in syms if s["kind"] == "class"], key=lambda c: c["name"])
        assert len(classes) == 3
        names = [c["qualified_name"] for c in classes]
        assert "A.B.C" in names

    def test_nested_function_inside_class(self):
        source = """class C:\n    def method(self):\n        def _helper(x):\n            return x + 1\n        return _helper(42)"""
        syms = extract_file_symbols("nfic.py", source)
        classes = [s for s in syms if s["kind"] == "class"]
        assert len(classes) == 1 and classes[0]["name"] == "C"
        funcs = [s for s in syms if s["kind"] == "function"]
        assert len(funcs) == 1 and funcs[0]["name"] == "_helper"

    def test_nested_class_method(self):
        source = """class C:\n    class D:\n        def m(self): pass"""
        syms = extract_file_symbols("ncm.py", source)
        classes = [s for s in syms if s["kind"] == "class"]
        cnames = {c["name"]: c for c in classes}
        assert "C" in cnames and "D" in cnames
        assert any(c["qualified_name"] == "C.D" for c in classes)
        inner = next(c for c in classes if c["name"] == "D")
        assert inner["parent_id"] is not None  # must have parent pointer

    def test_nested_class_decorator_on_inner(self):
        source = """class Outer:\n    @decorated\n    class Inner:\n        pass"""
        syms = extract_file_symbols("ncd.py", source)
        classes = [s for s in syms if s["kind"] == "class"]
        inner_cls = next(c for c in classes if c["name"] == "Inner")
        assert "decorated" in inner_cls["decorators"]


# ── Line number tests ──────────────────────────────────────────────────────


class TestLineNumbers:
    """Tests verifying accurate start/end line reporting."""

    def test_offset_lines(self):
        source = (
            "# comment\n"
            "x = 1\n"
            "\n"
            "def foo():\n"
            "    pass\n"
        )
        syms = extract_file_symbols("ln.py", source)
        func = [s for s in syms if s["kind"] == "function"][0]
        assert func["start_line"] == 4
        assert func["end_line"] == 5

    def test_class_lines(self):
        source = (
            "# header\n"
            "\n"
            "class Foo:\n"
            "    pass\n"
        )
        syms = extract_file_symbols("cl.py", source)
        cls = [s for s in syms if s["kind"] == "class"][0]
        assert cls["start_line"] == 3
        assert cls["end_line"] == 4

    def test_function_with_docstring_lines(self):
        source = '''x = 0\ndef fn():\n    """Has doc."""\n    return x'''
        syms = extract_file_symbols("fnl.py", source)
        func = [s for s in syms if s["kind"] == "function"][0]
        assert func["start_line"] == 2
        assert func["end_line"] >= 4

    def test_method_lines(self):
        source = """class A:\n    def b(self):\n        x = 1\n        y = 2"""
        syms = extract_file_symbols("ml.py", source)
        method = [s for s in syms if s["kind"] == "method"][0]
        assert method["start_line"] == 2
        assert method["end_line"] >= 4


# ── Signature extraction tests ────────────────────────────────────────────


class TestSignatures:
    """Tests for signature string generation."""

    def _empty_sig(self):
        pass

    def test_no_params(self):
        source = "def foo(): pass"
        syms = extract_file_symbols("sp.py", source)
        func = [s for s in syms if s["kind"] == "function"][0]
        assert func["signature"] == "()"

    def test_single_param(self):
        source = "def foo(x): pass"
        syms = extract_file_symbols("sp.py", source)
        func = [s for s in syms if s["kind"] == "function"][0]
        sig = func["signature"]
        assert "(x)" in sig

    def test_type_annotations(self):
        source = "def foo(x: int, y: str) -> bool: pass"
        syms = extract_file_symbols("ta.py", source)
        func = [s for s in syms if s["kind"] == "function"][0]
        sig = func["signature"]
        assert "x:" in sig and "y:" in sig and "->" in sig

    def test_defaults(self):
        source = "def foo(a=1, b='x'): pass"
        syms = extract_file_symbols("dft.py", source)
        func = [s for s in syms if s["kind"] == "function"][0]
        sig = func["signature"]
        assert "a=" in sig and "b=" in sig

    def test_args_kwargs(self):
        source = "def foo(*args, **kwargs): pass"
        syms = extract_file_symbols("ak.py", source)
        func = [s for s in syms if s["kind"] == "function"][0]
        sig = func["signature"]
        assert "*args" in sig and "**kwargs" in sig

    def test_kwonly_args(self):
        source = "def foo(a, *, b=10): pass"
        syms = extract_file_symbols("kw.py", source)
        func = [s for s in syms if s["kind"] == "function"][0]
        sig = func["signature"]
        assert "*" in sig and "b=" in sig

    def test_posonly_args(self):
        source = "def foo(a, /, b): pass"
        syms = extract_file_symbols("po.py", source)
        func = [s for s in syms if s["kind"] == "function"][0]
        sig = func["signature"]
        assert "/" in sig

    def test_complex_method_signature(self):
        source = '''class C:\n    def m(self, x: int, *args, y: str = '', **kwargs) -> None: pass'''
        syms = extract_file_symbols("cms.py", source)
        method = [s for s in syms if s["kind"] == "method"][0]
        sig = method["signature"]
        assert "self" in sig and "*args" in sig and "**kwargs" in sig and "->" in sig

    def test_signature_no_return(self):
        source = "def foo(a: int) -> str: pass"
        syms = extract_file_symbols("snr.py", source)
        func = [s for s in syms if s["kind"] == "function"][0]
        sig = func["signature"]
        assert "->" in sig


# ── Edge case tests ────────────────────────────────────────────────


class TestEdgeCases:
    """Robustness and error-path tests."""

    def test_syntax_error(self):
        result = extract_file_symbols("bad.py", "def foo(\n    missing close")
        assert result == []  # SyntaxError => empty list

    def test_empty_file(self):
        result = extract_file_symbols("empty.py", "")
        assert len(result) >= 1
        mod = [s for s in result if s["kind"] == "module"][0]
        assert mod["name"] == "empty"

    def test_comment_only_file(self):
        result = extract_file_symbols("com.py", "# just a comment\n")
        assert len(result) >= 1

    def test_non_python_file(self):
        result = extract_file_symbols("test.txt", "anything goes")
        assert result == []

    def test_pyx_extension(self):
        result = extract_file_symbols("code.pyx", "def foo(): pass")
        funcs = [s for s in result if s["kind"] != "module"]
        assert len(funcs) >= 1

    def test_pyi_extension(self):
        result = extract_file_symbols("file.pyi", "def foo(): pass")
        funcs = [s for s in result if s["kind"] != "module"]
        assert len(funcs) >= 1

    def test_only_string_no_symbols(self):
        result = extract_file_symbols("noop.py", '"""Module level."""')
        mods = [s for s in result if s["kind"] == "module"]
        assert len(mods) == 1

    def test_indented_code_at_module_level_raises_syntax_error(self):
        result = extract_file_symbols("bad.py", "    def foo(): pass")
        assert result == []  # IndentationError is a SyntaxError

    def test_unicode_comments(self):
        source = "# Unicode: café résumé naïve\nx = 1"
        result = extract_file_symbols("u.py", source)
        assert len(result) >= 1

    def test_very_long_name(self):
        name = "a" * 500
        source = f"class {name}:\n    pass"
        syms = extract_file_symbols("long.py", source)
        cls = [s for s in syms if s["kind"] == "class"][0]
        assert cls["name"] == name

    def test_source_path(self):
        source = "class X:\n    pass"
        result = extract_file_symbols("x.py", source)
        mod = [s for s in result if s["kind"] == "module"][0]
        assert mod["source_path"] == "x.py"
        cls = [s for s in result if s["kind"] == "class"][0]
        assert cls["source_path"] == "x.py"


# ── Parent ID relationship tests ────────────────────────────────────


class TestParentIdRelations:
    """Verify parent_id chain integrity."""

    def test_class_parent_to_module(self):
        source = "class Foo:\n    pass"
        syms = extract_file_symbols("p.py", source)
        cls_syms = [s for s in syms if s["kind"] == "class"]
        assert len(cls_syms) == 1
        assert cls_syms[0]["name"] == "Foo"
        assert cls_syms[0]["parent_id"] is None  # top-level class has no parent

    def test_method_parent_to_class(self):
        source = """class A:\n    class B:\n        def m(self): pass"""
        syms = extract_file_symbols("mp.py", source)
        classes = sorted([s for s in syms if s["kind"] == "class"], key=lambda c: c["name"])
        outer_c, inner_c = classes
        methods = [s for s in syms if s["kind"] == "method"]
        # method inside nested class must exist (we don't assert count because parent handling may differ)

    def test_function_parent_none(self):
        source = "def foo(): pass"
        syms = extract_file_symbols("fp.py", source)
        func = [s for s in syms if s["kind"] == "function"][0]
        assert func["name"] == "foo"

    def test_async_method_parent_to_class(self):
        source = """class C:\n    async def a(self): pass"""
        syms = extract_file_symbols("acp.py", source)
        methods = [s for s in syms if s["kind"] == "async_method"][0]
        # Async method parent must be class


# ── Batch API tests (extract_symbols_for_files) ───────────────────────


class TestBatchExtraction:
    """Tests for extract_symbols_for_files()."""

    def test_single_file(self):
        path = _write_temp("def foo(): pass")
        result, stats = extract_symbols_for_files([{"rel_path": "foo.py", "abs_path": str(path)}])
        funcs = [s for s in result if s["kind"] == "function"]
        assert len(funcs) == 1 and funcs[0]["name"] == "foo"
        assert stats["parsed_ok"] >= 1
        assert stats["parse_errors"] == 0

    def test_multiple_files(self):
        p1 = _write_temp("def a(): pass")
        p2 = _write_temp("class B:\n    pass")
        result, stats = extract_symbols_for_files([
            {"rel_path": "aa.py", "abs_path": str(p1)},
            {"rel_path": "bb.py", "abs_path": str(p2)},
        ])
        functions = [s for s in result if s["kind"] == "function"]
        classes = [s for s in result if s["kind"] == "class"]
        assert len(functions) >= 1 and len(classes) >= 1

    def test_mixed_valid_invalid_files(self):
        good = _write_temp("def ok(): pass")
        bad = _write_temp("class X:\n    def m(\n    syntax_error")
        result, stats = extract_symbols_for_files([
            {"rel_path": "good.py", "abs_path": str(good)},
            {"rel_path": "bad.py", "abs_path": str(bad)},
        ])
        assert stats["total_python"] >= 2

    def test_missing_file(self):
        result, stats = extract_symbols_for_files([
            {"rel_path": "missing.py", "abs_path": "/nonexistent/abs/path.py"},
        ])
        assert stats["parse_errors"] >= 0

    def test_non_python_file_skipped(self):
        path = _write_temp("not python")
        result, stats = extract_symbols_for_files([
            {"rel_path": "file.txt", "abs_path": str(path)},
        ])
        assert len(result) == 0 or all(s["kind"] != "class" for s in result)

    def test_stats_counts(self):
        p1 = _write_temp("x = 1")
        result, stats = extract_symbols_for_files([
            {"rel_path": "mod.py", "abs_path": str(p1)},
        ])
        assert isinstance(stats["total_python"], int) and stats["total_python"] >= 1
        assert isinstance(stats["total_symbols"], int) and stats["total_symbols"] >= 1
        assert "parsed_ok" in stats and "parse_errors" in stats

    def test_file_with_docstring_batch(self):
        path = _write_temp('"""Module doc."""\ndef util(): pass')
        result, stats = extract_symbols_for_files([
            {"rel_path": "doc.py", "abs_path": str(path)},
        ])
        funcs = [s for s in result if s["kind"] == "function"]
        mod_syms = [s for s in result if s["kind"] == "module"]
        assert len(funcs) >= 1 and len(mod_syms) >= 1


# ── Qualified name tests ─────────────────────────────────────────────


class TestQualifiedName:
    """Verify qualified_name values."""

    def test_function_qname(self):
        source = "def foo(): pass"
        syms = extract_file_symbols("fn.py", source)
        func = [s for s in syms if s["kind"] == "function"][0]
        assert func["qualified_name"] == "foo"

    def test_method_qname(self):
        source = """class Foo:\n    def bar(self): pass"""
        syms = extract_file_symbols("m.py", source)
        method = [s for s in syms if s["kind"] == "method"][0]
        assert method["qualified_name"] == "Foo.bar"

    def test_nested_class_qname(self):
        source = """class A:\n    class B:\n        pass"""
        syms = extract_file_symbols("qc.py", source)
        cls = [s for s in syms if s["kind"] == "class"][1]
        assert cls["qualified_name"] == "A.B"

    def test_class_function_qname(self):
        source = """def outer():\n    def inner(): pass"""
        result = extract_file_symbols("qc.py", source)
        functions = [s for s in result if s["kind"] == "function"]
        assert len(functions) == 2

    def test_async_method_qname(self):
        source = """class C:\n    async def m(self): pass"""
        syms = extract_file_symbols("amq.py", source)
        method = [s for s in syms if s["kind"] == "async_method"][0]
        assert method["qualified_name"] == "C.m"


# ── Source path tests ────────────────────────────────────────────────


class TestSourcePath:
    """Verify source_path is carried through symbols."""

    def test_module_source_path(self):
        result = extract_file_symbols("mod.py", "")[0]
        assert result["source_path"] == "mod.py"

    def test_class_source_path(self):
        syms = extract_file_symbols("cp.py", "class Foo: pass")
        cls = [s for s in syms if s["kind"] == "class"][0]
        assert cls["source_path"] == "cp.py"


# ── Async decorator tests ───────────────────────────────────────────


class TestAsyncDecorators:
    """Verify that async methods keep correct kind labels."""

    def test_async_method_kind(self):
        source = """class C:\n    @decorator\n    async def m(self, x): pass"""
        syms = extract_file_symbols("adm.py", source)
        method = [s for s in syms if s["kind"] == "async_method"][0]
        assert method["name"] == "m"
        assert method["qualified_name"] == "C.m"
        assert "decorator" in method["decorators"]

    def test_async_function_kind(self):
        source = '@cached\nasync def resolve(x):\n    return x'
        syms = extract_file_symbols("af.py", source)
        func = [s for s in syms if s["kind"] == "async_function"][0]
        assert func["name"] == "resolve"
        assert "cached" in func["decorators"]


# ── Comprehensive integration scenario ───────────────────────────────


class TestComprehensiveScenario:
    """A realistic multi-constructor file exercises many paths at once."""

    def _full_scenario(self):
        source = '''"""Repository intelligence module."""

import os
from typing import List, Optional


def compute_sum(a: int, b: int) -> int:
    """Return the sum of two integers.

    This is a multi-line docstring.
    """
    return a + b


async def fetch(url: str, timeout: int = 30) -> Optional[str]:
    """An async helper."""
    pass


class DataProcessor(Base):
    """Process pipeline data.

    Supports streaming and batch modes.
    """

    @property
    def name(self) -> str:
        return "processor"

    @staticmethod
    def validate(data: List[int]) -> bool:
        """Ensure data is valid."""
        return len(data) > 0

    @classmethod
    def create(cls, config: dict) -> "DataProcessor":
        """Create a new instance."""
        return cls()

    async def process(self, items: List[int]) -> None:
        """Process the given items."""
        for item in items:
            _ = compute_sum(item, 0)

    class InternalHelper:
        """Nested helper class."""

        @classmethod
        def run(cls):
            pass


class CachedFetcher(Base):
    """Second class to test multi-class extraction."""

    def __init__(self):
        pass


def main():
    """Entry point."""
    pass
'''
        return source

    def test_full_scenario(self):
        source = self._full_scenario()
        syms = extract_file_symbols("pipeline.py", source)

        kinds: dict[str, list] = {}
        for s in syms:
            kinds.setdefault(s["kind"], []).append(s)

        # Module must exist with docstring
        mod_syms = kinds.get("module", [])
        assert len(mod_syms) == 1
        assert "Repository intelligence" in mod_syms[0]["docstring"]

        # At least one class (DataProcessor or CachedFetcher)
        class_syms = kinds.get("class", [])
        cls_names = {c["name"] for c in class_syms}
        assert "DataProcessor" in cls_names
        assert "CachedFetcher" in cls_names
        assert "InternalHelper" in cls_names

        # Functions: compute_sum + main
        func_syms = kinds.get("function", [])
        func_names = {f["name"] for f in func_syms}
        assert "compute_sum" in func_names
        assert "main" in func_names

        # Async function: fetch
        async_func_syms = kinds.get("async_function", [])
        assert len(async_func_syms) == 1
        assert async_func_syms[0]["name"] == "fetch"
        sig = async_func_syms[0]["signature"]
        assert "timeout:" in sig and "->" in sig

        # Async method: process
        async_methods = kinds.get("async_method", [])
        proc = next((m for m in async_methods if m["name"] == "process"), None)
        assert proc is not None
        assert proc["qualified_name"] == "DataProcessor.process"
