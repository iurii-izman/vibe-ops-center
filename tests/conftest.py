"""Shared pytest fixtures."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from config_manager import ConfigManager
from storage import Storage

MINIMAL_VALID_YAML = textwrap.dedent(
    """
    app:
      polling_interval: 5
      health_check_timeout: 10
      degraded_threshold: 2.0
      autoheal_threshold: 3
      command_timeout: 60

    circuit_breaker:
      failure_threshold: 3
      recovery_timeout: 1

    notifications:
      sound_enabled: false

    services:
      - name: TestSvc
        url: http://127.0.0.1:18080/health
        type: process
        depends_on: []
        stop_cmd: ["echo", "stop"]
        start_cmd: ["echo", "start"]

    langfuse_api:
      url: http://127.0.0.1:13000/api/public/metrics
      public_key_env: LANGFUSE_PUBLIC_KEY
      secret_key_env: LANGFUSE_SECRET_KEY
      daily_budget_limit: 10.0

    n8n_api:
      url: http://127.0.0.1:15678/api/v1/executions
      header_key: X-N8N-API-KEY
      api_key_env: N8N_API_KEY

    qdrant_api:
      url: http://127.0.0.1:16333/collections

    scripts:
      profile_local_cmd: ["echo", "local"]
      profile_cloud_cmd: ["echo", "cloud"]

    tests:
      replyline_webhook: "echo ok"
    """
).strip()


@pytest.fixture(autouse=True)
def reset_singleton_and_breakers() -> Any:
    import checker

    ConfigManager.reset_singleton()
    checker.SERVICE_BREAKERS.clear()
    yield
    ConfigManager.reset_singleton()
    checker.SERVICE_BREAKERS.clear()


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Valid ``config.yaml`` on disk; loads into ``ConfigManager`` singleton."""
    p = tmp_path / "config.yaml"
    p.write_text(MINIMAL_VALID_YAML, encoding="utf-8")
    ConfigManager.reset_singleton()
    ConfigManager.reload(p)
    return p


@pytest.fixture
def tmp_db(tmp_path: Path) -> Storage:
    """Isolated SQLite file for ``Storage`` tests."""
    return Storage(tmp_path / "test_vibe_ops.db")


@pytest.fixture
def mock_aiohttp() -> Any:
    """``aioresponses`` context for mocking outbound HTTP."""
    try:
        from aioresponses import aioresponses
    except ImportError as e:  # pragma: no cover
        pytest.skip(f"aioresponses not installed: {e}")
    with aioresponses() as m:
        yield m
