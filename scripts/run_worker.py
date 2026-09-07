"""`ingest-worker` process entry point (spec 01 "Processes"; Task 7 final wiring for E1, #1).

Thin wiring only -- no business logic lives here. This just calls
`core/ingestion/worker.py::run_forever` with real dependencies: the real database DSN,
the real `SystemClock`, and the poison threshold from settings. `run_forever` itself
constructs its own `QueueRepository`/`ThreadFoldRepository` bound to its own connections
(see that module's docstring) -- this entry point does not duplicate that wiring, only
supplies the settings-derived arguments `run_forever` takes.

Run with:
    python -m scripts.run_worker

Configuration is read exclusively from `shared/config/settings.py::Settings` (env /
`.env`) -- nothing here is a literal secret or a hardcoded path.
"""

from __future__ import annotations

import logging

from core.ingestion.worker import run_forever
from shared.config.settings import Settings
from shared.events.clock import SystemClock

logger = logging.getLogger("ingest-worker")


def main() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    logger.info(
        "ingest-worker starting: poll_interval_seconds=%s poison_threshold=%s",
        settings.poll_interval_seconds,
        settings.poison_threshold,
    )

    run_forever(
        settings.database_url,
        poll_interval_seconds=settings.poll_interval_seconds,
        poison_threshold=settings.poison_threshold,
        clock=SystemClock(),
    )


if __name__ == "__main__":
    main()
