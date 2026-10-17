"""Tests for :mod:`edgarpit.config`."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from edgarpit.config import ConfigError, Settings, validate_user_agent

VALID_USER_AGENTS = [
    "Jane Doe jane@example.com",
    "Moksh Siruvani moksh.siruvani@gmail.com",
    "Acme Research ops@acme.co.uk",
    "a b c@d.io",
]

INVALID_USER_AGENTS = [
    "",
    "Jane Doe",
    "jane@example.com",
    "Jane <jane@example.com>",
    "jane@example.com Jane",
    "Jane Doe jane@example",
    "Jane Doe @example.com",
    "Jane Doe jane@@example.com",
]


@pytest.mark.parametrize("value", VALID_USER_AGENTS)
def test_validate_user_agent_accepts_name_and_email(value: str) -> None:
    assert validate_user_agent(value) == value


@pytest.mark.parametrize("value", INVALID_USER_AGENTS)
def test_validate_user_agent_rejects_malformed_values(value: str) -> None:
    with pytest.raises(ConfigError):
        validate_user_agent(value)


def test_validate_user_agent_strips_surrounding_whitespace() -> None:
    assert validate_user_agent("  Jane Doe jane@example.com  ") == "Jane Doe jane@example.com"


def test_settings_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.sec_user_agent is None
    assert settings.rate_limit_rps == 5.0
    assert settings.edgarpit_db == Path("/tmp/ep/edgarpit.duckdb")


def test_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "Jane Doe jane@example.com")
    monkeypatch.setenv("EDGARPIT_RATE_LIMIT_RPS", "3")
    monkeypatch.setenv("EDGARPIT_DB", str(tmp_path / "custom.duckdb"))

    settings = Settings(_env_file=None)

    assert settings.sec_user_agent == "Jane Doe jane@example.com"
    assert settings.rate_limit_rps == 3.0
    assert settings.edgarpit_db == tmp_path / "custom.duckdb"


def test_settings_rate_limit_bounds() -> None:
    assert Settings(_env_file=None, rate_limit_rps=5).rate_limit_rps == 5
    assert Settings(_env_file=None, rate_limit_rps=0.5).rate_limit_rps == 0.5
    for bad in (0, -1, 5.01, 10):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, rate_limit_rps=bad)


def test_settings_reject_non_numeric_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGARPIT_RATE_LIMIT_RPS", "fast")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
