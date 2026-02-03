# activity_snapshot.py (new module)

from .activity_registry import ActivityRegistry
from .activity_sections import ActivitySections
from .activity_editor import ActivityEditor
from .field_types import FIELD_TYPES

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

    logger = sections.logger

    # 1. List all section <li> elements
    li_list = sections.list()
    if not li_list:
        logger.warning("No sections found in sidebar; registry will remain empty.")
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
            logger.warning(
                "Could not select section id=%s; skipping field scan for this section.",
                sec_handle.section_id,
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
                logger.debug("Skipping field at index %d; could not infer CA field id.", idx)
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
                logger.debug(
                    "Could not determine field type for field id=%s (classes=%r); skipping.",
                    field_id,
                    classes,
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

    logger.info("Activity registry rebuilt from current activity snapshot.")


