# src/ca_bldr/activity_sections.py

import re
import time
from typing import Optional, Iterable

from dataclasses import replace

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException, WebDriverException, NoSuchElementException

from .session import CASession
from .activity_deleter import ActivityDeleter
from .section_handles import SectionHandle
from .activity_registry import ActivityRegistry
from .. import config  # src/config.py

_SECTION_ID_RE = re.compile(r"--(\d+)$")

class ActivitySections:
    """
    Manage sections on the Activity Builder screen.

    Responsibilities:
      - Ensure the Sections sidebar is visible
      - Discover section <li> elements
      - Select sections (by element / title / index / last)
      - Create sections
      - Ensure a 'question-ready' section exists and is selected
      - Delete sections (single or all)
      - Optionally clear fields in protected sections (e.g. 'Introduction')

    This class is separate from:
      - CAActivityBuilder (which focuses on fields *within* the active section)
      - ActivityDeleter (which deletes field elements on the canvas)

    All methods assume an active Activity Builder page and a valid CASession.
    """

    def __init__(
        self,
        session: CASession,
        registry: ActivityRegistry,
        deleter: ActivityDeleter,
    ) -> None:
        self.session = session
        self.driver = session.driver
        self.wait = session.wait
        self.logger = session.logger
        self.deleter = deleter
        self.registry = registry
        self.current_section_handle: Optional[SectionHandle] = None

        self._sections_list_cache = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sections_cache_get(self) -> Optional[list]:
        cached = self._sections_list_cache
        if not cached:
            return None

        ts, items = cached
        ttl = config.SECTIONS_LIST_CACHE_TTL
        if (time.monotonic() - ts) <= ttl:
            return items

        # expired
        self._sections_cache_invalidate(reason="ttl_expired")
        return None

    def _sections_cache_set(self, items: list) -> None:
        self._sections_list_cache = (time.monotonic(), items)

    def _sections_cache_invalidate(self, reason: str = "") -> None:
        if self._sections_list_cache is not None:
            self.logger.debug("Invalidating sections list cache. Reason=%s", reason)
        self._sections_list_cache = None

    def _ensure_sidebar_visible(self, timeout: int = 10) -> bool:
        """
        Ensure the 'Sections' sidebar is visible.

        This is essentially the 'sections' branch of CAActivityBuilder._ensure_sidebar_visible,
        but localised here so that all section UI concerns live in this class.
        """
        driver = self.driver
        wait = self.session.get_wait(timeout)
        logger = self.logger

        sidebars = config.BUILDER_SELECTORS.get("sidebars", {})
        cfg = sidebars.get("sections", {})

        tab_sel = cfg.get("tab")
        frame_sel = cfg.get("frame")

        if not tab_sel:
            logger.error("No tab selector configured for sidebar kind 'sections'.")
            return False

        def _items_present() -> bool:
            try:
                frame = self._get_sections_frame()
                items_sel = config.BUILDER_SELECTORS["sections"]["items"]
                return len(frame.find_elements(By.CSS_SELECTOR, items_sel)) > 0
            except Exception:
                return False

        try:
            # 1. Already visible?
            try:
                tab = driver.find_element(By.CSS_SELECTOR, tab_sel)
                if tab.is_displayed():
                    if not frame_sel:
                        logger.info("Sections sidebar tab is already visible (no frame selector configured).")
                        return True

                    # if frame is visible and items are present, we’re truly good
                    try:
                        frame = driver.find_element(By.CSS_SELECTOR, frame_sel)
                        if frame.is_displayed() and _items_present():
                            logger.info("Sections sidebar already visible and populated.")
                            return True
                    except Exception:
                        pass

                    logger.info("Sections sidebar appears visible but not populated; will try to reopen/nudge.")
            except Exception:
                logger.info("Sections sidebar tab not currently visible; will try to open it.")

            # 2. Click the 'Sections' toggle button
            sections_btn = None

            # a) Try using onclick attribute
            try:
                onclick_sel = cfg.get(
                    "toggle_button_onclick",
                    "button[onclick*='toggleSidebar'][onclick*='sections']",
                )
                logger.info("Looking for 'Sections' button by onclick attribute...")
                sections_btn = driver.find_element(By.CSS_SELECTOR, onclick_sel)
            except Exception:
                logger.info(
                    "No button with toggleSidebar(..., 'sections') found via CSS; "
                    "falling back to text-based button search."
                )
                # b) Fallback: any button whose visible text includes 'Sections'
                candidates = driver.find_elements(By.TAG_NAME, "button")
                for b in candidates:
                    try:
                        text = (b.text or "").strip()
                    except Exception:
                        text = ""
                    if text and "sections" in text.lower():
                        sections_btn = b
                        logger.info(f"Found 'Sections' button by text: '{text}'")
                        break

            if sections_btn is None:
                logger.error("Could not find any 'Sections' toggle button.")
                return False

            clicked = False
            if hasattr(self.session, "click_element_safely"):
                clicked = self.session.click_element_safely(sections_btn)
            if not clicked:
                self.driver.execute_script("arguments[0].click();", sections_btn)

            # 3. Wait for the tab to be visible
            def tab_visible(_):
                try:
                    el = self.driver.find_element(By.CSS_SELECTOR, tab_sel)
                    return el.is_displayed()
                except Exception:
                    return False

            wait.until(tab_visible)
            logger.info("Sections sidebar tab is now visible.")

            # 4. If a frame is configured, wait for it
            if frame_sel:
                def frame_ready(_):
                    try:
                        frame = self.driver.find_element(By.CSS_SELECTOR, frame_sel)
                        return frame.is_displayed()
                    except Exception:
                        return False

                wait.until(frame_ready)
                logger.info("Sections sidebar frame '%s' is loaded.", frame_sel)

            return True

        except TimeoutException as e:
            logger.error("Timed out ensuring Sections sidebar visibility: %s", e)
            return False
        except WebDriverException as e:
            logger.error("WebDriver error ensuring Sections sidebar visibility: %s", e)
            return False
        except Exception as e:
            logger.error("Unexpected error ensuring Sections sidebar visibility: %s", e)
            return False

    def _get_sections_frame(self):
        """
        Return the turbo-frame element that contains the sections list.
        """
        driver = self.driver
        sel = "turbo-frame#designer_sections"
        try:
            frame = driver.find_element(By.CSS_SELECTOR, sel)
            # touch it once to catch staleness early
            _ = frame.get_attribute("id")
            return frame
        except StaleElementReferenceException:
            frame = driver.find_element(By.CSS_SELECTOR, sel)
            return frame
        except Exception as e:
            self.logger.warning("Could not locate designer_sections frame: %s", e)
            raise

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @property
    def current_section_id(self) -> str:
        return (self.current_section_handle.section_id if self.current_section_handle else "") or ""

    def list(self):
        """
        Return a list of section <li> elements under the #sections-list,
        excluding any fixed header entries (e.g. 'Information').

        Turbo-safe principle:
        - Use a very short cache window to avoid repeated DOM scans.
        - If a stale error occurs, invalidate cache and retry once.
        """
        logger = self.logger

        cached = self._sections_cache_get()
        if cached is not None:
            return cached

        def _fetch() -> list:
            # Prefer not to toggle sidebar if already visible
            try:
                if self._is_sections_sidebar_visible():
                    pass
                else:
                    if not self._ensure_sidebar_visible():
                        logger.warning("Sections sidebar not visible; returning empty list.")
                        return []
            except Exception:
                # fallback to original behaviour
                if not self._ensure_sidebar_visible():
                    logger.warning("Sections sidebar not visible; returning empty list.")
                    return []

            frame = self._get_sections_frame()
            items_sel = config.BUILDER_SELECTORS["sections"]["items"]

            sections = frame.find_elements(By.CSS_SELECTOR, items_sel)
            logger.info("Found %d editable section(s) in the Sections sidebar.", len(sections))
            return sections

        try:
            sections = _fetch()
        except (StaleElementReferenceException, WebDriverException) as e:
            logger.warning("Stale/WebDriver while listing sections; retrying once: %s", e)
            # invalidate cache and retry once
            self._sections_cache_invalidate(reason="stale_fetch")
            try:
                sections = _fetch()
            except Exception:
                return []
        except Exception as e:
            logger.warning("Unexpected error while listing sections: %s", e)
            return []

        # store cache
        self._sections_cache_set(sections)
        return sections

    # keep compatibility name if you like
    get_sections = list

    def _is_sections_sidebar_visible(self) -> bool:
        """
        True if the sidebar is open and the 'sections' tab is currently shown.
        Cheap check: no clicks, no waits.
        """
        driver = self.driver

        try:
            # Root sidebar container should exist when any sidebar is open.
            sidebar_list = driver.find_elements(By.CSS_SELECTOR, "div.designer__sidebar")
            if not sidebar_list:
                return False

            sidebar = sidebar_list[0]
            if not sidebar.is_displayed():
                return False

            # The sections panel is explicitly a tab with data-type="sections"
            tab_list = sidebar.find_elements(By.CSS_SELECTOR, ".designer__sidebar__tab[data-type='sections']")
            if not tab_list:
                return False

            tab = tab_list[0]

            # If it’s displayed, we’re done. (Selenium respects display:none.)
            if tab.is_displayed():
                return True

            # Extra belt: some layouts may keep it “display:block” but still not interactable.
            style = (tab.get_attribute("style") or "").lower()
            return "display: block" in style

        except (StaleElementReferenceException, WebDriverException):
            return False
        except Exception:
            return False

    def get_title(self, section_el) -> str:
        """
        Best-effort retrieval of the visible section title from a <li> element.
        """
        try:
            title_el = section_el.find_element(
                By.CSS_SELECTOR,
                ".designer__sidebar__item__title",
            )
            text = (title_el.text or "").strip()
            if text:
                return text
        except Exception:
            pass

        # Fallback to the li text, first line only
        raw = (section_el.text or "").strip()
        if not raw:
            return ""
        return raw.splitlines()[0].strip()

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _select(self, section_el) -> str | None:
        """
        Low-level helper: click the sidebar button inside a <li> and wait for
        create_field_path to reflect the section.
        
        Returns section_id or None.
        """
        driver = self.driver
        wait = self.wait
        logger = self.logger

        # section_el is the <li id="designer__sidebar__item--<id>">
        li_id = section_el.get_attribute("id") or ""
        section_id = None
        m = re.search(r"designer__sidebar__item--(\d+)", li_id)
        if m:
            section_id = m.group(1)

        try:
            link = section_el.find_element(
                By.CSS_SELECTOR,
                ".designer__sidebar__item__link",
            )
        except Exception as e:
            logger.warning(f"Could not find section link to click: {e}")
            return None

        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});",
                link,
            )
            clicked = False
            if hasattr(self.session, "click_element_safely"):
                clicked = self.session.click_element_safely(link)
            if not clicked:
                driver.execute_script("arguments[0].click();", link)

            logger.info("Section selected via sidebar; waiting for canvas to load...")

            if section_id:
                def canvas_for_section_loaded(_):
                    try:
                        inp = driver.find_element(By.CSS_SELECTOR, "input#create_field_path")
                        val = inp.get_attribute("value") or ""
                        return f"/sections/{section_id}/fields" in val
                    except Exception:
                        return False

                wait.until(canvas_for_section_loaded)
                logger.info("Canvas updated for section %s.", section_id)
            else:
                logger.info("No section_id parsed; skipping create_field_path check.")

            return section_id

        except (TimeoutException, WebDriverException) as e:
            logger.warning(f"WebDriver error while selecting section: {e}")
            return None
        except Exception as e:
            logger.warning(f"Unexpected error while selecting section: {e}")
            return None

    def _select_from_current_handle(self) -> bool:
        """
        Refresh selection to current stored handle.

        Returns True if selection succeeded (or is already effectively selected),
        False otherwise.

        Turbo-safe principle:
        - If we can prove canvas alignment for current section, we skip sidebar work.
        - Otherwise we retry sidebar selection and fall back to id/title.
        """
        logger = self.logger

        handle = self.current_section_handle
        if handle is None or not handle.section_id:
            logger.warning(
                "_select_from_current_handle called but current_section_handle is not set or has no section_id."
            )
            return False

        # ✅ Fast-path: if canvas is already aligned for current section, don't touch sidebar.
        # This avoids a lot of Turbo-stale churn.
        try:
            if self.wait_for_canvas_for_current_section(timeout=3):
                logger.debug(
                    "_select_from_current_handle fast-path: canvas already aligned for section id=%s title=%r.",
                    handle.section_id,
                    handle.title,
                )
                return True
        except Exception:
            # If alignment check itself fails, fall through to robust selection.
            pass

        # Try a couple of times to account for sidebar re-renders
        for attempt in range(1, 3):
            try:
                ch = self.select_by_handle(handle)
                if ch:
                    logger.info(
                        "Section id=%s, title=%r selected via _select_from_current_handle (attempt %d).",
                        handle.section_id,
                        handle.title,
                        attempt,
                    )
                    return True

                logger.warning(
                    "select_by_handle returned empty for id=%s on attempt %d.",
                    handle.section_id,
                    attempt,
                )

            except StaleElementReferenceException as e:
                logger.warning(
                    "Stale element while selecting current section id=%s on attempt %d: %s",
                    handle.section_id,
                    attempt,
                    e,
                )
            except WebDriverException as e:
                logger.warning(
                    "WebDriver error while selecting current section id=%s on attempt %d: %s",
                    handle.section_id,
                    attempt,
                    e,
                )
            except Exception as e:
                logger.warning(
                    "Unexpected error while selecting current section id=%s on attempt %d: %s",
                    handle.section_id,
                    attempt,
                    e,
                )

        # Fallback: try selecting by id or title from scratch
        logger.info(
            "Falling back to select_by_id / select_by_title for current section id=%s, title=%r.",
            handle.section_id,
            handle.title,
        )

        try:
            if handle.section_id and self.select_by_id(handle.section_id):
                logger.info("Fallback select_by_id succeeded for id=%s.", handle.section_id)
                return True
        except Exception as e:
            logger.warning("Fallback select_by_id raised for id=%s: %s", handle.section_id, e)

        try:
            if handle.title and self.select_by_title(handle.title, exact=True):
                logger.info("Fallback select_by_title succeeded for title=%r.", handle.title)
                return True
        except Exception as e:
            logger.warning("Fallback select_by_title raised for title=%r: %s", handle.title, e)

        logger.error(
            "Failed to select current section id=%s, title=%r after retries and fallbacks.",
            handle.section_id,
            handle.title,
        )
        return False

    def select_by_handle(self, handle: SectionHandle) -> Optional[SectionHandle]:
        """
        Select the section described by this handle and update current_section_handle.

        Returns True on success, False otherwise.
        """
        logger = self.logger

        li = self._find_section_li_for_handle(handle)
        if li is None:
            return None

        section_id = self._select(li)
        if section_id is None:
            return None

        # Rebuild handle from the actual li (title/index might have changed)
        frame = self._get_sections_frame()
        items_sel = config.BUILDER_SELECTORS["sections"]["items"]
        sections = frame.find_elements(By.CSS_SELECTOR, items_sel)
        try:
            index = sections.index(li)
        except ValueError:
            index = handle.index

        resolved_handle = SectionHandle(
            section_id=section_id,
            title=handle.title,
            index=index,
        )
        self.current_section_handle = resolved_handle
        self.registry.add_or_update_section(resolved_handle)

        logger.info(
            "Selected section with id=%s, title=%r, index=%r",
            resolved_handle.section_id,
            resolved_handle.title,
            resolved_handle.index,
        )
        if resolved_handle.section_id == handle.section_id:
            return handle
        return None

    def _find_section_li_for_handle(self, handle: SectionHandle):
        """
        Given a SectionHandle, find the corresponding <li> in the sidebar.
        Prefer section_id when available, otherwise fall back to index/title.
        """
        logger = self.logger

        if not self._ensure_sidebar_visible():
            logger.warning("Sidebar is not visible; cannot find section li.")
            return None

        frame = self._get_sections_frame()
        items_sel = config.BUILDER_SELECTORS["sections"]["items"]

        # Defensive: wait briefly for list to populate
        try:
            self.wait.until(
                lambda d: len(frame.find_elements(By.CSS_SELECTOR, items_sel)) > 0
            )
        except TimeoutException:
            logger.debug(
                "Sections frame present but items not populated yet (items_sel=%r).",
                items_sel,
            )

        # 1) Fast path: use section_id from handle
        if handle.section_id:
            try:
                li = frame.find_element(
                    By.CSS_SELECTOR,
                    f"li#designer__sidebar__item--{handle.section_id}",
                )
                return li
            except NoSuchElementException:
                logger.debug(
                    "No li found for section id %s; falling back to index/title.",
                    handle.section_id,
                )

        # 2) Fallback: use index
        sections = frame.find_elements(By.CSS_SELECTOR, items_sel)
        if handle.index is not None:
            if 0 <= handle.index < len(sections):
                return sections[handle.index]
            else:
                logger.debug(
                    "Handle index %s out of range for sections (0..%d).",
                    handle.index,
                    len(sections) - 1,
                )

        # 3) Fallback: match by title
        if handle.title:
            for li in sections:
                try:
                    # Read title from reflector
                    li_id = li.get_attribute("id") or ""
                    m = _SECTION_ID_RE.search(li_id)
                    section_id = m.group(1) if m else ""
                    if section_id:
                        title_el = li.find_element(
                            By.CSS_SELECTOR,
                            f".section-title-reflector--{section_id}",
                        )
                        txt = (title_el.text or "").strip()
                        if txt == handle.title:
                            return li
                except Exception:
                    continue

        logger.warning(
            "Could not locate li for SectionHandle(id=%s, title=%r, index=%r)",
            handle.section_id,
            handle.title,
            handle.index,
        )
        return None

    def select_by_title(self, title_text: str, exact: bool = True):
        """
        Select a section by its visible title.

        Returns a SectionHandle on success, or None if not found / ambiguous.
        If more than one section shares the same title, no section is selected
        (the caller should then disambiguate via index or id).
        """
        logger = self.logger

        target = (title_text or "").strip()
        if not target:
            logger.warning("Empty section title provided for search.")
            return None

        target_lower = target.lower()
        sections = self.list()
        if not sections:
            logger.warning("No sections available to select by title.")
            return None

        # Collect all matching <li> elements
        matches: list[tuple[int, object]] = []  # (index, li)

        for idx, sec in enumerate(sections):
            name = self.get_title(sec)
            if not name:
                continue

            if exact:
                if name == target:
                    matches.append((idx, sec))
            else:
                if target_lower in name.lower():
                    matches.append((idx, sec))

        if not matches:
            logger.warning(
                "No section found with title %r (exact=%s).", title_text, exact
            )
            return None

        if len(matches) > 1:
            logger.warning(
                "Multiple sections (%d) found with title %r (exact=%s). "
                "Please disambiguate by index or id.",
                len(matches),
                title_text,
                exact,
            )
            # We *could* pick the first, but for automation it's safer to refuse.
            return None

        # Exactly one match: build a handle and delegate to select_by_handle
        idx, li = matches[0]
        handle = self._build_section_handle_from_li(li, index=idx)

        ch = self.select_by_handle(handle)
        if ch:
            # current_section_handle is updated inside select_by_handle
            return self.current_section_handle
        return None

    def select_by_index(self, index: int):
        """
        Select a section by 0-based index in the list returned by list().

        Returns SectionHandle on success, None on failure.
        """
        sections = self.list()
        if not sections:
            self.logger.warning("No sections available to select by index.")
            return None

        if index < 0 or index >= len(sections):
            self.logger.warning(
                "Section index %d out of range (0..%d).", index, len(sections) - 1
            )
            return None

        li = sections[index]
        # Build a handle from this li
        handle = self._build_section_handle_from_li(li, index=index)

        ch =  self.select_by_handle(handle)
        if ch:
            self.logger.info("Selected section index %d.", index)
            # current_section_handle is already updated
            return self.current_section_handle
        return None
    
    def select_by_id(self, section_id: str):
        """
        Select a section by its CloudAssess section_id (e.g. '1706532').

        Returns a SectionHandle on success, or None on failure.
        """
        logger = self.logger
        section_id = (section_id or "").strip()
        if not section_id:
            logger.warning("Empty section_id provided to select_by_id.")
            return None

        handle = SectionHandle(section_id=section_id)

        try:
            ch = self.select_by_handle(handle)
            if not ch:
                logger.warning("select_by_handle succeeded but current_section_handle is None (unexpected).")
                return None            
            logger.info(
                "Selected section by id=%s (title=%r, index=%r).",
                ch.section_id,
                ch.title,
                ch.index,
            )
            return self.current_section_handle
        except Exception as e:
            logger.error("Failed to select section with id=%s. Message: %s", section_id, e)

    def select_last(self):
        """
        Select the last (bottom-most) editable section in the Sections sidebar.

        Returns SectionHandle on success, None on failure.
        """
        sections = self.list()
        if not sections:
            self.logger.warning("No sections available to select.")
            return None

        li = sections[-1]
        index = len(sections) - 1
        handle = self._build_section_handle_from_li(li, index=index)

        ch = self.select_by_handle(handle)
        if ch:
            return self.current_section_handle
        return None

    # --- Hard refresh & resync ---
    def hard_resync_current_section(self, timeout: int = 10) -> bool:
        """
        Heavy-duty 'unstick' helper:
        - Refreshes the page.
        - Waits for the sections sidebar/turbo-frame.
        - Reselects self.current_section_handle via the sidebar.
        - Waits for the canvas to align again.

        Returns True if we end up with the intended section selected,
        False otherwise.
        """
        logger = self.logger
        driver = self.driver
        wait = self.session.get_wait(timeout)  

        self._sections_cache_invalidate(reason="hard_resync")

        handle = self.current_section_handle
        if not handle:
            logger.warning("hard_resync_current_section called but no current_section_handle is set.")
            return False

        is_info_handle = (handle.section_id == "information") or ((handle.title or "").strip().lower() == "information")

        logger.warning(
            "Hard resync: refreshing Activity Builder and re-selecting section id=%s title=%r",
            handle.section_id,
            handle.title,
        )

        def _is_information_url() -> bool:
            try:
                url = (driver.current_url or "").lower()
                return "/sections/information" in url
            except Exception:
                return False

        # 1) Refresh the page
        driver.refresh()
        frame_sel = None
        try:
            # 2) Wait for the sections sidebar to come back
            sections_cfg = config.BUILDER_SELECTORS.get("sidebars", {}).get("sections", {})
            frame_sel = sections_cfg.get("frame", "turbo-frame#designer_sections")

            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, frame_sel)))
        except TimeoutException:
            logger.error(
                "Hard resync: sections sidebar frame %r did not become present after refresh.",
                frame_sel,
            )
            return False

        # 3) Make sure the sections sidebar is visible
        if not self._ensure_sidebar_visible(timeout):
            logger.error("Hard resync: could not ensure sections sidebar is visible after refresh.")
            return False

        # 3b) In Information-only mode, the sections list may legitimately be empty.
        if _is_information_url() or is_info_handle:
            logger.info("Hard resync: detected Information-only section via URL; skipping sections list population wait.")
            # Best-effort: make sure sidebar is visible (already done above), then wait for canvas settle.
            try:
                self.wait_for_canvas_for_current_section(timeout=10)
            except Exception as e:
                logger.warning("Hard resync (information): error while waiting for canvas alignment: %s", e)
            logger.info("Hard resync completed in Information-only mode.")
            return True

        # Otherwise, normal behaviour:
        items_sel = config.BUILDER_SELECTORS["sections"]["items"]

        def _items_or_false():
            try:
                return self._get_sections_frame().find_elements(By.CSS_SELECTOR, items_sel) or False
            except Exception:
                return False

        # We’ll do up to 2 nudges inside the overall timeout window
        nudges = 0
        deadline = time.time() + timeout

        while True:
            remaining = max(0.5, deadline - time.time())
            local_wait = self.session.get_wait(int(min(3, remaining)))  # short waits per iteration
            try:
                items = local_wait.until(lambda d: _items_or_false())
                logger.info("Hard resync: sections list populated (items=%d).", len(items))
                break
            except TimeoutException:
                nudges += 1
                if time.time() >= deadline or nudges > 2:
                    logger.error(
                        "Hard resync: sections frame present but no section items appeared (selector=%r).",
                        items_sel,
                    )
                    try:
                        frame = self._get_sections_frame()
                        snippet = (frame.get_attribute("innerHTML") or "")[:500]
                        logger.debug("Hard resync: sections frame innerHTML (first 500 chars): %r", snippet)
                    except Exception:
                        pass
                    return False

                logger.warning("Hard resync: sections list still empty; nudging sidebar (nudge %d/2).", nudges)

                # nudge: click Sections toggle again (idempotent) and re-wait
                self._ensure_sidebar_visible(timeout=5)
                try:
                    # small micro-wait to allow Turbo render kick-off
                    time.sleep(0.2)
                except Exception:
                    pass        
        # 4) Reselect the section via its handle
        ch = self.select_by_handle(handle)
        if not ch:
            # One forced retry after invalidating cache + re-ensuring sidebar
            self._sections_cache_invalidate(reason="hard_resync select retry")
            self._ensure_sidebar_visible(timeout)
            ch = self.select_by_handle(handle)
            if not ch:
                logger.error(
                    "Hard resync: could not re-select section id=%s title=%r after refresh.",
                    handle.section_id,
                    handle.title,
                )
                return False

        # 5) Wait for the canvas to line up again (best-effort)
        try:
            self.wait_for_canvas_for_current_section(timeout=10)
        except Exception as e:
            logger.warning("Hard resync: error while waiting for canvas alignment: %s", e)

        logger.info(
            "Hard resync completed; current section is id=%s title=%r.",
            ch.section_id,
            ch.title,
        )
        return True

    # ------------------------------------------------------------------
    # Creation / ensuring a section exists
    # ------------------------------------------------------------------
    def create(self, timeout: int = 10):
        """
        Create a new section via the 'Create Section' button and return the
        newly created section <li> element (best effort).
        """
        driver = self.driver
        wait = self.session.get_wait(timeout)
        logger = self.logger

        self._sections_cache_invalidate(reason="create_section")

        if not self._ensure_sidebar_visible(timeout=timeout):
            logger.error("Cannot create section because Sections sidebar is not visible.")
            return None

        frame = self._get_sections_frame()

        # Current sections
        items_sel = config.BUILDER_SELECTORS["sections"]["items"]
        before_sections = frame.find_elements(By.CSS_SELECTOR, items_sel)
        before_count = len(before_sections)
        logger.info("Sections before creation: %d", before_count)

        # Locate 'Create Section' button inside sections tab
        try:
            create_btn = frame.find_element(
                By.CSS_SELECTOR,
                config.BUILDER_SELECTORS["sections"]["create_button"],
            )
            clicked = False
            if hasattr(self.session, "click_element_safely"):
                clicked = self.session.click_element_safely(create_btn)
            if not clicked:
                driver.execute_script("arguments[0].click();", create_btn)
        except Exception as e:
            logger.error("Could not find/click 'Create Section' button: %s", e)
            return None

        # Wait for a new section item to appear
        def new_section_appeared(_):
            try:
                current = frame.find_elements(By.CSS_SELECTOR, items_sel)
                return len(current) > before_count
            except Exception:
                return False

        try:
            wait.until(new_section_appeared)
        except TimeoutException:
            logger.warning("Timed out waiting for new section to appear.")
            # Still attempt to grab current last section
            current = frame.find_elements(By.CSS_SELECTOR, items_sel)
            if not current:
                return None
            new_li = current[-1]
            index = len(current) - 1
            handle = self._build_section_handle_from_li(new_li, index=index)
            self.current_section_handle = handle
            self.registry.add_or_update_section(handle)
            return handle

        # New section exists; pick the last one
        current = frame.find_elements(By.CSS_SELECTOR, items_sel)
        logger.info("Sections after creation: %d", len(current))
        if not current:
            return None

        new_li = current[-1]
        index = len(current) - 1
        handle = self._build_section_handle_from_li(new_li, index=index)

        self.current_section_handle = handle
        logger.info(
            "Created new section with id=%s, title=%r, index=%r",
            handle.section_id,
            handle.title,
            handle.index,
        )
        return handle

    def _build_section_handle_from_li(
        self,
        li_el,
        *,
        index: int | None = None,
    ) -> SectionHandle:
        """
        Build a SectionHandle from a <li id="designer__sidebar__item--<section_id>"> node.
        """
        logger = self.logger

        title = None

        # 1) Section id from the li id
        section_id = ""
        li_id = li_el.get_attribute("id") or ""
        # e.g. "designer__sidebar__item--1706532"
        m = _SECTION_ID_RE.search(li_id)
        if m:
            section_id = m.group(1)

        # 2) Visible title from the reflector <h4>
        try:
            # Prefer the reflector (what learners/assessors see)
            title_el = li_el.find_element(
                By.CSS_SELECTOR,
                ".section-title-reflector--" + section_id
            )
            txt = (title_el.text or "").strip()
            title = txt or None
        except Exception:
            # Fallback: try link text or something else if DOM changes
            try:
                link_el = li_el.find_element(
                    By.CSS_SELECTOR,
                    ".designer__sidebar__item__link"
                )
                txt = (link_el.text or "").strip()
                title = txt or None
            except Exception:
                pass

        handle = SectionHandle(
            section_id=section_id,
            title=title,
            index=index,
        )

        logger.debug(
            "Built SectionHandle from li: id=%s, title=%r, index=%r",
            handle.section_id,
            handle.title,
            handle.index,
        )
        return handle

    def ensure_section_ready(
        self,
        section_title: Optional[str] = None,
        index: Optional[int] = None,
        section_id: Optional[str] = None,
    ) -> Optional[SectionHandle]:
        """
        Ensure there is at least one editable section ready for adding
        question-type fields, and select an appropriate section.

        Key principle:
        - We only skip sidebar/list/selection work if we can PROVE the currently
          active section is correct AND the canvas is aligned.
        - Otherwise we fall back to the existing robust behaviour.
        """
        logger = self.logger

        logger.info(
            "ensure_section_ready called with section_title=%r, index=%r and section_id=%r.",
            section_title,
            index,
            section_id,
        )

        # -----------------------------
        # Special-case: Information
        # -----------------------------
        if section_title and section_title.strip().lower() == "information":
            handle = SectionHandle(
                section_id="information",
                title="Information",
                index=0,
            )
            self.current_section_handle = handle
            self.registry.add_or_update_section(handle)

            # Actively navigate to the Information section
            try:
                url = (self.driver.current_url or "")
                # If we are already in builder, just swap the tail to /sections/information
                # Works for URLs like: .../revisions/<rev_id>/sections/<something>
                if "/sections/" in url:
                    base = url.split("/sections/")[0]
                    info_url = base + "/sections/information"
                    self.driver.get(info_url)
                    # Confirm alignment (bounded, no sleeps)
                    self.wait_for_canvas_for_current_section(timeout=10)
            except Exception as e:
                logger.warning("Failed to navigate to Information section URL: %s", e)

            return handle
        
        # -----------------------------
        # Helpers
        # -----------------------------
        def _title_norm(t: str) -> str:
            return " ".join((t or "").strip().split()).lower()

        def _desired_is_current(handle: Optional[SectionHandle]) -> bool:
            """
            Decide if the current handle satisfies the requested selector.
            (We keep this conservative—if unsure, return False.)
            """
            if handle is None:
                return False

            # If section_id explicitly requested, it's the strongest constraint.
            if section_id is not None:
                return bool(handle.section_id) and str(handle.section_id) == str(section_id)

            # If title requested, match it (exact-ish, normalized)
            if section_title is not None:
                return _title_norm(handle.title or "") == _title_norm(section_title)

            # If index requested, trust it only if present
            if index is not None:
                try:
                    return handle.index == index
                except Exception:
                    return False

            # No specific request → any valid current handle could be acceptable,
            # BUT we should still ensure it's canvas-aligned.
            return True

        def _canvas_aligned(timeout: int = 3) -> bool:
            """
            Gate for fast-path: the canvas must match the current section.
            Uses existing alignment helper; if absent, be conservative.
            """
            try:
                if hasattr(self, "wait_for_canvas_for_current_section"):
                    return bool(self.wait_for_canvas_for_current_section(timeout=timeout))
            except Exception:
                return False
            return False

        def _try_fast_path() -> Optional[SectionHandle]:
            """
            If current section already matches request and canvas is aligned,
            return immediately (no sidebar/list/select).
            """
            current = self.current_section_handle

            if current is None:
                return None
            
            if not _desired_is_current(current):
                return None

            if not _canvas_aligned(timeout=3):
                return None

            # If we’re here: the requested/current section is already active and safe.
            logger.info(
                "Fast-path: current section already selected and canvas aligned (id=%s title=%r index=%r).",
                current.section_id,
                current.title,
                current.index,
            )
            # Ensure registry is up-to-date
            try:
                self.registry.add_or_update_section(current)
            except Exception:
                pass
            return current

        def _create_new() -> Optional[SectionHandle]:
            new_section = self.create()
            if not new_section:
                logger.error("Failed to create a new section; cannot prepare question section.")
                return None

            logger.info(
                "Created new section with id=%s, title=%r, index=%r",
                new_section.section_id,
                new_section.title,
                new_section.index,
            )

            if section_title and section_title.strip():
                logger.info("Renaming new section to %r from spec...", section_title)
                try:
                    self.rename_section(new_section, new_title=section_title)
                except Exception as e:
                    logger.warning("Failed to rename new section: %s", e)

            # After creation/rename, rely on current_section_handle (your existing pattern)
            created_handle = getattr(self, "current_section_handle", None) or new_section
            try:
                self.registry.add_or_update_section(created_handle)
            except Exception:
                pass
            return created_handle

        def _select_and_confirm(handle: Optional[SectionHandle], why: str) -> Optional[SectionHandle]:
            """
            Confirm selection + canvas alignment in one place.
            Use short alignment waits; if not aligned quickly, force a sidebar reselect.
            """
            if handle is None:
                return None

            # 1) Primary selection mechanism
            try:
                ok = self._select_from_current_handle()
            except Exception as e:
                logger.warning("Selection failed (%s): %s", why, e)
                ok = False

            if not ok:
                logger.error(
                    "Could not select section (%s): title=%r id=%s",
                    why,
                    getattr(handle, "title", None),
                    getattr(handle, "section_id", None),
                )
                return None

            # 2) Fast alignment check (avoid paying 10s repeatedly)
            if _canvas_aligned(timeout=3):
                return handle
            logger.warning(
                "Canvas not aligned after selecting section (%s); forcing sidebar reselect: title=%r id=%s",
                why,
                getattr(handle, "title", None),
                getattr(handle, "section_id", None),
            )

            # 3) Force a sidebar reselect (your proven recovery path)
            try:
                if getattr(handle, "section_id", None):
                    self.select_by_id(handle.section_id)
            except Exception as e:
                logger.warning("Sidebar reselect failed (%s): %s", why, e)

            # 4) Confirm again with a slightly longer, still bounded wait
            if _canvas_aligned(timeout=5):
                return handle

            logger.warning(
                "Canvas still not aligned after sidebar reselect (%s): title=%r id=%s",
                why,
                getattr(handle, "title", None),
                getattr(handle, "section_id", None),
            )

            return handle  # best-effort; caller/builder has its own guard too

        # -----------------------------
        # Fast-path attempt
        # -----------------------------
        fast = _try_fast_path()
        if fast is not None:
            return fast

        # -----------------------------
        # List sections (robust path)
        # -----------------------------
        sections = self.list()

        # If none exist: create + select
        if not sections:
            logger.info(
                "No editable sections found (other than 'Information'). Creating a new section..."
            )
            created = _create_new()
            if not created:
                return None

            # Ensure sidebar selection is real + canvas aligned
            selected = _select_and_confirm(created, why="created-first-section")
            if selected is None:
                return None

            logger.info("Created editable section titled: %r", selected.title)
            self.current_section_handle = selected
            return selected

        # -----------------------------
        # Requested selection (title/index/id)
        # -----------------------------
        if section_title is not None or index is not None or section_id is not None:
            # 1) select by title
            if section_title is not None:
                selected = self.select_by_title(section_title, exact=True)
                if selected is not None:
                    logger.info("Section selected by title: %r.", section_title)
                    # Confirm selection/canvas (cheap)
                    confirmed = _select_and_confirm(selected, why="select-by-title")
                    self.current_section_handle = confirmed or selected
                    return self.current_section_handle
                logger.warning(
                    "Requested section title %r not found; will try index / id or create.",
                    section_title,
                )

            # 2) select by index
            if index is not None:
                selected = self.select_by_index(index)
                if selected is not None:
                    logger.info("Section selected by index: %s.", index)
                    confirmed = _select_and_confirm(selected, why="select-by-index")
                    self.current_section_handle = confirmed or selected
                    return self.current_section_handle
                logger.warning(
                    "Requested section index %s not valid; will try id or create.",
                    index,
                )

            # 3) select by id
            if section_id is not None:
                selected = self.select_by_id(section_id)
                if selected is not None:
                    logger.info("Section selected by id: %s.", section_id)
                    confirmed = _select_and_confirm(selected, why="select-by-id")
                    self.current_section_handle = confirmed or selected
                    return self.current_section_handle
                logger.warning(
                    "Requested section id %s not valid; a new section will be created.",
                    section_id,
                )

            # 4) create new
            logger.info("Requested section not found by title/index/id; creating a new section...")
            created = _create_new()
            if not created:
                return None

            selected = _select_and_confirm(created, why="created-requested-section")
            if selected is None:
                return None

            logger.info("Created editable section titled: %r", selected.title)
            self.current_section_handle = selected
            return selected

        # -----------------------------
        # No specific request: select last
        # -----------------------------
        selected = self.select_last()
        if selected is None:
            logger.warning("No last section could be selected; creating a new section instead.")
            created = _create_new()
            if not created:
                return None

            selected2 = _select_and_confirm(created, why="created-fallback-last")
            if selected2 is None:
                return None

            logger.info("Created editable section titled: %r", selected2.title)
            self.current_section_handle = selected2
            return selected2

        # Confirm last selection (cheap)
        confirmed = _select_and_confirm(selected, why="select-last")
        self.current_section_handle = confirmed or selected

        logger.info("Question-ready section is selected and ready for adding fields.")
        return self.current_section_handle

    def rename_section(self, handle: SectionHandle, new_title: str, timeout: int = 10) -> bool:
        """
        Rename an existing section in the Sections sidebar to `new_title`.

        `handle` should be a SectionHandle with a valid section_id.
        Returns True on success, False on failure.
        """
        logger = self.logger
        driver = self.driver

        self._sections_cache_invalidate(reason="rename_section")

        if not handle.section_id:
            logger.warning("Cannot rename section without section_id (handle=%r).", handle)
            return False

        if not self._ensure_sidebar_visible(timeout=timeout):
            logger.error("Cannot rename section because Sections sidebar is not visible.")
            return False

        frame = self._get_sections_frame()

        # 1) Locate the <li> for this section by id
        li_id = f"designer__sidebar__item--{handle.section_id}"
        try:
            li = frame.find_element(By.ID, li_id)
        except Exception as e:
            logger.error("Could not locate section list item with id=%r: %s", li_id, e)
            return False

        # 2) Click the edit (pencil) button to toggle the input visible (best effort)
        try:
            edit_btn = li.find_element(
                By.CSS_SELECTOR,
                ".designer__sidebar__item__actions button.btn.btn-link.btn--icon",
            )
            driver.execute_script("arguments[0].click();", edit_btn)
        except Exception as e:
            logger.warning(
                "Could not click section edit button for id=%s (proceeding anyway): %s",
                handle.section_id,
                e,
            )

        # 3) Find the input inside this <li> (presence is enough)
        input_selector = ".designer__sidebar__section-input input"
        input_el = None
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                input_el = li.find_element(By.CSS_SELECTOR, input_selector)
                break
            except Exception:
                time.sleep(0.2)

        if input_el is None:
            logger.error(
                "Timed out locating section title input for id=%s using selector %r",
                handle.section_id,
                input_selector,
            )
            return False

        # 4) Use JS to set value + trigger Stimulus actions
        try:
            js = """
                const input = arguments[0];
                const newVal = arguments[1];

                // Set the value
                input.value = newVal;

                // keyup -> input-reflector#reflect (updates h4)
                const keyupEvent = new KeyboardEvent('keyup', {bubbles: true});
                input.dispatchEvent(keyupEvent);

                // keydown.enter -> ajax-input-value#sendRequest (saves via PATCH)
                const keydownEvent = new KeyboardEvent('keydown', {key: 'Enter', keyCode: 13, which: 13, bubbles: true});
                input.dispatchEvent(keydownEvent);

                // blur -> ajax-input-value#sendRequest (fallback save)
                const blurEvent = new FocusEvent('blur', {bubbles: true});
                input.dispatchEvent(blurEvent);
            """
            driver.execute_script(js, input_el, new_title)
        except Exception as e:
            logger.error("Failed to set new section title %r for id=%s via JS: %s", new_title, handle.section_id, e)
            return False

        # 5) Try to wait for the reflector <h4> text to update (best effort)
        reflector_class = f"section-title-reflector--{handle.section_id}"
        try:
            reflector = li.find_element(By.CSS_SELECTOR, f"h4.{reflector_class}")
            end_time = time.time() + timeout
            while time.time() < end_time:
                if reflector.text.strip() == new_title.strip():
                    break
                time.sleep(0.2)
            else:
                logger.warning(
                    "Reflector text for id=%s did not update to %r within timeout (current=%r).",
                    handle.section_id,
                    new_title,
                    reflector.text.strip(),
                )
        except Exception as e:
            logger.warning(
                "Could not read reflector text for id=%s after rename (wanted %r): %s",
                handle.section_id,
                new_title,
                e,
            )

        # 6) Update handle + registry
        new_handle = replace(handle, title=new_title)
        self.current_section_handle = new_handle
        self.registry.add_or_update_section(new_handle)

        logger.info("Renamed section id=%s to %r.", new_handle.section_id, new_title)
        return True

    def wait_for_canvas_for_current_section(self, timeout: int = 10) -> bool:
        """
        Wait until the Activity Builder canvas (create_field_path / designer_fields frame)
        is aligned with current_section_handle.section_id.

        Best-effort: logs a warning on timeout but does not raise.
        """
        logger = self.logger
        driver = self.driver

        handle = self.current_section_handle
        if not handle or not handle.section_id:
            logger.warning(
                "wait_for_canvas_for_current_section called but no current_section_handle "
                "or section_id is set."
            )
            return False

        wait = self.session.get_wait(timeout)

        title = (handle.title or "").strip().lower()
        section_id = (handle.section_id or "").strip()

        # --- SPECIAL CASE: Information ---
        if section_id == "information" or title == "information":
            info_fragment = "/sections/information"

            def _canvas_is_information(_):
                try:
                    frame = driver.find_element(By.CSS_SELECTOR, "turbo-frame#designer_fields")
                    src = (frame.get_attribute("src") or "").strip()
                    if info_fragment in src:
                        return True
                except Exception:
                    pass

                try:
                    url = (driver.current_url or "").strip()
                    if info_fragment in url:
                        return True
                except Exception:
                    pass

                return False

            try:
                wait.until(_canvas_is_information)
                logger.info("Canvas now aligned with Information section (wait_for_canvas_for_current_section).")
                return True
            except TimeoutException:
                logger.warning(
                    "Timed out waiting for canvas to align with Information (expected URL containing %r).",
                    info_fragment,
                )
                return False

        # --- NORMAL SECTION CASE ---
        if not section_id:
            logger.warning(
                "Current section handle has no section_id (title=%r); cannot verify canvas alignment.",
                handle.title,
            )
            return False

        def _canvas_matches_section(_):
            try:
                path_el = driver.find_element(By.CSS_SELECTOR, "input#create_field_path")
                path = (path_el.get_attribute("value") or "").strip()
                if f"/sections/{section_id}/fields" in path:
                    return True
            except Exception:
                pass

            try:
                frame = driver.find_element(By.CSS_SELECTOR, "turbo-frame#designer_fields")
                src = (frame.get_attribute("src") or "").strip()
                if src and f"/sections/{section_id}" in src:
                    return True
            except Exception:
                pass

            try:
                url = (driver.current_url or "").strip()
                if f"/sections/{section_id}" in url:
                    return True
            except Exception:
                pass

            return False

        try:
            wait.until(_canvas_matches_section)
            logger.info(
                "Canvas now aligned with section id=%s (wait_for_canvas_for_current_section).",
                section_id,
            )
            return True
        except TimeoutException:
            logger.warning("Timed out waiting for canvas to align with section id=%s.", section_id)
            return False
        
    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    def _delete_section_element(
        self,
        section_el,
        confirm_timeout: int = 10,
    ) -> bool:
        """
        Delete a section via its sidebar <li> element using the 'Delete section'
        button in the .designer__sidebar__item__actions.
        """
        driver = self.driver
        wait = self.wait
        logger = self.logger

        sec_id = section_el.get_attribute("id") or "<no-id>"

        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});",
                section_el,
            )

            actions = section_el.find_element(
                By.CSS_SELECTOR,
                ".designer__sidebar__item__actions",
            )

            delete_link = actions.find_element(
                By.CSS_SELECTOR,
                "a[data-turbo-method='delete']",
            )

            logger.info("Clicking delete control for section %s via JS...", sec_id)
            driver.execute_script("arguments[0].click();", delete_link)

            if hasattr(self.session, "handle_modal_dialogs"):
                try:
                    self.session.handle_modal_dialogs(
                        mode="confirm",
                        timeout=confirm_timeout,
                    )
                except Exception as e:
                    logger.warning(
                        "Error while handling delete-section modal for %s: %s",
                        sec_id,
                        e,
                    )

            def section_gone(_):
                try:
                    section_el.is_displayed()
                    return False
                except Exception:
                    return True

            try:
                wait.until(section_gone)
                logger.info("Section %s deleted (no longer present in DOM).", sec_id)
                return True
            except TimeoutException:
                logger.warning(
                    "Timeout waiting for section %s to disappear after delete.",
                    sec_id,
                )
                return False

        except WebDriverException as e:
            logger.warning("Could not delete section %s: %s", sec_id, e)
            return False
        except Exception as e:
            logger.warning("Unexpected error deleting section %s: %s", sec_id, e)
            return False

    def delete(
        self,
        title: Optional[str] = None,
        index: Optional[int] = None,
        confirm_timeout: int = 10,
    ) -> bool:
        """
        Delete a single section by title *or* by index.
        """
        logger = self.logger

        if title is not None and index is not None:
            raise ValueError("Provide either 'title' or 'index', not both.")
        if title is None and index is None:
            raise ValueError("Must provide either 'title' or 'index'.")

        if title is not None:
            sec_el = self.select_by_title(title)
            if sec_el is None:
                logger.warning("No section found with title %r to delete.", title)
                return False
        else:
            sec_el = self.select_by_index(index or 0)
            if sec_el is None:
                logger.warning("No section found at index %r to delete.", index)
                return False

        return self._delete_section_element(sec_el, confirm_timeout=confirm_timeout)

    def delete_all(
        self,
        *,
        skip_titles: Optional[Iterable[str]] = None,
        clear_skipped_sections: bool = True,
    ) -> dict[str, int]:
        """
        Delete all sections in the Sections sidebar, except those whose titles are in skip_titles.

        If clear_skipped_sections is True, we will:
          - select each skipped section
          - call ActivityDeleter.delete_all_fields() on it to clear its contents

        Returns:
            dict mapping section_title -> number_of_fields_deleted or status code:
              - 0  => section was deleted
              - >0 => section was kept and that many fields were cleared
              - -1 => operation failed for that section
        """
        logger = self.logger
        results: dict[str, int] = {}

        if skip_titles is None:
            skip_titles = {"Introduction"}
        else:
            skip_titles = set(skip_titles)

        if not self._ensure_sidebar_visible():
            logger.error("Sections sidebar not visible; cannot delete sections.")
            return results

        sections = self.list()
        if not sections:
            logger.info("No sections to delete.")
            return results

        # Iterate from bottom to top so indices don't shift undesirably when deleting
        for sec_el in reversed(sections):
            title = self.get_title(sec_el) or "<unnamed>"
            logger.info("Processing section %r.", title)

            if title in skip_titles:
                logger.info("Skipping deletion of protected section %r.", title)
                deleted_count = 0
                if clear_skipped_sections:
                    try:
                        if self._select(sec_el):
                            deleted_count = self.deleter.delete_all_fields()
                            logger.info(
                                "Cleared %d field(s) from protected section %r.",
                                deleted_count,
                                title,
                            )
                    except Exception as e:
                        logger.warning(
                            "Failed to clear fields from protected section %r: %s",
                            title,
                            e,
                        )
                        deleted_count = -1

                results[title] = deleted_count
                continue

            if self._delete_section_element(sec_el):
                results[title] = 0
            else:
                logger.warning("Failed to delete section %r.", title)
                results[title] = -1

        return results

