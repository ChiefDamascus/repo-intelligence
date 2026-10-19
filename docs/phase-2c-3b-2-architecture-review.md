# Phase 2C-3B-2 — Wildcard / `__all__` Static Evidence Analysis (Architecture Review)

**Status: PLANNING — APPROVAL PENDING** (documentation only; implementation NOT authorized)
**Drafted:** 2026-10-19 (America/New_York) · **Base:** branch `main`, Git HEAD `ee29406f3b8da1be4a4a182a54ee1c38c7428ef9` (observed via `git rev-parse HEAD` at preflight), clean tree
**Depends on (protected, unchanged by this scope; no new formal freeze asserted):** Phase 2C-3B-1 (`analyze_named_import`; merged to `main`, PR #4), 2C-3A, 2C-2A, 2C-2B, 2C-1, 2B-*, 2A, 1.

> Every API name, evidence type, reason/outcome code, and file allowlist
> below is **PROPOSED** and must pass independent Codex review and explicit
> user acceptance before any implementation. Nothing in this document
> authorizes coding, staging, committing, push, merge, or guard change.

## 1. Purpose

Propose a **pure, static, conservative** analyzer for `from X import *`
records that produces candidate-scoped evidence about which names the star
*declares*, using only the frozen inventory and caller-supplied source text.
Static evidence is distinguished from runtime bindings at every field.

## 2. Baseline & prerequisites

| Item | Actual status |
|---|---|
| Phases 1, 2A, 2B-1–2B-4, 2C-1 | FROZEN (accepted & merged; `main`) |
| 2C-2A, 2C-2B, 2C-3A | MERGED / COMPLETE (accepted & merged; `main`), **protected unchanged by this scope — no new formal freeze asserted here** |
| 2C-3B-1 production + tests + contract | MERGED (PR #4), **protected unchanged by this scope — no new formal freeze asserted here** |
| `.openhands/` 2C-3B-1 hook + guard | Installed; preserved as-is; not modified here |
| 2C-3B-2 guard | **NOT installed — prepared within the eventual approved 2C-3B-2 phase scope under the standing rules** (§13), not as separate reminders |
| Star-record shape `ImportRecord(names=(("*", None),))` | Already supported by the frozen `ImportRecord` contract |

## 3. PROPOSED scope & exclusions

**New production (PROPOSED allowlist):** one new module
`src/repo_intelligence/wildcard_import_analysis.py` exposing one public
function `analyze_wildcard_import` plus the new result/evidence dataclasses
and code constants in §7.

**New tests (PROPOSED allowlist):** one new file
`tests/test_wildcard_import_analysis.py` (W1–W24 in §12).

**Frozen / protected (no change in this scope):** every existing production
module and test, including both 2C-3B-1 files, all resolvers, inventory,
record contract, `.openhands/` configuration, and AGENTS.md phase-2c-3B-1
section. Standing-workflow paths (AGENTS.md 2C-3B-2 section, hook guide,
new scoped guard) are **proposed for later workflow preparation** (§13),
not modified now.

**Out of scope:** runtime import execution; transitive re-export star
resolution; `__getattr__`/`__dir__`/dunder-protocol bindings; dynamic
namespace mutation; CLI/DB/graph/MCP/documentation generation; and any edit
to protected files.

## 4. PROPOSED public API

```python
def analyze_wildcard_import(
    record: ImportRecord,
    inventory: tuple[ModuleCandidate, ...],
    target_sources: Mapping[ModuleCandidate, TargetSource],
) -> WildcardAnalysis: ...
```

- `record`: immutable `ImportRecord`. A **valid record must have exactly
  one name entry `("*", None)`** and a supported `kind` (`"from_import"`;
  a `"import"`-kind star is malformed; ordinary non-star records return
  `WR_NOT_IN_SCOPE`, per §11).
- `inventory`: a **tuple** of frozen `ModuleCandidate` values (exact
  type). The input is not mutated; results use the canonical candidate
  ordering of the frozen resolvers (permutation-invariant output).
- `target_sources`: **required** `Mapping[ModuleCandidate, TargetSource]`
  (inherited carrier class from 2C-3B-1; `None` → `TypeError`; missing key
  → synthesized `SRC_NOT_FOUND`). Every key must identify an inventory
  candidate; every value is validated against the 2C-3B-1 state table
  (invalid state or state/text/`parse_error` combination → `TypeError`)
  **before** routing or any target is matched.

**Guarantees inherited from 2C-3B-1:** pure, deterministic, candidate-
scoped, `result.record is record`, no fs/db/network/runtime-import access,
frozen-resolver semantics preserved verbatim (statuses, reasons,
partitions, provenance).

## 5. PROPOSED immutable result contract

All types new, `@dataclass(frozen=True)`, one declaration per code constant.
`TargetContext` / `SourceAssessment` / `TargetSource` are **reused verbatim
from 2C-3B-1** (no new context wrapper is invented).

```
WildcardAnalysis
  record:              ImportRecord                       # identity preserved
  target_kind:         TARGET_ABSOLUTE | TARGET_RELATIVE | TARGET_INDETERMINATE
  outcome:             str                                # WR_ANALYZED | WR_NOT_ANALYZED | WR_NOT_IN_SCOPE
  reason:              str | None                         # WR_* reason code (root-level reason)
  issue:               str | None                         # frozen record-level issue (None on normal analyzed path)
  contexts:            tuple[WildcardContextAnalysis, ...]    # empty for NOT_IN_SCOPE / pre-context failure
  source_assessments:  tuple[SourceAssessment, ...]       # one per unique inventory candidate, canonical order

WildcardContextAnalysis
  context_index:        int                               # 0-based
  context:              TargetContext                     # inherited 2C-3B-1 type
  candidates:           tuple[WildcardCandidateEvidence, ...]   # canonical order; multiplicity INSIDE

WildcardCandidateEvidence
  candidate:            ModuleCandidate
  source_state:         SRC_SUPPLIED | SRC_NOT_FOUND | SRC_UNAVAILABLE | SRC_UNPARSEABLE
  all_state:            ALL_ABSENT | ALL_DECLARED | ALL_UNKNOWN
  all_entries:          tuple[str, ...]                   # declared literal occurrences: order + duplicates preserved; () if ALL_ABSENT / ALL_UNKNOWN
  star_names:           tuple[str, ...]                   # syntactic name evidence, see §8
  default_scope:        str | None                        # DIRECT_ONLY | INCOMPLETE for ALL_ABSENT; otherwise None
  outcome:              str                                # WR_ANALYZED | WR_NOT_ANALYZED
  reason:               str                                # WR_* reason code
```

**`star_names` content — two distinct shapes (both syntactic evidence, never
established runtime bindings):**

- `ALL_DECLARED`: `star_names` carries the literal declared occurrences
  exactly as they appear in `__all__` (order and duplicates preserved); these
  are *declarations*, not evidence of actual binding (§8).
- `ALL_ABSENT` (star-default rule): `star_names` carries the concrete
  default-binding names, **deduplicated and sorted** for determinism.

**Per-candidate `not found` / `unparseable` / `unsupported` handling:** a
candidate whose source is missing, unparseable, or whose target is
unsupported is retained with `all_state=ALL_UNKNOWN` and EMPTY
`all_entries` / `star_names` tuples (never best-effort salvage); see §7, §9.

**Cardinality:** exactly one `WildcardContextAnalysis` per frozen resolver
context (absolute: one; relative: one per frozen
`ContextResolution` interpretation). Candidate multiplicity is **inside**
`candidates`, never converted into context multiplicity, and never merged
across roots or candidates. `source_assessments` is a flat tuple, one
entry per inventory candidate (state table inherited from 2C-3B-1).

## 6. PROPOSED static `__all__` grammar (small, closed set)

A module *declares* `__all__` **iff** its direct module-body statements
contain **exactly one** statement that is:

- `Assign`, `targets == [Name(id="__all__")]`, no annotation, OR
- `AnnAssign`, `target == Name(id="__all__")`, `value` not `None`,

and whose `value` is a **list or tuple literal every element of which is
`ast.Constant` of type `str`** (empty list/tuple allowed; duplicates and
order preserved as declared).

**Not declared / rejected as `__all__` (all → `ALL_UNKNOWN` when the
candidate source is parseable):**

- any other AST shape (`BinOp`, `IfExp`, `ListComp`, `ast.Name`, `Call`,
  starred/unpacking, annotation without value, tuple-of-list, etc.),
- module-executing loop or conditional writes to `__all__`. Ordinary
  function- and class-local `__all__` bindings are ignored; writes declared
  `global __all__` are treated conservatively as unknown,
- **multiple** `__all__` assignments (reassignment, mutation, conditional
  branches even if every branch is literal),
- `del __all__`, attribute-based writes (`m.__all__ = ...`), alias-escape
  (`x = __all__; x.append("z")`), `__all__.append(...)`, method calls,
  container mutation of any kind,
- any module-executing control flow / loop / `try` / `with` / `match` write
  to `__all__`, or a `global`/`nonlocal` `__all__` write under a nested
  scope — such writes **can overapproximate; the analyzer never infers
  runtime execution**, so they resolve to `ALL_UNKNOWN`,
- any `__all__ = ...` where the RHS cannot be proven a literal
  str-container by the closed grammar above.

**Dynamic namespace invalidation:** any detected module-namespace dynamic
manipulation (`exec`, `eval`, `globals()`/`locals()`/`vars()` writes,
`del __all__`, or a module-executing star import) or alias escape capable of changing `__all__`
**invalidates declared/absent certainty → `ALL_UNKNOWN`, no fallback.**

When `ALL_UNKNOWN`, `analyze_wildcard_import` emits **NO name evidence and
NO default fallback** for that candidate. **No first-assignment winner. No
best-effort or partial parse salvage. No name inference from dynamic state
(`globals`, `locals`, `exec`, `eval`, f-string, subscript reads).**

## 7. PROPOSED code constants (declared once, exhaustive list)

Source states — **inherited verbatim from 2C-3B-1:**
`SRC_SUPPLIED`, `SRC_NOT_FOUND`, `SRC_UNAVAILABLE`, `SRC_UNPARSEABLE`.

`__all__` states (**new**):
`ALL_ABSENT` (no `__all__` statement at all) ·
`ALL_DECLARED` (exactly one closed-grammar assignment) ·
`ALL_UNKNOWN` (present but not grammatically closed — covers mutation,
conditional, multiple, dynamic, non-literal, aliased escape).

Outcomes (**new namespace `WR_*`, consistent with 2C-3B-1 `NAME_*`):**
- `WR_ANALYZED` — static analysis completed against supplied evidence.
- `WR_NOT_ANALYZED` — target not analyzable (no candidate, unsupported).
- `WR_NOT_IN_SCOPE` — record is not a star record under this API.

Reasons (**new**):
- `WR_TARGET_NO_CANDIDATE` — frozen resolver found no supported candidate.
- `WR_TARGET_UNSUPPORTED` — candidate unsupported; not analyzable.
- `WR_ALL_ABSENT_DEFAULT_APPLIED` — no `__all__`; star-default rule
  (§8) applied on this candidate.
- `WR_ALL_DECLARED_APPLIED` — `__all__` declared per §6; entries adopted.
- `WR_ALL_UNKNOWN_NO_EVIDENCE` — `__all__` present but not grammatically
  closed; no name evidence emitted, no default fallback.
- `WR_SOURCE_UNPARSEABLE_NO_EVIDENCE` — source state is
  `SRC_UNPARSEABLE`; no name evidence.
- `WR_SOURCE_NOT_FOUND_NO_EVIDENCE` — source state is
  `SRC_NOT_FOUND`; no name evidence.
- `WR_SOURCE_UNAVAILABLE_NO_EVIDENCE` — source state is
  `SRC_UNAVAILABLE`; no name evidence.
- `WR_INVALID_RECORD` — a well-typed non-star record is outside this API;
  returned only at the root with `outcome=WR_NOT_IN_SCOPE`, `contexts=()`
  (see §11). Malformed star records raise `ValueError` instead.

**Malformed star** (a `*` with an alias, a mixed `*` plus other names, or a
star under the wrong `kind`) is a caller/contract error → **`ValueError`** is
raised (not returned as a result). Wrong Python types on any argument →
**`TypeError`**.

`target_kind` — **inherited from 2C-3B-1 constants:**
`TARGET_ABSOLUTE`, `TARGET_RELATIVE`, `TARGET_INDETERMINATE`. For
well-formed star records `target_kind` comes from the frozen routing; it is
`TARGET_INDETERMINATE` on the non-star `WR_NOT_IN_SCOPE` return path.
`TARGET_MIXED_ROUTING` is NOT used for star records in this API.

**Outcome semantics (declared once):**
- `WR_ANALYZED` — **root-level**: the analyzer wrapper ran against the
  supplied evidence; it does NOT assert resolver success. Per-candidate it
  means the candidate's `__all__` state is a known `ABSENT` or `DECLARED`
  (evidence emitted or legitimately empty).
- `WR_NOT_ANALYZED` — target not analyzable (no candidate, unsupported) or a
  candidate is in an `ALL_UNKNOWN` / unavailable-source state (no evidence).
- `WR_NOT_IN_SCOPE` — record is not a star record under this API (well-typed
  non-star), at the root with `contexts=()`.

## 8. Star-default evidence (only when `ALL_ABSENT`)

For `ALL_DECLARED`, `all_entries` and `star_names` retain every literal
occurrence in declaration order, including duplicates, underscores, empty
strings and absent bindings. They establish declaration membership only.
No name-to-binding cross-check or runtime export confirmation is performed.

For `ALL_ABSENT`, inspect direct module-body statements only:

- Collect names of `FunctionDef`, `AsyncFunctionDef`, and `ClassDef`.
- Collect a single `Name` target from `Assign` or valued `AnnAssign`.
- `import a.b` binds `a`; `import a.b as z` binds `z`.
- `from m import n` binds `n`; `from m import n as z` binds `z`.
- Exclude every local name beginning with `_`. Deduplicate and sort
  retained names by Python string code-point order.
- Never emit `*`, chase transitive star exports, or descend into ordinary
  class/function bodies to collect local bindings.

The proposed `default_scope` field is `DIRECT_ONLY` when no syntactically
visible module-executing binding construct falls outside those rules. It is
`INCOMPLETE` for module-executing control flow that binds/removes ordinary
names, deletion of an ordinary name, destructuring or other unhandled binding
forms. In that branch retain the direct syntactic occurrences and use
`WR_ALL_ABSENT_DEFAULT_APPLIED`; this is never a guarantee about runtime
exports. For example `a=1; del a` retains syntactic evidence for `a` and
sets `default_scope=INCOMPLETE`.

Any detected dynamic module namespace manipulation, alias escape involving
`__all__`, module-executing star import, or conditional/global write capable
of changing `__all__` instead yields `ALL_UNKNOWN`, `WR_NOT_ANALYZED`,
empty evidence, and `default_scope=None`. There is no default fallback.
`default_scope` is also `None` for declarations and missing/unparseable/
unsupported sources. All fields describe bounded static evidence; arbitrary
runtime side effects, decorators, imported code and function calls are not
executed or proved safe by this analysis.

## 9. Unsupported, unknown, and error handling

| Situation | Handling |
|---|---|
| `TargetSource.provenance` not in the inherited state set | `TypeError` (inherited 2C-3B-1 state table) |
| `TargetSource` state/text/parse_error combo invalid | `TypeError` (inherited) |
| `target_sources` key not identifying an inventory candidate | **PROPOSED: reject with `ValueError`** (explicitly proposed, not silently inherited); the key may be out-of-scope and is never resolved |
| `inventory` not a `tuple[ModuleCandidate, ...]`; non-candidate element | `TypeError` |
| `record` not an `ImportRecord` | `TypeError` |
| Any argument with a wrong Python type | `TypeError` |
| **Malformed star** (alias on `*`, `*` mixed with other names, star under wrong `kind`) | **`ValueError` raised** (contract error; NOT returned as a result) |
| Well-typed **non-star** record (e.g. `from m import a, b`) | Returned: root `outcome=WR_NOT_IN_SCOPE`, `reason=WR_INVALID_RECORD`, `contexts=()`, `target_kind=TARGET_INDETERMINATE` (see §11) |
| `SRC_UNPARSEABLE` / `SRC_UNAVAILABLE` / missing key | Candidate retained with `all_state=ALL_UNKNOWN`, `all_entries=()`, `star_names=()`; reason one of `WR_SOURCE_*_NO_EVIDENCE`; other candidates analyzed normally |
| `SRC_SUPPLIED` text fails `ast.parse` | Candidate is treated as `SRC_UNPARSEABLE` → `ALL_UNKNOWN`, `WR_SOURCE_UNPARSEABLE_NO_EVIDENCE`; parse is the **analyzer's** responsibility (caller need not pre-parse); **no salvage** |
| `ALL_ABSENT` | Star-default rule (§8) applies per candidate; known → `WR_ANALYZED` |
| `ALL_DECLARED` | Entries adopted per §6; binding evidence unestablished (§8); known → `WR_ANALYZED` |
| `ALL_UNKNOWN` | No name evidence, no default fallback; `WR_ALL_UNKNOWN_NO_EVIDENCE`; `WR_NOT_ANALYZED` |
| Multiple supported candidates (AMBIGUOUS, per D7 in 2C-3B-1) | One `WildcardCandidateEvidence` per candidate inside `candidates`, canonical order, never merged |
| **Unsupported candidate** | Preserve the inherited unsupported partition and emit a candidate row with `WR_NOT_ANALYZED`, `WR_TARGET_UNSUPPORTED`, `ALL_UNKNOWN`, empty evidence and `default_scope=None`; do not parse its source. Supported rows precede unsupported rows, preserving resolver order within each partition. |
| Frozen resolver pre-context failure (`REL_*` issue) | `WildcardAnalysis.issue` preserved; `contexts=()`; root `outcome=WR_NOT_ANALYZED` (mirror D17/F5) |
| Conflicting duplicate candidates | **Propagate the frozen resolver's `ValueError`** (frozen semantics preserved) |
| Unknown candidate states / unavailable sources | Per-candidate `WR_NOT_ANALYZED` with `ALL_UNKNOWN`; only known `ABSENT`/`DECLARED` candidates are `WR_ANALYZED` |
| Absolute + relative mixed star record | **Malformed star → `ValueError`** |

## 10. Ordering, identity, determinism

- **Exact canonical ordering (PROPOSED):** reuse the frozen resolver's
  context sequence and the resolver's candidate sequence **as-is**. The
  2C-3B-2 module does **not** invent candidate attributes and does **not
  import private helpers** from 2C-3B-1; it consumes the public resolver /
  inventory surface and preserves the ordering the frozen layers already
  produce.
- `WildcardAnalysis.contexts` ordered by the frozen resolver's context
  order (absolute: single; relative: `ContextResolution` order).
- `WildcardAnalysis.contexts[i].candidates` in that resolver candidate order.
- `WildcardAnalysis.source_assessments` is sorted by the public candidate
  key `(root.canonical, qualified_name, file_path, name_components)` after
  frozen structural deduplication; input tuple order is not output order.
- **Structural-identity duplicates** are deduplicated by the frozen
  **resolver** (its existing deterministic consolidation); **conflicting
  substantive duplicates** raise the resolver's `ValueError` — both behaviors
  are inherited, not re-implemented.
- **Extra `target_sources` keys not in the inventory** are rejected
  (`ValueError`) per §9 — not silently resolved.
- **All `Mapping` entries are validated before routing**, including any
  keys that ultimately resolve out-of-scope.
- `result.record is record` — identity preserved.
- **Permutation invariance:** for the **same record identity** and
  **equivalent valid evidence**, result ordering and evidence are equal under
  inventory-insertion and `target_sources`-insertion permutation (output is
  canonical regardless of input ordering).

## 11. Non-star and malformed-star record handling (single PROPOSED decision)

The following are the **single PROPOSED** behavior set (no alternative result
shapes are retained):

- **Well-typed non-star record** (e.g. `from m import a, b`, or a
  correctly-typed import whose names do not include a lone `*`): **returned**
  as a root result `WildcardAnalysis(outcome=WR_NOT_IN_SCOPE,
  reason=WR_INVALID_RECORD, contexts=(), target_kind=TARGET_INDETERMINATE)`.
  This is a valid, well-typed input outside this API's star scope — not an error.
- **Malformed star** (a `*` with an alias, a `*` mixed with other names, or a
  star under a wrong `kind`): **`ValueError` raised**.
- **Wrong Python types** on any argument: **`TypeError` raised**.
- All `target_sources` mapping entries are validated **before** routing
  (even out-of-scope keys); key-not-in-inventory → `ValueError` (§9).

## 12. PROPOSED acceptance test matrix (W1–W24)

| # | Setup | Expected (dominant) |
|---|-------|----------|
| W1 | `__all__ = ("a", "b")` direct | `all_state=ALL_DECLARED`; `all_entries==("a","b")`; `star_names==("a","b")`; `WR_ALL_DECLARED_APPLIED`; root/`candidate` `outcome=WR_ANALYZED` |
| W2 | `__all__ = []` (empty list literal) | `ALL_DECLARED`; `all_entries==()`; `star_names==()`; `WR_ANALYZED` |
| W3 | `__all__ = ["a"] + ["b"]` (BinOp) | `ALL_UNKNOWN`; no evidence; `WR_ALL_UNKNOWN_NO_EVIDENCE`; `WR_NOT_ANALYZED` |
| W4 | `__all__ = ("a",)` then `__all__ = ("b",)` (reassignment) | `ALL_UNKNOWN`; no evidence |
| W5 | `__all__.append("x")` | `ALL_UNKNOWN`; no evidence |
| W6 | `x = __all__; x.append("x")` (alias escape) | `ALL_UNKNOWN`; no evidence (dynamic-namespace invalidation) |
| W7 | `del __all__` after assignment | `ALL_UNKNOWN`; no evidence |
| W8 | `__all__ = ("a",) if c else ("b",)` (both branches literal conditional) | `ALL_UNKNOWN`; no evidence (conditional branch = execution, not inferable) |
| W9 | `__all__ = [n for n in names]` | `ALL_UNKNOWN`; no evidence |
| W10 | `__all__ = names` (bare name) | `ALL_UNKNOWN`; no evidence |
| W11 | No `__all__`; module-body `a=1`, `_p=2`, `def f()`, `class C` | `ALL_ABSENT`; default rule; `star_names` ⊇ `{a,f,C}` (sorted), excludes `_p`; bodies of `C`/`f` excluded; `WR_ALL_ABSENT_DEFAULT_APPLIED`; `WR_ANALYZED` |
| W12 | No `__all__`; `import os.path`, `import os as o`, `from m import x as y` | `star_names` ⊇ `{os,o,y}`; excludes `path`,`x`,`m` |
| W13 | `ALL_ABSENT` default with `exec("a=1")` in module body | **dynamic manipulation → `ALL_UNKNOWN`** / `WR_NOT_ANALYZED`, **no evidence** (exec invalidates default certainty) |
| W14 | Two supported candidates (AMBIGUOUS, D7) | Two `WildcardCandidateEvidence` inside `candidates`, canonical order; not merged |
| W15 | Unsupported target candidate | Preserve unsupported partition; emit its candidate row with `WR_NOT_ANALYZED`, `WR_TARGET_UNSUPPORTED`, `ALL_UNKNOWN`, empty tuples and `default_scope=None`; never parse or emit unsupported names. |
| W16 | A candidate in `SRC_UNPARSEABLE` | that candidate `ALL_UNKNOWN`, `WR_SOURCE_UNPARSEABLE_NO_EVIDENCE`; other candidates analyzed normally; no salvage |
| W17 | `TargetSource` state-table violation (e.g. `SRC_SUPPLIED` with `parse_error` set) | `TypeError` raised before routing |
| W18 | Relative star import with frozen `REL_MISSING_SOURCE_CONTEXT` pre-fail | `issue` preserved; `contexts=()`; root `WR_NOT_ANALYZED` |
| W19 | `__all__` with **literal duplicates**, a **`_private`** entry, an **empty string** `""`, and a name with **no actual binding** | all four occurrences **retained** in `all_entries`/declared `star_names` in declaration order (dupes + dunder + empty-string kept); private/empty-string/missing-binding are *declarations*, never asserted as bound; `WR_ALL_DECLARED_APPLIED` |
| W20 | **Module-executing conditional** `__all__` (e.g. `if x: __all__ = ("a",)`) | `ALL_UNKNOWN`; no evidence (execution not inferable — overapproximate, never infer) |
| W21 | `__all__` with **subscript**, **augassign** (`__all__ += [...]`), or an **invalid element** (non-str / `ast.Name`) | `ALL_UNKNOWN`; no evidence |
| W22 | **Non-star** well-typed record + a **malformed star** + **bad types** (validation order) | non-star → root `WR_NOT_IN_SCOPE`/`WR_INVALID_RECORD`,`contexts=()`,`TARGET_INDETERMINATE`; malformed star → `ValueError`; wrong types → `TypeError`; mapping validated before routing |
| W23 | **Inventory / `target_sources` permutation**, **duplicate structural identities**, **overlapping relative roots**, mix of **supported + unsupported** | permutation-invariant normalized output for same record/evidence; resolver dedupes structural dups, conflicting → `ValueError`; overlapping relative roots give one context per `ContextResolution`; supported get evidence, unsupported get none |
| W24 | Purity, immutability, incomplete defaults, and regressions | No I/O; immutable results; `if flag: a=1` yields bounded direct-only evidence with `default_scope=INCOMPLETE`, nested locals ignored; `a=1; del a` retains syntactic `a` with INCOMPLETE; a module star re-export is UNKNOWN. Run focused and full regressions after implementation. Historical 752 is not a result from this task. |

**Regression commands (PROPOSED, not to be run in this docs-only task):**
- Focused: `python -m pytest tests/test_wildcard_import_analysis.py -q`
- Full: `python -m pytest tests/ -q` (existing 752 suite plus W1–W24)
Both must pass; existing 752 must remain green (frozen regression).

## 13. Future guard & workflow preparation (PROPOSED — part of the eventual approved phase scope)

Per the standing rules in [../AGENTS.md](../AGENTS.md) and
[phase-workflow.md](phase-workflow.md) (§"Before each newly approved phase or
scope revision"), guard preparation and plan maintenance are **routine duties
automatically included in the eventual approved 2C-3B-2 phase scope** — not
separate per-step reminders. Under those rules and before any 2C-3B-2
implementation, the following are done as part of preparing that approved
scope (all **PROPOSED for approval; NOT modified now**):

1. A scoped **2C-3B-2 Stop guard** enforcing the implementation allowlist in
   §3 (`src/repo_intelligence/wildcard_import_analysis.py` +
   `tests/test_wildcard_import_analysis.py`). **Not modified now**; the
   existing 2C-3B-1 guard is preserved byte-for-byte.
2. **Root `AGENTS.md`** phase-2c-3B-2 section (scope + two-file allowlist).
   *Not modified now.*
3. **Hook guide append** (`docs/openhands-stop-hooks.md`) — 2C-3B-2 scope,
   W1–W24 acceptance references, and the two-file allowlist. *Not modified now.*
4. **External backup** of the existing hook configuration (outside the
   repository). *Not performed in this task.*
5. **Isolated validation** of deny-once, continuation, and pass cases with
   **bounded external retry state** (single-denial budget; operational
   failures → inconclusive diagnostic). *Not performed in this task.*
6. **Handoff to a fresh implementation conversation** that has loaded the
   new hook. *Not performed in this task.*

**Guards enforce only the checks they can actually verify** (static
presence/API/syntax at most); they do NOT assert purity, immutability,
semantic coverage, or regression success, and do NOT assert that the old
2C-3B-1 guard enforces the new allowlist (that is not claimed here).

**Single authorization gate:** the user's approval of 2C-3B-2 is ONE
explicit phase gate (scope + acceptance); the above setup items are covered
by that approval, not re-approved per step.

**Workflow-preparation file allowlist** (distinct from the §3 implementation
allowlist; all "proposed, unchanged NOW"): `AGENTS.md` (phase-2c-3B-2
append), `docs/openhands-stop-hooks.md`, `.openhands/hooks.json` (append),
`.openhands/phase2c3b2_stop_guard.py` (new), and an out-of-tree backup
directory. **None of these are touched in this documentation-only task.**

## 14. Risks, open decisions (single PROPOSED decisions — no alternative shapes retained)

The open items below are resolved to a single PROPOSED decision each (see
§7, §9, §10, §11); alternatives are no longer presented.

| # | Topic | Single PROPOSED decision |
|---|---|---|
| R-1 | Non-star vs. malformed star surface | Well-typed **non-star** → root `WR_NOT_IN_SCOPE`/`WR_INVALID_RECORD`,`contexts=()`; **malformed star** → `ValueError` |
| R-2 | `SRC_SUPPLIED` parse failure | Candidate → `SRC_UNPARSEABLE`/`ALL_UNKNOWN`/`WR_SOURCE_UNPARSEABLE_NO_EVIDENCE`; parse is the analyzer's duty; **no salvage** |
| R-3 | Conflicting duplicates | **Propagate the frozen resolver's `ValueError`** (inherited) |
| R-4 | Declaration ⇄ binding relationship | **Do not cross-check**; binding evidence explicitly unestablished this phase (declarations retained) |
| R-5 | `import a.b.c` in star default | Binds `a` only; `*` is never a concrete name (R-7) |
| R-6 | `ALL_UNKNOWN` vs. partial salvage | **No salvage** (conservative) |
| R-7 | Module-executing `from m import *` inside a candidate | `ALL_UNKNOWN`, no evidence or recursive expansion, because the imported namespace can affect `__all__`; never emit a literal `*`. |
| R-8 | Extra `target_sources` key not in inventory | **`ValueError`** (explicitly proposed; not silently inherited) |

## 15. Approval checklist (user completion, not agent-asserted)

- [X ] §3 scope/exclusions and the two-file PROPOSED allowlist reviewed and approved.
- [X ] §4 `analyze_wildcard_import` signature and the inheritance of `TargetSource` / `TargetContext` / `SourceAssessment` reviewed and approved.
- [X ] §5 result contract reviewed and approved as immutable / frozen; cardinality (one context per resolver context, candidates inside) approved.
- [X ] §6 closed `__all__` grammar reviewed and approved; rejection of `BinOp`, `IfExp`, comprehension, name/call, mutation, alias-escape, conditional, `del`, multi-assignment approved.
- [X ] §7 code constants reviewed and approved (no undefined `WOK`, no dangling references).
- [X ] §8 star-default rule, the `*`-exclusion rule, and the no-runtime-completeness position reviewed and approved.
- [X ] §8 *"declaration ≠ binding"* position approved (no cross-check this phase).
- [X ] §9 unsupported/unknown/error table and §10 ordering/identity reviewed and approved.
- [X ] §11 non-star / malformed record return decision (R-1) resolved.
- [X ] §12 W1–W24 acceptance test matrix approved.
- [X ] §13 workflow-preparation file allowlist accepted as **separate** from the implementation allowlist (§3); all items in §13 marked "not modified now" confirmed.
- [X ] §14 risk / decision table (R-1 – R-8) resolved.
- [X ] Explicit approval recorded with user identity, conversation id, timestamp, branch/HEAD at approval, and (if any) commit/PR reference in the phase record + PLAN.md.

---

*This document is a proposal. Nothing in it authorizes implementation.
All new API names, evidence types, reason codes, the `__all__` grammar, the
star-default rules, and the two-file PROPOSED allowlist remain PROPOSED and
subject to independent Codex review and explicit user acceptance before any
coding begins on 2C-3B-2.*


## 16. Preparation and verification record

OpenHands performed repository reconnaissance, drafted the architecture,
updated PLAN.md and ran documentation scope/whitespace checks. Codex
reviewed design consistency and made final editorial corrections. This
is a design review for user approval; implementation correctness, focused
tests, full regressions, independent implementation review, acceptance and
merge are future gates. No implementation or test execution is claimed.
