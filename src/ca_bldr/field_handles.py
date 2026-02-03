# ca_bldr/field_handles.py
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class FieldHandle:
    """
    Stable reference to a CloudAssess field inside a section.
    """
    field_id: str        # e.g. "27435179"
    section_id: str      # e.g. "1706532"
    field_type_key: str  # e.g. "short_answer", "paragraph"
    fi_index: Optional[int] = None 
    index_hint: Optional[int] = None  # optional: position in section at creation time
    index: Optional[int] = None   # 0-based index in the section at creation/snapshot
    title: Optional[str] = None   # visible title text at creation/snapshot