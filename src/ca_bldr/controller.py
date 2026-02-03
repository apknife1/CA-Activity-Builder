# controller.py
from __future__ import annotations

from typing import List, Any, Optional, Set
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from collections import defaultdict
import json
import logging
import random

from tkinter import Tk
from tkinter.filedialog import askopenfilenames, askdirectory

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from src.ca_bldr.field_configs import TableConfig

from .errors import TableResizeError, FieldPropertiesSidebarTimeout
from .spec_reader import ActivityInstruction
from .context import AppContext
from .config_builder import build_field_config
from .instruction_dump import dump_activity_instruction_json
from .timing import phase_timer
from .types import FailureRecord
from .failures import make_failure_record
from .. import config as config


@dataclass
class _RetryContext:
    builder: Any
    editor: Any
    sections: Any
    deleter: Any
    session: Any
    logger: Any


@dataclass
class FaultPlan:
    add_fail_fi_index: Optional[int] = None
    properties_fail_fi_index: Optional[int] = None
    configure_fail_fi_index: Optional[int] = None


class ActivityBuildController:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.logger = ctx.logger
        self.session = ctx.session
        self.builder = ctx.builder
        self.editor = ctx.editor
        self.sections = ctx.sections
        self.registry = ctx.registry
        self.reader = ctx.reader  # you said you’ve added this to AppContext
        self.deleter = ctx.deleter

    def control_process(self) -> None:
        """
        landing point from src/main.py
        
        from here all other processes are run
        """
        logger = self.logger
        session = self.session
        reader = self.reader

        # 1. Go to Activity Templates landing page
        session.go_to_activity_templates()

        run_dir = self._init_run_dir()
        self._attach_run_file_logger(run_dir)
        logger.info("Run output dir: %s", run_dir.as_posix())

        # 2. Get path to activity template yaml
        with phase_timer(logger, "Spec selection + parse"):
            spec_paths = self._get_spec_paths()
            logger.info("Selected %d spec file(s).", len(spec_paths))

        self._update_run_meta(
            run_dir,
            spec_paths=[str(p) for p in spec_paths],
            spec_count=len(spec_paths),
        )

        all_activities: list[ActivityInstruction] = []

        # 3. Read ActivityInstruction list from YAML
        for spec_path in spec_paths:
            self.logger.info("Reading activity spec from %s", spec_path)
            acts = reader.read_path(spec_path)
            if not acts:
                self.logger.warning("No activities found in spec %r", spec_path)
                continue
            all_activities.extend(acts)

        if not all_activities:
            self.logger.error("No activities found in selected spec(s).")
            return
        
        activities = all_activities
        logger.info("Total activities to build: %r", len(activities))

        self._update_run_meta(
            run_dir,
            activity_count=len(activities),
        )

        # # 4.For now, let you manually start the process
        # print("\nActivity template created.")
        # input(
        #     "➡ In the browser window, make sure that you are on the Activity Templates page.\n"
        #     "➡ Once the correct page is loaded, press Enter here to continue..."
        # )

        # 5. Iterate through Activities
        for act in activities:
            with phase_timer(logger, f"Activity {act.activity_code} full build"):

                title_val = getattr(act, "activity_title", "") or ""
                code_val  = getattr(act, "activity_code", "") or ""

                logger.info(
                    "Preparing to create activity template for code=%r, title=%r",
                    title_val,
                    code_val,
                )

                with phase_timer(logger, f"{act.activity_code}: locate existing template"):
                    match = session.find_activity_template_by_title_any_status(title_val)

                if match:
                    logger.warning(
                        "Skipping build: template already exists (%s). title=%r code=%r template_id=%r href=%s",
                        match.status,
                        match.title,
                        getattr(act, "activity_code", None),
                        match.template_id,
                        match.href,
                    )
                    if match.status == "inactive":
                        logger.warning(
                            "Note: inactive templates may be editable unless assigned; assigned templates are locked and require a new revision."
                        )
                    continue

                else:
                    # No match found → create new
                    # 5.1 Create the new activity template via offcanvas
                    with phase_timer(logger, f"{act.activity_code}: create template"):
                        created = self._create_activity_from_instruction(act)
                        if not created:
                            self.logger.error(
                                "Aborting build: failed to create activity template for code=%r.",
                                getattr(act, "activity_code", None),
                            )
                            return
                    
                    # 5.2 open the builder page
                    with phase_timer(logger, f"{act.activity_code}: open Activity Builder"):
                        if not self._open_activity_builder_for_new_activity():
                            logger.error(
                                "Aborting: could not open Activity Builder for code=%r.",
                                getattr(act, "activity_code", None),                    
                            )
                            return

                    # 5.3 Run the build loop for this single activity
                    ok = self._build_from_instruction(act, run_dir=run_dir)
                    if not ok:
                        logger.error(
                            "Aborting: build failed critically for activity code=%r.",
                            getattr(act, "activity_code", None),
                        )
                        return

        input(
            "\nCheck the Activity Builder page:\n"
            "- Does the activity have all of the designated sections?\n"
            "- Does it have all of the appropriate fields in each section?\n"
            "When you've checked, press Enter here to close the browser..."
        )

    def _get_spec_path(self) -> str:
        """
        Let the user pick a YAML spec from src/specs.
        Returns a path string like "src/specs/example_wa.yml".
        """
        specs_dir = Path("src/specs")
        if not specs_dir.exists():
            raise FileNotFoundError(f"Specs folder not found: {specs_dir.resolve()}")

        # List YAML files (sorted)
        files = sorted(
            [p for p in specs_dir.iterdir() if p.is_file() and p.suffix.lower() in {".yml", ".yaml"}],
            key=lambda p: p.name.lower(),
        )

        if not files:
            raise FileNotFoundError(f"No .yml/.yaml files found in {specs_dir.resolve()}")

        print("\nSelect a spec to run:")
        for i, p in enumerate(files, start=1):
            print(f"  {i:>2}) {p.name}")

        print("\nEnter a number, or paste a path. Press Enter for 1.")
        while True:
            choice = input("> ").strip()

            if choice == "":
                return str(files[0].as_posix())

            # Number selection
            if choice.isdigit():
                idx = int(choice)
                if 1 <= idx <= len(files):
                    return str(files[idx - 1].as_posix())
                print(f"Invalid selection. Choose 1..{len(files)}.")
                continue

            # Path selection (allow drag/drop quotes)
            raw = choice.strip("\"' ")
            p = Path(raw)
            if p.exists() and p.is_file() and p.suffix.lower() in {".yml", ".yaml"}:
                return str(p.as_posix())

            print("Not a valid selection. Enter a number from the list, or a valid .yml/.yaml path.")

    def _init_run_dir(self) -> Path:
        """
        Create a per-run output folder under ./runs/<timestamp>/ with subfolders.
        Returns the run_dir Path.
        """
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = Path("runs") / run_id
        (run_dir / "activities").mkdir(parents=True, exist_ok=True)
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)

        meta = {
            "run_id": run_id,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "spec_paths": [],
            "spec_count": 0,
            "activity_count": 0,
            "notes": "",
        }
        (run_dir / "run_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return run_dir


    def _update_run_meta(self, run_dir: Path, **updates) -> None:
        meta_path = run_dir / "run_meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

        meta.update(updates)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


    def _dump_json(self, path: Path, payload) -> None:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


    def _create_activity_from_instruction(self, act) -> bool:
        """
        Use the 'Create Activity' offcanvas to create a new template.

        Uses:
          - act.activity_title
          - act.activity_code

        Returns True on success, False if something obviously failed.
        """
        driver = self.session.driver
        wait   = self.session.wait
        logger = self.logger

        # 1. Ensure that we are on the correct page
        self.session.go_to_activity_templates()

        # 2. Click "Create Activity" button on the Activity Templates landing page
        try:
            create_btn = wait.until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "a.btn.btn-primary[href='/activity_templates/new']")
                )
            )
        except TimeoutException:
            logger.error("Could not find 'Create Activity' button on Activity Templates page.")
            return False

        if not self.session.click_element_safely(create_btn):
            create_btn.click()

        # 3. Wait for offcanvas form to appear
        try:
            form = wait.until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, "form#new_template"))
            )
        except TimeoutException:
            logger.error("Create Activity offcanvas form did not appear.")
            return False

        # 4. Fill Activity Title
        try:
            title_input = form.find_element(By.CSS_SELECTOR, "input#activity_title")
        except Exception as e:
            logger.error("Could not locate activity title input: %s", e)
            return False

        title_val = getattr(act, "activity_title", "") or ""
        self._set_text_input(title_input, title_val)

        # 5. Ensure 'Design from scratch' is selected (default, but let's be explicit)
        try:
            scratch_radio = form.find_element(
                By.CSS_SELECTOR,
                "input[type='radio'][name='content_creation_option'][value='scratch']",
            )
            if not scratch_radio.is_selected():
                driver.execute_script("arguments[0].click();", scratch_radio)
        except Exception as e:
            logger.warning("Could not enforce 'Design from scratch' option: %s", e)

        # 6. Set Category to 'Rpl'
        self._select_category(form)

        # 7. Set Activity Code
        try:
            code_input = form.find_element(
                By.CSS_SELECTOR, "input#activity_template_code__input"
            )
        except Exception as e:
            logger.error("Could not locate activity code input: %s", e)
            return False

        code_val = getattr(act, "activity_code", "") or ""
        self._set_text_input(code_input, code_val)

        # 8. Click 'Create'
        try:
            create_btn = form.find_element(
                By.CSS_SELECTOR,
                "button[form='new_template'][type='submit'][data-validate-form-target='submitButton']",
            )
        except Exception as e:
            logger.error("Could not locate 'Create' button in offcanvas: %s", e)
            return False

        if not self.session.click_element_safely(create_btn):
            create_btn.click()

        # 9. Wait for offcanvas to disappear (best-effort)
        try:
            wait.until(EC.invisibility_of_element(form))
        except TimeoutException:
            # Not fatal; CA sometimes just hides it via classes.
            logger.warning(
                "Create Activity offcanvas did not fully disappear within timeout; "
                "continuing anyway."
            )

        logger.info(
            "Created activity template for code=%r title=%r.",
            code_val,
            title_val,
        )
        return True

    def _set_text_input(self, element, value: str) -> None:
        """
        Set a text input's value via JS and dispatch input/change events so
        Stimulus/validation sees the change.
        """
        driver = self.session.driver
        js = """
            const el = arguments[0];
            const val = arguments[1];
            el.value = val;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        """
        driver.execute_script(js, element, value)

    def _select_category(
        self,
        form,
        category_label: str = "Rpl",
    ) -> None:
        """
        Set the Category dropdown by updating the hidden <select id="category_id">
        and firing a change event so the Choices controller picks it up.

        category_label:
            Visible text of the category (e.g. "Rpl"). Defaults to "Rpl".
        """
        driver = self.session.driver
        logger = self.logger

        try:
            select_el = form.find_element(By.CSS_SELECTOR, "select#category_id")
        except Exception as e:
            logger.warning("Could not locate hidden category <select>: %s", e)
            return

        # Enumerate options and try to find by label
        try:
            options = select_el.find_elements(By.TAG_NAME, "option")
        except Exception as e:
            logger.warning("Could not enumerate category <option> elements: %s", e)
            return

        # Normalise: lowercase + strip + remove all spaces
        def _norm(s: str) -> str:
            return "".join((s or "").lower().split())

        norm_label = _norm(category_label)
        target_value = None

        for opt in options:
            try:
                text_norm = _norm(opt.text)
            except Exception:
                continue

            if text_norm == norm_label:
                target_value = opt.get_attribute("value")
                break

        # Fallback: if we're trying to set "Rpl" and didn't find it by label,
        # use the known value from the DOM: <option value="184">Rpl</option>
        if not target_value and norm_label == "rpl":
            target_value = "184"
            logger.info(
                "Falling back to hard-coded category value %r for label %r.",
                target_value,
                category_label,
            )

        if not target_value:
            logger.warning(
                "No category option with label %r found in <select id='category_id'>.",
                category_label,
            )
            return

        # Use JS to set the value and dispatch change so Choices/Stimulus can react.
        js = """
            const sel = arguments[0];
            const val = arguments[1];

            sel.value = val;

            const evt = new Event('change', { bubbles: true });
            sel.dispatchEvent(evt);
        """
        try:
            driver.execute_script(js, select_el, target_value)
            logger.info(
                "Category set to %r (value=%r) via hidden <select>.",
                category_label,
                target_value,
            )
        except Exception as e:
            logger.warning(
                "Error applying category %r (value=%r) via JS: %s",
                category_label,
                target_value,
                e,
            )

    def _open_activity_builder_for_new_activity(self, timeout: int = 20) -> bool:
        """
        After creating an activity template, CA navigates to:

            /activity_templates/<template_id>/activity_revisions/<revision_id>

        This helper:
          - waits for that URL shape
          - clicks the 'Edit content' button
          - navigates directly to its href
          - waits for the Activity Builder page:
                /revisions/<revision_id>/sections/information

        Returns True on success, False on failure.
        """
        driver = self.session.driver
        wait   = self.session.wait
        logger = self.logger

        # 1) Wait until we're on the template revision page
        try:
            def on_revision_page(d):
                url = d.current_url or ""
                return (
                    "/activity_templates/" in url
                    and "/activity_revisions/" in url
                )

            wait.until(on_revision_page)
            logger.info("Detected activity template revision page: %s", driver.current_url)
        except TimeoutException:
            logger.error(
                "Timed out waiting for activity template revision page "
                "(URL containing '/activity_templates/' and '/activity_revisions/'). "
                "Current URL: %s",
                driver.current_url,
            )
            return False

        # 2) Click the "Edit content" button
        try:
            edit_btn = wait.until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "a.btn.btn-primary--50[href*='/sections/information']")
                )
            )
        except TimeoutException:
            logger.error(
                "Could not find clickable 'Edit content' button on template revision page."
            )
            return False

        logger.info("Clicking 'Edit content' to open Activity Builder...")
        if not self.session.click_element_safely(edit_btn):
            edit_btn.click()

        # 3) Wait for Activity Builder URL: /revisions/<id>/sections/information
        try:
            def on_builder_page(d):
                url = d.current_url or ""
                return "/revisions/" in url and "/sections/information" in url

            wait.until(on_builder_page)
            logger.info("Activity Builder is open: %s", driver.current_url)
            return True
        except TimeoutException:
            logger.error(
                "Timed out waiting for Activity Builder page "
                "(URL containing '/revisions/' and '/sections/information'). "
                "Current URL: %s",
                driver.current_url,
            )
            return False

    def _build_from_instruction(
            self,
            act: ActivityInstruction,
            *,
            run_dir: Path
        ) -> bool:
        builder = self.builder
        editor = self.editor
        logger = self.logger

        if not act:
            logger.error("No activity passed from controller")
            return

        builder.hard_resync_count = 0

        failures: list[FailureRecord] = []  # collect skipped fields
        consecutive_failures = 0
        FAILURE_THRESHOLD = 5  # you can tune this
        aborted = False
        last_successful_handle = None
        # ---- Body audit tracking (truth-at-boundary) ----
        expected_bodies_by_section: dict[str, dict[str, str]] = {}  # section_id -> {field_id: expected_html}
        last_section_id: str | None = None

        def _section_key(fi) -> tuple[str | None, int | None]:
            return (fi.section_title, fi.section_index)

        sec_to_indices: dict[tuple[str | None, int | None], list[int]] = defaultdict(list)
        for i, fi in enumerate(act.fields):
            sec_to_indices[_section_key(fi)].append(i)

        top_candidates: list[int] = []
        mid_candidates: list[int] = []

        for _, idxs in sec_to_indices.items():
            if not idxs:
                continue
            idxs_sorted = sorted(idxs)
            top_candidates.append(idxs_sorted[0])
            if len(idxs_sorted) >= 3:
                mid_candidates.extend(idxs_sorted[1:-1])

        add_pos = str(getattr(config, "FAULT_INJECT_TARGET_ADD_POSITION", "any")).lower()
        cfg_pos = str(getattr(config, "FAULT_INJECT_TARGET_CONFIGURE_POSITION", "any")).lower()
        fallback = str(getattr(config, "FAULT_INJECT_TARGET_FALLBACK", "any")).lower()  # "any" | "section_top" | "off"

        def _pool_for(pos: str) -> list[int] | None:
            """
            Returns:
            - list[int] => constrain selection to these fi_indices
            - None      => allow "any" (full range)
            - []        => force "no possible targets" (injector will pick None)
            """
            if pos == "any":
                return None
            if pos == "section_top":
                return top_candidates
            if pos == "section_mid":
                if mid_candidates:
                    return mid_candidates

                # --- Fallback handling when mid candidates don't exist ---
                if fallback == "section_top":
                    return top_candidates
                if fallback == "off":
                    return []          # disable this kind of injection
                return None            # fallback to any

            # Unknown value: be safe and treat as any
            return None

        add_candidates = _pool_for(add_pos)
        configure_candidates = _pool_for(cfg_pos)

        # --- Instantiate injector ONCE, with final pools ---
        faults = FaultInjector(
            total_fields=len(act.fields),
            add_candidates=add_candidates,
            configure_candidates=configure_candidates,
            # properties can stay "any" unless you also want to constrain it
        )

        if faults.enabled:
            logger.warning(
                "FAULT_INJECT enabled. Mode: add_pos=%r cfg_pos=%r fallback=%r "
                "Pools: add=%s configure=%s "
                "Targets: add=%r properties=%r configure=%r",
                add_pos,
                cfg_pos,
                fallback,
                "any" if add_candidates is None else f"{len(add_candidates)} candidates",
                "any" if configure_candidates is None else f"{len(configure_candidates)} candidates",
                faults.plan.add_fail_fi_index,
                faults.plan.properties_fail_fi_index,
                faults.plan.configure_fail_fi_index,
            )

        def _is_injected(kind: str, fi_index: int) -> bool:
            if not faults.enabled:
                return False
            if kind == "add":
                return faults.should_fail_add(fi_index)
            if kind == "properties":
                return faults.should_fail_properties(fi_index)
            if kind == "configure":
                return faults.should_fail_configure(fi_index)
            return False

        def _drain_editor_skips_into_failures(*, handle, field_key, sec_title, sec_index, title, source, fi_index):
            events = editor.pop_skip_events()
            for ev in events:
                failures.append(make_failure_record(
                    activity_code=act.activity_code or "",
                    kind=ev.get("kind") or "unknown",
                    reason=ev.get("reason"),
                    retryable=ev.get("retryable", False),
                    requested=ev.get("requested", {}),
                    field_key=field_key,
                    field_type_key=handle.field_type_key if handle else None,
                    field_id=handle.field_id if handle else ev.get("field_id"),
                    section_id=handle.section_id if handle else None,
                    section_title=sec_title,
                    section_index=sec_index,
                    source=source,
                    title=title or (handle.title if handle else None) or ev.get("field_title"),
                    fi_index=fi_index,
                ))

        def _requested_from_field_config(cfg) -> dict[str, object]:
            """
            Best-effort extraction of the properties knobs we would have attempted to set.
            Keep this intentionally tolerant: configs differ by field type.
            """
            requested: dict[str, object] = {}

            # Common keys used by ActivityEditor.set_field_properties
            for key in (
                "hide_in_report",
                "learner_visibility",
                "assessor_visibility",
                "required",
                "marking_type",
                "enable_model_answer",
                "enable_assessor_comments",
            ):
                if hasattr(cfg, key):
                    val = getattr(cfg, key)
                    if val is not None:
                        requested[key] = val

            # If your config uses different names (older/alternate), map them here:
            # (These are examples — keep only what exists in your codebase)
            if "enable_assessor_comments" not in requested and hasattr(cfg, "assessor_comments"):
                val = getattr(cfg, "assessor_comments")
                if val is not None:
                    requested["enable_assessor_comments"] = val

            if "enable_model_answer" not in requested and hasattr(cfg, "model_answer"):
                val = getattr(cfg, "model_answer")
                if val is not None:
                    requested["enable_model_answer"] = val

            return requested

        with phase_timer(logger, f"{act.activity_code}: build from instruction"):
        # For now we expect exactly one written_assessment per WA spec, but looping keeps it flexible
            logger.info(
                "Building activity from spec: code=%r, title=%r",
                act.activity_code,
                act.activity_title,
            )

            act_dir = run_dir / "activities"
            act_stem = f"{act.activity_code}_{act.activity_type}"
            instruction_path = act_dir / f"{act_stem}_instruction.json"
            dump_activity_instruction_json(act, instruction_path, logger)

            # debug stop point if necessary
            # result = input("Do you want to continue the process here or end? Y/N")
            # if result =="N":
            #     return False

            # Process fields in the exact order they appear in the ActivityInstruction
            for fi_index, fi in enumerate(act.fields):
                sec_title = fi.section_title
                sec_index = fi.section_index

                # ---- Section boundary: audit previous section bodies before moving on ----
                # We detect boundary by comparing the logical section tuple (title/index).
                current_section_key = (sec_title, sec_index)

                if fi_index == 0:
                    logger.warning("AUDIT SENTINEL: entered first section block fi_index=%d", fi_index)
                    prev_section_key = current_section_key
                else:
                    # look back at previous fi to compare section key
                    prev_fi = act.fields[fi_index - 1]
                    prev_section_key = (prev_fi.section_title, prev_fi.section_index)

                if current_section_key != prev_section_key:
                    # We are about to start a new section; audit the most recently used section_id.
                    logger.warning("AUDIT SENTINEL: entered section-boundary block fi_index=%d", fi_index)
                    expected: dict[str, str] = {}
                    if last_section_id is not None:
                        expected = expected_bodies_by_section.get(last_section_id, {})
                    if last_section_id is not None and expected:
                        logger.warning(
                            "AUDIT: running section audit before new section fi_index=%d last_section_id=%r expected_fields=%d",
                            fi_index,
                            last_section_id,
                            len(expected) if last_section_id else 0,
                        )
                        try:
                            editor.audit_bodies_now(
                                expected,
                                label=f"section-audit BEFORE new section fi_index={fi_index}",
                            )
                        except Exception as e:
                            logger.warning(
                                "AUDIT FAILED (section_id=%r fi_index=%d): %s",
                                last_section_id,
                                fi_index,
                                e,
                                exc_info=True,
                            )

                # Set the rest of the vars            
                field_key = fi.field_key
                raw = getattr(fi, "raw_component", {}) or {}
                title = raw.get("title")
                source = raw.get("source")

                is_injected = _is_injected("add", fi_index)

                logger.info("Adding field %r in section_title=%r, section_index=%r (fi_index=%d)", field_key, sec_title, sec_index, fi_index)

                # ---- 1. Create field ----
                with phase_timer(logger, f"{act.activity_code}: add field {field_key}"):
                    if faults.should_fail_add(fi_index):
                        logger.warning("FAULT_INJECT: forcing add failure at fi_index=%d field_key=%r", fi_index, field_key)
                        handle = None
                    else:
                        handle = builder.add_field_from_spec(
                            key=field_key,
                            section_title=sec_title,
                            section_index=sec_index,
                            fi_index=fi_index,
                        )
                    # try the same field once more before we decide to skip/abort.
                    if handle is None:
                        # If you added a flag like builder.last_add_triggered_hard_resync, use it here.
                        # For now we'll do a conservative single retry for tables (most common phantom).
                        retry_once = (field_key == "interactive_table")

                        if retry_once and not faults.should_fail_add(fi_index):
                            logger.warning(
                                "Field %r failed to add; retrying once (common phantom-add case).",
                                field_key,
                            )
                            handle = builder.add_field_from_spec(
                                key=field_key,
                                section_title=sec_title,
                                section_index=sec_index,
                                fi_index=fi_index,
                            )

                    if handle is None:
                        reason = f"add_field_from_spec failed (see logs for details)"

                        logger.error(
                            "Failed to add field %r (%r) in section %r; skipping configuration.",
                            field_key,
                            title,
                            sec_title,
                        )

                        failures.append(make_failure_record(
                            activity_code=act.activity_code or "Unknown",
                            kind="add",
                            reason=reason,
                            retryable=True,
                            requested={},
                            field_key=field_key,
                            field_type_key=None,
                            field_id=None,
                            section_id=None,
                            section_title=sec_title,
                            section_index=sec_index,
                            source=source,
                            title=title,
                            fi_index=fi_index,
                        ))

                        consecutive_failures += 1

                        # Critical fields: abort immediately (better than producing a broken activity)
                        if field_key in config.CRITICAL_FIELD_KEYS and not is_injected:
                            logger.error(
                                "Critical field %r failed to add in section %r. Aborting build.",
                                field_key,
                                sec_title,
                            )
                            aborted = True
                            break
                        if field_key in config.CRITICAL_FIELD_KEYS and is_injected:
                            logger.warning(
                                "Critical field %r failed to add due to FAULT_INJECT at fi_index=%d; "
                                "continuing so retry pipeline can be validated.",
                                field_key, fi_index
                            )

                        if consecutive_failures >= FAILURE_THRESHOLD:
                            logger.error(
                                "Encountered %d consecutive field-add failures "
                                " (most recently for field %r in section %r). "
                                "Aborting build early to avoid further inconsistent state.",
                                consecutive_failures,
                                field_key,
                                sec_title,
                            )
                            aborted = True
                            break  # break out of fields loop

                        continue  # move to next FieldInstruction

                    # Keep Audit tracking up to date
                    if handle and handle.section_id:
                        last_section_id = handle.section_id

                    # If we successfully added a field, reset consecutive failure count
                    consecutive_failures = 0

                    # ---- 2. Configure field ----
                    # Build config from spec + defaults and configure the new field
                    cfg = build_field_config(fi)

                    # after cfg is built, before configure:
                    if getattr(cfg, "body_html", None) and handle and handle.section_id and handle.field_id:
                        expected_bodies_by_section.setdefault(handle.section_id, {})[handle.field_id] = cfg.body_html or ""

                    if isinstance(cfg, TableConfig):
                        sample = next(iter(cfg.cell_overrides.items()), None)
                        logger.info("Table cell_overrides sample=%r", sample)

                with phase_timer(logger, f"{act.activity_code}: configure field {field_key}"):
                    prop_fault_inject: bool = False
                    try:
                        if faults.should_fail_properties(fi_index):
                            req = _requested_from_field_config(cfg)
                            prop_fault_inject = True
                            logger.warning("FAULT_INJECT: forcing properties failure via skip event at fi_index=%d (requested=%r)", fi_index, req)
                            editor.record_skip({
                                "kind": "properties",
                                "reason": f"FAULT_INJECT: forced properties failure at fi_index={fi_index}",
                                "retryable": True,
                                "field_id": handle.field_id if handle else None,
                                "field_title": title,
                                "requested": req,
                            })
                            # ensure drain happens; simplest is just `continue` after finally runs
                            continue
                        if faults.should_fail_configure(fi_index):
                            raise RuntimeError(f"FAULT_INJECT: forced configure failure at fi_index={fi_index}")                       
                        editor.configure_field_from_config(handle=handle, config=cfg, last_successful_handle=last_successful_handle, prop_fault_inject=prop_fault_inject)

                    except TableResizeError as e:
                        logger.warning("Table strict resize failed; attempting recovery for %r: %s", raw.get("title"), e)

                        # Recovery Step 1: refresh and try configure again on SAME field
                        try:
                            self.session.refresh_page()  # or session.refresh_page() depending on your wiring
                            self.sections.ensure_section_ready(section_title=sec_title, index=sec_index)
                            editor.configure_field_from_config(handle=handle, config=cfg, last_successful_handle=last_successful_handle, prop_fault_inject=prop_fault_inject)
                            consecutive_failures = 0
                            continue
                        except Exception as e2:
                            logger.warning("Recovery configure after refresh failed: %s", e2)

                        # Recovery Step 2: delete field + recreate + configure
                        try:
                            self.deleter.delete_field_by_handle(handle)   # or activity_deleter.delete_field_by_id(handle.field_id)
                            new_handle = builder.add_field_from_spec(key=field_key, section_title=sec_title, section_index=sec_index)
                            if new_handle:
                                editor.configure_field_from_config(handle=new_handle, config=cfg, last_successful_handle=last_successful_handle, prop_fault_inject=prop_fault_inject)
                                consecutive_failures = 0
                                continue
                            raise RuntimeError("Recreate returned None")
                        except Exception as e3:
                            logger.error("Table recovery failed; skipping field. %s", e3)
                            # fall through to existing failure append/skip logic

                    except FieldPropertiesSidebarTimeout as e:
                        logger.warning("Properties sidebar timeout; attempting refresh recovery: %s", e)
                        try:
                            builder.session.refresh_page()
                            self.sections.ensure_section_ready(section_title=sec_title, index=sec_index)
                            editor.configure_field_from_config(handle=handle, config=cfg, last_successful_handle=last_successful_handle, prop_fault_inject=prop_fault_inject)
                            consecutive_failures = 0
                            continue
                        except Exception as e2:
                            # fall back to your existing skip/failure tracking
                            logger.error(
                                "Properties recovery failed; skipping field %r (%r) in section %r: %s",
                                field_key, title, sec_title, e2
                            )
                            reason = f"properties sidebar recovery failed: {e2}"
                            failures.append(make_failure_record(
                                activity_code=act.activity_code or "Unknown",
                                kind="properties",
                                reason=reason,
                                retryable=True,
                                requested={},
                                field_key=field_key,
                                field_type_key=handle.field_type_key if handle else None,
                                field_id=handle.field_id if handle else None,
                                section_id=handle.section_id if handle else None,
                                section_title=sec_title,
                                section_index=sec_index,
                                source=source,
                                title=title,
                                fi_index=fi_index,
                            ))

                            consecutive_failures += 1

                            if field_key in config.CRITICAL_FIELD_KEYS and not is_injected:
                                logger.error(
                                    "Critical field %r failed to configure properties in section %r. Aborting build.",
                                    field_key, sec_title
                                )
                                aborted = True
                                break
                            if field_key in config.CRITICAL_FIELD_KEYS and is_injected:
                                logger.warning(
                                    "Critical field %r failed to configure properties in section %r due to FAULT_INJECT at fi_index=%d; "
                                    "continuing so retry pipeline can be validated.",
                                    field_key, sec_title, fi_index
                                )
                            if consecutive_failures >= FAILURE_THRESHOLD:
                                logger.error(
                                    "Encountered %d consecutive failures (latest properties for %r in section %r). Aborting build early.",
                                    consecutive_failures, field_key, sec_title
                                )
                                aborted = True
                                break

                            continue

                    except Exception as e:

                        logger.error("Error configuring field %r (%r) in section %r: %s", field_key, title, sec_title, e)
                        reason = f"configure_field_from_config error: {e}"

                        failures.append(make_failure_record(
                            activity_code=act.activity_code or "Unknown",
                            kind="configure",
                            reason=reason,
                            retryable=True,
                            requested={},
                            field_key=field_key,
                            field_type_key=handle.field_type_key if handle else None,
                            field_id=handle.field_id if handle else None,
                            section_id=handle.section_id if handle else None,
                            section_title=sec_title,
                            section_index=sec_index,
                            source=source,
                            title=title,
                            fi_index=fi_index,
                        ))

                        consecutive_failures += 1

                        # Critical fields: abort immediately
                        if field_key in config.CRITICAL_FIELD_KEYS and not is_injected:
                            logger.error(
                                "Critical field %r failed to configure in section %r. Aborting build.",
                                field_key,
                                sec_title,
                            )
                            aborted = True
                            break
                        if field_key in config.CRITICAL_FIELD_KEYS and is_injected:
                            logger.warning(
                                "Critical field %r failed to configure in section %r due to FAULT_INJECT at fi_index=%d; "
                                "continuing so retry pipeline can be validated.",
                                field_key, sec_title, fi_index
                            )

                        if consecutive_failures >= FAILURE_THRESHOLD:
                            logger.error(
                                "Encountered %d consecutive failures (latest configuring %r in section %r). Aborting build early.",
                                consecutive_failures,
                                field_key,
                                sec_title,
                            )
                            aborted = True
                            break

                        continue

                    finally:
                        # always drain, even if configure "succeeded" but recorded a skip
                        _drain_editor_skips_into_failures(
                            handle=handle,
                            field_key=field_key,
                            sec_title=sec_title,
                            sec_index=sec_index,
                            title=title,
                            source=source,
                            fi_index=fi_index,
                        )

                    if field_key != "interactive_table":
                        last_successful_handle = handle

            if failures:
                logger.warning("Build completed with %d skipped item(s):", len(failures))
                for f in failures:
                    logger.warning(
                        "- [%s] kind=%r field_key=%r field_id=%r title=%r section=%r(index=%r) reason=%s",
                        f["activity_code"],
                        f.get("kind"),
                        f["field_key"],
                        f.get("field_id"),
                        f.get("title"),
                        f["section_title"],
                        f["section_index"],
                        f["reason"],
                    )
                    if f.get("kind") == "properties":
                        req = f.get("requested") or {}
                        # only print requested non-None values
                        req_filtered = {k: v for k, v in req.items() if v is not None}
                        logger.warning("Manual settings required: %r", req_filtered)

                act_dir = run_dir / "activities"
                act_stem = f"{act.activity_code}_{act.activity_type}"
                failures_path = act_dir / f"{act_stem}_failures.json"
                self._dump_json(failures_path, failures)
                logger.warning("Wrote failures report: %s", failures_path.as_posix())

                # --- Retry Failures ---
                failures = self._maybe_retry_failures(act=act, failures=failures, run_dir=run_dir)

                # Re-log summary after retry
                if failures:
                    logger.warning("After retries, %d failure(s) remain.", len(failures))
                    for f in failures:
                        logger.warning(
                            "- [%s] kind=%r field_key=%r field_id=%r title=%r section=%r(index=%r) reason=%s (last_error=%r)",
                            f["activity_code"],
                            f.get("kind"),
                            f["field_key"],
                            f.get("field_id"),
                            f.get("title"),
                            f["section_title"],
                            f["section_index"],
                            f["reason"],
                            f.get("last_error"),
                        )
                        if f.get("kind") == "properties":
                            req = f.get("requested") or {}
                            # only print requested non-None values
                            req_filtered = {k: v for k, v in req.items() if v is not None}
                            logger.warning("Manual settings required: %r", req_filtered)
                else:
                    logger.info("All failures resolved by retry pass(es).")

            else:
                logger.info("Build completed with no skipped fields.")

            # Final audit for the last section we touched
            if last_section_id and last_section_id in expected_bodies_by_section:
                logger.warning(
                    "AUDIT: running FINAL section audit last_section_id=%r expected_fields=%d",
                    last_section_id,
                    len(expected_bodies_by_section.get(last_section_id, {})),
                )
                try:
                    editor.audit_bodies_now(
                        expected_bodies_by_section[last_section_id],
                        label="final-section-audit END of build",
                    )
                except Exception as e:
                    logger.warning(
                        "AUDIT FAILED FINAL (section_id=%r): %s",
                        last_section_id,
                        e,
                        exc_info=True,
                    )

            return not aborted

    def _maybe_retry_failures(
        self,
        *,
        act: ActivityInstruction,
        failures: list[FailureRecord],
        run_dir: Path,
    ) -> list[FailureRecord]:
        """
        Optionally retry failures based on config flags.

        Returns the (possibly updated) failures list (resolved items removed, unresolved retained/updated).
        """
        logger = self.logger
        builder = self.builder
        editor = self.editor
        sections = self.sections
        deleter = self.deleter
        session = self.session

        if not failures:
            return failures

        # ---- Config knobs (use getattr so your exact names can vary) ----
        # ---- Config knobs (match config.py) ----
        auto_retry = bool(getattr(config, "AUTO_RETRY_FAILURES", False))
        prompt_on_failures = bool(getattr(config, "PROMPT_ON_FAILURES", True))

        retry_max_passes = int(getattr(config, "RETRY_MAX_PASSES", 2))
        retry_failure_threshold = int(getattr(config, "RETRY_FAILURE_THRESHOLD", 5))
        retry_refresh_before_pass = bool(getattr(config, "RETRY_REFRESH_BEFORE_PASS", False))
        retryable_only_override = bool(getattr(config, "RETRYABLE_ONLY_OVERRIDE", False))

        # Derived behavior
        retry_enabled = auto_retry or prompt_on_failures
        retry_mode = "auto" if auto_retry else "ask"  # ask only if prompting is enabled

        if not retry_enabled:
            logger.info(
                "Retry skipped: AUTO_RETRY_FAILURES=%r PROMPT_ON_FAILURES=%r",
                auto_retry,
                prompt_on_failures,
            )
            return failures

        # Filter to retryable by default (properties skips set retryable=True, etc.)
        retry_pool = failures
        if not retryable_only_override:
            retry_pool = [f for f in failures if f.get("retryable", False)]

        if not retry_pool:
            logger.info("Retry enabled, but no retryable failures were recorded.")
            return failures

        # ASK vs AUTO
        if retry_mode == "ask":
            try:
                resp = input(f"\nRetry {len(retry_pool)} retryable failure(s) now? [y/N]: ").strip().lower()
                if resp not in ("y", "yes"):
                    logger.info("User declined retry run.")
                    return failures
            except Exception:
                # If stdin isn't available, fail safe: do not retry.
                logger.warning("Retry prompt unavailable (non-interactive). Skipping retries.")
                return failures

        # ---- Helper: get FieldInstruction from fi_index ----
        def _get_fi_from_index(fi_index: int | None):
            if fi_index is None:
                return None
            if fi_index < 0 or fi_index >= len(act.fields):
                return None
            return act.fields[fi_index]
        
        def _drop_plan_for(fi_index: int | None) -> tuple[str, str | None]:
            """
            Decide where to insert a recreated field during retry.

            Returns:
            (drop_location, anchor_field_id)

            drop_location is one of: "section_top" | "after_field" | "section_bottom"
            anchor_field_id is only used when drop_location == "after_field".
            """

            if fi_index is None:
                return ("section_bottom", None)
            
            fi = _get_fi_from_index(fi_index)
            if fi is None:
                return ("section_bottom", None)

            sec_title = fi.section_title
            sec_index = fi.section_index

            # Find previous FieldInstruction in SAME section (nearest above in spec order)
            prev_fi_index = None
            for j in range(fi_index - 1, -1, -1):
                prev = act.fields[j]
                if prev.section_title == sec_title and prev.section_index == sec_index:
                    prev_fi_index = j
                    break

            # No previous field → should be first in section
            if prev_fi_index is None:
                return ("section_top", None)

            sections.ensure_section_ready(sec_title, sec_index)
            sid = sections.current_section_id or (sections.current_section_handle.section_id if sections.current_section_handle else "")

            anchor_id = None
            if sid and fi_index is not None:
                anchor_id = self.builder.registry.anchor_before_fi_index(section_id=sid, fi_index=fi_index)

            if anchor_id:
                drop_location = "after_field"
            else:
                # if no prior field exists, it should be first
                drop_location = "section_top"

            return (drop_location, anchor_id)

        # ---- Helper: attempt to resolve one failure ----
        def _attempt_one(f: FailureRecord) -> bool:
            """
            Returns True if resolved, False if still failing.
            Updates f['attempts'] and f['last_error'] on failure.
            """
            f["attempts"] = int(f.get("attempts", 0)) + 1
            kind = (f.get("kind") or "unknown").lower()
            field_id = f.get("field_id")
            field_key = f.get("field_key")
            sec_title = f.get("section_title")
            sec_index = f.get("section_index")
            fi_index = f.get("fi_index")
            requested = f.get("requested") or {}

            if not fi_index or fi_index is None:
                f["last_error"] = "fi_index missing; cannot retry safely"
                return False

            logger.debug("Attempting retry for FailureRecord: %r", f)

            try:
                # Ensure section is selected (best effort)
                try:
                    sections.ensure_section_ready(section_title=sec_title, index=sec_index)
                except Exception:
                    # In some flows, section title/index might be missing; ignore
                    pass

                # ---- properties retry: re-apply only properties using field_id ----
                if kind == "properties":
                    if not field_id:
                        raise RuntimeError("properties retry requires field_id (missing)")
                    field_el = editor.get_field_by_id(str(field_id))
                    if field_el is None:
                        raise RuntimeError(f"Could not locate field by id={field_id}")

                    # Call set_field_properties directly using requested dict
                    editor.set_field_properties(
                        field_el,
                        hide_in_report=requested.get("hide_in_report"),
                        learner_visibility=requested.get("learner_visibility"),
                        assessor_visibility=requested.get("assessor_visibility"),
                        required=requested.get("required"),
                        marking_type=requested.get("marking_type"),
                        enable_model_answer=requested.get("enable_model_answer"),
                        enable_assessor_comments=requested.get("enable_assessor_comments"),
                    )

                    # If editor recorded a skip during retry, treat as still failing
                    evs = editor.pop_skip_events()
                    if evs:
                        # keep the latest reason
                        f["last_error"] = evs[-1].get("reason") or "properties retry produced skip events"
                        return False

                    return True

                # ---- add retry: re-add the missing field using fi_index (no field_id) ----
                if kind == "add":
                    fi = _get_fi_from_index(fi_index)
                    if fi is None:
                        raise RuntimeError(f"add retry requires valid fi_index; got {fi_index}")
                    
                    drop_location, anchor_id = _drop_plan_for(fi_index)
                    logger.debug("Add field retry for %s type field in %s section. Field to be placed at %s dropzone. Field Index: %s (anchor_id: %s)", fi.field_key, fi.section_title, drop_location, fi_index, anchor_id)
                    # re-add
                    handle = builder.add_field_from_spec(
                        key=fi.field_key,
                        section_title=fi.section_title,
                        section_index=fi.section_index,
                        drop_location=drop_location,
                        insert_after_field_id=anchor_id,
                        fi_index=fi_index,
                    )
                    if handle is None:
                        raise RuntimeError("add_field_from_spec returned None on retry")

                    # configure immediately (same as main loop)
                    cfg = build_field_config(fi)
                    editor.configure_field_from_config(handle=handle, config=cfg, last_successful_handle=None)

                    # if configure recorded skips, not resolved
                    evs = editor.pop_skip_events()
                    if evs:
                        f["last_error"] = evs[-1].get("reason") or "add retry produced skip events"
                        return False

                    # resolved
                    return True

                # ---- configure retry: re-run configure_field_from_config ----
                # Needs a way to rebuild config and a handle. If we have field_id, we can
                # (a) locate element, (b) build a minimal handle-like object OR just re-run
                # editor.configure_field_from_config if it requires handle. We'll rebuild via fi_index.
                if kind in ("configure", "table_resize"):
                    fi = _get_fi_from_index(fi_index)
                    if fi is None:
                        raise RuntimeError(f"{kind} retry requires valid fi_index; got {fi_index}")

                    # If we have a field_id, keep using it (more accurate)
                    # But configure_field_from_config wants a handle; easiest is to attempt delete+recreate if field_id missing.
                    if field_id:
                        # Best effort: delete + recreate + configure.
                        # (This avoids needing to perfectly reconstruct a handle.)
                        try:
                            deleter.delete_field_by_id(str(field_id))
                        except Exception:
                            # if delete fails, still try to reconfigure existing by element-only path where possible
                            pass

                    drop_location, anchor_id = _drop_plan_for(fi_index)
                    logger.debug("Add field retry and configure for %s type field in %s section. Field to be placed at %s dropzone. Field Index: %s (anchor_id: %s)", fi.field_key, fi.section_title, drop_location, fi_index, anchor_id)
                    new_handle = builder.add_field_from_spec(
                        key=fi.field_key,
                        section_title=fi.section_title,
                        section_index=fi.section_index,
                        drop_location=drop_location,
                        insert_after_field_id=anchor_id,
                        fi_index=fi_index,
                    )
                    if new_handle is None:
                        raise RuntimeError("recreate returned None during configure retry")

                    cfg = build_field_config(fi)
                    editor.configure_field_from_config(handle=new_handle, config=cfg, last_successful_handle=None)

                    evs = editor.pop_skip_events()
                    if evs:
                        f["last_error"] = evs[-1].get("reason") or "configure retry produced skip events"
                        return False

                    return True

                # Unknown kinds: don’t know how to retry safely
                raise RuntimeError(f"Unsupported retry kind: {kind}")

            except Exception as e:
                f["last_error"] = str(e)
                return False

        # ---- Retry passes ----
        # Prioritise in-order (fi_index) where present so we re-add in a predictable order.
        def _sort_key(f: FailureRecord):
            fi_idx = f.get("fi_index")
            return (fi_idx is None, fi_idx if fi_idx is not None else 10**9, f.get("field_key") or "")

        unresolved = failures[:]  # start with everything; remove items as they succeed
        for p in range(1, retry_max_passes + 1):

            # Optional refresh before each pass
            if retry_refresh_before_pass:
                try:
                    session.refresh_page()
                except Exception as e:
                    logger.warning("Retry pass %d: refresh failed: %s", p, e)
                    
            # Rebuild retry pool each pass
            pool = unresolved
            if not retryable_only_override:
                pool = [f for f in unresolved if f.get("retryable", False)]

            if not pool:
                break

            pool = sorted(pool, key=_sort_key)

            logger.warning("Retry pass %d/%d: attempting %d item(s)...", p, retry_max_passes, len(pool))

            resolved_ids = set()
            consecutive_retry_failures = 0
            for f in pool:
                ok = _attempt_one(f)
                if ok:
                    resolved_ids.add(id(f))
                    consecutive_retry_failures = 0
                else:
                    consecutive_retry_failures += 1
                    if consecutive_retry_failures >= retry_failure_threshold:
                        logger.warning(
                            "Retry pass %d: hit RETRY_FAILURE_THRESHOLD=%d; stopping early.",
                            p, retry_failure_threshold
                        )
                        break

            if resolved_ids:
                unresolved = [f for f in unresolved if id(f) not in resolved_ids]
                logger.warning("Retry pass %d: resolved %d item(s). Remaining=%d", p, len(resolved_ids), len(unresolved))
            else:
                logger.warning("Retry pass %d: no items resolved; stopping early.", p)
                break

        # If we resolved anything, rewrite failures report (optional but useful)
        try:
            act_dir = run_dir / "activities"
            act_stem = f"{act.activity_code}_{act.activity_type}"
            failures_path = act_dir / f"{act_stem}_failures.json"
            self._dump_json(failures_path, unresolved)
            logger.warning("Updated failures report after retries: %s", failures_path.as_posix())
        except Exception as e:
            logger.warning("Could not rewrite failures report after retries: %s", e)

        return unresolved

    def _pick_spec_paths_ui(self, default_dir: str = "src/specs") -> list[str]:
        """
        Tiny UI to pick one or more YAML specs.
        - User can optionally choose a folder first (to help navigation)
        - Multi-select enabled
        - Returns [] if cancelled
        """
        # Tk must be created/destroyed on the same thread
        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        try:
            # Optional: ask folder first, but allow cancel -> keep default_dir
            folder = askdirectory(
                title="Select folder containing spec(s) (Cancel to use default)",
                initialdir=default_dir,
                mustexist=True,
            )
            initial = folder or default_dir

            paths = askopenfilenames(
                title="Select YAML spec file(s)",
                initialdir=initial,
                filetypes=[("YAML files", "*.yml *.yaml"), ("All files", "*.*")],
            )
            # askopenfilenames returns a tuple
            return [str(p) for p in paths] if paths else []
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def _get_spec_paths(self) -> list[str]:
        """
        Prefer UI selection. If cancelled or Tk fails (e.g. headless), fall back to CLI.
        """
        try:
            paths = self._pick_spec_paths_ui(default_dir="src/specs")
            if paths:
                return paths
        except Exception as e:
            self.logger.warning("Spec picker UI unavailable; falling back to CLI selection: %s", e)

        # CLI fallback: your existing single path prompt
        return [self._get_spec_path()]

    def _attach_run_file_logger(self, run_dir: Path) -> None:
        logger = self.logger
        log_path = run_dir / "logs" / f"{run_dir.name}.log"

        # Remove the default file handler if present (prevents double logging)
        for h in list(logger.handlers):
            if getattr(h, "name", "") == "default_file":
                try:
                    h.flush()
                    h.close()
                except Exception:
                    pass
                logger.removeHandler(h)

        fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        formatter = logging.Formatter(fmt)

        run_fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        run_fh.setLevel(logging.DEBUG)
        run_fh.setFormatter(formatter)
        run_fh.name = "run_file"

        logger.addHandler(run_fh)
        logger.info("File logging redirected to: %s", log_path.as_posix())



class FaultInjector:
    """
    One-shot fault injection plan for a single build run.

    - Chooses targets once at the start (fi_index values).
    - At runtime you call should_fail_* per fi_index.
    - Uses your config flags:
        FAULT_INJECT_ENABLED
        FAULT_INJECT_SEED
        FAULT_INJECT_PROB_ADD_FAIL
        FAULT_INJECT_PROB_PROPERTIES_FAIL
        FAULT_INJECT_PROB_CONFIGURE_FAIL
    """

    def __init__(
        self,
        *,
        total_fields: int,
        add_candidates: list[int] | None = None,
        configure_candidates: list[int] | None = None,
        properties_candidates: list[int] | None = None,
    ):
        self.total_fields = total_fields
        self.enabled: bool = bool(getattr(config, "FAULT_INJECT_ENABLED", False))
        self.plan = FaultPlan()

        # Make "consume once" behavior possible (optional, but handy)
        self._add_consumed = False
        self._properties_consumed = False
        self._configure_consumed = False

        self._add_candidates = add_candidates
        self._configure_candidates = configure_candidates
        self._properties_candidates = properties_candidates

        if not self.enabled or total_fields <= 0:
            return

        seed = getattr(config, "FAULT_INJECT_SEED", None)
        self.rng = random.Random(seed if seed is not None else random.randrange(1, 1_000_000_000))

        self.plan.add_fail_fi_index = self._maybe_pick_from(
            candidates=self._add_candidates,
            prob=getattr(config, "FAULT_INJECT_PROB_ADD_FAIL", 0.0),
        )
        self.plan.properties_fail_fi_index = self._maybe_pick_from(
            candidates=self._properties_candidates,
            prob=getattr(config, "FAULT_INJECT_PROB_PROPERTIES_FAIL", 0.0),
        )
        self.plan.configure_fail_fi_index = self._maybe_pick_from(
            candidates=self._configure_candidates,
            prob=getattr(config, "FAULT_INJECT_PROB_CONFIGURE_FAIL", 0.0),
        )

        # Optional: avoid collisions (helps you test each failure type distinctly)
        self._deconflict()

    def _maybe_pick_from(self, candidates: list[int] | None, prob: float) -> Optional[int]:
        if candidates is None:
            return self._maybe_pick(total=self.total_fields, prob=prob)

        # same prob rules as _maybe_pick
        try:
            p = float(prob)
        except Exception:
            p = 0.0
        if p <= 0.0:
            return None
        if not candidates:
            return None
        if p >= 1.0:
            return self.rng.choice(candidates)
        return self.rng.choice(candidates) if self.rng.random() < p else None

    def _maybe_pick(self, *, total: int, prob: float) -> Optional[int]:
        """
        With probability `prob`, return a random fi_index.
        - prob <= 0 => None
        - prob >= 1 => always pick something
        """
        try:
            p = float(prob)
        except Exception:
            p = 0.0

        if p <= 0.0:
            return None
        if p >= 1.0:
            return self.rng.randrange(total)
        return self.rng.randrange(total) if self.rng.random() < p else None

    def _deconflict(self) -> None:
        """
        If two targets collide (same fi_index), reroll later ones a few times.
        Keeps testing clearer (you get separate add/properties/configure failures).
        """
        fields = [
            "add_fail_fi_index",
            "properties_fail_fi_index",
            "configure_fail_fi_index",
        ]

        used: set[int] = set()
        for name in fields:
            val = getattr(self.plan, name)
            if val is None:
                continue

            if val not in used:
                used.add(val)
                continue

            # Collision: reroll up to 5 times
            for _ in range(5):
                new_val = self.rng.randrange(self.total_fields)
                if new_val not in used:
                    setattr(self.plan, name, new_val)
                    used.add(new_val)
                    break

    # -----------------------
    # Runtime decision helpers
    # -----------------------

    def should_fail_add(self, fi_index: int, *, consume: bool = False) -> bool:
        """
        Return True if we should force an ADD failure on this fi_index.

        consume=False (default):
          - will return True every time you ask for the target index.
          - good if your add step has its own retry_once logic and you want to
            force the final "add failed" record to be created.

        consume=True:
          - will only fail once, then stop failing.
          - useful if you want to simulate intermittent flakes.
        """
        if not self.enabled or self.plan.add_fail_fi_index is None:
            return False
        if self.plan.add_fail_fi_index != fi_index:
            return False

        if consume:
            if self._add_consumed:
                return False
            self._add_consumed = True

        return True

    def should_fail_properties(self, fi_index: int, *, consume: bool = False) -> bool:
        if not self.enabled or self.plan.properties_fail_fi_index is None:
            return False
        if self.plan.properties_fail_fi_index != fi_index:
            return False

        if consume:
            if self._properties_consumed:
                return False
            self._properties_consumed = True

        return True

    def should_fail_configure(self, fi_index: int, *, consume: bool = False) -> bool:
        if not self.enabled or self.plan.configure_fail_fi_index is None:
            return False
        if self.plan.configure_fail_fi_index != fi_index:
            return False

        if consume:
            if self._configure_consumed:
                return False
            self._configure_consumed = True

        return True