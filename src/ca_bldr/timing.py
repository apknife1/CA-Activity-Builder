from __future__ import annotations

from contextlib import contextmanager
from typing import Any
import time

from .instrumentation import Cat
from .session import CASession


def _emit_phase(
    session: CASession | None,
    logger,
    *,
    level: str,
    msg: str,
    cat: Cat,
    ctx: dict[str, Any],
) -> None:
    if session:
        session.emit_signal(cat, msg, level=level, **ctx)
        return
    if logger:
        getattr(logger, level, logger.info)(msg)


@contextmanager
def phase_timer(
    session: CASession | None,
    label: str,
    *,
    logger=None,
    cat: Cat = Cat.NAV,
    ctx: dict[str, Any] | None = None,
):
    start = time.perf_counter()
    merged_ctx: dict[str, Any] = {"a": label}
    if ctx:
        merged_ctx.update(ctx)
    _emit_phase(session, logger, level="info", msg=f"START phase: {label}", cat=cat, ctx=merged_ctx)
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
                logger,
                level="warning",
                msg=f"END phase: {label} ({mins}m {secs}s)",
                cat=cat,
                ctx=merged_ctx,
            )
        else:
            _emit_phase(
                session,
                logger,
                level="info",
                msg=f"END phase: {label} ({elapsed:.2f} seconds)",
                cat=cat,
                ctx=merged_ctx,
            )
