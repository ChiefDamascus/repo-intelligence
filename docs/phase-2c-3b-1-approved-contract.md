# Phase 2C-3B-1 — Approved Contract & Recovery Record

**Status:** Implemented and independently reviewed. **INDEPENDENT CODEX REVIEW PASSED** (2026-10-19): production + tests PASS against A01–A23, corrected D1–D17 and F1–F6; 51 focused / 752 full passing plus 24 independent probes + 5 supplemental checks at verified source `b72db890…` / test `df5fa655…`; no blocking code/test findings. **USER ACCEPTANCE PENDING** — acceptance, merge, freeze, and 2C-3B-2 start remain explicit user decisions.

**Scope (exact authorized writes):**
- PRODUCTION (new): `src/repo_intelligence/imported_name_analysis.py`
- TEST (new): `tests/test_imported_name_analysis.py`
- DOCUMENTATION (standing maintenance): `PLAN.md` (targeted) and this file.

All other production and the 701 pre-existing regression tests remain **frozen** and unchanged (full suite still green).

---

## 1. Public API

`analyze_named_import(record, inventory, target_sources) -> ImportNameAnalysis`

- **Pure, deterministic, conservative.** No filesystem, database, network, or runtime import execution. `result.record is record` (identity preserved).
- `record`: immutable `ImportRecord` (`python_ast`).
- `inventory`: a tuple/sequence of the frozen `ModuleCandidate` values returned by `build_module_inventory` (there is no separate `ModuleInventory` public class; the candidates are the value objects). The input is not mutated; results use frozen canonical ordering (permutation invariant).
- `target_sources`: **required** `Mapping[ModuleCandidate, TargetSource]` (`None` → `TypeError`). Missing key ⇒ synthetic `SRC_NOT_FOUND`.
- Consumes the **frozen** absolute/relative resolver APIs without altering their semantics; preserves their statuses, reasons, partitions, provenance.

## 2. Result types (all frozen / immutable)

| Type | Key fields |
|------|------------|
| `ImportNameAnalysis` | `record`, `target_kind` (`absolute`/`relative`/`mixed_routing`), `issue`, `contexts` (tuple, ordered by `(context_index, name_index)`) |
| `TargetNameAnalysis` | `context_index`, `name_index`, `context` (TargetContext), `name` (NameEvidence) |
| `TargetContext` | `kind`, `source_candidate` (or None), `base_module`, `base_components`, `target_components` (optional), `lossless`, `status`, `reason`, `supported_candidates`, `unsupported_candidates`, `assessments` (tuple) |
| `NameEvidence` | `position`, `name`, `asname`, `outcome`, `reason`, `submodule` (tuple), `definitions` (tuple), `reexports` (tuple) |
| `SubmoduleNameEvidence` | `candidate` (PARENT), `submodule_candidate` (CHILD), `component`, `component_supported`, `name_supported`, `source='structural'` |
| `DefinitionNameEvidence` | `candidate`, `kind` (`module_attribute`/`module_class`/`module_function`/`module_async_function`), `symbol_kind`, `name`, `line`, `provenance` |
| `ReexportNameEvidence` | `candidate`, `kind` (`module_import`), `symbol_kind` (`import`/`from_import`), `name`, `asname` (source-side alias), `line`, `provenance` |
| `SourceAssessment` | `candidate`, `state` (SRC_*), `parse_diagnostic` |
| `TargetSource` | `provenance` (the state enum — required), `source_text`, `parse_error` (validated state table) |

**Cardinality:** exactly one `TargetNameAnalysis` per resolver-context × original `record.names` occurrence (~3D evidence model). Duplicate names, original indices and aliases preserved. Candidate multiplicity stays **inside** the unit tuple — never converted into new source contexts, never merged across roots.

**Alias distinction:** the *requested* alias (from the import occurrence) is `NameEvidence.asname`; the *source-statement* alias (from `import … as z` / `from … import y as z`) is carried on the re-export evidence as `ReexportNameEvidence.asname`. The two are independent and must not be conflated.

## 3. Source states

`SRC_SUPPLIED` (str text, err None) · `SRC_UNAVAILABLE` (None, None) · `SRC_UNPARSEABLE` (str text, str diagnostic — empty permitted) · `SRC_NOT_FOUND` (key absent, synthesized). An empty definition/re-export tuple must **never** silently imply the source was analyzed — the candidate-scoped `SourceAssessment` records state even when evidence tuples are empty. Malformed container/value/state ⇒ deliberate `TypeError`; a key not identifying an inventory candidate ⇒ `TypeError`.

## 4. Outcomes / reasons

- Outcomes: `OUTCOME_ANALYZED` · `OUTCOME_NOT_ANALYZED` · `OUTCOME_NOT_IN_SCOPE`.
- Reasons: `NAME_OK` · `NAME_IMPORT_FORM` · `NAME_TARGET_NO_CANDIDATE` · `NAME_TARGET_UNSUPPORTED` · `NAME_TARGET_NOT_ATTEMPTED` · `NAME_INVALID_KIND` · `NAME_OK_NO_EVIDENCE`.
- `NAME_OK` = static analysis completed, **not** a confirmed runtime binding.

## 5. F1–F6 (governing binding corrections)

- **F1 — Package eligibility:** structural child requires an *individual supported package-initializer* parent; child matched by exact `name_components == parent + (name,)`, same canonical root, case-sensitive. An ordinary-module **parent** (a plain `.py`) never qualifies; an index entry with **no initializer** (e.g., an unindexed namespace package) supplies no parent candidate and is never fabricated. Namespace-package metadata on an otherwise-eligible **child** does not suppress its structural evidence (D11). Namespace limitations are documented: a namespace package contributes children only where its parent has an indexed supported initializer.
- **F2 — Source independence:** source-derived and structural evidence independent; immutable candidate-scoped `SourceAssessment` observable even with empty evidence tuples (applies to D1/D10/D12).
- **F3 — Assignment evidence:** bounded AST pass for unambiguous module-scope `Assign`/`AnnAssign` (`AnnAssign` requires `value is not None`); direct class/function/async preserved; no runtime-value inference.
- **F4 — D6 correction:** a valid structural child is **not** suppressed by a nested-import-only source; nested import never yields module-scope re-export.
- **F5 — invalid-kind/empty-names:** absolute invalid-kind represented by the frozen NOT_ATTEMPTED context + original reason (no simultaneous empty-contexts); empty `record.names` rejected deterministically.
- **F6 — tests authorized:** the prohibition is on modifying frozen production/regression files, not on this new test file.

## 6. Re-export scope codes

`RS_MODULE_DIRECT` (module body) · `RS_MODULE_CONDITIONAL` (top-level `if`/`while` etc.) · class-body / function-body / unknown (NOT module re-exports). Alias binding rules: `import x`→x; `import x.y`→x (unless `as z`); `from x import y`→y; `from x import y as z`→z. No wildcard (`*`) name inference; no transitive re-export resolution.

## 7. Corrected named-import matrix D1–D17 (dominant outcome)

| # | Setup (abbr) | Context | outcome | reason | submodule | defs | reexports |
|---|------|---------|---------|--------|-----------|------|-----------|
| D1 | pkg pkg-init + child, no source | RESOLVED | ANALYZED | NAME_OK | 1 | ∅ | ∅ |
| D2 | same + `sub = 1` pkg init | RESOLVED | ANALYZED | NAME_OK | 1 | 1 (module_attribute) | ∅ |
| D3 | `sub` only inside `def f()` | RESOLVED | ANALYZED | NAME_OK | 1 | ∅ | ∅ |
| D4 | `from . import sub` module-scope | RESOLVED | ANALYZED | NAME_OK | 1 | ∅ | 1 (RS_MODULE_DIRECT) |
| D5 | `from . import sub` under top `if` | RESOLVED | ANALYZED | NAME_OK | 1 | ∅ | 1 (RS_MODULE_CONDITIONAL) |
| D6 | `from . import sub` inside `def f()` | RESOLVED | ANALYZED | NAME_OK | 1 (child, F4) | ∅ | ∅ |
| D7 | two supported roots (AMBIGUOUS) | MULTI | ANALYZED | NAME_OK | per-candidate | per-candidate | per-candidate |
| D8 | `from pkg import sub` where pkg source is `import os.path` (dotted) | RESOLVED | ANALYZED | NAME_OK | ∅ | ∅ | ∅ (dotted `import os.path` never re-exports `sub`) |
| D9 | `from pkg import sub as s` (as D4) | RESOLVED | ANALYZED | NAME_OK | per D4 | ∅ | 1 (matches `sub`) |
| D10 | key absent → SRC_NOT_FOUND | RESOLVED | ANALYZED | NAME_OK | 1 (rule S) | ∅ | ∅ |
| D11 | child namespace_package | RESOLVED | ANALYZED | NAME_OK | 1 (child) | per source | ∅ |
| D12 | pkg UNPARSEABLE | RESOLVED | ANALYZED | NAME_OK | 1 (rule S) | ∅ | ∅ |
| D13 | pkg UNSUPPORTED | UNSUPPORTED | NOT_ANALYZED | NAME_TARGET_UNSUPPORTED | ∅ | ∅ | ∅ |
| D14 | pkg UNRESOLVED | UNRESOLVED | NOT_ANALYZED | NAME_TARGET_NO_CANDIDATE | ∅ | ∅ | ∅ |
| D15 | `import pkg.sub` (kind="import") | ANY | NOT_IN_SCOPE | NAME_IMPORT_FORM | ∅ | ∅ | ∅ |
| D16 | relative, single context | RESOLVED | ANALYZED | NAME_OK | per D1/context | per source | ∅ |
| D17 | relative, pre-context failure | — | issue=REL_MISSING_SOURCE_CONTEXT, contexts=() | — | — | — | — |

## 8. Hard boundaries (NOT implemented)

Wildcard/`__all__` (2C-3B-2), `analyze_wildcard_import`, `AllSpec`, DB persistence/CLI, dependency graphs, MCP/DeepWiki, runtime import execution, unrelated refactoring.

## 9. Conservative limits / documented unknowns

- The definition/assignment pass is **direct module-body only**; conditional definitions (e.g. `if x: class C`) and conditional assignments are **omitted** — module-conditional *imports* (top `if`/`while`) remain supported as `RS_MODULE_CONDITIONAL`.
- The caller owns source/snapshot correspondence; the analyzer never re-reads a file to verify the supplied text.
- No runtime binding guarantee — evidence is syntactic/structural only.

## 10. Verification evidence (this conversation)

- Focused: `python -m pytest tests/test_imported_name_analysis.py -q` → **51 passed**.
- Full: `python -m pytest tests/ -q` → **752 passed** (51 phase + 701 frozen regression).
- Independent Codex review: PASSED (24 probes + 5 supplemental checks at the verified hashes).
- Production SHA256 stable at `b72db890…`; protected 5 files hash-unchanged; branch/HEAD/index preserved; no write outside authorized scope.
- Relative-context behavior confirmed: `TargetContext.base_module is None` for relative contexts; positive-level relative invalid-kind keeps its frozen issue + `contexts=()`.

## 11. Sources

- Authorization & F1–F6 & A01–A23: `docs/openhands-stop-hooks.md`.
- Standing workflow / guard: `AGENTS.md`, `docs/phase-workflow.md`.
- Recovery conversations: `697194aa-8c8f-4fc0-af37-876d01bec90e` (A→B→C→I + user authorization) and `71d4c52b-9c97-489d-a23a-ee0c1ae407b1` (recovery authorization).
- PLAN.md current status + change log (2026-10-19 entry records implementation complete + independent review passed; prior "incomplete/absent" and "review pending" statements are historical).
