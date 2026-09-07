"""Recorded-shape (synthetic, redacted) Gmail `history.list` fixtures for Task 3 (#11).

None of this is real mail data -- every address, subject, and header value below is
made up for these tests. Shapes mirror the real Gmail API (`users.history.list` /
`users.messages` resources) closely enough to exercise `adapters/gmail/adapter.py`
faithfully:

- `history.list` returns `{"history": [...], "historyId": "<new cursor>"}`, where each
  history entry has a `messagesAdded` or `messagesChanged` list.
- Each `message` entry is a full Gmail Message resource: `id`, `threadId`,
  `internalDate` (epoch millis, as a string), `labelIds`, `payload.headers` (list of
  `{"name", "value"}` pairs), and `raw` (the base64url-encoded full RFC 822 message).

Real `history.list` calls don't inline full message bodies -- callers normally fan out
to `users.messages.get(format="raw")` per message. For E1, `GmailToolClient` is
responsible for doing that fan-out internally (adapters/gmail/auth.py) so that
`ToolClient.call("history.list", ...)` always returns fully-hydrated message resources
like these; `adapter.py::to_envelope`/`poll_once` never make a second round-trip. That
fan-out isn't exercised here since it requires live credentials -- only the resulting
shape is scripted.
"""

from __future__ import annotations

import base64


def _raw(text: str) -> str:
    """Base64url-encode a synthetic RFC 822 message body, Gmail API style."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


# --- Fixture 1: a clean, well-authenticated inbound message (messageAdded) ---------

CLEAN_INBOUND_RAW = (
    "From: Alice Sender <alice@example.com>\r\n"
    "To: Me <me@example.com>\r\n"
    "Subject: Re: invoice\r\n"
    "Message-Id: <clean-inbound-1@example.com>\r\n"
    "\r\n"
    "Hi -- following up on the invoice.\r\n"
)

CLEAN_INBOUND_MESSAGE = {
    "id": "msg-clean-1",
    "threadId": "thread-clean-1",
    "labelIds": ["INBOX", "UNREAD"],
    "internalDate": "1767000000000",
    "payload": {
        "headers": [
            {"name": "From", "value": "Alice Sender <alice@example.com>"},
            {"name": "To", "value": "Me <me@example.com>"},
            {"name": "Subject", "value": "Re: invoice"},
            {"name": "Message-Id", "value": "<clean-inbound-1@example.com>"},
            {
                "name": "Authentication-Results",
                "value": (
                    "mx.google.com; spf=pass smtp.mailfrom=example.com; "
                    "dkim=pass header.i=@example.com; dmarc=pass header.from=example.com"
                ),
            },
        ]
    },
    "raw": _raw(CLEAN_INBOUND_RAW),
}

HISTORY_LIST_RESPONSE_CLEAN_INBOUND = {
    "history": [
        {
            "id": "hist-100",
            "messagesAdded": [{"message": CLEAN_INBOUND_MESSAGE}],
        }
    ],
    "historyId": "101",
}


# --- Fixture 2: an inbound message that fails SPF/DKIM/DMARC (spoof-shaped) --------

FAILED_AUTH_RAW = (
    "From: \"Your Bank\" <security@bank-example.com>\r\n"
    "To: Me <me@example.com>\r\n"
    "Subject: Urgent: verify your account\r\n"
    "Message-Id: <failed-auth-1@bank-example.com>\r\n"
    "\r\n"
    "Please verify your account immediately.\r\n"
)

FAILED_AUTH_MESSAGE = {
    "id": "msg-failed-auth-1",
    "threadId": "thread-failed-auth-1",
    "labelIds": ["INBOX", "UNREAD"],
    "internalDate": "1767000100000",
    "payload": {
        "headers": [
            {"name": "From", "value": '"Your Bank" <security@bank-example.com>'},
            {"name": "To", "value": "Me <me@example.com>"},
            {"name": "Subject", "value": "Urgent: verify your account"},
            {"name": "Message-Id", "value": "<failed-auth-1@bank-example.com>"},
            {
                "name": "Authentication-Results",
                "value": (
                    "mx.google.com; spf=fail smtp.mailfrom=bank-example.com; "
                    "dkim=fail header.i=@bank-example.com; dmarc=fail header.from=bank-example.com"
                ),
            },
        ]
    },
    "raw": _raw(FAILED_AUTH_RAW),
}

HISTORY_LIST_RESPONSE_FAILED_AUTH = {
    "history": [
        {
            "id": "hist-200",
            "messagesAdded": [{"message": FAILED_AUTH_MESSAGE}],
        }
    ],
    "historyId": "201",
}


# --- Fixture 3: an outbound (sent) reply, and an unrelated messageChanged entry ----

SENT_REPLY_RAW = (
    "From: Me <me@example.com>\r\n"
    "To: Alice Sender <alice@example.com>\r\n"
    "Subject: Re: invoice\r\n"
    "Message-Id: <sent-reply-1@example.com>\r\n"
    "\r\n"
    "Thanks, paid.\r\n"
)

SENT_REPLY_MESSAGE = {
    "id": "msg-sent-reply-1",
    "threadId": "thread-clean-1",
    "labelIds": ["SENT"],
    "internalDate": "1767000200000",
    "payload": {
        "headers": [
            {"name": "From", "value": "Me <me@example.com>"},
            {"name": "To", "value": "Alice Sender <alice@example.com>"},
            {"name": "Subject", "value": "Re: invoice"},
            {"name": "Message-Id", "value": "<sent-reply-1@example.com>"},
            {
                "name": "Authentication-Results",
                "value": (
                    "mx.google.com; spf=pass smtp.mailfrom=example.com; "
                    "dkim=pass header.i=@example.com; dmarc=pass header.from=example.com"
                ),
            },
        ]
    },
    "raw": _raw(SENT_REPLY_RAW),
}

THREAD_UPDATED_MESSAGE = {
    "id": "msg-clean-1",
    "threadId": "thread-clean-1",
    "labelIds": ["INBOX"],
    "internalDate": "1767000000000",
    "payload": {
        "headers": [
            {"name": "From", "value": "Alice Sender <alice@example.com>"},
            {"name": "To", "value": "Me <me@example.com>"},
            {"name": "Subject", "value": "Re: invoice"},
            {"name": "Message-Id", "value": "<clean-inbound-1@example.com>"},
            {
                "name": "Authentication-Results",
                "value": (
                    "mx.google.com; spf=pass smtp.mailfrom=example.com; "
                    "dkim=pass header.i=@example.com; dmarc=pass header.from=example.com"
                ),
            },
        ]
    },
    "raw": _raw(CLEAN_INBOUND_RAW),
}

HISTORY_LIST_RESPONSE_MIXED = {
    "history": [
        {
            "id": "hist-300",
            "messagesAdded": [{"message": SENT_REPLY_MESSAGE}],
        },
        {
            "id": "hist-301",
            "messagesChanged": [{"message": THREAD_UPDATED_MESSAGE}],
        },
    ],
    "historyId": "301",
}


ALL_HISTORY_LIST_RESPONSES = [
    HISTORY_LIST_RESPONSE_CLEAN_INBOUND,
    HISTORY_LIST_RESPONSE_FAILED_AUTH,
    HISTORY_LIST_RESPONSE_MIXED,
]
