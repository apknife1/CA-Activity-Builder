# CA Activity Builder - Rolling Checklist

Short, actionable execution queue. Keep this list small and current. Move new ideas into `TECH_DEBT.md`.

---

## NOW (actively working on)

### CL-002 - Phantom add reduction (not just recovery)

Origin: TD-020
Priority: P0 (from TD-020)
Status: partial (recovery paths and counters are in place; occurrence still present)
Risk: runtime bloat and instability under load

Goal
Reduce the number of phantom adds by addressing root causes, not just recovering.

Definition of Done

- Reduced hard resync count in large IE/AR runs
- Fewer phantom-timeout recoveries in logs
- No regression in correctness

---

### CL-001 - UI_STATE binding gate in ActivityEditor (HIGH)

Origin: TD-010
Priority: P0 (from TD-010)
Status: partial (binding proofs + instrumentation added)

Goal
Never apply field properties unless the sidebar is proven to belong to the expected field.

Definition of Done

- Sidebar binding verified before any property write
- Recovery path exists (re-open sidebar or skip field safely)
- No property writes occur when UI_STATE mismatch is detected
- At least 5 multi-spec runs with zero mis-bound property writes
- Binding proof/mismatch counters visible in logs

Evidence / Proof

- Log tag: UISTATE
- Binding proof/mismatch counters present
- Run notes recorded
- Recent run scan: 27 `ActivityStatus.OK` runs with 0 `UI_STATE mismatch` entries (still using single-spec-heavy validation)

Touchpoints

- `ActivityEditor.set_field_properties`
- `_open_props_frame_with_retry`
- `debug_ui_state`

---

## NEXT (queued, ready to pick up)

### CL-008 - Complete add_field_from_spec flow review

Origin: TD-024
Priority: P1 (from TD-024)
Status: partial (major wait reductions landed; final pass pending)

Goal
Finish the attempt-loop review and identify any additional safe optimizations.

Definition of Done

- Attempt-loop steps audited for redundant waits
- Any safe optimizations implemented or explicitly rejected (with rationale)
- Notes captured for follow-up if blocked by larger reset/flow work

Progress

- Attempt loop and table-visible flow were tightened.
- Remaining work is final review pass after latest table/section optimizations.

---

### CL-003 - Repo hygiene and artefact management

Origin: TD-041
Priority: P2 (from TD-041)
Status: partial (`runs/codex` added; consolidation pending)

Goal
Move run artefacts out of repo root and clarify what is transient versus persistent.

Definition of Done

- Logs and JSON dumps moved to `/runs` or `/out`
- `.gitignore` updated accordingly
- Repo root is clean after a normal run

---

### CL-006 - Sidebar ensure cadence / caching decision

Origin: TD-022
Priority: P1 (from TD-022)
Status: partial (conservative decision retained; fastpath improvements in place)
Risk: repeated sidebar ensures add overhead per field

Goal
Decide whether caching sidebar visibility / active tab state is safe and beneficial.

Definition of Done

- Clear decision documented (keep conservative, or add caching with proof)
- If caching: explicit invalidation and proof gates defined
- If no caching: rationale recorded for future reconsideration

Progress

- Current stance remains conservative (no broad sidebar state cache yet).
- Fastpath for `Add new field` from field-settings tab is implemented and active in logs.

---

## LATER (design or investigation)

### CL-007 - Field settings panel retention between fields

Origin: TD-023
Priority: P1 (from TD-023)
Status: partial (field-settings fastpath implemented)
Risk: repeated sidebar toggles increase run time

Goal
Keep field settings open between sequential fields when safe, without breaking UI proof guarantees.

Definition of Done

- Decision documented (retain open or keep current close behavior)
- If retaining open: define safe UI reset that preserves binding proof
- Reduced `fields_sidebar` counter in runs without increased UI_STATE errors

Progress

- Post-config sidebar state is instrumented.
- Add-new-field from field-settings fastpath is used when available; explicit full retention policy still to be finalized.

---

### CL-009 - Intermittent missing table headers in runs

Origin: TD-025
Priority: P1 (from TD-025)
Status: partial (post-props table probe + one-shot recovery implemented)
Risk: table outputs incomplete (header rows missing)

Goal
Investigate occasional missing table headers/values during field configuration runs.

Definition of Done

- Repro steps captured with run id(s)
- Root cause identified (UI timing vs config/data)
- Fix implemented or documented workaround

Recent Evidence

- `runs/20260212_162223`: post-props probe detected a missing header, re-applied table config once, and verified restore (`table_probe post-props` -> `post-props-recover`).
- Build completed with no skipped fields after recovery.

---

### CL-004 - TableCellConfig cell_type implementation

Origin: TD-031
Priority: P3 (from TD-031)

Goal
Support per-cell typing (heading, checkbox, text, etc.) beyond column-level control.

---

### CL-005 - Locked template or revision workflow

Origin: TD-032
Priority: P3 (from TD-032)

Goal
Handle existing activities intelligently: edit if editable, create revision if locked, or skip otherwise.
