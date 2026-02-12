import re
import time
from typing import Any

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException

from .activity_registry import ActivityRegistry
from .instrumentation import Cat
from .session import CASession
from .field_handles import FieldHandle

_FIELD_ID_RE = re.compile(r"--(\d+)$")

class ActivityDeleter:
    """
    Delete/remove fields from an existing activity on the Activity Builder canvas.

    This class is meant to be orthogonal to:
      - ActivityBuilder (creates fields)
      - ActivityEditor (edits fields)

    It assumes you are already on the Activity Builder screen.
    """

    # Generic selector for any field on the canvas
    FIELD_SELECTOR = ".designer__field"

    def __init__(self, session: CASession, registry: ActivityRegistry):
        """
        :param session: CASession instance
        """
        self.session = session
        self.driver = session.driver
        self.wait = session.wait
        self.registry = registry

    def _ctx(
        self,
        *,
        field_id: str | None = None,
        section_id: str | None = None,
        field_type: str | None = None,
        kind: str | None = None,
        attempt: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "sec": section_id or "",
            "fid": field_id,
            "type": field_type,
        }
        if kind:
            ctx["kind"] = kind
        if attempt:
            ctx["a"] = attempt
        if extra:
            ctx.update(extra)
        return ctx

    # ---------- field discovery ----------

    def get_all_fields(self, field_selector: str | None = None):
        """
        Return a list of all field elements on the canvas.

        :param field_selector: optional override to restrict by type,
                               e.g. '.designer__field.designer__field--text'
        """
        sel = field_selector or self.FIELD_SELECTOR
        fields = self.driver.find_elements(By.CSS_SELECTOR, sel)
        self.session.emit_diag(
            Cat.SECTION,
            f"Found {len(fields)} fields on the canvas (selector='{sel}').",
            **self._ctx(kind="field_discovery"),
        )
        return fields

    def get_last_field(self, field_selector: str | None = None):
        """
        Get the last field on the canvas (visually, the bottom-most
        given the DOM ordering).

        Raises TimeoutException if none exist.
        """
        fields = self.get_all_fields(field_selector=field_selector)
        if not fields:
            raise TimeoutException(f"No fields found on the canvas (selector='{field_selector or self.FIELD_SELECTOR}').")
        last = fields[-1]
        self.session.emit_diag(
            Cat.SECTION,
            "Using last field on the canvas for deletion.",
            **self._ctx(kind="field_discovery"),
        )
        return last

    # ---------- single-field deletion ----------

    def delete_field(self, field_el, confirm_timeout: int = 10) -> bool:
        """
        Delete a single field element from the canvas.

        Steps:
        - scroll field into view
        - locate its delete control inside .designer__field__actions
        - click the delete control (via JS to avoid hover issues)
        - handle CA's confirmation modal
        - wait until the element disappears from the DOM

        Returns True if it appears to have been deleted, False otherwise.
        """
        driver = self.driver
        self.session.counters.inc("deleter.delete_attempts")

        # Try to capture something stable to detect deletion (e.g. data attr or id)
        # CA field id (numeric) and raw DOM id for logging
        ca_field_id = self._get_ca_field_id_from_element(field_el)
        dom_field_id = field_el.get_attribute("id") or "<no-dom-id>"

        id_for_log = ca_field_id or dom_field_id
        ctx = self._ctx(field_id=ca_field_id, kind="delete_field", dom_id=dom_field_id)

        try:
            # 1. Scroll field into view
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});",
                field_el,
            )

            # 2. Find delete <a> inside this field's actions
            actions_container = field_el.find_element(
                By.CSS_SELECTOR,
                ".designer__field__actions"
            )

            delete_link = actions_container.find_element(
                By.CSS_SELECTOR,
                "a[data-turbo-method='delete']"
            )

            self.session.emit_diag(
                Cat.SECTION,
                f"Clicking delete control for field {id_for_log} via JS...",
                **ctx,
            )

            # 3. Click via JS to avoid any hover/visibility issues
            driver.execute_script("arguments[0].click();", delete_link)

            # 4. Handle confirmation modal (if CASession has a helper, use it)
            #    We'll be conservative: try session.handle_modal_dialogs('confirm')
            handled_modal = False
            if hasattr(self.session, "handle_modal_dialogs"):
                try:
                    self.session.counters.inc("deleter.modal_waits")
                    modal_wait_start = time.monotonic()
                    handled_modal = self.session.handle_modal_dialogs(
                        mode="confirm",
                        timeout=confirm_timeout
                    )
                    modal_wait_s = round(time.monotonic() - modal_wait_start, 2)
                    if handled_modal:
                        self.session.counters.inc("deleter.modal_confirmed")
                        self.session.counters.inc("deleter.modal_present")
                    else:
                        self.session.counters.inc("deleter.modal_absent")
                    self.session.emit_diag(
                        Cat.SECTION,
                        f"Modal handler result for field {id_for_log}: {handled_modal}",
                        modal_wait_s=modal_wait_s,
                        **ctx,
                    )
                except Exception as e:
                    self.session.counters.inc("deleter.modal_errors")
                    self.session.emit_signal(
                        Cat.SECTION,
                        f"Error while handling modal dialogs: {e}",
                        level="warning",
                        **ctx,
                    )

            # 5. Wait for field to disappear from DOM
            def field_gone(_):
                try:
                    # The original WebElement will usually go stale when removed.
                    field_el.is_displayed()
                    return False
                except Exception:
                    return True

            try:
                self.wait.until(field_gone)
                self.session.counters.inc("deleter.fields_deleted")
                self.session.emit_diag(
                    Cat.SECTION,
                    f"Field {id_for_log} deleted (no longer present in DOM).",
                    **ctx,
                )
                
                # Update registry: remove this field handle if we know its CA id
                if ca_field_id:
                    try:
                        self.registry.remove_field(ca_field_id)
                        self.session.emit_diag(
                            Cat.REG,
                            f"Registry: removed field handle for id {ca_field_id}.",
                            **self._ctx(field_id=ca_field_id, kind="registry_remove"),
                        )
                    except Exception as e:
                        self.session.emit_signal(
                            Cat.REG,
                            f"Registry: error while removing field id {ca_field_id}: {e}",
                            level="warning",
                            **self._ctx(field_id=ca_field_id, kind="registry_remove"),
                        )

                return True
            except TimeoutException:
                self.session.counters.inc("deleter.delete_timeouts")
                self.session.emit_signal(
                    Cat.SECTION,
                    f"Timeout waiting for field {id_for_log} to disappear after delete.",
                    level="warning",
                    **ctx,
                )
                return False

        except WebDriverException as e:
            self.session.counters.inc("deleter.delete_errors")
            self.session.emit_signal(
                Cat.SECTION,
                f"Could not delete field {id_for_log}: {e}",
                level="warning",
                **ctx,
            )
            return False
        except Exception as e:
            self.session.counters.inc("deleter.delete_errors")
            self.session.emit_signal(
                Cat.SECTION,
                f"Unexpected error while deleting field {id_for_log}: {e}",
                level="warning",
                **ctx,
            )
            return False

    # ---------- convenience helpers ----------

    def delete_last_field(self, field_selector: str | None = None) -> bool:
        """
        Delete the last field on the canvas (optionally restricted by selector).

        Returns True if deletion appears successful, False otherwise.
        """
        try:
            field_el = self.get_last_field(field_selector=field_selector)
        except TimeoutException as e:
            self.session.emit_signal(
                Cat.SECTION,
                str(e),
                level="warning",
                **self._ctx(kind="delete_last_field"),
            )
            return False

        return self.delete_field(field_el)

    def delete_all_fields(self, field_selector: str | None = None) -> int:
        """
        Delete all fields matching the selector, starting from the bottom.

        Returns the number of fields successfully deleted.

        field_selector:
          - None  -> all .designer__field
          - ".designer__field.designer__field--text" -> only text/paragraph fields
          - etc.
        """
        sel = field_selector or self.FIELD_SELECTOR
        self.session.emit_diag(
            Cat.SECTION,
            f"Starting bulk delete for fields matching selector='{sel}'",
            **self._ctx(kind="bulk_delete"),
        )

        count = 0

        while True:
            self.session.counters.inc("deleter.bulk_loop_iters")
            self.session.counters.inc("deleter.bulk_scan_calls")
            fields = self.get_all_fields(field_selector=field_selector)
            if not fields:
                break

            self.session.emit_diag(
                Cat.SECTION,
                "Bulk delete scan result",
                count=len(fields),
                key="deleter.bulk_scan",
                every_s=2.0,
                **self._ctx(kind="bulk_delete"),
            )

            field_el = fields[-1]  # always delete from the bottom
            if not self.delete_field(field_el):
                # If a deletion fails, stop rather than looping forever
                self.session.emit_signal(
                    Cat.SECTION,
                    "Deletion of a field failed during bulk delete; stopping early.",
                    level="warning",
                    **self._ctx(kind="bulk_delete"),
                )
                break

            count += 1

        self.session.emit_diag(
            Cat.SECTION,
            f"Deleted {count} field(s) from the canvas (selector='{sel}').",
            **self._ctx(kind="bulk_delete"),
        )
        return count
    
    def _get_ca_field_id_from_element(self, field_el) -> str | None:
        """
        Try to infer the CloudAssess field id (e.g. '27435179') from a field element.

        This mirrors the logic ActivityEditor uses: we look for known id patterns
        inside the field and extract the numeric suffix.
        """
        # 1) Try model-answer description id
        try:
            model_block = field_el.find_element(
                By.CSS_SELECTOR,
                "[id^='designer__field__model-answer-description--']",
            )
            mid = model_block.get_attribute("id") or ""
            m = _FIELD_ID_RE.search(mid)
            if m:
                return m.group(1)
        except NoSuchElementException:
            pass

        # 2) Fallback: main description id
        try:
            desc_block = field_el.find_element(
                By.CSS_SELECTOR,
                "[id^='designer__field__description--']",
            )
            did = desc_block.get_attribute("id") or ""
            m = _FIELD_ID_RE.search(did)
            if m:
                return m.group(1)
        except NoSuchElementException:
            pass

        self.session.emit_diag(
            Cat.SECTION,
            "Could not infer CA field id for field element during deletion.",
            **self._ctx(kind="field_id"),
        )
        return None
    
    def _get_field_element_by_id(self, field_id: str):
        """
        Locate a field element on the canvas by its CA field id.

        This mirrors ActivityEditor.get_field_by_id: find any element whose id
        ends with '--<field_id>', then climb to the .designer__field root.
        """
        driver = self.driver

        el = driver.find_element(
            By.CSS_SELECTOR,
            f"#section-fields [id$='--{field_id}']",
        )
        return el.find_element(
            By.XPATH,
            "./ancestor::div[contains(@class,'designer__field')]",
        )

    def delete_field_by_handle(self, handle: FieldHandle, confirm_timeout: int = 10) -> bool:
        """
        Delete the field identified by this FieldHandle, if present on the canvas.

        Returns True if deletion appears successful, False otherwise.
        """
        field_id = (handle.field_id or "").strip()
        if not field_id:
            self.session.emit_signal(
                Cat.SECTION,
                "FieldHandle has no field_id; cannot delete by handle.",
                level="warning",
                **self._ctx(kind="delete_by_handle"),
            )
            return False

        try:
            field_el = self._get_field_element_by_id(field_id)
        except Exception as e:
            self.session.emit_signal(
                Cat.SECTION,
                (
                    "Could not locate field element for id {fid} (type={ftype}, section={sec}): {err}"
                ).format(
                    fid=handle.field_id,
                    ftype=handle.field_type_key,
                    sec=handle.section_id,
                    err=e,
                ),
                level="warning",
                **self._ctx(
                    field_id=handle.field_id,
                    section_id=handle.section_id,
                    field_type=handle.field_type_key,
                    kind="delete_by_handle",
                ),
            )
            return False

        self.session.emit_diag(
            Cat.SECTION,
            "Deleting field by handle.",
            **self._ctx(
                field_id=handle.field_id,
                section_id=handle.section_id,
                field_type=handle.field_type_key,
                kind="delete_by_handle",
            ),
        )

        return self.delete_field(field_el, confirm_timeout=confirm_timeout)
    
    def delete_field_by_id(self, field_id: str, confirm_timeout: int = 10) -> bool:
        """
        Delete the field identified by CA field id, if present on canvas.
        """
        field_id = (field_id or "").strip()
        if not field_id:
            self.session.emit_signal(
                Cat.SECTION,
                "No field_id provided to delete_field_by_id.",
                level="warning",
                **self._ctx(kind="delete_by_id"),
            )
            return False

        try:
            field_el = self._get_field_element_by_id(field_id)
        except Exception as e:
            self.session.emit_signal(
                Cat.SECTION,
                f"Could not locate field element for id {field_id}: {e}",
                level="warning",
                **self._ctx(field_id=field_id, kind="delete_by_id"),
            )
            return False

        self.session.emit_diag(
            Cat.SECTION,
            f"Deleting field by id={field_id}.",
            **self._ctx(field_id=field_id, kind="delete_by_id"),
        )
        return self.delete_field(field_el, confirm_timeout=confirm_timeout)
