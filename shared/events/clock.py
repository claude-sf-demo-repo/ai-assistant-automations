"""Real-world implementation of the `Clock` seam (shared/events/seams.py)."""

from datetime import datetime, timezone


class SystemClock:
    """Reads the actual wall clock. This is the only place in the codebase allowed to."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)
