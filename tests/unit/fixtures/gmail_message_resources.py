"""Realistic (synthetic, redacted) Gmail `users.messages.get` response shapes, per
format, for testing `adapters/gmail/auth.py::_merge_full_and_raw`.

These deliberately do NOT match `tests/unit/fixtures/gmail_history_responses.py`'s
already-merged message shape -- they show what the real Gmail API actually returns for
each `format` value on its own:

- `format="full"`: `payload.headers` (and `payload.body`) present; NO `raw` field.
- `format="raw"`: `raw` (base64url RFC 822 bytes) present; NO `payload` field at all.

No real mail data -- every value below is made up for these tests.
"""

from __future__ import annotations

import base64

_MESSAGE_ID = "msg-full-raw-1"
_THREAD_ID = "thread-full-raw-1"

_RAW_RFC822 = (
    "From: Alice Sender <alice@example.com>\r\n"
    "To: Me <me@example.com>\r\n"
    "Subject: Re: contract\r\n"
    "Message-Id: <full-raw-1@example.com>\r\n"
    "\r\n"
    "Following up on the contract draft.\r\n"
)


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


# `format="full"` response: headers/body, no `raw` key at all.
FULL_MESSAGE_RESOURCE = {
    "id": _MESSAGE_ID,
    "threadId": _THREAD_ID,
    "labelIds": ["INBOX"],
    "snippet": "Following up on the contract draft.",
    "internalDate": "1767000300000",
    "payload": {
        "partId": "",
        "mimeType": "text/plain",
        "filename": "",
        "headers": [
            {"name": "From", "value": "Alice Sender <alice@example.com>"},
            {"name": "To", "value": "Me <me@example.com>"},
            {"name": "Subject", "value": "Re: contract"},
            {"name": "Message-Id", "value": "<full-raw-1@example.com>"},
            {
                "name": "Authentication-Results",
                "value": (
                    "mx.google.com; spf=pass smtp.mailfrom=example.com; "
                    "dkim=pass header.i=@example.com; dmarc=pass header.from=example.com"
                ),
            },
        ],
        "body": {"size": 37, "data": _b64url("Following up on the contract draft.\n")},
    },
    "sizeEstimate": 512,
}

# `format="raw"` response: raw bytes only, no `payload` key at all.
RAW_MESSAGE_RESOURCE = {
    "id": _MESSAGE_ID,
    "threadId": _THREAD_ID,
    "labelIds": ["INBOX"],
    "snippet": "Following up on the contract draft.",
    "internalDate": "1767000300000",
    "raw": _b64url(_RAW_RFC822),
    "sizeEstimate": 512,
}
