from __future__ import annotations

from contextlib import contextmanager
from typing import Any
import time

from .instrumentation import Cat
from .session import CASession


def _emit_phase(
    session: CASession,
    *,
    level: str,
    msg: str,
    cat: Cat,
    ctx: dict[str, Any],
) -> None:
    session.emit_signal(cat, msg, level=level, **ctx)


@contextmanager
def phase_timer(
    session: CASession | None,
    label: str,
    *,
    cat: Cat = Cat.NAV,
    ctx: dict[str, Any] | None = None,
):
    if session is None:
        raise RuntimeError("phase_timer requires an active CASession")
    start = time.perf_counter()
    merged_ctx: dict[str, Any] = {"a": label}
    if ctx:
        merged_ctx.update(ctx)
    _emit_phase(session, level="info", msg=f"START phase: {label}", cat=cat, ctx=merged_ctx)
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        merged_ctx["elapsed_s"] = elapsed
        if elapsed >= 300:  # 5 minutes
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            _emit_phase(
                session,
                level="warning",
                msg=f"END phase: {label} ({mins}m {secs}s)",
                cat=cat,
                ctx=merged_ctx,
            )
        else:
            _emit_phase(
                session,
                level="info",
                msg=f"END phase: {label} ({elapsed:.2f} seconds)",
                cat=cat,
                ctx=merged_ctx,
            )
