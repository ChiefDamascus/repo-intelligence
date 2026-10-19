# Standing phase workflow

This policy makes guard preparation and plan maintenance part of normal agent work. It applies to OpenHands and Codex when they work in a checkout containing these instructions. It is standing guidance, not a background service or a new guard engine.

## At task start or resume

Read root AGENTS.md and PLAN.md, relevant phase records, explicit user approvals and applicable acceptance criteria. Check current branch, HEAD and working-tree status without changing Git state. Identify the active phase/subphase and current scope revision. Preserve in-progress edits, existing unrelated hooks and frozen boundaries. Never stash, reset, clean, switch branches or commit merely to set up this workflow.

If PLAN.md is stale, reconcile it using available evidence. Record verified current facts and their source, leaving unavailable historical results unverified. Do not reconstruct prior acceptance, review, merge or freeze from a branch name, a newer source file, a task title, or an agent's unsupported claim. Ask only for genuinely missing information or authorization; honor approval already provided by the user.

Keep the currently running implementation's hook unchanged while installing these instructions. Adopt this policy at the next safe task boundary. Do not assume a live conversation reloads files that were changed after it began.

## Before each newly approved phase or scope revision

Treat guard preparation as part of the phase, without asking for another reminder. Record or update a phase-specific approval/acceptance record when the approved contract is otherwise only in the current conversation. Preserve the user's full approved prompt and corrections where available; do not invent missing behavior matrices.

Record the phase identifier, approval source, scope revision, permitted production/test paths, frozen boundaries, API expectations, exclusions and acceptance checks. Do not begin implementation from a proposal alone. If approval exists but an exact required boundary is missing, do independent planning and request only the missing boundary.

Reuse the installed project hook discovery mechanism and a suitable existing guard runner. If the available guard is tied to the previous phase, adapt or create the new scoped guard and update its documentation. Root AGENTS.md must identify the current phase scope. Prefer data-only per-phase configuration when a validated runner supports it; the original Phase 2C-3B-1 guard does not have a generic phase-policy loader, and installing this guide does not add one.

Back up existing hook configuration outside the repository. Preserve unrelated hook definitions. Validate the installed runtime's schema and run isolated deny-once, continuation and pass cases before activating a new policy. Keep per-conversation retry state external and bounded; a successful check must not reset a consumed budget. Operational failures allow completion with an inconclusive diagnostic. Never silently remove these safeguards or disable a guard because setup is inconvenient.

Automate only checks that the scoped implementation can actually establish. Static presence, API and syntax checks do not establish purity, immutability, semantic coverage or successful regression tests. Keep the approved focused tests, regression tests, independent review and acceptance as separate recorded gates. Do not force a failing production file to demonstrate denial; use external fixtures.

Verify that the implementation conversation has the intended hook. If hook configuration is loaded only at conversation creation, finish setup before handing implementation to a fresh conversation. Report that handoff requirement; do not assume a running conversation acquired the new hook or create a new user conversation without authorization. Preserve the single-denial budget for the existing conversation; do not reset markers to obtain more interventions.

## Automatic PLAN.md updates

The agent performing or observing a material event updates PLAN.md during that task, without waiting for a user reminder. The phase/subphase summary, detailed entry and dated decision/change log must agree. Keep existing structure and useful history; make targeted edits rather than rewriting the roadmap.

| Observed event | Required plan update |
| --- | --- |
| Phase architecture proposed | Record proposed scope, checks, exclusions, dependencies and approval still required. |
| User approves implementation | Record exact approved scope and approval source; do not claim work or tests are complete. |
| Implementation starts | Record in-progress status, branch/HEAD where useful, guard readiness, outstanding checks and next gate. |
| Blocker appears or clears | Record the actual blocker, affected phase and what resolves it. |
| Focused or regression tests finish | Record exact command, results/counts, tested revision or working-tree state, and failures/skips/unrun checks. |
| Work is ready for review | Record implementation/test evidence and independent review pending. |
| Review findings arrive or are resolved | Record the finding/disposition and review evidence; do not infer acceptance. |
| Scope changes are proposed | Record old scope, requested delta, reason, effect and pending approval; preserve the approved scope. |
| User approves a scope change | Record the approval and new revision, update boundaries/checks and guard policy before related implementation. |
| Acceptance, merge or freeze is verified | Record the specific decision or Git/PR evidence separately for each event. |

Use the existing status vocabulary, adding narrowly defined interim labels only when needed to avoid claiming more than the evidence supports. Implementation complete, tests passed, reviewed, accepted, merged and frozen are different facts. A failed review can reopen work without erasing earlier evidence.

An agent may maintain records of a user's decisions; it cannot grant itself broader production scope or accept/freeze its own work. Leave exclusions explicit. Reconcile approved replanning of later phases too, including renamed/split/deferred phases and changed dependencies, but never execute a newly listed phase merely because it appears in the roadmap.

## Before completion or handoff

Read the relevant plan sections again. Ensure status, approved scope, test/review evidence, blockers and next action reflect the current task. Record all changed files and the final Git working-tree state as required by the approved phase. If writes are unavailable or explicitly forbidden, report the documentation update as pending and provide a precise patch; never claim PLAN.md was updated remotely without evidence.

Do not edit another running agent's files concurrently. If another agent is changing PLAN.md, wait for a safe handoff and merge targeted documentation changes without discarding its edits. Install/revise standing instructions between active implementation tasks.

## Loading and portability

Keep root AGENTS.md short enough to load, with this guide as the detailed reference. Codex discovers AGENTS.md in the checkout it actually uses; a separate local checkout must receive these files before it can follow them. OpenHands repository-context loading must be checked in the installed Agent Canvas environment using a fresh conversation. A hook appearing in the UI does not prove AGENTS.md was loaded.

After installing once, use a fresh conversation to ask: "Without using tools, summarize the repository's standing phase-workflow responsibilities. If none were provided, say so." Verify that the reply includes automatic guard preparation, evidence-based PLAN.md updates, and approval boundaries. This is a setup check, not a recurring phase reminder. If the instructions were not loaded, inspect the installed repository-context loader rather than assuming the policy is active.

Copy these two instruction files to other projects after adapting their plan filename and approval conventions. No automatic cross-project synchronization is installed. Keep later changes reviewable in the repository; staging/committing/distributing them requires the user's existing authorization or an explicit request.

## Limits of this installation

The existing Phase 2C-3B-1 Stop hook checks only its two authorized production/test files. It does not enforce PLAN.md freshness, detect every lifecycle event or automatically generate the next guard. These standing instructions assign that preparation and maintenance to the working agent. No model change or OpenHands core patch is needed.

Updates occur while an agent is working or at its next task start/resume. Events outside its accessible context, such as an external merge while no agent is active, cannot be recorded immediately by these instructions. A background monitor or machine-enforced phase/plan consistency checker would be a separate extension and is not installed here.

## Documentation references

- Codex instruction discovery: https://learn.chatgpt.com/docs/agent-configuration/agents-md
- OpenHands repository guidance overview: https://github.com/OpenHands/docs/blob/main/openhands/usage/agent-canvas/overview.mdx
