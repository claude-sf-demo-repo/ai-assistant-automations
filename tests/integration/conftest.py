"""Registers the shared real-Postgres fixture (tests/integration/pg_fixture.py) so every
integration test module gets `pg_dsn` without importing it manually.
"""

pytest_plugins = ["tests.integration.pg_fixture"]
