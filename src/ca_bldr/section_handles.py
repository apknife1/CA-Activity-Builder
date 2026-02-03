from dataclasses import dataclass

@dataclass(frozen=True)
class SectionHandle:
    """
    Stable reference to a CloudAssess section within an activity builder.
    """
    section_id: str            # e.g. "1706532"
    title: str | None = None   # visible title in sidebar (if any)
    index: int | None = None   # position in the sections list
