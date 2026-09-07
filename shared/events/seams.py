"""Injectable seams (spec 00 - "Injectable seams (test doubles for all I/O)").

Every I/O call site in this codebase depends on one of these protocols, never on a
concretion. This keeps tests deterministic: no code reads the wall clock, hits the
network, or touches storage without going through a seam that can be faked.

Full implementations:
- `Clock`: `SystemClock` (shared/events/clock.py) and `FakeClock` (tests/unit/fakes.py) —
  provided in this task (Task 1).
- `ModelClient` / `ToolClient`: real implementations land in later epics (E3/E4 for models,
  the relevant adapter epics for tools). `ModelRequest`/`ModelResponse` below are placeholder
  types only; their full shape is out of scope for E1.
- `Store`: the full persistence-layer implementation lands in E2 (state & mutation layer,
  spec 02). `QueueItem` is defined properly in Task 4 of this epic. `MutationCommand` /
  `MutationResult` are E2 scope.
"""

from datetime import datetime
from typing import Any, Protocol


class Clock(Protocol):
    """No wall-clock reads outside an implementation of this protocol."""

    def now(self) -> datetime: ...


class ModelRequest:
    """Placeholder type. Full shape defined in spec 03 (E3/E4 scope, not needed for E1)."""


class ModelResponse:
    """Placeholder type. Full shape defined in spec 03 (E3/E4 scope, not needed for E1)."""


class ModelClient(Protocol):
    """Local and hosted model tiers are both fakeable behind this seam (spec 03)."""

    def generate(self, req: "ModelRequest") -> "ModelResponse": ...


class ToolClient(Protocol):
    """External side-effecting calls (Gmail, Telegram, ...) go through this seam."""

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]: ...


class QueueItem:
    """Placeholder type. Defined properly in Task 4 of this epic."""


class MutationCommand:
    """Placeholder type. E2 scope (state & mutation layer, spec 02); not implemented here."""


class MutationResult:
    """Placeholder type. E2 scope (state & mutation layer, spec 02); not implemented here."""


class Store(Protocol):
    """Persistence boundary (spec 02). Repositories are pure persistence; no business logic."""

    def enqueue(self, item: "QueueItem") -> None: ...
    def apply(self, cmd: "MutationCommand") -> "MutationResult": ...
