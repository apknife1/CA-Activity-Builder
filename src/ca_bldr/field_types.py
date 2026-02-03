from dataclasses import dataclass

@dataclass(frozen=True)
class FieldTypeSpec:
    key: str                      # internal key, e.g. "paragraph", "long_answer"
    display_name: str             # human label (optional, for logging)
    sidebar_group: str            # e.g. "QUESTION" or "CONTENT"
    sidebar_tab_label: str        # e.g. "Text", "Marked manually", "Auto marked"
    sidebar_data_type: str        # value of data-type attribute on the toolbox card
    canvas_field_selector: str    # CSS selector for fields on the canvas
    requires_section: bool        # True if must be added inside a section

FIELD_TYPES: dict[str, FieldTypeSpec] = {
    # Existing paragraph (content text)
    "paragraph": FieldTypeSpec(
        key="paragraph",
        display_name="Paragraph",
        sidebar_group="CONTENT",
        sidebar_tab_label="Text",
        sidebar_data_type="text",  # <div data-type="text"> in sidebar
        canvas_field_selector="#section-fields .designer__field.designer__field--text",
        requires_section=False,
    ),

    # Long answer (the one you just showed: designer__field--text_area, data-type="text_area")
    "long_answer": FieldTypeSpec(
        key="long_answer",
        display_name="Long Answer",
        sidebar_group="QUESTION",
        sidebar_tab_label="Marked manually",  # from the sidebar nav
        sidebar_data_type="text_area",        # <div data-type="text_area">
        canvas_field_selector="#section-fields .designer__field.designer__field--text_area",
        requires_section=True,
    ),

    # NEW: Short answer (designer__field--text_field, data-type="text_field")
    "short_answer": FieldTypeSpec(
        key="short_answer",
        display_name="Short answer",
        sidebar_group="QUESTION",
        sidebar_tab_label="Marked manually",  # same tab as long answer
        sidebar_data_type="text_field",       # <div data-type="text_field">
        canvas_field_selector="#section-fields .designer__field.designer__field--text_field",
        requires_section=True,
    ),

    # NEW: File upload question (designer__field--upload, data-type="upload")
    "file_upload": FieldTypeSpec(
        key="file_upload",
        display_name="File upload",
        sidebar_group="QUESTION",
        sidebar_tab_label="Marked manually",
        sidebar_data_type="upload",          # <div data-type="upload">
        canvas_field_selector="#section-fields .designer__field.designer__field--upload",
        requires_section=True,
    ),

    "interactive_table": FieldTypeSpec(
        key="interactive_table",
        display_name="Table",
        sidebar_group="CONTENT",
        sidebar_tab_label="Interactive",
        sidebar_data_type="table",
        canvas_field_selector="#section-fields .designer__field.designer__field--table",  # new key
        requires_section=True,
    ),

    "signature": FieldTypeSpec(
        key="signature",
        display_name="Signature Pad",
        sidebar_group="OTHER",
        sidebar_tab_label="Confirmation",  # or "Interactive" if that's where it lives in CA
        sidebar_data_type="signature",        # from data-type in the toolbox DOM
        canvas_field_selector="#section-fields .designer__field.designer__field--signature",
        requires_section=True,
    ),

    "date_field": FieldTypeSpec(
        key="date_field",
        display_name="Date Picker",
        sidebar_group="OTHER",
        sidebar_tab_label="Confirmation",  # adjust if it actually lives under a different tab
        sidebar_data_type="date_field",       # from data-type in the toolbox DOM
        canvas_field_selector="#section-fields .designer__field.designer__field--date_field",
        requires_section=True,
    ),
    "single_choice": FieldTypeSpec(
        key="single_choice",
        display_name="Single Choice",
        sidebar_group="QUESTION",
        sidebar_tab_label="Auto marked",
        sidebar_data_type="single_choice",  # best guess; we’ll select by icon/label anyway
        canvas_field_selector="#section-fields .designer__field.designer__field--question",
        requires_section=True,
    ),
}
