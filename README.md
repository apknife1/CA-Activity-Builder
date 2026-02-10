# CA Activity Builder

A Python + Selenium automation tool for creating and configuring **CloudAssess Activity Templates** from structured YAML specifications.

This project prioritises **correctness, determinism, and recoverability** over raw speed. Performance optimisation is applied only after stability is proven.

---

## Documentation

- [Architecture & invariants](ARCHITECTURE.md)
- [Instrumentation & logging matrix](INSTRUMENTATION_LOGGING_MATRIX.md)
- [Tech debt ledger](TECH_DEBT.md)
- [Field capability matrix](FIELD_CAPABILITIES.md)
- Review documentation (other than `TECH_DEBT.md` / `CHECKLIST.md`) lives under `notes/`

> Tip: If you’re changing builder/editor behaviour, read **ARCHITECTURE.md** first.

---

## What This Tool Does

CA Activity Builder can:

- Create new CloudAssess **Activity Templates**
- Build full activities from **YAML specs**
- Configure:

  - titles and rich text (Froala)
  - learner / assessor visibility
  - marking settings
  - tables (shape, headers, row labels, cell overrides)
  - auto-marked single-choice outcomes
- Detect and **skip existing templates** (active or inactive)
- Run **multiple specs in one execution**
- Recover from common CloudAssess UI issues (Turbo re-renders, stale elements, phantom adds)

Supported activity types:

- **Written Assessment (WA)**
- **Competency Conversation (CC)**
- **Industry Evidence (IE)**
- **Assessment Result (AR)**

---

## Architectural Overview

The project follows a strict separation of concerns.

### CASession

Handles:

- login
- navigation
- waits and timeouts
- safe clicking and JS fallbacks
- Turbo-aware page refresh
- debug logging and instrumentation

**Rule:** no builder or editor logic lives here.

---

### ActivityBuilder

Responsible for **structure only**:

- selecting sections
- dragging fields from the toolbox
- resolving dropzones
- detecting newly added fields via strict ID diffing
- handling phantom adds and recovery

**Rule:** the builder never edits content.

---

### ActivityEditor

Responsible for **configuration only**:

- setting titles and Froala HTML
- configuring visibility, marking, and switches
- table configuration (rows, columns, headers, overrides)
- single-choice option configuration
- model answer handling

**Rule:** the editor never creates or drags fields.

---

### ActivitySections

Manages:

- section creation and renaming
- sidebar selection
- canvas alignment verification
- fast-path re-selection to avoid unnecessary waits

---

### ActivityRegistry

Tracks:

- created fields and sections
- prevents rediscovery of existing fields
- supports strict “new field” detection

---

### ActivityBuildController

The orchestration layer:

- spec selection
- multi-spec execution
- create vs skip decision logic
- build sequencing
- error handling and abort policy

---

## Specs

### Location

All specs live in:

src/specs/

Example files included:

- `example_wa.yml`
- `example_cc.yml`
- `example_ie.yml`
- `example_ar.yml`

---

### Spec Selection

By default, the app uses a **Tk file picker** to allow selecting one or more YAML files.

If the Tk UI is unavailable (e.g. headless environments), it falls back to CLI selection.

---

### General Spec Structure

Each spec describes **one unit of competency** and one or more activities.

Example (simplified):

```yaml
unit_code: CPCCCA3006
unit_title: Erect roof trusses
activity_type: assessment_result

mapping:
  performance_evidence:
    - stem: "Evidence of safe work practices"
      lines:
        - text: "Uses appropriate PPE"
          evidence_sources: [IE1, CC2]
```

Each activity type has its own expected structure, handled by the `SpecReader`.

---

## Running the Tool

### Requirements

- Python 3.11+
- Google Chrome
- ChromeDriver (managed automatically)
- CloudAssess account credentials

### Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

### Environment Variables

Create a `.env` file or export variables:

```env
CA_BASE_URL=https://yourtenant.assessapp.com
CA_USERNAME=automation@example.com
CA_PASSWORD=********
CA_WAIT_TIME=10
HEADLESS=false
```

---

### Run

```bash
python -m src.main
```

You will be prompted to select one or more spec files.

---

## Logging & Diagnostics

- Logs are written under `runs/<timestamp>/logs/`
- `runs/codex/` is reserved for Codex/agent artifacts and is ignored by git
- Instrumentation level is controlled by `CA_LOG_MODE` (`live`, `debug`, `trace`)
- Timing data is captured using `phase_timer` instrumentation signals
 - TRACE mode enables heavy dumps (registry snapshots, UI probe heavy, DOM alignment dumps)

Logs include:

- field creation attempts
- dropzone diagnostics
- sidebar binding verification
- recovery actions (phantom adds, resyncs)

---

## Known Limitations / Design Decisions

- **Locked activities** (assigned to learners) cannot be edited
  → future work: detect lock state and create new revisions
- CloudAssess Turbo re-renders can cause **random stale elements**
  → handled via retries and re-finding
- Paragraph fields do not support assessor “update”
  → skipped intentionally
- Some UI controls exist only conditionally (best-effort application)

These are treated as **expected environmental constraints**, not bugs.

---

## Developer Notes

This project automates a **highly dynamic, Turbo-driven UI** that does not provide a stable public automation API. As a result:

- **Verification is mandatory** before and after every destructive action
- Cached WebElements are assumed to become stale at any time
- All editor actions follow a **prove → act → re-prove** pattern

### Core Principles

- Prefer correctness over speed
- Never assume a drag/drop succeeded — always verify
- Never assume a properties panel is bound to the correct field
- If UI state cannot be proven, **fail or skip**, never guess

### Stale Elements

Random stale element exceptions are expected. They are acceptable **only when**:

- they are non-deterministic
- retries succeed
- the operation ultimately verifies correct state

Consistent stale failures indicate a missing proof gate and must be addressed.

### Field Capabilities

Not all field types support the same configuration:

- Paragraph fields cannot be set to assessor "update"
- Auto-marked fields may require a "correct answer" even when repurposed

The editor should skip unsupported settings silently (debug-level log only).

### Instrumentation

Instrumentation flags exist to help diagnose:

- phantom adds
- covered dropzones
- mis-bound properties panels

Leave instrumentation enabled during development; use `CA_LOG_MODE=live` for production runs.

---

## Roadmap / Technical Debt

Planned future work:

- Activity revision workflow (locked templates)
- Dropzone coverage reduction to lower phantom adds
- Keep field capability matrix current as CloudAssess UI changes
- Split large AR mapping tables into logical sections
- Clean-up of legacy debug utilities

---

## Final Notes

This tool is intentionally verbose and defensive.

If something looks redundant, it is probably compensating for a CloudAssess UI edge case.
