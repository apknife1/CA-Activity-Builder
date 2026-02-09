# activity_snapshot.py (new module)

from typing import Any

from .activity_registry import ActivityRegistry
from .activity_sections import ActivitySections
from .activity_editor import ActivityEditor
from .field_types import FIELD_TYPES
from .instrumentation import Cat

from selenium.webdriver.common.by import By

def build_registry_from_current_activity(
    sections: ActivitySections,
    editor: ActivityEditor,
    registry: ActivityRegistry,
) -> None:
    """
    Populate the registry by inspecting the currently opened activity in CloudAssess.

    Assumes:
      - You're already on the Activity Builder page for a given activity.
    """

    session = sections.session
    session.counters.inc("registry.rebuilds")
    session.emit_signal(Cat.REG, "Registry rebuild started", reason="snapshot_rebuild")

    def _ctx(**extra: Any) -> dict[str, Any]:
        ctx: dict[str, Any] = {"kind": "snapshot_rebuild"}
        ctx.update(extra)
        return ctx

    # 1. List all section <li> elements
    li_list = sections.list()
    if not li_list:
        session.emit_signal(
            Cat.REG,
            "No sections found in sidebar; registry will remain empty.",
            level="warning",
            **_ctx(),
        )
        return

    # Map FIELD_TYPES to canvas selectors for type detection
    type_specs = FIELD_TYPES

    for index, li in enumerate(li_list):
        # 2. Build a SectionHandle and register it
        sec_handle = sections._build_section_handle_from_li(li, index=index)
        registry.add_or_update_section(sec_handle)

        # 3. Select this section so its fields load in the canvas
        ch = sections.select_by_handle(sec_handle)
        if not ch:
            session.counters.inc("registry.snapshot_section_skips")
            session.emit_signal(
                Cat.REG,
                f"Could not select section id={sec_handle.section_id}; skipping field scan for this section.",
                level="warning",
                **_ctx(sec=sec_handle.section_id),
            )
            continue

        # 4. Find all fields in this section's canvas
        driver = editor.driver
        field_els = driver.find_elements(
            By.CSS_SELECTOR,
            "#section-fields .designer__field",
        )

        for idx, field_el in enumerate(field_els):
            field_id = editor.get_field_id_from_element(field_el)
            if not field_id:
                session.counters.inc("registry.snapshot_missing_field_id")
                session.emit_diag(
                    Cat.REG,
                    f"Skipping field at index {idx}; could not infer CA field id.",
                    **_ctx(sec=sec_handle.section_id),
                )
                continue

            # 5. Determine field_type_key from its classes
            classes = set((field_el.get_attribute("class") or "").split())
            field_type_key = None

            for key, spec in type_specs.items():
                sel = spec.canvas_field_selector  # e.g. ".designer__field.designer__field--text_field"
                # Very simple seam: look for the specific modifier class
                # you use in your selectors (e.g. "designer__field--text_field")
                for token in sel.split("."):
                    if token.startswith("designer__field--") and token in classes:
                        field_type_key = key
                        break
                if field_type_key:
                    break

            if not field_type_key:
                session.counters.inc("registry.snapshot_unknown_field_type")
                session.emit_diag(
                    Cat.REG,
                    f"Could not determine field type for field id={field_id} (classes={classes!r}); skipping.",
                    **_ctx(sec=sec_handle.section_id, fid=field_id),
                )
                continue

            field_title = editor.get_field_title(field_el)

            from .field_handles import FieldHandle

            fh = FieldHandle(
                field_id=field_id,
                section_id=sec_handle.section_id,
                field_type_key=field_type_key,
                index=idx,
                title=field_title
            )
            registry.add_field(fh)

    section_count, field_count = registry.stats()
    session.emit_signal(
        Cat.REG,
        "Registry rebuild completed",
        reason="snapshot_rebuild",
        sections=section_count,
        fields=field_count,
    )
    session.emit_signal(
        Cat.REG,
        "Activity registry rebuilt from current activity snapshot.",
        reason="snapshot_rebuild",
        **_ctx(sections=section_count, fields=field_count),
    )


