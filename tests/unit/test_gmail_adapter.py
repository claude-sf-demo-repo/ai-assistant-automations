"""Unit tests for the Gmail adapter (adapters/gmail/adapter.py, issue #11)."""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path

import pytest

from adapters.gmail.adapter import poll_once, to_envelope
from shared.events.envelope import CommonEventEnvelope, EnvelopeValidationError
from shared.events.payload_store import LocalPayloadStore
from tests.unit.fakes import FakeClock, FakeGmailToolClient, FakeIngestStore
from tests.unit.fixtures.gmail_history_responses import (
    ALL_HISTORY_LIST_RESPONSES,
    HISTORY_LIST_RESPONSE_CLEAN_INBOUND,
    HISTORY_LIST_RESPONSE_FAILED_AUTH,
    HISTORY_LIST_RESPONSE_MIXED,
)

_ACCOUNT_REF = "personal"


def _extract_records(response: dict) -> list[dict]:
    from adapters.gmail.adapter import _extract_history_records

    return _extract_history_records(response)


def _payload_store(tmp_path: Path) -> LocalPayloadStore:
    return LocalPayloadStore(tmp_path / "payloads")


class TestToEnvelopeFixtureMapping:
    """Each recorded fixture maps to a well-formed CommonEventEnvelope."""

    @pytest.mark.parametrize(
        "response",
        ALL_HISTORY_LIST_RESPONSES,
        ids=["clean_inbound", "failed_auth", "mixed_sent_and_thread_updated"],
    )
    def test_every_record_maps_to_a_valid_envelope(self, response, tmp_path) -> None:
        clock = FakeClock()
        store = _payload_store(tmp_path)
        for record in _extract_records(response):
            envelope = to_envelope(record, _ACCOUNT_REF, clock=clock, payload_store=store)
            assert isinstance(envelope, CommonEventEnvelope)

    def test_clean_inbound_message_is_received_with_passing_auth(self, tmp_path) -> None:
        clock = FakeClock()
        store = _payload_store(tmp_path)
        [record] = _extract_records(HISTORY_LIST_RESPONSE_CLEAN_INBOUND)
        envelope = to_envelope(record, _ACCOUNT_REF, clock=clock, payload_store=store)

        assert envelope.type == "message.received"
        assert envelope.thread_ref == "thread-clean-1"
        assert envelope.sender.address == "alice@example.com"
        assert envelope.sender.domain_authenticated.spf == "pass"
        assert envelope.sender.domain_authenticated.dkim == "pass"
        assert envelope.sender.domain_authenticated.dmarc == "pass"
        assert envelope.subject.value == "Re: invoice"
        assert envelope.subject.untrusted is True

    def test_failed_auth_message_reports_failing_verdicts(self, tmp_path) -> None:
        clock = FakeClock()
        store = _payload_store(tmp_path)
        [record] = _extract_records(HISTORY_LIST_RESPONSE_FAILED_AUTH)
        envelope = to_envelope(record, _ACCOUNT_REF, clock=clock, payload_store=store)

        assert envelope.sender.domain_authenticated.spf == "fail"
        assert envelope.sender.domain_authenticated.dkim == "fail"
        assert envelope.sender.domain_authenticated.dmarc == "fail"
        # identity_trusted must be False regardless of the (failing) auth verdicts --
        # it is never derived from domain_authenticated.
        assert envelope.sender.identity_trusted is False

    def test_sent_message_is_message_replied_and_changed_entry_is_thread_updated(
        self, tmp_path
    ) -> None:
        clock = FakeClock()
        store = _payload_store(tmp_path)
        records = _extract_records(HISTORY_LIST_RESPONSE_MIXED)
        types = {
            to_envelope(r, _ACCOUNT_REF, clock=clock, payload_store=store).type: r
            for r in records
        }
        assert "message.replied" in types
        assert "thread.updated" in types

    def test_thread_ref_is_gmail_thread_id_not_a_header(self, tmp_path) -> None:
        clock = FakeClock()
        store = _payload_store(tmp_path)
        [record] = _extract_records(HISTORY_LIST_RESPONSE_CLEAN_INBOUND)
        # Sanity: no References/In-Reply-To header is even present in the fixture, so
        # thread_ref can only have come from the message's own threadId field.
        headers = {h["name"] for h in record["message"]["payload"]["headers"]}
        assert "References" not in headers
        assert "In-Reply-To" not in headers
        envelope = to_envelope(record, _ACCOUNT_REF, clock=clock, payload_store=store)
        assert envelope.thread_ref == record["message"]["threadId"]


class TestIdentityTrustedIsAlwaysFalse:
    def test_identity_trusted_false_for_every_fixture(self, tmp_path) -> None:
        clock = FakeClock()
        store = _payload_store(tmp_path)
        for response in ALL_HISTORY_LIST_RESPONSES:
            for record in _extract_records(response):
                envelope = to_envelope(record, _ACCOUNT_REF, clock=clock, payload_store=store)
                assert envelope.sender.identity_trusted is False


class TestPayloadStore:
    def test_payload_ref_and_sha256_round_trip_through_local_store(self, tmp_path) -> None:
        clock = FakeClock()
        store = _payload_store(tmp_path)
        [record] = _extract_records(HISTORY_LIST_RESPONSE_CLEAN_INBOUND)
        envelope = to_envelope(record, _ACCOUNT_REF, clock=clock, payload_store=store)

        assert envelope.payload_ref.startswith("store://raw/")
        assert len(envelope.payload_sha256) == 64  # hex sha256
        raw_back = store.get(envelope.payload_ref)
        assert b"Alice Sender" in raw_back or b"alice@example.com" in raw_back


class TestPollOnceTransactionalPattern:
    def test_poll_once_enqueues_and_advances_cursor_together(self, tmp_path) -> None:
        clock = FakeClock()
        payload_store = _payload_store(tmp_path)
        ingest_store = FakeIngestStore()
        gmail = FakeGmailToolClient([HISTORY_LIST_RESPONSE_CLEAN_INBOUND])

        assert ingest_store.get_cursor("gmail", _ACCOUNT_REF) is None

        poll_once(gmail, ingest_store, clock, _ACCOUNT_REF, payload_store=payload_store)

        assert len(ingest_store.queue) == 1
        assert ingest_store.get_cursor("gmail", _ACCOUNT_REF) == "101"
        # first call has no startHistoryId (cold start)
        assert gmail.calls[0] == ("history.list", {})

    def test_poll_once_passes_cursor_as_start_history_id_on_next_poll(self, tmp_path) -> None:
        clock = FakeClock()
        payload_store = _payload_store(tmp_path)
        ingest_store = FakeIngestStore()
        gmail = FakeGmailToolClient(
            [HISTORY_LIST_RESPONSE_CLEAN_INBOUND, HISTORY_LIST_RESPONSE_MIXED]
        )

        poll_once(gmail, ingest_store, clock, _ACCOUNT_REF, payload_store=payload_store)
        poll_once(gmail, ingest_store, clock, _ACCOUNT_REF, payload_store=payload_store)

        assert gmail.calls[1] == ("history.list", {"startHistoryId": "101"})
        assert ingest_store.get_cursor("gmail", _ACCOUNT_REF) == "301"
        assert len(ingest_store.queue) == 1 + 2  # 1 from first poll, 2 from second

    def test_poll_once_is_idempotent_on_redelivered_history(self, tmp_path) -> None:
        """Re-polling the same (unadvanced) response must not duplicate queue rows."""
        clock = FakeClock()
        payload_store = _payload_store(tmp_path)
        ingest_store = FakeIngestStore()
        gmail = FakeGmailToolClient(
            [HISTORY_LIST_RESPONSE_CLEAN_INBOUND, HISTORY_LIST_RESPONSE_CLEAN_INBOUND]
        )

        poll_once(gmail, ingest_store, clock, _ACCOUNT_REF, payload_store=payload_store)
        # Cursor already advanced past this response's historyId, but simulate a
        # redelivery of the *same* underlying message (e.g. a retried poll) by driving
        # poll_once again with the identical scripted response.
        poll_once(gmail, ingest_store, clock, _ACCOUNT_REF, payload_store=payload_store)

        assert len(ingest_store.queue) == 1


class TestCrashBetweenEnqueueAndCursorAdvance:
    """Failure-mode test (task-3 brief): a crash between enqueue_many and set_cursor
    must leave NEITHER the queue rows NOR the cursor advance visible, so a retry safely
    re-polls from the old cursor.
    """

    def test_exception_inside_transaction_rolls_back_both_writes(self, tmp_path) -> None:
        clock = FakeClock()
        payload_store = _payload_store(tmp_path)
        ingest_store = FakeIngestStore()
        [record] = _extract_records(HISTORY_LIST_RESPONSE_CLEAN_INBOUND)
        envelope = to_envelope(record, _ACCOUNT_REF, clock=clock, payload_store=payload_store)

        assert ingest_store.get_cursor("gmail", _ACCOUNT_REF) is None

        class _SimulatedCrash(Exception):
            pass

        with pytest.raises(_SimulatedCrash):
            with ingest_store.transaction() as tx:
                tx.enqueue_many([envelope])
                # Simulated crash: happens after enqueue_many, before set_cursor.
                raise _SimulatedCrash("process died before cursor advance")

        assert ingest_store.get_cursor("gmail", _ACCOUNT_REF) is None
        assert ingest_store.queue == []

    def test_poll_once_leaves_store_untouched_when_set_cursor_raises(self, tmp_path) -> None:
        clock = FakeClock()
        payload_store = _payload_store(tmp_path)
        gmail = FakeGmailToolClient([HISTORY_LIST_RESPONSE_CLEAN_INBOUND])

        class _CrashingStore(FakeIngestStore):
            class _Transaction(FakeIngestStore._Transaction):
                def __enter__(self):
                    tx = super().__enter__()
                    original_set_cursor = tx.set_cursor

                    def _crashing_set_cursor(*args, **kwargs):
                        original_set_cursor(*args, **kwargs)
                        raise RuntimeError("simulated crash after cursor write, before commit")

                    tx.set_cursor = _crashing_set_cursor
                    return tx

        ingest_store = _CrashingStore()

        with pytest.raises(RuntimeError):
            poll_once(gmail, ingest_store, clock, _ACCOUNT_REF, payload_store=payload_store)

        # Retry safety: old cursor unchanged, no partial queue rows persisted.
        assert ingest_store.get_cursor("gmail", _ACCOUNT_REF) is None
        assert ingest_store.queue == []


def _module_imports_adapters_gmail(source: str) -> bool:
    """AST-based check: does `source` contain an `import`/`from ... import` statement
    that pulls in `adapters.gmail` (or a submodule of it)?

    AST-based rather than a text/regex scan so that (a) prose mentions in docstrings/
    comments never false-positive, and (b) it isn't fooled by a bare `import adapters`
    followed by `adapters.gmail.foo(...)` attribute access -- that form still shows up
    as an `Import` node for `adapters` alone, which callers can additionally flag if
    they want to be stricter; as written, `core/` doesn't do a bare `import adapters`
    anywhere today (checked below), so this is airtight for the actual codebase.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "adapters.gmail" or alias.name.startswith("adapters.gmail."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "adapters.gmail" or module.startswith("adapters.gmail."):
                return True
    return False


def _module_imports_bare_adapters(source: str) -> bool:
    """Does `source` do a bare `import adapters` (which could then attribute-access
    `.gmail` without tripping `_module_imports_adapters_gmail`)?
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "adapters":
                    return True
    return False


class TestCoreImportsNothingGmailSpecific:
    """Prose (docstrings/comments) may mention `adapters/gmail` for cross-referencing --
    what must never happen is an actual import pulling Gmail-specific code into `core/`.
    """

    def test_no_core_module_imports_adapters_gmail(self) -> None:
        core_dir = Path("core")
        offenders = []
        for path in core_dir.rglob("*.py"):
            text = path.read_text()
            if _module_imports_adapters_gmail(text):
                offenders.append(str(path))
        assert offenders == []

    def test_no_core_module_does_a_bare_import_adapters_either(self) -> None:
        # Closes the gap a plain `import adapters.gmail` check can't: a bare
        # `import adapters` followed by `adapters.gmail.foo(...)` attribute access
        # wouldn't show up as an `adapters.gmail` import node at all.
        core_dir = Path("core")
        offenders = []
        for path in core_dir.rglob("*.py"):
            text = path.read_text()
            if _module_imports_bare_adapters(text):
                offenders.append(str(path))
        assert offenders == []


class TestOAuthTokenPathIsNeverALiteral:
    def test_auth_module_reads_token_path_from_a_parameter_not_a_literal(self) -> None:
        auth_source = Path("adapters/gmail/auth.py").read_text()
        # No hard-coded filesystem path literal standing in for the token file --
        # the only path-shaped string is the OAuth scope URL, and the constructor
        # parameter name documents where the real value comes from (settings).
        assert "oauth_token_path" in auth_source
        assert re.search(r'oauth_token_path\s*=\s*["\']', auth_source) is None

    def test_settings_defines_gmail_oauth_token_path_with_no_default_literal(self) -> None:
        settings_source = Path("shared/config/settings.py").read_text()
        assert re.search(r"gmail_oauth_token_path:\s*str\s*=\s*[\"']", settings_source) is None


class TestEnvelopeConstructionFailureSurfacesTypedError:
    def test_malformed_history_record_raises_envelope_validation_error(self, tmp_path) -> None:
        clock = FakeClock()
        payload_store = _payload_store(tmp_path)
        # Deep-copied: `_extract_records` returns records that share nested dicts with
        # the module-level fixture constant (it doesn't deep-copy), so mutating
        # `record["message"]` in place here would otherwise permanently corrupt
        # `HISTORY_LIST_RESPONSE_CLEAN_INBOUND` for every other test in the session
        # that imports the same fixture object afterward.
        [record] = _extract_records(HISTORY_LIST_RESPONSE_CLEAN_INBOUND)
        record = copy.deepcopy(record)
        record["message"]["threadId"] = ""  # violates min_length=1 on thread_ref

        with pytest.raises(EnvelopeValidationError):
            to_envelope(record, _ACCOUNT_REF, clock=clock, payload_store=payload_store)
