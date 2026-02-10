# CA Activity Builder – Architecture Notes

This document describes the architectural intent, invariants, and design constraints of the CA Activity Builder project.

It is intended for developers working on the automation, not end users.

---

## Related docs

- [README](README.md)
- [Tech debt ledger](TECH_DEBT.md)
- [Field capability matrix](FIELD_CAPABILITIES.md)

---

## Design Constraints

- CloudAssess does not expose a stable public automation API
- The UI is Turbo-driven and frequently re-renders DOM nodes
- Many actions succeed visually but fail silently without verification

As a result, the architecture is designed to be **defensive by default**.

---

## Core Invariants

The following rules must never be violated:

1. **Builder never edits content**
2. **Editor never creates or drags fields**
3. **Navigation lives in CASession**
4. **All UI actions must be verifiable**
5. **Failure is preferable to silent misconfiguration**

---

## Module Responsibilities

### CASession

Single source of truth for:

- browser lifecycle
- navigation
- waits
- safe clicking
- refresh and recovery

No activity-specific logic belongs here.

---

### ActivityBuilder

Responsible only for creating structure:

- sections
- fields
- placement

Uses strict ID diffing + registry filtering to identify new fields.

Never edits titles, bodies, or properties.

---

### ActivityEditor

Responsible only for configuration:

- titles
- Froala HTML
- visibility
- marking
- tables
- single choice options

Editor actions must:

- verify the correct field is bound
- tolerate Turbo swaps
- re-find elements before interaction

---

### ActivitySections

Manages the relationship between:

- sidebar section list
- active canvas section

Fast-path alignment checks are preferred; long waits are avoided.

---

### ActivityRegistry

Provides stability by:

- tracking created sections and fields
- preventing rediscovery
- supporting strict new-field detection

The registry is a safety mechanism, not a cache. It guards against duplicate IDs
and mismatched field types, and it emits diagnostics when drift is detected.

---

## Spec → Config → UI Flow

YAML Spec
   ↓
ActivityInstruction
   ↓
FieldInstruction(s)
   ↓
FieldConfig (dataclass)
   ↓
Editor applies config

Each layer narrows responsibility and enforces contracts.

---

## Verification Strategy

Every destructive action must be followed by proof:

- Drag/drop → ID diff + registry filter
- Sidebar open → verify bound field id
- Toggle → read back state
- Editor input → read back value

If proof fails, recovery or abort is preferred to guessing.

---

## Stale Element Handling

Stale elements are expected due to Turbo re-renders.

Allowed patterns:

- re-find and retry once
- re-resolve container then proceed

Disallowed patterns:

- caching WebElements across UI mutations
- retry loops without verification

---

## Field Capability Awareness

Not all field types support the same controls.

Examples:

- Paragraph: no assessor update
- Auto-marked: may require correct answer

Editor logic must treat unsupported controls as no-ops. The current capability
matrix lives in `FIELD_CAPABILITIES.md` and should be kept aligned with
`ActivityEditor` gating.

---

## Performance Philosophy

Performance optimisations must:

- preserve verification
- never remove guards
- be driven by measured timing data

Use instrumentation counters and TRACE-gated diagnostics to justify changes.
The goal is *predictable correctness*, not raw speed.

---

## Acceptable Failure Modes

- Skip field if config cannot be proven
- Abort activity if critical field fails
- Abort run if UI state cannot be verified

Silent corruption is never acceptable.

---

## Future Architecture Work

- Keep `FIELD_CAPABILITIES.md` authoritative and up to date with UI changes
- Revision-aware editing workflow
- Dropzone resolution improvements
- Declarative selector contracts

---

## Final Principle

> If the UI cannot be proven to be in the expected state, do not proceed.

This rule outweighs all others.
