# CA Activity Builder - Rolling Checklist

Short, actionable execution queue. Keep this list small and current. Move new ideas into `TECH_DEBT.md`.

---

## NOW (actively working on)

### CL-001 - UI_STATE binding gate in ActivityEditor (HIGH)

Origin: TD-010
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

Touchpoints

- `ActivityEditor.set_field_properties`
- `_open_props_frame_with_retry`
- `debug_ui_state`

---

## NEXT (queued, ready to pick up)

### CL-006 - Sidebar ensure cadence / caching decision

Origin: TD-022
Risk: repeated sidebar ensures add overhead per field

Goal
Decide whether caching sidebar visibility / active tab state is safe and beneficial.

Definition of Done

- Clear decision documented (keep conservative, or add caching with proof)
- If caching: explicit invalidation and proof gates defined
- If no caching: rationale recorded for future reconsideration

---

### CL-008 - Complete add_field_from_spec flow review

Origin: TD-024

Goal
Finish the attempt-loop review and identify any additional safe optimizations.

Definition of Done

- Attempt-loop steps audited for redundant waits
- Any safe optimizations implemented or explicitly rejected (with rationale)
- Notes captured for follow-up if blocked by larger reset/flow work

---

### CL-003 - Repo hygiene and artefact management

Origin: TD-041
Status: partial (`runs/codex` added; consolidation pending)

Goal
Move run artefacts out of repo root and clarify what is transient versus persistent.

Definition of Done

- Logs and JSON dumps moved to `/runs` or `/out`
- `.gitignore` updated accordingly
- Repo root is clean after a normal run

---

### CL-002 - Phantom add reduction (not just recovery)

Origin: TD-020
Risk: runtime bloat and instability under load

Goal
Reduce the number of phantom adds by addressing root causes, not just recovering.

Definition of Done

- Reduced hard resync count in large IE/AR runs
- Fewer phantom-timeout recoveries in logs
- No regression in correctness

---

## LATER (design or investigation)

### CL-009 - Intermittent missing table headers in runs

Origin: TD-025
Risk: table outputs incomplete (header rows missing)

Goal
Investigate occasional missing table headers/values during field configuration runs.

Definition of Done

- Repro steps captured with run id(s)
- Root cause identified (UI timing vs config/data)
- Fix implemented or documented workaround

---

### CL-007 - Field settings panel retention between fields

Origin: TD-023
Risk: repeated sidebar toggles increase run time

Goal
Keep field settings open between sequential fields when safe, without breaking UI proof guarantees.

Definition of Done

- Decision documented (retain open or keep current close behavior)
- If retaining open: define safe UI reset that preserves binding proof
- Reduced `fields_sidebar` counter in runs without increased UI_STATE errors

---

### CL-004 - TableCellConfig cell_type implementation

Origin: TD-031

Goal
Support per-cell typing (heading, checkbox, text, etc.) beyond column-level control.

---

### CL-005 - Locked template or revision workflow

Origin: TD-032

Goal
Handle existing activities intelligently: edit if editable, create revision if locked, or skip otherwise.
