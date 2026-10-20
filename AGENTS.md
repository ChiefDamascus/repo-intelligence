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

Three completion guards are now wired in `.openhands/hooks.json`:

- `.openhands/phase2c3b1_stop_guard.py` — historical two-file allowlist for the merged 2C-3B-1 scope; unchanged.
- `.openhands/phase2c3b2_stop_guard.py` — historical two-file allowlist for the merged 2C-3B-2 scope; unchanged.
- `.openhands/phase2c4_stop_guard.py` — new, prepared 2026-10-20 under the approved Phase 2C-4 scope record; checks the 2C-4 implementation allowlist (additive `resolve` CLI + `resolve_query.py` + its test file) and is wired **additively**, preserving the two historical registrations byte-for-byte. Per the standing phase-workflow rules in this file, neither agent may modify either historical guard script or delete any of the three; do not reset the external bounded retry state; do not weaken fail-open behavior. The 2C-4 guard is **not** an approval or implementation authorization by itself — it is a gate that fires when a conversation (current or successor) attempts to finish while the 2C-4 allowlist files are incomplete.

Starting Phase 2C-4 implementation **or editing any Phase 2C-4 allowlist file** still requires (a) a fresh, separately approved Git promotion of this configuration **and** (b) a fresh conversation that has loaded the merged `hooks.json` and the approved record (with the v3 fixture evidence) before production edits begin. The human's 2026-10-20 implementation authorization is an **approval of scope + a request for the standard workflow**; it does **not** itself perform (a) or (b) in this chat. Do not begin 2C-4 (or any new phase) based on this file's text alone; this file is a standing-maintenance surface, not an approval. A guard allow under any mode does not establish production authorization, readiness, review-pass, acceptance, merge, or freeze; those remain separate gates.

On a Stop denial, follow the guard's own diagnostic. Continue within the guard's scope budget (at most one denial per conversation) and report any blocker that would require touching a frozen or non-allowlisted file. An allow decision from a guard is **not** evidence of acceptance, merge, or freeze — those remain separate human gates per the master plan tracker.

## Phase 2C-3B-1 (merged — historical record)

Approved architecture: [`docs/phase-2c-3b-1-approved-contract.md`](docs/phase-2c-3b-1-approved-contract.md)
Merged as PR #4 (merge `894af8b`, 2026-10-19). Implementation files preserved on `main`. See PLAN.md §10 for the full evidence trail (acceptance, Codex review, merge commit, file SHA256s).

## Phase 2C-3B-2 (merged — historical record)

Approved architecture: [`docs/phase-2c-3b-2-architecture-approved.md`](docs/phase-2c-3b-2-architecture-approved.md)
Original review: [`docs/phase-2c-3b-2-architecture-review.md`](docs/phase-2c-3b-2-architecture-review.md)
Merged as PR #5 (merge `eed78f9`, 2026-10-19). Implementation files preserved on `main`. See PLAN.md §10 for the full evidence trail.

## Next phase — Phase 2C-4 (ARCHITECTURE APPROVED — GUARD PREPARED + v3 FIXTURES PASSED — IMPLEMENTATION AUTHORIZED 2026-10-20 — §11 GATES PENDING)

Per PLAN.md master tracker, Phase 2C-4 — Query / API exposure — is **architecture-APPROVED** (human approved A1–A8 and Q1–Q5 on 2026-10-20) and, on 2026-10-20 (America/New_York), **implementation-authorized** by the human within the already approved revision-2 contract and Q1–Q5 choices; the prior stop request is revoked. Approved record at [`docs/phase-2c-4-architecture-approved.md`](docs/phase-2c-4-architecture-approved.md) (including §8 implementation-authorization and §8a implementation-authorization record); reviewed revision-2 draft preserved byte-identically at [`docs/phase-2c-4-architecture-review.md`](docs/phase-2c-4-architecture-review.md). The 2C-4 Stop guard is prepared and wired additively (`.openhands/phase2c4_stop_guard.py`, registered in `.openhands/hooks.json` alongside the two historical guards); its v3 fixture suite (P/Q/T, 26 isolated assertions) passes outside the repository and covers the staged/unstaged/untracked tracking, the fixed-width `--porcelain=v1 -z --untracked-files=all` path parsing, the rename-boundary both-sides detection, the leading-space filename preservation, the readiness-vs-production-completion split, and the deny-once/per-session budget behavior. **Implementation is authorized but has NOT started in this conversation, and no production edits, stage/commit/push, or pytest/build has occurred or is requested here.** Per the standing phase workflow and the §11 gate, remaining prerequisites before any `src/repo_intelligence/resolve_query.py`, `tests/test_resolve_query.py`, or additive `cli.py` edit: (a) a **separately authorized** Git promotion of this configuration (staging + commit + push as approved by the project owner), and (b) a **fresh Agent Canvas conversation** that has loaded the merged `hooks.json` and the approved record (with the v3 fixture evidence) — the promotion gate is delegated to Codex, the production work to the fresh OpenHands conversation. The 2C-4 allowlist (the three authorized production/test/CLI paths plus PLAN maintenance) is unchanged by this authorization; neither agent may expand it, weaken fail-open, reset the external bounded retry state, modify either historical guard script, or begin 2C-4 (or any new phase) based on this file's text alone — this file is a standing-maintenance surface, not an approval.
