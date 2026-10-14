# Phase 2C-2B — Read-only Database-backed Absolute Import Resolution

**Status:** APPROVED — IMPLEMENTATION AUTHORIZED  
**Prepared:** 2026-10-14  
**Implementation authorization:** **APPROVED for Phase 2C-2B within checklist A–E; no implementation completion claimed**  
**Prerequisite:** Phase 2C-2A merged into `main` (PR #1, 2026-10-14)  
**Related roadmap:** [`PLAN.md`](PLAN.md)

## 1. Objective and non-negotiable constraints

Provide a narrow, deterministic, **read-only** adapter from an *existing* repo-intelligence SQLite index to the **frozen** pure resolver `resolve_absolute_import(record, inventory) -> Resolution` introduced in Phase 2C-2A.

- Read the **persisted index only**. Do not scan or reopen indexed repository files.
- Do not execute indexed Python modules, use `importlib`/`sys.path` to pick modules, or speculate about runtime path order.
- Do not create or alter databases, tables, columns, indexes, cached resolutions, migrations, index contents or source files.
- Do not modify frozen Phase 1, 2A, 2B, 2C-1 or 2C-2A code/contracts.
- No CLI addition, JSON output contract, MCP endpoint or agent integration in this milestone.
- No resolution of relative import levels or imported-name binding semantics. They belong to subsequent milestones.
- Retain every supported/unsupported candidate and every current Phase 2C-2A reason code; never select a winner by root order.
- Root configuration is explicit and nonempty. No automatic root guessing or implicit repository-root fallback.

## 2. Verified current code boundaries

**Phase 2C-1:** `repo_intelligence.module_inventory.build_inventory_from_db(conn, roots)` composes `load_file_records_from_db(conn)` and `build_module_inventory(...)`. The existing module-inventory loader supports an open SQLite connection and reads file records in `rel_path` order without creating schema.

**Phase 2C-2A:** `repo_intelligence.absolute_resolution.resolve_absolute_import(record, inventory)` is pure. `Resolution` contains `record`, `requested_module`, `requested_components`, `status`, `supported_candidates`, `unsupported_candidates`, `reason`. Matching is by `name_components`, and only candidates meeting both `component_supported` and `name_supported` count as supported.

**Phase 2B:** `repo_intelligence.python_ast.ImportRecord` is frozen with fields `kind`, `module`, `names`, `level`, `source_path`, `start_line`, `start_col`.

**SQLite schema:** `files(id, rel_path, language, size, sha256, is_binary)`, `imports(id, file_id, kind, module, level, start_line, start_col)`, `import_names(id, import_id, position, name, asname)`.

**Important hazard:** Existing `search.get_imports()` first calls `_ensure_imports_tables()`, which contains DDL (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`). Do not call this from a strict read-only adapter and do not patch frozen `search.py` in 2C-2B. Write a small SELECT-only loader confined to the new adapter.

## 3. Approved minimal API (A)

Add a **new** module (provisional filename `src/repo_intelligence/db_absolute_resolution.py`) exposing one function:

```python
# APPROVED API CONTRACT — NOT IMPLEMENTATION

def resolve_absolute_imports_from_db(
    db_path: str,
    roots: tuple[str, ...] | list[str],
    *,
    source_path: str | None = None,
) -> tuple[Resolution, ...]:
    ...
```

- `db_path`: path of an existing SQLite index; never create it.
- `roots`: explicit package roots using Phase 2C-1's root normalization/validation; nonempty.
- `source_path`: optional **exact** repository-relative path filter for import statements **only**; does not restrict the module inventory.
- Return: immutable tuple of the **existing** `Resolution` objects. They retain original `ImportRecord`s, statuses and all matching candidates. **Do not** create a redundant resolution schema or expose database row IDs as public semantic identity.
- No pagination, streaming, prefiltered inventory, caching or configurable root-precedence in the first iteration. Later query interface/pagination are Phase 2C-4 topics.

**Approved decision (A):** Accept only a database **path** in this milestone and use a single internally owned read-only connection. A connection-taking API may be considered later, but it complicates snapshot ownership and the strictly read-only guarantee for arbitrary caller-owned connections.

## 4. Approved read pipeline and transaction (B)

```text
existing index.db (path)
         |
         v
SQLite connection: file:<percent-encoded-absolute-path>?mode=ro
query_only = ON; begin consistent read transaction
         |
         +--> Phase 2C-1 file-row reader (full index, same connection)
         |             |
         |             v
         |      build_module_inventory(files, explicit_roots)
         |
         +--> adapter-only SELECT imports JOIN files
         |             |
         |             +--> adapter-only SELECT import_names (ordered by position)
         |             v
         |      reconstruct frozen ImportRecord objects
         |
         v
for each ImportRecord: frozen resolve_absolute_import(record, inventory)
         |
         v
immutable tuple[Resolution, ...]
(close owned connection, end read transaction)
```

Requirements:

1. Check that the path exists before connecting, raising `FileNotFoundError` if missing. SQLite URI `mode=ro` must not create a new file.
2. Safely percent-encode the absolute database path (`urllib.parse.quote(path, safe='/')`) and use `sqlite3.connect(..., uri=True)`; test literal `#`, `?`, `%` and encoded-looking filename substrings. Keep behavior consistent with the Phase 2C-1 read-only opener.
3. Enforce `PRAGMA query_only=ON` on the owned connection as defense in depth. A single explicit read transaction gives a consistent snapshot of `files`, `imports` and `import_names` even if another process rebuilds or writes concurrently.
4. Build **one complete** file inventory per invocation; do not apply `source_path`, `limit` or `offset` to the file inventory. Filtering candidates would cause false `UNRESOLVED` outcomes.
5. Read only the requested import statements (`source_path` filters statements, not the inventory); retrieve imported-name rows with a **set-based query or bounded batches** (respecting SQLite bind limits), not an N+1 query loop for large indexes.
6. Convert rows into actual `ImportRecord`s preserving all fields and name-order tuples. Then call the frozen resolver unchanged.
7. Release the connection/transaction on success or exception. Never commit changes, create any schema object, or write a derived result.
8. Do not use SQLite `immutable=1` to bypass journal logic: it can ignore concurrent changes and undermine snapshot assumptions. SQLite may use normal read-side journal/WAL coordination, which is not permission to write application/index data.

## 5. Approved data-mapping contract (C)

Use SELECT-only queries with parameterized filters and an explicit deterministic order. Reference statement projection (or the equivalent permitted by the contract below):

```sql
SELECT i.id, f.rel_path, i.kind, i.module, i.level,
       i.start_line, i.start_col
FROM imports AS i
JOIN files AS f ON f.id = i.file_id
WHERE (? IS NULL OR f.rel_path = ?)
ORDER BY f.rel_path COLLATE BINARY,
         i.start_line, i.start_col, i.kind, i.module, i.level, i.id;
```

The final `i.id` is an internal within-snapshot tie-breaker, **not** a user-facing stable import identity. After reconstructing `ImportRecord` objects (including the complete ordered `names` tuple), **sort the final records by** `(source_path, start_line, start_col, kind, module, level, names)` before resolution so row IDs cannot affect the externally visible ordering, even for pathological same-location records. Exactly identical records may repeat as equal results. A dynamic WHERE clause instead of `? IS NULL` is equally acceptable as long as filter behavior remains exact and parameterized.

For selected import IDs, read their names ordered by `(import_id, position)` and reconstruct:

```text
kind        <- imports.kind
module      <- imports.module (preserve empty string)
level       <- imports.level
source_path <- files.rel_path
start_line  <- imports.start_line
start_col   <- imports.start_col
names       <- tuple((import_names.name, import_names.asname)
                     ordered by import_names.position)
```

There is deliberately **no** `source_path` column in the `imports` table. It comes from `files.rel_path` via the join. Do not reconstruct statement text or infer names from module strings; imported name aliases remain intact. Source-order of *multi-name* `from ... import ...` is retained via `position`.

**Approved schema/corruption policy:** Validate required tables and columns with read-only `sqlite_master` and/or `PRAGMA table_info`; use read-only integrity checks or explicit orphan-detection SELECTs rather than relying on an INNER JOIN that would silently drop orphaned import records. Fail closed with a clear exception if an index is missing tables/required columns, contains orphaned rows, duplicate/invalid name positions, or cannot be losslessly reconstructed. Do not run migration/repair; do not silently drop records or transform malformed rows into fictitious valid imports. Distinguish storage errors from the pure resolver's own defined `UNSUPPORTED`/`NOT_ATTEMPTED` outcomes for synthetically malformed-but-representable `ImportRecord` values.

**Approved fail-closed coordinate policy:** Persisted `start_line`/`start_col` are nullable in SQLite even though `ImportRecord` types are `int`. Reject absent/invalid required coordinates; review legacy rows during implementation rather than weakening or silently repairing this contract.

## 6. Semantic pass-through (no new resolver policy)

| Persisted statement | Target passed to Phase 2C-2A | Adapter behavior |
|---|---|---|
| `import pkg.mod` | `pkg.mod` | Exact structured `name_components` match. |
| `import pkg.mod as pm` | `pkg.mod` | Alias remains in `record.names`. |
| `from pkg.mod import item` | `pkg.mod` | `item` is **not** resolved as a submodule/attribute here. |
| `from pkg import *` | `pkg` | Wildcard preserved; only explicit parent module target is resolved. |
| `from . import item` | Original `ImportRecord` | Pure resolver emits `NOT_ATTEMPTED` (Phase 2C-3). |
| `import stdlib_or_external` | Literal target | `UNRESOLVED` if no local structural match; no assertion that import is invalid at runtime. |
| Two supported files across roots | Literal target | `AMBIGUOUS`, all candidates retained; no `sys.path` precedence. |
| Unsupported spelling in indexed candidate | Literal target | Preserve unsupported evidence/flags and Phase 2C-2A status. |

The adapter does **not** retry submodule candidates if the explicit target has no match, resolve relative levels, parse source, infer namespace precedence, or modify reason/status fields.

## 7. Required acceptance tests (D)

**Read-only invariant:**

- Existing `index.db` can be queried; no application data, schema, `schema_version`, or index content changes. No SQL that writes or creates objects is executed.
- Missing database path produces `FileNotFoundError` and **does not create a file**.
- Missing `imports`/`import_names`/`files` tables causes a clear error and no schema repair.
- Spy/mock proves adapter never calls `search.get_imports()`/`_ensure_imports_tables()` or any build/write method.
- Query through a database path containing `#`, `?`, `%` and literal `%3F` round-trips correctly.

**Snapshot/integrity:**

- File inventory and imports/names are read from one owned read-only connection and consistent snapshot; concurrent index changes cannot mix generations.
- The complete file inventory is used even when filtering to one importing file.
- Orphaned import/file and import-name records, duplicate `position`, invalid field types and missing coordinates are either rejected deterministically or handled by an explicitly approved alternative; never silently fabricated/dropped.

**Resolution correctness:**

- `import x`, `import a.b`, `from a.b import item`, aliases, multiple imported names, and wildcard parent module cases.
- Relative imports preserved as `NOT_ATTEMPTED` without throwing away their names/source location.
- `RESOLVED`, `AMBIGUOUS`, `UNRESOLVED`, `UNSUPPORTED`, `NOT_ATTEMPTED` pass through exactly, including reason/evidence and supported/unsupported partitions.
- Duplicate supported candidates and cross-root collisions stay ambiguous; literal-dot path components never become false matches.
- Caller-supplied root sequence variations and SQLite insertion/row-ID orders do not alter semantic results or candidate ordering.
- Exact `source_path` filtering returns only that source's imports while resolving against the full repository inventory.
- Missing/invalid roots reject via frozen Phase 2C-1 validation; no implicit defaults.

**Regression:**

- Run existing Phase 2B, 2C-1, 2C-2A tests and the entire suite; review independently with Codex after OpenHands implementation. Report exact commands and counts. No test execution is claimed in this architecture document.
- Exercise the adapter against a clean index of the repository and, if desired, an index of `hermes-satisfactory` **without changing that repository**.

## 8. Authorized implementation footprint (E)

**Expected additions:**

- `src/repo_intelligence/db_absolute_resolution.py` — tiny SELECT-only adapter + result assembly.
- `tests/test_db_absolute_resolution.py` — focused SQLite fixtures, failure-mode tests and frozen-contract regression checks.

**Not allowed for 2C-2B:** Any edits to `search.py`, `python_ast.py`, `module_inventory.py`, `absolute_resolution.py`, `cli.py`, schema builders, repo scanning, or indexed repositories. Documentation files may be updated to reflect reviewed status transitions.

If these constraints cannot be satisfied by additions alone, return for architecture approval **before** expanding scope.

## 9. Architecture approval checklist

All of the following decisions were approved on 2026-10-14:

- [x] **A — API:** one new path-based function, explicit roots, optional exact source filter, returns tuple of frozen `Resolution` objects (no new public row-ID identity).
- [x] **B — Read model:** owned URI `mode=ro` connection, `query_only`, one read snapshot; reuse frozen Phase 2C-1 builder on the same connection.
- [x] **C — SQLite mapping:** SELECT-only import and imported-name loader; strict schema and malformed-row policy; no `get_imports()` DDL helper.
- [x] **D — Acceptance:** determinism, no writes, snapshot consistency, missing/corrupt database, candidate evidence, and full regression tests.
- [x] **E — Scope:** new adapter/tests only; no frozen code/DB schema/CLI updates and no relative/imported-name resolution.

**Decision: APPROVED.**
