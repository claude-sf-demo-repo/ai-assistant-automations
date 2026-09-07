"""In-memory fakes for the injectable seams (shared/events/seams.py), per spec 00's
"Test seams" section: "All four protocols have in-memory fakes in `tests/`."

Only `FakeClock` is implemented here in Task 1. Fakes for `ModelClient`, `ToolClient`, and
`Store` land alongside their real implementations in later tasks/epics.
"""

from datetime import datetime, timedelta, timezone


class FakeClock:
    """Deterministic, settable/advanceable stand-in for `Clock`.

    Defaults to a fixed instant rather than the real wall clock so tests never depend on
    when they happen to run.
    """

    def __init__(self, initial: datetime | None = None) -> None:
        self._current = initial or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current

    def set(self, when: datetime) -> None:
        self._current = when

    def advance(self, delta: timedelta) -> None:
        self._current += delta
