"""CLI interface for repo-intelligence commands."""

import json
import os
import sqlite3

import click

from repo_intelligence.python_ast import (
    extract_file_symbols,
    _is_python_file,
)
from repo_intelligence.search import (
    create_database,
    db_path as _db_path,
    get_file_list,
    get_stats,
    get_symbols,
    insert_file_metadata,
    insert_repo_info,
    insert_symbols,
    insert_text_content,
    search_files,
)
from repo_intelligence.scanner import scan_repo


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
                    try:
                        symbols = extract_file_symbols(rel_path, text_content)
                        insert_symbols(conn, file_id, symbols)
                    except Exception:
                        # Malformed Python must not fail the whole build.
                        pass
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


if __name__ == "__main__":
    cli()
