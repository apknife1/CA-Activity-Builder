from __future__ import annotations
from typing import TypedDict, NotRequired, Any, Literal
from enum import Enum
from dataclasses import dataclass

class ExpectedInfo(TypedDict):
    field_id: str | None
    title: str | None
    section_id: str | None

class FieldSettingsTabInfo(TypedDict):
    present: int | None
    displayed: bool | None

class FieldSettingsFrameInfo(TypedDict):
    present: int | None
    controls: int | None
    html_snippet: NotRequired[str]  # only sometimes included

class UIProbeSnapshot(TypedDict):
    label: str
    expected: ExpectedInfo
    observed_field_id: str | None
    active_element: str | None
    field_settings_tab: FieldSettingsTabInfo
    field_settings_frame: FieldSettingsFrameInfo
    froala_tooltips: int | None
    field_class: str | None

class ActivityStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED_EXISTING = "skipped_existing"
    ABORTED = "aborted"

FailureKind = Literal["add", "configure", "properties", "table_resize", "unknown"]

class FailureRecord(TypedDict):
    activity_code: str
    kind: FailureKind
    reason: str | None
    retryable: bool
    requested: dict[str, Any]     # for properties failures; empty dict otherwise
    field_key: str
    field_type_key: str | None
    field_id: str | None
    insert_after_field_id: str | None
    section_id: str | None
    section_title: str | None
    section_index: int | None
    source: str | None
    title: str | None
    fi_index: NotRequired[int]    # strongly recommended for stable retry ordering
    attempts: NotRequired[int]
    last_error: NotRequired[str]

#--- Dataclasses ---
@dataclass(frozen=True)
class TemplateMatch:
    title: str
    code: str | None
    href: str
    template_id: str | None
    status: str  # "active" or "inactive"