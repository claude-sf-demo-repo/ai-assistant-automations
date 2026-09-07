"""Real Gmail OAuth + `ToolClient` implementation (issue #11).

Loads credentials from `settings.gmail_oauth_token_path` -- a file path read from
`shared/config/settings.py::Settings`, never a literal in code -- and requests only the
`gmail.readonly` scope (spec 01: "The poller holds the OAuth token ... (scope
`gmail.readonly`)").

`GmailToolClient` is NOT exercised by this repo's test suite: it needs live Google
credentials and network access. Tests use `tests/unit/fakes.py::FakeGmailToolClient`
instead, which implements the same `ToolClient` protocol
(`shared/events/seams.py::ToolClient`) against scripted fixtures. Keep this module's
public surface (the `call("history.list", ...)` contract) in lockstep with what
`adapters/gmail/adapter.py` expects from either implementation.

The Google client libraries (`google-auth`, `google-api-python-client`) are an optional
dependency group (`pip install .[gmail]`) -- imported lazily inside `__init__` so that
importing this module (e.g. transitively, from a test that only needs the fake) never
requires them to be installed.
"""

from __future__ import annotations

from typing import Any

_GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class GmailToolClient:
    """Production `ToolClient` for Gmail: real OAuth credentials, real API calls.

    Only `history.list` is implemented (fanning out internally to
    `users.messages.get(format="raw")` for each changed message, so callers always get
    fully-hydrated message resources -- see the "Note on message hydration" in
    adapters/gmail/adapter.py). Any other `tool` name raises `NotImplementedError`.
    """

    def __init__(self, oauth_token_path: str, *, user_id: str = "me") -> None:
        # Imported lazily: these are an optional dependency (`pip install .[gmail]`),
        # and this constructor is the only place in the codebase allowed to touch real
        # Gmail credentials.
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credentials = Credentials.from_authorized_user_file(
            oauth_token_path, scopes=[_GMAIL_READONLY_SCOPE]
        )
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())

        self._user_id = user_id
        self._service = build("gmail", "v1", credentials=credentials)

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool != "history.list":
            raise NotImplementedError(f"GmailToolClient does not implement tool {tool!r}")
        return self._history_list(args)

    def _history_list(self, args: dict[str, Any]) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {"userId": self._user_id}
        if args.get("startHistoryId") is not None:
            request_kwargs["startHistoryId"] = args["startHistoryId"]

        response = self._service.users().history().list(**request_kwargs).execute()

        for entry in response.get("history", []):
            for bucket in ("messagesAdded", "messagesChanged"):
                for item in entry.get(bucket, []):
                    message_id = item["message"]["id"]
                    item["message"] = (
                        self._service.users()
                        .messages()
                        .get(userId=self._user_id, id=message_id, format="raw")
                        .execute()
                    )
        return response


def build_gmail_tool_client(oauth_token_path: str) -> GmailToolClient:
    """Construct the real `GmailToolClient` from a token file path (never a literal)."""
    return GmailToolClient(oauth_token_path)
