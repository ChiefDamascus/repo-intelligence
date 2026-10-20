"""CLI interface for repo-intelligence commands."""

import json
import os
import sqlite3

import click

from repo_intelligence.python_ast import (
    extract_file_imports,
    extract_file_symbols,
    _is_python_file,
)
from repo_intelligence.search import replace_imports_for_file
from repo_intelligence.search import (
    create_database,
    db_path as _db_path,
    get_file_list,
    get_imports,
    get_stats,
    get_symbols,
    insert_file_metadata,
    insert_repo_info,
    insert_symbols,
    insert_text_content,
    search_files,
)
from repo_intelligence.scanner import scan_repo
from repo_intelligence.db_absolute_resolution import AdapterError
from repo_intelligence.resolve_query import (
    resolve_imports_from_index,
    resolve_to_dict,
)


def _print_json(data):
    click.echo(json.dumps(data, indent=2))


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """Repository Intelligence - scanning, indexing, and searching codebases."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command("build")
@click.argument("path", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option("--index-path", "index_path", default=None, help="Override the index database path.")
@click.option(
    "--ignore-dirs",
    multiple=True,
    help="Additional directory names to exclude beyond defaults.",
)
def cmd_build(path, index_path, ignore_dirs):
    """Scan a repository and create / refresh the search index."""
    path = os.path.abspath(path)

    try:
        result = scan_repo(
            path,
            ignore_dirs=list(ignore_dirs),
        )
    except NotADirectoryError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    if index_path is None:
        db_file = _db_path(path)
    else:
        db_file = os.path.abspath(index_path)

    db_dir = os.path.dirname(db_file)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    create_database(db_file)

    files_list = result["files"]
    repo_info = result["repo_info"]

    conn = sqlite3.connect(db_file)
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        insert_repo_info(conn, **repo_info)

        # Clear all existing symbols so rebuilds don't leave stale entries.
        conn.execute("DELETE FROM symbols")

        for f_entry in files_list:
            rel_path = f_entry["rel_path"]
            lang_val = f_entry["lang"]
            size_val = f_entry["size"]
            sha_hash = f_entry["sha256"]

            insert_file_metadata(conn, rel_path, lang_val, size_val, sha_hash)

            if f_entry["is_binary"]:
                continue

            abs_fp = f_entry["abs_path"]
            try:
                with open(abs_fp, "r", encoding="utf-8", errors="replace") as fh:
                    text_content = fh.read()
                if text_content.strip():
                    insert_text_content(conn, rel_path, text_content)
            except (OSError, IOError):
                pass

            # --- Symbol extraction for Python files --------------------------
            if _is_python_file(rel_path) and not f_entry["is_binary"]:
                file_id_row = conn.execute(
                    "SELECT id FROM files WHERE rel_path = ?", (rel_path,)
                ).fetchone()
                file_id = file_id_row[0] if file_id_row else None

                if file_id is not None:
                    symbols = extract_file_symbols(rel_path, text_content)
                    insert_symbols(conn, file_id, symbols)

                    imports_list = extract_file_imports(rel_path, text_content)
                    replace_imports_for_file(conn, file_id, imports_list)
    finally:
        with conn:
            conn.commit()
        conn.close()

    click.echo(f"Indexed {len(files_list)} files from '{path}' into '{db_file}'.")


@cli.command("stats")
@click.option("--db-path", "db_file", default=None, help="Path to the index database file. Defaults to the external location for CWD.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def cmd_stats(db_file, as_json):
    """Show indexing statistics for the given database."""
    if db_file is None:
        db_file = _db_path(".")

    try:
        stats_data = get_stats(db_file)
    except FileNotFoundError:
        click.echo("Error: No index database found.", err=True)
        raise SystemExit(1)

    if as_json:
        _print_json(stats_data)
    else:
        repo_name = stats_data.get("repo_name", "N/A")
        total_files = stats_data.get("total_files", 0)
        total_bytes = stats_data.get("total_bytes", 0)
        click.echo(f"Repository    : {repo_name}")
        click.echo(f"Total files   : {total_files}")
        click.echo(f"Total bytes   : {total_bytes}")
        lb = stats_data.get("language_breakdown", {})
        if lb:
            click.echo("")
            click.echo("Language breakdown:")
            for lang_name, count in sorted(lb.items(), key=lambda x: x[1], reverse=True):
                click.echo(f"  {lang_name}: {count}")


@cli.command("files")
@click.option("--db-path", "db_file", default=None, help="Path to the index database file.")
@click.option("--lang", "lang_filter", default=None, help="Filter by language name (case-insensitive).")
@click.option("--path", "path_prefix", default=None, help="Filter by repository-relative path prefix (e.g. 'tests/').")
@click.option("--limit", "limit_val", default=100, help="Maximum number of results.")
@click.option("--offset", "offset_val", default=0, help="Row offset for pagination.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def cmd_files(db_file, lang_filter, path_prefix, limit_val, offset_val, as_json):
    """List indexed files with optional language and path filtering."""
    if db_file is None:
        db_file = _db_path(".")

    try:
        flist = get_file_list(
            db_file,
            lang_filter=lang_filter,
            path_prefix=path_prefix,
            limit=limit_val,
            offset=offset_val,
        )
    except FileNotFoundError:
        click.echo("Error: No index database found.", err=True)
        raise SystemExit(1)

    if as_json:
        _print_json(flist)
    else:
        if not flist:
            click.echo("No files matched.")
        else:
            for entry in flist:
                sz = f" ({entry['size']} B)" if entry.get("size") else ""
                rp = entry["rel_path"]
                lang = entry["language"]
                click.echo(f"  {rp} [{lang}{sz}]")
            click.echo(f"\nShowing {len(flist)} of indexed files.")


@cli.command("search-files")
@click.argument("query")
@click.option("--db-path", "db_file", default=None, help="Path to the index database file.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def cmd_search_files(query, db_file, as_json):
    """Search indexed file contents."""
    if db_file is None:
        db_file = _db_path(".")

    try:
        matches = search_files(db_file, query)
    except FileNotFoundError:
        click.echo("Error: No index database found.", err=True)
        raise SystemExit(1)

    if as_json:
        _print_json(matches)
    else:
        if not matches:
            click.echo(f"No results for '{query}'.")
        else:
            click.echo(f"Results for '{query}':")
            for entry in matches:
                rp = entry["rel_path"]
                lang = entry["language"]
                click.echo(f"  {rp} [{lang}]")


def _symbols_to_dict(row: dict) -> dict:
    """Normalise a raw symbol row dict for JSON output."""
    decorators = row.get("decorators", "")
    if isinstance(decorators, str) and decorators:
        try:
            import json as _json
            decorators = _json.loads(decorators)
        except (ValueError, TypeError):
            pass
    return {
        "id": row.get("id"),
        "file_id": row.get("file_id"),
        "kind": row.get("kind"),
        "name": row.get("name"),
        "qualified_name": row.get("qualified_name") or "",
        "parent_symbol_id": row.get("parent_symbol_id"),
        "signature": row.get("signature", "") or "",
        "decorators": decorators,
        "start_line": row.get("start_line"),
        "end_line": row.get("end_line"),
        "docstring": row.get("docstring", "") or "",
    }


@cli.command("symbols")
@click.option("--db-path", "db_file", default=None, help="Path to the index database file. Defaults to the external location for CWD.")
@click.option("--name", "name_filter", default=None, help="Filter by exact symbol name.")
@click.option("--qualified-name", "qname_filter", default=None, help="Filter by exact qualified name.")
@click.option("--kind", "kind_filter", default=None, help="Filter by symbol kind (e.g. class, function, method).")
@click.option("--file", "file_filter", default=None, help="Filter by repository-relative source path.")
@click.option("--limit", "limit_val", default=100, type=int, help="Maximum number of results (default: 100).")
@click.option("--offset", "offset_val", default=0, type=int, help="Row offset for pagination (default: 0).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def cmd_symbols(db_file, name_filter, qname_filter, kind_filter, file_filter, limit_val, offset_val, as_json):
    """List symbols from the index database (read-only query)."""
    if db_file is None:
        db_file = _db_path(".")

    if not os.path.exists(db_file):
        click.echo("Error: No index database found.", err=True)
        raise SystemExit(1)

    # Map CLI filter names → get_symbols() param names
    params = {
        "limit": limit_val,
        "offset": offset_val,
    }
    if name_filter is not None:
        params["name"] = name_filter
    if qname_filter is not None:
        params["qualified_name"] = qname_filter
    if kind_filter is not None:
        params["kind"] = kind_filter
    if file_filter is not None:
        params["source_path"] = file_filter

    # get_symbols now always JOINs files table — result dict includes file_id.
    try:
        syms = get_symbols(db_file, **params)
    except sqlite3.OperationalError as e:
        click.echo(f"Error querying symbols: {e}", err=True)
        raise SystemExit(1)

    if not syms:
        parts = []
        if name_filter:
            parts.append(f"name='{name_filter}'")
        if qname_filter:
            parts.append(f"qualified_name='{qname_filter}'")
        if kind_filter:
            parts.append(f"kind='{kind_filter}'")
        if file_filter:
            parts.append(f"file='{file_filter}'")
        where_str = f" ({', '.join(parts)})" if parts else ""
        click.echo(f"No symbols found{where_str}.")
    elif as_json:
        _print_json(syms)
    else:
        # Text output: kind  qualified_name  [signature]  rel_path:start-end
        for row in syms:
            kind = row.get("kind", "")
            qname = row.get("qualified_name") or ""
            sig = row.get("signature") or ""
            rp = row.get("rel_path", "")
            start = row.get("start_line")
            end = row.get("end_line")

            sig_part = sig if sig else ""
            line_part = ""
            if start is not None:
                line_part = f"{start}"
            if end is not None and end != start:
                line_part += f"-{end}"
            elif end is not None and end == start:
                pass  # single-line symbol

            parts_out = [f"{kind}  {qname}{(' ' + sig_part) if sig_part else ''}"]
            rp_display = "/" + rp.replace(os.sep, "/") if rp else ""
            line_display = f" ({rp_display}:{line_part})" if line_part and rp_display else (f" at {rp_display}" if rp_display else "")
            parts_out.append(line_display)
            click.echo("  " + " ".join(p for p in parts_out if p))
        click.echo(f"\nShowing {len(syms)} of indexed symbols.")


def _import_statement_text(row: dict) -> str:
    """Reconstruct a source-like import expression from a persisted import row.

    This is display formatting only — it never performs module or file resolution.
    """
    kind = row.get("kind", "") or ""
    module = row.get("module", "") or ""
    level = row.get("level", 0) or 0
    names = row.get("import_names") or []

    if kind == "from_import" and (level > 0 or module):
        prefix = "." * level + module
        rendered = ", ".join(
            entry["name"] + (f" as {entry['asname']}" if entry.get("asname") else "")
            for entry in names
        )
        return f"from {prefix} import {rendered}"

    # Plain ``import ...`` statement(s) — one name per persisted alias entry.
    parts = []
    for entry in names:
        name = entry["name"] if entry.get("name") is not None else module
        asname = entry.get("asname")
        parts.append(name + (f" as {asname}" if asname else ""))
    return "import " + ", ".join(parts) if parts else "import " + module


@cli.command("imports")
@click.option("--db-path", "db_file", default=None, help="Path to the index database file. Defaults to the external location for CWD.")
@click.option("--file", "file_filter", default=None, help="Filter by repository-relative source file containing the import.")
@click.option("--module", "module_filter", default=None, help="Filter by the persisted import module (exact match).")
@click.option("--name", "name_filter", default=None, help="Filter by an imported name (exact match, not the alias).")
@click.option("--kind", "kind_filter", default=None, help="Filter by import kind (e.g. import, from_import).")
@click.option("--limit", "limit_val", default=100, type=int, help="Maximum number of results (default: 100).")
@click.option("--offset", "offset_val", default=0, type=int, help="Row offset for pagination (default: 0).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def cmd_imports(db_file, file_filter, module_filter, name_filter, kind_filter, limit_val, offset_val, as_json):
    """List indexed import statements from the index database (read-only query)."""
    if db_file is None:
        db_file = _db_path(".")

    if not os.path.exists(db_file):
        click.echo("Error: No index database found.", err=True)
        raise SystemExit(1)

    params: dict = {
        "limit": limit_val,
        "offset": offset_val,
    }
    if file_filter is not None:
        params["source_path"] = file_filter
    if module_filter is not None:
        params["module"] = module_filter
    if name_filter is not None:
        params["name_filter"] = name_filter
    if kind_filter is not None:
        params["kind"] = kind_filter

    try:
        rows = get_imports(db_file, **params)
    except sqlite3.OperationalError as e:
        click.echo(f"Error querying imports: {e}", err=True)
        raise SystemExit(1)

    if not rows:
        parts = []
        if file_filter:
            parts.append(f"file='{file_filter}'")
        if module_filter:
            parts.append(f"module='{module_filter}'")
        if name_filter:
            parts.append(f"name='{name_filter}'")
        if kind_filter:
            parts.append(f"kind='{kind_filter}'")
        where_str = f" ({', '.join(parts)})" if parts else ""
        click.echo("No imports found" + where_str + ".")
    elif as_json:
        payload = []
        for row in rows:
            payload.append({
                "id": row.get("id"),
                "file_id": row.get("file_id"),
                "source_file": row.get("rel_path"),
                "kind": row.get("kind"),
                "module": row.get("module"),
                "level": row.get("level"),
                "imported_names": [
                    {
                        "position": entry.get("position"),
                        "name": entry.get("name"),
                        "asname": entry.get("asname"),
                    }
                    for entry in row.get("import_names", [])
                ],
                "start_line": row.get("start_line"),
                "start_col": row.get("start_col"),
            })
        _print_json(payload)
    else:
        for row in rows:
            stmt = _import_statement_text(row)
            rp = row.get("rel_path", "")
            line = row.get("start_line")
            col = row.get("start_col")
            rp_display = "/" + rp.replace(os.sep, "/") if rp else ""
            loc = f" ({rp_display}:{line}"
            if col is not None:
                loc += f", col {col}"
            loc += ")" if rp_display else ""
            click.echo(f"  {stmt}{loc}")
        click.echo(f"\nShowing {len(rows)} of indexed import statements.")


@cli.command("resolve")
@click.option("--root", "roots", required=True, multiple=True, help="Package root (repeatable). At least one is required.")
@click.option("--db-path", "db_file", default=None, help="Path to the index database. Defaults to the external location for CWD.")
@click.option("--file", "file_filter", default=None, help="Exact source-path filter (repository-relative).")
@click.option("--kind", "kind_filter", type=click.Choice(["import", "from_import"]), default=None, help="Exact import-kind filter.")
@click.option("--module", "module_filter", default=None, help="Exact module filter.")
@click.option("--limit", "limit_val", default=None, type=int, help="Maximum number of records (0 returns an empty page).")
@click.option("--offset", "offset_val", default=0, type=int, help="Row offset for pagination.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output the P4 wire projection as JSON.")
def cmd_resolve(roots, db_file, file_filter, kind_filter, module_filter, limit_val, offset_val, as_json):
    """Resolve indexed import statements against the explicit package roots."""
    # R8: pass ALL supplied roots (do not silently drop an empty one).
    # R6: typed pre-validation BEFORE any storage lookup. The wrapper's
    # ``_validate_scalar_args`` (argument types/values) and the frozen
    # ``_canonical_roots`` (root canonicalization) are the TWO typed
    # validation phases. They raise TypeError/ValueError with NO knowledge
    # of the DB, so they are query-input problems (INVALID_QUERY / exit 2).
    # Anything that only happens AFTER these succeed and AFTER the DB is
    # opened (frozen tier analyser ValueError/TypeError, schema/orphan
    # checks, engine errors) is an evidence/storage problem (exit 1,
    # sanitized label) — NEVER re-classified from str(exc) or type name.
    if not roots:
        click.echo("Error: error_label=INVALID_QUERY (at least one --root is required)", err=True)
        raise SystemExit(2)
    if db_file is None:
        db_file = _db_path(".")

    # Typed pre-validation phase (no DB).  Import the wrapper's private
    # validation seam so the CLI and the Python API share the exact same
    # rules (no re-implementation, no divergence).
    from repo_intelligence.resolve_query import _validate_scalar_args
    from repo_intelligence.module_inventory import _canonical_roots
    try:
        _validate_scalar_args(
            db_file, roots,
            source_path=file_filter, kind=kind_filter, module=module_filter,
            limit=limit_val, offset=offset_val,
        )
        _canonical_roots(roots)
    except (TypeError, ValueError) as exc:
        click.echo(f"Error: error_label=INVALID_QUERY ({type(exc).__name__})", err=True)
        raise SystemExit(2)

    # After valid args: a missing DB is storage failure (exit 1 /
    # INDEX_NOT_FOUND), NOT INVALID_QUERY.
    if not os.path.exists(db_file):
        click.echo("Error: error_label=INDEX_NOT_FOUND", err=True)
        raise SystemExit(1)

    # All scalar/roots inputs have now been typed-validated above. From
    # here on, any TypeError/ValueError that surfaces from the frozen
    # analyzers is an evidence-invalidation (exit 1 / EVIDENCE_INVALID) —
    # no substring/type-name heuristics.
    try:
        result = resolve_imports_from_index(
            db_file,
            list(roots),
            target_sources=None,
            source_path=file_filter,
            kind=kind_filter,
            module=module_filter,
            limit=limit_val,
            offset=offset_val,
        )
    except (TypeError, ValueError):
        click.echo("Error: error_label=EVIDENCE_INVALID", err=True)
        raise SystemExit(1)
    except AdapterError:
        click.echo("Error: error_label=INDEX_INVALID", err=True)
        raise SystemExit(1)
    except FileNotFoundError:
        click.echo("Error: error_label=INDEX_NOT_FOUND", err=True)
        raise SystemExit(1)
    except sqlite3.Error:
        click.echo("Error: error_label=INDEX_READ_FAILED", err=True)
        raise SystemExit(1)
    except Exception:
        click.echo("Error: error_label=EVIDENCE_INVALID", err=True)
        raise SystemExit(1)

    if as_json:
        _print_json(resolve_to_dict(result))
    else:
        _print_resolve_text(result)


def _print_resolve_text(result) -> None:
    """Human-readable projection of a resolve result.

    Absolute rows: ``<status>  <reason>  <names>  (source:line,col)``
    followed by a ``kind=<kind>    module=<module>`` line.  Relative
    rows: an issue line (when present), else one labeled line per
    preserved context — context index, source root/file, status, reason —
    without inventing an overall resolution status.
    """
    if not result.records:
        click.echo("No imports resolved.")
        return
    for rec in result.records:
        rp = rec.record.source_path
        loc = f"({rp}:{rec.record.start_line}"
        if rec.record.start_col is not None:
            loc += f", col {rec.record.start_col}"
        loc += ")"
        names = ",".join(n + (f" as {a}" if a else "") for n, a in rec.record.names)
        if rec.routing == "relative":
            rr = rec.relative
            if rr.issue:
                click.echo(f"  relative  {loc}  issue={rr.issue}")
            for i, ctx in enumerate(rr.contexts):
                src = ctx.source_candidate
                if src is not None:
                    src_root = src.root.canonical if src.root else "?"
                    src_file = src.file_path
                else:
                    src_root = "?"
                    src_file = "?"
                click.echo(
                    f"  relative[{i}]  root={src_root}  "
                    f"file={src_file}  status={ctx.status}  "
                    f"reason={ctx.reason}  {loc}"
                )
        else:
            ar = rec.absolute
            status = ar.status if ar else "?"
            reason = ar.reason if ar else ""
            # P2: emit kind, module, names, and location deterministically;
            # render None module as "module=(none)" (no invented name).
            kind = rec.record.kind if rec.record.kind is not None else "(none)"
            module = rec.record.module if rec.record.module not in (None, "") else "(none)"
            click.echo(
                f"  absolute  status={status}  reason={reason}  "
                f"kind={kind}  module={module}  {names}  {loc}"
            )
    click.echo(f"\nResolved {result.count} of {result.total_count} import records.")


if __name__ == "__main__":
    cli()
