# CA Activity Builder - Tech Debt Ledger

Living ledger of known issues, TODOs, and refactor targets. Keep this list short and high-signal; move only actionable work to `CHECKLIST.md`.

## Related Docs

- `README.md`
- `ARCHITECTURE.md`
- `FIELD_CAPABILITIES.md`

---

## 0. High Priority - Must Fix

### TD-001 - `spec_reader.py` invalid f-string syntax

**Status:** open
**Symptom:** f-string uses a backslash escape (e.g. `b.replace('\n', '<br>')`) which is invalid.
**Next:** refactor the replacement outside the f-string or use `.format()`.

---

## 1. Correctness / Safety

### TD-010 - Properties sidebar binding (UI_STATE mismatch risk)

**Status:** partial
**Symptom:** properties frame can be present but bound to the wrong field.
**Next:** ensure property writes are gated by a binding proof, with one deterministic recovery path and safe skip/abort on mismatch.

### TD-011 - Field capability rules not encoded

**Status:** open
**Symptom:** editor may attempt unsupported operations (example: assessor update on paragraph).
**Next:** keep capability gating in `ActivityEditor` aligned with `FIELD_CAPABILITIES.md`.

---

## 2. Stability

### TD-020 - Phantom adds (drag succeeds but field not detected)

**Status:** open
**Symptom:** phantom adds require recovery paths and increase run time.
**Next:** reduce occurrence by improving dropzone interactions (focus clear, covered dropzone handling, re-resolve before drag).

### TD-021 - Turbo re-renders causing stales

**Status:** open
**Symptom:** transient stale references are common during Turbo churn.
**Next:** treat consistent stales as missing proof gates and fix the relevant flow.

### TD-022 - Sidebar ensure cadence / caching decision

**Status:** open
**Symptom:** conservative sidebar ensures add overhead per field.
**Next:** decide whether caching sidebar visibility / active tab state is safe. If yes, define proof + invalidation rules.

### TD-023 - Field settings panel closes between fields

**Status:** open
**Symptom:** post-config cleanup/defocus closes field settings, forcing a sidebar toggle before each add.
**Next:** decide whether to keep field settings open between adds and define safe UI reset behavior.

### TD-024 - Complete add_field_from_spec flow review

**Status:** open
**Symptom:** flow review started; remaining steps may still contain avoidable waits.
**Next:** finish the attempt-loop review and identify any additional safe optimizations.

### TD-025 - Intermittent missing table headers during config

**Status:** open
**Symptom:** table fields occasionally lose header values (run-specific, non-deterministic).
**Next:** capture repro run id(s), determine whether this is UI timing or config application, and implement a fix or workaround.

---

## 3. Feature Completeness

### TD-030 - `navigation.py` is a stub

**Status:** open
**Next:** remove it or implement and integrate into CASession navigation helpers.

### TD-031 - Table cell override `cell_type` handling is TODO

**Status:** open
**Next:** implement `TableCellConfig.cell_type` application in `ActivityEditor`.

### TD-032 - Locked template / revision workflow

**Status:** open
**Next:** detect locked state, create revision, and apply edits to the revision.

---

## 4. Documentation / Maintenance

### TD-040 - README / docs drift

**Status:** open
**Next:** add field capability notes, known limitations, and troubleshooting sections.

### TD-041 - Run artefacts in repo root

**Status:** partial
**Symptom:** logs/snapshots still appear in repo root.
**Next:** move artefacts into `/runs` or `/out` and update `.gitignore`. `runs/codex` exists but consolidation is pending.

---

## 5. Refactor Targets

### TD-050 - Selector consolidation

**Status:** open
**Goal:** selectors live in `config.py` under feature groups (`table`, `single_choice`, `properties`, `sections`).

---

## 6. Tracking

Suggested prefix convention:

- `TODO(ANDREW): ...`
- `NOTE(ANDREW): ...`
- `DEBT(ANDREW): ...`
