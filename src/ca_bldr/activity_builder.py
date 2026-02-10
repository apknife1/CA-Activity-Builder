# src/ca_bldr/activity_builder.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Any
from collections import Counter
import re

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, WebDriverException, StaleElementReferenceException, NoSuchElementException, MoveTargetOutOfBoundsException

from .session import CASession

from .field_types import FIELD_TYPES, FieldTypeSpec
from .field_handles import FieldHandle
from .activity_sections import ActivitySections
from .activity_editor import ActivityEditor
from .activity_registry import ActivityRegistry
from .instrumentation import Cat, LogMode
from .. import config  # src/config.py

@dataclass(frozen=True)
class DropzoneCandidate:
    el: WebElement
    score: float
    reason: str

@dataclass(frozen=True)
class DropzoneProbe:
    rect: dict
    center_topmost_ok: bool
    topmost_summary: str
    turbo_busy_hint: str
    note: str = ""

@dataclass
class DropGestureResult:
    ok: bool
    reason: str
    dz_id: str
    offset_used: Optional[tuple[int, int]] = None
    used_js_fallback: bool = False

class CAActivityBuilder:
    def __init__(
            self,
            session: CASession,
            sections: ActivitySections,
            editor: ActivityEditor,
            registry: ActivityRegistry):
        self.session = session
        self.driver = session.driver
        self.wait = session.wait
        self.logger = session.logger
        self.sections = sections
        self.editor = editor
        self.registry = registry
        self.hard_resync_count = 0

    def _ctx(self, *, kind: str | None = None, sec=None, fid=None, spec=None, fi=None, a: str | None=None) -> dict[str, Any]:
        ctx = {
            "sec": sec or (self.sections.current_section_id or ""),
            "kind": kind or "",
            "fid": fid,
            "type": getattr(spec, "key", None) if spec else None,
            "fi": fi,
        }
        if a is not None:
            ctx["a"] = a
        return ctx

    def open_dev_unit(self, unit_url: str):
        self.session.emit_diag(
            Cat.STARTUP,
            "Opening dev unit",
            unit_url=unit_url,
            **self._ctx(kind="start"),
        )
        self.driver.get(unit_url)
        # wait for something that proves the page has loaded


    def _instrument(self) -> bool:
        return bool(getattr(config, "INSTRUMENT_DROPS", False))

    def _ensure_sidebar_visible(self, kind: str, timeout: int = 10) -> bool:
        """
        Ensure the given sidebar is visible.

        kind:
            'fields'   -> Add Fields (data-type='fields')
            'sections' -> Sections (data-type='sections')

        Returns:
            True if the requested sidebar tab is visible by the end, False otherwise.
        """
        driver = self.session.driver
        wait = self.session.get_wait(timeout)

        if kind =="fields":
            self.session.counters.inc("sidebar.fields_ensure_calls")

        ctx = self._ctx(kind=kind)
        self.session.emit_diag(
            Cat.SIDEBAR,
            f"Ensure {kind} sidebar visibility",
            key=f"SIDEBAR.{kind}.ensure.start",
            **ctx,
        )

        sidebars = config.BUILDER_SELECTORS.get("sidebars", {})
        cfg = sidebars.get(kind, {})

        tab_sel = cfg.get("tab")
        frame_sel = cfg.get("frame")

        if not tab_sel:
            self.session.emit_signal(
                Cat.SIDEBAR,
                f"Config error: missing tab selector for kind={kind}",
                kind=kind,
                level="error",
            )
            return False

        def _tab_is_visible() -> bool:
            try:
                el = driver.find_element(By.CSS_SELECTOR, tab_sel)
                return el.is_displayed()
            except Exception:
                self.session.emit_diag(
                    Cat.SIDEBAR,
                    f"{kind} sidebar not visible (fast-path)",
                    key=f"SIDEBAR.{kind}.not_visible",
                    every_s=1.0,
                    **ctx,
                )
                return False

        # Fast path: if the tab is already visible, we're done.
        if _tab_is_visible():
            if kind == "fields":
                self.session.counters.inc("sidebar.fields.fastpath_hits")
            self.session.emit_diag(
                Cat.SIDEBAR,
                f"{kind} sidebar already visible (fast-path)",
                key=f"SIDEBAR.{kind}.visible",
                every_s=1.0,
                **ctx,
            )
            return True

        # Helper: click the toggle button for this sidebar once
        def _click_toggle_once(ctx_attempt: dict) -> bool:
            nonlocal driver

            if kind == "fields":
                toggle_sel = cfg.get("toggle_button", "button[data-type='fields']")
                self.session.counters.inc("sidebar.fields.toggle_clicks")
                self.session.emit_diag(
                    Cat.SIDEBAR,
                    f"{kind} sidebar toggle click",
                    **{**ctx_attempt,"method": "selector"},
                )
                btn = driver.find_element(By.CSS_SELECTOR, toggle_sel)

                clicked = False
                if hasattr(self.session, "click_element_safely"):
                    clicked = self.session.click_element_safely(btn)
                if not clicked:
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    driver.execute_script("arguments[0].click();", btn)
                return True

            elif kind == "sections":
                sections_btn = None

                # a) Try using the onclick attribute
                try:
                    onclick_sel = cfg.get(
                        "toggle_button_onclick",
                        "button[onclick*='toggleSidebar'][onclick*='sections']",
                    )
                    self.session.emit_diag(
                        Cat.SIDEBAR,
                        f"{kind} sidebar toggle click",
                        **{**ctx_attempt, "method": "onclick_selector"},
                    )
                    sections_btn = driver.find_element(By.CSS_SELECTOR, onclick_sel)
                except Exception:
                    # fallback to text scan
                    self.session.emit_diag(
                        Cat.SIDEBAR,
                        f"{kind} sidebar toggle click",
                        **{**ctx_attempt, "method": "text_scan"},
                    )
                    candidates = driver.find_elements(By.TAG_NAME, "button")
                    for b in candidates:
                        try:
                            text = (b.text or "").strip()
                        except Exception:
                            text = ""
                        if text and "sections" in text.lower():
                            sections_btn = b
                            break

                if sections_btn is None:
                    self.session.emit_signal(
                        Cat.SIDEBAR,
                        f"Sections toggle button not found",
                        level="error",
                        **ctx_attempt,
                    )
                    return False

                clicked = False
                if hasattr(self.session, "click_element_safely"):
                    clicked = self.session.click_element_safely(sections_btn)
                if not clicked:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});", sections_btn
                    )
                    driver.execute_script("arguments[0].click();", sections_btn)
                return True

            else:
                self.session.emit_signal(
                    Cat.SIDEBAR,
                    f"Unknown sidebar kind: {kind}.",
                    level="error",
                    **ctx_attempt,
                )
                return False

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            ctx_attempt = self._ctx(kind=kind, a=f"attempt={attempt}/{max_attempts}")

            self.session.emit_diag(
                Cat.SIDEBAR,
                f"Ensure {kind} sidebar visible",
                **ctx_attempt,
            )
            try:
                # 1) Click the toggle
                if not _click_toggle_once(ctx_attempt):
                    if attempt == max_attempts:
                        self.session.emit_signal(
                            Cat.SIDEBAR,
                            f"FAILED to click {kind} sidebar toggle after {max_attempts} attempts",
                            level="error",
                            **ctx_attempt,
                        )
                        return False
                    continue  # try again

                # 2) Wait for tab to be visible
                def tab_visible(_):
                    return _tab_is_visible()

                wait.until(tab_visible)
                self.session.emit_diag(
                    Cat.SIDEBAR,
                    f"{kind} sidebar visible",
                    **ctx_attempt,
                )

                # 3) If this sidebar has a turbo-frame, wait for it too
                if frame_sel:

                    def frame_ready(_):
                        try:
                            frame = driver.find_element(By.CSS_SELECTOR, frame_sel)
                            return frame.is_displayed()
                        except Exception:
                            return False

                    wait.until(frame_ready)
                    self.session.emit_diag(
                        Cat.SIDEBAR,
                        f"{kind} sidebar frame {frame_sel} is loaded",
                        **ctx_attempt,
                    )

                # If we got here, everything is good
                return True

            except TimeoutException as e:
                if attempt < max_attempts:
                    self.session.emit_diag(
                        Cat.SIDEBAR,
                        f"Timed out ensuring {kind} sidebar visibility on attempt {attempt}: {str(e)}",
                        key=f"SIDEBAR.{kind}.timeout",
                        every_s=1.0,
                        **ctx_attempt,
                    )
                else:
                    self.session.emit_signal(
                        Cat.SIDEBAR,
                        f"Timed out ensuring {kind} sidebar visibility after {max_attempts} attempts",
                        level="warning",
                        **ctx_attempt,
                    )
                    return False
            except StaleElementReferenceException as e:
                if attempt < max_attempts:
                    self.session.emit_diag(
                        Cat.SIDEBAR,
                        f"Stale element while ensuring {kind} sidebar visibility on attempt {attempt}: {str(e)}",
                        **ctx_attempt,
                    )
                else:
                    self.session.emit_signal(
                        Cat.SIDEBAR,
                        f"Giving up ensuring {kind} sidebar visibility after {max_attempts} attempts due to stale elements.",
                        level="error",
                        **ctx_attempt,
                    )
                    return False
            except WebDriverException as e:
                if attempt < max_attempts:
                    self.session.emit_diag(
                        Cat.SIDEBAR,
                        f"WebDriver error while ensuring {kind} sidebar visibility on attempt {attempt}: {str(e)}",
                        **ctx_attempt,
                    )
                else:
                    self.session.emit_signal(
                        Cat.SIDEBAR,
                        f"Giving up ensuring {kind} sidebar visibility after {max_attempts} attempts due to WebDriver errors.",
                        level="error",
                        **ctx_attempt,
                    )
                    return False
            except Exception as e:
                self.session.emit_signal(
                    Cat.SIDEBAR,
                    f"Unexpected error while ensuring {kind} sidebar visibility on attempt {attempt}: {str(e)}",
                    level="error",
                    **ctx_attempt,
                )
                return False

        # Fallback (shouldn't be reached)
        self.session.emit_signal(
            Cat.SIDEBAR,
            f"Unexpected outcome while ensuring {kind} sidebar visibility. Needs investigation.",
            level="error",
            **ctx,
        )
        return False

    def _fields_sidebar_tab_visible(self) -> bool:
        sel = config.BUILDER_SELECTORS["sidebars"]["fields"]["tab"]
        try:
            el = self.session.driver.find_element(By.CSS_SELECTOR, sel)
            return el.is_displayed()
        except Exception:
            return False

    def _try_open_fields_sidebar_from_field_settings(
        self,
        *,
        timeout: int = 5,
        ctx: dict | None = None,
    ) -> bool:
        """
        Fast-path: when the Field Settings sidebar is open, click the "Add new field"
        button to return to the Fields sidebar without a full ensure pass.
        """
        if self._fields_sidebar_tab_visible():
            return True

        ctx = ctx or self._ctx(kind="fields_tab")
        self.session.counters.inc("sidebar.fields.add_new_fastpath_attempts")
        self.session.emit_diag(
            Cat.SIDEBAR,
            "Attempting add-new-field fastpath",
            **ctx,
        )

        driver = self.session.driver
        wait = self.session.get_wait(timeout)

        try:
            panel = driver.find_element(
                By.CSS_SELECTOR, config.BUILDER_SELECTORS["properties"]["root"]
            )
        except Exception:
            self.session.emit_diag(
                Cat.SIDEBAR,
                "Field settings panel not visible for add-new-field fastpath",
                **ctx,
            )
            self.session.counters.inc("sidebar.fields.add_new_fastpath_fallback")
            return False

        candidates: list = []
        for btn in panel.find_elements(By.TAG_NAME, "button"):
            try:
                text = (btn.text or "").strip().lower()
                aria = (btn.get_attribute("aria-label") or "").strip().lower()
                title = (btn.get_attribute("title") or "").strip().lower()
            except Exception:
                continue

            if "add new field" in text or "add new field" in aria or "add new field" in title:
                candidates.append(btn)
                continue
            if text == "add field" or aria == "add field" or title == "add field":
                candidates.append(btn)

        if not candidates:
            self.session.emit_diag(
                Cat.SIDEBAR,
                "Add-new-field button not found in field settings panel",
                **ctx,
            )
            self.session.counters.inc("sidebar.fields.add_new_fastpath_fallback")
            return False

        for btn in candidates:
            clicked = False
            try:
                if hasattr(self.session, "click_element_safely"):
                    clicked = self.session.click_element_safely(btn)
                if not clicked:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});", btn
                    )
                    driver.execute_script("arguments[0].click();", btn)
            except Exception:
                continue

            try:
                wait.until(lambda _: self._fields_sidebar_tab_visible())
            except TimeoutException:
                continue

            if self._fields_sidebar_tab_visible():
                self.session.counters.inc("sidebar.fields.add_new_fastpath_ok")
                self.session.emit_diag(
                    Cat.SIDEBAR,
                    "Add-new-field fastpath succeeded",
                    **ctx,
                )
                return True

        self.session.counters.inc("sidebar.fields.add_new_fastpath_fallback")
        self.session.emit_diag(
            Cat.SIDEBAR,
            "Add-new-field fastpath did not open fields sidebar; falling back",
            **ctx,
        )
        return False

    def _activate_fields_tab_for_spec(self, spec: FieldTypeSpec):
        """
        Activate the correct tab button in the Fields sidebar based on spec.sidebar_tab_label.

        Returns:
            The tab button WebElement if activation was attempted, or None on failure.
        """
        driver = self.session.driver
        wait = self.session.wait
        ctx = self._ctx(kind="fields_tab")

        tab_map = {
            "Auto marked": config.BUILDER_SELECTORS["fields_sidebar"]["tab_auto_marked"],
            "Marked manually": config.BUILDER_SELECTORS["fields_sidebar"]["tab_marked_manually"],
            "Text": config.BUILDER_SELECTORS["fields_sidebar"]["tab_text"],
            "Interactive": config.BUILDER_SELECTORS["fields_sidebar"]["tab_interactive"],
            "Confirmation": config.BUILDER_SELECTORS["fields_sidebar"]["tab_confirmation"],
        }
        tab_selector = tab_map.get(spec.sidebar_tab_label)

        if not tab_selector:
            self.session.emit_diag(
                Cat.SIDEBAR,
                f"Unknown sidebar_tab_label '{spec.sidebar_tab_label}', defaulting to 'Text'.",
                reason="unknown_tab_label",
                **ctx,
            )
            tab_selector = config.BUILDER_SELECTORS["fields_sidebar"]["tab_text"]

        # Scope lookup to the Fields sidebar root so we don't hit random duplicates
        fields_root_sel = config.BUILDER_SELECTORS["sidebars"]["fields"]["tab"]
        try:
            fields_root = wait.until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, fields_root_sel))
            )
        except Exception as e:
            self.session.emit_signal(
                Cat.SIDEBAR,
                "Fields sidebar root not visible when activating tab",
                level="error",
                **ctx,
            )
            return None

        try:
            tab_btn = fields_root.find_element(By.CSS_SELECTOR, tab_selector)
        except Exception:
            self.session.emit_signal(
                Cat.SIDEBAR,
                f"Could not find tab button '{spec.sidebar_tab_label}'",
                level="error",
                **ctx,
            )
            return None

        try:
            already_active = False
            try:
                cls = tab_btn.get_attribute("class") or ""
                aria = (tab_btn.get_attribute("aria-selected") or "").lower()
                already_active = ("active" in cls) or (aria == "true")
            except StaleElementReferenceException:
                already_active = False

            if already_active:
                self.session.counters.inc("sidebar.tab.fastpath")
                self.session.emit_diag(
                    Cat.SIDEBAR,
                    f"Tab already active; skipping click",
                    tab=spec.sidebar_tab_label,
                    key="SIDEBAR.tab.fastpath",
                    every_s=1.0,
                    **ctx,
                )
                return tab_btn

            self.session.counters.inc("sidebar.tab.activate")
            self.session.emit_diag(
                Cat.SIDEBAR,
                f"Activating '{spec.sidebar_tab_label}' tab",
                **ctx,
            )
            driver.execute_script("arguments[0].click();", tab_btn)
            return tab_btn
        except Exception:
            self.session.emit_signal(
                Cat.SIDEBAR,
                f"Could not click '{spec.sidebar_tab_label}' tab button",
                level="error",
                **ctx,
            )
            return None

    # --- add a new field to the canvas ---
    def add_field_from_spec(
        self,
        key: str,
        section_title: str | None = None,
        section_index: int | None = None,
        *,
        insert_after_field_id: str | None = None,
        drop_location: str = "section_bottom",
        fi_index: int | None = None,
        field_title: str | None = None,
    ):
        """
        Add a field of the given type (see FIELD_TYPES) and return its FieldHandle.

        This is the core "create field" helper. It must be resilient to Turbo/Stimulus
        re-renders, stale element references, and transient DOM states.

        Strategy:
        1) Ensure the target section is selected and the canvas is aligned.
        2) Snapshot existing field IDs (STRICT) for this field type in this section.
        3) Drag/drop the toolbox card into the section dropzone.
        4) Detect the new field by ID-diff, but *filter using the registry* so we
            never "rediscover" an existing field as new.
        5) Verify existence by re-finding the field root by ID (stable).
        6) Create a FieldHandle and register it.
        """
        driver = self.driver
        wait = self.wait
        sections = self.sections
        editor = self.editor
        registry = self.registry

        # self.hard_resync_count = 0
        used_hard_resync = False

        spec: FieldTypeSpec = FIELD_TYPES[key]
        canvas_sel = spec.canvas_field_selector

        handle = None
        sec_handle = None

        # -----------------------------
        # Strict ID + snapshot helpers
        # -----------------------------

        def _field_id_strict(el) -> str:
            try:
                return editor.try_get_field_id_strict(el) or ""
            except Exception:
                return ""

        def _snapshot_fields():
            """
            Return (elements, ids) for this field type in current section.
            IDs are strict and may be temporarily incomplete if DOM is mid-rerender.
            """
            els = driver.find_elements(By.CSS_SELECTOR, canvas_sel)
            ids = { _field_id_strict(e) for e in els }
            ids.discard("")
            return els, ids
        
        def _current_section_id() -> str:
            sid = sections.current_section_id or ""
            if sid:
                return sid

            # Fallback: derive from create_field_path hidden input
            try:
                create_field_path = driver.find_element(
                    By.CSS_SELECTOR, "input#create_field_path"
                ).get_attribute("value") or ""
                m = re.search(r"/sections/(\d+)/fields", create_field_path)
                if m:
                    return m.group(1)
            except Exception:
                pass
            return ""

        def _dom_index_in_section(field_id: str) -> int:
            ids = self._get_active_section_field_ids() or []
            try:
                return ids.index(str(field_id))
            except ValueError:
                return -1

        def _registry_ids_for_current_section() -> set[str]:
            sid = _current_section_id()
            if not sid:
                return set()
            return {
                fh.field_id
                for fh in registry.fields_by_type(key, section_id=sid)
                if fh.field_id
            }

        def _verify_field_by_id(fid: str):
            """
            Confirm the field exists by re-finding it by id.
            This is the only 'truth test' we trust after diffing.
            """
            return editor.get_field_by_id(fid)

        def _wrapper_matches_type(fid: str) -> bool:
            try:
                wrapper = driver.find_element(By.CSS_SELECTOR, f"#section-field-{fid}")
                return bool(wrapper.find_elements(By.CSS_SELECTOR, canvas_sel))
            except Exception:
                return False

        def _hard_resync_once_or_bail(reason: str) -> bool:
            nonlocal used_hard_resync
            if used_hard_resync:
                ctx = self._ctx(kind="hard_resync", fi=fi_index, a="reuse_refused")
                self.session.emit_signal(
                    Cat.PHANTOM,
                    "Hard resync already used for this add attempt",
                    reason=reason,
                    level="warning",
                    **ctx,
                )
                return False

            did = _hard_resync_or_bail(reason=reason)  # your existing helper
            if did:
                used_hard_resync = True
            return did
        
        def _hard_resync_or_bail(reason: str) -> bool:
            """
            Returns True if we performed a hard resync and should retry the add.
            Returns False if we should stop (skip/abort).
            """

            max_resync = getattr(config, "HARD_RESYNC_MAX_PER_ACTIVITY", 3)
            abort_on_fail = getattr(config, "PHANTOM_TIMEOUT_ABORT", False)

            if self.hard_resync_count >= max_resync:
                ctx = self._ctx(kind="hard_resync", fi=fi_index, a="budget_exhausted")
                self.session.emit_signal(
                    Cat.PHANTOM,
                    "Hard resync budget exhausted",
                    count=self.hard_resync_count,
                    limit=max_resync,
                    reason=reason,
                    level="warning",
                    **ctx,
                )
                if abort_on_fail:
                    # Fail out of the activity build altogether (caller should catch)
                    raise RuntimeError(f"Hard resync budget exhausted: {reason}")
                return False  # skip this field

            if not hasattr(sections, "hard_resync_current_section"):
                ctx = self._ctx(kind="hard_resync", fi=fi_index, a="missing_hook")
                self.session.emit_signal(
                    Cat.PHANTOM,
                    "Hard resync helper missing",
                    reason=reason,
                    level="warning",
                    **ctx,
                )
                if abort_on_fail:
                    raise RuntimeError(f"No hard resync available: {reason}")
                return False

            self.hard_resync_count += 1
            self.session.counters.inc("section.hard_resyncs")
            self.session.counters.inc("phantom.resync_triggered")
            ctx = self._ctx(kind="hard_resync", fi=fi_index, a="triggered")
            self.session.emit_signal(
                Cat.PHANTOM,
                "Hard resync triggered",
                count=self.hard_resync_count,
                limit=max_resync,
                reason=reason,
                **ctx,
            )
            ok = sections.hard_resync_current_section()
            if not ok:
                self.session.counters.inc("phantom.resync_failed")
                self.session.emit_signal(
                    Cat.PHANTOM,
                    "Hard resync attempt failed",
                    reason=reason,
                    level="warning",
                    **ctx,
                )
                if abort_on_fail:
                    raise RuntimeError(f"Hard resync failed: {reason}")
                return False

            self.session.counters.inc("phantom.resync_succeeded")
            return True  # resynced; retry add

        # -----------------------------
        # Ensure section + canvas
        # -----------------------------

        sec_handle = sections.ensure_section_ready(
            section_title=section_title,
            index=section_index,
        )
        if sec_handle is None:
            self.session.emit_signal(
                Cat.SECTION,
                "Could not prepare question section",
                section_title=section_title,
                section_index=section_index,
                level="error",
                **self._ctx(kind="section_prepare"),
            )
            return None

        # Wait for the canvas to actually match the section we think is active
        if not sections.wait_for_canvas_for_current_section():
            self.session.emit_signal(
                Cat.SECTION,
                "Canvas not aligned with current section",
                section_id=sec_handle.section_id,
                section_title=sec_handle.title,
                field_type=spec.display_name,
                level="error",
                **self._ctx(kind="canvas_align"),
            )
            return None

        # Sanity check: we expect section layout (#section-fields) to be present
        if not driver.find_elements(By.CSS_SELECTOR, "#section-fields"):
            self.session.emit_signal(
                Cat.SECTION,
                "Section fields container missing",
                level="error",
                **self._ctx(kind="section_mode"),
            )
            return None
        
        # --- SECTION MODE ---
        # -----------------------------
        # Create attempts
        # -----------------------------
        max_create_attempts = 3
        new_field = None
        new_id: str = ""
        index_in_section = -1

        dz_id = None

        for create_attempt in range(1, max_create_attempts + 1):
            # Recompute before-count each create attempt, in case previous attempts partially changed things
            before_fields, before_ids = _snapshot_fields()
            before_count = len(before_fields)
            registry_ids = _registry_ids_for_current_section()
            dom_before_ids = self._get_active_section_field_ids() or []
            dom_before_set = set(dom_before_ids)
            self.session.emit_diag(
                Cat.DROP,
                "Create attempt section snapshot",
                section=sections.current_section_id,
                create_attempt=create_attempt,
                before_count=before_count,
                registry_known=len(registry_ids),
                **self._ctx(kind="drop", spec=spec, fi=fi_index, a=f"create={create_attempt}"),
            )

            # Ensure correct sidebar tab + toolbox card is visible
            pane_locator = self._ensure_field_tab_visible(spec)
            if not pane_locator:
                return None

            card_selector = config.BUILDER_SELECTORS["fields_sidebar"]["card_by_data_type"].format(
                data_type=spec.sidebar_data_type
            )

            # -----------------------------
            # Drag attempts
            # -----------------------------
            # Inner loop: drag with stale-handling
            max_drag_attempts = 2
            drag_succeeded = False
            last_dropzone = None
            anchor_el = None
            pre_drop_dumped = False

            dom_changed_after_release = False
            dom_after_release_ids: list[str] | None = None

            for drag_attempt in range(1, max_drag_attempts + 1):
                try:
                    # Resolve + (optionally) enforce true visible location ONCE per drag attempt
                    drop_ctx = self._ctx(kind="drop", spec=spec, fi=fi_index, a=f"create={create_attempt}/drag={drag_attempt}")
                    if drop_location == "after_field":
                        if not insert_after_field_id:
                            self.session.emit_diag(
                                Cat.DROP,
                                "after_field requested but insert_after_field_id missing",
                                reason="missing_anchor",
                                **drop_ctx,
                            )
                            drop_location = "section_bottom"
                        else:
                            anchor_el = editor.get_field_by_id(str(insert_after_field_id))
                            if anchor_el is None:
                                self.session.emit_diag(
                                    Cat.DROP,
                                    "after_field anchor not found; falling back",
                                    reason="anchor_missing",
                                    anchor_id=insert_after_field_id,
                                    **drop_ctx,
                                )
                                drop_location = "section_bottom"

                    dz_id = self._compute_dropzone_dom_id(
                        drop_location=drop_location,
                        anchor_field_id=str(insert_after_field_id) if insert_after_field_id else None,
                    )
                    if not dz_id:
                        self.session.emit_diag(
                            Cat.DROP,
                            "Dropzone id could not be computed",
                            reason="dropzone_missing",
                            location=drop_location,
                            anchor=insert_after_field_id,
                            **drop_ctx,
                        )
                        continue

                    active_pane = wait.until(EC.visibility_of_element_located(pane_locator))
                    toolbox_item = None
                    if spec.key == "single_choice":
                        # Always scope to the active tab pane (Auto marked) to avoid false matches.
                        # CloudAssess SVG sprites can use xlink:href OR href, and paths may vary.
                        use_el = None

                        # 1) xlink:href contains match
                        sel = config.BUILDER_SELECTORS["single_choice"]
                        use_els = active_pane.find_elements(By.CSS_SELECTOR, sel["use_xlink"])
                        if use_els:
                            use_el = use_els[0]

                        # 2) href contains match (newer SVG)
                        if use_el is None:
                            use_els = active_pane.find_elements(By.CSS_SELECTOR, sel["use_href"])
                            if use_els:
                                use_el = use_els[0]

                        # 3) fallback: label text match inside the card
                        if use_el is None:
                            items = active_pane.find_elements(By.CSS_SELECTOR, sel["tool_item"])
                            for it in items:
                                try:
                                    label = (it.text or "").strip().lower()
                                    if "single choice" in label:
                                        toolbox_item = it
                                        break
                                except Exception:
                                    continue
                            else:
                                toolbox_item = None
                        else:
                            toolbox_item = use_el.find_element(
                                By.XPATH,
                                "./ancestor::div[contains(@class,'designer__fields-dragging__item')][1]"
                            )

                        if toolbox_item is None:
                            raise NoSuchElementException("Could not locate Single choice toolbox item in Auto marked tab pane.")
                    else:
                        toolbox_item = active_pane.find_element(By.CSS_SELECTOR, card_selector)

                    driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});",
                        toolbox_item,
                    )

                    drop_ctx = self._ctx(
                        kind="drop",
                        spec=spec,
                        fi=fi_index,
                        a=f"create={create_attempt}/drag={drag_attempt}",
                    )
                    try:
                        gesture = self._perform_drag_drop_gesture_by_id(
                            toolbox_item=toolbox_item,
                            dz_id=dz_id,
                            drop_location=drop_location,
                            key=key,
                            create_attempt=create_attempt,
                            drag_attempt=drag_attempt,
                            fi_index=fi_index,
                            dump_pre_drop_state=(not pre_drop_dumped),
                        )

                        pre_drop_dumped = True

                        if not gesture.ok:
                            self.session.counters.inc("drop.failures")
                            self.session.emit_diag(
                                Cat.DROP,
                                "Drop gesture failed",
                                reason=gesture.reason,
                                dz_id=gesture.dz_id,
                                **drop_ctx,
                            )
                            continue

                        deadline = time.time() + 2.0
                        dom_poll_start = time.monotonic()
                        dom_now_ids: list[str] | None = None
                        while time.time() < deadline:
                            dom_now_ids = self._get_active_section_field_ids() or []
                            if len(dom_now_ids) > len(dom_before_ids):
                                break
                            time.sleep(0.08)

                        dom_poll_elapsed = round(time.monotonic() - dom_poll_start, 2)
                        dom_after_release_ids = dom_now_ids or []
                        dom_changed_after_release = len(dom_after_release_ids) > len(dom_before_ids)

                        if dom_changed_after_release:
                            self.session.counters.inc("drop.dom_changed")
                            self.session.emit_diag(
                                Cat.DROP,
                                "DOM count increased after drop release",
                                section_id=self.sections.current_section_id or "",
                                before=len(dom_before_ids),
                                after=len(dom_after_release_ids),
                                dom_poll_s=dom_poll_elapsed,
                                **drop_ctx,
                            )
                        else:
                            self.session.counters.inc("drop.dom_no_change")
                            self.session.emit_diag(
                                Cat.DROP,
                                "DOM count unchanged after drop release",
                                section_id=self.sections.current_section_id or "",
                                before=len(dom_before_ids),
                                after=len(dom_after_release_ids),
                                dom_poll_s=dom_poll_elapsed,
                                **drop_ctx,
                            )

                        drag_succeeded = True
                        break
                    except StaleElementReferenceException as e:
                        self.session.counters.inc("drop.stale_elements")
                        self.session.emit_diag(
                            Cat.DROP,
                            "Stale element detected during drag",
                            exc=str(e),
                            **drop_ctx,
                        )
                    except WebDriverException as e:
                        self.session.counters.inc("drop.webdriver_errors")
                        self.session.emit_diag(
                            Cat.DROP,
                            "WebDriverException during drag",
                            exc=str(e),
                            **drop_ctx,
                        )
                    except Exception as e:
                        self.session.counters.inc("drop.unexpected_errors")
                        self.session.emit_diag(
                            Cat.DROP,
                            "Unexpected exception during drag",
                            exc=str(e),
                            **drop_ctx,
                        )
                        return None
                except Exception as e:
                    self.session.counters.inc("drop.pre_drag_errors")
                    self.session.emit_diag(
                        Cat.DROP,
                        "Pre-drag setup failed",
                        exc=str(e),
                        **self._ctx(kind="drop", spec=spec, fi=fi_index, a=f"create={create_attempt}/drag={drag_attempt}"),
                    )
                    return None

            if not drag_succeeded:
                # This create_attempt failed to drag; either try another create_attempt
                # (re-sync canvas) or give up entirely if we've exhausted them.
                create_ctx = self._ctx(
                    kind="drop",
                    spec=spec,
                    fi=fi_index,
                    a=f"create={create_attempt}",
                )
                if create_attempt == max_create_attempts:
                    self.session.counters.inc("drop.create_abort")
                    self.session.emit_diag(
                        Cat.DROP,
                        "Drag did not succeed after max create attempts",
                        max_attempts=max_create_attempts,
                        **create_ctx,
                    )
                    return None

                self.session.emit_diag(
                    Cat.DROP,
                    "Drag did not succeed; re-syncing canvas before retry",
                    **create_ctx,
                )
                try:
                    sections.wait_for_canvas_for_current_section()
                except Exception as e:
                    self.session.counters.inc("drop.canvas_resync_errors")
                    self.session.emit_diag(
                        Cat.DROP,
                        "Canvas resync failed after drag failure",
                        exc=str(e),
                        **create_ctx,
                    )
                continue  # go to next create_attempt

            # -----------------------------
            # Detect new field (id-diff + registry filter)
            # -----------------------------
            drop_summary_ctx = self._ctx(
                kind="drop",
                spec=spec,
                fi=fi_index,
                a=f"create={create_attempt}",
            )

            fast_confirmed = False
            if dom_changed_after_release and dom_after_release_ids is not None:
                dom_delta_candidates = [
                    fid for fid in dom_after_release_ids
                    if fid not in dom_before_set and fid not in registry_ids
                ]
                typed_candidates = [fid for fid in dom_delta_candidates if _wrapper_matches_type(fid)]
                candidates = typed_candidates or dom_delta_candidates

                if len(candidates) == 1:
                    candidate_id = candidates[0]
                    try:
                        new_field = _verify_field_by_id(candidate_id)
                    except Exception as e:
                        new_field = None
                        self.session.emit_diag(
                            Cat.PHANTOM,
                            "DOM-count fastpath candidate failed verification",
                            new_field_id=candidate_id,
                            exc=str(e),
                            **drop_summary_ctx,
                        )

                    if new_field is not None:
                        new_id = candidate_id
                        try:
                            index_in_section = dom_after_release_ids.index(candidate_id)
                        except ValueError:
                            index_in_section = -1
                        self.session.counters.inc("drop.confirm_fastpath")
                        self.session.emit_diag(
                            Cat.DROP,
                            "DOM-count fastpath accepted new field",
                            new_field_id=new_id,
                            index=index_in_section,
                            candidates=candidates[:8],
                            **drop_summary_ctx,
                        )
                        fast_confirmed = True

            # Wait for new field presence for this create attempt (Turbo-friendly: ID-based)
            def _snapshot_has_growth_or_candidate() -> bool:
                try:
                    _, ids_now = _snapshot_fields()
                    if len(ids_now) > len(before_ids):
                        return True
                    return bool((ids_now - before_ids) - registry_ids)
                except Exception:
                    return False

            try:
                if not fast_confirmed:
                    confirm_timeout = 6 if dom_changed_after_release else 10
                    confirm_wait = self.session.get_wait(confirm_timeout)
                    iddiff_wait_start = time.monotonic()
                    confirm_wait.until(lambda d: _snapshot_has_growth_or_candidate())
                    iddiff_wait_s = round(time.monotonic() - iddiff_wait_start, 2)
                else:
                    iddiff_wait_s = 0.0

            except TimeoutException:
                iddiff_wait_s = 0.0
                # Final re-check before declaring "not confirmed":
                # Turbo can hydrate late; a fresh snapshot can show the new id even if the wait timed out.
                try:
                    _, ids_now = _snapshot_fields()
                    late_candidates = list((ids_now - before_ids) - registry_ids)
                except Exception:
                    late_candidates = []

                phantom_ctx = self._ctx(
                    kind="phantom",
                    spec=spec,
                    fi=fi_index,
                    a=f"create={create_attempt}",
                )
                if late_candidates:
                    self.session.counters.inc("phantom.late_candidates")
                    self.session.emit_diag(
                        Cat.PHANTOM,
                        "Creation timeout but late candidates appeared; continuing with id-diff",
                        candidates=late_candidates[:8],
                        **phantom_ctx,
                    )
                else:
                    self.session.counters.inc("phantom.timeouts")
                    self.session.emit_signal(
                        Cat.PHANTOM,
                        "Field creation not confirmed within timeout; attempting DOM-delta recovery",
                        level="warning",
                        **phantom_ctx,
                    )

                    self._debug_dump_section_registry_vs_dom(
                        note=f"phantom-timeout BEFORE resync create_attempt={create_attempt}",
                        max_items=30,
                    )
                    self._debug_dump_section_order_alignment(
                        note=f"phantom-timeout BEFORE resync create_attempt={create_attempt}",
                        max_items=30,
                    )

                    # ----- DOM-delta candidates (preferred) -----
                    dom_now_ids = self._get_active_section_field_ids() or []

                    # --- Guard: empty-section "phantom" (drop claimed success but section still has no wrappers) ---
                    empty_placeholder = False
                    try:
                        empty_placeholder = bool(driver.execute_script("return !!document.querySelector('#drop-zone-0');"))
                    except Exception:
                        empty_placeholder = False

                    empty_section_phantom = (
                        before_count == 0
                        and len(dom_before_ids) == 0
                        and len(dom_now_ids) == 0
                        and empty_placeholder
                    )

                    if empty_section_phantom:
                        self.session.emit_signal(
                            Cat.PHANTOM,
                            "Empty-section phantom detected; retrying locally before hard resync",
                            level="warning",
                            **phantom_ctx,
                        )

                        # Best-effort: cancel any lingering drag state
                        try:
                            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                        except Exception:
                            pass

                        # Re-sync canvas and retry (treat as a failed create_attempt rather than hard resync)
                        try:
                            sections.wait_for_canvas_for_current_section(timeout=3)
                        except Exception:
                            pass

                        # If we still have create_attempts left, continue to next create_attempt.
                        if create_attempt < max_create_attempts:
                            continue

                        # Last attempt: allow a single hard resync as a final escape hatch
                        self.session.emit_signal(
                            Cat.PHANTOM,
                            "Empty-section phantom persisted on final create attempt; allowing hard resync",
                            level="warning",
                            **phantom_ctx,
                        )

                        did_resync = _hard_resync_once_or_bail(
                            reason=f"empty-section phantom persisted: {spec.display_name} section={sections.current_section_id}"
                        )

                        if did_resync:
                            # After resync, re-align canvas and retry the add locally (NO recursion)
                            try:
                                sections.wait_for_canvas_for_current_section(timeout=5)
                            except Exception:
                                pass

                            # Cancel any lingering drag state
                            try:
                                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                            except Exception:
                                pass

                            # Restart the whole create_attempt cycle (fresh DOM snapshot)
                            continue

                        # If we couldn't (or wouldn't) resync, fall through and handle as failure/skip later.
                        
                    if len(dom_now_ids) > len(dom_before_ids):
                        self.session.emit_diag(
                            Cat.PHANTOM,
                            "DOM count increased but typed selector not ready; using DOM-delta recovery",
                            before=len(dom_before_ids),
                            after=len(dom_now_ids),
                            **phantom_ctx,
                        )

                    # registry ids for the whole section (not type-filtered)
                    section_reg_ids = {
                        fh.field_id 
                        for fh in self.registry.fields_for_section(sections.current_section_id or "")
                        if fh.field_id
                    }

                    # candidates are ids that appeared since create_attempt started and aren’t in registry
                    dom_delta_candidates = [
                        fid
                        for fid in dom_now_ids
                        if fid not in dom_before_set and fid not in section_reg_ids
                    ]

                    typed_candidates = [fid for fid in dom_delta_candidates if _wrapper_matches_type(fid)]
                    candidates = typed_candidates or dom_delta_candidates

                    self.session.emit_diag(
                        Cat.PHANTOM,
                        "Phantom DOM/reg snapshot",
                        dom_before=len(dom_before_ids),
                        dom_now=len(dom_now_ids),
                        dom_delta=len(dom_delta_candidates),
                        typed_delta=len(typed_candidates),
                        reg_section=len(section_reg_ids),
                        **phantom_ctx,
                    )

                    if candidates:
                        chosen = candidates[0] if drop_location == "section_top" else candidates[-1]
                        verified = _verify_field_by_id(chosen)
                        if verified is not None:
                            new_field = verified
                            new_id = chosen
                            # compute index_in_section from dom_now_ids for reporting
                            try:
                                index_in_section = dom_now_ids.index(chosen)
                            except ValueError:
                                index_in_section = -1

                            self.session.emit_signal(
                                Cat.PHANTOM,
                                "Accepted new field by DOM-delta phantom recovery",
                                new_field_id=new_id,
                                index=index_in_section,
                                candidates=candidates[:8],
                                path="dom_delta",
                                **phantom_ctx,
                            )
                            self.session.counters.inc("phantom.dom_delta_recoveries")
                            break  # success: break out of create_attempt loop

                    # --- Last-chance: if count increased, try to accept the last element ---
                    try:
                        els_now = driver.find_elements(By.CSS_SELECTOR, canvas_sel)
                        if len(els_now) > before_count:
                            self.session.emit_diag(
                                Cat.PHANTOM,
                                "Count increased despite missing id-diff; trying last-element acceptance",
                                before=before_count,
                                after=len(els_now),
                                **phantom_ctx,
                            )

                            # Choose the last element and try to extract a strict id, allowing a brief stabilization window.
                            candidate_el = els_now[0] if drop_location == "section_top" else els_now[-1]

                            stabilize_until = time.time() + 2.0
                            candidate_id = ""

                            while time.time() < stabilize_until:
                                candidate_id = _field_id_strict(candidate_el)
                                if candidate_id:
                                    break
                                time.sleep(0.15)
                                # re-grab last element in case turbo swapped nodes
                                els_now = driver.find_elements(By.CSS_SELECTOR, canvas_sel)
                                if els_now:
                                    candidate_el = els_now[0] if drop_location == "section_top" else els_now[-1]

                            if candidate_id and candidate_id not in registry_ids and candidate_id not in before_ids:
                                try:
                                    verified = _verify_field_by_id(candidate_id)
                                except Exception:
                                    verified = None

                                if verified is not None:
                                    new_field = verified
                                    new_id = candidate_id

                                    # Compute index safely
                                    index_in_section = 0 if drop_location == "section_top" else len(els_now) - 1
                                    self.session.emit_signal(
                                        Cat.PHANTOM,
                                        "Accepted new field by last-element fallback",
                                        new_field_id=new_id,
                                        index=index_in_section,
                                        path="last_element",
                                        **phantom_ctx,
                                    )

                                    self.session.counters.inc("phantom.last_element_recoveries")
                                    # ✅ break out of create_attempt loop as success
                                    break

                            self.session.emit_diag(
                                Cat.PHANTOM,
                                "Last-element acceptance failed; proceeding with phantom recovery",
                                candidate_id=candidate_id,
                                **phantom_ctx,
                            )

                            # If we reached here, we couldn't accept a new field via DOM-delta or count-increase fallback.
                            self.session.emit_signal(
                                Cat.PHANTOM,
                                "No acceptable new field found after drag success; attempting hard resync",
                                max_attempts=max_create_attempts,
                                level="warning",
                                **phantom_ctx,
                            )

                            did_resync = _hard_resync_once_or_bail(
                                reason=f"phantom add unresolved: {spec.display_name} section={sections.current_section_id}"
                            )

                            if did_resync:
                                try:
                                    sections.wait_for_canvas_for_current_section(timeout=5)
                                except Exception:
                                    pass
                                try:
                                    ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                                except Exception:
                                    pass

                                # Retry next create_attempt locally (no recursion)
                                if create_attempt < max_create_attempts:
                                    continue

                            # If we can't resync (or are out of attempts), let the outer code treat it as failure/skip.

                    except Exception as e:
                        self.session.counters.inc("phantom.last_element_errors")
                        self.session.emit_diag(
                            Cat.PHANTOM,
                            "Error during last-element acceptance attempt",
                            exc=str(e),
                            **phantom_ctx,
                        )

                    # ✅ Instrument: capture dropzone state at phantom moment (before resync)
                    if self._instrument():
                        self._log_drop_diagnostics(
                            dropzone_el=last_dropzone,
                            drop_location=drop_location,
                            section_id=sections.current_section_id or "",
                            note=f"phantom-timeout create_attempt={create_attempt} type={key}",
                        )

                    # ✅ Hard resync immediately after first phantom timeout (best-effort)
                    if getattr(config, "PHANTOM_RESYNC_ON_FIRST", True) and not empty_section_phantom and not used_hard_resync:
                        did_resync = _hard_resync_once_or_bail(
                            reason=f"phantom add: {spec.display_name} in section {sections.current_section_id}"
                        )
                        if did_resync:
                            self._debug_dump_section_registry_vs_dom(note="AFTER hard_resync")
                            self._debug_dump_section_order_alignment(note="AFTER hard_resync")
                            # Re-align and retry locally instead of recursion
                            try:
                                sections.wait_for_canvas_for_current_section(timeout=3)
                            except Exception:
                                pass

                            # Treat this as a failed create_attempt and retry next create_attempt
                            continue
                        # If we didn't resync (budget exhausted or failed), skip/abort handled in helper
                        return None

                    # Otherwise: fall back to your existing soft retry behaviour
                    if create_attempt < max_create_attempts:
                        try:
                            sections.wait_for_canvas_for_current_section()
                        except Exception:
                            pass
                        continue

                    # Last chance: attempt hard resync if you kept old behaviour
                    did_resync = _hard_resync_once_or_bail(reason="phantom add at final attempt")
                    if did_resync:
                        self._debug_dump_section_registry_vs_dom(note="AFTER hard_resync at final attempt")
                        self._debug_dump_section_order_alignment(note="AFTER hard_resync at final attempt")
                        try:
                            sections.wait_for_canvas_for_current_section(timeout=5)
                        except Exception:
                            pass
                        try:
                            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                        except Exception:
                            pass
                        # retry locally (we are inside the create_attempt loop)
                        continue
                    return None

            if not fast_confirmed:
                after_fields, after_ids = _snapshot_fields()
                diff_ids = list(after_ids - before_ids)
                candidate_ids = [i for i in diff_ids if i and i not in registry_ids]

                self.session.emit_diag(
                    Cat.DROP,
                    "ID-diff after drop",
                    before_ids=len(before_ids),
                    after_ids=len(after_ids),
                    diff=diff_ids,
                    registry_known=len(registry_ids),
                    candidates=candidate_ids,
                    iddiff_wait_s=iddiff_wait_s,
                    **drop_summary_ctx,
                )

                # Choose an id
                new_id = ""

                if len(candidate_ids) == 1:
                    new_id = candidate_ids[0]
                elif len(candidate_ids) > 1:
                    # choose the last candidate in DOM order
                    for el in reversed(after_fields):
                        fid = _field_id_strict(el)
                        if fid in candidate_ids:
                            new_id = fid
                            break
                else:
                    # No usable candidate; treat as unstable snapshot and retry
                    self.session.emit_diag(
                        Cat.PHANTOM,
                        "No usable new id candidate; retrying create attempt",
                        diff=diff_ids,
                        **drop_summary_ctx,
                    )
                    continue

                # Verify existence by re-find
                try:
                    new_field = _verify_field_by_id(new_id)
                    self.session.emit_diag(
                        Cat.DROP,
                        "New field verified by id",
                        new_field_id=new_id,
                        section_id=self.sections.current_section_id or "",
                        **drop_summary_ctx,
                    )
                except Exception as e:
                    self.session.emit_diag(
                        Cat.PHANTOM,
                        "Detected new id but could not re-find field; retrying",
                        new_field_id=new_id,
                        exc=str(e),
                        **drop_summary_ctx,
                    )
                    new_field = None
                    new_id = ""
                    continue
                self.session.counters.inc("drop.confirm_fallback")

            # Hard guard: never accept an id already known in registry for this section/type
            if new_id in registry_ids:
                self.session.emit_diag(
                    Cat.REG,
                    "Selected id already in registry for section/type; retrying",
                    new_field_id=new_id,
                    **drop_summary_ctx,
                )
                new_field = None
                new_id = ""
                continue

            self._debug_dump_section_registry_vs_dom(
                note=f"post-add verified id={new_id} location={drop_location}"
            )
            self._debug_dump_section_order_alignment(
                note=f"post-add verified id={new_id} location={drop_location}"
            )

            # Compute index safely (optional; not critical)
            index_in_section = _dom_index_in_section(new_id)

            self.session.emit_signal(
                Cat.DROP,
                "New field detected and verified by id",
                new_field_id=new_id,
                index=index_in_section,
                **drop_summary_ctx,
            )

            # Enforce bottom placement if we asked for section_bottom
            dom_ids_now = self._get_active_section_field_ids()
            try_reorder = False

            def _expected_ok() -> tuple[bool, str]:
                if not dom_ids_now or new_id not in dom_ids_now:
                    return False, "new_id not in DOM ids"

                idx = dom_ids_now.index(new_id)

                if drop_location == "section_top":
                    return (idx == 0), f"expected idx=0 got idx={idx}"

                if drop_location == "section_bottom":
                    return (idx == len(dom_ids_now) - 1), f"expected idx=last({len(dom_ids_now)-1}) got idx={idx}"

                if drop_location == "after_field":
                    if not insert_after_field_id:
                        return False, "after_field requested but anchor missing"
                    if insert_after_field_id not in dom_ids_now:
                        return False, f"after_field anchor not in DOM ids (anchor={insert_after_field_id})"
                    anchor_idx = dom_ids_now.index(insert_after_field_id)
                    return (idx == anchor_idx + 1), f"expected idx={anchor_idx+1} got idx={idx} (anchor_idx={anchor_idx})"

                return True, "no invariant (unknown drop_location)"

            ok, why = _expected_ok()
            if not ok:
                try_reorder = True
                self.session.emit_diag(
                    Cat.DROP,
                    "Placement mismatch; attempting reorder",
                    new_field_id=new_id,
                    drop_location=drop_location,
                    reason=why,
                    **drop_summary_ctx,
                )

                moved = self._reposition_field(
                    field_id=new_id,
                    target=drop_location,              # "section_top" | "section_bottom" | "after_field"
                    anchor_field_id=insert_after_field_id,
                )
                if not moved:
                    self.sections.wait_for_canvas_for_current_section(timeout=2)
                    moved = self._reposition_field(
                        field_id=new_id,
                        target=drop_location,
                        anchor_field_id=insert_after_field_id,
                    )
            
            # Stabilize after reorder attempt (Turbo churn)
            self.sections.wait_for_canvas_for_current_section(timeout=3)

            if try_reorder:
                self._debug_dump_section_registry_vs_dom(
                    note=f"post-reorder attempt id={new_id} location={drop_location}"
                )                
                self._debug_dump_section_order_alignment(
                    note=f"post-reorder attempt id={new_id} location={drop_location}"
                ) 
            # ✅ Instrument: did it actually land where it was supposed to in the active section?
            if self._instrument():
                ok = self._log_field_placement(
                    new_field_id=new_id,
                    drop_location=drop_location,
                    section_id=sections.current_section_id or "",
                    anchor_field_id = insert_after_field_id or "",
                )

                # Optional: if not correct, snapshot dropzone state again
                ids_now = self._get_active_section_field_ids()
                if new_id in ids_now and not ok:
                    self._log_drop_diagnostics(
                        dropzone_el=last_dropzone,
                        drop_location=drop_location,
                        section_id=sections.current_section_id or "",
                        anchor_field_id = insert_after_field_id or "",
                        note=f"post-drop misplaced create_attempt={create_attempt} type={key}",
                    )

            break  # ✅ IMPORTANT: stop create_attempt loop once verified

        # -----------------------------
        # Finalize handle
        # -----------------------------
        if new_field is None or new_id == "":
            # If we somehow exit the loop without a field, treat as failure.
            self.session.emit_signal(
                Cat.DROP,
                "Failed to obtain new field handle after drag/drop attempts",
                level="error",
                **self._ctx(kind="drop", spec=spec, fi=fi_index, a="finalize"),
            )
            return None

        # 1) Get field id from the editor
        field_id = editor.try_get_field_id_strict(new_field) or new_id or ""
        if not field_id:
            self.session.emit_signal(
                Cat.REG,
                "Could not infer field id for newly created field",
                level="error",
                **self._ctx(kind="registry", spec=spec, fi=fi_index, a="finalize"),
            )
            handle = FieldHandle(
                field_id="",
                section_id=sections.current_section_id or "",
                field_type_key=key,
                fi_index=fi_index,
            )
            self.registry.add_field(handle)
            return handle

        # Identify current section id
        section_id = sections.current_section_id

        index_in_section = _dom_index_in_section(field_id or new_id)

        # title: read from the DOM via editor helper
        field_title = f"[UNASSIGNED]: {field_title}"

        handle = FieldHandle(
            field_id=field_id,
            section_id=section_id,
            field_type_key=key,        # the FIELD_TYPES key (e.g. "short_answer")
            index=index_in_section,
            title=field_title,
            fi_index=fi_index,
        )

        self.session.counters.inc("builder.fields_added")

        self.session.emit_signal(
            Cat.DROP,
            "Created field handle",
            fid=handle.field_id,
            sec=handle.section_id,
            type=key,
            fi=fi_index,
            index=handle.index,
        )
        self.registry.add_field(handle)
        return handle

    def _ensure_field_tab_visible(self, spec: FieldTypeSpec, timeout: int = 10):
        """
        Ensure the correct tab in the Fields sidebar is active for this field type.

        Returns:
            (by, value) locator tuple for the active tab pane, or None on failure.
        """
        wait = self.session.wait
        ctx = self._ctx(kind="fields_tab", spec=spec)

        # 1. Make sure the Fields sidebar is open (try add-new-field fastpath first)
        if not self._fields_sidebar_tab_visible():
            if not self._try_open_fields_sidebar_from_field_settings(
                timeout=timeout,
                ctx=ctx,
            ):
                if not self._ensure_sidebar_visible("fields", timeout=timeout):
                    self.session.emit_signal(
                        Cat.SIDEBAR,
                        "Fields sidebar could not be shown; cannot select tab",
                        level="error",
                        **ctx,
                    )
                    return None
        elif self._instrument():
            self.session.counters.inc("sidebar.fields.fastpath_hits")

        if not self._fields_sidebar_tab_visible():
            self.session.emit_signal(
                Cat.SIDEBAR,
                "Fields sidebar could not be shown; cannot select tab",
                level="error",
                **ctx,
            )
            return None

        # === CHANGED: wrap tab activation + pane detection in a retry loop ===
        for attempt in range(1, 4):
            # 2. Activate the appropriate tab and get its button element
            tab_btn = self._activate_fields_tab_for_spec(spec)
            if tab_btn is None:
                self.session.emit_signal(
                    Cat.SIDEBAR,
                    "Could not activate fields tab",
                    a=f"attempt={attempt}/3",
                    tab=spec.sidebar_tab_label,
                    level="warning",
                    **ctx,
                )
                if attempt == 3:
                    return None
                continue

            # 3. Figure out which pane we expect based on this button's data-bs-target
            try:
                raw_target = tab_btn.get_attribute("data-bs-target") or ""
            except StaleElementReferenceException:
                self.session.emit_diag(
                    Cat.SIDEBAR,
                    "Tab button went stale while reading data-bs-target",
                    a=f"attempt={attempt}/3",
                    tab=spec.sidebar_tab_label,
                    **ctx,
                )
                continue  # go to next attempt

            raw_target = raw_target.strip()

            # Debug log so we can see what CA is actually giving us
            self.session.emit_diag(
                Cat.SIDEBAR,
                "Tab data-bs-target value read",
                a=f"attempt={attempt}/3",
                tab=spec.sidebar_tab_label,
                target=raw_target,
                **ctx,
            )

            if raw_target and raw_target.startswith("#"):
                # Looks like a valid ID selector, e.g. "#b59c26bd3c-tab-pane"
                pane_id = raw_target[1:]
                pane_by = By.ID
                pane_value = pane_id
                self.session.emit_diag(
                    Cat.SIDEBAR,
                    "Using pane ID selector for tab",
                    a=f"attempt={attempt}/3",
                    tab=spec.sidebar_tab_label,
                    pane=pane_value,
                    **ctx,
                )
            else:
                if raw_target:
                    self.session.emit_diag(
                        Cat.SIDEBAR,
                        "Unexpected data-bs-target; falling back to generic active pane selector",
                        a=f"attempt={attempt}/3",
                        tab=spec.sidebar_tab_label,
                        target=raw_target,
                        **ctx,
                    )
                else:
                    self.session.emit_diag(
                        Cat.SIDEBAR,
                        "No data-bs-target on tab button; falling back to generic active pane selector",
                        a=f"attempt={attempt}/3",
                        tab=spec.sidebar_tab_label,
                        **ctx,
                    )
                pane_by = By.CSS_SELECTOR
                pane_value = config.BUILDER_SELECTORS["fields_sidebar"]["active_tab_pane"]

            pane_locator = (pane_by, pane_value)

            # 4. Wait until THIS tab is active and its pane is visible
            try:
                def tab_active(_):
                    if tab_btn is None:
                        self.session.emit_diag(
                            Cat.SIDEBAR,
                            "Tab button missing while checking active state",
                            a=f"attempt={attempt}/3",
                            tab=spec.sidebar_tab_label,
                            **ctx,
                        )
                        return False
                    try:
                        # 1) Check the tab button itself
                        cls = tab_btn.get_attribute("class") or ""
                        aria = tab_btn.get_attribute("aria-selected") or ""
                        tab_ok = ("active" in cls) and (aria.lower() == "true")

                        # 2) Check the associated pane (show + active)
                        try:
                            pane_el = self.session.driver.find_element(*pane_locator)
                            pane_classes = pane_el.get_attribute("class") or ""
                            pane_ok = ("show" in pane_classes) and ("active" in pane_classes)
                        except StaleElementReferenceException:
                            # pane is mid-re-render, treat as not ready yet
                            pane_ok = False
                        except Exception:
                            pane_ok = False

                        return tab_ok and pane_ok

                    except StaleElementReferenceException:
                        self.session.emit_diag(
                            Cat.SIDEBAR,
                            "Tab button stale while checking active state",
                            a=f"attempt={attempt}/3",
                            tab=spec.sidebar_tab_label,
                            **ctx,
                        )
                        return False
                    except Exception:
                        return False

                wait.until(tab_active)
                wait.until(EC.visibility_of_element_located(pane_locator))

                self.session.emit_diag(
                    Cat.SIDEBAR,
                    "Toolbox tab active",
                    a=f"attempt={attempt}/3",
                    tab=spec.sidebar_tab_label,
                    **ctx,
                )
                return pane_locator

            except TimeoutException:
                self.session.emit_signal(
                    Cat.SIDEBAR,
                    "Timed out waiting for tab and pane to become active",
                    a=f"attempt={attempt}/3",
                    tab=spec.sidebar_tab_label,
                    level="warning",
                    **ctx,
                )
                # try again if attempts remain
                continue

            except StaleElementReferenceException:
                self.session.emit_diag(
                    Cat.SIDEBAR,
                    "Tab button stale while waiting for active pane; retrying",
                    a=f"attempt={attempt}/3",
                    tab=spec.sidebar_tab_label,
                    **ctx,
                )
                continue

        # === END RETRY LOOP ===
        self.session.emit_signal(
            Cat.SIDEBAR,
            "Failed to ensure field tab visible after retries",
            tab=spec.sidebar_tab_label,
            level="error",
            **ctx,
        )
        return None

    def _perform_drag_drop_gesture_by_id(
        self,
        *,
        toolbox_item: WebElement,
        dz_id: str,
        drop_location: str,
        key: str,
        create_attempt: int,
        drag_attempt: int,
        fi_index: int | None = None,
        dump_pre_drop_state: bool = False,
    ) -> DropGestureResult:
        driver = self.driver
        self.session.counters.inc("drop.drag_attempts")
        ctx = self._ctx(kind="drop", spec=FIELD_TYPES.get(key), fi=fi_index, a=f"create={create_attempt}/drag={drag_attempt}")
        self.session.emit_diag(Cat.DROP, "Starting drag/drop gesture", **ctx)

        note = f"type={key} create_attempt={create_attempt} drag_attempt={drag_attempt} loc={drop_location}"

        # Start drag
        ActionChains(driver).move_to_element(toolbox_item).click_and_hold(toolbox_item).perform()

        if not self._wait_for_drag_mode(timeout=2.5):
            try:
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            except Exception:
                pass
            return DropGestureResult(False, "drag_mode_failed", dz_id)

        # Pre-drop diagnostics (once per create_attempt, while drag-mode is active).
        # Keep here so it captures transient drag-mode DOM (active section, dropzones, etc.).
        if dump_pre_drop_state:
            try:
                self._debug_dump_section_registry_vs_dom(
                    note=f"drag-mode active (pre-drop) create_attempt={create_attempt} drag_attempt={drag_attempt} type={key} loc={drop_location}"
                )
                self._debug_dump_section_order_alignment(
                    note=f"drag-mode active (pre-drop) create_attempt={create_attempt} drag_attempt={drag_attempt} type={key} loc={drop_location}"
                )
            except Exception:
                pass

        # tiny wake movement (helps dropzones render)
        try:
            ActionChains(driver).move_by_offset(1, 1).perform()
        except Exception:
            pass

        # Resolve dropzone by ID (re-find to avoid Turbo replacement)
        dropzone = self._find_dropzone_by_dom_id(dz_id, timeout=2.0)
        if dropzone is None:
            try:
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            except Exception:
                pass
            self.session.counters.inc("drop.dropzone_not_found")
            self.session.emit_diag(Cat.DROP, "Dropzone not found", note="dropzone_not_found", **ctx)
            return DropGestureResult(False, "dropzone_not_found", dz_id)

        # Diagnostics: pre-drop state
        if self._instrument():
            self._log_drop_diagnostics(
                dropzone_el=dropzone,
                drop_location=drop_location,
                section_id=self.sections.current_section_id or "",
                note=f"pre-drop {note}",
            )

        # Scroll only if needed
        try:
            r = self._rect_info(dropzone)
            top = float(r.get("top", 0))
            bottom = float(r.get("bottom", 0))
            vh = float(r.get("vh", 0))
            within = (top >= -5) and (bottom <= vh + 5)
            if not within:
                block = "end" if drop_location == "section_bottom" else "start"
                self._scroll_dropzone_to_visible(dropzone, block=block)
        except Exception:
            pass

        # Recompute rect (post-scroll)
        rect = driver.execute_script(
            "const r=arguments[0].getBoundingClientRect(); return {x:r.left,y:r.top,w:r.width,h:r.height};",
            dropzone,
        ) or {}
        w = float(rect.get("w", 0))
        h = float(rect.get("h", 0))
        top = float(rect.get("y", 0))
        vh = float(driver.execute_script("return window.innerHeight || document.documentElement.clientHeight;") or 0)

        if w < 10 or h < 10:
            try:
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            except Exception:
                pass
            self.session.counters.inc("drop.dropzone_rect_unusable")
            self.session.emit_diag(Cat.DROP, "Dropzone rect unusable", note="dropzone_rect_unusable", **ctx)
            return DropGestureResult(False, "dropzone_rect_unusable", dz_id)

        # Offsets: *center-relative* offsets.
        # Empirically, move_to_element(dropzone)+move_by_offset(dx,dy) is far more reliable
        # than move_to_element_with_offset under Turbo/Sortable reflows.
        safe = 6
        offsets: list[tuple[int, int]] = [
            (safe, safe),
            (0, 0),
            (-safe, safe),
            (safe, -safe),
            (-safe, -safe),
        ]

        huge = (vh > 0 and h > (vh - 10))

        # Huge-zone: prefer a viewport-anchored y (near bottom of viewport),
        # expressed as a center-relative offset.
        if huge and vh > 0:
            target_vy = int(vh - 25)
            center_vy = int(top + (h / 2))
            dy = int(target_vy - center_vy)
            lim = max(0, int((h / 2) - safe))
            dy = max(-lim, min(lim, dy))
            offsets = [(safe, dy)] + offsets  # huge-target first, then safe fallbacks

        # Try offsets
        for (ox, oy) in offsets:
            try:
                self.session.counters.inc("drop.offset_attempts")
                if not self._wait_for_drag_mode(timeout=0.2):
                    self.session.counters.inc("drop.drag_mode_collapsed")
                    self.session.emit_diag(Cat.DROP, "Drag mode collapsed", note="drag_mode_collapsed", **ctx)
                    return DropGestureResult(False, "drag_mode_collapsed", dz_id)

                # Re-find dropzone by id each attempt (Turbo swaps nodes)
                try:
                    dropzone = driver.find_element(By.ID, dz_id)
                except Exception:
                    dropzone = None
                if dropzone is None:
                    self.session.counters.inc("drop.dropzone_lost")
                    self.session.emit_diag(Cat.DROP, "Dropzone lost during offset retry", note="dropzone_lost", **ctx)
                    return DropGestureResult(False, "dropzone_lost", dz_id)

                active_ok = driver.execute_script(
                    "return arguments[0].classList.contains('draggable-dropzone--active');",
                    dropzone
                )
                if not active_ok:
                    self.session.emit_diag(
                        Cat.DROP,
                        "Dropzone not active during offset retry",
                        note=note,
                        offset=f"{ox},{oy}",
                        **ctx,
                    )
                    continue

                # Winner motion: element center + relative offset
                actions = ActionChains(driver)
                actions.move_to_element(dropzone)
                actions.move_by_offset(int(ox), int(oy))
                actions.release()
                actions.perform()

                self.session.counters.inc("drop.successes")
                self.session.counters.inc("drop.offset_success")
                self.session.emit_diag(
                    Cat.DROP,
                    "Drag/drop gesture released",
                    note="released",
                    offset=f"{ox},{oy}",
                    **ctx,
                )
                return DropGestureResult(
                    True,
                    "released",
                    dz_id,
                    offset_used=(ox, oy),
                    used_js_fallback=False,
                )

            except WebDriverException as e:
                # Huge fallback: JS mouse events if ActionChains refuses
                if huge and "out of bounds" in str(e).lower():
                    try:
                        px = int(float(rect.get("x", 0)) + (w / 2) + ox)
                        py = int(float(rect.get("y", 0)) + (h / 2) + oy)
                        driver.execute_script(
                            """
                            const x = arguments[0], y = arguments[1];
                            const target = document.elementFromPoint(x, y) || document.body;
                            const ev = (type) => new MouseEvent(type, {
                            bubbles: true, cancelable: true, view: window,
                            clientX: x, clientY: y, button: 0
                            });
                            target.dispatchEvent(ev('mousemove'));
                            target.dispatchEvent(ev('mouseup'));
                            """,
                            px, py
                        )
                        self.session.counters.inc("drop.js_fallbacks")
                        self.session.counters.inc("drop.offset_success")
                        self.session.emit_diag(
                            Cat.DROP,
                            "Drop gesture JS fallback success",
                            note="ok_js_fallback",
                            offset=f"{ox},{oy}",
                            **ctx,
                        )
                        return DropGestureResult(True, "ok_js_fallback", dz_id, offset_used=(ox, oy), used_js_fallback=True)
                    except Exception:
                        pass

                self.session.emit_diag(
                    Cat.DROP,
                    "Drop offset attempt exception",
                    note=note,
                    offset=f"{ox},{oy}",
                    exception=str(e),
                    **ctx,
                )
                continue

        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        except Exception:
            pass

        self.session.counters.inc("drop.all_offsets_failed")
        self.session.emit_diag(Cat.DROP, "All drop offsets exhausted", note="all_offsets_failed", **ctx)
        return DropGestureResult(False, "all_offsets_failed", dz_id)
     
    def _scroll_dropzone_to_visible(self, dropzone, *, max_attempts: int = 3, block: str = "end") -> bool:
        """
        Deterministically scroll a dropzone into a usable viewport position.

        Key behaviour:
        - For normal-sized zones: honour block ("start"/"end"/"center") and aim for within=True.
        - For HUGE zones (height ~ viewport): "within" is impossible; instead, force TOP alignment
        (top >= -5) to avoid negative-top geometry that causes MoveTargetOutOfBounds.
        - Uses container scroll if a scroll container exists; otherwise window scroll.
        """
        driver = self.driver
        ctx_scroll = self._ctx(
            kind="drop",
            sec=self.sections.current_section_id or "",
            a="scroll_dropzone",
        )

        def _scroll_by(delta: float, *, container=None) -> None:
            try:
                if container is not None:
                    driver.execute_script("arguments[0].scrollTop += arguments[1];", container, float(delta))
                else:
                    driver.execute_script("window.scrollBy(0, arguments[0]);", float(delta))
            except Exception:
                pass

        for attempt in range(1, max_attempts + 1):
            self.session.counters.inc("drop.scroll_attempts")
            # 1) Best-effort scrollIntoView first (cheap + helps Turbo lazily render)
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: arguments[1], inline: 'nearest'});",
                    dropzone,
                    block,
                )
            except StaleElementReferenceException:
                self.session.emit_diag(
                    Cat.DROP,
                    "Dropzone went stale during scrollIntoView",
                    block=block,
                    **ctx_scroll,
                )
                return False
            except Exception as e:
                self.session.emit_diag(
                    Cat.DROP,
                    "scrollIntoView failed for dropzone",
                    exception=str(e),
                    block=block,
                    **ctx_scroll,
                )
                return False

            # 2) Measure
            try:
                r = self._rect_info(dropzone)
            except Exception as e:
                self.session.emit_diag(
                    Cat.DROP,
                    "Could not read dropzone bounding rect",
                    exception=str(e),
                    **ctx_scroll,
                )
                return False

            top = float(r.get("top", 0))
            bottom = float(r.get("bottom", 0))
            height = float(r.get("height", 0))
            vh = float(r.get("vh", 0))

            within = (top >= -5) and (bottom <= vh + 5)
            huge = height > (vh * 0.85)
            intersects = (bottom >= 10) and (top <= vh - 10)

            if huge:
                self.session.counters.inc("drop.scroll_huge")

            self.session.emit_diag(
                Cat.DROP,
                "Dropzone rect metrics",
                attempt=attempt,
                top=top,
                bottom=bottom,
                height=height,
                viewport_height=vh,
                within=within,
                huge=huge,
                intersects=intersects,
                block=block,
                key="DROP.dropzone.rect",
                every_s=1.0,
                **ctx_scroll,
            )

            # ✅ Normal success: fully within and not huge
            if within and not huge:
                return True

            # ✅ Huge success condition:
            # We can’t make it fully "within", but we *can* make it usable by ensuring TOP is visible (non-negative).
            if huge and (top >= -5) and intersects:
                self.session.emit_diag(
                    Cat.DROP,
                    "Dropzone huge but top visible; treating as usable",
                    **ctx_scroll,
                )
                return True

            if attempt == 1:
                self.session.emit_diag(
                    Cat.DROP,
                    "Dropzone not confidently visible; correcting scroll",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    block=block,
                    **ctx_scroll,
                )

            # 3) Nudge deterministically
            try:
                container = self._find_scroll_container_for(dropzone)
            except Exception:
                container = None

            pad = 40

            if huge:
                # 🔥 Critical: for huge zones, align TOP into view.
                # Using block="end" often makes top go negative, which later triggers out-of-bounds moves.
                # We force a top-alignment nudge regardless of requested block.
                if container is not None:
                    try:
                        driver.execute_script(
                            """
                            const c = arguments[0];
                            const el = arguments[1];
                            const pad = arguments[2];
                            const cr = c.getBoundingClientRect();
                            const r = el.getBoundingClientRect();
                            c.scrollTop += (r.top - cr.top) - pad;
                            """,
                            container, dropzone, pad
                        )
                    except Exception:
                        pass
                else:
                    # Window scroll: bring element top to ~pad
                    _scroll_by(top - pad, container=None)

            else:
                # Non-huge: honour requested block intent
                if block == "start":
                    # Bring top near pad
                    if container is not None:
                        try:
                            driver.execute_script(
                                """
                                const c = arguments[0];
                                const el = arguments[1];
                                const pad = arguments[2];
                                const cr = c.getBoundingClientRect();
                                const r = el.getBoundingClientRect();
                                c.scrollTop += (r.top - cr.top) - pad;
                                """,
                                container, dropzone, pad
                            )
                        except Exception:
                            pass
                    else:
                        _scroll_by(top - pad, container=None)

                else:
                    # Default for end/center: bring bottom near viewport bottom - pad
                    if container is not None:
                        try:
                            driver.execute_script(
                                """
                                const c = arguments[0];
                                const el = arguments[1];
                                const pad = arguments[2];
                                const cr = c.getBoundingClientRect();
                                const r = el.getBoundingClientRect();
                                c.scrollTop += (r.bottom - cr.bottom) + pad;
                                """,
                                container, dropzone, pad
                            )
                        except Exception:
                            pass
                    else:
                        # window: bottom to (vh - pad)
                        _scroll_by(bottom - vh + pad, container=None)

            # micro-settle helps Turbo/scroll reflow
            try:
                time.sleep(0.08)
            except Exception:
                pass

            if not huge:
                self.session.emit_diag(
                    Cat.DROP,
                    "Dropzone still not verified as visible after scrolling",
                    block=block,
                    **ctx_scroll,
                )
            else:
                self.session.emit_diag(
                    Cat.DROP,
                    "Huge dropzone not fully within viewport (expected)",
                    block=block,
                    **ctx_scroll,
                )
            
        return False

    def _probe_dropzone(self, dropzone_el: WebElement, *, note: str = "") -> Optional[DropzoneProbe]:
        """
        Lightweight JS probe that describes whether the dropzone is actually droppable
        (topmost at center) and what is covering it if not.
        """
        driver = self.driver

        try:
            data = driver.execute_script(
                """
                const dz = arguments[0];
                if (!dz) return null;

                const r = dz.getBoundingClientRect();
                const cx = r.left + r.width/2;
                const cy = r.top + r.height/2;
                const top = document.elementFromPoint(cx, cy);

                function summarize(el) {
                  if (!el) return "null";
                  const id = el.id ? `#${el.id}` : "";
                  const cls = el.className ? `.${String(el.className).trim().split(/\\s+/).slice(0,4).join('.')}` : "";
                  const tag = el.tagName ? el.tagName.toLowerCase() : "unknown";
                  return `${tag}${id}${cls}`;
                }

                const ok = !!top && ((top === dz) || dz.contains(top));

                // Best-effort Turbo "busy" hint (not authoritative)
                const turboProgress = document.querySelector('[data-turbo-progress-bar]'); // sometimes present
                const hasAriaBusy = !!document.querySelector('[aria-busy="true"]');
                const hasTurboFrameBusy = !!document.querySelector('turbo-frame[busy]');

                const turboHint = [
                  turboProgress ? "progressbar" : "",
                  hasAriaBusy ? "aria-busy" : "",
                  hasTurboFrameBusy ? "frame-busy" : "",
                ].filter(Boolean).join(",");

                return {
                  rect: {left:r.left, top:r.top, right:r.right, bottom:r.bottom, width:r.width, height:r.height},
                  ok: ok,
                  topmost: summarize(top),
                  turboHint: turboHint || "none",
                };
                """,
                dropzone_el,
            )

            if not data:
                return None

            return DropzoneProbe(
                rect=data["rect"],
                center_topmost_ok=bool(data["ok"]),
                topmost_summary=str(data["topmost"]),
                turbo_busy_hint=str(data["turboHint"]),
                note=note or "",
            )

        except (StaleElementReferenceException, WebDriverException):
            return None

    def _get_active_section_field_ids(self) -> list[str]:
        driver = self.session.driver
        try:
            data = driver.execute_script("""
                const root = document.querySelector('#section-fields');
                const emptyDz = document.querySelector('#drop-zone-0');
                if (!root) {
                return { ids: [], reason: 'no_root', emptyDz: !!emptyDz };
                }
                const nodes = root.querySelectorAll('.section-field[id^="section-field-"]');
                const ids = Array.from(nodes).map(n => n.id.replace('section-field-','')).filter(Boolean);
                return { ids, reason: (ids.length ? 'ok' : 'no_nodes'), emptyDz: !!emptyDz };
            """)
            ids = [str(x) for x in (data.get("ids") or [])]
            if self._instrument() and not ids:
                self.session.emit_diag(
                    Cat.DROP,
                    "Active section field ids empty",
                    reason=data.get("reason"),
                    empty_dropzone=data.get("emptyDz"),
                    **self._ctx(
                        kind="drop",
                        sec=self.sections.current_section_id or "",
                        a="section_id_snapshot",
                    ),
                )
            return ids
        except Exception:
            return []

    def _log_field_placement(
        self,
        *,
        new_field_id: str,
        drop_location: str,
        section_id: str = "",
        anchor_field_id: str = "",
        tail_n: int = 8,
    ) -> bool:
        """
        Log where the new field landed within the active section and how that
        compares to the requested placement intent.

        This is instrumentation only; no control flow depends on this.
        """
        ctx = self._ctx(kind="drop", sec=section_id, fid=new_field_id, a="placement_check")

        ids = self._get_active_section_field_ids()
        if not ids:
            self.session.emit_diag(
                Cat.DROP,
                "Placement check: no section-field ids found",
                new_field_id=new_field_id,
                drop_location=drop_location,
                section_id=section_id,
                anchor_field_id=anchor_field_id,
                **ctx,
            )
            return False

        total = len(ids)
        tail = ids[-tail_n:] if total > tail_n else ids

        try:
            idx = ids.index(new_field_id)
        except ValueError:
            self.session.emit_diag(
                Cat.DROP,
                "Placement check: new_field_id not in DOM list",
                new_field_id=new_field_id,
                total=total,
                drop_location=drop_location,
                section_id=section_id,
                anchor_field_id=anchor_field_id,
                tail=tail,
                **ctx,
            )
            return False

        last_idx = total - 1

        # Determine expected index (if determinable)
        expected_idx: int | None = None
        expectation = ""

        if drop_location == "section_top":
            expected_idx = 0
            expectation = "top (index 0)"
        elif drop_location == "section_bottom":
            expected_idx = last_idx
            expectation = "bottom (last index)"
        elif drop_location == "after_field":
            if not anchor_field_id:
                expectation = "after_field requested but anchor missing"
            else:
                try:
                    anchor_idx = ids.index(anchor_field_id)
                    expected_idx = anchor_idx + 1
                    expectation = f"after anchor (anchor_index={anchor_idx})"
                except ValueError:
                    expectation = "after_field requested but anchor not found"
        else:
            expectation = f"unknown drop_location={drop_location!r}"

        # Log outcome
        if expected_idx is None:
            self.session.emit_diag(
                Cat.DROP,
                "Placement unverified",
                expectation=expectation,
                new_field_id=new_field_id,
                index=idx,
                total=total,
                drop_location=drop_location,
                section_id=section_id,
                anchor_field_id=anchor_field_id,
                tail=tail,
                key="DROP.placement.unverified",
                every_s=1.0,
                **ctx,
            )
            return False

        if idx == expected_idx:
            self.session.emit_diag(
                Cat.DROP,
                "Placement OK",
                expectation=expectation,
                new_field_id=new_field_id,
                index=idx,
                total=total,
                drop_location=drop_location,
                section_id=section_id,
                anchor_field_id=anchor_field_id,
                tail=tail,
                key="DROP.placement.ok",
                every_s=1.0,
                **ctx,
            )
            return True
        else:
            self.session.emit_diag(
                Cat.DROP,
                "Placement mismatch after reorder",
                expectation=expectation,
                new_field_id=new_field_id,
                index=idx,
                expected_index=expected_idx,
                total=total,
                drop_location=drop_location,
                section_id=section_id,
                anchor_field_id=anchor_field_id,
                tail=tail,
                **ctx,
            )
        return False

    def _log_drop_diagnostics(
        self,
        *,
        dropzone_el: WebElement | None,
        drop_location: str,
        section_id: str = "",
        anchor_field_id: str = "",
        note: str = "",
    ) -> None:
        """
        Logs whether the chosen dropzone is covered and what is on top of it.
        Call this immediately before drag and/or immediately after a misplaced insertion.
        """
        ctx = self._ctx(kind="drop", sec=section_id, a="drop_diagnostics")

        if dropzone_el is None:
            self.session.emit_diag(
                Cat.DROP,
                "Drop diagnostics: dropzone missing",
                drop_location=drop_location,
                section_id=section_id,
                anchor_field_id=anchor_field_id,
                note=note,
                **ctx,
            )
            return

        probe = self._probe_dropzone(dropzone_el)
        if probe is None:
            self.session.emit_diag(
                Cat.DROP,
                "Drop diagnostics: probe failed",
                drop_location=drop_location,
                section_id=section_id,
                anchor_field_id=anchor_field_id,
                note=note,
                **ctx,
            )
            return

        self.session.emit_diag(
            Cat.DROP,
            "Drop diagnostics: coverage snapshot",
            drop_location=drop_location,
            section_id=section_id,
            anchor_field_id=anchor_field_id,
            covered=not probe.center_topmost_ok,
            topmost_summary=probe.topmost_summary,
            turbo_hint=probe.turbo_busy_hint,
            rect=probe.rect,
            note=note,
            **ctx,
        )

    def _reposition_field(
        self,
        *,
        field_id: str,
        target: str,                 # "section_top" | "section_bottom" | "after_field"
        anchor_field_id: str | None = None,
        max_attempts: int = 2,
    ) -> bool:
        """
        Reposition a field using Sortable (NOT dropzones).

        Sortable mode signals:
        - #section-fields has class 'sortable--dragging'
        - dragged wrapper gains 'sortable-ghost' / 'sortable-chosen'
        """
        driver = self.driver
        sections = self.sections
        ctx_base = self._ctx(
            kind="drop",
            sec=sections.current_section_id or "",
            a="sortable_reorder",
        )

        def _js_drag(handle: WebElement, target_el: WebElement, dx: int, dy: int) -> None:
            driver.execute_script(
                """
                const handle = arguments[0];
                const target = arguments[1];
                const dx = arguments[2];
                const dy = arguments[3];

                const hr = handle.getBoundingClientRect();
                const tr = target.getBoundingClientRect();

                const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

                const vw = window.innerWidth || document.documentElement.clientWidth;
                const vh = window.innerHeight || document.documentElement.clientHeight;

                const sx0 = Math.floor(hr.left + hr.width/2);
                const sy0 = Math.floor(hr.top + hr.height/2);

                let tx0 = Math.floor(tr.left + tr.width/2 + dx);
                let ty0 = Math.floor(tr.top + tr.height/2 + dy);

                tx0 = clamp(tx0, 5, vw - 5);
                ty0 = clamp(ty0, 5, vh - 5);

                const ev = (type, x, y) => new MouseEvent(type, {
                bubbles:true, cancelable:true, view:window,
                clientX:x, clientY:y, button:0
                });

                handle.dispatchEvent(ev('mousemove', sx0, sy0));
                handle.dispatchEvent(ev('mousedown', sx0, sy0));
                document.dispatchEvent(ev('mousemove', sx0, sy0 + 12)); // wake move (bigger threshold)
                document.dispatchEvent(ev('mousemove', tx0, ty0));
                document.dispatchEvent(ev('mouseup', tx0, ty0));
                """,
                handle, target_el, int(dx), int(dy)
            )

        def _get_wrapper(fid: str) -> WebElement | None:
            try:
                return driver.find_element(By.CSS_SELECTOR, f"#section-field-{fid}")
            except Exception:
                return None

        def _is_sized(el: WebElement, min_px: float = 5.0) -> bool:
            try:
                r = self._rect_info(el)
                return float(r.get("width", 0) or 0) >= min_px and float(r.get("height", 0) or 0) >= min_px
            except Exception:
                return False

        def _pick_handle(wrapper: WebElement) -> WebElement | None:
            # Make the wrapper "active" (some handles only size/appear when active)
            try:
                wrapper.click()
            except Exception:
                try:
                    driver.execute_script("arguments[0].click();", wrapper)
                except Exception:
                    pass

            try:
                ActionChains(driver).move_to_element(wrapper).pause(0.05).perform()
            except Exception:
                pass

            sel = "button.field-drag-holder.btn--grab-handle"
            try:
                el = wrapper.find_element(By.CSS_SELECTOR, sel)
                if _is_sized(el):
                    return el
            except Exception:
                pass

            selectors = [
                "button.field-drag-holder",
                "button.btn--grab-handle",
                ".designer__field__dragger",
                ".field__dragger",
                ".field__drag-handle",
                ".designer__field__drag-handle",
                "[data-testid*='drag']",
                "[aria-label*='Drag']",
                "[aria-label*='Move']",
            ]
            deadline = time.time() + 0.8
            while time.time() < deadline:
                for s in selectors:
                    try:
                        el = wrapper.find_element(By.CSS_SELECTOR, s)
                        if _is_sized(el):
                            return el
                    except Exception:
                        continue
                time.sleep(0.06)

            return None

        def _sortable_started() -> bool:
            return bool(driver.execute_script(
                """
                if (document.querySelector('.sortable-ghost, .sortable-chosen')) return true;
                if (document.querySelector('#section-fields.sortable--dragging')) return true;
                if (document.querySelector('[class*="sortable--dragging"]')) return true;
                return false;
                """
            ))

        def _wait_sortable_started(timeout: float = 1.2) -> bool:
            end = time.time() + timeout
            while time.time() < end:
                try:
                    if _sortable_started():
                        return True
                except Exception:
                    pass
                time.sleep(0.05)
            return False

        def _confirm() -> bool:
            ids_now = self._get_active_section_field_ids() or []
            if not ids_now:
                return False

            def _ok(ids: list[str]) -> bool:
                if target == "section_top":
                    return ids[0] == str(field_id)
                if target == "section_bottom":
                    return ids[-1] == str(field_id)
                if target == "after_field" and anchor_field_id and anchor_field_id in ids and field_id in ids:
                    return ids.index(field_id) == ids.index(anchor_field_id) + 1
                return False

            if _ok(ids_now):
                return True

            # Give Turbo/Sortable a beat to settle and check again
            time.sleep(0.12)
            ids2 = self._get_active_section_field_ids() or []
            return bool(ids2) and _ok(ids2)

        for attempt in range(1, max_attempts + 1):
            ctx_attempt = {
                **ctx_base,
                "attempt": attempt,
                "target": target,
                "field_id": field_id,
                "anchor_field_id": anchor_field_id,
            }
            ids_now = self._get_active_section_field_ids() or []
            if not ids_now:
                self.session.emit_diag(
                    Cat.DROP,
                    "Sortable reorder aborted: no DOM field ids available",
                    max_attempts=max_attempts,
                    **ctx_attempt,
                )
                return False

            if field_id not in ids_now:
                self.session.emit_diag(
                    Cat.DROP,
                    "Sortable reorder aborted: field_id missing from DOM order",
                    max_attempts=max_attempts,
                    **ctx_attempt,
                )
                return False

            # Determine target wrapper + drop offset intent
            if target == "section_top":
                target_id = ids_now[0]
                drop_bias = "before"
            elif target == "section_bottom":
                target_id = ids_now[-1]
                drop_bias = "after"
            elif target == "after_field":
                if not anchor_field_id or anchor_field_id not in ids_now:
                    self.session.emit_diag(
                        Cat.DROP,
                        "Sortable reorder aborted: invalid anchor for after_field",
                        max_attempts=max_attempts,
                        **ctx_attempt,
                    )
                    return False
                else:
                    target_id = anchor_field_id
                    drop_bias = "after"
            else:
                self.session.emit_diag(
                    Cat.DROP,
                    "Sortable reorder aborted: unknown target",
                    **ctx_attempt,
                )
                return False
            
            if target_id == field_id:
                self.session.emit_diag(
                    Cat.DROP,
                    "Sortable reorder: target equals field_id; skipping",
                    **ctx_attempt,
                )
                return True

            ctx_zone = {**ctx_attempt, "target_id": target_id, "drop_bias": drop_bias}

            wrapper = _get_wrapper(field_id)
            target_wrapper = _get_wrapper(target_id)
            if wrapper is None or target_wrapper is None:
                self.session.emit_diag(
                    Cat.DROP,
                    "Sortable reorder aborted: wrapper missing",
                    target_id=target_id,
                    **ctx_attempt,
                )
                return False

            # Scroll both into view
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", wrapper)
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target_wrapper)
            except Exception:
                pass

            handle = _pick_handle(wrapper)
            if handle is None or not _is_sized(handle):
                self.session.emit_diag(
                    Cat.DROP,
                    "Sortable reorder: drag handle unavailable",
                    max_attempts=max_attempts,
                    **ctx_attempt,
                )
                continue

            # Compute offsets on the target wrapper (before/after)
            tr = self._rect_info(target_wrapper)
            w = float(tr.get("width", 0) or 0)
            h = float(tr.get("height", 0) or 0)
            if w < 10 or h < 10:
                self.session.emit_diag(
                    Cat.DROP,
                    "Sortable reorder: target wrapper rect unusable",
                    target_id=target_id,
                    rect=tr,
                    **ctx_attempt,
                )
                return False

            safe = 8
            dx = 0
            band = int(max(safe, min((h / 2) - safe, h * 0.30)))
            dy = -band if drop_bias == "before" else band

            self.session.emit_diag(
                Cat.DROP,
                "Sortable reorder attempt",
                max_attempts=max_attempts,
                target_id=target_id,
                drop_bias=drop_bias,
                dx=dx,
                dy=dy,
                key="DROP.sortable.attempt",
                every_s=1.0,
                **ctx_attempt,
            )

            # Perform drag using handle -> target wrapper offsets
            try:

                vw = driver.execute_script("return window.innerWidth")
                vh = driver.execute_script("return window.innerHeight")
                self.session.emit_diag(
                    Cat.DROP,
                    "Sortable reorder: target rect + viewport",
                    rect=tr,
                    viewport=(vw, vh),
                    key="DROP.sortable.rect",
                    every_s=1.0,
                    **ctx_zone,
                )

                # Step A: start drag (native)
                try:
                    ActionChains(driver)\
                        .move_to_element(handle)\
                        .click_and_hold(handle)\
                        .move_by_offset(0, 12)\
                        .perform()
                    self.session.emit_diag(
                        Cat.DROP,
                        "Sortable reorder: native drag start OK",
                        max_attempts=max_attempts,
                        key="DROP.sortable.native_start",
                        every_s=1.0,
                        **ctx_zone,
                    )
                except Exception as e_start:
                    self.session.emit_diag(
                        Cat.DROP,
                        "Sortable reorder: native drag start failed",
                        exception=str(e_start),
                        **ctx_zone,
                    )
                    try:
                        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                    except Exception:
                        pass
                    self.session.emit_diag(
                        Cat.DROP,
                        "Sortable reorder: falling back to JS drag (start failed)",
                        **ctx_zone,
                    )
                    _js_drag(handle, target_wrapper, dx, dy)
                    # regardless, clear residue and go to confirm
                    self._clear_sortable_residue(note="start-failed->js")
                    # proceed to confirm below (do not continue)

                # Step B: if native drag started, attempt native drop, else JS already ran
                if _wait_sortable_started(timeout=1.0):
                    self.session.emit_diag(
                        Cat.DROP,
                        "Sortable reorder: native drag stage entered",
                        **ctx_zone,
                    )
                    try:
                        ActionChains(driver)\
                            .move_to_element(target_wrapper)\
                            .move_by_offset(int(dx), int(dy))\
                            .pause(0.08)\
                            .release()\
                            .perform()
                        self.session.emit_diag(
                            Cat.DROP,
                            "Sortable reorder: native drop succeeded",
                            key="DROP.sortable.native_drop",
                            every_s=1.0,
                            **ctx_zone,
                        )
                    
                    except MoveTargetOutOfBoundsException as e:
                        self.session.emit_diag(
                            Cat.DROP,
                            "Sortable reorder: native drop out of bounds",
                            exception=str(e),
                            dx=dx,
                            dy=dy,
                            **ctx_zone,
                        )
                        try:
                            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                        except Exception:
                            pass
                        self.session.emit_diag(
                            Cat.DROP,
                            "Sortable reorder: falling back to JS drag (native drop OOB)",
                            **ctx_zone,
                        )
                        _js_drag(handle, target_wrapper, dx, dy)

                    except Exception as e_drop:
                        self.session.emit_diag(
                            Cat.DROP,
                            "Sortable reorder: native drop failed",
                            exception=str(e_drop),
                            dx=dx,
                            dy=dy,
                            **ctx_zone,
                        )
                        try:
                            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                        except Exception:
                            pass
                        self.session.emit_diag(
                            Cat.DROP,
                            "Sortable reorder: falling back to JS drag (drop failed)",
                            **ctx_zone,
                        )
                        _js_drag(handle, target_wrapper, dx, dy)
                else:
                    try:
                        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                    except Exception:
                        pass
                    self.session.emit_diag(
                        Cat.DROP,
                        "Sortable reorder: falling back to JS drag (did not start)",
                        **ctx_zone,
                    )
                    _js_drag(handle, target_wrapper, dx, dy)

                # Always clear residue after any drag attempt (success or failure)
                self._clear_sortable_residue(note="after-drag")

            except Exception as e:
                self.session.emit_diag(
                    Cat.DROP,
                    "Sortable reorder: drag attempt failed unexpectedly",
                    max_attempts=max_attempts,
                    exception=str(e),
                    **ctx_zone,
                )
                try:
                    ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                except Exception:
                    pass
                self._clear_sortable_residue(note="unexpected-exc")
                continue

            try:
                WebDriverWait(driver, 2.0).until(lambda d: _confirm())
                self.session.emit_diag(
                    Cat.DROP,
                    "Sortable reorder: confirmation success",
                    ok=True,
                    **ctx_zone,
                )
                return True
            except TimeoutException:
                self.session.emit_diag(
                    Cat.DROP,
                    "Sortable reorder: confirmation still pending (timeout)",
                    ok=False,
                    **ctx_zone,
                )

            try:
                self.sections.wait_for_canvas_for_current_section(timeout=2)
            except Exception:
                pass

        return False
    
    def _clear_sortable_residue(self, *, note: str = "", timeout: float = 1.5) -> None:
        """
        Best-effort cleanup of Sortable.js drag state to avoid ghost/chosen residue
        interfering with subsequent clicks/edits.
        """
        driver = self.driver
        ctx_cleanup = self._ctx(
            kind="drop",
            sec=self.sections.current_section_id or "",
            a="sortable_cleanup",
        )

        try:
            active = driver.execute_script(
                """
                return !!document.querySelector('.sortable-ghost, .sortable-chosen, #section-fields.sortable--dragging, [class*="sortable--dragging"]');
                """
            )
            if not active:
                return
        except Exception:
            pass

        # 1) ESC a couple times (often cancels drag mode)
        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).pause(0.05).send_keys(Keys.ESCAPE).perform()
        except Exception:
            pass

        # 2) Click a neutral area (canvas) to drop focus/drag mode
        try:
            canvas = driver.find_element(By.CSS_SELECTOR, "#section-fields")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", canvas)
            canvas.click()
        except Exception:
            pass

        # 3) Remove stubborn Sortable classes from DOM (best-effort)
        try:
            driver.execute_script(
                """
                // Remove obvious drag-state classes
                document.querySelectorAll('.sortable-ghost, .sortable-chosen').forEach(el => {
                el.classList.remove('sortable-ghost');
                el.classList.remove('sortable-chosen');
                });

                // Remove dragging class on container
                const sf = document.querySelector('#section-fields');
                if (sf) sf.classList.remove('sortable--dragging');

                // Remove any class containing sortable--dragging (defensive)
                document.querySelectorAll('[class*="sortable--dragging"]').forEach(el => {
                el.className = el.className.split(' ').filter(c => c.indexOf('sortable--dragging') === -1).join(' ');
                });
                """
            )
        except Exception:
            pass

        # 4) Wait briefly for UI to settle
        end = time.time() + timeout
        while time.time() < end:
            try:
                active = driver.execute_script(
                    """
                    return !!document.querySelector('.sortable-ghost, .sortable-chosen, #section-fields.sortable--dragging, [class*="sortable--dragging"]');
                    """
                )
                if not active:
                    return
            except Exception:
                return
            time.sleep(0.05)

        self.session.emit_diag(
            Cat.DROP,
            "Sortable residue still detected after cleanup",
            note=note,
            **ctx_cleanup,
        )

    def _find_scroll_container_for(self, el):
        """
        Return the nearest scrollable ancestor for `el` (including itself),
        preferring the real scrolling container (scrollHeight > clientHeight).
        """
        driver = self.driver
        try:
            return driver.execute_script(
                """
                function isScrollable(node) {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                const oy = style.overflowY;
                const canScroll = (oy === 'auto' || oy === 'scroll' || oy === 'overlay');
                return canScroll && node.scrollHeight > node.clientHeight + 5;
                }

                let node = arguments[0];
                // Try self + ancestors
                while (node) {
                if (isScrollable(node)) return node;
                node = node.parentElement;
                }

                // Fallback: document scrolling element
                return document.scrollingElement || document.documentElement;
                """,
                el,
            )
        except Exception:
            return None

    def _rect_info(self, el):
        return self.driver.execute_script(
            """
            const el = arguments[0];
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {
            left: r.left,
            top: r.top,
            right: r.right,
            bottom: r.bottom,
            width: r.width,
            height: r.height,
            vw: window.innerWidth,
            vh: window.innerHeight
            };
            """,
            el,
        )

    def _wait_for_drag_mode(self, timeout: float = 2.5) -> bool:
        driver = self.driver
        wait = WebDriverWait(driver, timeout)
        try:
            return bool(wait.until(lambda d: d.execute_script("""
                return !!document.querySelector('.designer__canvas--dragging')
                    || document.querySelectorAll('.draggable-dropzone--active').length > 0;
            """)))
        except TimeoutException:
            return False
        
    def _find_dropzone_by_dom_id(self, dz_id: Optional[str], timeout: float = 2.0) -> WebElement | None:
        """
        Resolve a dropzone element by its DOM id during drag mode.

        What this does (vs old version):
        - Requires drag-mode to be active (dropzones are transient).
        - Tries to "wake" dropzones (some UIs only mark them active after movement).
        - Scrolls dropzone into view and re-reads it (avoids offscreen/negative rect issues).
        - Verifies active dropzone state (draggable-dropzone--active) when present.
        """
        driver = self.driver
        ctx_resolve = self._ctx(
            kind="drop",
            sec=self.sections.current_section_id or "",
            a="dropzone_resolve",
        )

        if not dz_id:
            return None

        end = time.time() + timeout
        last_exc: Exception | None = None

        while time.time() < end:
            try:
                # Ensure drag mode is active (dropzones may not exist otherwise)
                if not self._wait_for_drag_mode(timeout=0.2):
                    time.sleep(0.05)
                    continue

                # Find element by id
                el = driver.find_element(By.ID, dz_id)

                # Some UIs only "activate" dropzones after movement; if you have a wake helper, call it
                # (safe even if it does nothing). We'll rely on the caller's existing micro-move too,
                # but keeping it here makes the resolver more self-sufficient.
                try:
                    ActionChains(driver).move_by_offset(0, 1).perform()
                except Exception:
                    pass

                # Scroll into view (important for huge/negative-top cases)
                try:
                    self._scroll_dropzone_to_visible(el, block="end")
                except Exception:
                    pass

                # Re-find after scroll (Turbo can stale elements)
                el = driver.find_element(By.ID, dz_id)

                # Active-state check (prefer JS because is_displayed() can be misleading for overlays)
                ok = driver.execute_script(
                    """
                    const el = arguments[0];
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const visible = r.width > 5 && r.height > 5 && r.bottom > 0 && r.top < window.innerHeight;
                    const active = el.classList.contains('draggable-dropzone--active') || el.classList.contains('designer__canvas__dropping-field-zone');
                    return !!(visible && active);
                    """,
                    el,
                )

                if ok:
                    return el

                # Not ready yet—give it a beat
                time.sleep(0.05)

            except (StaleElementReferenceException, WebDriverException) as e:
                last_exc = e
                time.sleep(0.05)
            except Exception as e:
                last_exc = e
                time.sleep(0.05)

        ids = driver.execute_script("""
            return Array.from(document.querySelectorAll('.designer__canvas__dropping-field-zone.draggable-dropzone--active'))
            .map(el => el.id)
            .filter(Boolean);
        """)
        self.session.emit_diag(
            Cat.DROP,
            "Dropzone resolve by id failed",
            dz_id=dz_id,
            last_exc=repr(last_exc),
            active_dropzones=ids,
            **ctx_resolve,
        )
        return None
        
    def _compute_dropzone_dom_id(
        self,
        *,
        drop_location: str,
        anchor_field_id: str | None,
    ) -> str | None:
        # empty section placeholder
        if self._section_has_empty_placeholder():
            return "drop-zone-0"

        if drop_location == "section_top":
            return self._get_first_dropzone_id()

        if drop_location == "section_bottom":
            return self._get_last_dropzone_id()

        if drop_location == "after_field":
            if not anchor_field_id:
                # if no anchor and not empty, we can fall back to bottom
                return self._get_last_dropzone_id()
            return f"dropzone-{anchor_field_id}--bottom"

        # section_top handled outside (selector-based) for now
        return None
    
    def _section_has_empty_placeholder(self) -> bool:
        """
        True only when the active section appears genuinely empty.

        Why:
        - Turbo can leave stale/hidden `#drop-zone-0` nodes around.
        - Using presence-only checks can misclassify a non-empty section as empty,
        causing drops to target the placeholder path incorrectly.
        """
        try:
            return bool(self.driver.execute_script("""
                const dz0 = document.querySelector('#drop-zone-0');
                if (!dz0) return false;

                // "Visible enough" check
                const r = dz0.getBoundingClientRect();
                const visible = r.width > 10 && r.height > 10 && r.bottom > 0 && r.top < window.innerHeight;

                // Confirm that the active section has no wrappers
                const wrappers = document.querySelectorAll('#section-fields .section-field[id^="section-field-"]');
                const isEmpty = !wrappers || wrappers.length === 0;

                return !!(visible && isEmpty);
            """))
        except Exception:
            return False
        
    def _get_last_dropzone_id(self) -> str | None:
        """
        Compute the DOM id for the 'section_bottom' dropzone using:
        - Registry last field id (authoritative intent) by default
        - DOM last field id as a guard when they diverge (prevents "between last two" cascades)

        Returns:
        - "drop-zone-0" if section appears empty
        - "dropzone-<field_id>--bottom" otherwise
        """
        try:
            # If truly empty, we always use the placeholder dropzone
            if self._section_has_empty_placeholder():
                return "drop-zone-0"

            # DOM reality (eventually consistent)
            dom_ids = self._get_active_section_field_ids() or []
            dom_last = dom_ids[-1] if dom_ids else None

            # Registry intent (usually correct if prior add confirmed)
            section_id = self.sections.current_section_id or ""
            ctx_anchor = self._ctx(kind="drop", sec=section_id, a="dropzone_anchor")
            reg_ids = [
                fh.field_id
                for fh in self.registry.fields_for_section(section_id)
                if fh.field_id
            ]
            reg_last = reg_ids[-1] if reg_ids else None

            # Choose anchor:
            # - Prefer registry if present
            # - If registry exists but DOM shows a different last, prefer DOM
            #   (until reorder is fully reliable, this prevents anchor drift)
            anchor = reg_last or dom_last
            if reg_last and dom_last and reg_last != dom_last:
                self.session.emit_diag(
                    Cat.DROP,
                    "Bottom anchor mismatch; using DOM last",
                    registry_last=reg_last,
                    dom_last=dom_last,
                    **ctx_anchor,
                )
                anchor = dom_last

            if not anchor:
                return None

            return f"dropzone-{anchor}--bottom"

        except Exception:
            return None

    def _get_first_dropzone_id(self) -> str | None:
        """
        Compute the DOM id for the 'section_top' dropzone.

        Returns:
        - "drop-zone-0" if section is empty
        - "dropzone-<field_id>--top" where <field_id> is the first field in DOM/registry
        """
        try:
            if self._section_has_empty_placeholder():
                return "drop-zone-0"

            dom_ids = self._get_active_section_field_ids() or []
            dom_first = dom_ids[0] if dom_ids else None

            section_id = self.sections.current_section_id or ""
            ctx_anchor = self._ctx(kind="drop", sec=section_id, a="dropzone_anchor")
            reg_ids = [
                fh.field_id
                for fh in self.registry.fields_for_section(section_id)
                if fh.field_id
            ]
            reg_first = reg_ids[0] if reg_ids else None

            anchor = reg_first or dom_first
            if reg_first and dom_first and reg_first != dom_first:
                self.session.emit_diag(
                    Cat.DROP,
                    "Top anchor mismatch; using DOM first",
                    registry_first=reg_first,
                    dom_first=dom_first,
                    **ctx_anchor,
                )
                anchor = dom_first

            if not anchor:
                return None

            return f"dropzone-{anchor}--top"

        except Exception:
            return None

    def _debug_dump_section_registry_vs_dom(
        self,
        *,
        note: str,
        section_id: str | None = None,
        max_items: int = 60,
    ) -> None:
        """
        Debug-only: print registry vs DOM state for the active section.

        Shows:
        - DOM field_id order
        - Registry FieldHandle order (field_id, field_type_key, fi_index)
        - Diff: dom_only + registry_only
        - Type counts
        """
        if not self._instrument():
            return
        self.session.counters.inc("trace.registry_vs_dom_dumps")
        if self.session.instr_policy.mode != LogMode.TRACE:
            return

        sid = section_id or (self.sections.current_section_id or "")
        ctx_dump = self._ctx(kind="registry", sec=sid, a="debug_dump_registry")

        # --- DOM snapshot (order matters) ---
        dom_ids = self._get_active_section_field_ids() or []

        # --- Registry snapshot (order matters: append order) ---
        reg_fields = self.registry.fields_for_section(sid) if sid else []
        reg_ids = [fh.field_id for fh in reg_fields if fh.field_id]

        # Registry tuples are more informative than ids alone
        reg_triplets = [
            (fh.field_id, fh.field_type_key, fh.fi_index)
            for fh in reg_fields
            if fh.field_id
        ]

        dom_set = set(dom_ids)
        reg_set = set(reg_ids)

        dom_only = list(dom_set - reg_set)
        reg_only = list(reg_set - dom_set)

        reg_type_counts = Counter([fh.field_type_key for fh in reg_fields if fh.field_type_key])

        dom_ids_disp = dom_ids[:max_items]
        reg_triplets_disp = reg_triplets[:max_items]

        self.session.emit_trace(
            Cat.REG,
            "Section registry vs DOM snapshot",
            note=note,
            section_id=sid,
            dom_count=len(dom_ids),
            reg_count=len(reg_ids),
            dom_only_count=len(dom_only),
            reg_only_count=len(reg_only),
            reg_type_counts=dict(reg_type_counts),
            dom_ids=dom_ids_disp,
            reg_triplets=reg_triplets_disp,
            dom_only_sample=dom_only[:20],
            reg_only_sample=reg_only[:20],
            key="REG.snapshot",
            every_s=2.0,
            **ctx_dump,
        )

    def _debug_dump_section_order_alignment(
        self,
        *,
        note: str,
        section_id: str | None = None,
        max_items: int = 60,
    ) -> None:
        """
        Debug-only: deeper ordering comparison.

        Prints:
        - DOM ids in order
        - Registry handles in append order (fi_index, type, id)
        - Registry handles sorted by fi_index (fi_index, type, id)
        - DOM order annotated with registry info when available
        """
        if not self._instrument():
            return
        self.session.counters.inc("trace.order_alignment_dumps")
        if self.session.instr_policy.mode != LogMode.TRACE:
            return

        sid = section_id or (self.sections.current_section_id or "")
        ctx_order = self._ctx(kind="registry", sec=sid, a="debug_dump_order")

        dom_ids = self._get_active_section_field_ids() or []

        reg_fields = self.registry.fields_for_section(sid) if sid else []
        reg_triplets_append = [
            (fh.fi_index, fh.field_type_key, fh.field_id)
            for fh in reg_fields
            if fh.field_id
        ]
        reg_triplets_spec = sorted(
            reg_triplets_append,
            key=lambda t: (t[0] is None, t[0] if t[0] is not None else 10**9),
        )

        # Map for annotation: field_id -> (fi_index, type)
        reg_map = {
            fh.field_id: (fh.fi_index, fh.field_type_key)
            for fh in reg_fields
            if fh.field_id
        }

        dom_annotated = [
            (fid, *reg_map.get(fid, (None, None)))
            for fid in dom_ids
        ]

        # Bound output
        dom_ids_disp = dom_ids[:max_items]
        reg_append_disp = reg_triplets_append[:max_items]
        reg_spec_disp = reg_triplets_spec[:max_items]
        dom_annotated_disp = dom_annotated[:max_items]

        dom_set = set(dom_ids)
        reg_set = set([t[2] for t in reg_triplets_append])
        dom_only = list(dom_set - reg_set)
        reg_only = list(reg_set - dom_set)

        self.session.emit_trace(
            Cat.REG,
            "Section order alignment snapshot",
            note=note,
            section_id=sid,
            dom_count=len(dom_ids),
            reg_append_count=len(reg_triplets_append),
            dom_ids=dom_ids_disp,
            reg_append=reg_append_disp,
            reg_spec=reg_spec_disp,
            dom_annotated=dom_annotated_disp,
            dom_only_sample=dom_only[:20],
            reg_only_sample=reg_only[:20],
            key="REG.order.snapshot",
            every_s=2.0,
            **ctx_order,
        )
