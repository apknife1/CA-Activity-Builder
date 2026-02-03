# src/ca_bldr/spec_reader.py

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, Sequence
import html

import yaml  # ensure PyYAML is in requirements.txt


@dataclass
class FieldInstruction:
    """
    A single field to build within an activity/section.

    For now we keep this generic and stash our derived details in raw_component.
    Builder/Editor code will later interpret raw_component into concrete configs.
    """
    field_key: str                 # e.g. "paragraph", "long_answer", "interactive_table"
    section_title: Optional[str]   # logical section title this belongs to
    section_index: Optional[int]   # numeric index if known
    raw_component: Dict[str, Any]  # derived + original info


@dataclass
class ActivityInstruction:
    """
    An activity to build/edit.
    """
    source_path: Path
    activity_code: Optional[str] = None
    activity_title: Optional[str] = None
    unit_code: Optional[str] = None
    unit_title: Optional[str] = None
    activity_type: Optional[str] = None
    fields: List[FieldInstruction] = field(default_factory=list)

    def __post_init__(self):
        if self.fields is None:
            self.fields = []


@dataclass(frozen=True)
class ActivityTypeInfo:
    abbr: str
    proper: str

ACTIVITY_TYPE_INFO: Dict[str, ActivityTypeInfo] = {
    "written_assessment": ActivityTypeInfo("WA", "Written Assessment"),
    "assessment_result": ActivityTypeInfo("AR", "Assessment Result"),
    "competency_conversation": ActivityTypeInfo("CC", "Competency Conversation"),
    "industry_evidence": ActivityTypeInfo("IE", "Industry Evidence"),
}

@dataclass(frozen=True)
class FieldDefaults:
    hide_in_report: bool
    learner_visibility: str
    assessor_visibility: str
    required: Optional[bool] = None
    marking_type: Optional[str] = None
    enable_assessor_comments: Optional[bool] = None

ACTIVITY_FIELD_DEFAULTS: Dict[str, Dict[str, FieldDefaults]] = {
    "written_assessment": {
        "paragraph": FieldDefaults(
            hide_in_report=False,
            learner_visibility="read",
            assessor_visibility="read",
        ),
        "long_answer": FieldDefaults(
            hide_in_report=False,
            learner_visibility="update",
            assessor_visibility="read",
            required=True,
            marking_type="manual",
            enable_assessor_comments=True,
        ),
        # later: interactive_table, file_upload, etc.
    },
    "competency_conversation": {
        "paragraph": FieldDefaults(
            hide_in_report=False,
            learner_visibility="read",
            assessor_visibility="read",
        ),
        "long_answer": FieldDefaults(
            hide_in_report=False,
            learner_visibility="read",
            assessor_visibility="update",
            required=True,
            marking_type="manual",
            enable_assessor_comments=True,
        ),
        "date_field": FieldDefaults(
            hide_in_report=False,
            learner_visibility="read",
            assessor_visibility="update",
            required=True,
        ),
        "signature": FieldDefaults(
            hide_in_report=False,
            learner_visibility="read",
            assessor_visibility="update",
            required=True,
        ),
    },
    "industry_evidence": {
        "paragraph": FieldDefaults(
            hide_in_report=False,
            learner_visibility="read",
            assessor_visibility="read",
        ),
        "file_upload": FieldDefaults(
            hide_in_report=False,
            learner_visibility="update",
            assessor_visibility="read",
            required=True,
            marking_type="manual",
            enable_assessor_comments=True,
        ),
        "interactive_table": FieldDefaults(
            hide_in_report=False,
            learner_visibility="update",
            assessor_visibility="read",
            # leave required/marking unset for tables by default
        ),
        "long_answer": FieldDefaults(
            hide_in_report=False,
            learner_visibility="update",
            assessor_visibility="read",
            required=True,
            marking_type="manual",
            enable_assessor_comments=True,
        ),
        "date_field": FieldDefaults(
            hide_in_report=False,
            learner_visibility="update",
            assessor_visibility="read",
            required=True,
        ),
        "signature": FieldDefaults(
            hide_in_report=False,
            learner_visibility="update",
            assessor_visibility="read",
            required=True,
        ),
    },
    "assessment_result": {
        "paragraph": FieldDefaults(
            hide_in_report=False,
            learner_visibility="hidden",
            assessor_visibility="read",
        ),
        "long_answer": FieldDefaults(
            hide_in_report=False,
            learner_visibility="hidden",
            assessor_visibility="update",
            required=False,                 # let assessor decide
            marking_type="not marked",      # or None if you prefer
            enable_assessor_comments=False,
        ),
        "interactive_table": FieldDefaults(
            hide_in_report=False,
            learner_visibility="hidden",
            assessor_visibility="update",
        ),
        "signature": FieldDefaults(
            hide_in_report=False,
            learner_visibility="hidden",
            assessor_visibility="update",
            required=True,
        ),
        "date_field": FieldDefaults(
            hide_in_report=False,
            learner_visibility="hidden",
            assessor_visibility="update",
            required=True,
        ),
        "single_choice": FieldDefaults(
            hide_in_report=False,
            learner_visibility="hidden",
            assessor_visibility="update",
            required=True,
            marking_type="not marked",
            enable_assessor_comments=None,
        ),        
    }
}


class SpecReader:
    """
    Read YAML/JSON spec files (or folders) and convert them into a normalised
    list of ActivityInstruction objects.

    This does NOT talk to Selenium or CA – it only parses specs and maps them
    into a "what to build" structure.
    """

    def __init__(self, logger):
        self.logger = logger

    # ---------- public API ----------

    def read_path(self, path: Union[str, Path]) -> List[ActivityInstruction]:
        """
        Read a single file OR a directory.

        - If it's a file: parse YAML/JSON and return activity instructions.
        - If it's a directory: read all *.yml/*.yaml/*.json in it (non-recursive).
        """
        p = Path(path)
        if p.is_dir():
            return self._read_directory(p)
        if p.is_file():
            return self._read_file(p)
        raise FileNotFoundError(f"Spec path not found: {p}")

    # ---------- internal helpers ----------

    def _read_directory(self, dir_path: Path) -> List[ActivityInstruction]:
        activities: List[ActivityInstruction] = []

        for ext in ("*.yml", "*.yaml", "*.json"):
            for file in dir_path.glob(ext):
                if self.logger:
                    self.logger.info("Reading spec file: %s", file)
                activities.extend(self._read_file(file))

        if self.logger:
            self.logger.info(
                "Loaded %d activity instruction(s) from directory %s",
                len(activities),
                dir_path,
            )

        return activities

    def _read_file(self, file_path: Path) -> List[ActivityInstruction]:
        data = self._load_raw(file_path)

        activities: List[ActivityInstruction] = []

        # RPL-unit style: unit_code/unit_title/activity_type present
        if isinstance(data, dict) and {
            "unit_code",
            "unit_title",
            "activity_type",
        }.issubset(data.keys()):
            activities.append(
                self._activity_from_unit_dict(data, source_path=file_path)
            )
        # Generic multi-activity style (if you end up using it later)
        elif isinstance(data, dict) and "activities" in data:
            for act in data["activities"]:
                activities.append(
                    self._activity_generic(act, source_path=file_path)
                )
        else:
            # Single generic activity
            activities.append(
                self._activity_generic(data, source_path=file_path)
            )

        if self.logger:
            self.logger.info(
                "Read %d activity instruction(s) from %s",
                len(activities),
                file_path,
            )
        return activities

    def _load_raw(self, file_path: Path) -> Any:
        suffix = file_path.suffix.lower()
        with file_path.open("r", encoding="utf-8") as f:
            if suffix in (".yml", ".yaml"):
                return yaml.safe_load(f)
            elif suffix == ".json":
                return json.load(f)
            else:
                raise ValueError(f"Unsupported spec file extension: {suffix}")

    # ---------- RPL written assessment path ----------
    def _get_field_defaults(
        self,
        activity_type: str,
        field_key: str,
        *,
        hide_in_report: Optional[bool] = None,
        learner_visibility: Optional[str] = None,
        assessor_visibility: Optional[str] = None,
        required: Optional[bool] = None,
        marking_type: Optional[str] = None,
        enable_assessor_comments: Optional[bool] = None,
    ) -> FieldDefaults:
        base = ACTIVITY_FIELD_DEFAULTS.get(activity_type, {}).get(
            field_key,
            FieldDefaults(
                hide_in_report=False,
                learner_visibility="read",
                assessor_visibility="read",
            ),
        )

        # Return a new FieldDefaults (do not mutate base)
        return FieldDefaults(
            hide_in_report=hide_in_report if hide_in_report is not None else base.hide_in_report,
            learner_visibility=learner_visibility if learner_visibility is not None else base.learner_visibility,
            assessor_visibility=assessor_visibility if assessor_visibility is not None else base.assessor_visibility,
            required=required if required is not None else base.required,
            marking_type=marking_type if marking_type is not None else base.marking_type,
            enable_assessor_comments=(
                enable_assessor_comments if enable_assessor_comments is not None else base.enable_assessor_comments
            ),
        )
    
    def _inject_defaults(self, raw: dict, defaults: FieldDefaults) -> dict:
        raw.setdefault("hide_in_report", defaults.hide_in_report)
        raw.setdefault("learner_visibility", defaults.learner_visibility)
        raw.setdefault("assessor_visibility", defaults.assessor_visibility)

        if defaults.required is not None:
            raw.setdefault("required", defaults.required)
        if defaults.marking_type is not None:
            raw.setdefault("marking_type", defaults.marking_type)
        if defaults.enable_assessor_comments is not None:
            raw.setdefault("enable_assessor_comments", defaults.enable_assessor_comments)

        return raw

    def _activity_from_unit_dict(
        self,
        data: Dict[str, Any],
        source_path: Path,
    ) -> ActivityInstruction:
        """
        Handle the specific RPL written_assessment YAML structure:

        unit_code, unit_title, activity_type, instructions, sections, marking_guide
        """
        unit_code = data.get("unit_code")
        unit_title = data.get("unit_title")
        activity_type = data.get("activity_type")

        # Activity naming
        info: ActivityTypeInfo | None = ACTIVITY_TYPE_INFO.get(activity_type or "")
        abbr = info.abbr if info else (activity_type or "")
        proper = info.proper if info else " ".join(w.capitalize() for w in (activity_type or "").split("_"))

        activity_code = f"{unit_code}RPL-{abbr}" if unit_code and abbr else None
        activity_title = (
            f"{unit_title} - RPL - {proper}"
            if unit_title and proper
            else None
        )

        act = ActivityInstruction(
            source_path=source_path,
            activity_code=activity_code,
            activity_title=activity_title,
            unit_code=unit_code,
            unit_title=unit_title,
            activity_type=activity_type,
        )

        # ---- 1. Information section (top-of-activity instructions) ----
        key = (activity_type or "").strip().lower()
        info_block = data.get("information") or {}
        info_instructions = None

        req = data.get("requirements") or {}
        elements_raw = req.get("elements") or []

        if isinstance(elements_raw, dict):
            # preserve insertion order as given by YAML loader
            unit_elements = [v for v in elements_raw.values() if v]
        elif isinstance(elements_raw, list):
            unit_elements = [x for x in elements_raw if x]
        else:
            unit_elements = []

        if isinstance(info_block, dict):
            # Primary: modern structure
            info_instructions = info_block.get("instructions")

            # Backward-compat: if you ever had candidate_notes here
            if not info_instructions:
                info_instructions = info_block.get("candidate_notes")

        defaults = self._get_field_defaults(key or "", "paragraph")

        if unit_code and unit_title and (unit_elements or key == "assessment_result"):
            intro_html = self._build_rpl_intro_html(
                unit_code,
                unit_title,
                unit_elements or [],
                activity_type=key or "",
                activity_type_proper=proper,
                )            
            raw = {
                "type": "paragraph",
                "source": "information_intro",
                "title": "Introduction",
                "body_html": intro_html,
            }
            raw_component = self._inject_defaults(raw, defaults)
            intro_field = FieldInstruction(
                field_key="paragraph",
                section_title="Information",
                section_index=0,
                raw_component=raw_component,
            )
            act.fields.append(intro_field)

        if (key or "").strip().lower() == "competency_conversation":
            info_html = self._build_competency_conversation_info_html()
            raw = {
                "type": "paragraph",
                "source": "cc_important_information",
                "title": "Important information",
                "body_html": info_html,
            }
            raw_component = self._inject_defaults(raw, defaults)
            info_field = FieldInstruction(
                field_key="paragraph",
                section_title="Information",
                section_index=0,
                raw_component=raw_component,
            )
            act.fields.append(info_field)

        if (key or "").strip().lower() == "industry_evidence":
            blocks = self._build_industry_evidence_intro_blocks(data.get("information") or {})
            ie_defaults = self._get_field_defaults(key or "", "paragraph")
            raw = {
                "type": "paragraph",
                "source": "ie_intro_what",
                "title": "What this assessment tool is",
                "body_html": blocks["what_html"],
            }
            raw_component = self._inject_defaults(raw, ie_defaults)
            if blocks.get("what_html"):
                what_field = FieldInstruction(
                    field_key="paragraph",
                    section_title="Information",
                    section_index=0,
                    raw_component=raw_component,
                )
                act.fields.append(what_field)

            if blocks.get("points_html"):
                raw = {
                    "type": "paragraph",
                    "source": "ie_intro_points",
                    "title": "What must be covered",
                    "body_html": blocks["points_html"],
                }
                raw_component = self._inject_defaults(raw, ie_defaults)
                points_field = FieldInstruction(
                    field_key="paragraph",
                    section_title="Information",
                    section_index=0,
                    raw_component=raw_component,
                )
                act.fields.append(points_field)

            if blocks.get("notes_html"):
                raw = {
                    "type": "paragraph",
                    "source": "ie_intro_notes",
                    "title": "Notes on evidence",
                    "body_html": blocks["notes_html"],
                }
                raw_component = self._inject_defaults(raw, ie_defaults)
                notes_field = FieldInstruction(
                    field_key="paragraph",
                    section_title="Information",
                    section_index=0,
                    raw_component=raw_component,
                )
                act.fields.append(notes_field)

        if info_instructions:
            raw = {
                    "type": "paragraph",
                    "source": "information_instructions",
                    "title": "Instructions",
                    "body_html": info_instructions,
                }
            raw_component = self._inject_defaults(raw, defaults)
            info_field = FieldInstruction(
                field_key="paragraph",
                section_title="Information",   # ✅ match CA’s wording
                section_index=0,               # first logical section
                raw_component=raw_component,
            )
            act.fields.append(info_field)

        # ---- 2. Sections (instructions + questions) ----
        try:
            if key == "written_assessment":
                self._append_wa_fields(act, data)
            elif key == "competency_conversation":
                self._append_cc_fields(act, data)
            elif key == "industry_evidence":
                self._append_ie_fields(act, data)
            elif key == "assessment_result":
                self._append_ar_fields(act, data)
            else:
                self.logger.warning(
                    "Unsupported activity_type=%r; no section/question fields appended.",
                    activity_type,
                )
        except Exception as e:
            self.logger.error("Failed to append specific fields for activity: %r, with message %r", activity_title, e)
        # return amended ActivityInstruction
        return act
    
    def _build_rpl_intro_html(
            self,
            unit_code: str,
            unit_title: str,
            elements: list[str],
            *,
            activity_type: str,
            activity_type_proper: str | None = None,
        ) -> str:
        # prefer the already-derived pretty label if you have it
        pretty = activity_type_proper or " ".join(
            w.capitalize() for w in (activity_type or "").split("_")
        )
        pretty = pretty.strip() or "RPL"

        title_line = f"{unit_code} - {unit_title}".strip(" -")

        # Sentence that changes by activity_type
        key = (activity_type or "").strip().lower()

        direction_by_type: dict[str, str] = {
            "written_assessment": (
                "On the following pages, you will need to answer the knowledge questions "
                "to provide evidence of your experience and understanding of what is "
                "covered by this unit."
            ),
            "competency_conversation": (
                "This competency conversation will be led by an assessor. You may view the questions here, and your "
                "assessor will record notes and outcomes during the conversation. There is scope for extra questions "
                "to be added should the assessor determine that there is need for them."
            ),
            "industry_evidence": (
                "In this activity, you will upload and describe workplace/industry evidence that demonstrates "
                "you meet the unit requirements."
            ),
            "assessment_result": (
                "This section records the outcome of your RPL assessment for this unit. Your assessor will "
                "finalise the result once all required evidence has been reviewed. There is nothing for you "
                "to complete on this form and you will only be able to see the outcome once your assessor "
                "has finalised their marking and response."
            ),
        }

        direction = direction_by_type.get(
            key,
            "Follow the instructions on the following pages to complete this component of the RPL assessment."
        )

        # ---- Build optional elements block ----
        # For assessment_result: omit elements list entirely (it’s mapped in the table)
        elements = elements or []
        include_elements_block = (key != "assessment_result") and any(e for e in elements)

        elements_html = ""
        if include_elements_block:
            li = "".join(f"<li><em>{e}</em></li>" for e in elements if e)
            elements_html = (
                "<p>The elements of this unit are:</p>"
                f"<ol>{li}</ol>"
            )

        # ---- Assemble final HTML ----
        html = (
            "<p>Welcome to the RPL for the unit:</p>"
            "<p><br></p>"
            f"<p><strong>{title_line}</strong><br style=\"box-sizing: border-box;\">&nbsp;</p>"
            f"{elements_html}"
            f"<p>This is the <strong>{pretty}</strong> component of the RPL assessment for this unit.</p>"
            "<p><br></p>"
            f"<p>{direction}&nbsp;</p>"
            "<p><br></p><p><br></p>"
        )

        return html

    def _build_competency_conversation_info_html(self) -> str:
        paras = [
            "This document is to guide the assessor in conducting a competency conversation "
            "for the listed unit in which the candidate is attempting to achieve recognition "
            "of prior learning (RPL). This document is to be used in conjunction with a "
            "recorded video conversation with the candidate or a recorded audio conversation "
            "where the assessor and candidate are communicating face-to-face.",

            "The purpose of the competency conversation is two-fold: "
            "to confirm the work history of the candidate as suggested in their RPL application; "
            "and to ensure the candidate is equipped with the knowledge and performance "
            "capabilities to achieve RPL in the listed unit, as suggested by their work "
            "history, written assessment and industry evidence submissions.",

            "The assessor must have completely evaluated all other candidate submissions for "
            "the unit listed before undertaking the competency conversation. If the candidate "
            "is unable to adequately satisfy the assessor that their previous submissions are "
            "accurate and wholly their own (when stated as such) and that they possess the "
            "knowledge and performance capabilities to achieve RPL in the listed unit, they "
            "will be required to undergo gap training for the listed unit, if gap training is "
            "available.",

            "It is the assessor’s responsibility to explain the competency conversation "
            "process to the candidate and the expectations that need to be fulfilled to "
            "achieve RPL within the listed unit. This document contains a minimum set of "
            "conversation topics that the assessor must raise with the candidate. There is "
            "also provision for extra questions to gain clarity where the candidate’s work "
            "history, written assessment and industry evidence do not clearly confirm a "
            "required level of ability or proficiency for RPL in the listed unit.",
        ]

        html = self._text_to_html_paragraphs(paras)
        return html
    
    def _text_to_html_paragraphs(self, text: Union[str, Sequence[str]]) -> str:
        """
        Convert either:
        - a single string (with optional newlines), OR
        - a list/sequence of paragraph strings
        into concatenated <p>...</p> HTML blocks.
        """
        # Case 1: sequence of paragraph strings (but NOT a str, since str is also a Sequence)
        if not isinstance(text, str):
            parts = [html.escape((p or "").strip()) for p in text if (p or "").strip()]
            return "".join(f"<p>{p}</p>" for p in parts)

        # Case 2: single string
        safe = html.escape(text.strip())
        if not safe:
            return ""

        blocks = [b.strip() for b in safe.split("\n\n") if b.strip()]
        if not blocks:
            safe = safe.replace("\n", "<br>")
            return f"<p>{safe}</p>"

        out = []
        for b in blocks:
            out.append("<p>" + b.replace("\n", "<br>") + "</p>")

        return "".join(out)

    def _bullets_to_html(self, items: list[str]) -> str:
        """
        Render a list of bullet points as a <ul>.
        """
        clean = [html.escape((x or "").strip()) for x in (items or []) if (x or "").strip()]
        if not clean:
            return ""
        lis = "".join(f"<li>{x}</li>" for x in clean)
        return f"<ul>{lis}</ul>"

    def _build_industry_evidence_intro_blocks(self, intro: dict) -> dict[str, str]:
        """
        Build the 3 Information-section blocks for Industry Evidence:
        - What this assessment tool is
        - What must be covered (bullets)
        - Notes on evidence
        Returns: {"what_html": ..., "points_html": ..., "notes_html": ...}
        """
        self.logger.debug("Building Industry Evidence specific intro")
        what = intro.get("what_this_is") if isinstance(intro, dict) else None
        points = intro.get("must_cover_points") if isinstance(intro, dict) else None
        notes = intro.get("notes") if isinstance(intro, dict) else None

        self.logger.info(
            "IE intro blocks: what=%s points=%d notes=%s",
            bool(what),
            len(points) if isinstance(points, list) else 0,
            bool(notes),
        )

        what_html = self._text_to_html_paragraphs(what or "")
        points_html = self._bullets_to_html(points if isinstance(points, list) else [])
        notes_html = self._text_to_html_paragraphs(notes or "")

        return {"what_html": what_html, "points_html": points_html, "notes_html": notes_html}

    def _append_wa_fields(self, act: ActivityInstruction, data: Dict[str, Any]) -> None:
        activity_type = "written_assessment"
        sections = data.get("sections") or []
        for sec_index, sec in enumerate(sections, start=1):
            sec_id = sec.get("id")
            sec_title = sec.get("title")
            sec_instructions = sec.get("instructions")
            para_defaults = self._get_field_defaults(activity_type, "paragraph")

            # Section instructions -> paragraph with title "Instructions"
            if sec_instructions:
                raw = {
                    "type": "paragraph",
                    "source": "section_instructions",
                    "section_id": sec_id,
                    "title": "Instructions",   # or sec_title if you prefer
                    "body_html": sec_instructions,
                }
                raw_component = self._inject_defaults(raw, para_defaults)
                inst_field = FieldInstruction(
                    field_key="paragraph",
                    section_title=sec_title,
                    section_index=sec_index,
                    raw_component=raw_component,
                )
                act.fields.append(inst_field)

            # Questions
            for q in sec.get("questions", []):
                q_id = q.get("id")
                disp_no = q.get("display_number")
                q_text = q.get("question_text")
                cand_help = q.get("candidate_help")
                ke_source = q.get("ke_source")
                field_type = q.get("field_type", "long_answer")

                q_title = f"Question {disp_no}" if disp_no else "Question"

                q_defaults = self._get_field_defaults(activity_type, field_type)

                # 1) Candidate help as its own paragraph BEFORE the question
                if cand_help:
                    #normal paragraph defaults but hidden in report
                    defaults = self._get_field_defaults(activity_type, "paragraph", hide_in_report=True)
                    notes_title = f"Question {disp_no} Notes" if disp_no else "Question Notes"
                    raw = {
                        "type": "paragraph",
                        "source": "candidate_help",
                        "question_id": q_id,
                        "section_id": sec_id,
                        "title": notes_title,
                        "body_html": cand_help,
                    }
                    raw_component = self._inject_defaults(raw, defaults)
                    help_field = FieldInstruction(
                        field_key="paragraph",
                        section_title=sec_title,
                        section_index=sec_index,
                        raw_component=raw_component,
                    )
                    act.fields.append(help_field)

                # 2) The actual question field
                raw = {
                    "type": field_type,
                    "source": "question",
                    "question_id": q_id,
                    "section_id": sec_id,
                    "display_number": disp_no,
                    "title": q_title,
                    "body_html": q_text,
                    "model_answer_html": ke_source,  # ke_source -> model answer
                }
                raw_component = self._inject_defaults(raw, q_defaults)
                q_field = FieldInstruction(
                    field_key=field_type,
                    section_title=sec_title,
                    section_index=sec_index,
                    raw_component=raw_component,
                )
                act.fields.append(q_field)

        # ---- 3. Marking guide (table + trainer notes) ----
        mg = data.get("marking_guide") or {}
        model_points = mg.get("model_points") or []
        decision_outcomes = mg.get("decision_outcomes") or []
        notes_label = mg.get("notes_label")
        num_rows = len(model_points)+1
        num_cols = len(decision_outcomes)+1
        t_outcome_defaults = self._get_field_defaults(activity_type, "interactive_table", 
            learner_visibility='read',
            assessor_visibility='update',
        )
        la_outcome_defaults = self._get_field_defaults(activity_type, "long_answer", 
            learner_visibility='read',
            assessor_visibility='update',
            marking_type='not marked',
            enable_assessor_comments=False,
        )

        if model_points and decision_outcomes:
            # Column 0 is the criterion/row-label column (text/heading),
            # subsequent columns are the decision outcome checkboxes.
            column_headers = [""] + decision_outcomes
            column_types = ["heading"] + ["checkbox"] * len(decision_outcomes)
            raw = {
                "type": "interactive_table",
                "source": "marking_guide_table",
                "title": "Marking Guide",
                "row_labels": model_points,
                "column_headers": column_headers,
                "column_types": column_types,
                "rows": num_rows,
                "cols": num_cols,
            }
            raw_component = self._inject_defaults(raw, t_outcome_defaults)
            table_field = FieldInstruction(
                field_key="interactive_table",
                section_title="Marking Guide",
                section_index=None,
                raw_component=raw_component,
            )
            act.fields.append(table_field)

        if notes_label:
            raw = {
                "type": "long_answer",
                "source": "marking_notes",
                "title": "Assessor's Written Assessment Marking Comments",
                "body_html": "",
                "notes_label": notes_label,
            }
            raw_component = self._inject_defaults(raw, la_outcome_defaults)
            notes_field = FieldInstruction(
                field_key="long_answer",
                section_title="Marking Guide",
                section_index=None,
                raw_component=raw_component,
            )
            act.fields.append(notes_field)
       

    def _append_cc_fields(self, act: ActivityInstruction, data: Dict[str, Any]) -> None:
        activity_type = "competency_conversation"

        conv = data.get("conversation") or {}
        section_title = conv.get("section_title") or "Competency Conversation Questions"
        max_questions = int(conv.get("max_questions") or 8)
        default_questions = conv.get("default_questions") or []

        para_defaults = self._get_field_defaults(activity_type, "paragraph")
        la_defaults = self._get_field_defaults(activity_type, "long_answer")

        # optional instructions paragraph for the section
        instructions_html = (
            "<p>These questions are to be asked of the candidate during the recorded competency conversation.</p>"
            "<p><br></p>"
        )
        raw = {
            "type": "paragraph",
            "source": "cc_section_instructions",
            "title": "Instructions",
            "body_html": instructions_html,
        }
        raw_component = self._inject_defaults(raw, para_defaults)
        act.fields.append(
            FieldInstruction(
                field_key="paragraph",
                section_title=section_title,
                section_index=1,
                raw_component=raw_component,
            )
        )

        # questions (pad to max)
        for idx in range(max_questions):
            disp_no = idx + 1
            q_text = default_questions[idx] if idx < len(default_questions) else ""

            raw = {
                "type": "long_answer",
                "source": "cc_question",
                "display_number": disp_no,
                "title": f"Question {disp_no}",
                "body_html": q_text,
                # no model answer for CC
                "model_answer_html": None,
            }
            raw_component = self._inject_defaults(raw, la_defaults)

            act.fields.append(
                FieldInstruction(
                    field_key="long_answer",
                    section_title=section_title,
                    section_index=1,
                    raw_component=raw_component,
                )
            )

        self._append_competency_conversation_signoff(act)


    def _append_competency_conversation_signoff(self, act: ActivityInstruction) -> None:
        section_title = "Assessor sign-off"
        activity_type = "competency_conversation"
        d_defaults = self._get_field_defaults(activity_type, "date_field")
        s_defaults = self._get_field_defaults(activity_type, "signature")

        d_raw = {
            "type": "date_field",
            "source": "cc_signoff_date",
            "title": "Date of competency conversation",
            "required": True,
        }
        d_raw_component = self._inject_defaults(d_raw, d_defaults)

        s_raw = {
            "type": "signature",
            "source": "cc_signoff_signature",
            "title": "Assessor signature",
            "required": True,
            "role": "assessor",
        }
        s_raw_component = self._inject_defaults(s_raw, s_defaults)
        # Date field
        act.fields.append(
            FieldInstruction(
                field_key="date_field",   # <-- must match FIELD_TYPES key
                section_title=section_title,
                section_index=2,
                raw_component=d_raw_component,
            )
        )

        # Signature field
        act.fields.append(
            FieldInstruction(
                field_key="signature",     # <-- must match FIELD_TYPES key
                section_title=section_title,
                section_index=2,
                raw_component=s_raw_component,
            )
        )

    def _append_ie_fields(self, act: ActivityInstruction, data: Dict[str, Any]) -> None:
        activity_type = "industry_evidence"

        projects = data.get("projects") or []
        for sec_index, proj in enumerate(projects, start=1):
            section_title = (proj.get("title") or f"Project {sec_index}").strip()

            p_defaults = self._get_field_defaults(activity_type, "paragraph")
            # --- Preamble paragraph ---
			
            raw = {
                "type": "paragraph",
                "source": "ie_project_preamble",
                "title": "What is required",
                "body_html": "Please see the list of requirements for this project in the self-check table.",
            }

            raw_component = self._inject_defaults(raw, p_defaults)
                        
            act.fields.append(
                FieldInstruction(
                    field_key="paragraph",
                    section_title=section_title,
                    section_index=sec_index,
                    raw_component=raw_component,
                )
            )             

            # --- Upload field ---
            uploads = proj.get("uploads") or {}
            upload_help = uploads.get("helper_text") or ""
            fu_defaults = self._get_field_defaults(activity_type, "file_upload")

            raw = {
                "type": "file_upload",
                "source": "ie_project_upload",
                "title": "Upload evidence below",
                "body_html": upload_help,
            }
            raw_component = self._inject_defaults(raw, fu_defaults)

            act.fields.append(
                FieldInstruction(
                    field_key="file_upload",
                    section_title=section_title,
                    section_index=sec_index,
                    raw_component=raw_component,
                )
            )

            # --- Describe evidence paragraph ---
            desc = proj.get("describe_evidence") or {}
            intro_text = desc.get("intro_text") or ""
            
            raw = {
                "type": "paragraph",
                "source": "ie_project_describe_intro",
                "title": "Describe your evidence below",
                "body_html": intro_text,
            }
            raw_component = self._inject_defaults(raw, p_defaults)

            act.fields.append(
                FieldInstruction(
                    field_key="paragraph",
                    section_title=section_title,
                    section_index=sec_index,
                    raw_component=raw_component,
                )
            )

            # --- Job details table (short_fields) ---
            short_fields = desc.get("short_fields") or []
            t_defaults = self._get_field_defaults(activity_type, "interactive_table")

            if short_fields:
                raw = {
                    "type": "interactive_table",
                    "source": "ie_job_details_table",
                    "title": "Job details",
                    "body_html": None,
                    "rows": len(short_fields) + 1,
                    "cols": 2,
                    "row_labels": short_fields,
                    "column_headers": ["", "Detail"],
                    "column_types": ["text","text"],
                }
                raw_component = self._inject_defaults(raw, t_defaults)
                act.fields.append(
                    FieldInstruction(
                        field_key="interactive_table",
                        section_title=section_title,
                        section_index=sec_index,
                        raw_component=raw_component,
                    )
                )

            # --- Long answers (long_fields) ---
            long_fields = desc.get("long_fields") or []
            la_defaults = self._get_field_defaults(activity_type, "long_answer")

            if long_fields:
                # 1st long field becomes "Description" title + uses the text as body
                first = (long_fields[0] or "").strip()
                raw = {
                    "type": "long_answer",
                    "source": "ie_long_desc",
                    "title": "Description",
                    "body_html": self._text_to_html_paragraphs(first),
                }
                raw_component = self._inject_defaults(raw, la_defaults)
                if first:
                    act.fields.append(
                        FieldInstruction(
                            field_key="long_answer",
                            section_title=section_title,
                            section_index=sec_index,
                            raw_component=raw_component,
                        )
                    )

                # 2nd long field becomes title only
                if len(long_fields) > 1:
                    second = (long_fields[1] or "").strip()
                    raw = {
                        "type": "long_answer",
                        "source": "ie_long_other",
                        "title": second.rstrip(":").strip(),
                        "body_html": None,
                    }
                    raw_component = self._inject_defaults(raw, la_defaults)
                    if second:
                        act.fields.append(
                            FieldInstruction(
                                field_key="long_answer",
                                section_title=section_title,
                                section_index=sec_index,
                                raw_component=raw_component,
                            )
                        )

            # --- Verifier fields table ---
            verifier_fields = desc.get("verifier_fields") or []
            if verifier_fields:
                raw = {
                    "type": "interactive_table",
                    "source": "ie_verifier_table",
                    "title": "Verifier details",
                    "body_html": None,
                    "rows": len(verifier_fields) + 1,
                    "cols": 2,
                    "row_labels": verifier_fields,
                    "column_headers": ["", "Detail"],
                    "column_types": ["text", "text"],
                }
                raw_component = self._inject_defaults(raw, t_defaults)
                act.fields.append(
                    FieldInstruction(
                        field_key="interactive_table",
                        section_title=section_title,
                        section_index=sec_index,
                        raw_component=raw_component,
                    )
                )

            # --- Placeholder: verifier_required (skip + log) ---
            if proj.get("verifier_required") is True:
                self.logger.warning(
                    "Industry Evidence: verifier_required=True for section %r but no checkbox field type exists yet; skipping.",
                    section_title,
                )

            # --- Self-check table ---
            sct = proj.get("self_check_table") or {}
            rows = sct.get("rows") or []
            cols = sct.get("candidate_columns") or []
            headers = ["Requirements"] + cols

            if rows and cols:
                raw = {
                    "type": "interactive_table",
                    "source": "ie_self_check_table",
                    "title": "Self-check",
                    "body_html": None,
                    "rows": len(rows) + 1,
                    "cols": len(cols) + 1,
                    "row_labels": rows,
                    "column_headers": headers,
                    "column_types": ["heading"] + ["checkbox"] * len(cols),
                }
                raw_component = self._inject_defaults(raw, t_defaults)
                act.fields.append(
                    FieldInstruction(
                        field_key="interactive_table",
                        section_title=section_title,
                        section_index=sec_index,
                        raw_component=raw_component,
                    )
                )

            # --- Declaration paragraph ---
            decl = proj.get("declaration_text") or ""
            if decl.strip():
                raw = {
                    "type": "paragraph",
                    "source": "ie_declaration",
                    "title": "Declaration",
                    "body_html": self._text_to_html_paragraphs(decl),
                }
                raw_component = self._inject_defaults(raw, p_defaults)
                act.fields.append(
                    FieldInstruction(
                        field_key="paragraph",
                        section_title=section_title,
                        section_index=sec_index,
                        raw_component=raw_component,
                    )
                )

            # --- Date + Signature (learner role) ---
            df_defaults = self._get_field_defaults(activity_type, "date_field")
            raw = {
                "type": "date_field",
                "source": "ie_date",
                "title": "Date",
                "body_html": None,
            }
            raw_component = self._inject_defaults(raw, df_defaults)
            act.fields.append(
                FieldInstruction(
                    field_key="date_field",
                    section_title=section_title,
                    section_index=sec_index,
                    raw_component=raw_component,
                )
            )

            sig_defaults = self._get_field_defaults(activity_type, "signature")
            raw = {
                "type": "signature",
                "source": "ie_signature",
                "title": "Signature",
                "body_html": None,
                "role": "learner",
            }
            raw_component = self._inject_defaults(raw, sig_defaults)
            act.fields.append(
                FieldInstruction(
                    field_key="signature",
                    section_title=section_title,
                    section_index=sec_index,
                    raw_component=raw_component,
                )
            )

        # --- Assessor section ---
        assessor = data.get("assessor_section") or {}
        if assessor:
            assessor_title = "Assessor"
            assessor_index = len(projects) + 1

            coverage = assessor.get("coverage_checks") or []
            decisions = assessor.get("decision_options") or []
            notes_label = (assessor.get("notes_label") or "").strip()

            # Assessor-only responses
            # Learner can see but not touch
            assess_t_defaults = self._get_field_defaults(activity_type, "interactive_table", assessor_visibility="update", learner_visibility="read")
            assess_la_defaults = self._get_field_defaults(activity_type, "long_answer", assessor_visibility="update", learner_visibility="read", marking_type="not marked")

            if coverage:
                raw = {
                    "type": "interactive_table",
                    "source": "ie_assessor_coverage",
                    "title": "Evidence Requirements",
                    "rows": len(coverage) + 1,
                    "cols": 2,
                    "row_labels": coverage,
                    "column_headers": ["", "Sighted Evidence"],
                    "column_types": ["text", "checkbox"],
                }
                raw_component = self._inject_defaults(raw, assess_t_defaults)
                act.fields.append(
                    FieldInstruction(
                        field_key="interactive_table",
                        section_title=assessor_title,
                        section_index=assessor_index,
                        raw_component=raw_component,
                    )
                )

            if decisions:
                raw = {
                    "type": "interactive_table",
                    "source": "ie_assessor_decision",
                    "title": "Decision",
                    "rows": len(decisions) + 1,
                    "cols": 2,
                    "row_labels": decisions,
                    "column_headers": ["Decision", "Outcome"],
                    "column_types": ["text", "checkbox"],
                }
                raw_component = self._inject_defaults(raw, assess_t_defaults)
                act.fields.append(
                    FieldInstruction(
                        field_key="interactive_table",
                        section_title=assessor_title,
                        section_index=assessor_index,
                        raw_component=raw_component,
                    )
                )

            raw = {
                "type": "long_answer",
                "source": "ie_assessor_comments",
                "title": "Assessor's Industry Evidence Marking Comments",
                "body_html": self._text_to_html_paragraphs(notes_label) if notes_label else None,
            }
            raw_component = self._inject_defaults(raw, assess_la_defaults)
            act.fields.append(
                FieldInstruction(
                    field_key="long_answer",
                    section_title=assessor_title,
                    section_index=assessor_index,
                    raw_component=raw_component,
                )
            )

    def _append_ar_fields(self, act: ActivityInstruction, data: Dict[str, Any]) -> None:
        """
        Assessment Result (AR):
        - Assessor-only determination section (outcome + reasons + sign-off)
        - One big Mapping Guide table (for now)

        YAML expected (example_ar.yml):
        mapping:
            performance_evidence: [ { stem, lines: [{text, evidence_sources}...] }... ]
            elements_pcs: [ { element, pc, text, evidence_sources }... ]
            knowledge_evidence: [ { text, evidence_sources }... ]
        """
        activity_type = "assessment_result"

        # Defaults (optional to use; we still override vis to assessor-only)
        p_defaults = self._get_field_defaults(activity_type, "paragraph")
        la_defaults = self._get_field_defaults(activity_type, "long_answer")
        sc_defaults = self._get_field_defaults(activity_type, "single_choice")
        d_defaults = self._get_field_defaults(activity_type, "date_field")
        s_defaults = self._get_field_defaults(activity_type, "signature")
        t_defaults = self._get_field_defaults(activity_type, "interactive_table")

        # -----------------------------
        # Section 1: Assessment result
        # -----------------------------
        sec_title = "Assessment result"
        sec_index = 1

        # Minimal, hard-coded declaration text for now (move into YAML later if you want)
        unit_code = (data.get("unit_code") or act.unit_code or "").strip()
        unit_title = (data.get("unit_title") or act.unit_title or "").strip()

        decl_parts = [
            "This document is used by the assessor to record the outcome of the RPL assessment for this unit of competency. "
            "It must be read together with the candidate's RPL application, written assessment, industry evidence, "
            "competency conversation and any assessor-observed evidence.",
            "The assessor must be satisfied that the candidate has provided sufficient, valid, current and authentic evidence "
            "to meet all requirements of the unit of competency, including performance evidence, knowledge evidence and "
            "elements and performance criteria.",
            "Where the assessor determines that evidence is incomplete or not to the required standard, the candidate will be "
            "required to complete gap training and reassessment for this unit if gap training is available.",
        ]
        decl_html = self._text_to_html_paragraphs(decl_parts)

        raw = {
                    "type": "paragraph",
                    "source": "ar_assessor_declaration",
                    "title": f"{unit_code} RPL Assessment of Candidate Sufficiency".strip() or "RPL Assessment of Candidate Sufficiency",
                    "body_html": decl_html,
                }
        raw_component = self._inject_defaults(raw, p_defaults)
        act.fields.append(
            FieldInstruction(
                field_key="paragraph",
                section_title=sec_title,
                section_index=sec_index,
                raw_component=raw_component,
            )
        )

        # Overall outcome: single choice (Auto marked) with 2 options
        outcome_options = [
            "RPL applicable",
            "RPL denied (gap training required)",
        ]
        raw = {
            "type": "single_choice",
            "source": "ar_overall_outcome",
            "title": "Overall RPL outcome for this unit",
            # optional: keep a short instruction (uses Froala description area)
            "body_html": self._text_to_html_paragraphs([
                "Select the overall RPL outcome for this unit."
            ]),
            # Single choice specifics (handled by editor)
            "options": outcome_options,
            # CA complains if no correct answers are selected.
            # We treat "correct" as the selected outcome default.
            # If you want no default, we can try None later, but expect the CA badge to complain.
            "correct_index": 0,
        }
        raw_component = self._inject_defaults(raw, sc_defaults)
        act.fields.append(
            FieldInstruction(
                field_key="single_choice",
                section_title=sec_title,
                section_index=sec_index,
                raw_component=raw_component,
            )
        )

        # Reasons / determination notes (assessor writes)
        raw = {
            "type": "long_answer",
            "source": "ar_assessor_reasons",
            "title": "Assessor reason(s) for determination",
            "body_html": None,
        }
        raw_component = self._inject_defaults(raw, la_defaults)
        act.fields.append(
            FieldInstruction(
                field_key="long_answer",
                section_title=sec_title,
                section_index=sec_index,
                raw_component=raw_component,
            )
        )

        # Sign-off
        raw = {
            "type": "date_field",
            "source": "ar_signoff_date",
            "title": "Date",
        }
        raw_component = self._inject_defaults(raw, d_defaults)
        act.fields.append(
            FieldInstruction(
                field_key="date_field",
                section_title=sec_title,
                section_index=sec_index,
                raw_component=raw_component,
            )
        )
        raw = {
            "type": "signature",
            "source": "ar_signoff_signature",
            "title": "Assessor signature",
            "role": "assessor",
        }
        raw_component = self._inject_defaults(raw, s_defaults)
        act.fields.append(
            FieldInstruction(
                field_key="signature",
                section_title=sec_title,
                section_index=sec_index,
                raw_component=raw_component,
            )
        )

        # --------------------------------
        # Section 2: Mapping guide (table)
        # --------------------------------
        mapping = data.get("mapping") or {}

        pe_list = mapping.get("performance_evidence") or []
        epc_list = mapping.get("epc") or mapping.get("elements_pcs") or []
        ke_list = mapping.get("ke") or mapping.get("knowledge_evidence") or []

        # Flatten mapping items into row labels + evidence sources for col 3
        row_labels: list[str] = []
        evidence_by_row: list[str] = []  # joined sources aligned to row_labels

        def add_row(label: str, sources: list[str] | None):
            clean_label = (label or "").strip()
            if not clean_label:
                return
            row_labels.append(clean_label)
            srcs = [s for s in (sources or []) if (s or "").strip()]
            evidence_by_row.append(", ".join(srcs))

        # Performance Evidence
        if pe_list:
            add_row("Performance evidence", [])
            for group in pe_list:
                stem = (group.get("stem") or "").strip()
                if stem:
                    add_row(f"— {stem}", [])
                for line in (group.get("lines") or []):
                    text = (line.get("text") or "").strip()
                    sources = line.get("evidence_sources") or []
                    add_row(text, sources)

        # Elements + PCs
        if epc_list:
            add_row("Elements and performance criteria", [])
            for epc in epc_list:
                element = epc.get("element")
                pc = epc.get("pc")
                text = (epc.get("text") or "").strip()
                sources = epc.get("evidence_sources") or []

                prefix = ""
                if element is not None and pc is not None:
                    prefix = f"Element {element}, PC {pc}: "
                elif element is not None:
                    prefix = f"Element {element}: "

                add_row(prefix + text if text else prefix.strip(), sources)

        # Knowledge Evidence
        if ke_list:
            add_row("Knowledge evidence", [])
            for ke in ke_list:
                text = (ke.get("text") or "").strip()
                sources = ke.get("evidence_sources") or []
                add_row(text, sources)

        # Build table with cell_overrides to populate Evidence source column (index 3)
        # Columns: 0 outcome text, 1 Full, 2 Part, 3 Evidence source, 4 Notes
        cell_overrides: dict[str, str] = {}
        for r_idx, src in enumerate(evidence_by_row, start=1):  # table body rows start at 1
            if src:
                cell_overrides[f"{r_idx},3"] = src

        # Only append mapping table if we have rows
        if row_labels:
            raw = {
                "type": "interactive_table",
                "source": "ar_mapping_guide",
                "title": f"RPL Mapping Guide – {unit_code} {unit_title}".strip(" -") or "RPL Mapping Guide",
                "rows": len(row_labels) + 1,
                "cols": 5,
                "row_labels": row_labels,
                "column_headers": ["Package outcome", "Full", "Part", "Evidence source", "Notes"],
                "column_types": ["text", "checkbox", "checkbox", "text", "text"],
                "cell_overrides": cell_overrides,
            }
            raw_component = self._inject_defaults(raw, t_defaults)
            act.fields.append(
                FieldInstruction(
                    field_key="interactive_table",
                    section_title="RPL Mapping Guide",
                    section_index=sec_index + 1,
                    raw_component=raw_component,
                )
            )

    # ---------- generic fallback (not used much yet, but kept for future) ----------

    def _activity_generic(
        self,
        data: Dict[str, Any],
        source_path: Path,
    ) -> ActivityInstruction:
        act_code = data.get("activity_code") or data.get("code")
        act_title = data.get("activity_title") or data.get("title")

        act = ActivityInstruction(
            source_path=source_path,
            activity_code=act_code,
            activity_title=act_title,
        )
        # You can extend this later for generic specs.
        return act
