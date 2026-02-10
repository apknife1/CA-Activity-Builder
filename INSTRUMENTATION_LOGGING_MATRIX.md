# Instrumentation & Logging Matrix

This document defines a **unified, global instrumentation and logging policy** for the CA Activity Builder project.

Its goals are to:

- Preserve **deep, verbose logs** during build/debug phases
- Provide **clean, high-signal logs** in live/production runs
- Ensure logs are **coherent, searchable, and attributable** to a specific activity / section / field
- Avoid performance penalties from uncontrolled diagnostic output

This document is intentionally **implementation-agnostic**. It describes *what* should be logged and *when*, not *how* it is coded.

---

## Core Principles

1. **Signal vs Diagnostics**
   - *Signal logs* are always meaningful and low-volume
   - *Diagnostic logs* are gated, rate-limited, and optional
2. **Context First**
   - Logs are only useful if they can be traced to a specific runtime entity
   - Every meaningful log line must include consistent context keys where available
3. **Counters Are Cheap, Dumps Are Not**
   - Counters are always on
   - Heavy probes, dumps, and DOM snapshots are opt-in
4. **Live Mode Is the Default**
   - Debug and Trace modes must be explicitly enabled

---

## Standard Log Context Keys

These keys should be included whenever the emitting function has access to them:

- `cat`  - category (NAV / SECTION / SIDEBAR / DROP / PHANTOM / FROALA / TABLE / RETRY / UISTATE / REG / STARTUP / CONFIGURE / PROPS)
- `act`  - activity_code
- `sec`  - section_id (optionally with short title)
- `fid`  - field_id
- `type` - field_type_key
- `fi`   - fi_index
- `a`    - attempt counters (`create=`, `drag=`, `stage=`, `pass=`)

> Rule: **If the function has the value, it must log it.**

---

## Categories (Cat Enum)

Current categories (must stay in sync with `instrumentation.Cat`):

- `NAV`
- `SECTION`
- `SIDEBAR`
- `DROP`
- `PHANTOM`
- `FROALA`
- `TABLE`
- `RETRY`
- `UISTATE`
- `REG`
- `STARTUP`
- `CONFIGURE`
- `PROPS`

---

## Logging Modes

### LIVE (default)

- Minimal, high-signal output
- Suitable for unattended or production runs

### DEBUG

- Adds structured per-field and per-stage visibility
- Rate-limited diagnostics

### TRACE

- Deep, forensic detail
- Intended for diagnosing stubborn Turbo / Sortable / Froala failures

### Helper Usage

- `emit_signal(...)` for signal logs (always allowed)
- `emit_diag(...)` for diagnostics (DEBUG+)
- `emit_trace(...)` for heavy diagnostics (TRACE only)

---

## Instrumentation Matrix

Legend:

- ✅ Emit by default at this level
- ⚠️ Emit only on failure / retry / slow-path
- 🔒 Never emit at this level
- 🧮 Counter always increments

| Category | Purpose | LIVE | DEBUG | TRACE |
| --- | --- | --- | --- | --- |
| NAV | Template lookup, creation, navigation | ✅ | ✅ | ✅ |
| SECTION | Section selection and canvas alignment | ⚠️ | ✅ | ✅ |
| SIDEBAR | Sidebar open / tab / pane logic | ⚠️ | ✅ | ✅ |
| DROP | Drag/drop execution and confirmation | ⚠️ | ⚠️ | ✅ |
| PHANTOM | Phantom detection and recovery | ✅ | ✅ | ✅ |
| FROALA | Body/model answer editing | ⚠️ | ⚠️ | ✅ |
| TABLE | Table configuration stages | ⚠️ | ✅ | ✅ |
| RETRY | Retry passes and outcomes | ✅ | ✅ | ✅ |
| UISTATE | Overlays, modals, UI probes | ⚠️ | ⚠️ | ✅ |
| REG | Registry updates and drift | ⚠️ | ⚠️ | ✅ |
| STARTUP | Driver and login | ✅ | ✅ | ✅ |

---

## Category-Specific Guidance

### NAV

- Signal (LIVE): Activity start/end.
- Signal (LIVE): Template locate result (found / not found, active / inactive).
- Signal (LIVE): Create template result.
- Signal (LIVE): Open builder result.
- Diagnostics (DEBUG/TRACE): URL gating checks.
- Diagnostics (DEBUG/TRACE): Search staleness proofs.
- Diagnostics (DEBUG/TRACE): Pagination attempts.

---

### SECTION

- Signal (LIVE): Section misalignment.
- Signal (LIVE): Forced reselection.
- Signal (LIVE): Hard resync start/end.
- Diagnostics (DEBUG): One line per `ensure_section_ready` call (rate-limited).
- Diagnostics (TRACE): Alignment proof source (create_field_path / turbo-frame / URL).

---

### SIDEBAR

- Signal (LIVE): Attempt >1 to open sidebar.
- Signal (LIVE): Properties binding failure.
- Diagnostics (DEBUG): Tab switches.
- Diagnostics (DEBUG): Sidebar fast-path vs fallback.
- Diagnostics (TRACE): Selector attempts.
- Diagnostics (TRACE): Binding proof detail.

---

### DROP

- Signal (LIVE): Drop failure summary.
- Signal (LIVE): JS fallback usage.
- Diagnostics (DEBUG): Per drag attempt outcome.
- Diagnostics (TRACE): Dropzone probes.
- Diagnostics (TRACE): Offsets tried.

---

### PHANTOM

- Signal (LIVE): Always signal-worthy; phantom suspected.
- Signal (LIVE): Recovery path chosen.
- Signal (LIVE): Hard resync triggered.
- Diagnostics (DEBUG/TRACE): Candidate counts.
- Diagnostics (DEBUG/TRACE): Registry vs DOM deltas.
- Diagnostics (TRACE): Snapshot dumps.

---

### FROALA

- Signal (LIVE): Body persistence failure.
- Diagnostics (DEBUG): Verification failures.
- Diagnostics (DEBUG): Reapply attempts.
- Diagnostics (TRACE): Signature diffs.
- Diagnostics (TRACE): Editor state samples.

---

### TABLE

- Signal (LIVE): Stage start/end.
- Signal (LIVE): Stage failure.
- Diagnostics (DEBUG): Stage retries and timing.
- Diagnostics (TRACE): Per-cell and per-header detail.

---

### RETRY

- Signal (LIVE): Retry pass summary.
- Signal (LIVE): Final outcomes.
- Diagnostics (DEBUG): Per-failure execution details.
- Diagnostics (TRACE): Decision trace and anchors.

---

### UISTATE

- Signal (LIVE): Properties binding mismatch / frame binding failure.
- Signal (LIVE): Critical UI state blockers (overlays/modals preventing action).
- Diagnostics (DEBUG): Only on failure paths.
- Diagnostics (TRACE): Heavy UI probes.

---

### REG

- Signal (LIVE): Registry drift warnings.
- Diagnostics (DEBUG): Anchor selection decisions.
- Diagnostics (TRACE): Registry snapshots.

---

## Counters (Always On)

Examples:

- `canvas_align_checks`
- `sidebar_open_calls`
- `sidebar_fastpath_hits`
- `tab_switches`
- `drag_attempts`
- `phantom_timeouts`
- `hard_resyncs`
- `properties_opens`
- `table_stage_retries`
- `trace.registry_vs_dom_dumps`
- `trace.order_alignment_dumps`
- `ui_probe_heavy_skipped_non_trace`

These should be summarised **per activity** at the end of a run.

---

## Per-Activity Summary Block

At activity completion, emit a compact summary including:

- total fields built
- total elapsed time
- counts from the counters above
- failures + retries executed

This summary is the primary artifact for live monitoring.

---

## Final Notes

- This policy is designed to **support optimisation work**, not replace it
- Logging discipline here will make redundancy and performance issues obvious
- Implementation should be incremental and gated

---

End of Instrumentation & Logging Matrix.
