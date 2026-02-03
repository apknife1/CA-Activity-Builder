# CA Activity Builder – Rolling Checklist

This checklist tracks active and near-term development work for the CA Activity Builder project.

* Derived from TECH_DEBT.md
* Intentionally short and opinionated
* Updated continuously as work progresses
* Serves as the starting point for each dev session

TECH_DEBT.md is the long-term ledger.
CHECKLIST.md is the execution queue.

---

## NOW (actively working on)

### CL-001 — UI_STATE binding gate in ActivityEditor (HIGH)

Origin: TD-010
Risk: Silent misconfiguration if sidebar is bound to the wrong field

Goal
Never apply field properties unless the properties sidebar is proven to belong to the expected field.

Definition of Done

* Sidebar binding is verified before any property write
* Recovery path exists:

  * re-open sidebar for the field, or
  * skip field safely
* No property writes occur when UI_STATE mismatch is detected
* At least 5 multi-spec runs with zero mis-bound property writes

Evidence / Proof

* Log tag: UI_STATE
* Recovery counter visible in logs
* Run notes recorded

Touchpoints

* ActivityEditor.set_field_properties
* _open_props_frame_with_retry
* debug_ui_state

---

## NEXT (queued, ready to pick up)

### CL-002 — Phantom add reduction (not just recovery)

Origin: TD-020
Risk: Runtime bloat and instability under load

Goal
Reduce the number of phantom adds by addressing root causes, not just recovering.

Likely tactics

* Clear focus before drag
* Detect and handle covered dropzones
* Re-resolve dropzone before drag if needed

Definition of Done

* Reduced hard resync count in large IE/AR runs
* Fewer phantom-timeout recoveries in logs
* No regression in correctness

---

### CL-003 — Repo hygiene and artefact management

Origin: TD-041
Risk: Noise, confusion, accidental commits

Goal
Move run artefacts out of repo root and clarify what is transient versus persistent.

Definition of Done

* Logs and JSON dumps moved to /runs or /out
* .gitignore updated accordingly
* Repo root is clean after a normal run

---

## LATER (design or investigation)

### CL-004 — TableCellConfig cell_type implementation

Origin: TD-031

Goal
Support per-cell typing (heading, checkbox, text, etc.) beyond column-level control.

Notes

* Not blocking current AR functionality
* Needed for richer mapping tables

---

### CL-005 — Locked template or revision workflow

Origin: TD-032

Goal
Handle existing activities intelligently:

* Edit-only if editable
* Create new revision if locked
* Skip otherwise

Notes

* Requires policy decisions
* Needs careful verification logic

---

## DONE (recent)

### CL-000 — Defaults and capability enforcement refactor

Origin: TD-010, TD-011

Completed

* Unified defaults system in SpecReader
* Explicit override support
* Capability-aware property setting in ActivityEditor
* Removal of ad-hoc visibility dictionaries
* Noise reduction in logs

Evidence

* _get_field_defaults and _inject_defaults
* Editor capability gating
* Successful multi-spec runs including AR

---

## Usage notes

* At the start of a dev session, pick one item from NOW
* New ideas go into TECH_DEBT.md, not here
* Only promote items to CHECKLIST when they are actionable
* Items move to DONE only with proof (logs, runs, commits)
