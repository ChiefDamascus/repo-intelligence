# Project completion guards for OpenHands

Status: project guard policy. Installation and Agent Canvas activation require the validation evidence described below; this document alone does not establish either.

## Verified runtime contract

The inspected Agent Canvas container runs openhands-sdk and openhands-agent-server 1.54.0. Its project is /projects/repo-intelligence. The observed branch is feat/phase-2c-3b-1-named-import-analysis and the inspected working tree was clean.

The installed agent server discovers project hooks in .openhands/hooks.json. User-level hook discovery is described as future functionality in the inspected source. Share this guide across workspaces, but install and validate a separate configuration in each project.

HookConfig uses the lowercase stop field, containing matcher entries and command hooks. A synchronous command receives the serialized event on stdin. The executor sets OPENHANDS_PROJECT_DIR, OPENHANDS_SESSION_ID and OPENHANDS_EVENT_TYPE. Its subprocess working directory is the configured project directory. A command can return JSON with decision set to allow or deny and a reason. Exit status 2 also blocks. Use exit status 0 and explicit JSON decisions for deliberate outcomes; reserve stderr for diagnostics. Never emit a deny decision from an error handler.

## Required project policy

Before activation, record the exact project root, authorized production and test paths, approved checks, and frozen boundaries. A branch name is not a file allowlist. Historical planning documents do not establish a newer milestone's authorization.

Check only the explicitly authorized production and test files. Do not scan other projects, rewrite code, repair the workspace, change models, patch OpenHands, stage changes, commit, switch branches, or alter Git history. Avoid checks that import project code or load arbitrary pytest plugins unless their execution is explicitly part of the approved checks. Static checks do not prove semantic correctness or passing tests.

Only a confirmed failure of an approved check may deny completion. Missing identity, an unexpected project root, malformed input, missing configuration, an unavailable checker, timeout, or state-storage failure must allow completion with a diagnostic. Do not describe that outcome as a passing check.

## Retry state

Keep state outside every repository, in a private directory owned by the runtime user. Identify records using a digest of the canonical project path and conversation/session identifier; never turn raw event data into a filesystem path. Refuse symlink-based state paths. Use atomic updates and locking so concurrent invocations cannot evade the retry limit.

Allow at most one deliberate denial per conversation for the initial policy. Persist the consumed denial before returning deny. A subsequent failing check allows completion and reports the unresolved finding. Passing checks must not reset the denial budget. Keep at most 4096 small marker records, with a nonblocking lock around capacity checks and marker creation. At capacity or on lock contention, fail open. Never automatically expire or evict markers: an old conversation can resume, and expiry would give it another denial. Clean up only after conversations are permanently retired, with deliberate operator action. Loss or manual removal of state resets the affected retry history.

## Installation and rollback

Capture branch, HEAD and Git status before installation. Inspect existing agent instructions and hooks. Back up an existing hooks.json outside the repository before replacement; merge the new named hook without deleting unrelated entries. Validate the merged configuration using the installed HookConfig. Use a bounded synchronous command timeout and an absolute interpreter path.

Document the hook in the repository's existing agent instruction file; if none exists, create AGENTS.md with a short link to docs/openhands-stop-hooks.md. Documentation and hook files are explicit installation additions, separate from the production/test allowlist. Review the resulting diff without staging or committing it. Backups and retry state stay outside the repository.

To roll back, restore the exact backed-up configuration, or remove only the newly installed named hook if other hooks have subsequently changed. Remove newly created files only after confirming they have not acquired unrelated changes. No core patch or model change is required.

## Validation evidence

Use a temporary fixture project and separate temporary state, not deliberate failures in production files. Exercise the installed SDK executor/manager with the real command protocol. Verify first failure denies, the next failure in the same session allows, a passing check allows, a separate session has its own budget, and a pass does not reset a consumed budget. Verify malformed input, absent session identity, wrong working directory, timeout, unwritable/corrupt state, and concurrent attempts fail open or preserve the denial bound as appropriate.

Then perform an Agent Canvas conversation test to establish actual hook loading and feedback-driven continuation. A direct command test alone does not prove Agent Canvas activation. Record exact commands and observed results, and confirm only intended installation/documentation files changed. Never label unrun checks as passed.

## Reusing this guide

Copy the guide to another project's documentation and supply a fresh explicit policy. Reinspect that environment's installed SDK schema and hook discovery behavior. Do not reuse repo-intelligence file paths, frozen boundaries, or test assumptions in another workspace. Hook feedback guides continuation; it does not expand the agent's authorization or replace independent review.


## Exact automatic checks for this installation

The guard reads only the two authorized files below, with a 1 MiB limit per file. Missing files and Python syntax errors are incomplete work. The production file must declare a top-level synchronous analyze_named_import accepting record, inventory and target_sources positionally in that order, with no additional required arguments. Optional extra arguments are tolerated. The test file must contain a top-level test_* function or a test_* method inside a top-level Test* class.

These checks do not execute production or test code. Test presence is a syntactic check, not proof of pytest collection or useful coverage. Decorators, reassignment, annotations, fixture validity and runtime behavior are not established. The guard never runs pytest, Git, a model call or a network request. A successful check permits completion without claiming acceptance.

Only Stop events with metadata.reason equal to agent_finished and a matching project working directory are eligible to deny. Missing or inconsistent session identity fails open. Each failure finding can request only the single continuation allowed for that conversation; later unresolved findings are reported with allow. There is no supported runtime switch to broaden the allowlist.

## Activation and validation

The installer validates the installed SDK schema and runs isolated standard-library tests plus seven real SDK manager/executor outcomes before writing project files, including an executor timeout that must allow completion. It verifies workspace configuration discovery after writing. Fixtures and their retry markers remain outside the real repository. No real production/test failures are introduced and the real conversation's retry budget is not consumed during fixture validation.

Configuration discovery and fixture SDK tests do not prove that a currently running Agent Canvas conversation has reloaded configuration. Start a new conversation in this project and inspect its Stop hook event and feedback. With incomplete authorized files, the first normal completion should request one continuation and the next should allow. With basic file checks passing, completion should allow immediately. If the files are already valid, use the isolated fixture results for denial testing; do not damage production files to force a denial.

The installer prints the external backup path and a guarded rollback command. Rollback restores only files whose contents still exactly match installation outputs, aborting if an operator has edited them. Backups and retry records are retained. The original feature branch and HEAD remain unchanged, while the four expressly authorized hook/documentation files become reviewable working-tree changes.

## Approved Phase 2C-3B-1 scope supplied by the project owner

The following supplied approval is recorded for scope and acceptance. It does not assert that implementation or tests are complete. The corrected D1-D17 matrix is referenced but its individual entries were not included in the supplied approval; this guide does not invent them.

# Phase 2C-3B-1 — Authorized Scope and Acceptance Checks

The Phase 2C-3B-1 approval authorizes implementation of named imported-item static evidence analysis only. The following is the approved scope from our implementation authorization, including binding corrections F1–F6.

## 1. Authorized production and test paths

Authorized production file — NEW

`src/repo_intelligence/imported_name_analysis.py`

Authorized test file — NEW

`tests/test_imported_name_analysis.py`

No other production or test file changes are authorized.

The following existing production modules are specifically frozen:

```
src/repo_intelligence/python_ast.py
src/repo_intelligence/module_inventory.py
src/repo_intelligence/absolute_resolution.py
src/repo_intelligence/relative_resolution.py
src/repo_intelligence/db_absolute_resolution.py
src/repo_intelligence/search.py
```

Existing frozen regression tests must not be modified.

## 2. Authorized public API

```
analyze_named_import(    record,    inventory,    target_sources) -> ImportNameAnalysis
```

Required properties:

- Pure, deterministic static analysis.
- Immutable result objects.
- Preservation of original `ImportRecord` identity.
- Candidate-scoped evidence with original root/file provenance.
- Compatibility with frozen absolute and relative resolvers.
- No filesystem access, database access, runtime import execution, or network access.

## 3. Approved acceptance checks

The following checks are mandatory for Phase 2C-3B-1.

| ID  | Acceptance check             | Expected result                                                                                   |
| --- | ---------------------------- | ------------------------------------------------------------------------------------------------- |
| A01 | Absolute imports             | Correct target resolution and named-item evidence                                                 |
| A02 | Relative imports             | Preserve each frozen relative-resolution context                                                  |
| A03 | Multiple target candidates   | Preserve candidate-specific evidence without aggregation                                          |
| A04 | Multiple package roots       | Never combine evidence across roots                                                               |
| A05 | Package eligibility          | Package initializers qualify as structural parents; ordinary modules do not automatically qualify |
| A06 | Literal-dot collisions       | Match using structural `name_components`, not dotted display aliases                              |
| A07 | Duplicate imported names     | Preserve every occurrence and original `name_index`                                               |
| A08 | Import aliases               | Preserve aliases; match evidence against the original imported name                               |
| A09 | Module-level definitions     | Detect applicable classes, functions, and async functions                                         |
| A10 | Assignment evidence          | Detect bounded module-scope `Assign` and `AnnAssign` bindings                                     |
| A11 | Direct module imports        | Recognize potential module-level re-export evidence                                               |
| A12 | Conditional imports          | Mark conditional module-scope evidence without asserting runtime execution                        |
| A13 | Function/class-local imports | Do not misclassify nested imports as module-level re-exports                                      |
| A14 | Missing source               | Preserve `SRC_NOT_FOUND` and independently available structural evidence                          |
| A15 | Unavailable source           | Preserve `SRC_UNAVAILABLE` without manufacturing definitions                                      |
| A16 | Unparseable source           | Preserve `SRC_UNPARSEABLE`; structural evidence remains intact                                    |
| A17 | Unsupported candidates       | Retain frozen support classifications; do not treat unsupported targets as confirmed              |
| A18 | Malformed records            | Deterministic handling of invalid kinds, empty names, and unsupported import forms                |
| A19 | Deterministic ordering       | Identical results under inventory permutations and repeated calls                                 |
| A20 | Immutability                 | Inputs are not mutated; outputs contain immutable values                                          |
| A21 | No I/O                       | No filesystem, SQLite, network, or runtime module execution                                       |
| A22 | Resolver compatibility       | Frozen statuses, reason codes, target candidates, and pre-context failures remain intact          |
| A23 | Regression suite             | All applicable existing tests pass without changes to frozen production code                      |

The approved OpenHands prompt also requires coverage of the corrected D1–D17 named-import behavior matrix.

## 4. Binding corrections F1–F6

These supersede contradictory provisions in earlier design reports.

| Correction | Mandatory requirement                                                                                          |
| ---------- | -------------------------------------------------------------------------------------------------------------- |
| F1         | Structural children require an eligible supported package initializer; do not invent namespace-package parents |
| F2         | Candidate-scoped source availability must remain observable even when evidence tuples are empty                |
| F3         | A bounded new AST pass may detect assignments without changing Phase 2A                                        |
| F4         | Nested imports must never suppress independent structural child evidence                                       |
| F5         | Invalid-kind and empty-name behavior must be deterministic and consistent with frozen failure semantics        |
| F6         | Creating the new test file is authorized; modifying existing frozen tests is not                               |

## 5. Explicitly excluded work

- Wildcard imports and `__all__` analysis — Phase 2C-3B-2.
- SQLite schemas, persistence, or query changes.
- CLI integration — Phase 2C-4.
- Dependency graphs.
- MCP integration or repository documentation generation.
- Runtime import execution or import-path precedence simulation.
- Unrelated refactoring.
- Automatic merge into `main`.

## 6. Required verification before acceptance

OpenHands must report the focused test results, full applicable regression results, F1–F6 dispositions, all changed files, the final Git working-tree status, and known limitations.

The implementation must then undergo independent Codex review before being accepted and merged.

Scope rule: Any change outside the two authorized paths, or any requirement to modify a frozen interface, is a blocker requiring separate approval. Phase 2C-3B-2 must not begin automatically.

## Installation-specific paths

Persistent external state: `/projects/.openhands-stop-guard-state-10001/retries-v1`. Backups and rollback manifests: `/projects/.openhands-stop-guard-state-10001/backups`.

## Phase 2C-3B-2 scope and guard (approved architecture; guard prepared 2026-10-19)

Approved architecture: `docs/phase-2c-3b-2-architecture-approved.md` (status:
ARCHITECTURE APPROVED — IMPLEMENTATION AUTHORIZATION, NOT STARTED). Original
review: `docs/phase-2c-3b-2-architecture-review.md`. Scope revision recorded
in `PLAN.md`. See §3, §7 and §13 of the approved architecture for the
implementation allowlist, W1–W24 acceptance matrix, and the
workflow-preparation allowlist, respectively.

### Implementation allowlist (two files, separate from the workflow allowlist)

```
src/repo_intelligence/wildcard_import_analysis.py
tests/test_wildcard_import_analysis.py
```

- Production: one public function
  `analyze_wildcard_import(record, inventory, target_sources)
  -> WildcardAnalysis` plus new result/evidence dataclasses and code
  constants in the approved architecture §7. Pure, deterministic, no
  filesystem/DB/network/runtime-import access; reuses the 2C-3B-1 carrier
  types `TargetContext` / `SourceAssessment` / `TargetSource` verbatim.
- Test: W1–W24 acceptance matrix (approved architecture §12).
- All other existing production modules and tests remain frozen /
  protected unchanged by this scope, including both 2C-3B-1 files, the
  2C-3B-1 stop guard, the existing `.openhands/hooks.json`, the AGENTS.md
  2C-3B-1 section, and the existing hook guide.

### Scoped 2C-3B-2 completion guard

Installed as the 2C-3B-2 named hook (alongside, not replacing, the 2C-3B-1
named hook):

- Guard file: `.openhands/phase2c3b2_stop_guard.py`
- Merged `hooks.json` entry (appended after the 2C-3B-1 entry under the
  same `stop.matcher = "*"`):
  name `repo-intelligence-phase2c3b2-completion-guard`;
  command `/usr/local/bin/python3 -B /projects/repo-intelligence/.openhands/phase2c3b2_stop_guard.py`;
  synchronous, timeout 10s.
- Prefix: `PHASE2C3B2_GUARD` (distinct from the 2C-3B-1 guard, which is
  `PHASE2C3B1_GUARD`). The retry-budget key is therefore
  `sha256(prefix || project-root || session)`, giving this guard its own
  per-conversation denial budget even when both guards share the same state
  directory.
- Retry state: `/projects/.openhands-stop-guard-state-10001/retries-v1/`
  (same external state root as the 2C-3B-1 guard; marker keys are
  phase-tagged so budgets do not collide).
- Backup and rollback: `/projects/.openhands-stop-guard-state-10001/backups/`
  holds `hooks.json.bak`, `AGENTS.md.bak`,
  `openhands-stop-hooks.md.bak`, and `RESTORE_2C3B2.txt` (restore
  instructions and pre-preparation SHA-256 for the three backup-tracked
  files).

### Checks performed (and not performed) by the 2C-3B-2 guard

- Performed (static, syntactic only):
  - Both authorized files exist and are regular files (no symlinks in the
    inspected path chain), under 1 MiB each.
  - Both files parse as valid Python (AST).
  - The production file declares a top-level synchronous
    `analyze_wildcard_import` whose first three positional parameter names
    are `record`, `inventory`, `target_sources`, with no additional
    required positional or keyword-only parameters.
  - The test file contains a top-level `test_*` function, or a `test_*`
    method inside a top-level class whose name begins with `Test`.
- Not performed (not claimed by the guard):
  - Purity, immutability, no I/O, no runtime import execution.
  - Semantic correctness of `analyze_wildcard_import`; W1–W24 coverage;
    pytest collection and passing.
  - Independent Codex review; user acceptance; merge.
  - Enforcement of the 2C-3B-1 allowlist (the 2C-3B-1 guard remains in
    place for its own scope; this guard does not substitute for it).
- Operational failures (missing state, malformed input, wrong working
  directory, missing identity, timeout, corrupt markers, capacity, lock
  contention) **fail open** — the guard returns `decision=allow` with a
  `FAIL_OPEN` diagnostic. It never emits `deny` from an error handler.
- Denial budget: at most one `deny` per conversation. The consumed budget
  is persisted (atomic marker write + fsync) before the `deny` decision is
  returned. Passing checks do not reset the budget. Subsequent failures in
  the same conversation allow completion with the unresolved finding
  reported.

### Validation performed during this preparation (2026-10-19)

- Guard script AST-parses under Python 3.13 (stdlib-only, no third-party
  imports).
- `hooks.json` parses as valid JSON and loads via the installed
  `openhands.sdk.hooks.config.HookConfig` (`stop` field carries both
  named hooks).
- 2C-3B-2 guard isolated checks (fixtures in
  `/projects/.openhands-stop-guard-state-10001/fixtures/`):
  - PASS case (valid fixture tree → `decision=allow`, prefix
    `PHASE2C3B2_GUARD`).
  - Deny-once case (fixture tree with missing authorized files → first
    call `decision=deny`, second call in the same session
    `decision=allow`, marker persisted to
    `retries-v1/`).
  - Fail-open case (guard run with a non-existent state directory →
    `decision=allow` with `FAIL_OPEN` in the reason; no marker is created
    as a side effect).
  - Independent-budget case (2C-3B-1 and 2C-3B-2 guards invoked with the
    same fixture tree and the same session id produce different marker
    keys, so they do not consume each other's budget).
- The 2C-3B-1 guard file was not modified (SHA-256 recorded in
  `RESTORE_2C3B2.txt` as `01bf67199702be94e1f2abe69438474d40054e6cc1821dd3fcd22d538e525c83`).
- Unrelated files (`PLAN.md`, the two approved architecture documents, the
  two 2C-3B-1 implementation and test files) retain their pre-preparation
  SHA-256 digests.

### Handoff notes (not performed in this task)

- The 2C-3B-2 implementation must run in a fresh Agent Canvas conversation
  in which the merged `hooks.json` is loaded at conversation start.
- The single-denial budget of the current 2C-3B-2 preparation conversation
  is preserved; this preparation task does not consume it (fixtures are
  outside the real repository, and the guard's state directory is only
  touched via the marker paths named above).
