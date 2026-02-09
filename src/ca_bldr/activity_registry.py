from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, cast

from .instrumentation import Cat
from .section_handles import SectionHandle
from .field_handles import FieldHandle
from .session import CASession


@dataclass
class SectionRecord:
    """
    In-memory record for a section: handle + the field handles it contains.
    """
    handle: SectionHandle
    fields: List[FieldHandle] = field(default_factory=list)


class ActivityRegistry:
    """
    Ephemeral 'database' of sections and fields for a single Activity Builder page.

    - Updated as we create/select sections and fields.
    - Can be reconstructed later from the DOM if needed.
    """

    def __init__(self, *, session: CASession | None = None) -> None:
        # section_id -> SectionRecord
        self._sections: Dict[str, SectionRecord] = {}
        # field_id -> FieldHandle
        self._fields: Dict[str, FieldHandle] = {}
        self._session = session

    def _handle_ctx(self, handle: FieldHandle) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "sec": handle.section_id or "",
            "fid": handle.field_id,
            "type": handle.field_type_key,
        }
        if handle.fi_index is not None:
            ctx["fi"] = handle.fi_index
        return ctx

    def _emit_signal(self, msg: str, *, level: str | int = "info", **ctx: Any) -> None:
        if self._session:
            self._session.emit_signal(Cat.REG, msg, level=level, **ctx)

    def _emit_diag(self, msg: str, *, key: str | None = None, every_s: float | None = None, **ctx: Any) -> None:
        if self._session:
            self._session.emit_diag(Cat.REG, msg, key=key, every_s=every_s, **ctx)

    def _inc_counter(self, key: str, n: int = 1) -> None:
        if self._session:
            self._session.counters.inc(key, n)

    def stats(self) -> tuple[int, int]:
        return len(self._sections), len(self._fields)

    # --- sections ---

    def add_or_update_section(self, handle: SectionHandle) -> None:
        """
        Insert or update a section record. If the section already exists, update
        its handle (e.g. new title or index) but preserve its fields list.
        """
        if not handle.section_id:
            return

        rec = self._sections.get(handle.section_id)
        if rec is None:
            self._sections[handle.section_id] = SectionRecord(handle=handle)
        else:
            self._sections[handle.section_id] = SectionRecord(
                handle=handle,
                fields=rec.fields,
            )

    def get_section(self, section_id: str) -> Optional[SectionHandle]:
        rec = self._sections.get(section_id)
        return rec.handle if rec else None

    def all_sections(self) -> Iterable[SectionHandle]:
        return (rec.handle for rec in self._sections.values())

    # --- fields ---

    def add_field(self, handle: FieldHandle) -> None:
        """
        Record a newly created field and attach it to its section record.
        """
        if not handle.field_id:
            return

        ctx = self._handle_ctx(handle)
        duplicate_field = handle.field_id in self._fields
        if duplicate_field:
            self._inc_counter("registry.duplicate_field_ids")
            self._emit_signal(
                "Duplicate field handle re-registered",
                note="duplicate_field_id",
                level="warning",
                **ctx,
            )

        self._fields[handle.field_id] = handle

        if handle.section_id:
            rec = self._sections.get(handle.section_id)
            if rec is None:
                # section might not have been registered yet - create a bare record
                rec = SectionRecord(handle=SectionHandle(section_id=handle.section_id))
                self._sections[handle.section_id] = rec
                self._inc_counter("registry.field_without_section")
                self._emit_signal(
                    "Field added before section record existed",
                    reason="missing_section_record",
                    level="warning",
                    **ctx,
                )
            existing_index = next(
                (idx for idx, f in enumerate(rec.fields) if f.field_id == handle.field_id),
                None,
            )
            if existing_index is not None:
                self._inc_counter("registry.section_duplicate_handles")
                self._emit_signal(
                    "Section already referenced this field id",
                    reason="duplicate_section_handle",
                    level="warning",
                    **ctx,
                )
                rec.fields[existing_index] = handle
            else:
                rec.fields.append(handle)
        else:
            self._inc_counter("registry.field_missing_section")
            self._emit_signal(
                "Field handle without section information",
                reason="missing_section_info",
                level="warning",
                **ctx,
            )

    def get_field(self, field_id: str) -> Optional[FieldHandle]:
        return self._fields.get(field_id)

    def fields_for_section(self, section_id: str) -> List[FieldHandle]:
        rec = self._sections.get(section_id)
        return list(rec.fields) if rec else []

    def fields_by_type(
        self,
        field_type_key: str,
        section_id: Optional[str] = None,
    ) -> List[FieldHandle]:
        """
        Return all fields matching a type key, optionally restricted to a section.
        """
        if section_id:
            rec = self._sections.get(section_id)
            if not rec:
                return []
            return [f for f in rec.fields if f.field_type_key == field_type_key]

        # No section filter – search all fields
        return [
            f for f in self._fields.values()
            if f.field_type_key == field_type_key
        ]

    def field_ids_for_section_and_type(self, section_id: str, field_type_key: str) -> set[str]:
        return {f.field_id for f in self.fields_by_type(field_type_key, section_id=section_id) if f.field_id}

    def anchor_before_fi_index(self, *, section_id: str, fi_index: int) -> str | None:
        """
        Return the field_id of the nearest field in this section with fh.fi_index < fi_index.
        This gives a stable 'insert_after' anchor for retries.
        """
        rec = self._sections.get(section_id)
        if not rec or not rec.fields:
            return None

        candidates = [
            fh for fh in rec.fields
            if fh.field_id
            and fh.fi_index is not None
            and fh.fi_index < fi_index
        ]

        if not candidates:
            ctx = {"sec": section_id, "fi": fi_index}
            self._inc_counter("registry.anchor_misses")
            self._emit_diag(
                "No anchor found before requested fi index",
                key="REG.anchor_missing",
                **ctx,
            )
            return None

        best = max(candidates, key=lambda fh: cast(int, fh.fi_index))
        return best.field_id

    # --- deletion hooks for future ---

    def remove_field(self, field_id: str) -> None:
        handle = self._fields.pop(field_id, None)
        if handle and handle.section_id in self._sections:
            rec = self._sections[handle.section_id]
            rec.fields = [f for f in rec.fields if f.field_id != field_id]

    def remove_section(self, section_id: str) -> None:
        rec = self._sections.pop(section_id, None)
        if rec:
            for f in rec.fields:
                self._fields.pop(f.field_id, None)

    # --- debug helpers ---

    def snapshot(self) -> dict:
        """
        Return a simple dict representation of the current registry,
        suitable for JSON/YAML dumping.
        """
        snapshot = {
            "sections": {
                section_id: {
                    "handle": {
                        "section_id": rec.handle.section_id,
                        "title": rec.handle.title,
                        "index": rec.handle.index,
                    },
                    "fields": [
                        {
                            "field_id": f.field_id,
                            "section_id": f.section_id,
                            "field_type_key": f.field_type_key,
                            "index_hint": f.index_hint,
                            "index": f.index,
                            "title": f.title,
                        }
                        for f in rec.fields
                    ],
                }
                for section_id, rec in self._sections.items()
            },
            "fields": {
                field_id: {
                    "field_id": fh.field_id,
                    "section_id": fh.section_id,
                    "field_type_key": fh.field_type_key,
                    "index_hint": fh.index_hint,
                    "index": fh.index,
                    "title": fh.title,
                }
                for field_id, fh in self._fields.items()
            },
        }

        self._inc_counter("registry.snapshot_count")
        self._emit_diag(
            "Registry snapshot emitted",
            key="REG.snapshot",
            every_s=60.0,
            sections=len(self._sections),
            fields=len(self._fields),
        )

        return snapshot
