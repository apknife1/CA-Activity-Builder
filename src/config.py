
import os
from dotenv import load_dotenv

load_dotenv()

# Base URL for your CA instance (dev sandbox if you have it)
CA_BASE_URL = os.getenv("CA_BASE_URL", "https://cti.assessapp.com")

# Domain used in url_contains checks
CA_BASE_DOMAIN = CA_BASE_URL.split("://", 1)[-1]  # e.g. "cti.assessapp.com"

# Common entry points
CA_DASHBOARD_URL = f"{CA_BASE_URL}/dashboard"
CA_LOGIN_URL = f"{CA_BASE_URL}/users/sign_in"
CA_ACTIVITY_TEMPLATES_URL = f"{CA_BASE_URL}/activity_templates"
CA_ACTIVITY_TEMPLATES_INACTIVE_URL = f"{CA_ACTIVITY_TEMPLATES_URL}?type=inactive"

# Explicit wait time for Selenium + Headless mode
WAIT_TIME = int(os.getenv("CA_WAIT_TIME", "10"))
IMPLICIT_WAIT = int(os.getenv("CA_IMPLICIT_WAIT", "3"))
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

# Very short cache to avoid re-scanning the sidebar repeatedly in tight loops.
SECTIONS_LIST_CACHE_TTL = 0.75

# --- Instrumentation / diagnostics ---
# When True, builder will log extra diagnostics around dropzones + placement.
INSTRUMENT_DROPS: bool = True
INSTRUMENT_UI_STATE: bool = True
LOG_MODE = os.getenv("CA_LOG_MODE", "live").lower()  # live | debug | trace
TEMPLATE_SEARCH_INACTIVE_FIRST = os.getenv("CA_TEMPLATE_SEARCH_INACTIVE_FIRST", "false").lower() == "true"
LOG_RATE_LIMITS_S = {
    "SECTION.canvas_aligned": 1.0,
    "SIDEBAR.fields_visible": 1.0,
}


# --- Retry behavior configuration ---
AUTO_RETRY_FAILURES = True          # unattended mode
PROMPT_ON_FAILURES = True           # interactive mode
RETRY_MAX_PASSES = 2                # how many retry sweeps
RETRY_FAILURE_THRESHOLD = 5         # stop early if too many retry failures in a row
RETRY_REFRESH_BEFORE_PASS = False   # optional: refresh once before each pass
RETRYABLE_ONLY_OVERRIDE = False     # if True, don't ignore non-retryable failures

# --- fault injection (testing retry logic) ---
FAULT_INJECT_ENABLED = False
# If None, uses time-based seed
FAULT_INJECT_SEED: int | None = None
# Probability (0..1) used ONLY to choose targets; injection triggers once per kind
FAULT_INJECT_PROB_ADD_FAIL = 1.0
FAULT_INJECT_PROB_PROPERTIES_FAIL = 1.0
FAULT_INJECT_PROB_CONFIGURE_FAIL = 1.0
# --- fault injector locations for specific testing ---
FAULT_INJECT_TARGET_ADD_POSITION = "section_top"        # "any" | "section_top" | "section_mid"
FAULT_INJECT_TARGET_CONFIGURE_POSITION = "section_top"  # "any" | "section_top" | "section_mid"
# Optional: if no mid exists, allow fallback to top/any
FAULT_INJECT_TARGET_FALLBACK = "section_top"            # "any" | "section_top" | "off"

# Builder resync constants
HARD_RESYNC_MAX_PER_ACTIVITY = 3
PHANTOM_TIMEOUT_ABORT = False   # True = abort activity build, False = skip field after retries
PHANTOM_RESYNC_ON_FIRST = True  # do hard resync after first phantom timeout

CRITICAL_FIELD_KEYS = {"file_upload", "long_answer", "short_answer", "signature", "date_field"}

# Credentials from environment variables
CA_USERNAME = os.getenv("CA_USERNAME")
CA_PASSWORD = os.getenv("CA_PASSWORD")

# Selectors used in login; adjust once you confirm them in the dev UI
SELECTORS = {
    "username_id": "user_login",
    "password_id": "user_password",
    "login_xpath": "/html/body/div[2]/div[1]/div/form/input[2]",  # from your working app
}

BUILDER_SELECTORS = {
    # --- Sidebars ---
    "sidebars": {
        "fields": {
            "tab": ".designer__sidebar__tab[data-type='fields']",
            "toggle_button": "button[data-type='fields']",
            "frame": None,  # no turbo-frame for fields
        },
        "sections": {
            "tab": ".designer__sidebar__tab[data-type='sections']",
            # we still need special handling for onclick/text here, so this is optional
            "toggle_button_onclick": "button[onclick*='toggleSidebar'][onclick*='sections']",
            "frame": "turbo-frame#designer_sections",
        },
    },
    # --- Sections sidebar ---
    "sections": {
        # <li> items for *editable* sections (excluding any fixed headers if needed)
        "items": "#sections-list li.designer__sidebar__item",
        # currently active section <li>
        "active_item": "#sections-list li.designer__sidebar__item.is-active",
        # label element that shows the section title (and is often clickable to rename)
        "title_label": ".designer__sidebar__item__title",
        # input used to edit the section title (we’ll refine this once you inspect CA DOM)
        "title_input": "input[name='section[title]']",
        # "Create Section" button inside the sections tab
        "create_button": (
            "button[data-controller='turbo-post'][data-url*='/sections']"
        ),
    },
    # --- Fields sidebar/toolbox ---
    "fields_sidebar": {
        # The whole fields sidebar (if you need to ensure it’s visible)
        "root": ".designer__fields-dragging",
        # Tab buttons (if you end up needing them by label)
        "tab_auto_marked": "button.nav-section[role='tab'][label='Auto marked']",
        "tab_marked_manually": "button.nav-section[role='tab'][label='Marked manually']",
        "tab_text": "button.nav-section[role='tab'][label='Text']",
        "tab_interactive": "button.nav-section[role='tab'][label='Interactive']",
        "tab_confirmation": "button.nav-section[role='tab'][label='Confirmation']",
        # You can later make a format string if CA gives you data-tab or similar.
        "tab_by_label": "button[role='tab'][aria-label='{label}']",
        # The active tab pane (to scope card searches)
        "active_tab_pane": ".tab-content .tab-pane.active.show",
        # Generic card selector by data-type (this is the one you’re already using)
        "card_by_data_type": (
            ".designer__fields-dragging__item[data-type='{data_type}']"
        ),
        # Specific convenience entries if you want
        "paragraph_tool": (
            ".designer__fields-dragging__item[data-type='text']"
        ),
        "long_answer_tool": (
            ".designer__fields-dragging__item[data-type='text_area']"
        ),
        "short_answer_tool": (
            ".designer__fields-dragging__item[data-type='text_field']"
        ),
        "file_upload_tool": (
            ".designer__fields-dragging__item[data-type='upload']"
        ),
        "single_choice_tool": (
            ".designer__fields-dragging__item "
            "use[xlink\\:href='/icons.svg#designer-field--single_choice']"
        ),
    },
    # --- Canvas / section fields ---
    "canvas": {
        # The generic canvas (you already had this)
        "canvas_root": ".designer__canvas",
        # The per-section dropzone you drag into
        "section_drop_zone": (
            "#section-fields-container .designer__canvas__dropping-field-zone"
        ),
        # Field wrappers by type (these match your FieldTypeSpec values)
        "field_paragraph": (
            "#section-fields .designer__field.designer__field--text"
        ),
        "field_long_answer": (
            "#section-fields .designer__field.designer__field--text_area"
        ),
        "field_short_answer": (
            "#section-fields .designer__field.designer__field--text_field"
        ),
        "field_file_upload": (
            "#section-fields .designer__field.designer__field--upload"
        ),
        "field_table": (
            "#section-fields .designer__field.designer__field--table"
        ),
        # Field action area (for delete etc., already used by ActivityDeleter)
        "field_actions": ".designer__field__actions",
        "field_delete_button": "button[data-action='delete']",
        # Field titles/descriptions
        "field_title_label": ".designer__field__editable-label--title",
        "field_body_label": ".designer__field__editable-label--description",
    },
    # --- Field properties panel (for later, but useful to plan now) ---
    "properties": {
        "root": ".designer__field-properties-panel",
        "hide_in_report_checkbox": "input[type='checkbox'][name='hide_in_report']",
        "learner_visibility_radios": "input[type='radio'][name='learners']",
        "assessor_visibility_radios": "input[type='radio'][name='assessors']",
        "required_checkbox": "input[type='checkbox'][name='required_field']",
        "marking_type_radios": "input[type='radio'][name='marking_type']",
        "model_answer_toggle": "input[type='checkbox'][name='model_answer']",
        "assessor_comments_toggle": "input[type='checkbox'][name='assessor_comments']",
        # Model answer editor (if it uses its own Froala)
        "model_answer_tab": "[data-role='model-answer-tab']",
        "model_answer_editor_root": ".model-answer-editor",
        # Assessor comments textarea
        "assessor_comments_textarea": "textarea[name='assessor_comments']",
    },
    # --- table properties panel ---
    "table": {
        # Root of the dynamic table under a table field
        "root": ".dynamic-table",

        # Inside that root:
        "header_cells": "thead tr th",
        "body_rows": "tbody tr",

        "body_cells": "div.dynamic-table__cell",

        # Add row/column buttons — inner Turbo buttons
        "add_column_button": (
            ".dynamic-table__add-action.dynamic-table__add-action--column "
            "button[data-controller='turbo-post']"
        ),
        "add_row_button": (
            ".dynamic-table__add-action.dynamic-table__add-action--row "
            "button[data-controller='turbo-post']"
        ),

        # Active state class for editable labels in table cells
        "editable_label_wrapper": ".designer__field__editable-label",
        "editable_label_display": ".field__editable-label",
        "editable_label_active_class": "designer__field__editable-label--active",

        # Class that marks a table cell as a heading
        "cell_heading_class": "designer__field__editable-label--table-cell-heading",

        # Row/column bulk-action containers
        "row_actions": ".dynamic-table__actions.dynamic-table__actions--rows",
        "column_actions": ".dynamic-table__actions.dynamic-table__actions--columns",

        # Add row/column buttons – use the wrapper divs that have the transparent-click controller
        "add_column_wrapper": ".dynamic-table__add-action.dynamic-table__add-action--column",
        "add_row_wrapper": ".dynamic-table__add-action.dynamic-table__add-action--row",
    },
    "single_choice": {
        "use_xlink": "use[xlink\\:href*='designer-field--single_choice']",
        "use_href": "use[href*='designer-field--single_choice']",
        "tool_item": ".designer__fields-dragging__item",
        # container holding option rows
        "answers_container": "div[id$='-assessment_field_answers']",
        # option rows
        "answer_rows": "div[id^='field-answer-row-']",
        "answer_wrapper": "div.designer__field__editable-label--question",
        "answer_wrapper_active": "div.designer__field__editable-label--question.designer__field__editable-label--active",
        "answer_label_display": "h4.field__editable-label",
        # the editable input for each option label
        "answer_text_input": "input[type='text'][data-ajax-input-value-url-value*='/field_answers/']",
        # correct answer checkbox
        "correct_checkbox": (
            "input[type='checkbox'][data-ajax-input-value-url-value*='update_field=correct_answer']"
        ),
        # delete option link (within a row)
        "delete_option_link": "a[data-turbo-method='delete'][href*='/field_answers/']",
        # add choice button (within field)
        "add_choice_button": "button[data-controller='turbo-post'][data-url$='/field_answers']",
    },
}

TEMPLATES_SELECTORS = {
    "page_sentinel": "turbo-frame#templates",
    "search_input": "input#search[name='search'][type='search']",
    "rows": "turbo-frame#templates tbody tr.tr-hover",
    "row_title_link": "td:nth-child(3) a[href*='/activity_templates/'][href$='/activity_revisions']",
    "row_code_link": "td:nth-child(2) a[href*='/activity_templates/'][href$='/activity_revisions']",
    "per_page_select": "select#items[name='items']",
    "pager_next": ".table__footer a[href*='page=2'], .table__footer a[href*='page=']",
    "table_footer": ".table__footer",
}
