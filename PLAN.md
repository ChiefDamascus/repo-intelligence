# Repo Intelligence — Project Plan and Phase Status

**Status:** Living project roadmap  
**Last updated:** 2026-10-14  
**Project:** `repo-intelligence`  
**Authoritative implementation repository:** `ChiefDamascus/repo-intelligence`  
**Active planning milestone:** Phase 2C-2B — read-only database-backed absolute import resolution (**architecture approval pending**)  

> **Maintenance requirement:** Update this file whenever a phase or milestone changes status, scope, acceptance decision, merge state, or blocking dependency. Changes to this file should accompany the corresponding implementation or review milestone, not be deferred to a later documentation cleanup. Do not mark a milestone frozen, accepted, or merged without recorded evidence.

## 1. Mission and principles

Build a reusable, local, offline-first, Devin-like repository intelligence system that enables OpenHands, Codex, and local LLM agents to understand repositories without repeatedly rediscovering their structure.

Development principles:

- Start with deterministic, inspectable repository facts, then add static relationships, analysis, documentation, and agent integration.
- Index databases live outside repository working trees. The default path is `<XDG_DATA_HOME or ~/.local/share>/repo-intelligence/<sha256-prefix-of-absolute-repo-path>/index.db`.
- Do not execute or dynamically import indexed code to infer relationships.
- Prefer explicit, conservative outcomes and preserved provenance over guesses or precedence-based winners.
- Respect frozen milestone boundaries. Existing contracts change only through an explicitly scoped, separately approved milestone.
- Use `hermes-satisfactory` only as a **read-only validation repository**: indexing or examining it is allowed, modifying its source or Git history is not.
- Implementation of a pending milestone requires explicit approval. An architecture draft is **not** approval to change code.

## 2. Status vocabulary

| Status | Meaning |
|---|---|
| **FROZEN** | Implemented, accepted, and treated as a stable contract. No modifications without an explicitly approved correction milestone. |
| **MERGED / COMPLETE** | Implementation accepted and merged, with remaining integration/freeze terminology recorded separately if applicable. |
| **COMPLETE (DESIGN)** | Planning/reconnaissance milestone finished; no additional implementation is implied. |
| **PLANNING — APPROVAL PENDING** | Architecture discussion or document only. Coding not authorized. |
| **PENDING — NOT AUTHORIZED** | Planned work that has not been approved for implementation. |
| **PROPOSED** | A roadmap suggestion; number, scope, and sequence may change. |
| **BLOCKED** | Cannot proceed until the recorded prerequisite is satisfied. |

## 3. Master phase tracker

| Phase | Deliverable | Status as of 2026-10-14 | Evidence / next gate |
|---|---|---|---|
| **1** | Repository inventory, scanning, SHA-256, Git metadata, SQLite and FTS5 | **FROZEN** | Established Phase 1 baseline and CLI. |
| **2A** | Python AST symbol extraction and persistence | **FROZEN** | Symbols query CLI and persistence accepted. |
| **2B-1** | Python AST import extraction | **FROZEN** | Immutable `ImportRecord` contract; source-order import extraction. |
| **2B-2** | SQLite persistence of imports and imported names | **FROZEN** | `imports`/`import_names` with FK integrity and replacement semantics. |
| **2B-3** | Integrate import extraction into `repo-index build` | **FROZEN** | Existing build path persists extracted imports. |
| **2B-4** | Imports query CLI, filtering, deterministic pagination | **FROZEN** | `repo-index imports`; earlier full suite reported 431 passing at recovery. |
| **2C-0 / 2C-0A / 2C-0B** | Import-resolution reconnaissance and corrected architecture | **COMPLETE (DESIGN)** | Documented in the prior project roadmap. |
| **2C-1** | Deterministic Python module inventory and package-root mapping | **FROZEN** | Final R3 Codex PASS; frozen `main` base commit `039b399807a9b844e11acae35171464a5e02483a`. |
| **2C-2A** | Pure deterministic absolute import resolver | **MERGED / COMPLETE** | [PR #1](https://github.com/ChiefDamascus/repo-intelligence/pull/1) merged 2026-10-14; source commit `7c0ab4296cf2b97ac45539e90ffdebf85279ee9b`, merge commit `d4fc71d18eff7f2d0e7f41af781df2125d7a4f22`. Final Codex review was reported PASS. |
| **2C-2B** | Read-only database-backed absolute import adapter | **PLANNING — APPROVAL PENDING** | Review and approve `PHASE_2C_2B_ARCHITECTURE_DRAFT.md` before any implementation. |
| **2C-3** | Relative imports and separate from-import name/submodule analysis | **PENDING — NOT AUTHORIZED** | Requires Phase 2C-2 acceptance and an independently approved scope. |
| **2C-4** | Resolution integration, query exposure and optional CLI | **PENDING — NOT AUTHORIZED** | Requires prior resolver milestones and interface approval. |
| **2D** | Direct local module/file dependency relationships | **PROPOSED** | Confirm design after Phase 2C acceptance. |
| **2E** | Reverse dependencies and bounded graph traversal/impact assessment | **PROPOSED** | Depends on trustworthy direct relationships. |
| **3** | Repository knowledge model linking files, modules, symbols and imports | **PROPOSED** | Define stable entity/relationship contracts. |
| **4** | DeepWiki-style navigable documentation and architecture summaries | **PROPOSED** | Derived from structural data; evidence links required. |
| **5** | Agent/tool integration for OpenHands, Codex and local LLMs | **PROPOSED** | Prefer early read-only access to stable queries, possibly MCP. |
| **6** | Multi-repository reuse and incremental index maintenance | **PROPOSED** | Confirm index versioning, rebuild, isolation and invalidation model. |

**Important:** Phase 2C-2A is the pure resolver; Phase 2C-2B is its database adapter. The initial uploaded roadmap grouped absolute resolution under Phase 2C-2 before this split was introduced. Future phases 2D–6 remain suggestions, **not authorized commitments**.

## 4. Completed foundation

### Phase 1 — Repository inventory and full-text search (FROZEN)

- Enumerate files with repository-relative paths, language/size and SHA-256 metadata.
- Capture Git metadata and persist repository/file data in SQLite.
- Index file content using SQLite FTS5 and expose file/stat/search queries.
- Keep index data in a deterministic, external SQLite path rather than inside the repository.

### Phase 2A — Python symbols (FROZEN)

- Extract Python AST symbols, including modules, classes, functions and methods.
- Retain qualified names, declarations/source ranges, signatures, decorators and docstrings as supported by the existing index.
- Persist symbol information and expose the `repo-index symbols` CLI with filters/pagination/JSON.

### Phase 2B — Python imports (FROZEN)

1. **2B-1:** AST extraction into `ImportRecord(kind, module, names, level, source_path, start_line, start_col)`; no runtime import execution.
2. **2B-2:** Persist imports and imported names in related SQLite tables, preserving name order and FK behavior.
3. **2B-3:** Index extracted imports during normal `repo-index build`; preserve errors rather than silently suppressing broad failures.
4. **2B-4:** Query persisted imports through `repo-index imports`, with deterministic ordering, filters, pagination and JSON.

### Phase 2C-0 / 0A / 0B — Design foundation (COMPLETE, DESIGN)

- Reconnaissance, architecture corrections, and design groundwork for conservative structural module-to-file import resolution.

### Phase 2C-1 — Module inventory (FROZEN)

- Map persisted `.py` file records and **explicitly configured** package roots into immutable, deterministic `ModuleCandidate` tuples.
- Distinguish module files, package initializers and package-directory evidence.
- Preserve actual `name_components` as structural identity; a dotted display alias alone is not sufficient.
- Preserve unsupported names and evidence, without silently normalizing them into legal imports.
- Support read-only SQLite file-record loading through `load_file_records_from_db` and `build_inventory_from_db`.
- No `sys.path` discovery, runtime resolution, filesystem probing of indexed files, or candidate precedence assumptions.
- Historic R3 corrections (candidate ordering, validation of excluded paths, deterministic duplicate diagnostics) passed the final review before freeze. The uploaded roadmap was older and still described R3 as pending.

### Phase 2C-2A — Pure absolute import resolution (MERGED / COMPLETE)

- Existing entry point: `resolve_absolute_import(record, inventory) -> Resolution` in `src/repo_intelligence/absolute_resolution.py`.
- Pure and deterministic over `ImportRecord` plus a Phase 2C-1 module-inventory tuple; no I/O, SQL, file reads or dynamic imports.
- Uses exact component-wise matching, conservative support flags, canonical ordering, structural deduplication and explicit failure on conflicting duplicates.
- Returns an immutable `Resolution` with `record`, `requested_module`, `requested_components`, `status`, `supported_candidates`, `unsupported_candidates` and `reason`.
- Statuses: `RESOLVED`, `AMBIGUOUS`, `UNRESOLVED`, `UNSUPPORTED`, `NOT_ATTEMPTED`.
- For `from pkg.mod import item`, the resolver targets **`pkg.mod` only**, not the imported name `item`. Relative imports remain unattempted for 2C-3.
- Merged through [PR #1](https://github.com/ChiefDamascus/repo-intelligence/pull/1). Test counts reported during review are historical agent reports rather than tests rerun while preparing this document.

## 5. Current work — Phase 2C-2B

**Goal:** Make the Phase 2C-2A pure resolver useful against an existing SQLite index, while enforcing database read-only behavior and leaving frozen components unchanged.

**Current authorization:** Architecture and test planning **only**. No code changes.

**Proposed boundaries (not approved):**

- A new, small adapter module operating on an **existing** database path and an explicit nonempty root list.
- Database opened `mode=ro`; a single consistent read snapshot for complete file inventory, import statements and imported-name rows.
- Read-only `SELECT`/introspection against `files`, `imports` and `import_names`; no DDL, schema repair, caching, writes, rebuild, or new tables.
- Reconstruct exact `ImportRecord` instances with names ordered by persisted `position`, preserving source path, import kind, module, level and source coordinates.
- Pass the **complete** Phase 2C-1 inventory and each reconstructed import to the frozen `resolve_absolute_import()`; retain all outcomes in deterministic source order.
- Explicit roots supplied by caller; no automatic root guessing or `sys.path`-based discovery.
- No CLI, no new persistence format, no runtime imports, no relative-resolution expansion, and no modification to Phase 1/2A/2B/2C-1/2C-2A.

**Design issue to resolve:** The existing `search.get_imports()` invokes `_ensure_imports_tables()` with `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` statements. It therefore does **not** meet the strict Phase 2C-2B no-DDL contract. The proposed adapter must use its **own SELECT-only loader** instead of calling that helper; **do not edit** the frozen helper as part of 2C-2B.

**Approval gate:** Approve (1) public adapter API/return type, (2) consistent read snapshot/connection ownership, (3) strict schema and malformed-row behavior, (4) deterministic ordering and full inventory rules, and (5) the no-write acceptance-test matrix. Only then prepare an OpenHands implementation prompt.

See [`PHASE_2C_2B_ARCHITECTURE_DRAFT.md`](PHASE_2C_2B_ARCHITECTURE_DRAFT.md) for the proposal. This document is not an approval.

## 6. Remaining Phase 2C milestones

### Phase 2C-3 — Relative and imported-name resolution (PENDING)

- Interpret package context for `from . import x`, `from ..pkg import y` and related relative imports.
- Model *imported-name* uncertainty separately from resolving the explicitly named parent module; distinguish attributes, submodules and wildcard imports.
- Preserve ambiguity and evidence; do not infer runtime exports or dynamic import paths without explicit scope approval.

### Phase 2C-4 — Query/API exposure (PENDING)

- Approve stable resolution query contracts and optionally a dedicated `repo-index resolve` CLI.
- Make results discoverable to higher-level dependency and agent-query features.
- Define filtering, pagination, JSON conventions and index/rebuild semantics independently of 2C-2B.

## 7. Proposed downstream roadmap (not approved)

### Phase 2D — Direct dependencies

Build explicit, evidenced local file/module relationships from supported resolutions; retain unresolved and ambiguous links as non-definitive.

### Phase 2E — Reverse dependencies and impact traversal

Add bounded inbound/outbound dependency navigation, cycle controls, confidence/uncertainty reporting and change-impact questions.

### Phase 3 — Repository knowledge model

Unify files, modules, symbols, imports and relationships into stable, queryable records with evidence references.

### Phase 4 — DeepWiki

Generate navigable module guides, architecture notes and cross-references grounded in the index, with refresh and provenance rules.

### Phase 5 — OpenHands/Codex/local-model integration

Expose safe read-only repository queries through agent tools or MCP, ideally before all DeepWiki generation is complete; prevent agent tools from silently mutating the source repositories.

### Phase 6 — Multi-repository and incremental maintenance

Support deterministic repository isolation, index freshness checks, incremental updates, version migration strategy and operational performance improvements.

**Suggested delivery sequence:** Finish 2C -> basic dependency queries -> early read-only OpenHands/Codex integration -> broader knowledge model/DeepWiki -> multi-repository and incremental enhancements. Phase numbers/scope can be revised only as a documented decision.

## 8. Current CLI surface (already present)

```text
repo-index build
repo-index stats
repo-index files
repo-index search-files
repo-index symbols
repo-index imports
```

Phase 2C-2B does **not** add a CLI. The current README still contains some Phase 1-era descriptions and may need a separately authorized documentation refresh.

## 9. Status-update protocol (mandatory)

For **every** milestone transition:

1. Update its status and evidence/next-gate cell in the master tracker above.
2. Update the milestone detail section, recording what was accepted, deferred, or explicitly excluded.
3. Add a dated entry to the decision/change log below with commit/PR/review/test evidence and any unresolved validation caveats.
4. Move the **Active planning milestone** at the top if the next objective changes.
5. Distinguish *architecture approved*, *implementation started*, *tests reported*, *Codex reviewed*, *merged*, and *frozen*. These are different events.
6. Do not announce completion based solely on a proposed prompt, green-looking report, or open PR. Verify the actual merge/freeze state where possible.
7. Submit the PLAN.md update in the same documentation/implementation PR or commit when practical. If the code milestone is already merged, make an explicit documentation-only update.
8. Leave frozen components intact unless a separate correction scope is reviewed and approved.

### Suggested change-entry template

```text
YYYY-MM-DD — [phase] STATUS_BEFORE -> STATUS_AFTER
Decision: ...
Evidence: PR URL / commit / review finding / test run
Scope accepted: ...
Deferred or excluded: ...
Next prerequisite: ...
```

## 10. Decision and change log

- **2026-10-14 — 2C-2A MERGED / COMPLETE:** GitHub [PR #1](https://github.com/ChiefDamascus/repo-intelligence/pull/1) is merged (`d4fc71d18eff7f2d0e7f41af781df2125d7a4f22`). Pure resolver added without touching frozen components.
- **2026-10-14 — 2C-2B PLANNING:** Began read-only database-backed adapter architecture; **no implementation authorized**. Strict read-only constraint rules out direct reuse of the `get_imports()` helper because it contains schema-ensuring DDL.
- **2026-10-11 — 2C-1 FROZEN:** Final R3 Codex review reported PASS; frozen base commit later verified as `039b399807a9b844e11acae35171464a5e02483a`.
- **Earlier — Phases 1/2A/2B FROZEN:** Acceptance and historical test milestones documented in the prior project roadmap; individual original merge references not reconstructed here.

## 11. Source and verification limits

This plan draws on the previously uploaded *Repo Intelligence — Complete Project Roadmap*, prior milestone review context and **read-only** GitHub inspection of current `main` and PR #1 on 2026-10-14. The older uploaded roadmap listed Phase 2C-1R3 as pending; the current status above intentionally supersedes that older snapshot. The local OpenHands workspace was **not** mounted into this chat, and no test suite was executed or implementation file altered while writing this plan. Only Markdown artifacts were prepared for addition to the repository.
