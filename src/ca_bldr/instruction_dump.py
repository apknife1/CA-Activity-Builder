import json
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .spec_reader import ActivityInstruction
from .instrumentation import Cat
from .session import CASession

def dump_activity_instruction_json(
    act: ActivityInstruction,
    out_path: Path,
    *,
    logger=None,
    session: CASession | None = None,
) -> None:
    def _emit(level: str, msg: str, **ctx: Any) -> None:
        if session:
            session.emit_signal(Cat.CONFIGURE, msg, level=level, **ctx)
            return
        if logger:
            getattr(logger, level, logger.info)(msg)

    if is_dataclass(act):
        _emit("info", "ActivityInstruction outputting asdict")
        payload = asdict(act)
    else:
        # fallback: best effort
        _emit("info", "ActivityInstruction payload building from attributes")
        payload = {
            "activity_code": getattr(act, "activity_code", None),
            "activity_title": getattr(act, "activity_title", None),
            "unit_code": getattr(act, "unit_code", None),
            "unit_title": getattr(act, "unit_title", None),
            "activity_type": getattr(act, "activity_type", None),
            "fields": [asdict(f) if is_dataclass(f) else f for f in (getattr(act, "fields", []) or [])],
        }

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        _emit("info", f"Wrote activity dump to: {out_path}")
    except Exception as e:
        _emit("warning", f"Could not output to json. Message: {e!r}")
