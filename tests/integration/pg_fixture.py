"""Dual-backend real-Postgres fixture for E1 integration tests (Task 4/5/6).

ALL integration tests that need a real Postgres consume the ONE fixture this module
exports -- `pg_dsn` -- never a hand-rolled second way to get a test database.

Backend selection (`pg_dsn` -> session-scoped `_pg_cluster` underneath):
1. testcontainers, if the package is importable AND a Docker daemon actually responds
   AND `postgres:16` can actually be pulled. On this dev machine none of that holds --
   `docker info` succeeds (Docker Desktop is running) but `docker pull postgres:16`
   fails with an org-membership sign-in error, so this branch is exercised only far
   enough to observe the pull failure and fall through. It is structurally correct
   (import-guarded, never raises out of `_try_testcontainers`) but NOT exercised
   end-to-end here -- do not treat it as tested until it's re-run where Docker access
   works.
2. A local ephemeral cluster: `initdb` a fresh data directory under a session-scoped
   tmp dir, `pg_ctl start` on a free localhost port with `trust` auth (a throwaway
   single-use cluster, never reachable off localhost, needs no password), wait for
   `pg_isready`, then `pg_ctl stop -m fast` + directory cleanup at session end. Homebrew
   binaries are located via `shutil.which` first (this dev machine's PATH already
   includes `/opt/homebrew/bin`, which symlinks to `/opt/homebrew/opt/postgresql@16/
   bin`), falling back to the literal versioned path if `which` comes up empty.
3. If neither backend is available: `pytest.skip(...)` with a clear message. Tests
   built against this fixture never silently pass and never fall back to a mock.

`pg_dsn` (function-scoped) runs the real Alembic migration (storage/migrations) against
the cluster once per session, then TRUNCATEs every E1 table before each test for
isolation -- cheaper than tearing down/rebuilding a whole cluster per test, and still
gives every test a schema built by the real migration rather than a hand-rolled one.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Iterator

import psycopg
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_FALLBACK_BIN_DIR = Path("/opt/homebrew/opt/postgresql@16/bin")

# Every table Task 4+ migrations create. TRUNCATEd between tests by `pg_dsn`.
_E1_TABLES = ["ingest_queue", "gmail_cursor", "ingest_dlq", "thread_fold_projection"]


def _find_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    fallback = _FALLBACK_BIN_DIR / name
    if fallback.exists():
        return str(fallback)
    return None


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Cluster:
    """A running Postgres instance plus the means to tear it down."""

    def __init__(self, dsn: str, stop: "callable[[], None]") -> None:
        self.dsn = dsn
        self._stop = stop

    def stop(self) -> None:
        self._stop()


def _try_testcontainers() -> _Cluster | None:
    """Testcontainers backend. Import-guarded; never raises -- returns `None` on any
    failure so `_pg_cluster` can fall through to the local-cluster backend.

    NOT exercised end-to-end on this dev machine (Docker daemon responds, but pulling
    `postgres:16` fails with an org sign-in requirement) -- see module docstring.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        return None

    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5, check=False
        )
        if result.returncode != 0:
            return None
        # `docker info` only proves the daemon responds, not that we can pull images
        # (registry auth is a separate failure mode -- exactly what's blocked here).
        pulled = subprocess.run(
            ["docker", "pull", "postgres:16"], capture_output=True, timeout=120, check=False
        )
        if pulled.returncode != 0:
            return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    try:
        container = PostgresContainer("postgres:16", driver="psycopg")
        container.start()
    except Exception:
        return None

    def _stop() -> None:
        container.stop()

    return _Cluster(container.get_connection_url(), _stop)


def _start_local_cluster(base_dir: Path) -> _Cluster | None:
    """Local ephemeral cluster backend: `initdb` + `pg_ctl start` under `base_dir`."""
    initdb = _find_binary("initdb")
    pg_ctl = _find_binary("pg_ctl")
    pg_isready = _find_binary("pg_isready")
    if not (initdb and pg_ctl and pg_isready):
        return None

    data_dir = base_dir / "pgdata"
    log_file = base_dir / "postgres.log"
    port = _free_tcp_port()

    subprocess.run(
        [
            initdb,
            "-D", str(data_dir),
            "-U", "postgres",
            "-A", "trust",
            "--auth-host=trust",
            "--auth-local=trust",
            "-E", "UTF8",
            "--no-locale",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            pg_ctl,
            "-D", str(data_dir),
            # No Unix-domain socket: pytest's tmp dir path is routinely longer than the
            # ~103-byte limit on a Unix socket path, which would otherwise make the
            # server fail to start. We only ever connect over TCP (127.0.0.1) anyway.
            "-o", f"-p {port} -h 127.0.0.1 -c unix_socket_directories=''",
            "-w",
            "-t", "30",
            "-l", str(log_file),
            "start",
        ],
        check=True,
        capture_output=True,
    )

    deadline = time.monotonic() + 15
    ready = False
    while time.monotonic() < deadline:
        probe = subprocess.run(
            [pg_isready, "-h", "127.0.0.1", "-p", str(port), "-U", "postgres"],
            capture_output=True,
        )
        if probe.returncode == 0:
            ready = True
            break
        time.sleep(0.3)
    if not ready:
        subprocess.run([pg_ctl, "-D", str(data_dir), "-m", "fast", "stop"], capture_output=True)
        raise RuntimeError(
            f"local Postgres cluster never became ready; see log at {log_file}"
        )

    dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"

    def _stop() -> None:
        subprocess.run([pg_ctl, "-D", str(data_dir), "-m", "fast", "stop"], capture_output=True)

    return _Cluster(dsn, _stop)


def _run_migrations(dsn: str) -> None:
    """Run the real Alembic migration (storage/migrations) against `dsn`.

    `storage/migrations/env.py` reads `DATABASE_URL` from the environment (via
    `shared.config.settings.Settings`, falling back to a bare env read) rather than
    from `alembic.ini` -- so the DSN is threaded through via the environment, not
    `Config.set_main_option`.
    """
    from alembic import command
    from alembic.config import Config

    old_dsn = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn
    try:
        cfg = Config(str(_REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_REPO_ROOT / "storage" / "migrations"))
        command.upgrade(cfg, "head")
    finally:
        if old_dsn is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_dsn


@pytest.fixture(scope="session")
def _pg_cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Session-scoped: start exactly one Postgres (testcontainers or local), migrate it
    once, yield its DSN, tear it down at session end. Private to this module -- tests
    depend on `pg_dsn` below, not this fixture directly.
    """
    cluster = _try_testcontainers()
    if cluster is None:
        base_dir = tmp_path_factory.mktemp("pg-local-cluster")
        cluster = _start_local_cluster(base_dir)

    if cluster is None:
        pytest.skip(
            "No real Postgres backend available: testcontainers could not start "
            "postgres:16 (Docker daemon/image pull unavailable) AND the local "
            "initdb/pg_ctl fallback could not find its binaries. Skipping rather than "
            "silently passing or falling back to a mock."
        )

    try:
        _run_migrations(cluster.dsn)
        yield cluster.dsn
    finally:
        cluster.stop()


@pytest.fixture
def pg_dsn(_pg_cluster: str) -> Iterator[str]:
    """The ONE fixture E1 integration tests consume for a real Postgres DSN.

    Truncates every E1 table before yielding, so each test starts from an empty
    schema regardless of what earlier tests left behind.
    """
    with psycopg.connect(_pg_cluster, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE {', '.join(_E1_TABLES)} RESTART IDENTITY CASCADE")
    yield _pg_cluster
