# src/ca_bldr/session.py
import time
import re
import logging
from typing import Optional, Tuple, Union, Any
from dataclasses import dataclass

from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    StaleElementReferenceException,
    ElementClickInterceptedException,
    ElementNotInteractableException,
)

from .driver import create_driver  # your driver factory
from .types import UIProbeSnapshot, FieldSettingsFrameInfo, FieldSettingsTabInfo, TemplateMatch
from .instrumentation import Cat, Counters, InstrumentPolicy, LogMode, RateLimiter, format_ctx
from .. import config  # src/config.py

Locator = Tuple[str, str]
ClickableTarget = Union[WebElement, Locator]

class LoginError(Exception):
    """Raised when Cloud Assess login fails."""
    pass


class CASession:
    def __init__(self, logger=None):
        self.driver = create_driver()
        self.logger = logger or logging.getLogger("ca_bldr")

        self.username = config.CA_USERNAME
        self.password = config.CA_PASSWORD
        if not self.username or not self.password:
            raise ValueError("CA_USERNAME and CA_PASSWORD must be set in .env")

        self.wait = WebDriverWait(self.driver, config.WAIT_TIME)

        # Instrumentation setup
        mode = LogMode(config.LOG_MODE) if config.LOG_MODE in ("live", "debug", "trace") else LogMode.LIVE

        self.instr_policy = InstrumentPolicy(
            mode=mode,
            diag_min_mode={},  # we can fill this later; start simple
            include_ctx=True,
            rate_limits_s=getattr(config, "LOG_RATE_LIMITS_S", {}) or {},
        )
        self.counters = Counters()
        self._rate = RateLimiter()

    def get_wait(self, timeout: int | None = None) -> WebDriverWait:
        """
        Return a WebDriverWait.

        - If timeout is None: return the session default wait (self.wait).
        - If timeout is provided: return a new WebDriverWait with that timeout.
        """
        if timeout is None:
            return self.wait
        return WebDriverWait(self.driver, timeout)

    def login(self):
        # Go to dashboard, then log in if redirected to the login page.
        # Raises LoginError if login does not succeed.
        self.driver.get(config.CA_DASHBOARD_URL)
        self.wait.until(EC.url_contains(config.CA_BASE_DOMAIN))

        # If we're already on the dashboard, nothing to do
        if not self.driver.current_url.startswith(config.CA_LOGIN_URL):
            self.logger.debug("Already logged in at %s", self.driver.current_url)
            return
        
        # Otherwise, perform login
        self.logger.info("Logging into Cloud Assess...")
        u = self.driver.find_element(By.ID, config.SELECTORS["username_id"])
        u.clear()
        u.send_keys(self.username or "")

        p = self.driver.find_element(By.ID, config.SELECTORS["password_id"])
        p.clear()
        p.send_keys(self.password or "")

        btn = self.driver.find_element(By.XPATH, config.SELECTORS["login_xpath"])
        btn.click()

        # After clicking login, we expect EITHER:
        # - dashboard URL, OR
        # - we stay on login page and see an error message
        try:
            self.wait.until(EC.url_contains(config.CA_DASHBOARD_URL))
            self.logger.info("Logged in successfully.")
            return
        except Exception:
            # Still on login page or somewhere unexpected
            current = self.driver.current_url
            self.logger.error(f"Did not reach dashboard after login attempt (URL={current})")

            # Try to detect a CA error message on the login page (flash alert)
            error_text = None
            try:
                # adjust selector once you see the actual HTML of the login error
                alert = self.driver.find_element(By.CSS_SELECTOR, ".alert.alert-danger, .alert-danger")
                if alert.is_displayed():
                    error_text = alert.text.strip()
            except Exception:
                pass

            if error_text:
                self.logger.error(f"Cloud Assess reported a login error: {error_text}")
            else:
                self.logger.error("No explicit error message found on login page.")

            # # Optional: save screenshot for debugging
            # try:
            #     self.driver.save_screenshot("login_error.png")
            #     self.logger.info("Saved screenshot to login_error.png")
            # except Exception:
            #     pass

            # Raise a custom error so callers can handle it
            raise LoginError("Cloud Assess login failed. Check credentials/URL.")

    def refresh_page(self) -> None:
        self.driver.refresh()

    def click_element_safely(
        self,
        target: ClickableTarget,
        *,
        retries: int = 3,
        scroll: bool = True,
        use_js_fallback: bool = True,
        post_wait: Optional[Locator] = None,
        label: str = "",
    ) -> bool:
        """
        Click an element robustly in a Turbo/Stimulus-heavy UI.

        Supports:
          - target as WebElement OR (By, selector) locator
          - retries for stale/intercepted/not-interactable states
          - scroll into view before click
          - JS click fallback
          - optional post-click wait (presence/visibility/clickable anchor)

        Args:
            target: WebElement or locator tuple (By.CSS_SELECTOR, "...")
            timeout: wait timeout per attempt when locating/clickable
            retries: number of click attempts
            scroll: scroll element into view before click
            use_js_fallback: attempt JS click when normal click fails
            post_wait: optional locator to wait for after click succeeds
            post_wait_timeout: timeout for post_wait
            label: for nicer logs

        Returns:
            True if click (and optional post_wait) succeeded, else False.
        """
        driver = self.driver
        logger = self.logger
        wait = self.wait

        if not label:
            if isinstance(target, tuple):
                label = f"{target[0]} {target[1]}"
            else:
                html = ""
                try:
                    label = target.get_attribute("outerHTML") or ""
                except Exception:
                    html = ""
                label = html[:120] if html else "<element>"

        def _resolve_element() -> WebElement:
            if isinstance(target, tuple):
                by, sel = target
                return wait.until(
                    EC.presence_of_element_located((by, sel))
                )
            return target

        def _scroll_into_view(el: WebElement) -> None:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', inline:'center'});", el
            )

        for attempt in range(1, retries + 1):
            try:
                el = _resolve_element()
                if el is None:
                    logger.warning("click_element_safely: could not resolve element %s", label or target)
                    continue

                # Ensure it's interactable (best effort)
                try:
                    wait.until(EC.visibility_of(el))
                except Exception:
                    # Some elements are visible but fail the visibility check due to overlays/frames
                    pass

                if scroll:
                    try:
                        _scroll_into_view(el)
                    except Exception:
                        pass

                # Try normal click first
                try:
                    wait.until(lambda d: el.is_enabled())
                except Exception:
                    pass

                try:
                    el.click()
                    logger.debug("click_element_safely: clicked (native) %s on attempt %d", label or "", attempt)
                except (ElementClickInterceptedException, ElementNotInteractableException, WebDriverException) as e:
                    logger.debug(
                        "click_element_safely: native click failed %s on attempt %d: %s",
                        label or "",
                        attempt,
                        e,
                    )
                    if not use_js_fallback:
                        raise

                    # JS click fallback
                    try:
                        driver.execute_script("arguments[0].click();", el)
                        logger.debug("click_element_safely: clicked (js) %s on attempt %d", label or "", attempt)
                    except Exception as e2:
                        logger.debug(
                            "click_element_safely: JS click failed %s on attempt %d: %s",
                            label or "",
                            attempt,
                            e2,
                        )
                        raise

                # Optional post-click wait
                if post_wait:
                    try:
                        wait.until(
                            EC.presence_of_element_located(post_wait)
                        )
                    except TimeoutException:
                        logger.warning(
                            "click_element_safely: post_wait not satisfied after click %s (attempt %d).",
                            label or "",
                            attempt,
                        )
                        # This may still be a valid click in Turbo UI; retry if attempts remain
                        if attempt < retries:
                            time.sleep(0.2)
                            continue
                        return False

                return True

            except StaleElementReferenceException:
                logger.debug(
                    "click_element_safely: stale element %s on attempt %d; retrying...",
                    label or "",
                    attempt,
                )
                time.sleep(0.2)
                continue
            except TimeoutException as e:
                logger.debug(
                    "click_element_safely: timeout locating/clicking %s on attempt %d: %s",
                    label or "",
                    attempt,
                    e,
                )
                time.sleep(0.2)
                continue
            except Exception as e:
                logger.debug(
                    "click_element_safely: unexpected error %s on attempt %d: %s",
                    label or "",
                    attempt,
                    e,
                )
                time.sleep(0.2)
                continue

        logger.warning("click_element_safely: giving up on %s after %d attempts.", label or target, retries)
        return False
    
    def handle_modal_dialogs(self, mode: str = "confirm", timeout: int = 10) -> bool:
        """
        Handle Cloud Assess confirmation modals (e.g. delete field, delete section).

        mode:
          - "confirm": click the primary/accept/delete button
          - "cancel": click the cancel/close button

        Returns:
          True if a relevant modal was found and a button was clicked,
          False if no modal appeared or we couldn't act on it.
        """
        driver = self.driver
        wait = self.get_wait(timeout)
        logger = self.logger

        # We look for a generic visible modal. CA appears to use standard
        # Bootstrap-style modals, so we'll use '.modal.show' as a starting point.
        modal_sel = ".modal.show, [role='dialog'][aria-modal='true']"

        try:
            logger.info(f"Waiting for modal dialog (mode='{mode}') up to {timeout}s...")

            def modal_visible(_):
                try:
                    els = driver.find_elements(By.CSS_SELECTOR, modal_sel)
                    # pick only those that are displayed
                    return any(el.is_displayed() for el in els)
                except Exception:
                    return False

            try:
                wait.until(modal_visible)
            except TimeoutException:
                logger.info("No modal dialog appeared within timeout.")
                return False

            # At this point, we should have at least one visible modal
            modals = [
                el
                for el in driver.find_elements(By.CSS_SELECTOR, modal_sel)
                if el.is_displayed()
            ]
            if not modals:
                logger.info("Modal seemed to appear then disappear; nothing to handle.")
                return False

            modal = modals[-1]  # assume the last one is the active one
            logger.info("Modal dialog detected; attempting to handle it...")

            # Gather clickable candidates inside the modal
            buttons = modal.find_elements(By.CSS_SELECTOR, "button, a.btn, a[role='button']")

            # Helper to score buttons by text for confirm/cancel
            def pick_button(candidates, keywords):
                lowered_keywords = [k.lower() for k in keywords]
                scored = []
                for b in candidates:
                    try:
                        text = (b.text or "").strip().lower()
                    except Exception:
                        text = ""
                    if not text:
                        continue
                    score = sum(1 for k in lowered_keywords if k in text)
                    if score > 0:
                        scored.append((score, b))
                if not scored:
                    return None
                # highest score first
                scored.sort(key=lambda t: t[0], reverse=True)
                return scored[0][1]

            if mode == "confirm":
                # Common confirm labels: Delete field, Yes, Delete, OK, Yes, duplicate, etc.
                confirm_btn = pick_button(
                    buttons,
                    ["delete", "yes", "ok", "confirm", "duplicate"]
                )
                if not confirm_btn:
                    # fallback: any primary/danger button
                    for b in buttons:
                        classes = b.get_attribute("class") or ""
                        if "btn-danger" in classes or "btn-primary" in classes:
                            confirm_btn = b
                            break
                if not confirm_btn:
                    logger.warning("No suitable confirm button found in modal.")
                    return False

                logger.info(f"Clicking confirm button in modal: '{confirm_btn.text.strip()}'")
                driver.execute_script("arguments[0].click();", confirm_btn)

            elif mode == "cancel":
                cancel_btn = pick_button(
                    buttons,
                    ["cancel", "close", "no"]
                )
                if not cancel_btn:
                    # fallback: look for a secondary button
                    for b in buttons:
                        classes = b.get_attribute("class") or ""
                        if "btn-secondary" in classes:
                            cancel_btn = b
                            break
                if not cancel_btn:
                    logger.warning("No suitable cancel button found in modal.")
                    return False

                logger.info(f"Clicking cancel button in modal: '{cancel_btn.text.strip()}'")
                driver.execute_script("arguments[0].click();", cancel_btn)

            else:
                logger.warning(f"Unknown modal handling mode: {mode}")
                return False
            
            # --- Wait for modal to close, then clean up body state ---

            def no_visible_modal(_):
                try:
                    els = driver.find_elements(By.CSS_SELECTOR, modal_sel)
                    return not any(el.is_displayed() for el in els)
                except Exception:
                    return True

            try:
                wait.until(no_visible_modal)
                logger.info("Modal dialog has closed.")
            except TimeoutException:
                logger.warning("Modal dialog did not fully close within timeout.")

            # Clean up typical Bootstrap-style 'modal-open' artifacts
            try:
                driver.execute_script("""
                    (function() {
                        var docEl = document.documentElement;
                        var body = document.body;

                        // 1. Remove typical modal-open classes / inline styles
                        if (body) {
                            body.classList.remove('modal-open');
                            if (body.style.overflow === 'hidden') body.style.overflow = '';
                            if (body.style.paddingRight) body.style.paddingRight = '';
                        }
                        if (docEl) {
                            docEl.classList.remove('modal-open');
                            if (docEl.style.overflow === 'hidden') docEl.style.overflow = '';
                            if (docEl.style.paddingRight) docEl.style.paddingRight = '';
                        }

                        // 2. Remove any modal backdrops still hanging around
                        var backdrops = document.querySelectorAll('.modal-backdrop, .modal-backdrop.show, .modal-backdrop.fade');
                        backdrops.forEach(function(bd) {
                            if (bd && bd.parentNode) {
                                bd.parentNode.removeChild(bd);
                            }
                        });

                        // 3. In case CA uses a generic overlay class, try a conservative cleanup:
                        var overlays = document.querySelectorAll('[data-controller="modal-backdrop"], .overlay, .backdrop');
                        overlays.forEach(function(el) {
                            // Only remove if it looks like a full-screen cover
                            var style = window.getComputedStyle(el);
                            if (style.position === 'fixed' || style.position === 'absolute') {
                                if (style.zIndex && parseInt(style.zIndex, 10) >= 1000) {
                                    if (el.parentNode) el.parentNode.removeChild(el);
                                }
                            }
                        });

                        // 4. Trigger a resize so any layout scripts recalc sizes if needed
                        window.dispatchEvent(new Event('resize'));
                    })();
                """)
                logger.info("Body/html scroll and overlay state cleaned after modal.")
            except Exception as e:
                logger.debug(f"Ignoring error while cleaning scroll/overlay state: {e}")

            return True        

        except WebDriverException as e:
            logger.warning(f"WebDriverException while handling modal dialog: {e}")
            return False
        except Exception as e:
            logger.warning(f"Unexpected error while handling modal dialog: {e}")
            return False

    def clear_and_type(self, el: WebElement, text: str, *, click_first: bool = True) -> None:
        if click_first:
            try:
                el.click()
            except Exception:
                pass
        try:
            el.clear()
        except Exception:
            # Some inputs don't support clear() well; fallback to ctrl+a
            el.send_keys(Keys.CONTROL, "a")
        el.send_keys(text)

    def go_to_activity_templates(self, *, inactive: bool = False, force: bool = False, timeout: int = 10) -> None:
        """
        Navigate the current session to the CloudAssess Activity Templates page.

        If we're already there (URL + DOM sentinel), do nothing unless force=True.
        """
        target = (
            config.CA_ACTIVITY_TEMPLATES_INACTIVE_URL
            if inactive
            else config.CA_ACTIVITY_TEMPLATES_URL
        )
        driver = self.driver
        wait = self.wait
        logger = self.logger

        if not force:
            url = ""
            try:
                url = driver.current_url or ""
            except Exception:
                pass

            is_on_templates = "/activity_templates" in url
            is_inactive_now = "type=inactive" in url

            if is_on_templates and (is_inactive_now == inactive):
                # DOM sentinel to prevent false positives
                try:
                    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "turbo-frame#templates")))
                    logger.info(
                        "Already on Activity Templates page (%s); skipping navigation.",
                        "inactive" if inactive else "active",
                    )
                    return
                except TimeoutException:
                    logger.info("Templates URL matches, but frame sentinel missing; reloading page.")

        logger.info("Navigating to Activity Templates page: %s", target)
        driver.get(target)

        # Post-nav confirm (best effort)
        try:
            wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "a.btn.btn-primary[href='/activity_templates/new']")
                )
            )
        except TimeoutException:
            logger.warning("Activity Templates page loaded but 'Create Activity' button not detected (timeout=%s).", timeout)

    def close(self):
        self.driver.quit()

    def probe_ui_state_heavy(
        self,
        *,
        label: str,
        expected_field_id: str | None = None,
        expected_title: str | None = None,
        expected_section_id: str | None = None,
        field_el=None,
        include_frame_html_snippet: bool = True,
        frame_html_snippet_len: int = 1200,
        include_canvas_snippet: bool = False,
        canvas_snippet_len: int = 800,
        include_overlay_details: bool = True,
    ) -> dict[str, Any]:
        driver = self.driver

        out: dict[str, Any] = {
            "label": label,
            "expected": {
                "field_id": expected_field_id,
                "title": expected_title,
                "section_id": expected_section_id,
            },
            "observed": {
                "field_id_from_frame": None,
                "frame_present": None,
                "frame_controls": None,
                "frame_html_snippet": None,
            },
            "active_element_html": None,
            "froala_tooltips": {
                "count": None,
                "visible_first_n": [],
            },
            "field_settings_tab": {
                "present": None,
                "displayed": None,
            },
            "field_el": {
                "class": None,
                "data": {},
            },
            "overlays": {},
            "canvas": {},
        }

        # --- Observed field id from field_settings_frame ---
        try:
            frames = driver.find_elements(By.CSS_SELECTOR, "turbo-frame#field_settings_frame")
            out["observed"]["frame_present"] = len(frames)
            if frames:
                frame = frames[0]
                try:
                    controls = frame.find_elements(By.CSS_SELECTOR, "input, select, textarea, button")
                    out["observed"]["frame_controls"] = len(controls)
                except Exception:
                    out["observed"]["frame_controls"] = None

                html = frame.get_attribute("innerHTML") or ""
                m = re.search(r"/fields/(\d+)\.turbo_stream", html)
                out["observed"]["field_id_from_frame"] = m.group(1) if m else None

                if include_frame_html_snippet:
                    out["observed"]["frame_html_snippet"] = html[:frame_html_snippet_len]
        except Exception:
            pass

        # --- active element (more detailed) ---
        try:
            active = driver.execute_script("return document.activeElement;")
            out["active_element_html"] = (active.get_attribute("outerHTML") or "")[:600] if active else None
        except Exception:
            out["active_element_html"] = None

        # --- froala tooltip details (count + first N visibility) ---
        try:
            tips = driver.find_elements(By.CSS_SELECTOR, ".fr-tooltip")
            out["froala_tooltips"]["count"] = len(tips)
            vis = []
            for t in tips[:6]:
                try:
                    vis.append(bool(t.is_displayed()))
                except Exception:
                    vis.append(False)
            out["froala_tooltips"]["visible_first_n"] = vis
        except Exception:
            pass

        # --- field-settings tab details ---
        try:
            tabs = driver.find_elements(By.CSS_SELECTOR, ".designer__sidebar__tab[data-type='field-settings']")
            out["field_settings_tab"]["present"] = len(tabs)
            out["field_settings_tab"]["displayed"] = bool(tabs and tabs[0].is_displayed())
        except Exception:
            pass

        # --- field_el details ---
        if field_el is not None:
            try:
                out["field_el"]["class"] = field_el.get_attribute("class")
            except Exception:
                pass

            # optional: some useful attributes if you want them
            for attr in ("id", "data-field-id", "data-id", "data-controller"):
                try:
                    v = field_el.get_attribute(attr)
                    if v:
                        out["field_el"]["data"][attr] = v
                except Exception:
                    pass

        # --- overlays (more than just froala tooltips) ---
        if include_overlay_details:
            try:
                # bootstrap-ish / generic backdrops
                backdrops = driver.find_elements(By.CSS_SELECTOR, ".modal-backdrop, .modal-backdrop.show, .overlay, .backdrop")
                out["overlays"]["backdrops_count"] = len(backdrops)

                # any visible modals
                modals = driver.find_elements(By.CSS_SELECTOR, ".modal.show, [role='dialog'][aria-modal='true']")
                out["overlays"]["modals_visible_count"] = sum(1 for m in modals if m.is_displayed())

                # body/html scroll locks
                body_cls = driver.execute_script("return document.body ? document.body.className : '';")
                out["overlays"]["body_class"] = body_cls
                body_overflow = driver.execute_script("return document.body ? getComputedStyle(document.body).overflow : '';")
                out["overlays"]["body_overflow"] = body_overflow
            except Exception:
                pass

        # --- canvas snippet (optional; this can be “heavier”) ---
        if include_canvas_snippet:
            try:
                canvas = driver.find_element(By.CSS_SELECTOR, "#canvas-parent, .designer__canvas, #designer")
                out["canvas"]["snippet"] = (canvas.get_attribute("innerHTML") or "")[:canvas_snippet_len]
            except Exception:
                out["canvas"]["snippet"] = None

        return out

    def probe_ui_state(
        self,
        label: str,
        *,
        expected_field_id: str | None = None,
        expected_title: str | None = None,
        expected_section_id: str | None = None,
        field_el=None,
        include_frame_html_snippet: bool = False,
        frame_html_snippet_len: int = 600,
    ) -> UIProbeSnapshot:
        driver = self.driver

        out: UIProbeSnapshot = {
            "label": label,
            "expected": {
                "field_id": expected_field_id,
                "title": expected_title,
                "section_id": expected_section_id,
            },
            "observed_field_id": None,
            "active_element": None,
            "field_settings_tab": {"present": None, "displayed": None},
            "field_settings_frame": {"present": None, "controls": None},
            "froala_tooltips": None,
            "field_class": None,
        }

        # --- infer expected from field_el (best effort) ---
        try:
            if field_el is not None:
                if out["expected"]["title"] is None:
                    try:
                        t = field_el.find_element(By.CSS_SELECTOR, "h2.field__editable-label").text
                        out["expected"]["title"] = (t or "").strip() or None
                    except Exception:
                        pass

                if out["expected"]["field_id"] is None:
                    try:
                        any_id = driver.execute_script(
                            """
                            const root = arguments[0];
                            if (!root) return "";
                            const node = root.querySelector("[id*='--']");
                            return node ? (node.id || "") : "";
                            """,
                            field_el,
                        ) or ""
                        m = re.search(r"--(\d+)$", any_id)
                        if m:
                            out["expected"]["field_id"] = m.group(1)
                    except Exception:
                        pass
        except Exception:
            pass

        # --- active element summary ---
        try:
            ae = driver.switch_to.active_element
            if ae is not None:
                out["active_element"] = (ae.get_attribute("outerHTML") or "")[:220]
        except Exception:
            out["active_element"] = None

        # --- field-settings sidebar tab visible? ---
        try:
            tabs = driver.find_elements(By.CSS_SELECTOR, ".designer__sidebar__tab[data-type='field-settings']")
            out["field_settings_tab"] = {
                "present": len(tabs),
                "displayed": bool(tabs and tabs[0].is_displayed()),
            }
        except Exception:
            out["field_settings_tab"] = {"present": None, "displayed": None}

        # --- field settings frame + observed field id ---
        try:
            frames = driver.find_elements(By.CSS_SELECTOR, "turbo-frame#field_settings_frame")
            frame_info: FieldSettingsFrameInfo = {"present": len(frames), "controls": None}

            if frames:
                frame = frames[0]
                try:
                    controls = frame.find_elements(By.CSS_SELECTOR, "input, select, textarea, button")
                    frame_info["controls"] = len(controls)
                except:
                    frame_info["controls"] = None

                html = frame.get_attribute("innerHTML") or ""
                m = re.search(r"/fields/(\d+)\.turbo_stream", html)
                out["observed_field_id"] = m.group(1) if m else None

                if include_frame_html_snippet:
                    frame_info["html_snippet"] = html[:frame_html_snippet_len]

            out["field_settings_frame"] = frame_info
        except Exception:
            out["field_settings_frame"] = {"present": None, "controls": None}
            out["observed_field_id"] = None

        # --- overlays hint ---
        try:
            tips = driver.find_elements(By.CSS_SELECTOR, ".fr-tooltip")
            out["froala_tooltips"] = len(tips)
        except Exception:
            out["froala_tooltips"] = None

        # --- field root class ---
        try:
            if field_el is not None:
                out["field_class"] = (field_el.get_attribute("class") or "")[:180]
        except Exception:
            out["field_class"] = None

        return out

    def log_ui_probe(self, probe: UIProbeSnapshot, *, level: str = "info") -> None:
        """
        Single formatting point so logs are consistent.
        """
        logger = self.logger
        msg = "UI_PROBE: %s" % probe
        if level == "debug":
            logger.debug(msg)
        elif level == "warning":
            logger.warning(msg)
        else:
            logger.info(msg)

    def log_ui_probe_heavy(self, probe: dict[str, Any], *, level: str = "warning") -> None:
        logger = self.logger

        # Short headline first (easy to scan in logs)
        expected = probe.get("expected", {})
        observed = probe.get("observed", {})
        headline = {
            "label": probe.get("label"),
            "expected_field_id": expected.get("field_id"),
            "expected_title": expected.get("title"),
            "observed_field_id_from_frame": observed.get("field_id_from_frame"),
            "frame_present": observed.get("frame_present"),
            "frame_controls": observed.get("frame_controls"),
            "tooltips": probe.get("froala_tooltips", {}).get("count"),
            "tab_displayed": probe.get("field_settings_tab", {}).get("displayed"),
        }

        msg = f"UI_PROBE_HEAVY: headline={headline} details={probe}"

        if level == "debug":
            logger.debug(msg)
        elif level == "info":
            logger.info(msg)
        else:
            logger.warning(msg)

    def _norm_title(self,s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip()).casefold()
    
    def find_activity_template_by_title_any_status(self, title: str) -> TemplateMatch | None:
        m = self.find_activity_template_by_title(title, status="active")
        if m:
            return m
        return self.find_activity_template_by_title(title, status="inactive")
    
    def find_activity_template_by_title(self, title: str, status: str = "active", *, max_pages: int = 4) -> TemplateMatch | None:
        """
        Find an Activity Template by exact title match on /activity_templates.

        Stability-first:
        - Uses the built-in search input when available.
        - Proves Turbo updates via staleness of a known element (no sleeps).
        - Falls back to pagination scan if needed.
        """
        logger = self.logger
        target = self._norm_title(title)

        self.go_to_activity_templates(inactive=(status == "inactive"))

        sel = config.TEMPLATES_SELECTORS  # or config.TEMPLATES_SELECTORS
        wait = self.wait  # WebDriverWait instance
        driver = self.driver

        # Sentinel: results frame present
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel["page_sentinel"])))

        # Optional: set results per page = 100 (reduces paging overhead)
        try:
            per_page = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel["per_page_select"])))
            if per_page.get_attribute("value") != "100":
                Select(per_page).select_by_value("100")
            # Changing items triggers a turbo stream update; best-effort prove by waiting for frame to be ready again
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel["page_sentinel"])))
        except Exception as e:
            logger.debug("Could not set results per page (continuing): %s", e)

        # Grab a "before" element to prove update after typing
        before_first_row = None
        rows = driver.find_elements(By.CSS_SELECTOR, sel["rows"])
        if rows:
            before_first_row = rows[0]

        # Type into search (this uses table-search controller)
        search = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel["search_input"])))
        search.clear()
        search.send_keys(title)

        # Prove the list updated after typing (Turbo)
        if before_first_row is not None:
            try:
                WebDriverWait(driver, 6).until(EC.staleness_of(before_first_row))
            except Exception:
                # If staleness didn't happen (sometimes it reuses nodes), we still proceed,
                # but we avoid long waits; we'll scan what is present now.
                pass
        else:
            # No rows beforehand; just wait for either rows to appear or some stable frame presence
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel["page_sentinel"])))

        def scan_current_page() -> TemplateMatch | None:
            rows = driver.find_elements(By.CSS_SELECTOR, sel["rows"])
            for r in rows:
                try:
                    title_a = r.find_element(By.CSS_SELECTOR, sel["row_title_link"])
                    row_title = title_a.text
                    if self._norm_title(row_title) != target:
                        continue

                    href = title_a.get_attribute("href") or ""
                    code = None
                    try:
                        code_a = r.find_element(By.CSS_SELECTOR, sel["row_code_link"])
                        code = (code_a.text or "").strip() or None
                    except Exception:
                        pass

                    # Extract template id from /activity_templates/<id>/activity_revisions
                    template_id = None
                    try:
                        parts = href.split("/activity_templates/")[1].split("/")
                        template_id = parts[0]
                    except Exception:
                        pass

                    return TemplateMatch(title=row_title.strip(), code=code, href=href, template_id=template_id, status=status)
                except Exception:
                    continue
            return None

        # First scan (usually sufficient because search filters globally)
        match = scan_current_page()
        if match:
            return match

        # Fallback: paginate a few pages (only if search doesn’t filter globally / edge cases)
        for _ in range(max_pages - 1):
            next_btn = self._find_templates_next_button(driver)
            if next_btn is None:
                break
            if self._is_disabled(next_btn):
                break

            # Prove update by staleness of a row (or the button itself)
            rows = driver.find_elements(By.CSS_SELECTOR, sel["rows"])
            before = rows[0] if rows else next_btn

            self.click_element_safely(next_btn)  # your safe click

            try:
                WebDriverWait(driver, 6).until(EC.staleness_of(before))
            except Exception:
                pass

            match = scan_current_page()
            if match:
                return match

        return None

    def _find_templates_next_button(self, driver):
        """
        Locate the right-arrow pagination button in the table footer.
        Uses the icon reference '#arrow-right' which is stable in your HTML.
        """
        try:
            return driver.find_element(
                By.CSS_SELECTOR,
                ".table__footer a.btn--icon svg use[xlink\\:href='/icons.svg#arrow-right']"
            )
        except Exception:
            pass

        # More robust: find the <a> that *contains* the arrow-right use element
        try:
            use_el = driver.find_element(By.CSS_SELECTOR, ".table__footer use[xlink\\:href='/icons.svg#arrow-right']")
            return use_el.find_element(By.XPATH, "./ancestor::a[1]")
        except Exception:
            return None

    def _is_disabled(self, a_el) -> bool:
        cls = (a_el.get_attribute("class") or "")
        if "disabled" in cls.split():
            return True
        if a_el.get_attribute("disabled") is not None:
            return True
        aria = a_el.get_attribute("aria-disabled")
        if aria and aria.lower() == "true":
            return True
        return False
    
    def emit_signal(self, cat: Cat, msg: str, **ctx):
        # always allowed
        prefix = f"[{cat}]"
        if self.instr_policy.include_ctx:
            c = format_ctx(**ctx)
            if c:
                msg = f"{msg} :: {c}"
        self.logger.info(f"{prefix} {msg}")

    def emit_diag(self, cat: Cat, msg: str, *, key: str | None = None, every_s: float | None = None, **ctx):
        # gated by mode; DEBUG+ only for now
        if self.instr_policy.mode == LogMode.LIVE:
            return
        if key and every_s:
            if not self._rate.allow(key, every_s):
                return

        prefix = f"[{cat}]"
        if self.instr_policy.include_ctx:
            c = format_ctx(**ctx)
            if c:
                msg = f"{msg} :: {c}"
        self.logger.debug(f"{prefix} {msg}")