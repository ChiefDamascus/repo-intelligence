# Phase 2C-4 — Approved Architecture Record

**Status: ARCHITECTURE APPROVED — GUARD PREPARED + v3 FIXTURES PASSED — IMPLEMENTATION AUTHORIZED 2026-10-20 — GIT PROMOTION / FRESH-LOADED-CHAT READINESS STILL PENDING — PRODUCTION NOT STARTED (see §6, §8a).**
**Approved:** 2026-10-20 (UTC). **Approved by:** the human / project owner of `ChiefDamascus/repo-intelligence`.
**Base implementation revision:** `main` / `origin/main` at `4232f1486b220f15dcec41f0fa7294a940b68e7e` (verified identical to `origin/main` at task start).
**Working-tree state (honest record):** the working tree was already dirty when this approval-record task began (the 2C-4 review draft `docs/phase-2c-4-architecture-review.md` and the 2C-4 section of `PLAN.md` were present as uncommitted planning work from the earlier planning conversation). The tree remains dirty after this task: the six approved workflow paths are modified/created as authorized, and no other tracked or untracked file is affected.
**Reviewed artifact:** [`docs/phase-2c-4-architecture-review.md`](phase-2c-4-architecture-review.md) — Codex revision 2 (addresses independent-review findings R1–R6 from revision 1), SHA-256 `9eac2c76c171f8a2a4d29ef2a3c84927a4cc9f0ec319b98a6ef410a4bc742019`. Preserved on disk unchanged by this task; it remains the historical revision-2 artifact, not rewritten into a PASS or acceptance.
**PLAN.md baseline (pre-approval-record task) SHA-256:** `3b682480076d4378d06f76185d738befd4eebca5761b9e48451147d5cfa02a04`.

> This document is an **approval record and contract**, not an approval by assertion. The checkboxes below were completed by the human during the approval conversation on 2026-10-20; the agent is not permitted to fill them in on the human's behalf. "Approved" below is distinct from "implementation started", "tests passed", "reviewed", "accepted", "merged", or "frozen" — each of those remains a separate, unfulfilled gate at the time of this record.

## 1. Exact human approval

Verbatim approval conveyed by the human on 2026-10-20:

> "I approve these choices
> Q1: include P5 now
> Q2: approve P2's absence of an aggregate.
> Q3: approve the mapping validation/default policy, source-dependent determinism and CLI-empty-mapping policy.
> Q4: approve relative named/wildcard deferral.
> Q5: approve propagated frozen exceptions plus distinct, sanitized CLI error presentation
> I approve all of revision 2 and A1 - A8
> I approve the Phase 2C-4 architecture review
> Have codex use openhands and conserve the usage of codex"

**Scope of this approval:** architecture review (revision 2) + A1–A8 + Q1–Q5 resolutions, authorizing **workflow/guard preparation only**. This does **not** authorize production changes in the current conversation; a separate, explicitly authorized Git promotion of this configuration and a fresh implementation conversation that loads the merged configuration remain required before any `src/` or `tests/` edit for Phase 2C-4. "Have codex use openhands and conserve the usage of codex" is a resource-usage preference for this and future phases; it does not change the approved contract.

## 2. Approved checklist (A1–A8 — completed by the human)

- [x] **A1 — Scope:** §1/§3 of the review (single new query/serialization module + tests; optional thin CLI binding; separately approved workflow preparation); frozen inventory/resolver/analyzer boundaries and exclusions preserved.
- [x] **A2 — Public contract:** P1 `ResolveFilters` / `ResolvedImportRecord` / `ResolveQueryResult`; P3 `resolve_imports_from_index(db_path, roots, *, target_sources=None, source_path=None, kind=None, module=None, limit=None, offset=0)`; P4 `resolve_to_dict` with the complete field table and canonical-JSON body; `schema_version='repo-intelligence.resolve.v1'`.
- [x] **A3 — Resolution semantics:** P2's no-aggregate design; per-context/per-tier statuses and reasons preserved verbatim from the frozen 2C-2A/2C-3A/2C-3B-1/2C-3B-2 outputs; no new top-level status enum.
- [x] **A4 — Scope choices (Q1–Q5):** see §3 below for each answer and its approved effect.
- [x] **A5 — Safety:** single read-only snapshot (§5), fail-closed on storage/schema/orphan corruption, diagnostic-text omission from the wire projection, and the explicit warning that identifiers/DB paths/export literals (`all_entries`, `star_names`) may themselves be sensitive and must be treated as such by any external consumer.
- [x] **A6 — Paths and guard:** separate workflow-preparation allowlist (§4) and production/test allowlist (§5); §6 activation/handoff sequence as the required order.
- [x] **A7 — Next gate:** authorize workflow/guard preparation (this task). Implementation, testing, review, acceptance, and Git promotion remain separate, explicitly authorized, future gates — none are pre-completed by this approval.
- [x] **A8 — Status:** architecture approval is recorded here; production work still requires its own explicit authorization plus completed guard readiness and a fresh conversation that has loaded the merged intended configuration.

## 3. Q1–Q5 approved resolutions

| Question | Approved answer | Effect |
|---|---|---|
| **Q1 — CLI** | **Include P5 now** | `src/repo_intelligence/cli.py` gains an additive `resolve` subcommand (P5, §5 of the review). Existing six CLI commands remain unchanged. Guard checks include "additive `resolve` present + all six existing commands still present". |
| **Q2 — summary status** | **Approve P2's absence of an aggregate** | No new top-level `status`/`reason`; direct frozen tier outputs are preserved. No aggregate taxonomy is introduced. |
| **Q3 — source evidence** | **Approve mapping validation/default policy, source-dependent determinism, and CLI-empty-mapping policy** | P3 `target_sources=None` normalizes to `{}` at the wrapper boundary only; supplied mappings are snapshotted, validated, and passed unchanged; CLI calls with `target_sources=None` ⇒ empty mapping, no hidden source reads. |
| **Q4 — relative analysis attachment** | **Approve deferral** | Named/wildcard evidence attaches only to applicable **absolute** records; relative rows expose the complete frozen `RelativeResolution` and no named/wildcard attachment. Including it later requires a revised contract + tests, not an informal choice. |
| **Q5 — conflicting evidence** | **Approve propagated frozen exceptions + distinct, sanitized CLI error presentation** | Frozen tier exceptions propagate without wrapping; CLI uses stable error labels (`INVALID_QUERY`, `INDEX_NOT_FOUND`, `INDEX_INVALID`, `INDEX_READ_FAILED`, `EVIDENCE_INVALID`) rather than raw analyzer/adapter text. |

## 4. Approved workflow-preparation allowlist (this task)

Effective and performed as part of this approval-record task:

- `AGENTS.md` — identifies the approved phase, boundaries, and activation gate.
- `PLAN.md` — records the approval source, scope, readiness, evidence, and next gate.
- `docs/phase-2c-4-architecture-approved.md` — this approved-contract record.
- `docs/openhands-stop-hooks.md` — documents the new 2C-4 registration, checks, retry behavior, and rollback.
- `.openhands/hooks.json` — **additive** registration of the 2C-4 named hook; both historical registrations and all unrelated definitions preserved unchanged.
- `.openhands/phase2c4_stop_guard.py` — the approved scoped 2C-4 Stop guard (new file).

Backups, fixtures, and bounded retry state live outside the repository under `/projects/.openhands-stop-guard-state-10001/` (`backups/`, `fixtures/`, `retries-v1/`). This task's external backup set is stamped `*-2C3B4-20261020T023541Z` plus `PRE_STATE_2C3B4.txt`; see §7 for the exact paths and digests.

This allowlist does not expand production scope, and does not authorize modification of either historical guard script, `hooks.json`'s existing two entries, or any production/test file.

## 5. Approved production/test allowlist (future; not yet active)


1. `src/repo_intelligence/resolve_query.py` — new P1/P3/P4 implementation (models, validation, dispatch, snapshot, serializer).
2. `tests/test_resolve_query.py` — T1–T12 and REG coverage from §8 of the review.
3. `src/repo_intelligence/cli.py` — **additive** `resolve` command only; the six existing commands retain their exact names and signatures.

**Standing maintenance permission (routine in both preparation and production modes):** `PLAN.md` — summary / detail / dated-log entries. Per AGENTS.md and `docs/phase-workflow.md`, PLAN maintenance is a routine part of each phase's implementation work and is permitted in the production-phase scope check in addition to the three files above. This is **not** an exception to the three-file production allowlist and does **not** permit routine edits to `AGENTS.md`, `hooks.json`, approved contracts, or any other workflow-preparation path during production. The guard reflects this: `scope_check` in PRODUCTION mode allows `PRODUCTION_SCOPE union {PLAN.md}`, while PREPARATION mode additionally allows the full six-path `WORKFLOW_SCOPE` plus the guard file itself (as a preparation-phase gate artifact).

All other existing production/test/dependency files, frozen resolver/analyzer contracts, and the two historical guard scripts remain protected. Workflow-preparation edits are a separate approved scope, not exceptions available to production work.

## 6. Required activation and handoff sequence (approved order)

1. Record the real human approval, scope, exact paths, exclusions, and acceptance checks in this approved-contract file and `PLAN.md`. **DONE by this task.**
2. Back up current hook configuration outside the repository; preserve both historical guard registrations/scripts unchanged; add only the new 2C-4 registration. **DONE by this task.**
3. Validate the installed runtime schema and the new policy using external fixtures; keep per-conversation retry state external and bounded; a passing check must not reset a consumed budget; operational failures remain fail-open with an inconclusive diagnostic. **DONE by this task** (see §8; Agent Canvas live-conversation reload is NOT tested here).
4. Record checks and limitations. The guard establishes only the basic file/API/syntax and scope-boundary checks listed in §7; focused/regression tests and independent review establish separate evidence. **DONE by this task.**
5. Complete a **separately authorized** Git promotion of the configuration. Then start implementation in a **fresh conversation that has loaded the merged intended configuration**, verify the hook and current instructions, and report readiness before production edits. **NOT PERFORMED — separate gate; this chat has not reloaded the new guard and does not claim to.**

Guard preparation, production implementation, test evidence, independent review, human acceptance, and Git promotion remain **separate gates**; this task performs **only** the first two (workflow preparation + guard v3 fixture verification) and does **not** itself perform any of the rest. The **architecture-only approval** in this section 6 (steps 1–4 above) does not itself constitute implementation authorization — the subsequent **implementation authorization** by the human, recorded in §8a on 2026-10-20 (America/New_York), is a **separate, later** authorization that is **not** part of the architecture-only approval record in this section 6. No production file is created or modified by this task; see §8a for the implementation-authorization record.

## 7. The approved 2C-4 guard (as implemented)

- File: `.openhands/phase2c4_stop_guard.py` (new; standard-library-only).
- Registered in `.openhands/hooks.json` as the named hook `repo-intelligence-phase2c4-completion-guard`, command `/usr/local/bin/python3 -B /projects/repo-intelligence/.openhands/phase2c4_stop_guard.py`, `timeout=10`, `async=false`, under the existing `stop` / `matcher="*"` entry (additive; the two historical registrations byte-identical to the backup).
- Distinct prefix `PHASE2C4_GUARD` (vs. `PHASE2C3B1_GUARD`, `PHASE2C3B2_GUARD`); retry marker key `sha256(PREFIX || project-root || session)` so this guard has its own per-conversation denial budget even though it shares the same external state directory as the 3B-1/3B-2 guards.
- Retry state root: `/projects/.openhands-stop-guard-state-10001/retries-v1/` (shared directory; per-phase keys keep budgets independent).
- Checks performed (static/syntactic only): all three authorized files exist, are regular (no symlinks in the path chain), are under 1 MiB, and parse as valid Python; the production file declares a top-level synchronous `resolve_imports_from_index` whose first two positional parameter names are `db_path`, `roots` with no additional required positional/keyword-only parameters; the test file contains a top-level `test_*` function or a `test_*` method inside a top-level `Test*` class; `cli.py` contains an additive `resolve` binding on the `cli` group object **and** all six existing commands (`build`, `stats`, `files`, `search-files`, `symbols`, `imports`) remain present.
- Checks NOT performed (explicitly not claimed): purity, immutability, no-I/O, semantic correctness, T1–T12/REG coverage, pytest collection/passing, independent review, human acceptance, merge/freeze. A basic Stop-hook allow does not establish any of these.
- Operational failures (missing state, malformed input, wrong working directory, missing identity, timeout, corrupt/out-of-capacity markers, lock contention) **fail open** with a `FAIL_OPEN` diagnostic; the guard never emits `deny` from an error handler. Denial budget is at most one `deny` per conversation, persisted before the `deny` is returned; passing checks do not reset a consumed budget; later failures in the same conversation allow completion with the unresolved finding reported.

## 8. External locations (recorded before writing, outside the repository) and validation

**External locations (all outside the repository):**
- Backups: `/projects/.openhands-stop-guard-state-10001/backups/` — `AGENTS.md.bak-2C3B4-20261020T023541Z`, `PLAN.md.bak-2C3B4-20261020T023541Z`, `openhands-stop-hooks.md.bak-2C3B4-20261020T023541Z`, `hooks.json.bak-2C3B4-20261020T023541Z`, and `PRE_STATE_2C3B4.txt` (pre-state SHA-256 of the affected tracked files, including the two historical guard scripts).
- Retry state (per-conversation, bounded): `/projects/.openhands-stop-guard-state-10001/retries-v1/` (shared with 3B-1/3B-2; 2C-4 markers are `PHASE2C4_GUARD`-keyed).
- Fixtures (isolated, recorded before writing): `/projects/.openhands-stop-guard-state-10001/fixtures/2C4-scope/` containing `prep/` (scratch git repo: HEAD without the approved record), `prod/` (scratch git repo: HEAD with the approved record + valid production files), and `state/` (isolated bounded retry state, 0o700). Driver script: `/projects/.openhands-stop-guard-state-10001/fixtures/2C4-scope-run.py`.

**Validated here (direct, isolated guard calls — NOT the live Agent Canvas SDK executor):**
- `phase2c4_stop_guard.py` AST-parses under `/usr/local/bin/python3` (stdlib-only; Python 3.13-compatible).
- **`PLAN.md` standing-maintenance reconciliation (2026-10-20).** The guard's PRODUCTION-mode scope check was updated so `PLAN.md` is in the allowed changed-path set (per AGENTS.md and `docs/phase-workflow.md`, PLAN maintenance is a routine part of implementation work). No other workflow path gains a standing production exception; PREPARATION mode remains unchanged (full six-path WORKFLOW_SCOPE + guard file). The guard file and its scope constants, `mode_from_head` documentation, `scope_check` allowed set, and the deny diagnostic string were all reconciled consistently. This change does not expand the three-file production allowlist and does not permit routine edits to `AGENTS.md`, `hooks.json`, approved contracts, or any other workflow path in PRODUCTION mode. The external fixture suite was re-run (v3 extended) with two new cases: `T7` (PRODUCTION mode: change set = {`PLAN.md`, `src/repo_intelligence/resolve_query.py`} → **allow**, PLAN maintenance is permitted) and `T8` (PRODUCTION mode: change set = {`src/repo_intelligence/resolve_query.py`, `docs/phase-2c-4-architecture-approved.md`} → **deny**, unrelated workflow path is a scope violation). Full suite: P1–P3, Q1–Q6, T1–T12/REG (28 assertions in total) all pass.
- `.openhands/hooks.json` parses as valid JSON; the three named hooks load, and the first two (2C-3B-1, 2C-3B-2) are verified byte-identical to the `hooks.json.bak-2C3B4` backup (additive-only registration; no historical entry altered).
- `git diff --check` clean against the working tree (exit status 0) including the new untracked guard file.
- Historical-preservation hash check: the two historical guard scripts `phase2c3b1_stop_guard.py` and `phase2c3b2_stop_guard.py` retain their pre-preparation SHA-256 digests recorded in `PRE_STATE_2C3B4.txt`; the 2C-4 review draft `docs/phase-2c-4-architecture-review.md` retains SHA-256 `9eac2c76c171f8a2a4d29ef2a3c84927a4cc9f0ec319b98a6ef410a4bc742019`; the 2C-3B-1/2C-3B-2 implementation and test files retain their pre-preparation digests.
- **Isolated guard fixture results v3 (run with the saved driver `2C4-scope-run.py`; each case uses a distinct scratch repo + session ID; real state and historical guards are never touched):**

  *PREPARATION mode (HEAD lacks the approved record; production files absent by design):*
  - `P1` — AGENTS.md + PLAN.md change set → **allow** (labeled preparation/readiness; production artifacts explicitly NOT evaluated in this mode).
  - `P2` — out-of-scope `src/repo_intelligence/scanner.py` added → **deny** (scope violation detected).
  - `P3` — same session, 2nd call after P2 → **allow** (budget consumed; no fresh deny).

  *PRODUCTION mode — clean state (HEAD has approved record + valid production files):*
  - `Q1` — valid production change set (resolve_query.py + test + additive `resolve` CLI, all api/syntax/CLI-valid) → **allow** (file checks pass).
  - `Q2` — clean readiness (no production change) → **allow** (labeled readiness).
  - `Q3` — out-of-scope `docs/scanner-extra.md` → **deny**.
  - `Q4` — same session, 2nd call after Q3 → **allow** (budget consumed).
  - `Q5` — fresh session ID on same repo, out-of-scope `docs/scanner-extra-2.md` → **deny** (fresh session gets its own budget).
  - `Q6` — same fresh session, 2nd call after Q5 → **allow** (budget consumed).

  *PRODUCTION mode — staged / unstaged / rename / leading-space / readiness-specific:*
  - `T1` — tracked out-of-scope file, UNSTAGED modification → **deny** (exact path preserved: `tracked_outbase.txt`).
  - `T2` — tracked out-of-scope file, STAGED modification → **deny** (staged change detected; exact path preserved).
  - `T3` — rename IN→OUT (`git mv src/repo_intelligence/resolve_query.py scanner.py`) → **deny** (both sides detected; out-of-scope `scanner.py` is the violation; `missing src/repo_intelligence/resolve_query.py` also reported because the file now sits outside the scope).
  - `T3b` — rename OUT→IN (`git mv tracked_outbase.txt src/repo_intelligence/moved.py`) → **deny** (both sides detected; `src/repo_intelligence/moved.py` is the violation; `tracked_outbase.txt` also reported).
  - `T3c` — rename IN→IN across PRODUCTION_SCOPE (`git mv -f src/repo_intelligence/cli.py tests/test_resolve_query.py`) → **scope PASS** (asserted via `scope_check` directly: both endpoints in PRODUCTION_SCOPE; completion-checks failure on the destination is separate from scope, as approved).
  - `T6` — untracked out-of-scope file with a **LEADING-SPACE filename** (a real path character, not a separator) → **deny** (exact path preserved: ` leading space.txt`).
  - `T4-a` — out-of-scope file present → **deny** (consumes budget for session T4).
  - `T4-b` — repaired (file restored to HEAD content) → **allow** (genuine pass).
  - `T4-c` — out-of-scope file re-introduced AFTER the genuine pass, same session T4 → **allow** (budget already consumed; no fresh deny; unresolved finding reported).
  - `T4-d` — fresh session T4-fresh on same repo with the out-of-scope file still present → **deny** (own budget, own denial).
  - `T5` — clean post-promotion READINESS state (approved record in HEAD, no production change, **both `src/repo_intelligence/resolve_query.py` and `tests/test_resolve_query.py` ABSENT in the working tree**) → **allow**, explicitly labeled readiness (artifact/API/test/CLI completion checks are NOT evaluated in this readiness state; readiness notes carried: `missing src/repo_intelligence/resolve_query.py`, `missing tests/test_resolve_query.py`, `missing src/repo_intelligence/cli.py`).

  - The v3 fixture suite exercises the guard's HEAD-based mode discrimination, the fixed-width `--porcelain=v1 -z --untracked-files=all` path parsing (no path stripping; both rename sides consumed; leading-space filename preserved), the staged/unstaged/untracked tracking, the PRODUCTION gate for artifact/API/test/CLI checks (`production_present = any PRODUCTION_SCOPE path changed`), the readiness-state branch (missing files → notes, not failures), the denial-budget retention across an intervening pass, and the pass-does-not-consume behavior. It does NOT exercise the installed SDK executor/manager, the timeout path, or a live Agent Canvas conversation reload.

**Not performed here (explicit limits, not silently skipped):**
- Agent Canvas live-conversation reload of the new guard (requires a fresh conversation; the guard is not active in this chat).
- The timeout/fail-open path via the real SDK executor (the guard's fail-open code path is implemented and AST-verified; a dedicated timeout fixture would exercise it).
- Any production `pytest` run, build, or implementation test for Phase 2C-4 (no production code is created for Phase 2C-4 in this task).
- No independent PASS of revision 2 and no implementation acceptance is asserted by this record; revision 2 remains the reviewed human-approved architecture, preserved byte-identically at the SHA-256 above.

## 8a. Implementation authorization (truthful record, 2026-10-20)

On 2026-10-20 (America/New_York), the human explicitly authorized Phase 2C-4 implementation **within the already approved revision-2 contract and Q1–Q5 choices**, subject to the §11/standing-workflow prerequisite (Git promotion of the verified configuration, then a freshly-loaded conversation). This is an **authorization to implement**, not a record of implementation: as of this record, (a) no production code has been written, (b) no `pytest` or build has run, (c) no PR or merge or freeze has occurred. Implementation is delegated per the standing workflow to a new OpenHands conversation that has loaded the promoted configuration and the v3 fixture evidence above; Codex is reserved for the §11 git promotion gate and for the fresh-conversation handoff only. The prior stop request is revoked; the implementation scope is unchanged from the approved **§5 production/test allowlist** — exactly: `src/repo_intelligence/resolve_query.py`, `tests/test_resolve_query.py`, and the additive `resolve` command in `src/repo_intelligence/cli.py`. The §11 gate is the activation / handoff and readiness sequence of the historical review document, [`docs/phase-2c-4-architecture-review.md`](phase-2c-4-architecture-review.md), at its "## 11" section (the approved-record's §6 restates the same order).

## 9. Rollback

Restore the four backed-up working-tree files exactly as backed up (see §8), and remove the additive 2C-4 entry from `.openhands/hooks.json` (or restore the full 2C3B4 backup of `hooks.json`). Confirm the two historical guard scripts still match their pre-preparation digests before deleting the new 2C-4 guard file. Backups and retry markers are retained; do not delete retry state unless the conversation it belongs to is permanently retired.

## 10. Next gate (honest, separate, and NOT performed here)

A **separately authorized** Git promotion of this configuration (commit/push and, if desired, PR/merge) followed by a **fresh Agent Canvas conversation** that has loaded the merged intended `hooks.json` and this approved record, verifies the 2C-4 guard is installed and firing, and reports implementation readiness — before any `src/repo_intelligence/resolve_query.py`, `tests/test_resolve_query.py`, or additional `cli.py` edit for Phase 2C-4. This chat has **not** created or modified any production/test file, has **not** reloaded hooks live, and does **not** claim Phase 2C-4 implementation has started.
