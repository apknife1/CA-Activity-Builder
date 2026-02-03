# src/ca_bldr/field_configs.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Literal, List, Dict, Tuple


# ---------------------------------------------------------------------------
# Common type aliases
# ---------------------------------------------------------------------------

LearnerVisibility = Literal["hidden", "read", "update", "read-on-submit"]
AssessorVisibility = Literal["hidden", "read", "update"]
SignerRole = Literal["learner", "assessor", "both"]

# For now keep marking type open-ended; we can later restrict to Literals if desired.
MarkingType = str  # e.g. "manual", "model_answer", "ai_assisted", ...


# ---------------------------------------------------------------------------
# Base config (shared knobs)
# ---------------------------------------------------------------------------

@dataclass
class BaseFieldConfig:
    """
    Common configuration knobs for most field types.

    Any attribute left as None means "do not change this setting"
    when the editor applies the config.
    """
    title: Optional[str] = None
    body_html: Optional[str] = None  # HTML for Froala/body where applicable

    hide_in_report: Optional[bool] = None

    learner_visibility: Optional[LearnerVisibility] = None
    assessor_visibility: Optional[AssessorVisibility] = None


# ---------------------------------------------------------------------------
# Question-like fields (short/long answer, file upload, etc.)
# ---------------------------------------------------------------------------

@dataclass
class QuestionFieldConfig(BaseFieldConfig):
    """
    Shared configuration for question-style fields that can be marked and
    may have model answers, assessor comments, etc.
    """
    required: Optional[bool] = None

    # Marking / feedback
    marking_type: Optional[MarkingType] = None         # e.g. "manual", "model_answer"
    model_answer_html: Optional[str] = None            # HTML for model answer body
    enable_assessor_comments: Optional[bool] = None            # Plain text or simple HTML


@dataclass
class ParagraphConfig(BaseFieldConfig):
    """
    Paragraph / content-only block.

    Typically just title + body + visibility/hide_in_report.
    """
    # Currently no extra options beyond BaseFieldConfig,
    # but we keep a dedicated class for clarity & future growth.
    pass


@dataclass
class LongAnswerConfig(QuestionFieldConfig):
    """
    Long answer question (designer__field--text_area).
    """
    # For now this is identical to QuestionFieldConfig, but
    # we keep a dedicated class to allow future long-answer-specific options.
    pass


@dataclass
class ShortAnswerConfig(QuestionFieldConfig):
    """
    Short answer question (designer__field--text_field).

    Some marking options (e.g. model_answer_html) may or may not be used in the UI;
    the editor will simply ignore unsupported knobs.
    """
    pass


@dataclass
class FileUploadConfig(QuestionFieldConfig):
    """
    File upload question (designer__field--upload).

    In future we might add options like allowed file types, max file count, etc.
    """
    # Example future options:
    # allowed_extensions: Optional[List[str]] = None
    # max_files: Optional[int] = None
    pass

@dataclass
class SingleChoiceConfig(QuestionFieldConfig):
    """
    Single choice (Auto marked) field.

    options:
      - list of option labels (in order)
    correct_index:
      - which option is marked as correct (0-based)
      - for AR outcome we can set it to None to avoid "correct answer" semantics,
        but CA appears to require at least one correct answer (badge shows that).
    """
    options: Optional[List[str]] = None
    correct_index: Optional[int] = None

# ---------------------------------------------------------------------------
# Table / interactive table configs
# ---------------------------------------------------------------------------

@dataclass
class TableCellConfig:
    """
    Optional per-cell configuration.

    This is a fine-grained override; in many cases you won't need this and
    will instead use column-level settings + simple row labels.
    """
    text: Optional[str] = None         # visible label / content in the cell
    cell_type: Optional[str] = None    # e.g. "heading", "text", "text_field", "date_field", "checkbox"


@dataclass
class TableConfig(QuestionFieldConfig):
    """
    Configuration for an interactive table (designer__field--table).

    The editor will interpret these as "desired shape" and "desired types/labels".
    All attributes are optional; None means "leave as-is".
    """
    # Desired shape of the body (not counting the header row).
    rows: Optional[int] = None
    cols: Optional[int] = None  # number of data columns (excluding control column)

    # High-level description of table contents.
    # If provided, the editor can set first-column labels and column headers.
    row_labels: Optional[List[str]] = None          # One label per body row (e.g. element descriptions)
    column_headers: Optional[List[str]] = None      # Header labels for each data column
    column_types: Optional[List[str]] = None        # Per-column type, e.g. ["text", "checkbox", "checkbox", "checkbox"]

    # Optional per-cell overrides: mapping from (row_index, col_index) -> TableCellConfig.
    # row_index and col_index are 0-based body row / data-column indices.
    cell_overrides: Dict[Tuple[int, int], TableCellConfig] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Future field types (signature, date picker, etc.)
# ---------------------------------------------------------------------------

@dataclass
class SignatureConfig(BaseFieldConfig):
    """
    Signature pad (designer__field--signature, when we add it).

    For now this is just a placeholder inheriting BaseFieldConfig; we can
    add specific options (e.g. required, label text) as we explore the UI.

        role:
      - 'learner'  → learner signs, assessor can view
      - 'assessor' → assessor signs, learner can view (or be hidden)
      - 'both'     → both can sign/update (if you ever want that)

    If learner_visibility / assessor_visibility are explicitly set on the
    config, they override the role-based defaults.
    """
    required: Optional[bool] = None
    role: Optional[SignerRole] = None


@dataclass
class DatePickerConfig(BaseFieldConfig):
    """
    Date picker field (designer__field--date_field, when we add it).

    - required: whether the learner must supply a date.
    """
    required: Optional[bool] = None

