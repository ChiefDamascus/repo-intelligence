"""CLI interface for repo-intelligence commands."""

import json
import os
import sqlite3

import click

from repo_intelligence.search import (
    create_database,
    db_path as _db_path,
    get_file_list,
    get_stats,
    insert_file_metadata,
    insert_repo_info,
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


if __name__ == "__main__":
    cli()
