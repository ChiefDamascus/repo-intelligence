# Phase 2C-4 — Resolution Integration, Query/API Exposure and Optional CLI

**Status: PLANNING — APPROVAL PENDING. Implementation is NOT authorized.**
**Drafted:** 2026-10-20 (UTC). **Revision:** Codex revision 2, addressing independent-review findings R1–R6; human review pending.
**Implementation baseline:** `main` / `origin/main` at `4232f1486b220f15dcec41f0fa7294a940b68e7e`. The original drafting preflight observed a clean tree; the draft and its PLAN entries are uncommitted planning work.

This is a proposed contract, not an approval record. Revising it does not authorize guard setup, production changes, staging, commits, push or merge. All approval boxes remain unchecked. The completed resolver/analyzer contracts remain protected; this document asserts no new freeze.

## 1. Purpose, scope and exclusions

Expose the existing static import-resolution stack through one read-only query API, an explicit JSON projection and, if approved, an additive `repo-index resolve` command. Preserve original import records, candidate partitions, source-context multiplicity and evidence provenance.

**Proposed scope:** one new query/serialization module and its tests; an optional thin CLI binding; separately approved workflow preparation described in §11.

**Excluded:** changes to frozen inventory/resolver/analyzer semantics; SQLite schema or persistence changes; target-file reads or execution; automatic source acquisition; semantic/vector search, LLM analysis, MCP integration, repository-document generation, dependency graphs or impact traversal; new packages; unrelated refactoring.

The new query performs database reads, so it is a read-only adapter, not a mathematically pure function. Frozen computational functions remain pure. Serialization performs no I/O.

## 2. Baseline evidence and dependencies

Paths below are relative to the repository root and describe the pinned baseline, not new functionality.

| Dependency | Existing contract / evidence |
|---|---|
| Phase 2C-1 | `module_inventory.py`: `PackageRoot`, `ModuleCandidate`, frozen root validation, `build_module_inventory`, `build_inventory_from_db` |
| Phase 2C-2A | `absolute_resolution.py`: `resolve_absolute_import`, frozen `Resolution` and supported/unsupported partitions |
| Phase 2C-2B | `db_absolute_resolution.py`: existing-file read-only URI, query-only handle, one explicit read snapshot, schema/orphan checks and row-ID-independent record ordering |
| Phase 2C-3A | `relative_resolution.py`: `RelativeResolution` with ordered per-source contexts and an optional pre-context issue; **no aggregate status** |
| Phase 2C-3B-1 | `imported_name_analysis.py`: `ImportNameAnalysis`, `TargetSource` and caller-supplied source mapping; passing `None` directly to the analyzer is invalid |
| Phase 2C-3B-2 | `wildcard_import_analysis.py`: `WildcardAnalysis`, preserved `__all__` occurrences and source assessments; mapping argument is required |
| Imports / CLI | Frozen `ImportRecord`; six existing CLI commands remain unchanged. Existing JSON printing uses `json.dumps(data, indent=2)` and a final newline |
| Workflow | [AGENTS.md](../AGENTS.md), [standing workflow](phase-workflow.md), [hook guide](openhands-stop-hooks.md); two historical guards are installed. No 2C-4 guard is installed |

Relevant approved evidence: [2C-3B-1 contract](phase-2c-3b-1-approved-contract.md), [2C-3B-2 contract](phase-2c-3b-2-architecture-approved.md), and [PLAN.md](../PLAN.md). Read-only helpers that accept an existing SQLite connection can participate in the same transaction; calling a path-taking helper separately does not share a snapshot.

## 3. Proposed deliverable and design decisions

The recommended contract is API plus optional CLI, with named/wildcard evidence attached to applicable **absolute** records. Relative rows expose the complete frozen relative result; relative named/wildcard attachment is deferred.

| Decision | Revision-2 proposal; still subject to human approval |
|---|---|
| Q1 — CLI | Include the additive CLI if explicitly approved; API-only is a valid narrower approval |
| Q2 — summary status | **No new top-level aggregate status or reason.** Preserve existing tier and context codes |
| Q3 — source evidence | Explicit optional mapping; absence normalizes to an empty mapping at the new wrapper boundary, never `None` at an analyzer boundary |
| Q4 — relative analysis attachment | Defer. Including it requires a revised contract and tests before implementation |
| Q5 — conflicting evidence | Propagate the frozen exception; abort the query with no partial return |

These are proposed resolutions of the original open questions, not answers attributed to the human. Changes requested during review must be recorded before implementation.

## 4. Proposed public Python API

All names and shapes in this section are new proposals. They are not baseline exports.

### P1. Immutable result models

```python
@dataclass(frozen=True)
class ResolveFilters:
    source_path: str | None
    kind: str | None
    module: str | None

@dataclass(frozen=True)
class ResolvedImportRecord:
    record: ImportRecord
    routing: str                         # "absolute" or "relative"
    absolute: Resolution | None
    relative: RelativeResolution | None
    named: ImportNameAnalysis | None
    wildcard: WildcardAnalysis | None

@dataclass(frozen=True)
class ResolveQueryResult:
    schema_version: str                  # "repo-intelligence.resolve.v1"
    db_path: str                         # exact caller-supplied path string
    roots: tuple[PackageRoot, ...]       # frozen canonical root records
    filters: ResolveFilters
    limit: int | None
    offset: int
    total_count: int                     # matching records before pagination
    count: int                           # len(records), after pagination
    records: tuple[ResolvedImportRecord, ...]
```

All collections reachable from the Python result remain immutable. Frozen tier objects are attached without rewriting their record, evidence, ordering or partitions. `tier.record is wrapper.record` for every attached tier. Metadata belongs to the query envelope, including when `records == ()`.

`routing` describes the **new wrapper's dispatch**, not a replacement for an analyzer's `target_kind`. Nested analyzer values such as `mixed_routing` or `indeterminate`, where applicable, are preserved.

### P2. Preserve status and reason semantics

There is no `ResolvedImportRecord.status`, `ResolvedImportRecord.reason` or query-wide resolution status.

- Absolute rows expose the frozen `Resolution.status` and `reason`. Exactly one supported candidate remains `RESOLVED` even when unsupported candidates coexist; multiple distinct supported candidates remain `AMBIGUOUS`.
- Relative rows expose `RelativeResolution.issue` and each ordered context's status/reason. Multiple source-package interpretations remain separate, even if their targets differ or some contexts fail.
- `REL_*` constants are relative reasons/issues, not a new status enum.
- Named/wildcard outcomes and reasons remain separate from module-target resolution. Source absence, `ALL_UNKNOWN` and static name evidence do not replace the module-target status.
- Returning a query envelope means the query completed, not that every import resolved.

Consumers needing a summary must inspect the preserved results. A new aggregate taxonomy would require a separately reviewed scope change.

### P3. Query entry point and source evidence

```python
def resolve_imports_from_index(
    db_path: str,
    roots: tuple[str, ...] | list[str],
    *,
    target_sources: Mapping[ModuleCandidate, TargetSource] | None = None,
    source_path: str | None = None,
    kind: str | None = None,
    module: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> ResolveQueryResult:
    ...
```

**Database and filters.** `db_path` is a nonempty string identifying an existing database. Roots use the frozen explicit, nonempty tuple/list validation and canonicalization; no guessing or implicit root fallback. `source_path` and `module` are optional exact string matches without case folding or path rewriting; an empty string remains an exact empty-string filter. `kind` is `None`, `"import"` or `"from_import"`. Wrong Python argument types raise `TypeError`; unsupported `kind` values raise `ValueError`.

`limit` is `None` or a nonnegative integer and `offset` is a nonnegative integer; booleans are not accepted as integers. Wrong types raise `TypeError`, negative values raise `ValueError`. `limit=0` returns an empty page with the correct `total_count`.

**Source mapping.** At the new wrapper boundary only, `None` means no caller-supplied sources and normalizes to `{}`. No analyzer receives `None`. A provided mapping is snapshotted once and never mutated. Keys must be `ModuleCandidate` values equal to candidates in the complete inventory built for this call; matching includes the frozen substantive fields, not just dotted name or file path. Callers can construct the mapping from an inventory for the same database and roots; the query performs no automatic reconciliation with stale candidates.

Values must be frozen `TargetSource` objects obeying the existing state table:

| Value / absence | Required fields and meaning |
|---|---|
| Missing candidate key | Frozen analyzer synthesizes `SRC_NOT_FOUND` |
| `SRC_SUPPLIED` | `source_text` is a string; `parse_error is None` |
| `SRC_UNAVAILABLE` | Both text and parse error are `None` |
| `SRC_UNPARSEABLE` | Text and parse error are strings, including permitted empty strings |
| Mapped `SRC_NOT_FOUND` or invalid combination | Invalid input; never silently repaired |

The wrapper validates every supplied entry against the full inventory before dispatch, even on an empty page. Non-mapping input, wrong key/value types or state-table violations raise `TypeError`. A well-typed key absent from the call's inventory raises `ValueError`. This is an explicit new-wrapper input policy; it does not change the different exception details of either frozen analyzer called directly. Once validated, the same mapping is passed unchanged to applicable analyzer calls.

**Ordering and determinism.** Match statements, reconstruct their names, validate them, and sort with the exact Phase 2C-2B total-order key `(source_path, start_line, start_col, kind, module, level, names)`, including its tagged ordering for optional aliases. SQLite row IDs are not output keys; equal records may repeat. Apply offset/limit only after this ordering. The complete inventory is never filtered or paginated.

Equivalent results and canonical JSON require the same DB read snapshot, exact `db_path` metadata string, frozen normalized root records (including their representative `original` spelling), filters, pagination and snapshotted source-mapping values. Mapping insertion order does not matter. Changing supplied source text can legitimately change evidence with the same database. No source-text hash, raw mapping or timestamp is added to the public output.

### P4. Serializer and complete wire projection

```python
def resolve_to_dict(result: ResolveQueryResult) -> dict[str, object]:
    ...
```

The serializer receives all envelope metadata explicitly; it reads no database, filesystem, globals or previous query state. It returns a fresh JSON-compatible projection and never modifies the immutable Python result.

Every key below is required, even for `null` values. Tuples become arrays, preserving order and duplicates. Strings, integers, booleans and `None` retain their JSON equivalents. Nested objects use the mappings below recursively. There is no `str(obj)`, `repr(obj)` or unrestricted `asdict` fallback.

| JSON object / source type | Exact fields |
|---|---|
| Query envelope / `ResolveQueryResult` | `schema_version, db_path, roots, filters, limit, offset, total_count, count, records` |
| Filters / `ResolveFilters` | `source_path, kind, module` |
| Per-import wrapper / `ResolvedImportRecord` | `record, routing, absolute, relative, named, wildcard` |
| Import record / `ImportRecord` | `kind, module, names, level, source_path, start_line, start_col`; each `names` pair becomes `{"name": ..., "asname": ...}` |
| Root / `PackageRoot` | `canonical, original`; original spelling is diagnostic, not structural identity |
| Candidate / `ModuleCandidate` | `file_path, root, qualified_name, name_components, file_kind, directory_kind, component_supported, name_supported, evidence` |
| Absolute / `Resolution` | `record, requested_module, requested_components, status, supported_candidates, unsupported_candidates, reason` |
| Relative / `RelativeResolution` | `record, contexts, issue` |
| Relative context / `ContextResolution` | `source_candidate, base_components, target_components, lossless, status, reason, supported_candidates, unsupported_candidates` |
| Source assessment / `SourceAssessment` | `candidate, state, diagnostic_present`; `diagnostic_present = (parse_diagnostic is not None)`; diagnostic text is deliberately omitted |
| Structural child evidence / `SubmoduleNameEvidence` | `candidate, submodule_candidate, component, component_supported, name_supported, source` |
| Definition evidence / `DefinitionNameEvidence` | `candidate, kind, symbol_kind, name, line, provenance` |
| Re-export evidence / `ReexportNameEvidence` | `candidate, kind, symbol_kind, name, asname, line, provenance` |
| Name occurrence / `NameEvidence` | `position, name, asname, outcome, reason, submodule, definitions, reexports` |
| Analyzer target context / `TargetContext` | `kind, source_candidate, base_module, base_components, target_components, lossless, status, reason, supported_candidates, unsupported_candidates, assessments` |
| Named context/occurrence / `TargetNameAnalysis` | `context_index, name_index, context, name` |
| Named analysis / `ImportNameAnalysis` | `record, target_kind, issue, contexts` |
| Wildcard candidate / `WildcardCandidateEvidence` | `candidate, source_state, all_state, all_entries, star_names, default_scope, outcome, reason` |
| Wildcard context / `WildcardContextAnalysis` | `context_index, context, candidates` |
| Wildcard analysis / `WildcardAnalysis` | `record, target_kind, outcome, reason, issue, contexts, source_assessments` |

Repeated nested `record` fields serialize the same record value; JSON does not preserve Python object identity. Full root and component fields remain present for every candidate, including relative `source_candidate`. Never replace structural identity with dotted display names, merge relative contexts, interleave supported/unsupported partitions or deduplicate declared export/name occurrences. The Python result retains all original diagnostics; omission is an explicit wire-only privacy projection.

**Canonical JSON body:** recursively insert object keys in lexicographic order, preserve array order, then use `json.dumps(data, indent=2, ensure_ascii=True)`. The schema contains no floats. CLI printing adds exactly the existing final newline; byte comparisons distinguish the body from body-plus-newline. No schema-version placeholder remains: this draft proposes `repo-intelligence.resolve.v1`. Approval fixes that version and projection; incompatible later changes require a new version.

**Complete empty-page example** (demonstrates that metadata survives an empty result):

```json
{
  "count": 0,
  "db_path": "/indexes/example.db",
  "filters": {
    "kind": "from_import",
    "module": "missing",
    "source_path": null
  },
  "limit": 10,
  "offset": 0,
  "records": [],
  "roots": [
    {
      "canonical": "src",
      "original": "src"
    }
  ],
  "schema_version": "repo-intelligence.resolve.v1",
  "total_count": 0
}
```

### P5. Optional additive CLI

```text
repo-index resolve --root ROOT [--root ROOT]... [--db-path PATH]
                   [--file PATH] [--kind import|from_import] [--module NAME]
                   [--limit N] [--offset N] [--json]
```

At least one explicit `--root` is required. `--file` maps to `source_path`; absent `--db-path` uses the existing current-directory index lookup. Existing commands and their behavior are unchanged.

**Proposed CLI source policy:** no source-text acquisition flags in this phase. The command calls the API with `target_sources=None`, which the wrapper normalizes to an empty mapping. Inventory-based structural evidence can still be reported; definition/re-export or declared-`__all__` evidence that needs supplied text cannot be promised by this CLI. The Python API supports caller-supplied mappings. Reading index text or files in a future CLI mode needs a separate design/scope revision.

`--json` prints P4 through the existing JSON printer. Text mode prints absolute status/reason unchanged; relative output prints the issue or one labeled line per context, including context index and source root/file, without inventing an overall resolution status. Pagination and `count` concern import records, not those display lines.

Exit status is 0 for a successfully completed query even if individual imports are unresolved, 2 for invalid CLI arguments, and 1 for a query/storage failure. Do not print raw analyzer/adapter exception text or parse diagnostics on CLI errors; use stable error labels such as `INVALID_QUERY`, `INDEX_NOT_FOUND`, `INDEX_INVALID`, `INDEX_READ_FAILED` or `EVIDENCE_INVALID`. These are CLI error categories, not resolution statuses.

## 5. Composition and one consistent read snapshot

1. Validate ordinary arguments and frozen root rules before opening the database. Snapshot caller mapping entries without mutation.
2. Open **one** existing-file, correctly URI-escaped, `mode=ro` SQLite connection using the Phase 2C-2B pattern, including query-only protection. Start one explicit `BEGIN DEFERRED` transaction before schema/data reads.
3. Validate required schema and global import/name orphan integrity on that handle. Call `build_inventory_from_db(conn, roots)` (or `load_file_records_from_db(conn)` followed by the frozen builder) using **that existing connection**, not a separate `db_path` call.
4. On the same snapshot, load the complete statement set matching the exact requested filters and all names for those statements. Preserve contiguous name positions, aliases, duplicate occurrences and representable malformed records under the frozen adapter's storage-validation rules. Invalid unrelated filtered-out rows need not be examined beyond global schema/orphan checks; invalid matching rows must fail even when pagination would hide them.
5. Validate all supplied source keys/values against the complete inventory. Compute the row-ID-free canonical order, `total_count`, and the selected page. Do not page inventory, candidate sets, name occurrences or relative contexts.
6. Dispatch each selected record according to the table below and attach unchanged frozen results. A tier conflict or validation exception aborts the call; never return a partial envelope.
7. Roll back/finish the read transaction and close the owned handle on every path. Do not create files, modify the database, close a caller-owned mapping or consult another DB connection during the query.

| Stored record | Wrapper dispatch and attached tiers |
|---|---|
| `kind="from_import"` and positive integer `level` | `routing="relative"`; attach the complete frozen relative result; `absolute/named/wildcard=None` under proposed Q4 deferral |
| All other structurally representable records | `routing="absolute"`; call the frozen absolute resolver even for empty targets, negative levels or unknown kind strings, preserving its classification |
| Absolute-routed `from_import` with any `"*"` name occurrence | Also call the frozen wildcard analyzer with the validated mapping; malformed star shapes raise its frozen exception; `named=None` |
| Absolute-routed `from_import` with nonempty names and no star occurrence | Also call the frozen named analyzer with the validated mapping; `wildcard=None` |
| Remaining records | `named/wildcard=None`; do not fabricate analysis for an empty names tuple or an ordinary `import` |

These rules do not call both absolute and relative resolution just to invent a preference. Existing analyzer functions may internally invoke the frozen resolver as their own contract requires. Valid-input tier parity is measured against direct calls with the same record, full inventory and mapping; wrapper argument validation is a separately specified boundary.

## 6. Proposed behavior matrix

| ID | Scenario | Required composed behavior |
|---|---|---|
| B1 | One supported absolute candidate plus unsupported matches | Absolute result remains `RESOLVED`; retain both candidate partitions; no top-level status |
| B2 | Multiple supported absolute matches | Frozen `AMBIGUOUS`; retain all supported candidates and any unsupported matches |
| B3 | No structural match, unsupported-only match, empty target, representable unknown kind/negative level | Preserve the appropriate frozen absolute status/reason; never silently discard the record |
| B4 | `from pkg import x` with a valid supplied source mapping | Named result equals the direct frozen analyzer result, including context/name indices, aliases and source assessments |
| B5 | Valid star import with supplied declared/default/unknown export evidence | Preserve wildcard state, occurrences, candidate order and source assessments; no runtime binding claim |
| B6 | Relative import with overlapping roots / contexts resolving differently | Preserve each source candidate, target components, lossless flag and context outcome; no aggregate winner |
| B7 | Relative pre-context issue or conflicting duplicate evidence | Preserve the issue and empty contexts, or propagate the frozen conflict exception; no partial return |
| B8 | Absent mapping / empty mapping / explicit unavailable source / invalid entry | Absence and empty mapping agree; missing key is `SRC_NOT_FOUND`, explicit unavailable is distinct; invalid mapping raises the specified wrapper error |
| B9 | Unparseable source with a diagnostic containing a canary | Python tier retains its original assessment; JSON omits diagnostic text and retains state plus `diagnostic_present` |
| B10 | Missing DB, malformed matching storage, orphan or engine read failure | Correct exception and no created database or partial envelope |
| B11 | Empty result, non-default filters, offset-only or zero-limit page | Envelope retains exact query metadata, pre-page total and correct page count |
| B12 | Writer commits between inventory and import reads | One query observes one established snapshot; never a mixture of generations |
| B13 | Overlapping roots / literal-dot components / repeated names and exports | JSON preserves root/component/context identity, partition order and all required occurrences |
| B14 | Optional CLI without a source mapping | Output matches API-with-empty-mapping projection; no hidden source reads or source-dependent evidence claim |

## 7. Error and sensitive-content boundaries

Missing DB raises `FileNotFoundError`; wrong argument types raise `TypeError`; invalid argument values and the specified unknown source-key case raise `ValueError`. Storage reconstruction/schema/orphan failures use `AdapterError`; engine errors remain `sqlite3.Error`. Frozen tier exceptions propagate without wrapping or partial results. These categories concern query execution, not per-context resolver status.

The Python API preserves frozen exceptions and diagnostics and is **not a confidentiality boundary**. Callers must protect inputs, results and exception logs. The CLI uses P5's safe error projection instead of dumping those messages.

The query reads required inventory/import metadata, not target files or FTS text. The wire projection never emits `TargetSource.source_text`, `TargetSource.parse_error`, free-form `SourceAssessment.parse_diagnostic`, source snippets, arbitrary object representations or caller mappings.

Identifiers, DB paths, aliases, structural evidence strings and declared export literals (`all_entries`, `star_names`) are deliberately preserved and **may themselves be sensitive**. Export literals can be arbitrary source-derived strings; the design does not promise secret detection, redaction, or that output is safe to share. Protect the JSON/index and review contents before external transmission. Removing raw text/diagnostics does not remove this responsibility.

## 8. Proposed acceptance matrix

| Test | Required coverage |
|---|---|
| T1 — absolute parity | B1–B3, including supported-plus-unsupported matches; no aggregate fields or invented routing precedence |
| T2 — source input | B4/B8; `None` normalizes only at wrapper; valid supplied/missing/unavailable/unparseable states; every invalid entry and unknown candidate checked even on empty pages |
| T3 — named evidence | Direct-call equality, record identity, flat context/name indexing, aliases/duplicates and missing-source structural evidence |
| T4 — wildcard evidence | B5; declared/default/unknown states, duplicate literal occurrences, unsupported partitions and malformed-star exception behavior |
| T5 — relative parity | B6/B7; differing root contexts, issue versus per-context failure, lossless flags, conflicts; named/wildcard remain absent under Q4 deferral |
| T6 — storage/errors | Missing DB, URI-special paths, roots/argument errors, schema and orphan corruption, bad matching names outside returned page; no partial envelope |
| T7 — metadata/paging | B11; empty results retain DB/roots/filters, `total_count` before page, `count=len(records)`, zero/offset-only pages and row-ID-independent ordering |
| T8 — canonical bytes | Same full input/snapshot gives identical P4 body; mapping insertion order irrelevant; changed supplied source can change evidence; CLI adds exactly one newline |
| T9 — JSON projection | Every P4 type/field; nested original records, candidate roots/components, relative source contexts, null versus empty arrays and repeated occurrences; no fallback `repr` |
| T10 — privacy | Source-text/diagnostic canaries omitted by projection; `diagnostic_present` retained; declared export strings preserved and explicitly treated as potentially sensitive |
| T11 — read-only snapshot | B12, one owned handle/transaction, helper called with that handle, no DDL/writes/FS reads of targets; DB unchanged in no-writer case; handles closed on success/failure |
| T12 — optional CLI | B14, exact JSON equality with API result, context-labeled text, required roots, exit categories and safe stderr; no behavior change to six existing commands |
| REG — regression | Run focused tests and the complete existing suite against the implementation revision; compare with recorded baseline evidence rather than asserting it was freshly run for this draft |
| GUARD — activation | Isolated schema, deny-once, continuation and pass checks on external fixtures; current-phase instructions and fresh-conversation load evidence |

These are proposed future checks. **No implementation tests or builds have been run for this planning revision.** A basic Stop-hook allow does not establish semantic parity, purity, completed tests or human acceptance.

## 9. Remaining approval choices

The revision provides one coherent recommended contract rather than leaving required API mechanics undefined. Human review may approve it or request another revision:

- **Q1:** include P5 now (recommended), or approve API-only. If deferred, remove the CLI production exception and mark T12 deferred in the approval record.
- **Q2:** approve P2's absence of an aggregate. A requested summary requires a new explicit table and tests before code.
- **Q3:** approve the mapping validation/default policy, source-dependent determinism and CLI-empty-mapping policy.
- **Q4:** approve relative named/wildcard deferral. Inclusion needs revised per-context attachment and validation/serialization tests, not an informal implementation choice.
- **Q5:** approve propagated frozen exceptions plus distinct, sanitized CLI error presentation.

## 10. Risks and correction traceability

| Risk / original finding | Revision-2 treatment |
|---|---|
| R1 — unreachable source-dependent analysis | P3 includes and validates `target_sources`; CLI limitation is explicit; T2–T4 |
| R2 — aggregate hides or reclassifies outcomes | P1/P2 remove it; direct statuses and relative contexts preserved; T1/T5 |
| R3 — serializer cannot reconstruct metadata | Immutable `ResolveQueryResult` supplies all context, counts and paging; T7 |
| R4 — provenance / unsafe projection unspecified | P4 fixes every field and recursive mapping; §7 states diagnostic omission and remaining sensitive metadata; T9/T10 |
| R5 — mixed database generations | §5 requires one connection/snapshot and a concurrent-writer test; T11 |
| R6 — setup scope incompatible with activation | §11 separates workflow and production paths and requires registration, validation and a fresh loaded conversation |

**Review disposition:** the independent review of revision 1 required changes. Codex has addressed R1–R6 in this proposed revision; that is an author disposition, **not** a new independent PASS, human acceptance or implementation authorization.

## 11. Proposed allowlists and guard activation

### Current documentation permission

Only this architecture draft and required proposal/review entries in PLAN.md may change now. Existing guards, hook configuration, AGENTS.md, source and tests remain untouched.

### Future workflow-preparation allowlist

Effective only after explicit approval of the scope revision:

- `AGENTS.md` — identify the approved phase, boundaries and activation gate.
- `PLAN.md` — record the exact approval source, scope, readiness, evidence and next gate.
- `docs/phase-2c-4-architecture-approved.md` — preserve the reviewed contract and actual human approval record.
- `docs/openhands-stop-hooks.md` — document new registration, checks, retry behavior and rollback.
- `.openhands/hooks.json` — **add** the new guard registration through the installed discovery mechanism.
- `.openhands/phase2c4_stop_guard.py` — implement the approved scoped guard.

These filenames are proposed exact choices. Any replacement must be recorded in the approval before setup. Backups, fixtures and bounded retry state belong outside the repository; record their resolved absolute locations before writing them. This permission does not expand production scope or authorize changes to either historical guard script.

### Future production/test allowlist

Effective only after guard activation **and** explicit implementation authorization:

1. `src/repo_intelligence/resolve_query.py` — new API, models and P4 projection.
2. `tests/test_resolve_query.py` — T1–T12 as applicable and REG evidence.
3. `src/repo_intelligence/cli.py` — only the additive resolve command if Q1 is approved.

All other existing production/test/dependency files, frozen contracts and historical guard scripts remain protected. Existing six CLI commands must not be altered. Workflow-preparation edits above are a separate approved scope, not exceptions available to routine production work.

### Required activation and handoff sequence

1. Record the real human approval, scope revision, exact paths, exclusions and acceptance checks in the approved contract and PLAN.md. Never convert this draft into an approval by assertion.
2. Back up the current hook configuration outside the repository. Preserve unrelated definitions and both historical guard registrations/scripts unchanged. Add the new registration; do not assume a generic phase-policy loader exists.
3. Validate the installed runtime schema and the new policy using external deny-once, continuation and pass fixtures. Keep per-conversation retry state external and bounded; passing checks must not reset a consumed budget. Operational failures remain fail-open with an inconclusive diagnostic.
4. Record checks and limitations. The guard checks the approved basic file/API/syntax and scope boundaries that can actually be established; focused/regression tests and independent review establish separate evidence.
5. Complete any **separately authorized** Git promotion of the configuration. Start implementation in a **fresh conversation that has loaded the merged intended configuration**, verify the hook and current instructions, and report readiness before production edits. Do not assume this planning conversation reloads changed hooks. Creation of the implementation conversation is a distinct authorized handoff.

Guard preparation, production implementation, test evidence, independent review, human acceptance and Git promotion remain separate gates. No setup, hook edit or new conversation is performed by approval-pending drafting.

## 12. Human review and approval checklist

All items are unchecked. The human can request revisions; the agent cannot mark its own acceptance.

- [ ] **A1 — Scope:** approve §1/§3, preserve frozen boundaries and exclusions.
- [ ] **A2 — Public contract:** approve P1/P3/P4 models, mapping input, validation, counts, pagination, schema version and complete serialization fields.
- [ ] **A3 — Resolution semantics:** approve P2's no-aggregate design and preserved direct/per-context outcomes.
- [ ] **A4 — Scope choices:** record Q1–Q5 answers, including CLI inclusion and deferred relative named/wildcard attachment.
- [ ] **A5 — Safety:** approve single-snapshot/read-only/error behavior, diagnostic projection and the warning that output metadata/export strings may be sensitive.
- [ ] **A6 — Paths and guard:** approve the separate workflow/production allowlists and exact activation/handoff requirements in §11.
- [ ] **A7 — Next gate:** explicitly authorize the requested next step (normally workflow preparation); record implementation, testing, review, acceptance and Git-promotion permissions separately rather than treating them as pre-completed.
- [ ] **A8 — Status:** confirm architecture approval is recorded only after A1–A7 are resolved; production work still requires its explicit authorization and completed guard readiness.

Until the human approves the revised contract, status remains **PLANNING — APPROVAL PENDING**. No checkbox, review report, draft save, passing hook or documentation check activates Phase 2C-4.

## 13. Planning and verification record

- Original draft: OpenHands; 287 content lines at the pinned implementation baseline.
- Independent review: CHANGES REQUIRED, findings R1–R6. This Codex revision addresses those findings and provides proposed choices for the human to review.
- Current content scope: this draft and minimal synchronized PLAN.md proposal/review entries only. The original review report remains a historical record, not rewritten into a PASS.
- Documentation validation for this revision is recorded in the delivery report. No production code, test, dependency, hook/configuration or AGENTS.md edit; no implementation test/build; no staging, commit or push.
- Next action: human review of revision 2 and A1–A8. Scope changes requested during that review must be incorporated before any guard/implementation gate is authorized.
