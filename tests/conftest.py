"""Shared pytest fixtures.

The environment is isolated from the developer's shell: settings tests must not
depend on (or be broken by) locally exported ``SEC_USER_AGENT`` or
``EDGARPIT_*`` variables.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SEC_USER_AGENT", "EDGARPIT_DB", "EDGARPIT_RATE_LIMIT_RPS"):
        monkeypatch.delenv(name, raising=False)
