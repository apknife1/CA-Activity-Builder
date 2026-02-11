import logging
import time
import re

from typing import Any, Sequence, Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    StaleElementReferenceException,
    NoSuchElementException,
)

from .errors import TableResizeError, FieldPropertiesSidebarTimeout
from .session import CASession
from .activity_registry import ActivityRegistry
from .field_handles import FieldHandle
from .field_types import FIELD_TYPES
from .field_configs import (
    BaseFieldConfig,
    QuestionFieldConfig,
    ParagraphConfig,
    LongAnswerConfig,
    TableConfig,
    ShortAnswerConfig,
    FileUploadConfig,
    SignatureConfig,
    DatePickerConfig,
    LearnerVisibility,
    AssessorVisibility,
    MarkingType,
    TableCellConfig,
    SingleChoiceConfig,
    )
from .. import config
from .instrumentation import Cat, LogMode

FIELD_ID_SUFFIX_RE = re.compile(r"--(\d+)$")

FIELD_CAPS = {
    "paragraph": {
        "assessor_visibility_update": False,
        "required": False,
        "marking_type": False,
        "model_answer": False,
        "assessor_comments": False,
    },
    # question-like fields
    "long_answer": {"assessor_visibility_update": True, "required": True, "marking_type": True, "model_answer": True, "assessor_comments": True},
    "short_answer": {"assessor_visibility_update": True, "required": True, "marking_type": True, "model_answer": True, "assessor_comments": True},
    "file_upload": {"assessor_visibility_update": True, "required": True, "marking_type": True, "model_answer": False, "assessor_comments": True},
    "interactive_table": {"assessor_visibility_update": True, "required": False, "marking_type": True, "model_answer": False, "assessor_comments": False},
    "signature": {"assessor_visibility_update": True, "required": True, "marking_type": False, "model_answer": False, "assessor_comments": False},
    "date_field": {"assessor_visibility_update": True, "required": True, "marking_type": False, "model_answer": False, "assessor_comments": False},
    # default for other question blocks (e.g. single_choice)
    "question": {"assessor_visibility_update": True, "required": True, "marking_type": True, "model_answer": True, "assessor_comments": True},
    "unknown": {"assessor_visibility_update": True, "required": True, "marking_type": True, "model_answer": True, "assessor_comments": True},
}

PROBE_PRESENT = "present"
PROBE_MISSING = "missing"
PROBE_UNKNOWN = "unknown"


class ActivityEditor:
    """
    Edit/update functionality for an *existing* activity on the builder page.
    This assumes you're already on the Activity Builder screen for a given activity.
    """

    def __init__(self, session: CASession, registry: ActivityRegistry):
        """
        :param session: CASession instance
        """
        self.session = session
        self.driver = session.driver
        self.wait = session.wait
        self.registry = registry

        self.ui_state_recovery_count = 0

        self._skip_events: list[dict] = []

    def _editor_ctx(self, *, field_id: str | None = None, section_id: str | None = None, kind: str | None = None, stage: str | None = None) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "sec": section_id or "",
            "fid": field_id,
            "kind": kind or "editor",
        }
        if stage:
            ctx["a"] = stage
        return ctx

    # -------- Field discovery --------
    
    def get_field_id_from_element(self, field_el) -> Optional[str]:
        """
        Infer the CloudAssess field id (like '27432871') from known id patterns
        inside a field element.

        Now hardened against stale field_el: if the element is stale, we try to
        re-locate the currently-selected field and extract the id from there.
        """
        ctx = self._editor_ctx(kind="field_id")

        def _extract_from_root(root_el):
            # Try model-answer id first
            try:
                model_block = root_el.find_element(
                    By.CSS_SELECTOR,
                    "[id^='designer__field__model-answer-description--']",
                )
                mid = model_block.get_attribute("id") or ""
                m = FIELD_ID_SUFFIX_RE.search(mid)
                if m:
                    return m.group(1)
            except NoSuchElementException:
                pass

            # Fall back to main description id
            try:
                desc_block = root_el.find_element(
                    By.CSS_SELECTOR,
                    "[id^='designer__field__description--']",
                )
                did = desc_block.get_attribute("id") or ""
                m = FIELD_ID_SUFFIX_RE.search(did)
                if m:
                    return m.group(1)
            except NoSuchElementException:
                pass

            return None

        # First attempt: use the element we were given
        try:
            field_id = _extract_from_root(field_el)
            if field_id:
                return field_id
        except StaleElementReferenceException:
            self.session.emit_diag(
                Cat.CONFIGURE,
                "Field element became stale while extracting field id; attempting to re-locate selected field.",
                **ctx,
            )

        # Second attempt: re-locate the currently selected field in the canvas
        try:
            driver = self.driver
            # Adjust selector if needed: this assumes a selected field has a distinct class
            selected_root = driver.find_element(
                By.CSS_SELECTOR,
                ".designer__field.designer__field--active, .designer__field--selected",
            )
            field_id = _extract_from_root(selected_root)
            if field_id:
                return field_id
        except (NoSuchElementException, StaleElementReferenceException) as e:
            self.session.emit_signal(
                Cat.CONFIGURE,
                f"Could not re-locate selected field after stale reference: {e}",
                level="error",
                **ctx,
            )

        self.session.emit_diag(
            Cat.CONFIGURE,
            "Could not infer field id for field element.",
            **ctx,
        )
        return None
    
    def get_field_by_id(self, field_id: str):
        """
        Re-find a field element by its CA field id on the canvas.
        Assumes the correct section is already active.
        """
        driver = self.driver

        # Find any element with an id suffix matching this field_id,
        # then climb up to the .designer__field root.
        el = driver.find_element(
            By.CSS_SELECTOR,
            f"#section-fields [id$='--{field_id}']",
        )
        return el.find_element(
            By.XPATH,
            "./ancestor::div[contains(@class,'designer__field')]",
        )

    def get_fields(self, field_selector: str):
        """
        Generic helper: return all fields matching a CSS selector.
        Typically used with section-scoped selectors, e.g.
        '#section-fields .designer__field.designer__field--text_area'
        """
        elems = self.driver.find_elements(By.CSS_SELECTOR, field_selector)
        self.session.emit_diag(
            Cat.CONFIGURE,
            f"Found {len(elems)} fields with selector '{field_selector}'.",
            **self._editor_ctx(kind="field_discovery"),
        )
        return elems

    def get_last_field(self, field_selector: str):
        """
        Generic: get the last field matching selector.
        Raises TimeoutException if none exist.
        """
        elems = self.get_fields(field_selector)
        if not elems:
            raise TimeoutException(f"No fields found with selector '{field_selector}'.")
        last = elems[-1]
        self.session.emit_diag(
            Cat.CONFIGURE,
            "Using last field for editing.",
            **self._editor_ctx(kind="field_discovery"),
        )
        return last
    

    def get_last_field_for_type(self, field_key: str):
        """
        Get the last field for the given type key (e.g. 'paragraph', 'long_answer')
        using that type's canvas_field_selector.
        """
        spec = FIELD_TYPES[field_key]
        return self.get_last_field(spec.canvas_field_selector)
    

    def get_fields_for_type(self, field_key: str):
        """
        Return a list of all fields for the given type (paragraph, long_answer,
        short_answer, file_upload, etc.) in the *currently selected section*.

        Uses the type's canvas_field_selector, which should already be
        scoped to '#section-fields'.
        """
        spec = FIELD_TYPES[field_key]
        return self.get_fields(spec.canvas_field_selector)

    def get_nth_field_for_type(self, field_key: str, index: int):
        """
        Return the nth field (0-based) of the given type in the current section,
        or None if index is out of range.
        """
        fields = self.get_fields_for_type(field_key)
        if 0 <= index < len(fields):
            self.session.emit_diag(
                Cat.CONFIGURE,
                f"Using index {index} of {len(fields)} for type '{field_key}'.",
                **self._editor_ctx(kind="field_discovery"),
            )
            return fields[index]
        self.session.emit_signal(
            Cat.CONFIGURE,
            f"Index {index} out of range for type '{field_key}' (found {len(fields)} fields).",
            level="warning",
            **self._editor_ctx(kind="field_discovery"),
        )
        return None

    def find_field_by_title(
        self,
        field_key: str,
        title_text: str,
        *,
        exact: bool = True,
    ):
        """
        Find the first field of the given type whose title matches title_text.
        - If exact=True, match title text exactly.
        - If exact=False, do a case-insensitive substring match.
        """
        fields = self.get_fields_for_type(field_key)
        target = title_text.strip()
        target_lower = target.lower()

        for field in fields:
            try:
                title_el = field.find_element(
                    By.CSS_SELECTOR,
                    ".designer__field__editable-label--title"
                )
                actual = (title_el.text or "").strip()
                if exact:
                    if actual == target:
                        self.session.emit_diag(
                            Cat.CONFIGURE,
                            f"Matched '{field_key}' field with exact title '{actual}'.",
                            **self._editor_ctx(kind="field_discovery"),
                        )
                        return field
                else:
                    if target_lower in actual.lower():
                        self.session.emit_diag(
                            Cat.CONFIGURE,
                            f"Matched '{field_key}' field with partial title '{actual}'.",
                            **self._editor_ctx(kind="field_discovery"),
                        )
                        return field
            except Exception:
                continue

        self.session.emit_signal(
            Cat.CONFIGURE,
            f"No '{field_key}' field found with title '{title_text}' (exact={exact}).",
            level="warning",
            **self._editor_ctx(kind="field_discovery"),
        )
        return None
    
    def get_field_title(self, field_el) -> str | None:
        """
        Return the visible title text for a field element, or None if not found.
        """
        try:
            container = field_el.find_element(
                By.CSS_SELECTOR,
                ".designer__field__editable-label--title",
            )
            title_el = container.find_element(
                By.CSS_SELECTOR,
                "h2.field__editable-label",
            )
            txt = (title_el.text or "").strip()
            return txt or None
        except NoSuchElementException:
            return None
        except Exception:
            self.session.emit_diag(
                Cat.CONFIGURE,
                "Could not read field title from element.",
                **self._editor_ctx(kind="field_title"),
            )
            return None

    def try_get_field_id_strict(self, field_el) -> Optional[str]:
        """
        Strict id extraction for snapshot/diff logic.
        Never falls back to '.designer__field--selected' because that can lie during DOM churn.
        """
        try:
            # reuse the internal logic but without the selected-field fallback
            # (copy the _extract_from_root inner helper from get_field_id_from_element)
            def _extract_from_root(root_el):
                try:
                    model_block = root_el.find_element(By.CSS_SELECTOR, "[id^='designer__field__model-answer-description--']")
                    mid = model_block.get_attribute("id") or ""
                    m = FIELD_ID_SUFFIX_RE.search(mid)
                    if m:
                        return m.group(1)
                except NoSuchElementException:
                    pass

                try:
                    desc_block = root_el.find_element(By.CSS_SELECTOR, "[id^='designer__field__description--']")
                    did = desc_block.get_attribute("id") or ""
                    m = FIELD_ID_SUFFIX_RE.search(did)
                    if m:
                        return m.group(1)
                except NoSuchElementException:
                    pass

                return None

            return _extract_from_root(field_el)
        except StaleElementReferenceException:
            return None
        except Exception:
            return None
        
    def _observed_field_id_from_settings_frame(self, frame: WebElement) -> str | None:
        """
        Extract the field id that the field_settings_frame is currently bound to.

        DO NOT regex innerHTML - it frequently contains multiple /fields/<id> entries
        (especially for tables), leading to false binding proofs.
        """
        try:
            # Best signal: ajax-input-value url (radio/checkbox/select controls)
            # Example: /revisions/<rev>/sections/<sid>/fields/<fid>.turbo_stream?field_type=...
            els = frame.find_elements(By.CSS_SELECTOR, "[data-ajax-input-value-url-value*='/fields/']")
            for el in els:
                url = el.get_attribute("data-ajax-input-value-url-value") or ""
                m = re.search(r"/fields/(\d+)\.turbo_stream\b", url)
                if m:
                    return m.group(1)

            # Secondary: sometimes forms/buttons carry a data-url / formaction style attribute
            for attr in ("data-url", "formaction", "href"):
                els = frame.find_elements(By.CSS_SELECTOR, f"[{attr}*='/fields/']")
                for el in els:
                    url = el.get_attribute(attr) or ""
                    m = re.search(r"/fields/(\d+)\.turbo_stream\b", url)
                    if m:
                        return m.group(1)

            # Fallback: turbo-frame src (if present)
            src = frame.get_attribute("src") or ""
            m = re.search(r"/fields/(\d+)\.turbo_stream\b", src)
            if m:
                return m.group(1)

            return None
        except Exception:
            return None
        
    def _reset_canvas_ui_state(self) -> None:
        """
        Best-effort: collapse Froala tooltips/overlays and exit any active editor.
        Cheap + safe to call repeatedly.
        """
        driver = self.driver
        self.session.counters.inc("editor.canvas_resets")
        try:
            driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        except Exception:
            pass

        # Click a neutral canvas area (not inside the table) to defocus cell editors.
        try:
            canvas = driver.find_element(By.CSS_SELECTOR, "#section-fields")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", canvas)
            canvas.click()
        except Exception:
            pass
            
    # -------- Generic Field configuration --------
    def configure_field_from_config(
        self,
        handle: FieldHandle,
        cfg: BaseFieldConfig,
        last_successful_handle: FieldHandle | None,
        prop_fault_inject: bool = False,
    ) -> None:
        """
        Apply a FieldConfig dataclass to an existing Activity Builder field.

        This method is the main “edit” entry-point: it locates the field on the
        canvas using the provided FieldHandle, then applies any non-None settings
        from the cfg object.

        Key behaviour / ordering
        ------------------------
        1) Locate the field element:
        - Uses handle.field_id to resolve the field's root WebElement on the canvas.
        - If needed, derives the field_id from the element for downstream calls.

        2) Generic content:
        - title (cfg.title) -> set_field_title()
        - body_html (cfg.body_html) -> set_field_body()

        3) Type-specific structure (before properties):
        - Interactive table: when handle.field_type_key == "interactive_table"
            and cfg is a TableConfig, applies table structure/overrides via
            _configure_table_from_config() BEFORE toggling properties.

        - Signature: when handle.field_type_key == "signature" and cfg is a
            SignatureConfig, derives signature-specific visibility/required values
            via _set_signature_config_specifics().

        4) Visibility / marking / switches:
        - Applies report visibility and role visibility:
            hide_in_report, learner_visibility, assessor_visibility
        - Applies question properties when present:
            required, marking_type
        - Applies toggle switches when present:
            enable_model_answer (derived True when model_answer_html is not None),
            enable_assessor_comments
        - Only calls set_field_properties() if at least one of these values is
            non-None (meaning “change requested”).

        5) Model answer content (if enabled):
        - If model_answer_html is provided and a field_id is known, injects model
            answer HTML via set_field_model_answer(field_id, model_answer_html).

        Convention
        ----------
        Any attribute that is None means “do not change this setting”.

        Parameters
        ----------
        handle:
            FieldHandle describing the target field (field_id, field_type_key, and
            any other identifying metadata captured when the field was created or
            discovered on the canvas).
        cfg:
            BaseFieldConfig instance (or subclass such as ParagraphConfig,
            LongAnswerConfig, TableConfig, SignatureConfig). Only non-None attributes
            are applied.

        Raises
        ------
        TypeError
            If cfg is not an instance of BaseFieldConfig.
        """

        ctx_config = self._editor_ctx(
            field_id=handle.field_id,
            section_id=handle.section_id,
            kind="configure",
            stage="start",
        )
        self.session.counters.inc("editor.configure_attempts")
        self.session.emit_diag(Cat.CONFIGURE, "Configure field start", **ctx_config)

        def _emit_step_timing(step: str, start: float, cat: Cat = Cat.CONFIGURE, **extra) -> None:
            try:
                self.session.emit_diag(
                    cat,
                    "Step timing",
                    step=step,
                    elapsed_s=round(time.monotonic() - start, 3),
                    **self._editor_ctx(
                        field_id=handle.field_id,
                        section_id=handle.section_id,
                        kind="configure",
                        stage=step,
                    ),
                    **extra,
                )
            except Exception:
                pass

        def _cleanup_canvas(stage: str) -> WebElement:
            t0 = time.monotonic()
            self.session.counters.inc("editor.cleanup_calls")
            self.session.counters.inc(f"editor.cleanup_{stage}")
            self._reset_canvas_ui_state()
            fresh = self.get_field_by_id(handle.field_id)
            try:
                self._ensure_field_active(fresh)
            except Exception:
                pass
            elapsed_s = round(time.monotonic() - t0, 2)
            self.session.emit_diag(
                Cat.UISTATE,
                "Cleanup canvas complete",
                elapsed_s=elapsed_s,
                **self._editor_ctx(
                    field_id=handle.field_id,
                    section_id=handle.section_id,
                    kind="ui_state",
                    stage=f"cleanup_{stage}",
                ),
            )
            return fresh     

        if not isinstance(cfg, BaseFieldConfig):
            raise TypeError(
                f"cfg must be a BaseFieldConfig, got {type(cfg).__name__}"
            )

        # Paragraph fields cannot be assessor update in CA.
        if isinstance(cfg, ParagraphConfig) and cfg.assessor_visibility == "update":
            cfg.assessor_visibility = "read"

        field_el = self.get_field_by_id(handle.field_id)
        pivot_el = None
        if last_successful_handle and last_successful_handle.section_id == handle.section_id:
            try:
                # Only pivot within the same section to avoid cross-section lookup failures
                pivot_el = self.get_field_by_id(last_successful_handle.field_id)
            except Exception:
                pivot_el = None

        # --- 1) Generic title + body ---------------------------------------
        if cfg.title is not None:
            t_step = time.monotonic()
            self.set_field_title(field_el, cfg.title)
            _emit_step_timing("set_title", t_step)
            # field_el = _cleanup_canvas()

        if cfg.body_html is not None:
            t_step = time.monotonic()
            self.set_field_body(field_el, cfg.body_html)
            _emit_step_timing("set_body", t_step, cat=Cat.FROALA)
            t_step = time.monotonic()
            self._probe_body_persistence(
                field_id=handle.field_id,
                field_el=field_el,
                desired_html=cfg.body_html,
                phase="pre-props",
                allow_refind=True,
            )
            _emit_step_timing("probe_body_pre_props", t_step, cat=Cat.FROALA)
            t_step = time.monotonic()
            field_el = _cleanup_canvas("post_body")
            _emit_step_timing("cleanup_post_body", t_step, cat=Cat.UISTATE)

        # --- 2) Configure Interactive table structure BEFORE properties ----------
        if handle.field_type_key == "interactive_table" and isinstance(cfg, TableConfig):
            try:
                t_step = time.monotonic()
                self._configure_table_from_config(field_el, cfg)
                _emit_step_timing("table_config", t_step, cat=Cat.TABLE)
            except Exception as e:
                self._record_config_skip(
                    kind="configure",
                    reason=f"table configure exception: {type(e).__name__}: {e}",
                    retryable=True,
                    field_id=handle.field_id,
                    field_title=cfg.title,
                    requested={"field_type_key": handle.field_type_key},
                )
                raise                
            self.session.emit_diag(
                Cat.TABLE,
                f"Interactive table: {cfg.title!r} configured.",
                **self._editor_ctx(
                    field_id=handle.field_id,
                    section_id=handle.section_id,
                    kind="table_config",
                ),
            )
            t_step = time.monotonic()
            field_el = _cleanup_canvas("post_table")
            _emit_step_timing("cleanup_post_table", t_step, cat=Cat.UISTATE)

        if handle.field_type_key =="single_choice" and isinstance(cfg, SingleChoiceConfig):
            try:
                t_step = time.monotonic()
                self._configure_single_choice_answers(field_el, cfg.options, cfg.correct_index)
                _emit_step_timing("single_choice_config", t_step, cat=Cat.CONFIGURE)
            except Exception as e:
                self._record_config_skip(
                    kind="configure",
                    reason=f"single choice configure exception: {type(e).__name__}: {e}",
                    retryable=True,
                    field_id=handle.field_id,
                    field_title=cfg.title,
                    requested={"field_type_key": handle.field_type_key},
                )
                raise
            t_step = time.monotonic()
            field_el = _cleanup_canvas("post_single_choice")
            _emit_step_timing("cleanup_post_single_choice", t_step, cat=Cat.UISTATE)

        # --- 3) Visibility + marking properties ----------------------------
        # Question-like configs extend BaseFieldConfig with these attributes.
        required = getattr(cfg, "required", None)
        marking_type = getattr(cfg, "marking_type", None)
        model_answer_html = getattr(cfg, "model_answer_html", None)
        enable_assessor_comments = getattr(cfg, "enable_assessor_comments", None)

        field_el = self.get_field_by_id(handle.field_id)
        field_id = handle.field_id or self.get_field_id_from_element(field_el)

        # --- 4) Set signature specifics ------------------------
        if handle.field_type_key == "signature" and isinstance(cfg, SignatureConfig):
            learner_visibility, assessor_visibility, sig_required = self._set_signature_config_specifics(cfg)
            # Update config with derived values
            cfg.learner_visibility = learner_visibility
            cfg.assessor_visibility = assessor_visibility
            required = sig_required

        # Bug out early if we are skipping the property setting because of debug fault injector
        if prop_fault_inject:
            return

        # --- Derive "enable_model_answer": ---
        enable_model_answer = None

        if model_answer_html is not None:
            enable_model_answer = True

        self.session.emit_diag(
            Cat.PROPS,
            f"Switch values: enable_model_answer={enable_model_answer}, enable_assessor_comments={enable_assessor_comments}",
            **self._editor_ctx(
                field_id=handle.field_id,
                section_id=handle.section_id,
                kind="properties",
            ),
        )

        props_list = [
            ("hide_in_report", cfg.hide_in_report),
            ("learner_visibility", cfg.learner_visibility),
            ("assessor_visibility", cfg.assessor_visibility),
            ("required", required),
            ("marking_type", marking_type),
            ("enable_model_answer", enable_model_answer),
            ("enable_assessor_comments", enable_assessor_comments),
        ]
        requested_props = [name for name, value in props_list if value is not None]
        if requested_props:
            ctx_props = self._editor_ctx(
                field_id=handle.field_id,
                section_id=handle.section_id,
                kind="properties",
                stage="start",
            )
            self.session.counters.inc("editor.properties_writes")
            self.session.emit_diag(
                Cat.PROPS,
                "Applying field properties",
                properties=",".join(requested_props),
                **ctx_props,
            )

        # Only call set_field_properties if we have *something* to set.
        if any(
            value is not None
            for value in (
                cfg.hide_in_report,
                cfg.learner_visibility,
                cfg.assessor_visibility,
                required,
                marking_type,
                enable_assessor_comments,
                model_answer_html,
            )
        ):
            t_step = time.monotonic()
            self.set_field_properties(
                field_el,
                pivot_el,
                hide_in_report=cfg.hide_in_report,
                learner_visibility=cfg.learner_visibility,
                assessor_visibility=cfg.assessor_visibility,
                required=required,
                marking_type=marking_type,
                enable_model_answer=enable_model_answer,
                enable_assessor_comments=enable_assessor_comments,
            )
            _emit_step_timing(
                "set_properties",
                t_step,
                cat=Cat.PROPS,
                properties=",".join(requested_props),
            )

        # 5) Model answer content (canvas Froala)
        if model_answer_html and field_id:
            try:
                t_step = time.monotonic()
                self.set_field_model_answer(field_id, model_answer_html)
                _emit_step_timing("set_model_answer", t_step, cat=Cat.FROALA)
            except TimeoutException:
                self.session.emit_signal(
                    Cat.FROALA,
                    "Could not re-locate field after applying properties; skipping model answer configuration.",
                    level="warning",
                    **self._editor_ctx(
                        field_id=field_id,
                        section_id=handle.section_id,
                        kind="model_answer",
                    ),
                )
                return
            except Exception as e:
                self.session.emit_signal(
                    Cat.FROALA,
                    f"Error retreiving last field: {e}",
                    level="warning",
                    **self._editor_ctx(
                        field_id=field_id,
                        section_id=handle.section_id,
                        kind="model_answer",
                    ),
                )
            return
        
        t_step = time.monotonic()
        self._verify_body_after_properties_and_recover_once(
            handle=handle,
            desired_html=cfg.body_html,
        )
        _emit_step_timing("verify_body_post_props", t_step, cat=Cat.FROALA)

        try:
            fields_tab_visible = False
            field_settings_tab_visible = False
            fields_tab_sel = config.BUILDER_SELECTORS.get("sidebars", {}).get("fields", {}).get("tab")
            if fields_tab_sel:
                try:
                    el = self.driver.find_element(By.CSS_SELECTOR, fields_tab_sel)
                    fields_tab_visible = el.is_displayed()
                except Exception:
                    fields_tab_visible = False
            try:
                tab = self.driver.find_element(
                    By.CSS_SELECTOR,
                    ".designer__sidebar__tab[data-type='field-settings']",
                )
                field_settings_tab_visible = tab.is_displayed()
            except Exception:
                field_settings_tab_visible = False

            self.session.emit_diag(
                Cat.SIDEBAR,
                "Sidebar state after configure",
                fields_tab_visible=fields_tab_visible,
                field_settings_tab_visible=field_settings_tab_visible,
                **self._editor_ctx(
                    field_id=handle.field_id,
                    section_id=handle.section_id,
                    kind="sidebar_state",
                    stage="post_configure",
                ),
            )
        except Exception:
            pass

    # ---------- title ----------

    def set_field_title(self, field_el, title_text: str) -> None:
        desired = self._norm_text(title_text)

        if desired == "":
            return

        fid = None
        try:
            fid = self.get_field_id_from_element(field_el)
        except Exception:
            fid = None
        ctx = self._editor_ctx(field_id=fid, kind="title")

        def _emit_title_step(step: str, start: float, **extra) -> None:
            try:
                self.session.emit_diag(
                    Cat.CONFIGURE,
                    "Step timing",
                    step=f"title_{step}",
                    elapsed_s=round(time.monotonic() - start, 3),
                    **ctx,
                    **extra,
                )
            except Exception:
                pass

        def _refresh_field_el():
            nonlocal field_el
            if fid:
                try:
                    fresh = self.get_field_by_id(fid)
                    if fresh is not None:
                        field_el = fresh
                except Exception:
                    pass

        def _read_display_title() -> str:
            try:
                h2 = field_el.find_element(By.CSS_SELECTOR, ".designer__field__editable-label--title h2.field__editable-label")
                return self._norm_text(h2.text or "")
            except Exception:
                return ""

        def _read_input_value_if_present() -> str:
            try:
                inp = field_el.find_element(By.CSS_SELECTOR, ".designer__field__editable-label--title input[name='title']")
                if inp.is_displayed():
                    return self._norm_text(inp.get_attribute("value") or "")
            except Exception:
                pass
            return ""

        # Fast path
        try:
            if _read_display_title() == desired:
                self.session.emit_diag(
                    Cat.CONFIGURE,
                    f"Field title already correct ({desired!r}); skipping title set.",
                    **ctx,
                )
                return
        except Exception:
            pass

        last_err: Exception | None = None

        for attempt in range(1, 4):
            try:
                _refresh_field_el()

                t_step = time.monotonic()
                title_display = field_el.find_element(
                    By.CSS_SELECTOR,
                    ".designer__field__editable-label--title h2.field__editable-label"
                )
                _emit_title_step(f"locate_display_a{attempt}", t_step)

                self.session.emit_diag(
                    Cat.CONFIGURE,
                    f"Setting field title (attempt {attempt}/3): {desired!r}",
                    **ctx,
                )

                # Activate editor
                t_step = time.monotonic()
                if not self.session.click_element_safely(title_display):
                    title_display.click()
                _emit_title_step(f"activate_a{attempt}", t_step)

                # Find the input directly (no closure variable)
                t_step = time.monotonic()
                title_input = field_el.find_element(
                    By.CSS_SELECTOR,
                    ".designer__field__editable-label--title input[name='title']"
                )

                WebDriverWait(self.driver, 2.0).until(lambda d: title_input.is_displayed() and title_input.is_enabled())
                _emit_title_step(f"input_ready_a{attempt}", t_step)

                t_step = time.monotonic()
                try:
                    title_input.clear()
                except Exception:
                    title_input.send_keys(Keys.CONTROL, "a")
                    title_input.send_keys(Keys.BACKSPACE)

                title_input.send_keys(title_text)
                title_input.send_keys(Keys.TAB)
                _emit_title_step(f"apply_text_a{attempt}", t_step)

                # Verification: prefer input value if still present; else read the display title
                t_step = time.monotonic()
                _refresh_field_el()
                observed = _read_input_value_if_present() or _read_display_title()
                _emit_title_step(f"verify_a{attempt}", t_step)

                if observed == desired:
                    self.session.emit_diag(
                        Cat.CONFIGURE,
                        f"Field title verified: {desired!r}",
                        **ctx,
                    )
                    return

                self.session.emit_signal(
                    Cat.CONFIGURE,
                    f"Field title not applied after attempt {attempt}/3 (wanted={desired!r} got={observed!r}).",
                    level="warning",
                    **ctx,
                )

                # Cleanly exit any half-open editor before retry
                try:
                    ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
                except Exception:
                    pass

                time.sleep(0.2)

            except Exception as e:
                last_err = e
                self.session.emit_signal(
                    Cat.CONFIGURE,
                    f"Field title set attempt {attempt}/3 failed: {e}",
                    level="warning",
                    **ctx,
                )

                try:
                    ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
                except Exception:
                    pass

                time.sleep(0.2)
                _refresh_field_el()

        msg = f"title set failed after retries (wanted={desired!r})"
        if last_err is not None:
            msg += f": {type(last_err).__name__}: {last_err}"

        self.session.emit_signal(
            Cat.CONFIGURE,
            f"Could not set field title: {msg}",
            level="warning",
            **ctx,
        )

        self._record_config_skip(
            kind="configure",
            reason=msg,
            retryable=True,
            field_id=fid,
            field_title=title_text,
            requested={"title": title_text},
        )

    # ---------- body (Froala) ----------
    # ---------- Froala helpers ----------

    def _wait_turbo_idle(self, timeout: float = 3.0) -> bool:
        """
        Best-effort: wait for Turbo to not be busy.
        This is not perfect, but it helps us avoid verifying during hydration.
        """
        driver = self.driver
        end = time.time() + timeout
        last = None

        while time.time() < end:
            try:
                data = driver.execute_script(
                    """
                    const busyFrames = document.querySelectorAll('turbo-frame[busy]').length;
                    const progress = !!document.querySelector('[data-turbo-progress-bar], .turbo-progress-bar');
                    return {busyFrames: busyFrames, progress: progress};
                    """
                ) or {}
                last = data
                if int(data.get("busyFrames", 0)) == 0 and not bool(data.get("progress", False)):
                    return True
            except Exception:
                pass

            time.sleep(0.08)

        self.session.emit_diag(
            Cat.FROALA,
            f"Turbo idle wait timed out (last={last!r}).",
            **self._editor_ctx(kind="turbo_idle"),
        )
        return False

    def _read_froala_block_state(
        self,
        field_el,
        *,
        block_selector: str,
        textarea_selector: str,
    ) -> dict:
        """
        JS-first read of editor.innerHTML and textarea.value (if present).
        Returns: {ok, editorHtml, textareaVal, reason}
        """
        driver = self.driver
        script_get = """
        const field = arguments[0];
        const blockSelector = arguments[1];
        const textareaSelector = arguments[2];

        if (!field) return {ok:false, reason:'no_field'};

        const block = field.querySelector(blockSelector);
        if (!block) return {ok:false, reason:'no_block'};

        const container = block.querySelector('.designer__field__editable-label__container') || block;
        const editor = container.querySelector('.fr-element.fr-view[contenteditable="true"]');
        if (!editor) return {ok:false, reason:'no_editor'};

        const taPrimary = container.querySelector(textareaSelector);

        // broader fallback: any textarea named description within the whole field
        const taAny = field.querySelector("textarea[name='description']");

        // helper to describe textarea
        function taInfo(ta) {
        if (!ta) return null;
        const attrs = {};
        for (const a of ta.attributes) {
            if (a && a.name) attrs[a.name] = a.value;
        }
        return {
            value: ta.value || "",
            id: ta.id || null,
            name: ta.getAttribute("name") || null,
            classes: ta.className || "",
            attrs: attrs
        };
        }

        return {
        ok: true,
        editorHtml: editor.innerHTML || "",
        textareaPrimary: taInfo(taPrimary),
        textareaAny: taInfo(taAny),
        };
        """
        try:
            return driver.execute_script(script_get, field_el, block_selector, textarea_selector) or {}
        except Exception as e:
            return {"ok": False, "reason": f"exec_error:{type(e).__name__}"}
        
    def _read_description_block_state(self, field_el) -> dict:
        """
        Convenience: read the 'description' Froala block (Paragraph/Long Answer body, etc.)
        using the default selectors used in set_field_body().
        """
        return self._read_froala_block_state(
            field_el,
            block_selector=(
                ".designer__field__editable-label--description"
                "[id^='designer__field__description--']"
            ),
            textarea_selector=(
                "textarea[name='description']"
                # "textarea.froala-editor[name='description']"
                # "[data-froala-save-source-value*='field_type=description']"
            ),
        )
    
    def _probe_body_persistence(
        self,
        *,
        field_id: str | None,
        field_el=None,
        desired_html: str | None,
        phase: str,
        allow_refind: bool = True,
        turbo_idle_timeout: float = 3.0,
        tries: int = 3,
    ) -> str:
        """
        Probe whether desired_html's signature is currently present in the Froala description block.
        Tri-state probe for Froala description block.

        Returns one of:
        - PROBE_PRESENT: signature seen (editor or textarea) on a fresh node
        - PROBE_MISSING: signature not seen on a fresh node
        - PROBE_UNKNOWN: couldn't read reliably (stale / no editor / turbo churn)
        """
        ctx = self._editor_ctx(field_id=field_id, kind="body_probe", stage=phase)

        if desired_html is None:
            return PROBE_PRESENT  # nothing to check

        desired_sig = self._froala_sig(str(desired_html))
        if not desired_sig:
            return PROBE_PRESENT  # empty segnature means nothing to check

        last_reason = None

        for attempt in range(1, tries + 1):
            # Always prefer refind by id to beat Turbo swaps
            if allow_refind and field_id:
                try:
                    field_el = self.get_field_by_id(field_id)
                except Exception as e:
                    last_reason = f"refind:{type(e).__name__}"
                    field_el = field_el  # keep what we had

            if field_el is None:
                last_reason = "no_field_el"
                time.sleep(0.12)
                continue

            self._wait_turbo_idle(timeout=turbo_idle_timeout)

            try:
                state = self._read_description_block_state(field_el)
            except StaleElementReferenceException:
                last_reason = "stale_field_el"
                time.sleep(0.12)
                continue
            except Exception as e:
                last_reason = f"read_exc:{type(e).__name__}"
                time.sleep(0.12)
                continue

            if not state.get("ok"):
                last_reason = state.get("reason") or "read_not_ok"
                time.sleep(0.12)
                continue

            ta_primary = (state.get("textareaPrimary") or {}).get("value") if state.get("textareaPrimary") else None
            ta_any = (state.get("textareaAny") or {}).get("value") if state.get("textareaAny") else None
            source = "ta_primary" if ta_primary is not None else "ta_any" if ta_any is not None else "editor"
            ed_html = state.get("editorHtml") or ""

            if ta_primary is not None:
                present = desired_sig in self._froala_sig(ta_primary)
            elif ta_any is not None:
                present = desired_sig in self._froala_sig(ta_any)
            else:
                present = desired_sig in self._froala_sig(ed_html)

            if present:
                self.session.emit_diag(
                    Cat.FROALA,
                    f"Body probe ({phase}): present (field_id={field_id!r} sig={desired_sig!r} attempt={attempt} source={source}).",
                    **ctx,
                )
                return PROBE_PRESENT

            # We successfully read a fresh node and it's not there => definite missing
            ed_len = len(ed_html)

            ta_present = (ta_primary is not None) or (ta_any is not None)
            ta_val = ta_primary if ta_primary is not None else ta_any
            ta_len = len(ta_val) if isinstance(ta_val, str) else None

            self.session.emit_signal(
                Cat.FROALA,
                (
                    "Body probe ({phase}): MISSING (field_id={field_id!r} sig={sig!r} attempt={attempt}) "
                    "source={source} ta_present={ta_present} ta_len={ta_len} ed_len={ed_len}"
                ).format(
                    phase=phase,
                    field_id=field_id,
                    sig=desired_sig,
                    attempt=attempt,
                    source=source,
                    ta_present=ta_present,
                    ta_len=ta_len,
                    ed_len=ed_len,
                ),
                level="warning",
                **ctx,
            )
            return PROBE_MISSING

        # never got a reliable read
        self.session.emit_diag(
            Cat.FROALA,
            f"Body probe ({phase}): UNKNOWN (field_id={field_id!r} sig={desired_sig!r} last_reason={last_reason!r}).",
            **ctx,
        )
        return PROBE_UNKNOWN
    
    def _verify_body_after_properties_and_recover_once(
        self,
        *,
        handle,
        desired_html: str | None,
    ) -> None:
        """
        Post-properties guard: if body was lost, re-apply once to stabilize the build,
        while still leaving a clear warning trail in logs.
        """
        if desired_html is None:
            return

        fid = getattr(handle, "field_id", None)

        result = self._probe_body_persistence(
            field_id=fid,
            desired_html=desired_html,
            phase="post-props",
            allow_refind=True,
            tries=3,
        )

        if result == PROBE_PRESENT:
            return

        if result == PROBE_UNKNOWN:
            # Don't “fix” on uncertainty – that hides real causes and adds churn.        
            self.session.emit_signal(
                Cat.FROALA,
                f"Post-props body probe unknown (field_id={fid!r}). Skipping recovery.",
                level="warning",
                **self._editor_ctx(field_id=fid, kind="body_probe", stage="post-props"),
            )
            return
        
        # only here if definite
        self.session.emit_signal(
            Cat.FROALA,
            f"Body LOST after properties (definite) field_id={fid!r}. Re-applying body once.",
            level="warning",
            **self._editor_ctx(field_id=fid, kind="body_probe", stage="post-props"),
        )

        # Re-apply and let _set_froala_block do its persisted+verified routine
        field_el = self.get_field_by_id(fid) if fid else None
        self.set_field_body(field_el, desired_html)

        # signature used for containment checks (Froala normalizes HTML)
    def _froala_sig(self,s: str, max_len: int = 60) -> str:
        """
        Normalize HTML/text to a small stable signature for containment checks.
        Froala/CA may normalize tags/whitespace, so we compare a text signature.
        """
        s = (s or "").strip()
        s = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", s)
        s = re.sub(r"<[^>]+>", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s[:max_len]

    def audit_bodies_now(
        self,
        expected_by_field_id: dict[str, str],
        *,
        label: str = "end-audit",
        max_report: int = 50,
    ) -> dict:
        """
        Re-find each field_id and read its description Froala block *now*.
        Returns summary dict and logs missing/mismatched bodies.

        expected_by_field_id: {field_id: expected_html}
        """
        if self.session.instr_policy.mode == LogMode.LIVE:
            self.session.counters.inc("editor.body_audit_skipped")
            return {"ok": 0, "missing": [], "unknown": []}

        ctx = self._editor_ctx(kind="body_audit", stage=label)
        self._wait_turbo_idle(timeout=5.0)

        missing = []
        unknown = []
        ok = 0

        def sig_full(html: str) -> str:
            # stronger signature than 60 chars to avoid collisions
            txt = re.sub(r"<[^>]+>", "", (html or ""))
            txt = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", txt)
            txt = re.sub(r"\s+", " ", txt).strip()
            # include length + head/tail
            if not txt:
                return "0:"
            head = txt[:80]
            tail = txt[-40:] if len(txt) > 120 else ""
            return f"{len(txt)}:{head}|{tail}"

        for field_id, expected_html in expected_by_field_id.items():
            exp_sig = sig_full(expected_html)

            try:
                field_el = self.get_field_by_id(field_id)
            except Exception as e:
                unknown.append((field_id, f"refind:{type(e).__name__}"))
                continue

            state = self._read_description_block_state(field_el)
            if not state.get("ok"):
                unknown.append((field_id, state.get("reason") or "read_failed"))
                continue

            act_sig = sig_full(state.get("editorHtml") or state.get("textareaVal") or "")

            if exp_sig == act_sig:
                ok += 1
                continue

            # treat empty as missing
            if act_sig.startswith("0:"):
                missing.append((field_id, exp_sig, act_sig))
            else:
                missing.append((field_id, exp_sig, act_sig))

        # Log summary
        self.session.emit_diag(
            Cat.FROALA,
            f"Body audit ({label}): ok={ok} missing={len(missing)} unknown={len(unknown)} total={len(expected_by_field_id)}",
            **ctx,
        )

        # Log details (bounded)
        for field_id, exp_sig, act_sig in missing[:max_report]:
            self.session.emit_signal(
                Cat.FROALA,
                f"Body audit ({label}): MISMATCH field_id={field_id} exp={exp_sig!r} act={act_sig!r}",
                level="warning",
                **ctx,
            )

        for field_id, reason in unknown[:max_report]:
            self.session.emit_diag(
                Cat.FROALA,
                f"Body audit ({label}): UNKNOWN field_id={field_id} reason={reason!r}",
                **ctx,
            )

        return {"ok": ok, "missing": missing, "unknown": unknown}

    def _set_froala_block(
        self,
        field_el,
        block_selector: str,
        textarea_selector: str,
        html: str,
        log_label: str = "Froala block",
    ) -> None:
        """
        Robust Froala setter with *persistence* verification.

        Guarantees we only log success after:
        1) JS inject + commit events
        2) Turbo idle (best effort)
        3) Re-find field by id and re-read content (survives stale swap)
        """
        driver = self.driver
        wait = self.wait

        if html is None:
            return

        desired = str(html)

        desired_sig = self._froala_sig(desired)
        if desired_sig == "":
            return

        fid = None
        try:
            fid = self.get_field_id_from_element(field_el)
        except Exception:
            fid = None
        ctx = self._editor_ctx(field_id=fid, kind="froala", stage=log_label)

        def _emit_froala_step(step: str, start: float, **extra) -> None:
            try:
                self.session.emit_diag(
                    Cat.FROALA,
                    "Step timing",
                    step=step,
                    elapsed_s=round(time.monotonic() - start, 3),
                    block=log_label,
                    **ctx,
                    **extra,
                )
            except Exception:
                pass

        def _refind_field():
            nonlocal field_el
            if fid:
                try:
                    fresh = self.get_field_by_id(fid)
                    if fresh is not None:
                        field_el = fresh
                except Exception:
                    pass

        def _contains_signature(state: dict) -> bool:
            if not state or not state.get("ok"):
                return False
            editor_html = state.get("editorHtml") or ""
            textarea_val = state.get("textareaVal") or ""
            editor_sig = self._froala_sig(editor_html)
            textarea_sig = self._froala_sig(textarea_val)
            # Accept if signature shows in either editor DOM or textarea backing store.
            return (desired_sig in editor_sig) or (desired_sig in textarea_sig)

        script_set = """
            const field = arguments[0];
            const value = arguments[1];
            const blockSelector = arguments[2];
            const textareaSelector = arguments[3];

            if (!field) return {ok:false, reason:'no_field'};

            const block = field.querySelector(blockSelector);
            if (!block) return {ok:false, reason:'no_block'};

            const container = block.querySelector('.designer__field__editable-label__container') || block;

            // Prefer textarea as the authoritative "save source"
            const ta = container.querySelector(textareaSelector);

            // If Froala instance exists, use its API (more likely to trigger CA wiring)
            let froalaEditor = null;
            try {
              if (window.jQuery && ta) {
                const inst = window.jQuery(ta).data('froala.editor');
                if (inst) froalaEditor = inst;
              }
            } catch (e) {}

            // Editor element for event dispatch / visual state
            const editorEl = container.querySelector('.fr-element.fr-view[contenteditable="true"]');
            if (!editorEl) return {ok:false, reason:'no_editor'};

            if (froalaEditor) {
              try {
                froalaEditor.html.set(value);
                froalaEditor.events.trigger('contentChanged');
                froalaEditor.events.trigger('keyup');
              } catch (e) {}
            } else {
              editorEl.innerHTML = value;
            }

            // Placeholder cleanup (cosmetic but also avoids "empty" heuristics)
            const wrapper = editorEl.closest('.fr-wrapper');
            if (wrapper && wrapper.classList.contains('show-placeholder')) {
              wrapper.classList.remove('show-placeholder');
            }
            const placeholder = wrapper ? wrapper.querySelector('.fr-placeholder') : null;
            if (placeholder) placeholder.style.display = 'none';

            // Update textarea backing store if present
            if (ta) {
              ta.value = value;

              // Mimic other CA inputs: keyup reflect + keydown enter save + blur save fallback
              try { ta.dispatchEvent(new Event('input', {bubbles:true})); } catch(e) {}
              try { ta.dispatchEvent(new Event('change', {bubbles:true})); } catch(e) {}
              try { ta.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true})); } catch(e) {}
              try { ta.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', keyCode:13, which:13, bubbles:true})); } catch(e) {}
              try { ta.dispatchEvent(new FocusEvent('blur', {bubbles:true})); } catch(e) {}
            }

            // Also dispatch on editorEl (some wiring listens here)
            try { editorEl.dispatchEvent(new Event('input', {bubbles:true})); } catch(e) {}
            try { editorEl.dispatchEvent(new Event('change', {bubbles:true})); } catch(e) {}
            try { editorEl.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true})); } catch(e) {}
            try { editorEl.dispatchEvent(new FocusEvent('blur', {bubbles:true})); } catch(e) {}

            // If we had Froala instance, explicitly blur it (often commits on blur)
            if (froalaEditor) {
              try { froalaEditor.events.trigger('blur'); } catch(e) {}
              try { froalaEditor.$el && froalaEditor.$el.blur && froalaEditor.$el.blur(); } catch(e) {}
            }

            return {ok:true, reason:'set'};
        """

        # Wait for block/editor presence (but don't rely on element stability beyond that)
        def _block_and_editor_present(_):
            try:
                block = field_el.find_element(By.CSS_SELECTOR, block_selector)
                _ = block.find_element(By.CSS_SELECTOR, ".fr-element.fr-view[contenteditable='true']")
                return True
            except Exception:
                return False

        try:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field_el)
            except Exception:
                pass

            t_step = time.monotonic()
            wait.until(_block_and_editor_present)
            _emit_froala_step("froala_block_ready", t_step)

            last_state = None
            last_reason = None

            for attempt in range(1, 4):
                try:
                    t_step = time.monotonic()
                    res = driver.execute_script(script_set, field_el, desired, block_selector, textarea_selector) or {}
                    _emit_froala_step(
                        f"froala_js_set_a{attempt}",
                        t_step,
                        ok=res.get("ok"),
                        reason=res.get("reason"),
                    )
                    if not res.get("ok"):
                        last_reason = res.get("reason")
                        self.session.emit_diag(
                            Cat.FROALA,
                            f"{log_label}: JS set failed (attempt {attempt}): {last_reason}",
                            **ctx,
                        )
                        _refind_field()
                        time.sleep(0.18)
                        continue

                    # Defocus to encourage commit
                    t_step = time.monotonic()
                    try:
                        canvas = driver.find_element(By.CSS_SELECTOR, "#section-fields")
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", canvas)
                        canvas.click()
                    except Exception:
                        pass
                    _emit_froala_step(f"froala_defocus_a{attempt}", t_step)

                    # Best-effort allow Turbo patch/hydration
                    t_step = time.monotonic()
                    self._wait_turbo_idle(timeout=2.5)
                    _emit_froala_step(f"froala_idle_1_a{attempt}", t_step)

                    # Read immediately (current node)
                    t_step = time.monotonic()
                    state1 = self._read_froala_block_state(
                        field_el,
                        block_selector=block_selector,
                        textarea_selector=textarea_selector,
                    )
                    last_state = state1
                    ok1 = _contains_signature(state1)
                    _emit_froala_step(f"froala_verify_1_a{attempt}", t_step, ok=ok1)
                    if not ok1:
                        self.session.emit_diag(
                            Cat.FROALA,
                            (
                                f"{log_label}: verify-1 failed (attempt {attempt}). "
                                f"desired_sig={desired_sig!r} state={{'ok': {state1.get('ok')!r}, 'reason': {state1.get('reason')!r}}}"
                            ),
                            **ctx,
                        )
                        _refind_field()
                        time.sleep(0.18)
                        continue

                    # Re-find field (forces us to survive Turbo swaps) and verify again
                    t_step = time.monotonic()
                    _refind_field()
                    self._wait_turbo_idle(timeout=2.5)
                    _emit_froala_step(f"froala_refind_idle_a{attempt}", t_step)

                    t_step = time.monotonic()
                    state2 = self._read_froala_block_state(
                        field_el,
                        block_selector=block_selector,
                        textarea_selector=textarea_selector,
                    )
                    last_state = state2
                    ok2 = _contains_signature(state2)
                    _emit_froala_step(f"froala_verify_2_a{attempt}", t_step, ok=ok2)
                    if ok2:
                        self.session.emit_diag(
                            Cat.FROALA,
                            f"{log_label} set successfully (persisted + verified).",
                            **ctx,
                        )
                        return

                    self.session.emit_diag(
                        Cat.FROALA,
                        (
                            f"{log_label}: verify-2 failed after refind (attempt {attempt}). "
                            f"desired_sig={desired_sig!r} state={{'ok': {state2.get('ok')!r}, 'reason': {state2.get('reason')!r}}}"
                        ),
                        **ctx,
                    )

                except StaleElementReferenceException:
                    _refind_field()
                except Exception as e:
                    last_reason = f"{type(e).__name__}: {e}"

                time.sleep(0.18)
                _refind_field()

            # If we get here: did not persist
            self.session.emit_signal(
                Cat.FROALA,
                (
                    f"{log_label}: FAILED to persist after retries. "
                    f"desired_sig={desired_sig!r} last_reason={last_reason!r} last_state_ok={(last_state or {}).get('ok')!r}"
                ),
                level="warning",
                **ctx,
            )

        except TimeoutException as e:
            self.session.emit_signal(
                Cat.FROALA,
                (
                    f"{log_label}: TIMEOUT waiting for Froala block/editor. "
                    f"block_selector={block_selector!r} textarea_selector={textarea_selector!r} "
                    f"({type(e).__name__}: {e})"
                ),
                level="warning",
                **ctx,
            )
            raise

    def set_field_body(self, field_el, body_text: str):
        """
        Set the main description/body Froala block for this field.
        """
        # Only target the main description block, not model answer
        block_selector = (
            ".designer__field__editable-label--description"
            "[id^='designer__field__description--']"
        )
        textarea_selector = (
            "textarea.froala-editor[name='description']"
            "[data-froala-save-source-value*='field_type=description']"
        )
        self._set_froala_block(
            field_el,
            block_selector=block_selector,
            textarea_selector=textarea_selector,
            html=body_text,
            log_label="Field body",
        )

    # ---------- thin wrappers ----------

    def _set_signature_config_specifics(self, config: SignatureConfig):
        """
        Internal helper to set signature-field-specific options.
        Always returns (learner_visibility, assessor_visibility, required).
        """

        # 1) Start from explicit values if provided
        learner_visibility = config.learner_visibility
        assessor_visibility = config.assessor_visibility

        # 2) If either is missing, derive defaults from role (and fill only what's missing)
        role = (config.role or "").strip().lower()

        if learner_visibility is None or assessor_visibility is None:
            if role == "learner":
                default_lv, default_av = "update", "read"
            elif role == "assessor":
                default_lv, default_av = "read", "update"
            elif role == "both":
                default_lv, default_av = "update", "update"
            else:
                # Safe fallback if role is missing/unknown
                default_lv, default_av = "read", "read"

            if learner_visibility is None:
                learner_visibility = default_lv
            if assessor_visibility is None:
                assessor_visibility = default_av

        # 3) Required default
        sig_required = config.required if config.required is not None else True

        return learner_visibility, assessor_visibility, sig_required

    def _configure_single_choice_answers(
        self,
        field_el,
        options: list[str] | None,
        correct_index: int | None,
    ) -> None:
        """
        Configure Single Choice options and the correct answer selection.

        - Ensures the number of options matches `options`.
        - Sets each option label.
        - Sets exactly one correct answer (CA warns if none selected).

        Assumes:
        - field_el is the active single choice field container on canvas
        - title/description already handled elsewhere (optional)
        """
        driver = self.driver
        wait = self.wait

        options = options or []
        if not options:
            self.session.emit_diag(
                Cat.CONFIGURE,
                "Single choice: no options provided; skipping answers config.",
                **self._editor_ctx(kind="single_choice"),
            )
            return

        field_id = self.get_field_id_from_element(field_el)
        if not field_id:
            raise RuntimeError("Could not determine field_id for single choice field.")
        ctx = self._editor_ctx(field_id=field_id, kind="single_choice")

        sel = config.BUILDER_SELECTORS["single_choice"]
        answers_container_css = sel["answers_container"]
        add_choice_btn_css = sel["add_choice_button"]
        answer_row_css = sel["answer_rows"]
        answer_text_input_css = sel["answer_text_input"]
        correct_checkbox_css = sel["correct_checkbox"]
        delete_option_link_css = sel["delete_option_link"]

        def get_rows():
            # Re-scope to container each time to avoid stale references
            cont = driver.find_element(By.CSS_SELECTOR, answers_container_css)
            return cont.find_elements(By.CSS_SELECTOR, answer_row_css)

        def click_add_choice() -> None:
            # The Add choice button is inside the field element, but it’s safe to locate by selector near the field
            btns = field_el.find_elements(By.CSS_SELECTOR, add_choice_btn_css)
            if not btns:
                # fallback: global search (rare)
                btns = driver.find_elements(By.CSS_SELECTOR, add_choice_btn_css)
            if not btns:
                raise RuntimeError("Single choice: could not find 'Add choice' button.")
            btn = btns[0]
            before = len(get_rows())
            self.session.click_element_safely(btn)
            wait.until(lambda d: len(get_rows()) == before + 1)

        def delete_last_choice() -> None:
            rows = get_rows()
            if not rows:
                return
            row = rows[-1]
            links = row.find_elements(By.CSS_SELECTOR, delete_option_link_css)
            if not links:
                raise RuntimeError("Single choice: could not find delete link for extra option.")
            before = len(rows)
            self.session.click_element_safely(links[0])
            wait.until(lambda d: len(get_rows()) == before - 1)

        # 1) Ensure correct number of options
        # Add missing
        for _ in range(max(0, len(options) - len(get_rows()))):
            click_add_choice()

        # Remove extras
        while len(get_rows()) > len(options):
            delete_last_choice()

        # 2) Set option texts
        rows = get_rows()
        for idx, label in enumerate(options):
            label = (label or "").strip()
            row = rows[idx]

            # --- Activate edit mode for this option row (prove it) ---
            # Click the display <h4> (this triggers Helpers.Designer.toggleFieldInput)
            try:
                display = row.find_element(By.CSS_SELECTOR, "h4.field__editable-label")
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", display)
                self.session.click_element_safely(display)
            except Exception:
                # best-effort: some layouts need clicking the wrapper
                try:
                    wrapper = row.find_element(By.CSS_SELECTOR, ".designer__field__editable-label--question")
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", wrapper)
                    self.driver.execute_script("arguments[0].click();", wrapper)
                except Exception:
                    pass

            # Wait until the wrapper becomes active (designer__field__editable-label--active)
            def _row_active() -> bool:
                try:
                    w = row.find_element(By.CSS_SELECTOR, ".designer__field__editable-label--question")
                    cls = (w.get_attribute("class") or "")
                    return "designer__field__editable-label--active" in cls
                except Exception:
                    return False

            try:
                wait.until(lambda d: _row_active())
            except Exception:
                self.session.emit_diag(
                    Cat.CONFIGURE,
                    f"Single choice: option row {idx} did not enter active edit mode promptly.",
                    **ctx,
                )

            # --- Now locate the real option input (more specific) ---
            # Prefer field_answers input, which is the option-title input.
            inputs = row.find_elements(
                By.CSS_SELECTOR,
                "input[type='text'][data-ajax-input-value-url-value*='/field_answers/']"
            )
            if not inputs:
                # fallback to your existing selector if needed
                inputs = row.find_elements(By.CSS_SELECTOR, answer_text_input_css)

            if not inputs:
                raise RuntimeError(f"Single choice: option row {idx} has no text input.")

            inp = inputs[0]
            try:
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", inp)
            except Exception:
                pass

            # Type + blur (blur triggers ajax-input-value#sendRequest in your DOM)
            self.session.clear_and_type(inp, label)
            try:
                self.driver.execute_script("arguments[0].blur();", inp)
            except Exception:
                pass

            # Best-effort readback (some UIs update after blur)
            try:
                wait.until(lambda d: (inp.get_attribute("value") or "").strip() == label)
            except Exception:
                self.session.emit_diag(
                    Cat.CONFIGURE,
                    f"Single choice: option {idx} value did not read back immediately.",
                    **ctx,
                )

            # 3) Set correct answer
            # CA complains if none selected, so default to first if not specified
            if correct_index is None:
                correct_index = 0
            # Normalize correct_index to a definite int
            ci: int = correct_index if correct_index is not None else 0

            if ci < 0 or ci >= len(options):
                raise ValueError(
                    f"Single choice: correct_index {ci} out of range for {len(options)} option(s)."
                )

            def _get_checkbox_for_row(row_el):
                checks = row_el.find_elements(By.CSS_SELECTOR, correct_checkbox_css)
                return checks[0] if checks else None

            def _row_checkbox_selected(row_el) -> bool:
                cb = _get_checkbox_for_row(row_el)
                return bool(cb and cb.is_selected())

            # Re-fetch rows (avoid stales)
            rows = get_rows()

            # First, clear any existing correct selections
            for i in range(len(rows)):
                # always re-fetch inside loop to avoid stale rows after ajax
                rows = get_rows()
                row = rows[i]
                cb = _get_checkbox_for_row(row)
                if not cb:
                    continue

                try:
                    if cb.is_selected():
                        try:
                            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cb)
                        except Exception:
                            pass

                        self.session.click_element_safely(cb)

                        # Prove unchecked by re-checking on fresh DOM
                        wait.until(lambda d: not _row_checkbox_selected(get_rows()[i]))
                except StaleElementReferenceException:
                    # If ajax swaps nodes, just continue; next iteration re-fetches
                    continue

            # Now set the desired correct selection
            rows = get_rows()
            target_row = rows[ci]
            cb = _get_checkbox_for_row(target_row)
            if not cb:
                raise RuntimeError("Single choice: could not find correct checkbox on target option row.")

            if not cb.is_selected():
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cb)
                except Exception:
                    pass

                self.session.click_element_safely(cb)

                # Prove checked using fresh DOM element (avoids stale cb reference)
                def _is_correct_selected() -> bool:
                    rows = get_rows()
                    if ci >= len(rows):
                        return False
                    return _row_checkbox_selected(rows[ci])

                wait.until(lambda d: _is_correct_selected())

        self.session.emit_diag(
            Cat.CONFIGURE,
            f"Single choice answers configured: options={len(options)} correct_index={correct_index} field_id={field_id}",
            **ctx,
        )

    # ---------- field settings: open sidebar ----------
    def _open_field_settings_sidebar(self, field_el, timeout: int = 5, pivot_el=None, force_reopen: bool = False) -> None:
        driver = self.driver
        wait = self.session.get_wait(timeout)
        ctx = self._editor_ctx(kind="field_settings")

        def _defocus():
            try:
                driver.switch_to.active_element.send_keys(Keys.ESCAPE)
            except Exception:
                pass
            try:
                canvas = driver.find_element(By.CSS_SELECTOR, "#section-fields")
                canvas.click()
            except Exception:
                pass

        def _defocus_and_close_best_effort():
            try:
                driver.switch_to.active_element.send_keys(Keys.ESCAPE)
            except Exception:
                pass
            try:
                canvas = driver.find_element(By.CSS_SELECTOR, "#section-fields")
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", canvas)
                canvas.click()
            except Exception:
                pass

        def _tab_visible(_):
            try:
                tab = driver.find_element(By.CSS_SELECTOR, ".designer__sidebar__tab[data-type='field-settings']")
                return tab.is_displayed()
            except Exception:
                return False

        def _frame_loaded(_):
            # Use the looser “open for field” check (now non-fatal on id confirm)
            try:
                frame = driver.find_element(By.CSS_SELECTOR, "turbo-frame#field_settings_frame")
                controls = frame.find_elements(By.CSS_SELECTOR, "input, select, textarea, button")
                if not controls:
                    return False
                return self._is_field_settings_open_for_field(field_el)
            except Exception:
                return False

        # Fast path
        try:
            if (not force_reopen) and self._is_field_settings_open_for_field(field_el):
                self.session.emit_diag(
                    Cat.UISTATE,
                    "Field settings sidebar already open for this field; skipping open.",
                    **ctx,
                )
                return
        except Exception:
            pass

        for attempt in range(1, 3):
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field_el)
                self.session.emit_diag(
                    Cat.UISTATE,
                    f"Opening Field settings sidebar for selected field... (attempt {attempt}/2)",
                    **ctx,
                )

                _defocus()
                if pivot_el is not None:
                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pivot_el)
                        ActionChains(driver).move_to_element(pivot_el).move_by_offset(8, 8).pause(0.05).click().perform()
                        time.sleep(0.15)
                    except Exception:
                        pass                
                clicked = False
                try:
                    # Prefer an offset click (avoids table cells/title editor hit targets)
                    ActionChains(driver).move_to_element(field_el).move_by_offset(8, 8).pause(0.05).click().perform()
                    clicked = True
                except Exception:
                    clicked = False

                if not clicked:
                    # fall back to your existing safe click then JS click
                    try:
                        clicked = self.session.click_element_safely(field_el)
                    except Exception:
                        clicked = False
                    if not clicked:
                        driver.execute_script("arguments[0].click();", field_el)

                wait.until(_tab_visible)
                wait.until(_frame_loaded)

                self.session.emit_diag(
                    Cat.UISTATE,
                    "Field settings sidebar is open and settings frame is loaded.",
                    **ctx,
                )
                return

            except TimeoutException as e:
                self.session.emit_signal(
                    Cat.UISTATE,
                    f"Timed out waiting for field settings sidebar (attempt {attempt}/2): {e}",
                    level="warning",
                    **ctx,
                )
                if attempt < 2:
                    _defocus_and_close_best_effort()
                    time.sleep(0.25)
                    continue
                raise

            except WebDriverException as e:
                self.session.emit_signal(
                    Cat.UISTATE,
                    f"WebDriver error while opening field settings sidebar (attempt {attempt}/2): {e}",
                    level="warning",
                    **ctx,
                )
                if attempt < 2:
                    _defocus_and_close_best_effort()
                    time.sleep(0.25)
                    continue
                raise

    def _get_field_settings_frame(self, timeout: int = 3):
        """
        Return the turbo-frame element that contains the field settings
        for the currently selected field.
        """
        ctx = self._editor_ctx(kind="field_settings_frame")
        wait = self.session.get_wait(timeout)
        try:
            return wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "turbo-frame#field_settings_frame"))
        except Exception as e:
            self.session.emit_signal(
                Cat.UISTATE,
                f"Could not locate field_settings_frame: {e}",
                level="warning",
                **ctx,
            )
            raise

    def _is_field_settings_open_for_field(self, field_el) -> bool:
        """
        True only if the properties frame is loaded and 
        the bound field id matches the expected field id
        """
        driver = self.driver

        # 1) field-settings tab visible
        try:
            tab = driver.find_element(By.CSS_SELECTOR, ".designer__sidebar__tab[data-type='field-settings']")
            if not tab.is_displayed():
                return False
        except Exception as e:
            self.session.emit_diag(
                Cat.UISTATE,
                f"Failed to find tab. Reason: {e!r}",
                **self._editor_ctx(kind="ui_state", stage="tab"),
            )
            return False

        # 2) frame present + loaded-ish (cheap: any inputs exist)
        try:
            frame = driver.find_element(By.CSS_SELECTOR, "turbo-frame#field_settings_frame")
        except Exception as e:
            self.session.emit_diag(
                Cat.UISTATE,
                f"Failed to find frame. Reason: {e!r}",
                **self._editor_ctx(kind="ui_state", stage="frame"),
            )
            return False

        try:
            # If your "hide_in_report" checkbox isn't universal, use a softer signal:
            # any input/select/textarea inside frame.
            loaded_controls = frame.find_elements(By.CSS_SELECTOR, "input, select, textarea, button")
            if not loaded_controls:
                return False
        except Exception as e:
            self.session.emit_diag(
                Cat.UISTATE,
                f"Failed to load controls. Reason: {e!r}",
                **self._editor_ctx(kind="ui_state", stage="controls"),
            )
            return False

        # 3) STRICT: prove "is this the right field?"
        try:
            field_id = self.try_get_field_id_strict(field_el)

            # Optional fallback: if strict couldn't extract, try the non-selected fallback version.
            # This is still safe because we are NOT using ".designer__field--selected" here.
            if not field_id:
                try:
                    field_id = self.get_field_id_from_element(field_el)
                except Exception:
                    field_id = None

            if not field_id:
                ctx = self._editor_ctx(kind="ui_state", stage="missing_expected")
                self.session.counters.inc("editor.ui_state_missing_expected")
                self.session.emit_diag(
                    Cat.UISTATE,
                    "UI_STATE missing expected field_id",
                    **ctx,
                )
                return False

            html = frame.get_attribute("innerHTML") or ""
            m = re.search(r"/fields/(\d+)\.turbo_stream", html)
            if not m:
                # If the frame doesn't expose a field id, we cannot prove binding.
                ctx = self._editor_ctx(kind="ui_state", stage="missing_observed_html", field_id=field_id)
                self.session.counters.inc("editor.ui_state_missing_html")
                self.session.emit_diag(
                    Cat.UISTATE,
                    "UI_STATE missing observed field_id in frame html",
                    **ctx,
                )
                return False

            observed = self._observed_field_id_from_settings_frame(frame)
            if not observed:
                ctx = self._editor_ctx(kind="ui_state", stage="missing_observed_controls", field_id=field_id)
                self.session.counters.inc("editor.ui_state_missing_control")
                self.session.emit_diag(
                    Cat.UISTATE,
                    "UI_STATE missing observed field_id from controls",
                    **ctx,
                )
                return False

            if observed != str(field_id):
                ctx = self._editor_ctx(kind="ui_state", stage="mismatch", field_id=field_id)
                self.session.counters.inc("editor.ui_state_mismatch")
                self.session.emit_diag(
                    Cat.UISTATE,
                    "UI_STATE binding mismatch",
                    expected=field_id,
                    observed=observed,
                    **ctx,
                )
                return False

            ctx = self._editor_ctx(kind="ui_state", stage="verified", field_id=field_id)
            self.session.counters.inc("editor.ui_state_proved")
            self.session.emit_diag(
                Cat.UISTATE,
                "UI_STATE binding proven",
                **ctx,
            )
            return True

        except Exception as e:
            ctx = self._editor_ctx(kind="ui_state", stage="exception")
            self.session.counters.inc("editor.ui_state_error")
            self.session.emit_diag(
                Cat.UISTATE,
                "UI_STATE binding proof exception",
                exc=str(e),
                **ctx,
            )
            return False

    # ---------- field properties: reporting, permissions etc. ----------

    def set_field_properties(
        self,
        field_el,
        pivot_el = None,
        *,
        hide_in_report: bool | None = None,
        learner_visibility: str | None = None,    # "hidden", "read", "update", "read-on-submit"
        assessor_visibility: str | None = None,   # "hidden", "read", "update"
        required: bool | None = None,
        marking_type: str | None = None,          # "manual", "not marked"
        enable_model_answer: bool | None = None, 
        enable_assessor_comments: bool | None = None,
    ):
        """
        Generic property setter that works for both paragraphs and long answer,
        and gracefully skips options that aren't present on a given field type.
        """
        driver = self.driver
        props = config.BUILDER_SELECTORS["properties"]

        missed: dict[str, str] = {}  # knob -> reason

        def _infer_field_type_key(field_el) -> str:
            # Matches your canvas selectors / field classes
            classes = (field_el.get_attribute("class") or "")
            if "designer__field--text_area" in classes:
                return "long_answer"
            if "designer__field--text_field" in classes:
                return "short_answer"
            if "designer__field--upload" in classes:
                return "file_upload"
            if "designer__field--table" in classes:
                return "interactive_table"
            if "designer__field--signature" in classes:
                return "signature"
            if "designer__field--date_field" in classes:
                return "date_field"
            if "designer__field--text" in classes:
                return "paragraph"
            if "designer__field--question" in classes:
                # single choice and other auto-marked questions land here; treat as generic question
                return "question"
            return "unknown"

        def _defocus_and_close_best_effort():
            # ESC to close tooltips / panels
            try:
                driver.switch_to.active_element.send_keys(Keys.ESCAPE)
            except Exception:
                pass

            # Click neutral area to defocus cell editors
            try:
                canvas = driver.find_element(By.CSS_SELECTOR, "#section-fields")
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", canvas)
                canvas.click()
            except Exception:
                pass

        def _get_loaded_frame_or_none():
            try:
                frame = driver.find_element(By.CSS_SELECTOR, "turbo-frame#field_settings_frame")
                controls = frame.find_elements(By.CSS_SELECTOR, "input, select, textarea, button")
                return frame if controls else None
            except Exception:
                return None
            
        def _log_misbind_probes(stage: str, *, attempt: int, retries: int, heavy: bool = False) -> None:
            """
            stage: short context label like 'loaded_frame_misbound' or 'sidebar_open_misbound'
            heavy: True => include frame HTML snippet etc
            """
            try:
                expected_id = self.try_get_field_id_strict(field_el)
            except Exception:
                expected_id = None
            try:
                expected_title = self.get_field_title(field_el)
            except Exception:
                expected_title = None

            # Light probe (always)
            try:
                probe = self.session.probe_ui_state(
                    label=f"{stage} attempt={attempt}/{retries}",
                    field_el=field_el,
                    expected_field_id=expected_id,
                    expected_title=expected_title,
                    include_frame_html_snippet=False,
                )
                self.session.log_ui_probe(probe, level="warning")
            except Exception:
                pass

            if not heavy:
                return

            # Heavy probe (only when asked)
            try:
                heavy_probe = self.session.probe_ui_state_heavy(
                    label=f"{stage} attempt={attempt}/{retries} (heavy)",
                    expected_field_id=expected_id,
                    expected_title=expected_title,
                    field_el=field_el,
                    include_frame_html_snippet=True,
                    frame_html_snippet_len=1200,
                    include_overlay_details=True,
                    include_canvas_snippet=False,  # keep off unless needed
                )
                self.session.log_ui_probe_heavy(heavy_probe, level="warning")
            except Exception:
                pass

        def _open_props_frame_with_retry(retries: int = 3):
            last_err = None
            saw_loaded_frame = False

            for attempt in range(1, retries + 1):
                self.session.counters.inc("editor.properties_open_attempts")
                try:
                    frame = _get_loaded_frame_or_none()
                    if frame is not None:
                        saw_loaded_frame = True
                        if self._is_field_settings_open_for_field(field_el):
                            self.session.counters.inc("editor.properties_frame_reuse")
                            self.session.counters.inc("editor.properties_binding_proven")
                            self.session.emit_diag(
                                Cat.UISTATE,
                                "UI_STATE: frame loaded and matches this field.",
                                key="UISTATE.binding.proven",
                                every_s=1.0,
                                **self._editor_ctx(field_id=fid, kind="ui_state", stage="frame_loaded"),
                            )
                            return frame
                        self.session.counters.inc("editor.properties_binding_mismatch")
                        self.session.emit_signal(
                            Cat.UISTATE,
                            f"UI_STATE: loaded frame is misbound (attempt {attempt}/{retries}).",
                            level="warning",
                            **self._editor_ctx(field_id=fid, kind="ui_state", stage="misbound_loaded"),
                        )
                        _log_misbind_probes("loaded_frame_misbound", attempt=attempt, retries=retries, heavy=False)

                    # try to open sidebar for this field
                    if self._ensure_field_active(field_el):
                        self._open_field_settings_sidebar(field_el, pivot_el=pivot_el, force_reopen=True)
                        frame = self._get_field_settings_frame()  # may throw TimeoutException internally
                        saw_loaded_frame = True

                        # binding proof gate
                        if self._is_field_settings_open_for_field(field_el):
                            self.session.counters.inc("editor.properties_opens")
                            self.session.counters.inc("editor.properties_binding_proven")
                            return frame

                        # mismatch -> log probes then recovery then retry
                        _log_misbind_probes("sidebar_open_misbound", attempt=attempt, retries=retries, heavy=True)

                        # mismatch -> recovery then retry
                        self.ui_state_recovery_count += 1
                        self.session.emit_signal(
                            Cat.UISTATE,
                            (
                                "UI_STATE: recovery {recovery} (attempt {attempt}/{retries}) "
                                "- sidebar misbound; will retry."
                            ).format(
                                recovery=self.ui_state_recovery_count,
                                attempt=attempt,
                                retries=retries,
                            ),
                            level="warning",
                            **self._editor_ctx(field_id=fid, kind="ui_state", stage="recovery"),
                        )
                        _defocus_and_close_best_effort()
                        continue

                    raise Exception("UI_STATE: couldn't make field active to open sidebar")

                except TimeoutException as e:
                    last_err = e
                    # This is a real load failure; we didn't get a usable frame
                    if attempt < retries:
                        self.session.emit_signal(
                            Cat.UISTATE,
                            f"UI_STATE: timeout loading sidebar frame (attempt {attempt}/{retries}).",
                            level="warning",
                            **self._editor_ctx(field_id=fid, kind="ui_state", stage="timeout"),
                        )
                        _defocus_and_close_best_effort()
                        continue
                    break

                except Exception as e:
                    last_err = e
                    if attempt < retries:
                        self.session.emit_signal(
                            Cat.UISTATE,
                            f"UI_STATE: error opening sidebar (attempt {attempt}/{retries}): {e}",
                            level="warning",
                            **self._editor_ctx(field_id=fid, kind="ui_state", stage="error"),
                        )
                        _defocus_and_close_best_effort()
                        continue
                    break

            # End of retries:
            if not saw_loaded_frame or isinstance(last_err, TimeoutException):
                # true inability to load the frame/controls
                raise FieldPropertiesSidebarTimeout(f"Could not open field settings sidebar/frame: {last_err}")

            # We saw a frame, but it never matched the expected field => UI_STATE mismatch => SKIP
            self.session.emit_signal(
                Cat.UISTATE,
                f"UI_STATE: could not prove binding after {retries} attempt(s); skipping property writes.",
                level="warning",
                **self._editor_ctx(field_id=fid, kind="ui_state", stage="binding_failure"),
            )
            self.session.counters.inc("editor.properties_binding_failed")
           
            # One final heavy snapshot (single-shot)
            _log_misbind_probes("final_misbound_skip", attempt=retries, retries=retries, heavy=True)
            
            return None

        def _radio_checked_value(root, name: str) -> str | None:
            try:
                el = root.find_element(By.CSS_SELECTOR, f"input[type='radio'][name='{name}']:checked")
                return el.get_attribute("value")
            except Exception:
                return None

        def _set_radio_by_value_with_verify(root, name: str, target_value: str, *, label: str) -> bool:
            # Find the target radio
            radios = root.find_elements(By.CSS_SELECTOR, f"input[type='radio'][name='{name}']")
            radio = next((r for r in radios if r.get_attribute("value") == target_value), None)
            if radio is None:
                self.session.emit_diag(
                    Cat.PROPS,
                    f"No {label} radio found for value {target_value!r} (skipping).",
                    **self._editor_ctx(field_id=fid, kind="properties"),
                )
                return False

            # Click + verify (one retry)
            for attempt in (1, 2):
                self.session.emit_diag(
                    Cat.PROPS,
                    f"Setting {label} to {target_value!r} (attempt {attempt}/2)...",
                    **self._editor_ctx(field_id=fid, kind="properties"),
                )
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", radio)
                    driver.execute_script("arguments[0].click();", radio)
                except Exception:
                    # fallback: native click
                    try:
                        radio.click()
                    except Exception:
                        pass

                time.sleep(0.1)
                current = _radio_checked_value(root, name)
                if current == target_value:
                    return True

            self.session.emit_signal(
                Cat.PROPS,
                f"Failed to set {label} to {target_value!r} (checked={_radio_checked_value(root, name)!r})",
                level="warning",
                **self._editor_ctx(field_id=fid, kind="properties"),
            )
            return False
        
        # --- capability gating based on field type ---
        field_type = _infer_field_type_key(field_el)
        fid = self.get_field_id_from_element(field_el)
        title = self.get_field_title(field_el)

        def _emit_prop_step(step: str, start: float, **extra) -> None:
            try:
                self.session.emit_diag(
                    Cat.PROPS,
                    "Step timing",
                    step=step,
                    elapsed_s=round(time.monotonic() - start, 3),
                    **self._editor_ctx(field_id=fid, kind="properties", stage=step),
                    **extra,
                )
            except Exception:
                pass
        self.session.emit_diag(
            Cat.PROPS,
            f"set_field_properties: field_type={field_type} field_id={fid} title={title!r}",
            **self._editor_ctx(field_id=fid, kind="properties"),
        )

        caps = FIELD_CAPS.get(field_type, FIELD_CAPS["unknown"])

        if getattr(config, "INSTRUMENT_UI_STATE", False):
            probe = self.session.probe_ui_state(
                label="pre-properties",
                expected_field_id=fid,
                expected_title=title,
                field_el=field_el,
            )
            self.session.log_ui_probe(probe, level="debug")

        # Paragraph cannot do assessor update
        if assessor_visibility == "update" and not caps.get("assessor_visibility_update", True):
            self.session.emit_diag(
                Cat.PROPS,
                f"Skipping assessor_visibility=update (unsupported) field_type={field_type} field_id={fid} title={title!r}",
                **self._editor_ctx(field_id=fid, kind="properties"),
            )
            assessor_visibility = None  # or force to "read"

        # Skip unsupported knobs entirely (capability gating)
        if required is not None and not caps.get("required", True):
            self.session.emit_diag(
                Cat.PROPS,
                f"Skipping required for (unsupported) field_type={field_type} field_id={fid} title={title!r}",
                **self._editor_ctx(field_id=fid, kind="properties"),
            )
            required = None

        if marking_type is not None and not caps.get("marking_type", True):
            self.session.emit_diag(
                Cat.PROPS,
                f"Skipping marking_type for (unsupported) field_type={field_type} field_id={fid} title={title!r}",
                **self._editor_ctx(field_id=fid, kind="properties"),
            )
            marking_type = None

        if enable_model_answer is not None and not caps.get("model_answer", True):
            self.session.emit_diag(
                Cat.PROPS,
                f"Skipping model_answer toggle for (unsupported) field_type={field_type} field_id={fid} title={title!r}",
                **self._editor_ctx(field_id=fid, kind="properties"),
            )
            enable_model_answer = None

        if enable_assessor_comments is not None and not caps.get("assessor_comments", True):
            self.session.emit_diag(
                Cat.PROPS,
                f"Skipping assessor_comments toggle for (unsupported) field_type={field_type} field_id={fid} title={title!r}",
                **self._editor_ctx(field_id=fid, kind="properties"),
            )
            enable_assessor_comments = None

        # --- get the properties frame robustly ---
        t_step = time.monotonic()
        frame = _open_props_frame_with_retry(retries=3)
        _emit_prop_step("props_open_frame", t_step, ok=frame is not None)
        if frame is None:
            fid = None
            title_txt = None
            try:
                fid = self.try_get_field_id_strict(field_el) or self.get_field_id_from_element(field_el)
            except Exception:
                pass
            try:
                title_txt = self.get_field_title(field_el)
            except Exception:
                pass

            ctx = self._editor_ctx(field_id=fid, section_id=None, kind="properties", stage="binding_failure")
            self.session.counters.inc("editor.ui_state_property_skips")
            self.session.emit_diag(
                Cat.UISTATE,
                "UI_STATE binding not proven; skipping property writes",
                title=title_txt,
                **ctx,
            )

            # Record for controller reporting/retry
            self.record_skip({
                "kind": "properties",
                "reason": "UI_STATE binding not proven after retries",
                "retryable": True,  # retry at end of activity
                "field_id": fid,
                "field_title": title_txt,
                "requested": {
                    "hide_in_report": hide_in_report,
                    "learner_visibility": learner_visibility,
                    "assessor_visibility": assessor_visibility,
                    "required": required,
                    "marking_type": marking_type,
                    "enable_model_answer": enable_model_answer,
                    "enable_assessor_comments": enable_assessor_comments,
                },
            })
            return     

        # --- hide_in_report ---
        try:
            if hide_in_report is not None:
                t_step = time.monotonic()
                self._set_checkbox(
                    props["hide_in_report_checkbox"],
                    hide_in_report,
                    root=frame,
                    expected_field_id=fid,
                    expected_title=title,
                    field_el=field_el,
                )
                _emit_prop_step("props_hide_in_report", t_step, desired=hide_in_report)
        except Exception as e:
            self.session.emit_diag(
                Cat.PROPS,
                f"hide_in_report not set/available: {e}",
                **self._editor_ctx(field_id=fid, kind="properties"),
            )
            missed["hide_in_report"] = f"exception: {type(e).__name__}: {e}"

        # --- learner visibility ---
        try:
            if learner_visibility is not None:
                value_map = {
                    "hidden": "learners_hidden",
                    "read": "learners_read",
                    "update": "learners_update",
                    "read-on-submit": "learners_read-on-submit",
                }
                target_value = value_map.get(learner_visibility)
                if target_value:
                    t_step = time.monotonic()
                    ok = _set_radio_by_value_with_verify(
                        frame,
                        name="learners",
                        target_value=target_value,
                        label=f"learner visibility ({learner_visibility})",
                                    )
                    _emit_prop_step(
                        "props_learner_visibility",
                        t_step,
                        desired=learner_visibility,
                        ok=ok,
                    )
                    if not ok:
                        missed["learner_visibility"] = f"verify failed (wanted={target_value})"
                else:
                    self.session.emit_signal(
                        Cat.PROPS,
                        f"Unknown learner_visibility {learner_visibility!r}",
                        level="warning",
                        **self._editor_ctx(field_id=fid, kind="properties"),
                    )
                    missed["learner_visibility"] = f"unknown value {learner_visibility!r}"
        except Exception as e:
            self.session.emit_diag(
                Cat.PROPS,
                f"Learner visibility not set/available: {e}",
                **self._editor_ctx(field_id=fid, kind="properties"),
            )
            missed["learner_visibility"] = f"exception: {type(e).__name__}: {e}"

        # --- assessor visibility ---
        try:
            if assessor_visibility is not None:
                value_map = {
                    "hidden": "assessors_hidden",
                    "read": "assessors_read",
                    "update": "assessors_update",
                }
                target_value = value_map.get(assessor_visibility)
                if target_value:
                    t_step = time.monotonic()
                    ok = _set_radio_by_value_with_verify(
                        frame,
                        name="assessors",
                        target_value=target_value,
                        label=f"assessor visibility ({assessor_visibility})",
                    )
                    _emit_prop_step(
                        "props_assessor_visibility",
                        t_step,
                        desired=assessor_visibility,
                        ok=ok,
                    )
                    if not ok:
                        missed["assessor_visibility"] = f"verify failed (wanted={target_value})"
                else:
                    self.session.emit_signal(
                        Cat.PROPS,
                        f"Unknown assessor_visibility {assessor_visibility!r}",
                        level="warning",
                        **self._editor_ctx(field_id=fid, kind="properties"),
                    )
                    missed["assessor_visibility"] = f"unknown value {assessor_visibility!r}"
        except Exception as e:
            self.session.emit_diag(
                Cat.PROPS,
                f"Assessor visibility not set/available: {e}",
                **self._editor_ctx(field_id=fid, kind="properties"),
            )
            missed["assessor_visibility"] = f"exception: {type(e).__name__}: {e}"

        # --- required_field ---
        try:
            if required is not None:
                t_step = time.monotonic()
                self._set_checkbox(
                    props["required_checkbox"],
                    required,
                    root=frame,
                    expected_field_id=fid,
                    expected_title=title,
                    field_el=field_el,
                )
                _emit_prop_step("props_required", t_step, desired=required)
        except Exception as e:
            self.session.emit_diag(
                Cat.PROPS,
                f"Required field checkbox not set/available: {e}",
                **self._editor_ctx(field_id=fid, kind="properties"),
            )
            missed["required_checkbox"] = f"exception: {type(e).__name__}: {e}"

        # --- marking_type (question only) ---
        try:
            if marking_type is not None:
                # This is choices.js; simplest is to set the underlying select value and fire change
                script = """
                    var frame = arguments[0];
                    var value = arguments[1];
                    var select = frame.querySelector('select[name="marking_type"]');
                    if (!select) return false;
                    select.value = value;
                    var event = new Event('change', { bubbles: true });
                    select.dispatchEvent(event);
                    return true;
                """
                self.session.emit_diag(
                    Cat.PROPS,
                    f"Setting marking_type to '{marking_type}'...",
                    **self._editor_ctx(field_id=fid, kind="properties"),
                )
                t_step = time.monotonic()
                ok = driver.execute_script(script, frame, marking_type)
                _emit_prop_step("props_marking_type", t_step, desired=marking_type, ok=ok)
                if not ok:
                    missed["marking_type"] = "select[name='marking_type'] not found or change not applied"
        except Exception as e:
            self.session.emit_diag(
                Cat.PROPS,
                f"marking_type not set/available: {e}",
                **self._editor_ctx(field_id=fid, kind="properties"),
            )

        # --- model_answer switch ---
        try:
            if enable_model_answer is not None:
                t_step = time.monotonic()
                self._set_checkbox(
                    props["model_answer_toggle"],
                    enable_model_answer,
                    root=frame,
                    expected_field_id=fid,
                    expected_title=title,
                    field_el=field_el,
                )
                _emit_prop_step("props_model_answer_toggle", t_step, desired=enable_model_answer)
        except Exception as e:
            self.session.emit_diag(
                Cat.PROPS,
                f"Model answer switch not set/available: {e}",
                **self._editor_ctx(field_id=fid, kind="properties"),
            )
            missed["enable_model_answer"] = f"exception: {type(e).__name__}: {e}"

        # --- assessor_comments switch ---
        try:
            if enable_assessor_comments is not None:
                t_step = time.monotonic()
                self._set_checkbox(
                    props["assessor_comments_toggle"],
                    enable_assessor_comments,
                    root=frame,
                    expected_field_id=fid,
                    expected_title=title,
                    field_el=field_el,
                )
                _emit_prop_step("props_assessor_comments_toggle", t_step, desired=enable_assessor_comments)
        except Exception as e:
            self.session.emit_diag(
                Cat.PROPS,
                f"Assessor comments switch not set/available: {e}",
                **self._editor_ctx(field_id=fid, kind="properties"),
            )
            missed["enable_assessor_comments"] = f"exception: {type(e).__name__}: {e}"

        if missed:
            self.record_skip({
                "kind": "properties",
                "reason": f"One or more property writes failed: {missed}",
                "retryable": True,   # or False if you want purely manual follow-up
                "field_id": fid,
                "field_title": title,
                "requested": {
                    "hide_in_report": hide_in_report,
                    "learner_visibility": learner_visibility,
                    "assessor_visibility": assessor_visibility,
                    "required": required,
                    "marking_type": marking_type,
                    "enable_model_answer": enable_model_answer,
                    "enable_assessor_comments": enable_assessor_comments,
                },
                # optional but very useful:
                "missed": missed,
            })

    def set_field_model_answer(
        self,
        field_id: str,
        model_answer_html: str,
        max_attempts: int = 3,
    ) -> None:
        """
        Set the model answer Froala block for the last field of the given type.

        This helper is robust against Turbo re-renders by re-resolving the field
        element and retrying a few times if we hit stale element references.
        """
        driver = self.driver
        if not model_answer_html:
            return
        ctx = self._editor_ctx(field_id=field_id, kind="model_answer")

        block_selector = (
            ".designer__field__editable-label--description"
            "[id^='designer__field__model-answer-description--']"
        )
        textarea_selector = (
            "textarea.froala-editor[name='description']"
            "[data-froala-save-source-value*='field_type=model_answer_description']"
        )

        for attempt in range(1, max_attempts + 1):
            try:
                # 1) Make sure CA has actually toggled the model answer editor into edit mode
                self._activate_model_answer_editor(field_id)
                
                # 2) Wait for a fresh field_el with an initialised Froala editor
                self.session.emit_diag(
                    Cat.FROALA,
                    f"Model answer: waiting for Froala editor for field id {field_id} (attempt {attempt})...",
                    **ctx,
                )
                field_el = self._wait_for_model_answer_editor(field_id, block_selector, "Model answer")

                # 3) Inject the HTML via your existing helper
                self._set_froala_block(
                    field_el,
                    block_selector=block_selector,
                    textarea_selector=textarea_selector,
                    html=model_answer_html,
                    log_label="Model answer",
                )

                # 4) Belt-and-braces: update any model answer textarea for this field id
                script = """
                    var fieldId = arguments[0];
                    var value = arguments[1];

                    // Match only this field's model_answer_description textareas
                    var selector = "textarea.froala-editor[name='description']" +
                                "[data-froala-save-source-value*='field_type=model_answer_description']";

                    var textareas = document.querySelectorAll(selector);
                    textareas.forEach(function (ta) {
                        var src = ta.getAttribute("data-froala-save-source-value") || "";
                        if (!src.includes("/fields/" + fieldId + ".turbo_stream")) return;

                        ta.value = value;
                        ['input', 'change'].forEach(function (name) {
                            var evt = new Event(name, { bubbles: true });
                            ta.dispatchEvent(evt);
                        });

                        // If there is a Froala editor bound to this textarea via the froala controller,
                        // it will usually mirror the content automatically from textarea -> editor.
                    });

                    return true;
                """
                driver.execute_script(script, field_id, model_answer_html)
                self.session.emit_diag(
                    Cat.FROALA,
                    f"Model answer text synchronised for field id {field_id}.",
                    **ctx,
                )
                return  # success
            except StaleElementReferenceException:
                self.session.emit_diag(
                    Cat.FROALA,
                    f"Model answer: stale element on attempt {attempt} for {field_id!r}; retrying...",
                    **ctx,
                )
                continue
            except TimeoutException as e:
                self.session.emit_signal(
                    Cat.FROALA,
                    f"Model answer: timeout waiting for editor on attempt {attempt} for {field_id!r}: {e}",
                    level="warning",
                    **ctx,
                )
                # no point retrying immediately if editor never appeared
                break
            except Exception as e:
                self.session.emit_signal(
                    Cat.FROALA,
                    f"Model answer: unexpected error on attempt {attempt} for {field_id!r}: {e}",
                    level="warning",
                    **ctx,
                )
                break

        self.session.emit_signal(
            Cat.FROALA,
            f"Model answer: giving up after {max_attempts} attempt(s) for field id {field_id!r}.",
            level="warning",
            **ctx,
        )

    def _wait_for_model_answer_editor(self, field_id: str, block_selector: str, log_label: str) -> WebElement:
        """
        Wait until the model answer Froala editor exists for the given field id,
        returning the *fresh* field element.
        """
        wait = self.wait
        ctx = self._editor_ctx(field_id=field_id, kind="model_answer", stage=log_label)

        def block_and_editor_present(_):
            try:
                field = self.get_field_by_id(field_id)
            except NoSuchElementException:
                self.session.emit_diag(
                    Cat.FROALA,
                    f"{log_label}: field for id {field_id} not found yet.",
                    **ctx,
                )
                return False

            try:
                block = field.find_element(By.CSS_SELECTOR, block_selector)
            except NoSuchElementException:
                self.session.emit_diag(
                    Cat.FROALA,
                    f"{log_label}: field {field_id} found but block {block_selector!r} not present yet.",
                    **ctx,
                )
                return False
            except StaleElementReferenceException:
                self.session.emit_diag(
                    Cat.FROALA,
                    f"{log_label}: field element for id {field_id} became stale while looking for block {block_selector!r}.",
                    **ctx,
                )
                return False

            try:
                editor = block.find_element(
                    By.CSS_SELECTOR,
                    ".fr-element.fr-view[contenteditable='true']",
                )
                self.session.emit_diag(
                    Cat.FROALA,
                    f"{log_label}: Froala editor present for field id {field_id}.",
                    **ctx,
                )
                return True
            except NoSuchElementException:
                self.session.emit_diag(
                    Cat.FROALA,
                    f"{log_label}: block present for field id {field_id} but Froala editor not yet initialised.",
                    **ctx,
                )
                return False
            except StaleElementReferenceException:
                self.session.emit_diag(
                    Cat.FROALA,
                    f"{log_label}: block became stale for field id {field_id}.",
                    **ctx,
                )
                return False

        wait.until(block_and_editor_present)
        # After wait, re-resolve the fresh field element and return it
        return self.get_field_by_id(field_id)

    def _activate_model_answer_editor(self, field_id: str, log_label: str = "Model answer") -> None:
        """
        Pre-activate model answer editor by clicking its display label.

        Turbo-safe:
        - Re-finds elements immediately before click.
        - Retries once on StaleElementReferenceException.
        - Optionally proves activation by waiting for an 'active' class on the wrapper.
        """
        driver = self.driver
        session = self.session
        ctx = self._editor_ctx(field_id=field_id, kind="model_answer", stage=log_label)

        # Prefer a selector that doesn't depend on a stale field root.
        label_css = f".field__editable-label.designer__field__model-answer-description--{field_id}"

        def _click_label_once() -> bool:
            # Re-find field + label fresh each time
            field = self.get_field_by_id(field_id)
            label = field.find_element(By.CSS_SELECTOR, label_css)

            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", label)
            driver.execute_script("arguments[0].click();", label)
            return True

        try:
            _click_label_once()
        except StaleElementReferenceException:
            # One retry: Turbo likely swapped the node
            try:
                _click_label_once()
            except Exception as e2:
                self.session.emit_diag(
                    Cat.FROALA,
                    f"{log_label}: could not pre-activate editor for field id {field_id} (stale retry failed): {e2}",
                    **ctx,
                )
                return
        except Exception as e:
            self.session.emit_diag(
                Cat.FROALA,
                f"{log_label}: could not pre-activate editor for field id {field_id}: {e}",
                **ctx,
            )
            return

        # Best-effort “prove”: wait briefly for active state to appear
        # (If you have a known wrapper selector for model answer, use it here.)
        try:
            # Many editable labels toggle an active wrapper class nearby; this is safe + short.
            session.get_wait(2).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, label_css))
            )
        except Exception:
            pass

        self.session.emit_diag(
            Cat.FROALA,
            f"{log_label}: pre-activated editor by clicking model answer label for field id {field_id}.",
            **ctx,
        )

    def _set_checkbox(
        self,
        selector: str,
        desired: bool,
        *,
        root: Optional[WebElement] = None,
        timeout: float = 3.0,
        expected_field_id: str | None = None,
        expected_title: str | None = None,
        field_el: WebElement | None = None,
    ) -> None:
        """
        Ensure that the checkbox at `selector` is in the desired state.
        - If `root` is provided (e.g. field_settings_frame), we try it first (fast path).
        - If `root` is stale / missing selector, we fall back to refinding the settings frame.
        - Retries on stale elements because the settings turbo-frame often re-renders.
        """
        driver = self.driver
        ctx = self._editor_ctx(field_id=expected_field_id, kind="properties", stage="checkbox")

        max_attempts = 3

        def _find_in_root(r: WebElement) -> Optional[WebElement]:
            try:
                return r.find_element(By.CSS_SELECTOR, selector)
            except (StaleElementReferenceException, NoSuchElementException):
                return None

        def _locate_checkbox() -> Optional[WebElement]:
            end_time = time.time() + timeout
            cb = None
            while time.time() < end_time:
                # 1) Prefer caller-provided root if available
                if root is not None:
                    cb = _find_in_root(root)
                    if cb is not None:
                        return cb
                # 2) Fall back to refinding the frame (turbo-safe)
                try:
                    frame = self._get_field_settings_frame()
                    cb = frame.find_element(By.CSS_SELECTOR, selector)
                    return cb
                except (StaleElementReferenceException, NoSuchElementException):
                    self.session.emit_diag(
                        Cat.PROPS,
                        f"Checkbox {selector!r} not ready / stale when locating; retrying...",
                        **ctx,
                    )
                    time.sleep(0.2)
            return None

        def _is_checked(el) -> Optional[bool]:
            try:
                # Use JS to read .checked for robustness
                return bool(driver.execute_script("return arguments[0].checked === true;", el))
            except StaleElementReferenceException:
                return None
            
        def _maybe_probe(label: str, *, heavy: bool = False) -> None:
            if not getattr(config, "INSTRUMENT_UI_STATE", False):
                return
            try:
                probe = self.session.probe_ui_state(
                    label=label,
                    expected_field_id=expected_field_id,
                    expected_title=expected_title,
                    field_el=field_el,
                    include_frame_html_snippet=heavy,
                )
                self.session.log_ui_probe(probe, level="warning")
            except Exception:
                pass

        for attempt in range(1, max_attempts + 1):
            checkbox = _locate_checkbox()
            if checkbox is None:
                self.session.emit_diag(
                    Cat.PROPS,
                    f"Checkbox {selector!r} not found in settings sidebar (attempt {attempt});",
                    **ctx,
                )
                
                _maybe_probe(
                f"checkbox_missing selector={selector} attempt={attempt}/{max_attempts}",
                heavy=(attempt == max_attempts)
                )

                if attempt == max_attempts:
                    self.session.emit_diag(
                        Cat.PROPS,
                        f"Giving up on checkbox {selector!r} after {max_attempts} attempts.",
                        **ctx,
                    )
                continue

            # Read current state
            current = _is_checked(checkbox)
            self.session.emit_diag(
                Cat.PROPS,
                f"Checkbox {selector!r} state before attempt {attempt}: {current!r} (desired={desired})",
                **ctx,
            )

            if current is not None and current == desired:
                self.session.emit_diag(
                    Cat.PROPS,
                    f"Checkbox {selector!r} already in desired state ({desired}).",
                    **ctx,
                )
                return

            # Click via JS
            try:
                self.session.emit_diag(
                    Cat.PROPS,
                    f"Clicking checkbox {selector!r} to set to {desired} (attempt {attempt}).",
                    **ctx,
                )
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", checkbox)
                driver.execute_script("arguments[0].click();", checkbox)

                # Fire change event for Stimulus, just in case
                try:
                    driver.execute_script(
                        """
                        const input = arguments[0];
                        const ev = new Event('change', { bubbles: true });
                        input.dispatchEvent(ev);
                        """,
                        checkbox,
                    )
                except Exception:
                    pass

            except StaleElementReferenceException as e:
                self.session.emit_diag(
                    Cat.PROPS,
                    f"Checkbox {selector!r} went stale during click on attempt {attempt}: {e}; will retry.",
                    **ctx,
                )
                
                _maybe_probe(
                f"checkbox_stale_click selector={selector} attempt={attempt}/{max_attempts}",
                heavy=(attempt == max_attempts)
                )

                # retry with a fresh element in next loop iteration
                if attempt == max_attempts:
                    self.session.emit_diag(
                        Cat.PROPS,
                        f"Giving up on checkbox {selector!r} after stale click on final attempt.",
                        **ctx,
                    )
                continue

            # Confirm window: re-locate and check state
            confirm_until = time.time() + 1.25  # ~1.25s confirmation window
            while time.time() < confirm_until:
                cb2 = _locate_checkbox()
                if cb2 is None:
                    time.sleep(0.1)
                    continue
                final = _is_checked(cb2)
                if final is not None and final == desired:
                    self.session.emit_diag(
                        Cat.PROPS,
                        f"Checkbox {selector!r} now in desired state ({desired}).",
                        **ctx,
                    )
                    return
                time.sleep(0.1)

            if attempt == max_attempts:
                _maybe_probe(
                    f"checkbox_no_confirm selector={selector} desired={desired} attempt={attempt}/{max_attempts}",
                    heavy=True
                )
            else:
                _maybe_probe(
                    f"checkbox_no_confirm selector={selector} desired={desired} attempt={attempt}/{max_attempts}",
                    heavy=False
                )

            self.session.emit_diag(
                Cat.PROPS,
                f"Checkbox {selector!r} did not reach desired state ({desired}) within confirmation window on attempt {attempt}.",
                **ctx,
            )

        self.session.emit_diag(
            Cat.PROPS,
            f"Checkbox {selector!r} may not have reached desired state ({desired}) after {max_attempts} attempts; continuing.",
            **ctx,
        )

# --- TABLES ---

    def _configure_table_from_config(self, field_el, config: TableConfig) -> None:
        """
        Apply a TableConfig to an interactive table field.

        Resilience strategy:
        - Resolve a stable field_id once.
        - Between stages, re-find the field element by id to avoid stale anchors.
        - Retry each stage a few times on stale/DOM churn.
        """
        self.session.counters.inc("editor.table_config_calls")
        ctx = self._editor_ctx(kind="table_config")

        # Resolve a stable id for the field so we can re-find the root element
        field_id = None
        try:
            field_id = self.get_field_id_from_element(field_el)
        except Exception:
            field_id = None

        if not field_id:
            self.session.emit_signal(
                Cat.TABLE,
                "Table config: could not resolve field_id from field_el; proceeding without re-find safeguards.",
                level="warning",
                **ctx,
            )

        section_id = ""
        if field_id:
            field_handle = self.registry.get_field(field_id)
            section_id = field_handle.section_id if field_handle else ""

        def _emit_table_step(step: str, start: float, **extra) -> None:
            try:
                self.session.emit_diag(
                    Cat.TABLE,
                    "Step timing",
                    step=step,
                    elapsed_s=round(time.monotonic() - start, 3),
                    **self._editor_ctx(
                        field_id=field_id,
                        section_id=section_id,
                        kind="table",
                        stage=step,
                    ),
                    **extra,
                )
            except Exception:
                pass

        def _fresh_field_el():
            if not field_id:
                return field_el
            return self.get_field_by_id(field_id)

        def _run_stage(stage_name: str, fn, *, attempts: int = 3, sleep_s: float = 0.15) -> bool:
            """
            Run a stage with retries. Always re-find the field element before each attempt.
            """
            ctx_stage = self._editor_ctx(
                field_id=field_id,
                section_id=section_id,
                kind="table",
                stage=stage_name,
            )
            for attempt in range(1, attempts + 1):
                self.session.counters.inc(f"editor.table_stage_{stage_name}_attempts")
                if attempt > 1:
                    self.session.counters.inc(f"editor.table_stage_{stage_name}_retries")
                t_step = time.monotonic()
                try:
                    fresh = _fresh_field_el()
                    fn(fresh)
                    self.session.counters.inc(f"editor.table_stage_{stage_name}_success")
                    self.session.emit_diag(
                        Cat.TABLE,
                        f"Table stage '{stage_name}' succeeded",
                        attempt=attempt,
                        **ctx_stage,
                    )
                    _emit_table_step(f"table_{stage_name}_a{attempt}", t_step, ok=True)
                    return True
                except StaleElementReferenceException as e:
                    self.session.counters.inc(f"editor.table_stage_{stage_name}_stale")
                    self.session.emit_diag(
                        Cat.TABLE,
                        f"Table stage '{stage_name}' stale element",
                        attempt=attempt,
                        exc=str(e),
                        **ctx_stage,
                    )
                    _emit_table_step(f"table_{stage_name}_a{attempt}", t_step, ok=False, exc=type(e).__name__)
                except NoSuchElementException as e:
                    self.session.counters.inc(f"editor.table_stage_{stage_name}_missing")
                    self.session.emit_diag(
                        Cat.TABLE,
                        f"Table stage '{stage_name}' missing element",
                        attempt=attempt,
                        exc=str(e),
                        **ctx_stage,
                    )
                    _emit_table_step(f"table_{stage_name}_a{attempt}", t_step, ok=False, exc=type(e).__name__)
                except TableResizeError as e:
                    self.session.counters.inc(f"editor.table_stage_{stage_name}_resize_error")
                    self.session.emit_diag(
                        Cat.TABLE,
                        f"Table stage '{stage_name}' resize error",
                        attempt=attempt,
                        exc=str(e),
                        **ctx_stage,
                    )
                    _emit_table_step(f"table_{stage_name}_a{attempt}", t_step, ok=False, exc=type(e).__name__)
                    raise
                except Exception as e:
                    self.session.counters.inc(f"editor.table_stage_{stage_name}_error")
                    self.session.emit_diag(
                        Cat.TABLE,
                        f"Table stage '{stage_name}' error",
                        attempt=attempt,
                        exc=str(e),
                        **ctx_stage,
                    )
                    _emit_table_step(f"table_{stage_name}_a{attempt}", t_step, ok=False, exc=type(e).__name__)

                if attempt < attempts:
                    time.sleep(sleep_s)
                self.session.emit_signal(
                    Cat.TABLE,
                    f"Table stage {stage_name} giving up after {attempts} attempts.",
                    level="warning",
                    **ctx_stage,
                )
                self.session.counters.inc(f"editor.table_stage_{stage_name}_gave_up")
                self.session.emit_diag(
                    Cat.TABLE,
                    f"Table stage '{stage_name}' gave up after {attempts} attempts",
                    **ctx_stage,
                )
                
                # Record a configure skip event for controller
                self._record_config_skip(
                    kind="configure",
                    reason=f"table stage '{stage_name}' failed after {attempts} attempts",
                    retryable=True,
                    field_id=field_id,
                    field_title=(getattr(config, "title", None) or None),  # careful: config is TableConfig here
                    requested={"stage": stage_name},
                )
                
            return False

        # ---- 1) Dimensions ----
        rows = config.rows
        cols = config.cols
        if rows is not None and cols is not None:
            ok = _run_stage(
                "dimensions",
                lambda fresh_field: self.ensure_table_dimensions_strict(fresh_field, rows, cols),
                attempts=3,
            )
            if not ok:
                return

        # ---- 2) Column types ----
        col_types = config.column_types
        if col_types:
            ok = _run_stage(
                "column_types",
                lambda fresh_field: self._set_column_types(fresh_field, col_types),
                attempts=3,
            )
            if not ok:
                return

        # ---- 3) Column headers ----
        col_headers = config.column_headers
        if col_headers:
            _run_stage(
                "column_headers",
                lambda fresh_field: self._set_table_column_headers(fresh_field, col_headers),
                attempts=3,
            )

        # ---- 4) Row labels ----
        row_labels = config.row_labels
        if row_labels:
            _run_stage(
                "row_labels",
                lambda fresh_field: self._set_table_row_labels(fresh_field, row_labels),
                attempts=3,
            )

        # ---- 5) Per-cell overrides ----
        if config.cell_overrides:
            def _apply_overrides(fresh_field):
                table_root = self._get_dynamic_table_root(fresh_field)
                for (r, c), cell_cfg in (config.cell_overrides or {}).items():
                    self.session.emit_diag(
                        Cat.TABLE,
                        f"Applying cell_override at (r={r},c={c}) text={cell_cfg.text!r}",
                        **self._editor_ctx(kind="table_override"),
                    )
                    ok = self._apply_table_cell_override(table_root, r, c, cell_cfg)
                    self.session.emit_diag(
                        Cat.TABLE,
                        f"cell_override result at (r={r},c={c}): ok={ok}",
                        **self._editor_ctx(kind="table_override"),
                    )

            _run_stage("cell_overrides", _apply_overrides, attempts=3)

    def _get_dynamic_table_root(self, field_el):
        """
        Given a designer__field--table element, return its .dynamic-table root.
        """
        return field_el.find_element(
            By.CSS_SELECTOR,
            config.BUILDER_SELECTORS["table"]["root"],
        )

    def ensure_table_dimensions_strict(self, field_el, rows: int, cols: int) -> None:
        final_rows, final_cols = self.ensure_table_dimensions(field_el, rows, cols)

        if (final_rows, final_cols) != (rows, cols):
            field_id = None
            try:
                field_id = self.get_field_id_from_element(field_el)
            except Exception:
                pass

            raise TableResizeError(
                f"Strict table resize failed"
                f"{' field_id=' + str(field_id) if field_id else ''}: "
                f"got {final_rows}x{final_cols}, expected {rows}x{cols}"
            )

    def ensure_table_dimensions(
        self,
        field_el,
        rows: int,
        cols: int,
        timeout: int = 10,
    ) -> tuple[int, int]:
        """
        Ensure the dynamic table attached to this field has at least the given
        number of body rows and data columns.

        Returns:
            (final_rows, final_cols) observed at the end.

        Notes:
        - Only grows; does not shrink.
        - Uses polling after each add to confirm DOM state change.
        """
        driver = self.driver
        table_selectors = config.BUILDER_SELECTORS["table"]
        ctx = self._editor_ctx(kind="table_resize")

        def get_shape():
            """
            Return (table_root, header_cells, body_rows, data_cols, body_row_count).

            Always re-query to avoid stale references.
            """
            table_root = self._get_dynamic_table_root(field_el)
            header_cells = table_root.find_elements(By.CSS_SELECTOR, table_selectors["header_cells"])
            body_rows = table_root.find_elements(By.CSS_SELECTOR, table_selectors["body_rows"])

            # Heuristic: first header cell is row-label / control column
            data_cols = max(len(header_cells) - 1, 0) if header_cells else 0
            return table_root, header_cells, body_rows, data_cols, len(body_rows)

        def get_add_wrappers(table_root):
            """
            Return (add_column_wrapper, add_row_wrapper) freshly located.
            """
            self.session.emit_diag(
                Cat.TABLE,
                "Locating and assigning wrappers",
                **ctx,
            )
            add_col_wrapper = table_root.find_element(By.CSS_SELECTOR, table_selectors["add_column_button"])
            add_row_wrapper = table_root.find_element(By.CSS_SELECTOR, table_selectors["add_row_button"])
            return add_col_wrapper, add_row_wrapper

        def click_wrapper(elem):
            """
            Use a real pointer-style click on the wrapper.
            """
            self.session.emit_diag(
                Cat.TABLE,
                "Attempting to click wrapper",
                **ctx,
            )
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
                    elem,
                )
            except Exception:
                pass

            ActionChains(driver).move_to_element(elem).pause(0.1).click().perform()

        def click_add_action(
            *,
            table_root,
            button_el,
            wrapper_css: str,
            kind: str,
            target_value: int,
        ) -> None:
            """
            Robust click for add-row/add-col turbo-post buttons.

            Strategy:
            1) pointer click button
            2) quick verify (<= ~1.2s) that shape is moving toward target
            3) if not, JS click button
            4) if not, click wrapper div (transparent click region)
            5) proceed; outer poll loop will confirm.
            """
            # Cheap overlay cleanup (helps when a cell editor/tooltip steals focus)
            try:
                self._reset_canvas_ui_state()
            except Exception:
                pass

            # Ensure visible
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                    button_el,
                )
            except Exception:
                pass

            def _shape_value():
                # Reuse get_shape() to avoid stale
                _, _, _, c, r = get_shape()
                return (r if kind == "row" else c)

            before = None
            try:
                before = _shape_value()
            except Exception:
                before = None

            # 1) Pointer click
            try:
                ActionChains(driver).move_to_element(button_el).pause(0.1).click().perform()
            except Exception:
                pass

            # Quick verify
            quick_deadline = time.time() + 1.2
            while time.time() < quick_deadline:
                try:
                    now = _shape_value()
                    if now >= target_value:
                        return
                    # If at least changed vs before, we consider it "triggered"
                    if before is not None and now != before:
                        return
                except StaleElementReferenceException:
                    time.sleep(0.1)
                except Exception:
                    time.sleep(0.1)
                time.sleep(0.1)

            # 2) JS click button
            try:
                driver.execute_script("arguments[0].click();", button_el)
            except Exception:
                pass

            quick_deadline = time.time() + 1.0
            while time.time() < quick_deadline:
                try:
                    now = _shape_value()
                    if now >= target_value:
                        return
                    if before is not None and now != before:
                        return
                except Exception:
                    time.sleep(0.1)
                time.sleep(0.1)

            # 3) Click wrapper div
            try:
                wrapper = table_root.find_element(By.CSS_SELECTOR, wrapper_css)
                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                        wrapper,
                    )
                except Exception:
                    pass
                driver.execute_script("arguments[0].click();", wrapper)
            except Exception:
                pass

        # --- Initial shape --------------------------------------------------
        table_root, header_cells, body_rows, current_cols, current_rows = get_shape()
        self.session.emit_diag(
            Cat.TABLE,
            f"Dynamic table current shape: rows={current_rows}, cols={current_cols} (requested rows={rows}, cols={cols}).",
            **ctx,
        )

        last_seen_cols = current_cols
        last_seen_rows = current_rows

        # --- Grow columns ---------------------------------------------------
        while current_cols < cols:
            try:
                self.session.emit_diag(
                    Cat.TABLE,
                    "Fetching table shape",
                    **ctx,
                )
                table_root, header_cells, body_rows, current_cols, current_rows = get_shape()
                last_seen_cols = current_cols
                add_col_btn, _ = get_add_wrappers(table_root)
            except NoSuchElementException:
                self.session.emit_signal(
                    Cat.TABLE,
                    "Aborting column growth: add_column_wrapper not found.",
                    level="warning",
                    **ctx,
                )
                break

            target_cols = current_cols + 1
            self.session.emit_diag(
                Cat.TABLE,
                f"Adding column {target_cols} (current={current_cols}).",
                **ctx,
            )
            # click_wrapper(add_col_wrapper)
            click_add_action(
                table_root=table_root,
                button_el=add_col_btn,
                wrapper_css=table_selectors["add_column_wrapper"],
                kind="col",
                target_value=target_cols,
            )

            deadline = time.time() + timeout
            poll_i = 0  # ✅ reset per add attempt

            # Poll until data column count increases
            while time.time() < deadline:
                try:
                    _, _, _, new_cols, _ = get_shape()
                except StaleElementReferenceException:
                    time.sleep(0.1)
                    continue

                last_seen_cols = new_cols
                # ✅ log first poll + every 10th poll
                if poll_i == 1 or poll_i % 10 == 0:
                    self.session.emit_diag(
                        Cat.TABLE,
                        f"[table] Polling columns: current={new_cols}, target={target_cols}",
                        **ctx,
                    )

                if new_cols >= target_cols:
                    current_cols = new_cols
                    break

                time.sleep(0.2)
            else:
                self.session.emit_signal(
                    Cat.TABLE,
                    f"Timed out waiting for table columns to grow to {target_cols}. Last observed cols={last_seen_cols}.",
                    level="warning",
                    **ctx,
                )
                break

        # --- Grow rows ------------------------------------------------------
        while current_rows < rows:
            try:
                table_root, header_cells, body_rows, current_cols, current_rows = get_shape()
                last_seen_rows = current_rows
                _, add_row_btn = get_add_wrappers(table_root)
            except NoSuchElementException:
                self.session.emit_signal(
                    Cat.TABLE,
                    "Aborting row growth: add_row_button not found.",
                    level="warning",
                    **ctx,
                )
                break

            target_rows = current_rows + 1
            self.session.emit_diag(
                Cat.TABLE,
                f"Adding row {target_rows} (current={current_rows}).",
                **ctx,
            )

            # click_wrapper(add_row_wrapper)
            click_add_action(
                table_root=table_root,
                button_el=add_row_btn,
                wrapper_css=table_selectors["add_row_wrapper"],
                kind="row",
                target_value=target_rows,
            )

            deadline = time.time() + timeout
            poll_i = 0  # ✅ reset per add attempt

            # Poll until body row count increases
            while time.time() < deadline:
                try:
                    _, _, _, _, new_rows = get_shape()
                except StaleElementReferenceException:
                    time.sleep(0.1)
                    continue

                last_seen_rows = new_rows
                # ✅ log first poll + every 10th poll
                if poll_i == 1 or poll_i % 10 == 0:
                    self.session.emit_diag(
                        Cat.TABLE,
                        f"[table] Polling rows: current={new_rows}, target={target_rows}",
                        **ctx,
                    )

                if new_rows >= target_rows:
                    current_rows = new_rows
                    break

                time.sleep(0.2)
            else:
                self.session.emit_signal(
                    Cat.TABLE,
                    f"Timed out waiting for table rows to grow to {target_rows}. Last observed rows={last_seen_rows}.",
                    level="warning",
                    **ctx,
                )
                break

        # Final measure (fresh)
        try:
            _, _, _, final_cols, final_rows = get_shape()
            current_cols, current_rows = final_cols, final_rows
        except Exception:
            # If final measure fails, fall back to last known counters.
            pass

        self.session.emit_diag(
            Cat.TABLE,
            f"Dynamic table resized to rows={current_rows}, cols={current_cols}.",
            **ctx,
        )
        return current_rows, current_cols
    
    def _get_table_header_cell(self, table_root, *, body_row_index: int, cell_index: int):
        """
        Return the WebElement for a specific header cell in the dynamic table.
        body_row_index is the index within tbody rows where row 0 is the 'header row'.
        cell_index includes the control column at 0.
        """
        selectors = config.BUILDER_SELECTORS["table"]

        # Find body rows fresh
        body_rows = table_root.find_elements(By.CSS_SELECTOR, selectors["body_rows"])
        if body_row_index >= len(body_rows):
            return None

        row = body_rows[body_row_index]
        # Cells within that row
        cells = row.find_elements(By.CSS_SELECTOR, selectors["body_cells"])
        if cell_index >= len(cells):
            return None

        return cells[cell_index]

    def _set_table_column_headers(self, field_el, column_headers: list[str]) -> None:
        """
        Set the column headers using the first body row as the header row.

        Notes:
        - Some CA tables include an extra leading <td> in the header row that is not editable.
        We detect this and apply a DOM offset to keep header mapping stable.
        """
        driver = self.driver
        selectors = config.BUILDER_SELECTORS["table"]
        max_attempts = 3
        ctx = self._editor_ctx(kind="table_headers")

        header_row_index = 0  # first body row acts as header row
        self.session.emit_diag(
            Cat.TABLE,
            f"Setting {len(column_headers)} column header(s) on first body row.",
            **ctx,
        )

        # 0) Mark row 0 as heading (best effort, but do it once)
        try:
            self._set_row_type(field_el, row_index=0, type_name="heading")
            self.session.get_wait(timeout=3).until(lambda d: self._row_looks_like_heading(field_el, 0))
        except TimeoutException:
            self.session.emit_diag(
                Cat.TABLE,
                "Header row did not confirm heading; proceeding anyway.",
                **ctx,
            )
        except Exception as e:
            self.session.emit_signal(
                Cat.TABLE,
                f"Failed to set column heading row to heading: {e}",
                level="warning",
                **ctx,
            )

        def _get_header_row_tds():
            table_root = self._get_dynamic_table_root(field_el)
            rows = table_root.find_elements(By.CSS_SELECTOR, selectors["body_rows"])
            if len(rows) <= header_row_index:
                return None
            return rows[header_row_index].find_elements(By.CSS_SELECTOR, "td")

        # 1) Wait until the header row exists and has at least *some* cells.
        #    We do a small baseline wait here, then we derive exact expectations below.
        try:
            self.session.get_wait(timeout=8).until(lambda d: bool(_get_header_row_tds()))
        except TimeoutException:
            self.session.emit_signal(
                Cat.TABLE,
                "Header row not present after wait; proceeding with best-effort writes.",
                level="warning",
                **ctx,
            )

        # 2) Derive DOM offset once: sometimes there is an extra leading TD in the DOM.
        tds = _get_header_row_tds() or []
        if not tds:
            self.session.emit_signal(
                Cat.TABLE,
                "No header row cells found; cannot set column headers.",
                level="warning",
                **ctx,
            )
            return

        # If DOM has one extra cell, assume it's a non-editable leading cell.
        # Example: len(tds)=5 but headers=4 -> dom_offset=1, write into td[1..4]
        dom_offset = 0
        if len(tds) == len(column_headers) + 1:
            dom_offset = 1

        # If DOM has fewer cells than headers, log and proceed; writes will skip missing cells.
        self.session.emit_diag(
            Cat.TABLE,
            f"Header row cells (td)={len(tds)}, headers={len(column_headers)}, dom_offset={dom_offset}",
            **ctx,
        )

        # 3) For each header cell: stabilise UI + re-find td + write (bounded time)
        PER_HEADER_DEADLINE_S = 6.0

        for offset, header_text in enumerate(column_headers):
            target_td_index = offset + dom_offset

            success = False
            deadline = time.time() + PER_HEADER_DEADLINE_S

            attempt = 0
            while time.time() < deadline and attempt <= max_attempts:
                attempt += 1
                try:
                    self._reset_canvas_ui_state()

                    # Re-find the target td every attempt (Turbo-safe)
                    tds = _get_header_row_tds()
                    if not tds or target_td_index >= len(tds):
                        self.session.emit_diag(
                            Cat.TABLE,
                            f"Skipping header {header_text!r}: no td at index {target_td_index} (attempt {attempt}).",
                            **ctx,
                        )
                        break

                    td = tds[target_td_index]

                    try:
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                            td,
                        )
                    except Exception:
                        pass

                    # Preferred: locate the dynamic cell container inside this td
                    # and use your unified cell writer (which now includes contenteditable fallback).
                    cell = None
                    try:
                        cell = td.find_element(By.CSS_SELECTOR, selectors["body_cells"])
                    except NoSuchElementException:
                        # Some DOMs may not nest the div; fall back to td itself.
                        cell = td

                    ok = self._set_table_cell_text(cell, header_text, retries=3)
                    if ok:
                        self.session.emit_diag(
                            Cat.TABLE,
                            (
                                f"Set column header {header_text!r} at offset={offset} "
                                f"(td_index={target_td_index} dom_offset={dom_offset}) on attempt {attempt}."
                            ),
                            **ctx,
                        )
                        success = True
                        break

                    # If writer couldn’t find an editable surface, pause briefly and retry.
                    time.sleep(0.15)

                except StaleElementReferenceException:
                    self.session.emit_diag(
                        Cat.TABLE,
                        f"Header write stale for {header_text!r} at td_index={target_td_index} (attempt {attempt}).",
                        **ctx,
                    )
                    time.sleep(0.15)
                except Exception as e:
                    self.session.emit_diag(
                        Cat.TABLE,
                        f"Header write error for {header_text!r} at td_index={target_td_index} (attempt {attempt}): {e}",
                        **ctx,
                    )
                    time.sleep(0.15)

            if not success:
                self.session.emit_signal(
                    Cat.TABLE,
                    f"Could not set column header {header_text!r} (td_index={target_td_index} dom_offset={dom_offset}).",
                    level="warning",
                    **ctx,
                )
                self._record_config_skip(
                    kind="configure",
                    reason="table column header not set (no editable control / retries exhausted)",
                    retryable=False,
                    field_id=self.get_field_id_from_element(field_el),
                    field_title=self.get_field_title(field_el),
                    requested={
                        "table_part": "column_headers",
                        "row_index": header_row_index,   
                        "td_index": target_td_index,
                        "dom_offset": dom_offset,
                        "value": header_text,
                    },
                )

    def _set_table_row_labels(self, field_el, row_labels: list[str]) -> None:
        """
        Set row labels in the first *data* column of body rows.

        Convention:
        - body row 0 is reserved as the header row
        - row labels start from body row 1
        - cell index 0 is a control column
        - cell index 1 is the first data column (row label column)
        """
        selectors = config.BUILDER_SELECTORS["table"]
        ctx = self._editor_ctx(kind="table_row_labels")

        # Best effort: make row-label column "heading" type
        try:
            self._set_column_type(field_el, col_index=0, type_name="heading")
        except Exception as e:
            self.session.emit_signal(
                Cat.TABLE,
                f"Failed to set row-label column to heading: {e}",
                level="warning",
                **ctx,
            )

        self.session.emit_diag(
            Cat.TABLE,
            f"Setting {len(row_labels)} row labels via dynamic table cells.",
            **ctx,
        )

        for offset, label in enumerate(row_labels):
            label_text = label or ""

            # row 0 = header, labels start at row 1
            target_row_index = 1 + offset

            max_attempts = 4
            for attempt in range(1, max_attempts + 1):
                try:
                    table_root = self._get_dynamic_table_root(field_el)
                    body_rows = table_root.find_elements(By.CSS_SELECTOR, selectors["body_rows"])
                    if not body_rows:
                        self.session.emit_signal(
                            Cat.TABLE,
                            "No body rows in table; cannot set row labels.",
                            level="warning",
                            **ctx,
                        )
                        return

                    if target_row_index >= len(body_rows):
                        self.session.emit_diag(
                            Cat.TABLE,
                            f"Skipping row label {label_text!r}: no body row at index {target_row_index} (rows={len(body_rows)}).",
                            **ctx,
                        )
                        return  # nothing further to do

                    row = body_rows[target_row_index]
                    cells = row.find_elements(By.CSS_SELECTOR, "th, td")

                    # Need at least control + first data column
                    if len(cells) < 2:
                        self.session.emit_diag(
                            Cat.TABLE,
                            f"Body row {target_row_index} has fewer than 2 cells; skipping label {label_text!r}.",
                            **ctx,
                        )
                        break

                    data_cell = cells[1]  # first data column
                    ok = self._set_table_cell_text(data_cell, label, retries=3)
                    if not ok:
                        self.session.emit_signal(
                            Cat.TABLE,
                            f"Could not set row label {label!r} at cell_index={target_row_index} (no editable control).",
                            level="warning",
                            **ctx,
                        )
                        if attempt < max_attempts:
                            time.sleep(0.15)
                            continue

                        # FINAL FAIL: record manual-fix item (non-retryable)
                        self._record_config_skip(
                            kind="configure",
                            reason="table row label not set (no editable control / retries exhausted)",
                            retryable=False,
                            field_id=self.get_field_id_from_element(field_el),          # whatever you have in scope in this method
                            field_title=self.get_field_title(field_el),    # likewise (or None)
                            requested={
                                "table_part": "row_labels",
                                "row_index": target_row_index,
                                "col_index": 1,          # first data column per your comment
                                "value": label,
                            },
                        )
                        break

                    self.session.emit_diag(
                        Cat.TABLE,
                        f"Set row label {label_text!r} at body_row={target_row_index} on attempt {attempt}/{max_attempts}.",
                        **ctx,
                    )
                    break  # success for this label

                except StaleElementReferenceException as e:
                    self.session.emit_diag(
                        Cat.TABLE,
                        (
                            f"Stale element while setting row label {label_text!r} (body_row={target_row_index}) "
                            f"attempt {attempt}/{max_attempts}: {e}"
                        ),
                        **ctx,
                    )
                    if attempt < max_attempts:
                        time.sleep(0.15)
                        continue
                    self.session.emit_signal(
                        Cat.TABLE,
                        (
                            f"Giving up setting row label {label_text!r} (body_row={target_row_index}) "
                            f"after {max_attempts} attempts due to repeated staleness."
                        ),
                        level="warning",
                        **ctx,
                    )
                    break

                except Exception as e:
                    self.session.emit_signal(
                        Cat.TABLE,
                        (
                            f"Error setting row label {label_text!r} (body_row={target_row_index}) "
                            f"attempt {attempt}/{max_attempts}: {e}"
                        ),
                        level="warning",
                        **ctx,
                    )
                    if attempt < max_attempts:
                        time.sleep(0.15)
                        continue
                    break

    def _set_table_cell_text(self, cell, text: str, *, retries: int = 2) -> bool:
        """
        Set the text for a single dynamic table cell (Turbo/Stimulus-safe).

        Attempt order (per retry attempt):
        1) textarea[name='cell_title'] OR input[name='cell_title']
        2) [contenteditable='true'] within the cell
        3) label-like nodes (.field__editable-label / .designer__field__editable-label ...)

        Stale handling:
        - If the *cell reference* goes stale mid-attempt, we can't "refresh" it here.
            We treat it as retryable and continue, expecting the caller to re-find the cell
            (your header writer already re-finds td per attempt).
        """
        driver = self.session.driver
        ctx = self._editor_ctx(kind="table_cell")

        def _dismiss_overlays_best_effort():
            try:
                driver.switch_to.active_element.send_keys("\u001b")  # ESC
            except Exception:
                pass

        def _js_set_value(el) -> bool:
            try:
                driver.execute_script(
                    """
                    const el = arguments[0];
                    const value = arguments[1];
                    el.focus?.();
                    el.value = value;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                    """,
                    el,
                    text,
                )
                return True
            except Exception as e:
                self.session.emit_diag(
                    Cat.TABLE,
                    f"[table] JS value-set failed: {e}",
                    **ctx,
                )
                return False

        def _js_set_textcontent(el) -> bool:
            try:
                driver.execute_script(
                    """
                    const el = arguments[0];
                    const value = arguments[1];
                    el.focus?.();
                    el.textContent = value;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                    """,
                    el,
                    text,
                )
                return True
            except Exception as e:
                self.session.emit_diag(
                    Cat.TABLE,
                    f"[table] JS textContent-set failed: {e}",
                    **ctx,
                )
                return False

        for attempt in range(1, retries + 1):
            _dismiss_overlays_best_effort()

            # NOTE: Don't return False on stale; just retry the whole attempt.
            try:
                # 1) Primary: textarea/input name=cell_title
                control = None
                for sel in ("textarea[name='cell_title']", "input[name='cell_title']"):
                    els = cell.find_elements(By.CSS_SELECTOR, sel)
                    if els:
                        control = els[0]
                        break

                if control is not None and _js_set_value(control):
                    self.session.emit_diag(
                        Cat.TABLE,
                        f"[table] cell_title set via {control.tag_name} (attempt {attempt}/{retries}).",
                        **ctx,
                    )
                    return True

                # 2) Fallback: contenteditable within cell
                eds = cell.find_elements(By.CSS_SELECTOR, "[contenteditable='true']")
                if eds:
                    try:
                        driver.execute_script("arguments[0].click();", eds[0])  # wake click
                    except Exception:
                        pass

                    if _js_set_textcontent(eds[0]):
                        self.session.emit_diag(
                            Cat.TABLE,
                            f"[table] cell set via contenteditable (attempt {attempt}/{retries}).",
                            **ctx,
                        )
                        return True

                # 3) Fallback: label-like elements (for tricky headers, checkbox cols, etc.)
                for sel in (
                    ".field__editable-label",
                    ".designer__field__editable-label",
                    ".designer__field__editable-label *",
                ):
                    els = cell.find_elements(By.CSS_SELECTOR, sel)
                    if not els:
                        continue

                    target = els[-1]  # deepest leaf tends to be the writable node
                    try:
                        driver.execute_script("arguments[0].click();", target)
                    except Exception:
                        pass

                    if _js_set_textcontent(target):
                        self.session.emit_diag(
                            Cat.TABLE,
                            f"[table] cell set via '{sel}' (attempt {attempt}/{retries}).",
                            **ctx,
                        )
                        return True

                # 4) Checkbox column header fallback (column-level label)
                try:
                    # climb to table root
                    table = cell.find_element(By.XPATH, "ancestor::table")
                    labels = table.find_elements(By.CSS_SELECTOR, ".field__editable-label")

                    if labels:
                        # heuristic: checkbox column header is last label
                        target = labels[-1]
                        driver.execute_script("arguments[0].click();", target)
                        if _js_set_textcontent(target):
                            self.session.emit_diag(
                                Cat.TABLE,
                                "[table] cell set via column-level editable-label.",
                                **ctx,
                            )
                            return True
                except StaleElementReferenceException:
                    continue
                except Exception:
                    pass

            except StaleElementReferenceException:
                self.session.emit_diag(
                    Cat.TABLE,
                    f"[table] Cell went stale during attempt {attempt}/{retries}; will retry.",
                    **ctx,
                )
                # Give Turbo a beat to settle; caller should be re-finding cells between attempts anyway.
                if attempt < retries:
                    time.sleep(0.10)
                continue
            except Exception as e:
                # Any other transient issue: retry, but keep it quiet.
                self.session.emit_diag(
                    Cat.TABLE,
                    f"[table] Unexpected error during attempt {attempt}/{retries}: {e}",
                    **ctx,
                )
                if attempt < retries:
                    time.sleep(0.10)
                continue

            if attempt < retries:
                self.session.emit_diag(
                    Cat.TABLE,
                    f"[table] No editable control matched; retrying ({attempt}/{retries}).",
                    **ctx,
                )
                time.sleep(0.10)

        self.session.emit_signal(
            Cat.TABLE,
            f"[table] Could not set cell text {text!r} after {retries} attempt(s).",
            level="warning",
            **ctx,
        )
        return False
    
    def _set_table_cell_type(self, cell, cell_type: str) -> None:
        """
        Change the type of a dynamic table cell using the per-cell dropdown.

        Supported cell_type values (for now): "heading".
        
        - Does NOT block the rest of the table config if it fails.
        - Uses JS clicks to avoid 'element not interactable' as much as possible.
        """
        driver = self.driver
        table_sel = config.BUILDER_SELECTORS["table"]
        ctx = self._editor_ctx(kind="table_cell_type")

        # We currently only implement 'heading'
        if cell_type != "heading":
            self.session.emit_diag(
                Cat.TABLE,
                f"_set_table_cell_type: unsupported type '{cell_type}', skipping.",
                **ctx,
            )
            return

        heading_class = table_sel.get(
            "cell_heading_class",
            "designer__field__editable-label--table-cell-heading",
        )
        wrapper_selector = table_sel.get(
            "editable_label_wrapper",
            ".designer__field__editable-label",
        )

        # Helper to (re)locate the editable-label wrapper in this cell
        def get_wrapper():
            try:
                return cell.find_element(By.CSS_SELECTOR, wrapper_selector)
            except NoSuchElementException:
                return None

        wrapper = get_wrapper()
        if not wrapper:
            self.session.emit_diag(
                Cat.TABLE,
                "No editable-label wrapper in cell; cannot set cell type.",
                **ctx,
            )
            return

        # Already heading? Nothing to do.
        try:
            current_classes = wrapper.get_attribute("class") or ""
            if heading_class in current_classes:
                self.session.emit_diag(
                    Cat.TABLE,
                    "Cell already has heading class; skipping type change.",
                    **ctx,
                )
                return
        except StaleElementReferenceException:
            pass

        # 1) Open the dropdown for this cell
        try:
            dropdown = cell.find_element(By.CSS_SELECTOR, ".dropdown")
            toggle = dropdown.find_element(By.CSS_SELECTOR, "button.dropdown-toggle")
        except NoSuchElementException:
            self.session.emit_diag(
                Cat.TABLE,
                "No dropdown toggle found in cell; cannot set cell type.",
                **ctx,
            )
            return

        # 2) Open the dropdown (real click to trigger Stimulus)
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
                toggle,
            )
        except Exception:
            pass

        try:
            actions = ActionChains(driver)
            actions.move_to_element(toggle).pause(0.1).click().perform()
        except Exception as e:
            self.session.emit_signal(
                Cat.TABLE,
                f"Error clicking cell type dropdown toggle: {e}",
                level="warning",
                **ctx,
            )
            return

        # 3) Locate the dropdown menu for THIS cell, then its Heading option
        try:
            menu = dropdown.find_element(By.CSS_SELECTOR, "ul.dropdown-menu.ca-dropdown-menu")
            heading_btn = menu.find_element(
                By.CSS_SELECTOR,
                "button[data-url*='type=heading']",
            )
        except NoSuchElementException:
            self.session.emit_signal(
                Cat.TABLE,
                "Heading option not found in this cell's dropdown menu.",
                level="warning",
                **ctx,
            )
            return

        # 4) Click Heading via JS (bypassing size/visibility quirks)
        try:
            driver.execute_script("arguments[0].click();", heading_btn)
        except Exception as e:
            self.session.emit_signal(
                Cat.TABLE,
                f"JS click on 'Heading' menu item failed: {e}",
                level="warning",
                **ctx,
            )
            return
        
        # 5) Optionally: light-touch check to see if the class appears, but don’t wait long
        try:
            wrapper = get_wrapper()
            if wrapper:
                classes = wrapper.get_attribute("class") or ""
                if heading_class in classes:
                    self.session.emit_diag(
                        Cat.TABLE,
                        "Cell heading type applied (wrapper has heading class).",
                        **ctx,
                    )
        except StaleElementReferenceException:
            # Turbo may have re-rendered; it's fine, we already clicked
            pass

    def _set_column_types(
        self,
        field_el,
        column_types: Optional[Sequence[Optional[str]]],
    ) -> None:
        """
        Apply column types from a config-driven list.

        `column_types[i]` refers to the i-th *data* column:
          0 = first data column
          1 = second data column, etc.

        Supported values (for now):
          - 'checkbox'
          - 'text'
          - 'text_field'
          - 'date_field'
          - 'heading'   (use sparingly – usually only col 0 if at all)

        Any value of None is ignored.
        """
        if not column_types:
            return
        ctx = self._editor_ctx(kind="table_column_types")

        for idx, col_type in enumerate(column_types):
            if not col_type:
                continue  # explicitly skipped

            # Normalise string just in case
            col_type = col_type.strip().lower()

            # Only act on types we know how to translate directly to CA's type param
            if col_type in {"checkbox", "text", "text_field", "date_field", "heading"}:
                try:
                    self._set_column_type(field_el, col_index=idx, type_name=col_type)
                except Exception as e:
                    self.session.emit_signal(
                        Cat.TABLE,
                        f"Failed to set column {idx} type to '{col_type}': {e}",
                        level="warning",
                        **ctx,
                    )
            else:
                self.session.emit_diag(
                    Cat.TABLE,
                    f"Column {idx} type '{col_type}' not implemented; skipping.",
                    **ctx,
                )

    def _set_column_type(self, field_el, col_index: int, type_name: str) -> None:
        """
        Bulk-update the type for a whole column (using the column header dropdown).

        col_index is 0-based for *data* columns:
            0 = first data column
            1 = second data column, etc.

        type_name: "heading", "text", "text_field", "date_field", "checkbox"
        """
        selectors = config.BUILDER_SELECTORS["table"]
        ctx = self._editor_ctx(kind="table_column_type")

        self.session.emit_diag(
            Cat.TABLE,
            f"Applying column type '{type_name}' to data column {col_index} via bulk update.",
            **ctx,
        )

        # 1. Locate table + header row (fresh each time)
        try:
            table_root = self._get_dynamic_table_root(field_el)
        except Exception as e:
            self.session.emit_signal(
                Cat.TABLE,
                f"Cannot locate table root for column update: {e}",
                level="warning",
                **ctx,
            )
            return

        # Use the actual header cells in <thead>, safer for column actions
        header_cells = table_root.find_elements(
            By.CSS_SELECTOR, selectors["header_cells"]
        )
        if not header_cells:
            self.session.emit_signal(
                Cat.TABLE,
                "No header cells found; cannot set column type.",
                level="warning",
                **ctx,
            )
            return

        # Skip control column at index 0
        th_index = col_index + 1  # column 0 = first data column
        if th_index >= len(header_cells):
            self.session.emit_signal(
                Cat.TABLE,
                f"No header cell for data column {col_index}.",
                level="warning",
                **ctx,
            )
            return

        header_cell = header_cells[th_index]

        # 2. Locate column actions container
        try:
            column_actions_sel = selectors.get(
                "column_actions",
                ".dynamic-table__actions.dynamic-table__actions--columns",
            )
            actions_container = header_cell.find_element(
                By.CSS_SELECTOR,
                column_actions_sel
            )
        except NoSuchElementException as e:
            self.session.emit_signal(
                Cat.TABLE,
                f"No column actions container found for data column {col_index}.",
                level="warning",
                **ctx,
            )
            return

        # 3. Inside the column actions, find the bulk_update button for the requested type
        try:
            menu = actions_container.find_element(
                By.CSS_SELECTOR, "ul.dropdown-menu.ca-dropdown-menu"
            )
            type_btn = menu.find_element(
                By.CSS_SELECTOR, f"button[data-url*='type={type_name}']"
            )
        except NoSuchElementException:
            self.session.emit_signal(
                Cat.TABLE,
                f"No bulk-update button for type '{type_name}' found in column {col_index} actions.",
                level="warning",
                **ctx,
            )
            return
        except StaleElementReferenceException:
            self.session.emit_signal(
                Cat.TABLE,
                f"Column actions became stale while looking for type '{type_name}' in col {col_index}.",
                level="warning",
                **ctx,
            )
            return

        # 5. Dispatch synthetic click on the turbo button
        self._dispatch_turbo_click(type_btn)
        self.session.emit_diag(
            Cat.TABLE,
            f"Column {col_index}: requested type '{type_name}' via bulk-update button.",
            **ctx,
        )

    def _set_row_type(self, field_el, row_index: int, type_name: str) -> None:
        """
        Bulk-update a whole row's cell type (e.g. 'heading') using the row actions menu.

        type_name: 'heading', 'text', 'text_field', 'date_field', 'checkbox'
        """
        selectors = config.BUILDER_SELECTORS["table"]
        ctx = self._editor_ctx(kind="table_row_type")
    
        self.session.emit_diag(
            Cat.TABLE,
            f"Applying row type '{type_name}' to row {row_index} via bulk update.",
            **ctx,
        )

        try:
            table_root = self._get_dynamic_table_root(field_el)
            body_rows = table_root.find_elements(By.CSS_SELECTOR, selectors["body_rows"])
        except Exception as e:
            self.session.emit_signal(
                Cat.TABLE,
                f"Failed to locate table/rows for row type '{type_name}': {e}",
                level="warning",
                **ctx,
            )
            return
        
        if not body_rows or row_index >= len(body_rows):
            self.session.emit_signal(
                Cat.TABLE,
                f"No body row at index {row_index}; cannot set row type.",
                level="warning",
                **ctx,
            )
            return

        # row = body_rows[row_index]

        try:
            # Find row actions container
            row_actions_sel = selectors.get(
                "row_actions",
                ".dynamic-table__actions.dynamic-table__actions--rows",
            )
            row_actions_groups = table_root.find_elements(By.CSS_SELECTOR, row_actions_sel)
            if not row_actions_groups:
                raise NoSuchElementException("No row actions groups found under table_root")
            # If CA renders one group per row, index into it. If it renders one shared group,
            # row_index=0 is fine (and you can ignore the index).
            actions_container = row_actions_groups[min(row_index, len(row_actions_groups) - 1)]
        except NoSuchElementException:
            self.session.emit_signal(
                Cat.TABLE,
                f"No row actions container found for row {row_index}.",
                level="warning",
                **ctx,
            )
            return
        
        try:
            # Menu and type button
            menu = actions_container.find_element(
                By.CSS_SELECTOR, "ul.dropdown-menu.ca-dropdown-menu"
            )
            type_btn = menu.find_element(
                By.CSS_SELECTOR, f"button[data-url*='type={type_name}']"
            )
        except NoSuchElementException as e:
            self.session.emit_signal(
                Cat.TABLE,
                f"Cannot set row type for row {row_index}: {e}",
                level="warning",
                **ctx,
            )
            return
        except StaleElementReferenceException:
            self.session.emit_signal(
                Cat.TABLE,
                f"Row actions became stale while looking for type '{type_name}' in row {row_index}.",
                level="warning",
                **ctx,
            )
            return
        except Exception as e:
            self.session.emit_signal(
                Cat.TABLE,
                f"Error setting row type '{type_name}' for row {row_index}: {e}",
                level="warning",
                **ctx,
            )
            return

        # Dispatch synthetic click (no native click() → no size/location complaints)
        self._dispatch_turbo_click(type_btn)
        self.session.emit_diag(
            Cat.TABLE,
            f"Row {row_index}: requested type '{type_name}' via bulk-update button.",
            **ctx,
        )        
        # Post-click settle: wait for the row to actually become heading (best-effort)
        if type_name == "heading":
            try:
                self.session.get_wait(timeout=3).until(lambda d: self._row_looks_like_heading(field_el, row_index))
            except TimeoutException:
                self.session.emit_diag(
                    Cat.TABLE,
                    f"Row {row_index} did not confirm as heading within settle window.",
                    **ctx,
                )

    def _apply_table_cell_override(
        self,
        table_root,
        row_idx: int,
        col_idx: int,
        cell_cfg: TableCellConfig,
    ) -> bool:
        """
        Apply a specific override to a given (row, col) cell:
        - text
        - cell_type (heading/text/checkbox/etc)
        """
        selectors = config.BUILDER_SELECTORS["table"]
        body_rows = table_root.find_elements(
            By.CSS_SELECTOR,
            selectors["body_rows"],
        )
        if row_idx >= len(body_rows):
            return False
        row = body_rows[row_idx]
        cells = row.find_elements(By.CSS_SELECTOR, "td, th")

        # ✅ CloudAssess has a control column at index 0
        dom_offset = 1
        dom_col_idx = col_idx + dom_offset

        if dom_col_idx >= len(cells):
            return False

        cell = cells[dom_col_idx]

        if cell_cfg.text is not None:
            # 1) Try to activate the cell first
            try:
                self.session.click_element_safely(cell)  # or JS click
            except Exception:
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cell)
                    self.driver.execute_script("arguments[0].click();", cell)
                except Exception:
                    pass

            # 2) Re-query inputs after activation (important)
            inputs = cell.find_elements(By.CSS_SELECTOR, "textarea[name='cell_title'], input[name='cell_title']")
            if inputs:
                try:
                    self.session.clear_and_type(inputs[0], cell_cfg.text)
                    return True
                except Exception:
                    # fall through
                    pass

            # 3) Fallback: set text via editable label (same style as row-label fallback)
            labels = cell.find_elements(By.CSS_SELECTOR, ".field__editable-label")
            if labels:
                self.session.clear_and_type(labels[0], cell_cfg.text)
                return True
    
        if cell_cfg.cell_type is not None:
            # TODO: similar to column_types; depends on CA's UI
            pass

        return False

    def _wait_for_header_editors_ready(self, field_el, timeout: int = 4) -> bool:
        """
        After setting row 0 to 'heading', wait until the first body row exposes
        editable header controls (textarea[name='cell_title']) in at least one cell.
        """
        driver = self.driver
        selectors = config.BUILDER_SELECTORS["table"]

        wait = self.session.get_wait(timeout)

        def _ready(_):
            try:
                table_root = self._get_dynamic_table_root(field_el)
                body_rows = table_root.find_elements(By.CSS_SELECTOR, selectors["body_rows"])
                if not body_rows:
                    return False

                header_row = body_rows[0]
                cells = header_row.find_elements(By.CSS_SELECTOR, "th, td")
                if not cells:
                    return False

                # If any cell has a cell_title textarea, the row is in the editable state.
                for c in cells:
                    if c.find_elements(By.CSS_SELECTOR, "textarea[name='cell_title']"):
                        return True
                return False
            except Exception:
                return False

        try:
            wait.until(_ready)
            return True
        except Exception:
            return False

    def _dispatch_turbo_click(self, button_el) -> None:
        """
        Dispatch a synthetic click event on a turbo-put/turbo-post button.

        This avoids Chrome's 'element not interactable' restrictions on
        hidden elements, while still triggering CA's Stimulus controllers.
        """
        driver = self.driver
        ctx = self._editor_ctx(kind="table_click")

        if button_el is None:
            return

        try:
            driver.execute_script(
                """
                const btn = arguments[0];
                if (!btn) return;
                const evt = new MouseEvent('click', {
                  view: window,
                  bubbles: true,
                  cancelable: true
                });
                btn.dispatchEvent(evt);
                """,
                button_el,
            )
            self.session.emit_diag(
                Cat.TABLE,
                "Dispatched synthetic click on turbo button.",
                **ctx,
            )
        except Exception as e:
            self.session.emit_signal(
                Cat.TABLE,
                f"Failed to dispatch turbo click: {e}",
                level="warning",
                **ctx,
            )

    def _row_looks_like_heading(self, field_el, row_index: int) -> bool:
        selectors = config.BUILDER_SELECTORS["table"]
        table_root = self._get_dynamic_table_root(field_el)
        body_rows = table_root.find_elements(By.CSS_SELECTOR, selectors["body_rows"])
        if row_index >= len(body_rows):
            return False

        row = body_rows[row_index]
        # Look for a heading marker anywhere in that row
        return bool(row.find_elements(By.CSS_SELECTOR, f".{selectors['cell_heading_class']}"))

    def _norm_text(self, s: str | None) -> str:
        return " ".join((s or "").split())
    
    def _ensure_field_active(self, field_el, timeout: int = 2) -> bool:
        driver = self.driver
        wait = self.session.get_wait(timeout)

        def is_active(_):
            try:
                cls = field_el.get_attribute("class") or ""
                return "designer__field--active" in cls
            except Exception:
                return False

        if is_active(None):
            return True

        # Try clicking the title label (most reliable)
        try:
            title = field_el.find_element(By.CSS_SELECTOR, "h2.field__editable-label, .designer__field__editable-label--title")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", title)
            driver.execute_script("arguments[0].click();", title)
        except Exception:
            # Fallback: offset click on the field root
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field_el)
                ActionChains(driver).move_to_element(field_el).move_by_offset(8, 8).pause(0.05).click().perform()
            except Exception:
                return False

        try:
            wait.until(is_active)
            return True
        except Exception:
            return False

#  --- Recording and tracking skip events in editor process ---

    def _record_config_skip(
        self,
        *,
        kind: str,
        reason: str,
        retryable: bool,
        field_id: str | None = None,
        field_title: str | None = None,
        requested: dict | None = None,
    ) -> None:
        self.record_skip({
            "kind": kind,
            "reason": reason,
            "retryable": retryable,
            "field_id": field_id,
            "field_title": field_title,
            "requested": requested or {},
        })

    def record_skip(self, event: dict) -> None:
        # event should already be structured; keep editor dumb
        self._skip_events.append(event)

    def pop_skip_events(self) -> list[dict]:
        ev = self._skip_events
        self._skip_events = []
        return ev
