"""Pydantic-settings configuration, loaded from environment / `.env` (never from literals).

Per spec 00's acceptance criteria: "`.env.example` + pydantic-settings config module load all
settings; no secret is read from a hard-coded literal." No field below has a real secret as
its default.

The Gmail OAuth token file is treated as "the secrets store" for E1: it lives outside the repo
at the path pointed to by `gmail_oauth_token_path`, and is never committed (see `.gitignore`).
A real secrets manager (e.g. a cloud KMS-backed store) is a documented fast-follow, not in
scope for E1.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str
    gmail_account_ref: str = "personal"
    gmail_oauth_token_path: str
    poll_interval_seconds: int = 60
    poison_threshold: int = 5
    log_level: str = "INFO"
    # Local-filesystem content-addressed payload store (shared/events/payload_store.py).
    # Object storage is a documented fast-follow (spec 00 open items), not E1 scope.
    payload_store_dir: str = "var/payloads"
