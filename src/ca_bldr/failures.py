from __future__ import annotations
from typing import Any, TypedDict
from .types import FailureRecord, FailureKind

# --- TypedDicts ---
class RetryResults(TypedDict):
    passes: int
    attempted: int
    fixed: int
    remaining: int
    failures_remaining: list[FailureRecord]
    fixed_records: list[FailureRecord]
    retry_errors: list[str]

# --- functions and defs ---
def make_failure_record(
    *,
    activity_code: str,
    field_key: str,
    section_title: str | None,
    section_index: int | None,
    source: str | None,
    title: str | None,

    kind: FailureKind = "unknown",
    reason: str | None = None,
    retryable: bool = False,
    requested: dict[str, Any] | None = None,

    field_type_key: str | None = None,
    field_id: str | None = None,
    insert_after_field_id: str | None = None,
    section_id: str | None = None,
    fi_index: int | None = None,

    attempts: int | None = None,
    last_error: str | None = None,
) -> FailureRecord:
    rec: FailureRecord = {
        "activity_code": activity_code,
        "kind": kind,
        "reason": reason,
        "retryable": retryable,
        "requested": requested or {},

        "field_key": field_key,
        "field_type_key": field_type_key,
        "field_id": field_id,
        "insert_after_field_id": insert_after_field_id,

        "section_id": section_id,
        "section_title": section_title,
        "section_index": section_index,

        "source": source,
        "title": title,
    }
    if fi_index is not None:
        rec["fi_index"] = fi_index
    if attempts is not None:
        rec["attempts"] = attempts
    if last_error is not None:
        rec["last_error"] = last_error
    return rec