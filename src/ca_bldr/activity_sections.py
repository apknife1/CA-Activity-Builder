# src/ca_bldr/activity_sections.py

import re
import time
from typing import Optional, Iterable, Any
from contextlib import contextmanager

from dataclasses import replace

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException, WebDriverException, NoSuchElementException

from .session import CASession
from .activity_deleter import ActivityDeleter
from .section_handles import SectionHandle
from .activity_registry import ActivityRegistry
from .instrumentation import Cat, LogMode
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

    @contextmanager
    def _implicit_wait(self, seconds: float):
        driver = self.driver
        driver.implicitly_wait(seconds)
        try:
            yield
        finally:
            driver.implicitly_wait(config.IMPLICIT_WAIT)

    def _section_ctx(self, *, action: str, attempt: str | None = None) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "sec": self.current_section_id or "",
            "kind": action,
        }
        if attempt:
            ctx["a"] = attempt
        return ctx

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sections_cache_get(self) -> Optional[list]:
        cached = self._sections_list_cache
        if not cached:
            self.session.counters.inc("section.cache.miss")
            return None

        ts, items = cached
        ttl = config.SECTIONS_LIST_CACHE_TTL
        if (time.monotonic() - ts) <= ttl:
            self.session.counters.inc("section.cache.hit")
            return items

        # expired
        self.session.counters.inc("section.cache.expired")
        self._sections_cache_invalidate(reason="ttl_expired")
        return None

    def _sections_cache_set(self, items: list) -> None:
        self._sections_list_cache = (time.monotonic(), items)
        self.session.counters.inc("section.cache.set")

    def _sections_cache_invalidate(self, reason: str = "") -> None:
        if self._sections_list_cache is not None:
            self.session.counters.inc("section.cache.invalidate")
            self.session.emit_diag(
                Cat.SECTION,
                "Invalidating sections list cache",
                reason=reason,
                key="SECTION.cache.invalidate",
                every_s=1.0,
                **self._section_ctx(action="cache_invalidate"),
            )
        self._sections_list_cache = None

    def _ensure_sidebar_visible(self, timeout: int = 10) -> bool:
        """
        Ensure the 'Sections' sidebar is visible.

        This is essentially the 'sections' branch of CAActivityBuilder._ensure_sidebar_visible,
        but localised here so that all section UI concerns live in this class.
        """
        driver = self.driver
        wait = self.session.get_wait(timeout)
        ctx = self._section_ctx(action="ensure_sidebar")

        self.session.counters.inc("section.sidebar_ensure_calls")

        sidebars = config.BUILDER_SELECTORS.get("sidebars", {})
        cfg = sidebars.get("sections", {})

        tab_sel = cfg.get("tab")
        frame_sel = cfg.get("frame")

        if not tab_sel:
            self.session.emit_signal(
                Cat.SECTION,
                "No tab selector configured for sidebar kind 'sections'",
                level="error",
                **ctx,
            )
            return False

        def _items_present() -> bool:
            try:
                frame = self._get_sections_frame()
                items_sel = config.BUILDER_SELECTORS["sections"]["items"]
                with self._implicit_wait(0):
                    return len(frame.find_elements(By.CSS_SELECTOR, items_sel)) > 0
            except Exception:
                return False

        try:
            # 1. Already visible?
            try:
                tab = driver.find_element(By.CSS_SELECTOR, tab_sel)
                if tab.is_displayed():
                    self.session.counters.inc("section.sidebar_fastpath_hits")
                    ctx = self._section_ctx(action="ensure_sidebar", attempt="fastpath")
                    self.session.emit_diag(
                        Cat.SECTION,
                        "Sections sidebar already visible",
                        key="SECTION.sidebar.fastpath",
                        every_s=1.0,
                        **ctx,
                    )
                    if not frame_sel:
                        return True

                    try:
                        frame = driver.find_element(By.CSS_SELECTOR, frame_sel)
                        if frame.is_displayed() and _items_present():
                            return True
                    except Exception:
                        pass
                    self.session.emit_diag(
                        Cat.SECTION,
                        "Sections sidebar visible but not populated; will try to reopen/nudge",
                        **self._section_ctx(action="ensure_sidebar", attempt="fastpath_no_items"),
                    )
            except Exception:
                self.session.emit_diag(
                    Cat.SECTION,
                    "Sections sidebar tab not currently visible; will try to open it",
                    **self._section_ctx(action="ensure_sidebar", attempt="open"),
                )

            # 2. Click the 'Sections' toggle button
            sections_btn = None

            # a) Try using onclick attribute
            try:
                onclick_sel = cfg.get(
                    "toggle_button_onclick",
                    "button[onclick*='toggleSidebar'][onclick*='sections']",
                )
                self.session.emit_diag(
                    Cat.SECTION,
                    "Looking for 'Sections' button by onclick selector",
                    method="onclick_selector",
                    **self._section_ctx(action="ensure_sidebar", attempt="find_button"),
                )
                sections_btn = driver.find_element(By.CSS_SELECTOR, onclick_sel)
            except Exception:
                self.session.emit_diag(
                    Cat.SECTION,
                    "No sections toggle via onclick selector; falling back to text search",
                    method="text_scan",
                    **self._section_ctx(action="ensure_sidebar", attempt="find_button"),
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
                        self.session.emit_diag(
                            Cat.SECTION,
                            "Found 'Sections' button by text",
                            text=text,
                            **self._section_ctx(action="ensure_sidebar", attempt="find_button"),
                        )
                        break

            if sections_btn is None:
                self.session.emit_signal(
                    Cat.SECTION,
                    "Could not find any 'Sections' toggle button",
                    level="error",
                    **ctx,
                )
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
            self.session.emit_diag(
                Cat.SECTION,
                "Sections sidebar tab is now visible",
                **self._section_ctx(action="ensure_sidebar", attempt="tab_visible"),
            )

            # 4. If a frame is configured, wait for it
            if frame_sel:
                def frame_ready(_):
                    try:
                        frame = self.driver.find_element(By.CSS_SELECTOR, frame_sel)
                        return frame.is_displayed()
                    except Exception:
                        return False

                wait.until(frame_ready)
                self.session.emit_diag(
                    Cat.SECTION,
                    "Sections sidebar frame is loaded",
                    frame_sel=frame_sel,
                    **self._section_ctx(action="ensure_sidebar", attempt="frame_ready"),
                )

            return True

        except TimeoutException as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Timed out ensuring Sections sidebar visibility",
                exception=str(e),
                level="error",
                **ctx,
            )
            return False
        except WebDriverException as e:
            self.session.emit_signal(
                Cat.SECTION,
                "WebDriver error ensuring Sections sidebar visibility",
                exception=str(e),
                level="error",
                **ctx,
            )
            return False
        except Exception as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Unexpected error ensuring Sections sidebar visibility",
                exception=str(e),
                level="error",
                **ctx,
            )
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
            self.session.emit_signal(
                Cat.SECTION,
                "Could not locate designer_sections frame",
                exception=str(e),
                level="warning",
                **self._section_ctx(action="get_frame"),
            )
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
        cached = self._sections_cache_get()
        if cached is not None:
            return cached

        def _fetch() -> list:
            # Prefer not to toggle sidebar if already visible
            frame = None
            try:
                if self._is_sections_sidebar_visible():
                    try:
                        with self._implicit_wait(0):
                            frame = self._get_sections_frame()
                    except Exception:
                        frame = None
                else:
                    frame = None
            except Exception:
                frame = None

            if frame is None:
                # fallback to original behaviour
                if not self._ensure_sidebar_visible():
                    self.session.emit_signal(
                        Cat.SECTION,
                        "Sections sidebar not visible; returning empty list",
                        level="warning",
                        **self._section_ctx(action="list"),
                    )
                    return []
                frame = self._get_sections_frame()

            items_sel = config.BUILDER_SELECTORS["sections"]["items"]

            with self._implicit_wait(0):
                sections = frame.find_elements(By.CSS_SELECTOR, items_sel)
            self.session.emit_diag(
                Cat.SECTION,
                "Found editable sections in sidebar",
                count=len(sections),
                **self._section_ctx(action="list"),
            )
            return sections

        try:
            sections = _fetch()
        except (StaleElementReferenceException, WebDriverException) as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Stale/WebDriver while listing sections; retrying once",
                exception=str(e),
                level="warning",
                **self._section_ctx(action="list_retry"),
            )
            # invalidate cache and retry once
            self._sections_cache_invalidate(reason="stale_fetch")
            try:
                sections = _fetch()
            except Exception:
                return []
        except Exception as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Unexpected error while listing sections",
                exception=str(e),
                level="warning",
                **self._section_ctx(action="list"),
            )
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
            with self._implicit_wait(0):
                sidebar_list = driver.find_elements(By.CSS_SELECTOR, "div.designer__sidebar")
            if not sidebar_list:
                return False

            sidebar = sidebar_list[0]
            if not sidebar.is_displayed():
                return False

            # The sections panel is explicitly a tab with data-type="sections"
            with self._implicit_wait(0):
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
            with self._implicit_wait(0):
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
        ctx = self._section_ctx(action="select")

        # section_el is the <li id="designer__sidebar__item--<id>">
        li_id = section_el.get_attribute("id") or ""
        section_id = None
        m = re.search(r"designer__sidebar__item--(\d+)", li_id)
        if m:
            section_id = m.group(1)

        def _resolve_link(root_el):
            return root_el.find_element(
                By.CSS_SELECTOR,
                ".designer__sidebar__item__link",
            )

        try:
            link = _resolve_link(section_el)
        except Exception as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Could not find section link to click",
                exception=str(e),
                level="warning",
                **ctx,
            )
            return None

        try:
            # One stale-retry with refind by li id.
            for attempt in range(1, 3):
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
                    break
                except (StaleElementReferenceException, WebDriverException) as e:
                    stale = isinstance(e, StaleElementReferenceException) or ("stale element reference" in str(e).lower())
                    if not stale or attempt >= 2:
                        raise
                    if li_id:
                        section_el = driver.find_element(By.CSS_SELECTOR, f"li[id='{li_id}']")
                    link = _resolve_link(section_el)

            self.session.emit_diag(
                Cat.SECTION,
                "Section selected via sidebar; waiting for canvas to load",
                **ctx,
            )

            if section_id:
                def canvas_for_section_loaded(_):
                    try:
                        inp = driver.find_element(By.CSS_SELECTOR, "input#create_field_path")
                        val = inp.get_attribute("value") or ""
                        return f"/sections/{section_id}/fields" in val
                    except Exception:
                        return False

                wait.until(canvas_for_section_loaded)
                self.session.emit_diag(
                    Cat.SECTION,
                    "Canvas updated for section",
                    section_id=section_id,
                    **ctx,
                )
            else:
                self.session.emit_diag(
                    Cat.SECTION,
                    "No section_id parsed; skipping create_field_path check",
                    **ctx,
                )

            return section_id

        except (TimeoutException, WebDriverException) as e:
            self.session.emit_signal(
                Cat.SECTION,
                "WebDriver error while selecting section",
                exception=str(e),
                level="warning",
                **ctx,
            )
            return None
        except Exception as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Unexpected error while selecting section",
                exception=str(e),
                level="warning",
                **ctx,
            )
            return None

    def _select_from_current_handle(self) -> tuple[bool, bool]:
        """
        Refresh selection to current stored handle.

        Returns (selection_ok, canvas_aligned).
        canvas_aligned is True only when we can prove alignment.

        Turbo-safe principle:
        - If we can prove canvas alignment for current section, we skip sidebar work.
        - Otherwise we retry sidebar selection and fall back to id/title.
        """
        ctx = self._section_ctx(action="select_current")

        handle = self.current_section_handle
        if handle is None or not handle.section_id:
            self.session.emit_signal(
                Cat.SECTION,
                "_select_from_current_handle called but current_section_handle is not set or has no section_id",
                level="warning",
                **ctx,
            )
            return False, False

        # ✅ Fast-path: if canvas is already aligned for current section, don't touch sidebar.
        # This avoids a lot of Turbo-stale churn.
        try:
            if self.wait_for_canvas_for_current_section(timeout=3):
                self.session.counters.inc("section.fastpath_hits")
                self.session.emit_diag(
                    Cat.SECTION,
                    "Fast-path: canvas already aligned for current section",
                    section_id=handle.section_id,
                    section_title=handle.title,
                    key="SECTION.canvas.fastpath",
                    every_s=1.0,
                    **ctx,
                )
                return True, True
        except Exception:
            # If alignment check itself fails, fall through to robust selection.
            pass

        # Try a couple of times to account for sidebar re-renders
        for attempt in range(1, 3):
            try:
                ch = self.select_by_handle(handle)
                if ch:
                    self.session.emit_diag(
                        Cat.SECTION,
                        "Selected current section via handle",
                        section_id=handle.section_id,
                        section_title=handle.title,
                        attempt=attempt,
                        **ctx,
                    )
                    return True, False

                self.session.emit_signal(
                    Cat.SECTION,
                    "select_by_handle returned empty",
                    section_id=handle.section_id,
                    attempt=attempt,
                    level="warning",
                    **ctx,
                )

            except StaleElementReferenceException as e:
                self.session.emit_signal(
                    Cat.SECTION,
                    "Stale element while selecting current section",
                    section_id=handle.section_id,
                    attempt=attempt,
                    exception=str(e),
                    level="warning",
                    **ctx,
                )
            except WebDriverException as e:
                self.session.emit_signal(
                    Cat.SECTION,
                    "WebDriver error while selecting current section",
                    section_id=handle.section_id,
                    attempt=attempt,
                    exception=str(e),
                    level="warning",
                    **ctx,
                )
            except Exception as e:
                self.session.emit_signal(
                    Cat.SECTION,
                    "Unexpected error while selecting current section",
                    section_id=handle.section_id,
                    attempt=attempt,
                    exception=str(e),
                    level="warning",
                    **ctx,
                )

        # Fallback: try selecting by id or title from scratch
        self.session.emit_signal(
            Cat.SECTION,
            "Falling back to select_by_id / select_by_title for current section",
            section_id=handle.section_id,
            section_title=handle.title,
            level="warning",
            **ctx,
        )

        try:
            if handle.section_id and self.select_by_id(handle.section_id):
                self.session.emit_diag(
                    Cat.SECTION,
                    "Fallback select_by_id succeeded",
                    section_id=handle.section_id,
                    **ctx,
                )
                return True, False
        except Exception as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Fallback select_by_id raised",
                section_id=handle.section_id,
                exception=str(e),
                level="warning",
                **ctx,
            )

        try:
            if handle.title and self.select_by_title(handle.title, exact=True):
                self.session.emit_diag(
                    Cat.SECTION,
                    "Fallback select_by_title succeeded",
                    section_title=handle.title,
                    **ctx,
                )
                return True, False
        except Exception as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Fallback select_by_title raised",
                section_title=handle.title,
                exception=str(e),
                level="warning",
                **ctx,
            )

        self.session.emit_signal(
            Cat.SECTION,
            "Failed to select current section after retries and fallbacks",
            section_id=handle.section_id,
            section_title=handle.title,
            level="error",
            **ctx,
        )
        return False, False

    def select_by_handle(self, handle: SectionHandle) -> Optional[SectionHandle]:
        """
        Select the section described by this handle and update current_section_handle.

        Returns True on success, False otherwise.
        """
        ctx = self._section_ctx(action="select_by_handle")

        last_err: Exception | None = None

        for attempt in range(1, 3):
            li = self._find_section_li_for_handle(handle)
            if li is None:
                return None

            try:
                section_id = self._select(li)
            except StaleElementReferenceException as e:
                last_err = e
                self.session.emit_signal(
                    Cat.SECTION,
                    "Stale element while selecting section by handle",
                    section_id=handle.section_id,
                    attempt=attempt,
                    exception=str(e),
                    level="warning",
                    **ctx,
                )
                continue
            except Exception as e:
                last_err = e
                self.session.emit_signal(
                    Cat.SECTION,
                    "Unexpected error while selecting section by handle",
                    section_id=handle.section_id,
                    attempt=attempt,
                    exception=str(e),
                    level="warning",
                    **ctx,
                )
                return None

            if section_id is None:
                return None

            # Rebuild handle from the actual li (title/index might have changed)
            index = handle.index
            try:
                frame = self._get_sections_frame()
                items_sel = config.BUILDER_SELECTORS["sections"]["items"]
                sections = frame.find_elements(By.CSS_SELECTOR, items_sel)

                li_fresh = None
                if handle.section_id:
                    try:
                        li_fresh = frame.find_element(
                            By.CSS_SELECTOR,
                            f"li#designer__sidebar__item--{handle.section_id}",
                        )
                    except Exception:
                        li_fresh = None

                if li_fresh is not None:
                    try:
                        index = sections.index(li_fresh)
                    except ValueError:
                        index = handle.index
                else:
                    try:
                        index = sections.index(li)
                    except ValueError:
                        index = handle.index
            except Exception:
                index = handle.index

            resolved_handle = SectionHandle(
                section_id=section_id,
                title=handle.title,
                index=index,
            )
            self.current_section_handle = resolved_handle
            self.registry.add_or_update_section(resolved_handle)

            self.session.emit_diag(
                Cat.SECTION,
                "Selected section with handle",
                section_id=resolved_handle.section_id,
                section_title=resolved_handle.title,
                section_index=resolved_handle.index,
                **ctx,
            )
            return resolved_handle

        if last_err is not None:
            self.session.emit_signal(
                Cat.SECTION,
                "Failed to select section by handle after retries",
                section_id=handle.section_id,
                exception=str(last_err),
                level="warning",
                **ctx,
            )
        return None

    def _find_section_li_for_handle(self, handle: SectionHandle):
        """
        Given a SectionHandle, find the corresponding <li> in the sidebar.
        Prefer section_id when available, otherwise fall back to index/title.
        """
        ctx = self._section_ctx(action="find_li")

        if not self._ensure_sidebar_visible():
            self.session.emit_signal(
                Cat.SECTION,
                "Sidebar is not visible; cannot find section li",
                level="warning",
                **ctx,
            )
            return None

        frame = self._get_sections_frame()
        items_sel = config.BUILDER_SELECTORS["sections"]["items"]

        # Defensive: wait briefly for list to populate
        try:
            self.wait.until(
                lambda d: len(frame.find_elements(By.CSS_SELECTOR, items_sel)) > 0
            )
        except TimeoutException:
            self.session.emit_diag(
                Cat.SECTION,
                "Sections frame present but items not populated yet",
                items_sel=items_sel,
                **ctx,
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
                self.session.emit_diag(
                    Cat.SECTION,
                    "No li found for section id; falling back to index/title",
                    section_id=handle.section_id,
                    **ctx,
                )

        # 2) Fallback: use index
        sections = frame.find_elements(By.CSS_SELECTOR, items_sel)
        if handle.index is not None:
            if 0 <= handle.index < len(sections):
                return sections[handle.index]
            else:
                self.session.emit_diag(
                    Cat.SECTION,
                    "Handle index out of range for sections",
                    section_index=handle.index,
                    max_index=len(sections) - 1,
                    **ctx,
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

        self.session.emit_signal(
            Cat.SECTION,
            "Could not locate li for SectionHandle",
            section_id=handle.section_id,
            section_title=handle.title,
            section_index=handle.index,
            level="warning",
            **ctx,
        )
        return None

    def select_by_title(self, title_text: str, exact: bool = True):
        """
        Select a section by its visible title.

        Returns a SectionHandle on success, or None if not found / ambiguous.
        If more than one section shares the same title, no section is selected
        (the caller should then disambiguate via index or id).
        """
        ctx = self._section_ctx(action="select_by_title")

        target = (title_text or "").strip()
        if not target:
            self.session.emit_signal(
                Cat.SECTION,
                "Empty section title provided for search",
                level="warning",
                **ctx,
            )
            return None

        target_lower = target.lower()
        sections = self.list()
        if not sections:
            self.session.emit_signal(
                Cat.SECTION,
                "No sections available to select by title",
                level="warning",
                **ctx,
            )
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
            self.session.emit_signal(
                Cat.SECTION,
                "No section found with title",
                title=title_text,
                exact=exact,
                level="warning",
                **ctx,
            )
            return None

        if len(matches) > 1:
            self.session.emit_signal(
                Cat.SECTION,
                "Multiple sections found with title; please disambiguate",
                count=len(matches),
                title=title_text,
                exact=exact,
                level="warning",
                **ctx,
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
        ctx = self._section_ctx(action="select_by_index")
        sections = self.list()
        if not sections:
            self.session.emit_signal(
                Cat.SECTION,
                "No sections available to select by index",
                level="warning",
                **ctx,
            )
            return None

        if index < 0 or index >= len(sections):
            self.session.emit_signal(
                Cat.SECTION,
                "Section index out of range",
                index=index,
                max_index=len(sections) - 1,
                level="warning",
                **ctx,
            )
            return None

        li = sections[index]
        # Build a handle from this li
        handle = self._build_section_handle_from_li(li, index=index)

        ch =  self.select_by_handle(handle)
        if ch:
            self.session.emit_diag(
                Cat.SECTION,
                "Selected section by index",
                index=index,
                **ctx,
            )
            # current_section_handle is already updated
            return self.current_section_handle
        return None
    
    def select_by_id(self, section_id: str):
        """
        Select a section by its CloudAssess section_id (e.g. '1706532').

        Returns a SectionHandle on success, or None on failure.
        """
        ctx = self._section_ctx(action="select_by_id")
        section_id = (section_id or "").strip()
        if not section_id:
            self.session.emit_signal(
                Cat.SECTION,
                "Empty section_id provided to select_by_id",
                level="warning",
                **ctx,
            )
            return None

        handle = SectionHandle(section_id=section_id)

        try:
            ch = self.select_by_handle(handle)
            if ch:
                self.session.emit_diag(
                    Cat.SECTION,
                    "Selected section by id",
                    section_id=ch.section_id,
                    section_title=ch.title,
                    section_index=ch.index,
                    **ctx,
                )
                return self.current_section_handle

            current = self.current_section_handle
            if current and current.section_id == section_id:
                self.session.emit_diag(
                    Cat.SECTION,
                    "Selected section by id (current handle already set)",
                    section_id=current.section_id,
                    section_title=current.title,
                    section_index=current.index,
                    **ctx,
                )
                return current

            self.session.emit_signal(
                Cat.SECTION,
                "select_by_id failed to confirm selection",
                section_id=section_id,
                level="warning",
                **ctx,
            )
            return None
        except Exception as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Failed to select section by id",
                section_id=section_id,
                exception=str(e),
                level="error",
                **ctx,
            )

    def select_last(self):
        """
        Select the last (bottom-most) editable section in the Sections sidebar.

        Returns SectionHandle on success, None on failure.
        """
        sections = self.list()
        if not sections:
            self.session.emit_signal(
                Cat.SECTION,
                "No sections available to select last",
                level="warning",
                **self._section_ctx(action="select_last"),
            )
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
        start_ts = time.monotonic()
        driver = self.driver
        wait = self.session.get_wait(timeout)
        ctx = self._section_ctx(action="hard_resync")

        self.session.counters.inc("section.hard_resyncs")

        self._sections_cache_invalidate(reason="hard_resync")

        handle = self.current_section_handle
        if not handle:
            self.session.emit_signal(
                Cat.SECTION,
                "hard_resync_current_section called but no current_section_handle is set",
                level="warning",
                **ctx,
            )
            return False

        is_info_handle = (handle.section_id == "information") or ((handle.title or "").strip().lower() == "information")

        self.session.emit_signal(
            Cat.SECTION,
            "Hard resync: refreshing Activity Builder and re-selecting section",
            section_id=handle.section_id,
            section_title=handle.title,
            level="warning",
            **ctx,
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
            self.session.emit_signal(
                Cat.SECTION,
                "Hard resync: sections sidebar frame did not become present after refresh",
                frame_sel=frame_sel,
                level="error",
                **ctx,
            )
            return False

        # 3) Make sure the sections sidebar is visible
        if not self._ensure_sidebar_visible(timeout):
            self.session.emit_signal(
                Cat.SECTION,
                "Hard resync: could not ensure sections sidebar is visible after refresh",
                level="error",
                **ctx,
            )
            return False

        # 3b) In Information-only mode, the sections list may legitimately be empty.
        if _is_information_url() or is_info_handle:
            self.session.emit_diag(
                Cat.SECTION,
                "Hard resync: detected Information-only section; skipping sections list population wait",
                **ctx,
            )
            # Best-effort: make sure sidebar is visible (already done above), then wait for canvas settle.
            try:
                self.wait_for_canvas_for_current_section(timeout=10)
            except Exception as e:
                self.session.emit_signal(
                    Cat.SECTION,
                    "Hard resync (information): error while waiting for canvas alignment",
                    exception=str(e),
                    level="warning",
                    **ctx,
                )
            self.session.emit_signal(
                Cat.SECTION,
                "Hard resync completed in Information-only mode",
                elapsed_s=round(time.monotonic() - start_ts, 2),
                **ctx,
            )
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
                self.session.emit_diag(
                    Cat.SECTION,
                    "Hard resync: sections list populated",
                    items=len(items),
                    **ctx,
                )
                break
            except TimeoutException:
                nudges += 1
                if time.time() >= deadline or nudges > 2:
                    self.session.emit_signal(
                        Cat.SECTION,
                        "Hard resync: sections frame present but no section items appeared",
                        items_sel=items_sel,
                        level="error",
                        **ctx,
                    )
                    try:
                        frame = self._get_sections_frame()
                        self.session.counters.inc("trace.sections_frame_html_dumps")
                        if self.session.instr_policy.mode == LogMode.TRACE:
                            snippet = (frame.get_attribute("innerHTML") or "")[:500]
                            self.session.emit_trace(
                                Cat.SECTION,
                                "Hard resync: sections frame innerHTML (first 500 chars)",
                                snippet=snippet,
                                **ctx,
                            )
                    except Exception:
                        pass
                    return False

                self.session.emit_signal(
                    Cat.SECTION,
                    "Hard resync: sections list still empty; nudging sidebar",
                    nudge=nudges,
                    level="warning",
                    **ctx,
                )

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
                self.session.emit_signal(
                    Cat.SECTION,
                    "Hard resync: could not re-select section after refresh",
                    section_id=handle.section_id,
                    section_title=handle.title,
                    level="error",
                    **ctx,
                )
                return False

        # 5) Wait for the canvas to line up again (best-effort)
        try:
            self.wait_for_canvas_for_current_section(timeout=10)
        except Exception as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Hard resync: error while waiting for canvas alignment",
                exception=str(e),
                level="warning",
                **ctx,
            )

        self.session.emit_signal(
            Cat.SECTION,
            "Hard resync completed",
            section_id=ch.section_id,
            section_title=ch.title,
            elapsed_s=round(time.monotonic() - start_ts, 2),
            **ctx,
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
        ctx = self._section_ctx(action="create")

        self._sections_cache_invalidate(reason="create_section")

        if not self._ensure_sidebar_visible(timeout=timeout):
            self.session.emit_signal(
                Cat.SECTION,
                "Cannot create section because Sections sidebar is not visible",
                level="error",
                **ctx,
            )
            return None

        frame = self._get_sections_frame()

        # Current sections
        items_sel = config.BUILDER_SELECTORS["sections"]["items"]
        def _list_section_items_now() -> list:
            try:
                frame_now = self._get_sections_frame()
                with self._implicit_wait(0):
                    return frame_now.find_elements(By.CSS_SELECTOR, items_sel)
            except Exception:
                return []

        before_sections = _list_section_items_now()
        before_count = len(before_sections)
        self.session.emit_diag(
            Cat.SECTION,
            "Sections before creation",
            count=before_count,
            **ctx,
        )

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
            self.session.emit_signal(
                Cat.SECTION,
                "Could not find/click 'Create Section' button",
                exception=str(e),
                level="error",
                **ctx,
            )
            return None

        # Wait for a new section item to appear
        def new_section_appeared(_):
            try:
                current = _list_section_items_now()
                return len(current) > before_count
            except Exception:
                return False

        try:
            wait.until(new_section_appeared)
        except TimeoutException:
            self.session.emit_signal(
                Cat.SECTION,
                "Timed out waiting for new section to appear",
                level="warning",
                **ctx,
            )
            # Still attempt to grab current last section
            current = _list_section_items_now()
            if not current:
                return None
            new_li = current[-1]
            index = len(current) - 1
            handle = self._build_section_handle_from_li(new_li, index=index)
            self.current_section_handle = handle
            self.registry.add_or_update_section(handle)
            return handle

        # New section exists; pick the last one
        current = _list_section_items_now()
        self.session.emit_diag(
            Cat.SECTION,
            "Sections after creation",
            count=len(current),
            **ctx,
        )
        if not current:
            return None

        new_li = current[-1]
        index = len(current) - 1
        handle = self._build_section_handle_from_li(new_li, index=index)

        self.current_section_handle = handle
        self.session.emit_signal(
            Cat.SECTION,
            "Created new section",
            section_id=handle.section_id,
            section_title=handle.title,
            section_index=handle.index,
            **ctx,
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
        ctx = self._section_ctx(action="build_handle")

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

        self.session.emit_diag(
            Cat.SECTION,
            "Built SectionHandle from li",
            section_id=handle.section_id,
            section_title=handle.title,
            section_index=handle.index,
            **ctx,
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
        ctx = self._section_ctx(action="ensure_ready")

        self.session.emit_diag(
            Cat.SECTION,
            "ensure_section_ready called",
            section_title=section_title,
            section_index=index,
            section_id=section_id,
            **ctx,
        )

        # -----------------------------
        # Special-case: Information
        # -----------------------------
        if section_title and section_title.strip().lower() == "information":
            current = self.current_section_handle
            if current and (
                current.section_id == "information"
                or (current.title or "").strip().lower() == "information"
            ):
                try:
                    if self.wait_for_canvas_for_current_section(timeout=3):
                        self.session.emit_diag(
                            Cat.SECTION,
                            "Fast-path: Information section already active and aligned",
                            section_id=current.section_id,
                            section_title=current.title,
                            **ctx,
                        )
                        try:
                            self.registry.add_or_update_section(current)
                        except Exception:
                            pass
                        return current
                except Exception:
                    pass

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
                self.session.emit_signal(
                    Cat.SECTION,
                    "Failed to navigate to Information section URL",
                    exception=str(e),
                    level="warning",
                    **ctx,
                )

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
                self.session.counters.inc("section.fastpath_bypass.no_current")
                return None
            
            if not _desired_is_current(current):
                self.session.counters.inc("section.fastpath_bypass.mismatch")
                return None

            if not _canvas_aligned(timeout=3):
                self.session.counters.inc("section.fastpath_bypass.not_aligned")
                return None

            # If we’re here: the requested/current section is already active and safe.
            self.session.emit_diag(
                Cat.SECTION,
                "Fast-path: current section already selected and canvas aligned",
                section_id=current.section_id,
                section_title=current.title,
                section_index=current.index,
                **ctx,
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
                self.session.emit_signal(
                    Cat.SECTION,
                    "Failed to create a new section; cannot prepare question section",
                    level="error",
                    **ctx,
                )
                return None

            self.session.emit_diag(
                Cat.SECTION,
                "Created new section (ensure_section_ready)",
                section_id=new_section.section_id,
                section_title=new_section.title,
                section_index=new_section.index,
                **ctx,
            )

            if section_title and section_title.strip():
                self.session.emit_diag(
                    Cat.SECTION,
                    "Renaming new section from spec",
                    new_title=section_title,
                    **ctx,
                )
                try:
                    self.rename_section(new_section, new_title=section_title)
                except Exception as e:
                    self.session.emit_signal(
                        Cat.SECTION,
                        "Failed to rename new section",
                        exception=str(e),
                        level="warning",
                        **ctx,
                    )

            # After creation/rename, rely on current_section_handle (your existing pattern)
            created_handle = getattr(self, "current_section_handle", None) or new_section
            # Alignment is confirmed by _select_and_confirm() at call sites.

            try:
                self.registry.add_or_update_section(created_handle)
            except Exception:
                pass
            return created_handle

        def _select_and_confirm(
            handle: Optional[SectionHandle],
            why: str,
            *,
            skip_initial_align_probe: bool = False,
        ) -> Optional[SectionHandle]:
            """
            Confirm selection + canvas alignment in one place.
            Use short alignment waits; if not aligned quickly, force a sidebar reselect.
            """
            if handle is None:
                return None

            # 1) Primary selection mechanism
            if skip_initial_align_probe:
                # Freshly-created sections often aren't alignable until we select by id once.
                # Skip the pre-probe path that can spend ~3s timing out before this click.
                try:
                    if getattr(handle, "section_id", None):
                        ok = self.select_by_id(handle.section_id) is not None
                    else:
                        ok = self.select_by_handle(handle) is not None
                    aligned = False
                except Exception as e:
                    self.session.emit_signal(
                        Cat.SECTION,
                        "Selection failed",
                        reason=why,
                        exception=str(e),
                        level="warning",
                        **ctx,
                    )
                    ok = False
                    aligned = False
            else:
                try:
                    ok, aligned = self._select_from_current_handle()
                except Exception as e:
                    self.session.emit_signal(
                        Cat.SECTION,
                        "Selection failed",
                        reason=why,
                        exception=str(e),
                        level="warning",
                        **ctx,
                    )
                    ok = False
                    aligned = False

            if not ok:
                self.session.emit_signal(
                    Cat.SECTION,
                    "Could not select section",
                    reason=why,
                    section_title=getattr(handle, "title", None),
                    section_id=getattr(handle, "section_id", None),
                    level="error",
                    **ctx,
                )
                return None

            # 2) Fast alignment check (avoid paying 10s repeatedly)
            if aligned or _canvas_aligned(timeout=3):
                return handle
            self.session.emit_signal(
                Cat.SECTION,
                "Canvas not aligned after selecting section; forcing sidebar reselect",
                reason=why,
                section_title=getattr(handle, "title", None),
                section_id=getattr(handle, "section_id", None),
                level="warning",
                **ctx,
            )

            # 3) Force a sidebar reselect (your proven recovery path)
            try:
                if getattr(handle, "section_id", None):
                    self.select_by_id(handle.section_id)
            except Exception as e:
                self.session.emit_signal(
                    Cat.SECTION,
                    "Sidebar reselect failed",
                    reason=why,
                    exception=str(e),
                    level="warning",
                    **ctx,
                )

            # 4) Confirm again with a slightly longer, still bounded wait
            if _canvas_aligned(timeout=5):
                return handle

            self.session.emit_signal(
                Cat.SECTION,
                "Canvas still not aligned after sidebar reselect",
                reason=why,
                section_title=getattr(handle, "title", None),
                section_id=getattr(handle, "section_id", None),
                level="warning",
                **ctx,
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
            self.session.emit_signal(
                Cat.SECTION,
                "No editable sections found; creating a new section",
                level="warning",
                **ctx,
            )
            created = _create_new()
            if not created:
                return None

            # Ensure sidebar selection is real + canvas aligned
            selected = _select_and_confirm(
                created,
                why="created-first-section",
                skip_initial_align_probe=True,
            )
            if selected is None:
                return None

            self.session.emit_diag(
                Cat.SECTION,
                "Created editable section",
                section_title=selected.title,
                **ctx,
            )
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
                    self.session.emit_diag(
                        Cat.SECTION,
                        "Section selected by title",
                        section_title=section_title,
                        **ctx,
                    )
                    # Confirm selection/canvas (cheap)
                    confirmed = _select_and_confirm(selected, why="select-by-title")
                    self.current_section_handle = confirmed or selected
                    return self.current_section_handle
                self.session.emit_signal(
                    Cat.SECTION,
                    "Requested section title not found; will try index/id or create",
                    section_title=section_title,
                    level="warning",
                    **ctx,
                )

            # 2) select by index
            if index is not None:
                selected = self.select_by_index(index)
                if selected is not None:
                    self.session.emit_diag(
                        Cat.SECTION,
                        "Section selected by index",
                        section_index=index,
                        **ctx,
                    )
                    confirmed = _select_and_confirm(selected, why="select-by-index")
                    self.current_section_handle = confirmed or selected
                    return self.current_section_handle
                self.session.emit_signal(
                    Cat.SECTION,
                    "Requested section index not valid; will try id or create",
                    section_index=index,
                    level="warning",
                    **ctx,
                )

            # 3) select by id
            if section_id is not None:
                selected = self.select_by_id(section_id)
                if selected is not None:
                    self.session.emit_diag(
                        Cat.SECTION,
                        "Section selected by id",
                        section_id=section_id,
                        **ctx,
                    )
                    confirmed = _select_and_confirm(selected, why="select-by-id")
                    self.current_section_handle = confirmed or selected
                    return self.current_section_handle
                self.session.emit_signal(
                    Cat.SECTION,
                    "Requested section id not valid; a new section will be created",
                    section_id=section_id,
                    level="warning",
                    **ctx,
                )

            # 4) create new
            self.session.emit_signal(
                Cat.SECTION,
                "Requested section not found; creating a new section",
                level="warning",
                **ctx,
            )
            created = _create_new()
            if not created:
                return None

            selected = _select_and_confirm(
                created,
                why="created-requested-section",
                skip_initial_align_probe=True,
            )
            if selected is None:
                return None

            self.session.emit_diag(
                Cat.SECTION,
                "Created editable section",
                section_title=selected.title,
                **ctx,
            )
            self.current_section_handle = selected
            return selected

        # -----------------------------
        # No specific request: select last
        # -----------------------------
        selected = self.select_last()
        if selected is None:
            self.session.emit_signal(
                Cat.SECTION,
                "No last section could be selected; creating a new section instead",
                level="warning",
                **ctx,
            )
            created = _create_new()
            if not created:
                return None

            selected2 = _select_and_confirm(
                created,
                why="created-fallback-last",
                skip_initial_align_probe=True,
            )
            if selected2 is None:
                return None

            self.session.emit_diag(
                Cat.SECTION,
                "Created editable section",
                section_title=selected2.title,
                **ctx,
            )
            self.current_section_handle = selected2
            return selected2

        # Confirm last selection (cheap)
        confirmed = _select_and_confirm(selected, why="select-last")
        self.current_section_handle = confirmed or selected

        self.session.emit_diag(
            Cat.SECTION,
            "Question-ready section is selected and ready for adding fields",
            **ctx,
        )
        return self.current_section_handle

    def rename_section(self, handle: SectionHandle, new_title: str, timeout: int = 10) -> bool:
        """
        Rename an existing section in the Sections sidebar to `new_title`.

        `handle` should be a SectionHandle with a valid section_id.
        Returns True on success, False on failure.
        """
        driver = self.driver
        ctx = self._section_ctx(action="rename")

        self._sections_cache_invalidate(reason="rename_section")

        if not handle.section_id:
            self.session.emit_signal(
                Cat.SECTION,
                "Cannot rename section without section_id",
                handle=handle,
                level="warning",
                **ctx,
            )
            return False

        if not self._ensure_sidebar_visible(timeout=timeout):
            self.session.emit_signal(
                Cat.SECTION,
                "Cannot rename section because Sections sidebar is not visible",
                level="error",
                **ctx,
            )
            return False

        frame = self._get_sections_frame()

        # 1) Locate the <li> for this section by id
        li_id = f"designer__sidebar__item--{handle.section_id}"
        try:
            li = frame.find_element(By.ID, li_id)
        except Exception as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Could not locate section list item",
                section_id=li_id,
                exception=str(e),
                level="error",
                **ctx,
            )
            return False

        # 2) Click the edit (pencil) button to toggle the input visible (best effort)
        try:
            edit_btn = li.find_element(
                By.CSS_SELECTOR,
                ".designer__sidebar__item__actions button.btn.btn-link.btn--icon",
            )
            driver.execute_script("arguments[0].click();", edit_btn)
        except Exception as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Could not click section edit button (proceeding anyway)",
                section_id=handle.section_id,
                exception=str(e),
                level="warning",
                **ctx,
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
            self.session.emit_signal(
                Cat.SECTION,
                "Timed out locating section title input",
                section_id=handle.section_id,
                input_selector=input_selector,
                level="error",
                **ctx,
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
            self.session.emit_signal(
                Cat.SECTION,
                "Failed to set new section title via JS",
                new_title=new_title,
                section_id=handle.section_id,
                exception=str(e),
                level="error",
                **ctx,
            )
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
                self.session.emit_signal(
                    Cat.SECTION,
                    "Reflector text did not update within timeout",
                    section_id=handle.section_id,
                    new_title=new_title,
                    current_text=reflector.text.strip(),
                    level="warning",
                    **ctx,
                )
        except Exception as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Could not read reflector text after rename",
                section_id=handle.section_id,
                new_title=new_title,
                exception=str(e),
                level="warning",
                **ctx,
            )

        # 6) Update handle + registry
        new_handle = replace(handle, title=new_title)
        self.current_section_handle = new_handle
        self.registry.add_or_update_section(new_handle)

        self.session.emit_signal(
            Cat.SECTION,
            "Renamed section",
            section_id=new_handle.section_id,
            new_title=new_title,
            **ctx,
        )
        return True

    def is_canvas_aligned_with_current_section(self) -> bool:
        """
        Fast, non-blocking alignment probe. Returns True only when alignment can be proven.
        """
        driver = self.driver
        handle = self.current_section_handle
        if not handle or not handle.section_id:
            return False

        title = (handle.title or "").strip().lower()
        section_id = (handle.section_id or "").strip()

        with self._implicit_wait(0):
            # Information special-case
            if section_id == "information" or title == "information":
                info_fragment = "/sections/information"
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

            # Normal sections: check create_field_path or designer_fields frame src
            try:
                create_field_path = driver.find_element(
                    By.CSS_SELECTOR, "input#create_field_path"
                ).get_attribute("value") or ""
                if f"/sections/{section_id}/fields" in create_field_path:
                    return True
            except Exception:
                pass

            try:
                frame = driver.find_element(By.CSS_SELECTOR, "turbo-frame#designer_fields")
                src = (frame.get_attribute("src") or "").strip()
                if f"/sections/{section_id}" in src:
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

    def wait_for_canvas_for_current_section(self, timeout: int = 10) -> bool:
        """
        Wait until the Activity Builder canvas (create_field_path / designer_fields frame)
        is aligned with current_section_handle.section_id.

        Best-effort: logs a warning on timeout but does not raise.
        """
        ctx = self._section_ctx(action="canvas_align")

        self.session.counters.inc("section.canvas_align_checks")

        handle = self.current_section_handle
        if not handle or not handle.section_id:
            self.session.emit_signal(
                Cat.SECTION,
                "wait_for_canvas_for_current_section called but no current_section_handle or section_id is set",
                level="warning",
                **ctx,
            )
            return False
    
        wait = self.session.get_wait(timeout)

        title = (handle.title or "").strip().lower()
        section_id = (handle.section_id or "").strip()

        if not section_id:
            self.session.emit_signal(
                Cat.SECTION,
                "Current section handle has no section_id; cannot verify canvas alignment",
                section_title=handle.title,
                level="warning",
                **ctx,
            )
            return False

        def _canvas_matches_section(_):
            return self.is_canvas_aligned_with_current_section()

        try:
            wait.until(_canvas_matches_section)
            if section_id == "information" or title == "information":
                self.session.emit_diag(
                    Cat.SECTION,
                    "Canvas now aligned with Information section",
                    key="SECTION.canvas.aligned.info",
                    every_s=1.0,
                    **ctx,
                )
            else:
                self.session.emit_diag(
                    Cat.SECTION,
                    "Canvas now aligned with section",
                    section_id=section_id,
                    key="SECTION.canvas.aligned",
                    every_s=1.0,
                    **ctx,
                )
            return True
        except TimeoutException:
            self.session.emit_signal(
                Cat.SECTION,
                "Timed out waiting for canvas to align with section",
                section_id=section_id,
                level="warning",
                **ctx,
            )
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
        ctx = self._section_ctx(action="delete_section")

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

            self.session.emit_diag(
                Cat.SECTION,
                "Clicking delete control for section via JS",
                section_id=sec_id,
                **ctx,
            )
            driver.execute_script("arguments[0].click();", delete_link)

            if hasattr(self.session, "handle_modal_dialogs"):
                try:
                    self.session.handle_modal_dialogs(
                        mode="confirm",
                        timeout=confirm_timeout,
                    )
                except Exception as e:
                    self.session.emit_signal(
                        Cat.SECTION,
                        "Error while handling delete-section modal",
                        section_id=sec_id,
                        exception=str(e),
                        level="warning",
                        **ctx,
                    )

            def section_gone(_):
                try:
                    section_el.is_displayed()
                    return False
                except Exception:
                    return True

            try:
                wait.until(section_gone)
                self.session.emit_diag(
                    Cat.SECTION,
                    "Section deleted (no longer present in DOM)",
                    section_id=sec_id,
                    **ctx,
                )
                return True
            except TimeoutException:
                self.session.emit_signal(
                    Cat.SECTION,
                    "Timeout waiting for section to disappear after delete",
                    section_id=sec_id,
                    level="warning",
                    **ctx,
                )
                return False

        except WebDriverException as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Could not delete section",
                section_id=sec_id,
                exception=str(e),
                level="warning",
                **ctx,
            )
            return False
        except Exception as e:
            self.session.emit_signal(
                Cat.SECTION,
                "Unexpected error deleting section",
                section_id=sec_id,
                exception=str(e),
                level="warning",
                **ctx,
            )
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
        ctx = self._section_ctx(action="delete")

        if title is not None and index is not None:
            raise ValueError("Provide either 'title' or 'index', not both.")
        if title is None and index is None:
            raise ValueError("Must provide either 'title' or 'index'.")

        if title is not None:
            sec_el = self.select_by_title(title)
            if sec_el is None:
                self.session.emit_signal(
                    Cat.SECTION,
                    "No section found with title to delete",
                    title=title,
                    level="warning",
                    **ctx,
                )
                return False
        else:
            sec_el = self.select_by_index(index or 0)
            if sec_el is None:
                self.session.emit_signal(
                    Cat.SECTION,
                    "No section found at index to delete",
                    index=index,
                    level="warning",
                    **ctx,
                )
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
        ctx = self._section_ctx(action="delete_all")
        results: dict[str, int] = {}

        if skip_titles is None:
            skip_titles = {"Introduction"}
        else:
            skip_titles = set(skip_titles)

        if not self._ensure_sidebar_visible():
            self.session.emit_signal(
                Cat.SECTION,
                "Sections sidebar not visible; cannot delete sections",
                level="error",
                **ctx,
            )
            return results

        sections = self.list()
        if not sections:
            self.session.emit_diag(
                Cat.SECTION,
                "No sections to delete",
                **ctx,
            )
            return results

        # Iterate from bottom to top so indices don't shift undesirably when deleting
        for sec_el in reversed(sections):
            title = self.get_title(sec_el) or "<unnamed>"
            self.session.emit_diag(
                Cat.SECTION,
                "Processing section",
                section_title=title,
                **ctx,
            )

            if title in skip_titles:
                self.session.emit_diag(
                    Cat.SECTION,
                    "Skipping deletion of protected section",
                    section_title=title,
                    **ctx,
                )
                deleted_count = 0
                if clear_skipped_sections:
                    try:
                        if self._select(sec_el):
                            deleted_count = self.deleter.delete_all_fields()
                            self.session.emit_diag(
                                Cat.SECTION,
                                "Cleared fields from protected section",
                                section_title=title,
                                deleted_count=deleted_count,
                                **ctx,
                            )
                    except Exception as e:
                        self.session.emit_signal(
                            Cat.SECTION,
                            "Failed to clear fields from protected section",
                            section_title=title,
                            exception=str(e),
                            level="warning",
                            **ctx,
                        )
                        deleted_count = -1

                results[title] = deleted_count
                continue

            if self._delete_section_element(sec_el):
                results[title] = 0
            else:
                self.session.emit_signal(
                    Cat.SECTION,
                    "Failed to delete section",
                    section_title=title,
                    level="warning",
                    **ctx,
                )
                results[title] = -1

        return results
