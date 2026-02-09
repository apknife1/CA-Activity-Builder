# Whole-of-Process Review Backlog (Pre-Instrumentation)

This document is the **authoritative, verbose review backlog** produced during the end-to-end walkthrough of the CA Activity Builder *before* behavioural changes were made.

It captures **all review points identified across the controller, session, builder, sections, editor, registry, and supporting modules**, including observability concerns that led to the instrumentation work.

Nothing in this list implies that changes have been made. This is a **review and optimisation ledger**, intended to be worked through *after* instrumentation is in place.

## Progress (2026-02-09)

- Instrumentation migration completed for `controller.py`, `activity_builder.py`, and `activity_sections.py` (logger calls replaced with `emit_signal`/`emit_diag`).
- Instrumentation migration completed for `activity_editor.py` (logger calls replaced with `emit_signal`/`emit_diag`; table, Froala, UI state, and properties are now instrumented).
- `activity_sections._section_ctx` typing widened to avoid type-checker issues when passing `**ctx` to `emit_diag`.
- `emit_signal` now supports a `level` parameter (default `info`) and warning/error levels are in use where appropriate.
- `scripts/check_cat_enum.py` added and run to validate `Cat.*` references (OK).
- `phase_timer` refactor to instrumentation remains pending (see Observability priorities).

---

## 1. Controller & Activity Lifecycle

1. Active -> inactive template lookup causes at least one extra full navigation when an activity does not already exist.
2. Template lookup logic always checks inactive templates even when not strictly necessary.
3. Locate + create flow can result in active -> inactive -> active page churn.
4. Create flow always navigates back to active templates regardless of prior state.
5. Controller had multiple early returns that prevented a unified per-activity summary.
6. No single authoritative activity lifecycle boundary (start -> end) existed originally.
7. Activity timing was fragmented across phases without a final roll-up.
8. Failure reasons were logged inline but not summarised at the activity level.

### Controller Instrumentation Priorities

- Emit `Cat.NAV` signals for activity start/finish with consistent context keys (`act`, `sec`, `fid`, `fi`, `a`) so each run is bookended by a single summary line containing counters and elapsed time.
- Count template lookup outcomes (found/active/inactive) and the number of navigation hops to prove whether the extra navigation can be skipped for already-closed flows.
- Consolidate failure logging into a per-activity counter summary (e.g., `retry.pass_runs`, `drop.drag_attempts`) and surface actionable reasons in `LIVE` mode while keeping noisy per-field diagnostics in `DEBUG`.

---

## 2. Section Handling & Canvas Alignment

1. Canvas alignment checks are duplicated across `_select_from_current_handle`, `_select_and_confirm`, and builder-level guards.
2. Alignment checks incur waits even when already aligned.
3. Alignment logging is verbose and repetitive.
4. Section reselection occurs even when the current section is already correct.
5. Section list cache TTL (`SECTIONS_LIST_CACHE_TTL = 0.75s`) may be too short for multi-field operations.
6. Section list cache invalidation happens frequently due to conservative error handling.
7. Hard resync logic is expensive but opaque in logs.

### Section Handling Instrumentation Priorities

- Track `section.canvas_align_checks`, `section.fastpath_hits`, and `section.hard_resyncs` counts to spot redundant waits before tweaking cache TTLs.
- Emit `Cat.SECTION` diagnostics only when alignment actually re-runs and pair them with cache invalidation reasons so the log noise is proportional to real work.
- Record section selection attempts that bypass the fast path and log canvas-aligned state in `DEBUG` so future refactors can rely on data-driven decisions.

---

## 3. Builder - Sidebar, Tabs, Drag & Drop

1. Fields sidebar is re-ensured for every field add.
2. Sidebar fast-path vs fallback behaviour was not clearly visible in logs.
3. Toolbox tab activation repeats even when adding multiple fields of the same type.
4. Sidebar/tab logging was extremely noisy and inconsistent.
5. Drag offset attempts are conservative and often repeated.
6. Drop confirmation performs both DOM count polling and ID-diff confirmation.
7. Huge dropzone scroll logic is expensive but correctness-critical.
8. Sortable reorder logging is very verbose.
9. Sortable residue cleanup always runs, even on no-op reorders.
10. Placement verification logging is heavy and unconditional.

### Builder Instrumentation Priorities

- Emit `Cat.SIDEBAR` signals with `kind` and `method` context for each ensure operation and gate the `DEBUG` logs behind rate limiters so the fast path is clearly separable from fallbacks.
- Count per-tab activations plus sidebar focus resets and keep the drag offset trial counts in `Cat.DROP` diagnostics so we can optimise repeated behaviour once the data proves it is redundant.
- Make sort/order logging signal-only (LIVE) when a failure happens and push the heavy re-render traces into TRACE, while always incrementing counters like `drop.drag_attempts` and `drop.offset_tries`.

---

## 4. Phantom Detection & Recovery

1. Hard resync is triggered on the first phantom timeout.
2. Phantom diagnostics (registry vs DOM, order alignment) are very heavy.
3. Late-candidate recovery may succeed without resync but is not measured.
4. Phantom events were not clearly distinguished as signal vs diagnostic.
5. Phantom recovery paths were difficult to trace in logs.

### Phantom Detection Instrumentation Priorities

- Emit `Cat.PHANTOM` signals for each timeout, recovery decision, and hard resync so operators can trace the exact path; move the registry-vs-DOM dumps to TRACE.
- Count late-candidate recovery hits separately from hard resyncs so instrumentation can confirm whether the resync can be delayed.
- Gate `Cat.REG` diagnostics through rate limiters (e.g., increment `registry.drift_warnings` before dumping) to keep the logging payload proportional to the seriousness of the drift.

---

## 5. Editor - Content & Properties

1. Repeated `_cleanup_canvas()` and refind cycles occur after multiple operations.
2. Cleanup timing interacts poorly with Froala editor stability.
3. Properties sidebar may open multiple times per field.
4. Properties binding verification is expensive and always on.
5. Body audit (`audit_bodies_now`) is extremely expensive.
6. Body audits were not gated to debug-only scenarios.
7. Table configuration retries are multiplicative (stages x retries x cells).
8. No visibility into which table stage causes most retries.

### Editor Instrumentation Priorities

- Instrument `_cleanup_canvas()` invocations and durations via `Cat.UISTATE` so you can see whether the cleanup is correlated with Froala retries before enforcing it unconditionally.
- Count sidebar opens, binding retries, and skip events while keeping the detailed binding proof logs in `DEBUG`; once binding is stable you can mute `LIVE` noise.
- Add `Cat.TABLE` counters per stage (header, column, cell override) plus retry attempts so the instrumentation identifies the most expensive stage before reducing retries.

---

## 6. Registry Integrity

1. Section field lists can accumulate duplicate handles.
2. Registry drift is possible if delete paths cannot infer `field_id`.
3. Anchor computation correctness depends on strict registry hygiene.
4. Registry vs DOM diagnostics are heavy but always emitted.

### Registry Instrumentation Priorities

- Emit `Cat.REG` signals when duplicates or drift arise and keep the verbose snapshots gated behind TRACE with counters like `registry.snapshot_count`.
- Count anchor-computation failures separately from the creation path so you can prove whether registry hygiene affects dropzone accuracy.
- Track registry rebuilds via instrumentation so the heavy rebuild paths only occur when triggered intentionally (e.g., recovery mode).

---

## 7. Deleter & Reset Paths

1. Bulk delete rescans the DOM on every iteration.
2. Modal handler may wait full timeout even when modal is absent.
3. Delete + recreate retry paths are very expensive.

### Deleter Instrumentation Priorities

- Emit deletion counters (`deleter.fields_deleted`, `deleter.modal_waits`) so we know whether the DOM rescan is actually a bottleneck before optimizing it.
- Track modal-find durations and fallback hits; keep the heavier trace only when a modal is unexpectedly slow or absent.
- Count delete+recreate retry cycles so that retries can be gated behind a threshold instead of always running.

---

## 8. Session / Selenium Layer

1. Implicit wait (`implicitly_wait(3)`) may amplify DOM scan cost.
2. Template per-page selector is set repeatedly.
3. Navigation helpers ignore passed timeout in favour of the global wait.
4. UI probe helpers are heavy and ungated.

### Session Instrumentation Priorities

- Emit `Cat.NAV` navigation durations and failure counts so we can prove whether the implicit wait is masking or amplifying real slowness before adjusting it.
- Gate UI probes behind TRACE and count how often they fire; use counters to expose the difference between normal and diagnostic executions.
- Emit a `Cat.STARTUP` signal that records the session configuration (implicit wait, log mode) so instrumentation can tie infrastructure choices to later behaviour.

---

## 9. Snapshot & Full Rebuild

1. `build_registry_from_current_activity` is extremely expensive.
2. Snapshot usage was not clearly bounded to recovery/debug scenarios.

### Snapshot Instrumentation Priorities

- Track snapshot entry/exit and guard the heavy dump by TRACE; expose a counter (e.g., `registry.snapshots`) so snapshots only occur when needed.
- Emit a `Cat.REG` signal summarising why a rebuild ran (manual vs recovery) so the instrumentation differentiates intentional rebuilds from regressions.

---

## 10. Observability & Logging (Drivers for Instrumentation)

1. Logs were verbose but disjointed.
2. No consistent context keys (`act/sec/fid/fi/a`).
3. No clear signal vs diagnostic distinction.
4. Heavy diagnostics were always emitted.
5. No live-safe logging mode existed.
6. No counters existed to quantify redundancy.
7. No per-activity summary anchored long logs.

### Observability Instrumentation Priorities

- Finalise the `Cat`/`LogMode` gating rules so that counters stay always-on while diagnostics respect the configured mode (`live`, `debug`, `trace`).
- Make the per-activity summary the only `LIVE` signal that includes all key counters (`drop.drag_attempts`, `phantom.timeouts`, `retry.pass_runs`) to keep logs manageable.
- Keep context formatting centralized in `instrumentation.format_ctx` so every signal stays searchable and the whole system can leverage the same keys.
- Refactor `phase_timer` to emit instrumentation signals/diags instead of `logger.info` phase logs.
- During module refactors, run `scripts/check_cat_enum.py` to confirm all `Cat.*` references exist in the enum.

---

## Notes

- Items **52-58** directly motivated the decision to implement the instrumentation system first.
- Items **9-51** should not be actioned until instrumentation and counters are complete.
- This document is intended to live in the repository alongside design and architecture notes and to be referenced during future optimisation passes.

---

## Logging / Instrumentation Matrix

This section inventories the remaining `logger.*` calls and captures which ones should be migrated into structured `Cat.*` instrumentation (to avoid redundant output), kept for narrative context, or dropped.

| Module | Logging focus | Instrumentation analogue | Recommendation |
| --- | --- | --- | --- |
| `ActivityBuilder` | Drag/drop phase markers, phantom recovery, section alignment retries | `Cat.DROP`, `Cat.PHANTOM`, `Cat.SECTION` | Migrate the signal-level messages into `emit_signal`/`emit_diag`, then drop the duplicate `logger.*` calls so that instrumentation owns verbosity. Keep one `logger.info` per phase boundary if it still adds narrative value. |
| `ActivityEditor` | Properties writes, UI_STATE gate proofs, table stage retries | `Cat.UISTATE`, `Cat.PROPS`, `Cat.CONFIGURE`, `Cat.TABLE` | Emit these diagnostics through the instrumentation helpers and remove the noisy `logger.debug` duplicates. Only the higher-level audits or fallback summaries (e.g., body audits) should remain as plain logs. |
| `ActivitySections` / `ActivityDeleter` | Sidebar visibility, delete modal handling, registry removals | `Cat.SECTION`, `Cat.REG` (pending) | Leave the low-frequency narrative logs (`info`/`warning`) but add structured emits for counters/retries; once instrumentation is enriched, the rest can be trimmed. |
| `CASession` / `ActivityBuildController` | Navigation start/end, template lookups, login flow | `Cat.NAV`, `Cat.STARTUP` | Keep the current per-activity summaries in plain `logger.info` for readability; instrumentation already owns the counterized data, so no additional migration is needed. |
| `ActivitySnapshot` & helpers | Registry rebuild / snapshot dumps | `Cat.REG` | Already emitting structured signals; drop any extra `logger.*` that duplicates those counts once we confirm the instrumentation blocks are present. |

We can move this matrix into its own reference document later, but for now keeping it inside this backlog keeps the decisions close to the work.

---
