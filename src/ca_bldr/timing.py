import time
from contextlib import contextmanager

@contextmanager
def phase_timer(logger, label: str):
    start = time.perf_counter()
    logger.info("⏱ START phase: %s", label)
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start

        if elapsed >= 300:  # 5 minutes
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            logger.info("⏱ END phase: %s (%dm %ds)", label, mins, secs)
        else:
            logger.info("⏱ END phase: %s (%.2f seconds)", label, elapsed)
