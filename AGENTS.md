# Agent instructions

<!-- BEGIN STANDING PHASE WORKFLOW v1 -->
## Standing phase workflow

For every phase and subphase, guard preparation and PLAN.md maintenance are routine agent responsibilities. Do them without waiting for reminders. Read and follow [docs/phase-workflow.md](docs/phase-workflow.md) at task start/resume and before reporting completion.

- At task start/resume, read PLAN.md, the applicable approved scope and acceptance criteria, and current Git state. Reconcile observed progress with the plan. A branch name or a stale planning row is not evidence of approval or acceptance.
- Before production edits for a newly approved phase or scope revision, configure and validate its completion guard using the existing project hook mechanism. Reuse a suitable existing guard; create or adapt a scoped guard when necessary. Preserve unrelated hooks, frozen boundaries, external bounded retry state and fail-open behavior. Do not wait for the user to request guard setup again.
- Automatically update PLAN.md when implementation starts, a material blocker changes, tests finish, review findings arrive or are resolved, scope changes are proposed or approved, or acceptance/merge/freeze is verified. Update the summary, relevant phase detail and dated change log together, with evidence and next action.
- Record proposed scope separately from approved scope. An approved scope change updates the permitted paths, exclusions, checks and guard policy before related production work. Neither agent may approve its own scope expansion, acceptance or freeze. Existing user approval remains valid; do not repeatedly request it.
- Before finishing, synchronize PLAN.md with what actually happened, including pending review, limitations and unrun checks. A passing hook or test run is not acceptance or merge. Never advance the next phase automatically without its required authorization.
- Standing maintenance permission covers PLAN.md, this phase-workflow section, docs/phase-workflow.md, relevant approved phase records, and the project's named completion-hook configuration/script/tests/documentation. It does not expand any production/test allowlist, authorize changing OpenHands core or the model, permit changing unrelated settings/hooks, or authorize staging, committing, pushing, merging or repairing unrelated work.

Phase-specific sections below apply only to their named phase. When a later phase is explicitly approved, update the phase-specific guidance to its recorded scope; do not reuse an earlier phase's allowlist as a general policy. A read-only request or other explicit user constraint takes precedence: report a pending documentation patch when writes are not allowed.
<!-- END STANDING PHASE WORKFLOW v1 -->

## OpenHands completion guard

See [the project hook guide](docs/openhands-stop-hooks.md) for SDK 1.54.0 configuration, scope, validation, retry state, rollback and reuse in other workspaces.

**Current state (as of 2026-10-20):** Phases 2C-3B-1, 2C-3B-2 and **2C-4** are accepted, merged and frozen under their recorded scopes. Phase 2D remains **PROPOSED / NOT AUTHORIZED**:

- **Phase 2C-3B-1** (named imported-item static evidence analysis) — merged as PR #4 (`merge 894af8b`, 2026-10-19). Implementation files on `main`:
  - `src/repo_intelligence/imported_name_analysis.py`
  - `tests/test_imported_name_analysis.py`
- **Phase 2C-3B-2** (wildcard / `__all__` static evidence analysis) — merged as PR #5 (`merge eed78f9`, 2026-10-19). Implementation files on `main`:
  - `src/repo_intelligence/wildcard_import_analysis.py`
  - `tests/test_wildcard_import_analysis.py`
- **Phase 2C-4** (resolution query integration / CLI) — implementation [PR #6](https://github.com/ChiefDamascus/repo-intelligence/pull/6) merged at `a40de0a`; formal freeze [PR #7](https://github.com/ChiefDamascus/repo-intelligence/pull/7) merged at `56d427d` (2026-10-20). Frozen API: `ResolveFilters`, `ResolvedImportRecord`, `ResolveQueryResult`, `resolve_imports_from_index`, `resolve_to_dict`; additive `repo-index resolve`. Acceptance and tests: see PLAN.md §6 and §10.

Three completion guards are now wired in `.openhands/hooks.json`:

- `.openhands/phase2c3b1_stop_guard.py` — historical two-file allowlist for the merged 2C-3B-1 scope; unchanged.
- `.openhands/phase2c3b2_stop_guard.py` — historical two-file allowlist for the merged 2C-3B-2 scope; unchanged.
- `.openhands/phase2c4_stop_guard.py` — historical 2C-4 implementation-scope guard, prepared 2026-10-20 and wired additively; preserve its frozen scope, historical registrations, fail-open semantics and external bounded retry state. The guard does not itself establish acceptance/freeze or prevent edits on remote `main` or a fresh clone.

**Phase 2C-4 is closed and frozen.** Its historical guard-promotion and fresh-conversation prerequisites were satisfied before implementation; they are not pending implementation gates. Do not restart Phase 2C-4 or treat its former allowlist as authorization for new edits. Any correction to its frozen resolution interfaces, CLI integration or tests requires a separately approved scope, updated guard policy where applicable, and recorded verification. A guard `allow` is never acceptance, merge or freeze evidence. Phase 2D has no architecture or implementation approval.

On a Stop denial, follow the guard's own diagnostic. Continue within the guard's scope budget (at most one denial per conversation) and report any blocker that would require touching a frozen or non-allowlisted file. An allow decision from a guard is **not** evidence of acceptance, merge, or freeze — those remain separate human gates per the master plan tracker.

## Phase 2C-3B-1 (merged — historical record)

Approved architecture: [`docs/phase-2c-3b-1-approved-contract.md`](docs/phase-2c-3b-1-approved-contract.md)
Merged as PR #4 (merge `894af8b`, 2026-10-19). Implementation files preserved on `main`. See PLAN.md §10 for the full evidence trail (acceptance, Codex review, merge commit, file SHA256s).

## Phase 2C-3B-2 (merged — historical record)

Approved architecture: [`docs/phase-2c-3b-2-architecture-approved.md`](docs/phase-2c-3b-2-architecture-approved.md)
Original review: [`docs/phase-2c-3b-2-architecture-review.md`](docs/phase-2c-3b-2-architecture-review.md)
Merged as PR #5 (merge `eed78f9`, 2026-10-19). Implementation files preserved on `main`. See PLAN.md §10 for the full evidence trail.

## Phase 2C-4 (ACCEPTED, MERGED, FROZEN — historical record)

Approved architecture: [`docs/phase-2c-4-architecture-approved.md`](docs/phase-2c-4-architecture-approved.md); reviewed revision: [`docs/phase-2c-4-architecture-review.md`](docs/phase-2c-4-architecture-review.md). Human acceptance recorded 2026-10-20; implementation [PR #6](https://github.com/ChiefDamascus/repo-intelligence/pull/6) merged (`a40de0a7445065013eed428bd5734027bf365c53`), followed by formal freeze [PR #7](https://github.com/ChiefDamascus/repo-intelligence/pull/7) merged (`56d427d09d42b79eacb0221a7d3e63f1b248d219`). Post-merge validation: 87 focused and 911 full-suite tests passed (exit 0). The new resolver module, its tests, the additive CLI command and the approved public interfaces are frozen. The existing Stop guard is scoped to the historical implementation allowlist and is not a remote branch protection mechanism. Historical v3 guard fixtures, implementation authorization and review corrections are retained in PLAN.md and the approved record.

## Next proposed phase — Phase 2D (NOT AUTHORIZED)

Phase 2D would model evidenced direct local module/file dependencies. It is a roadmap proposal only. Do not begin architecture drafting, guard setup or implementation without the project owner's next-phase authorization. Frozen Phase 2C contracts may change only via a separately approved correction milestone.
