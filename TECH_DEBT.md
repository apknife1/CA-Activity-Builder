# CA Activity Builder - Tech Debt Ledger

Living ledger of known issues, TODOs, and refactor targets. Keep this list short and high-signal; move only actionable work to `CHECKLIST.md`.

## Related Docs

- `README.md`
- `ARCHITECTURE.md`
- `FIELD_CAPABILITIES.md`

## Priority Scale

- `P0`: correctness/safety risk, frequent impact, weak recovery
- `P1`: high impact, recoverable, or narrower scope
- `P2`: medium impact, maintenance/documentation/refactor debt
- `P3`: lower urgency, future feature work

---

## 0. High Priority - Must Fix

### TD-001 - `spec_reader.py` invalid f-string syntax

**Priority:** n/a (resolved)
**Status:** resolved
**Symptom:** f-string uses a backslash escape (e.g. `b.replace('\n', '<br>')`) which is invalid.
**Evidence:** `python -m py_compile src/ca_bldr/spec_reader.py` passes.
**Next:** none.

---

## 1. Correctness / Safety

### TD-010 - Properties sidebar binding (UI_STATE mismatch risk)

**Priority:** P0
**Status:** partial
**Symptom:** properties frame can be present but bound to the wrong field.
**Next:** ensure property writes are gated by a binding proof, with one deterministic recovery path and safe skip/abort on mismatch.

### TD-011 - Field capability rules not encoded

**Priority:** P2
**Status:** partial
**Symptom:** editor may attempt unsupported operations (example: assessor update on paragraph).
**Progress:** capability gating matrix exists in `ActivityEditor` (`FIELD_CAPS`) with type-specific behavior in configure flow.
**Next:** continue aligning edge cases with `FIELD_CAPABILITIES.md`.

---

## 2. Stability

### TD-020 - Phantom adds (drag succeeds but field not detected)

**Priority:** P0
**Status:** partial
**Symptom:** phantom adds require recovery paths and increase run time.
**Progress:** DOM-delta recovery and hard-resync paths are in place with instrumentation/counters.
**Next:** continue reducing occurrence rate (not just recovery path use).

### TD-021 - Turbo re-renders causing stales

**Priority:** P1
**Status:** partial
**Symptom:** transient stale references are common during Turbo churn.
**Progress:** many stale-prone flows now re-find before act and verify after act.
**Next:** keep converting remaining stale hotspots to prove -> act -> re-prove.

### TD-022 - Sidebar ensure cadence / caching decision

**Priority:** P1
**Status:** partial
**Symptom:** conservative sidebar ensures add overhead per field.
**Progress:** conservative no-cache policy retained; field-settings add-new-field fastpath implemented.
**Next:** finalize whether broader caching is safe, with explicit invalidation rules if adopted.

### TD-023 - Field settings panel closes between fields

**Priority:** P1
**Status:** partial
**Symptom:** post-config cleanup/defocus closes field settings, forcing a sidebar toggle before each add.
**Progress:** post-config sidebar state instrumentation added; fastpath from field-settings tab used when available.
**Next:** finalize retention policy and UI reset contract.

### TD-024 - Complete add_field_from_spec flow review

**Priority:** P1
**Status:** partial
**Symptom:** flow review started; remaining steps may still contain avoidable waits.
**Progress:** attempt-loop and alignment checks reviewed; multiple wait reductions implemented.
**Next:** complete final pass and document explicit keep/remove decisions for remaining waits.

### TD-025 - Intermittent missing table headers during config

**Priority:** P1
**Status:** partial
**Symptom:** table fields occasionally lose header values (run-specific, non-deterministic).
**Progress:** post-properties table persistence probe + one-shot recovery now implemented in `ActivityEditor` (headers + row labels).
**Evidence:** `runs/20260212_162223` detected missing header post-props and restored it via recovery.
**Next:** keep monitoring run logs; if recurrence remains high, tighten write/read selectors for checkbox-header cells.

---

## 3. Feature Completeness

### TD-030 - `navigation.py` is a stub

**Priority:** P3
**Status:** open
**Next:** remove it or implement and integrate into CASession navigation helpers.

### TD-031 - Table cell override `cell_type` handling is TODO

**Priority:** P3
**Status:** open
**Next:** implement `TableCellConfig.cell_type` application in `ActivityEditor`.

### TD-032 - Locked template / revision workflow

**Priority:** P3
**Status:** open
**Next:** detect locked state, create revision, and apply edits to the revision.

---

## 4. Documentation / Maintenance

### TD-040 - README / docs drift

**Priority:** P2
**Status:** partial
**Progress:** README/architecture/matrix documents were updated during instrumentation refactor.
**Next:** keep troubleshooting and capability notes synchronized with ongoing behavior changes.

### TD-041 - Run artefacts in repo root

**Priority:** P2
**Status:** partial
**Symptom:** logs/snapshots still appear in repo root.
**Next:** move artefacts into `/runs` or `/out` and update `.gitignore`. `runs/codex` exists but consolidation is pending.

---

## 5. Refactor Targets

### TD-050 - Selector consolidation

**Priority:** P2
**Status:** open
**Goal:** selectors live in `config.py` under feature groups (`table`, `single_choice`, `properties`, `sections`).

---

## 6. Tracking

Suggested prefix convention:

- `TODO(ANDREW): ...`
- `NOTE(ANDREW): ...`
- `DEBT(ANDREW): ...`
