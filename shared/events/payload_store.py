"""Minimal local-filesystem, content-addressed payload store (spec 00 open item:
"`payload_ref` store backend (filesystem vs object store) and its retention/TTL wiring
(spec 07/09)" is explicitly called out as deferred).

Stores raw message payload bytes on local disk, keyed by the sha256 of the content, and
returns a `store://raw/<hash>`-style ref plus the digest itself (for
`CommonEventEnvelope.payload_sha256`). This is deliberately the simplest thing that could
work for E1: a real object-storage-backed implementation (S3/GCS-style, with
retention/TTL policy) is a documented fast-follow, not attempted here.

Never used to store anything but opaque bytes -- this module has no opinion about what's
inside the payload (raw RFC 822 message bytes, in the Gmail adapter's case).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_REF_PREFIX = "store://raw/"


class LocalPayloadStore:
    """Content-addressed store for raw message payload bytes on local disk.

    Idempotent by construction: storing the same bytes twice yields the same ref and
    does not rewrite the file on disk.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def put(self, payload: bytes) -> tuple[str, str]:
        """Store `payload`; return `(payload_ref, payload_sha256)`."""
        digest = hashlib.sha256(payload).hexdigest()
        path = self._base_dir / digest
        if not path.exists():
            path.write_bytes(payload)
        return f"{_REF_PREFIX}{digest}", digest

    def get(self, payload_ref: str) -> bytes:
        """Read back the raw bytes for a ref previously returned by `put`."""
        if not payload_ref.startswith(_REF_PREFIX):
            raise ValueError(f"unrecognized payload ref: {payload_ref!r}")
        digest = payload_ref[len(_REF_PREFIX) :]
        return (self._base_dir / digest).read_bytes()
