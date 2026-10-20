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

**Current state (as of 2026-10-19):** Both Phase 2C-3B-1 and Phase 2C-3B-2 have been **accepted, merged, and frozen** under their respective approved scope records:

- **Phase 2C-3B-1** (named imported-item static evidence analysis) — merged as PR #4 (`merge 894af8b`, 2026-10-19). Implementation files on `main`:
  - `src/repo_intelligence/imported_name_analysis.py`
  - `tests/test_imported_name_analysis.py`
- **Phase 2C-3B-2** (wildcard / `__all__` static evidence analysis) — merged as PR #5 (`merge eed78f9`, 2026-10-19). Implementation files on `main`:
  - `src/repo_intelligence/wildcard_import_analysis.py`
  - `tests/test_wildcard_import_analysis.py`

Both completion guards (`.openhands/phase2c3b1_stop_guard.py` and `.openhands/phase2c3b2_stop_guard.py`) remain **wired and unchanged** in `.openhands/hooks.json`; their scope is the historical two-file allowlist for each merged phase respectively. Per the standing phase-workflow rules in this file, neither agent may expand that allowlist for a new phase. Do not modify either guard script, do not modify `hooks.json`, do not remove the guards, and do not reset the external bounded retry state. Do not weaken fail-open behavior. Any future phase (e.g. Phase 2C-4) requires a fresh, separately-approved scope record, a matching approved guard, and a fresh conversation that has loaded the merged hook configuration before implementation begins. Do not begin Phase 2C-4 or any new phase based on this file's text alone; this file is a standing-maintenance surface, not an approval.

On a Stop denial, follow the guard's own diagnostic. Continue within the guard's scope budget (at most one denial per conversation) and report any blocker that would require touching a frozen or non-allowlisted file. An allow decision from a guard is **not** evidence of acceptance, merge, or freeze — those remain separate human gates per the master plan tracker.

## Phase 2C-3B-1 (merged — historical record)

Approved architecture: [`docs/phase-2c-3b-1-approved-contract.md`](docs/phase-2c-3b-1-approved-contract.md)
Merged as PR #4 (merge `894af8b`, 2026-10-19). Implementation files preserved on `main`. See PLAN.md §10 for the full evidence trail (acceptance, Codex review, merge commit, file SHA256s).

## Phase 2C-3B-2 (merged — historical record)

Approved architecture: [`docs/phase-2c-3b-2-architecture-approved.md`](docs/phase-2c-3b-2-architecture-approved.md)
Original review: [`docs/phase-2c-3b-2-architecture-review.md`](docs/phase-2c-3b-2-architecture-review.md)
Merged as PR #5 (merge `eed78f9`, 2026-10-19). Implementation files preserved on `main`. See PLAN.md §10 for the full evidence trail.

## Next phase — Phase 2C-4 (PENDING — NOT AUTHORIZED)

Per PLAN.md master tracker, the immediate next candidate milestone is **Phase 2C-4 — Query / API exposure**, status **PENDING — NOT AUTHORIZED**. As of this document, its scope, acceptance criteria, API contract, and completion guard have **not** been approved. The standing phase-workflow rules above still apply: no production edits without an approved scope revision, no guard installation without an approved record, and no stage/commit/push outside the two approved documentation files this task touches. Starting Phase 2C-4 (or any later proposed milestone 2D/2E/3/4/5/6) requires a fresh human approval and a matching approved scoped guard, as in the prior two phases.
