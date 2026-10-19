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

The Phase 2C-3B-1 production/test allowlist is exactly:

- `src/repo_intelligence/imported_name_analysis.py`
- `tests/test_imported_name_analysis.py`

All other existing production modules and tests remain frozen for this milestone. The separately authorized hook installation adds only `.openhands/hooks.json`, `.openhands/phase2c3b1_stop_guard.py`, this file and the hook guide. Do not expand production scope, modify OpenHands core, change the model, start Phase 2C-3B-2, stage/commit changes or merge automatically.

On a Stop denial, inspect the reported issue within the two-file scope and continue once. If fixing it would require frozen-code changes, report the blocker and request separate approval. The guard permits at most one denial per conversation and fails open on operational errors. An allow decision is not evidence of acceptance.

Before acceptance, report focused tests, full applicable regression results, F1-F6 dispositions, changed files, final Git working-tree status and limitations. A01-A23 and the corrected D1-D17 matrix require verification beyond this hook. Independent Codex review remains required before acceptance and merge. Do not mark milestones complete or frozen based on hook output.

The historical `PLAN.md` was not updated by hook installation. Preserve the distinction between that historical plan and the separately approved Phase 2C-3B-1 scope documented in the hook guide.
