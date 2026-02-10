# CA Activity Builder – Field Capability Matrix

This document defines which UI controls are supported per field type.

The goal is to prevent the editor from attempting unsupported operations (e.g. setting assessor visibility to update on paragraphs).

Last reviewed: 2026-02-10. Re-verify after UI changes.

---

## Related docs

- [README](README.md)
- [Architecture](ARCHITECTURE.md)
- [Tech debt ledger](TECH_DEBT.md)

---

## Capability Concepts

- **Visibility**
  - learner: hidden / read / update / read-on-submit
  - assessor: hidden / read / update

- **Marking / feedback**
  - required
  - marking_type
  - model_answer
  - assessor_comments

- **Content editors**
  - title input
  - Froala HTML body

- **Composite controls**
  - tables (shape/types/headers/overrides)
  - single choice options + correct answer

---

## Current Matrix (v1)

### paragraph

- title: ✅
- Froala body: ✅
- learner visibility: ✅ (hidden/read)
- assessor visibility: ✅ (read only)
- assessor update: ❌
- marking_type / required: ❌
- model_answer: ❌
- assessor_comments: ❌

### long_answer

- title: ✅
- Froala body: ✅
- learner visibility: ✅
- assessor visibility: ✅
- required: ✅
- marking_type: ✅
- model_answer: ✅
- assessor_comments: ✅

### short_answer

- title: ✅
- body: (usually none / minimal)
- learner visibility: ✅
- assessor visibility: ✅
- required: ✅
- marking_type: ✅
- model_answer: ✅ (if enabled in UI)
- assessor_comments: ✅

### file_upload

- title: ✅
- body/help text: ✅ (if supported)
- learner visibility: ✅
- assessor visibility: ✅
- required: ✅
- marking_type: ✅ (often not marked/manual)
- model_answer: ❌
- assessor_comments: ✅ (best effort)

### interactive_table

- title: ✅
- table shape: ✅
- column types: ✅
- headers: ✅
- row labels: ✅
- cell overrides (text): ✅
- cell overrides (cell_type): ⚠️ (not yet implemented)
- learner visibility: ✅
- assessor visibility: ✅
- marking_type: ⚠️ depends on CA UI (treat as best-effort)

### single_choice (auto marked)

- title: ✅
- Froala body/description: ✅
- options add/remove: ✅
- option labels: ✅
- correct answer selection: ✅ (CA often requires at least one)
- marking_type: ✅ (e.g. not marked) – available in CA UI
- learner visibility: ✅
- assessor visibility: ✅

### signature

- title: ✅
- required: ✅
- role: ✅ (assessor/learner/both via policy)
- learner visibility: ✅
- assessor visibility: ✅

### date_field

- title: ✅
- required: ✅
- learner visibility: ✅
- assessor visibility: ✅

---

## Implementation Notes

1) Encode these capabilities in code (e.g. dict keyed by field type key).
2) Editor should:

- apply a config knob only if supported
- otherwise skip silently and log at DEBUG
- treat this matrix as the reference, but use ActivityEditor gating as the source of truth

Related tech debt: [TD-011 — Field capability rules are not encoded](TECH_DEBT.md#td-011--field-capability-rules-are-not-encoded)

This turns "No radio found" warnings into intentional no-ops.

---

## How to Update

- When adding a new field type, update this matrix and the capability gating in `ActivityEditor`.
- If UI behavior changes, update both the matrix and the relevant tests or run notes.
