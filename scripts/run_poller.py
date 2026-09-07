"""`poller` process entry point (spec 01 "Processes"; Task 7 final wiring for E1, #1).

Thin wiring only -- no business logic lives here. The loop just calls
`adapters/gmail/adapter.py::poll_once` on a `settings.poll_interval_seconds` cadence,
against the real `GmailToolClient`, the real `PostgresIngestStore`, and the real
`SystemClock`. All actual polling/enqueue/cursor-advance logic is in `poll_once` itself
(Task 3) and `PostgresIngestStore` (Task 4); this module owns none of it.

Run with:
    python -m scripts.run_poller

Configuration is read exclusively from `shared/config/settings.py::Settings` (env /
`.env`) -- nothing here is a literal secret or a hardcoded path.
"""

from __future__ import annotations

import logging
import time

from adapters.gmail.adapter import poll_once
from adapters.gmail.auth import build_gmail_tool_client
from shared.config.settings import Settings
from shared.events.clock import SystemClock
from shared.events.payload_store import LocalPayloadStore
from storage.repositories.queue import PostgresIngestStore

logger = logging.getLogger("poller")


def main() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    gmail = build_gmail_tool_client(settings.gmail_oauth_token_path)
    store = PostgresIngestStore(settings.database_url)
    clock = SystemClock()
    payload_store = LocalPayloadStore(settings.payload_store_dir)

    logger.info(
        "poller starting: account_ref=%s poll_interval_seconds=%s",
        settings.gmail_account_ref,
        settings.poll_interval_seconds,
    )

    while True:
        try:
            poll_once(
                gmail,
                store,
                clock,
                settings.gmail_account_ref,
                payload_store=payload_store,
            )
        except Exception:  # noqa: BLE001 - keep the poller alive across a bad cycle
            logger.exception("poll_once failed; will retry next cycle")
        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    main()
