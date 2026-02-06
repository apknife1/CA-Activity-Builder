# AGENTS.md - CA Activity Builder

This file guides Codex/agent work in this repo. It is derived from:
- README.md
- ARCHITECTURE.md
- FIELD_CAPABILITIES.md
- INSTRUMENTATION_LOGGING_MATRIX.md
- TECH_DEBT.md
- CHECKLIST.md

## Project Summary
CA Activity Builder is a Python + Selenium automation tool that builds CloudAssess Activity Templates from YAML specs. The project prioritizes correctness, determinism, and recoverability over speed.

## Non-Negotiable Invariants
These rules must not be broken (ARCHITECTURE.md):
1. Builder never edits content
2. Editor never creates or drags fields
3. Navigation lives in CASession
4. All UI actions must be verifiable
5. Failure is preferable to silent misconfiguration

If UI state cannot be proven, skip or abort. Never guess.

## Module Responsibilities (Short)
- CASession: browser lifecycle, navigation, waits, safe clicking, refresh/recovery
- ActivityBuilder: structure only (sections/fields/placement), no content edits
- ActivityEditor: configuration only (titles, Froala, visibility, marking, tables, options)
- ActivitySections: sidebar/canvas alignment
- ActivityRegistry: tracks created fields/sections; supports strict new-field detection
- ActivityBuildController: orchestration (spec selection, sequencing, error policy)

## Spec Flow
YAML spec -> ActivityInstruction -> FieldInstruction(s) -> FieldConfig -> Editor applies config

## Verification Pattern
Use prove -> act -> re-prove for all destructive or stateful UI actions.
- Drag/drop: ID diff + registry filter
- Sidebar open: verify bound field id before any property write
- Toggles/inputs: read back value

Stale elements are expected; re-find once and proceed only if verification passes.

## Field Capability Gating
Not all field types support all controls. Use FIELD_CAPABILITIES.md and treat unsupported controls as no-ops with debug logging.

## Logging & Instrumentation
Follow INSTRUMENTATION_LOGGING_MATRIX.md:
- LIVE is default (high-signal only)
- DEBUG adds structured diagnostics
- TRACE is deep forensics
Include context keys (cat, act, sec, fid, type, fi, a) when available.
Counters are always on and summarized per activity.

## Tech Debt & Checklist
- New ideas go to TECH_DEBT.md
- CHECKLIST.md is the short execution queue
- Only move items to DONE with proof (logs/runs/commits)

## Running
- Python 3.11+
- `python -m venv .venv`
- `pip install -r requirements.txt`
- `python -m src.main`
Env vars live in `.env` (CA_BASE_URL, CA_USERNAME, CA_PASSWORD, etc.)

## Known Constraints
- Locked activities cannot be edited (revision workflow is future work)
- Turbo re-renders cause random stales; retries are acceptable only when verification succeeds
- Correctness beats speed

## Repo Hygiene
Run artifacts currently appear in repo root; planned move to `/runs` or `/out`.
Treat log/json dumps as transient.

## Change Discipline
If changing builder/editor behavior, read ARCHITECTURE.md first.
When behavior changes, update README/ARCHITECTURE/TECH_DEBT as appropriate.
