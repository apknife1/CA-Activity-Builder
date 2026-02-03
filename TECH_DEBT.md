# CA Activity Builder – Tech Debt Ledger

This is a living ledger of known issues, TODOs, and refactor targets discovered in the current codebase snapshot.

## Related docs

- [README](README.md)
- [Architecture](ARCHITECTURE.md)
- [Field capability matrix](FIELD_CAPABILITIES.md)

---

## 0. High Priority – Must Fix

### TD-001 — `spec_reader.py` contains invalid f-string syntax

**File:** `src/ca_bldr/spec_reader.py` (around line ~603)

Current code uses an f-string expression containing a backslash escape (e.g. `b.replace('\n', '<br>')`) which is invalid Python syntax and can prevent the module from importing.

**Fix:** compute the replacement outside the f-string, or use `.format()`.

---

## 1. Correctness / Safety

### TD-010 — Properties sidebar binding (UI_STATE mismatch risk)

**Symptom:** Properties frame can be present but bound to the wrong field.

**Goal:** make configuration changes only after proving the sidebar is bound to the expected `field_id`.

**Approach:**

- Gate all property writes behind a `prove_sidebar_bound(field_id)` check.
- If mismatch, perform one deterministic recovery:
  1) click field root
  2) open sidebar (or refresh it)
  3) re-check binding
- If still mismatched, skip/abort (policy-dependent) rather than guessing.

---

### TD-011 — Field capability rules are not encoded

Example:

- Paragraph fields do not support assessor `update`.

**Fix:** introduce a capability matrix per field type/config and skip unsupported controls silently.

See: [FIELD_CAPABILITIES.md](FIELD_CAPABILITIES.md)

---

## 2. Stability

### TD-020 — Phantom adds (drag succeeds but field not detected)

**Status:** recovery exists and works (ID-diff + last-element fallback + hard resync budget).

**Next work:** reduce occurrence by improving dropzone interactions:

- if drop diagnostics show `covered=True`, perform a focus-clear + re-resolve dropzone pass before dragging.

---

### TD-021 — Turbo re-renders causing stale element references

**Status:** random stales are acceptable when retries succeed.

**Rule:** consistent stales indicate missing proof gates.

---

## 3. Feature Completeness

### TD-030 — `navigation.py` is a stub

**File:** `src/ca_bldr/navigation.py`

- contains TODO + `NotImplementedError`
- not referenced elsewhere

**Decision needed:**

- remove it for now, OR
- implement and integrate into CASession navigation helpers.

---

### TD-031 — Table cell override `cell_type` handling is TODO

**File:** `src/ca_bldr/activity_editor.py`

`TableCellConfig.cell_type` is parsed but not yet applied.

---

### TD-032 — Locked template / revision workflow

Templates assigned to learners become locked.

**Future:**

- detect locked state
- create new revision
- apply edits to revision

---

## 4. Documentation / Maintenance

### TD-040 — README / docs drift

**Status:** README and ARCHITECTURE docs are being refreshed.

**Next:** add:

- field capability notes
- known limitations
- troubleshooting section

---

### TD-041 — Run artefacts in repo root

Files like `act_*_log.json`, `ca_activity_builder.log`, and registry snapshots should be moved to `/runs/` (or `/out/`) and ignored by git.

---

## 5. Refactor Targets

### TD-050 — Selector consolidation

Goal: selectors live in `config.py` under feature groups:

- `table`
- `single_choice`
- `properties`
- `sections`

Avoid hardcoded selectors inside editor/builder helpers unless they are truly ephemeral.

---

## 6. Tracking

Suggested prefix convention:

- `TODO(ANDREW): ...`
- `NOTE(ANDREW): ...`
- `DEBT(ANDREW): ...`

So future scanning can generate this ledger automatically.
