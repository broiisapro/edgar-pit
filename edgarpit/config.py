"""Application settings, loaded from the environment or a ``.env`` file.

The SEC requires a declared User-Agent of the form ``"Name email"`` on every
request (decision A-3); the rate limit is capped at 5 requests per second
(decision A-2). Both are read from the environment here so every entry point
(CLI, tests, services) sees the same configuration.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(ValueError):
    """A required setting is missing or malformed."""


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def validate_user_agent(value: str) -> str:
    """Validate a SEC User-Agent string.

    The value must be ``"Name email"``: at least one non-space token followed by
    an email address. Returns the stripped value unchanged when valid and raises
    :class:`ConfigError` with a clear message otherwise.
    """
    text = value.strip()
    parts = text.split()
    if len(parts) >= 2 and _EMAIL_RE.fullmatch(parts[-1]):
        return text
    raise ConfigError(
        'SEC_USER_AGENT must look like "Name email" '
        f'(e.g. "Jane Doe jane@example.com"); got {value!r}'
    )


class Settings(BaseSettings):
    """edgar-pit settings.

    Environment variables: ``SEC_USER_AGENT``, ``EDGARPIT_DB`` and
    ``EDGARPIT_RATE_LIMIT_RPS``; a ``.env`` file in the working directory is
    read when present (see ``.env.example``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    sec_user_agent: str | None = None
    rate_limit_rps: float = Field(
        default=5.0,
        gt=0,
        le=5,
        validation_alias=AliasChoices("EDGARPIT_RATE_LIMIT_RPS", "rate_limit_rps"),
    )
    edgarpit_db: Path = Field(
        default=Path("/tmp/ep/edgarpit.duckdb"),
        validation_alias=AliasChoices("EDGARPIT_DB", "edgarpit_db"),
    )
