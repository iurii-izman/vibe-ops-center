"""Tests for ``checker`` HTTP helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import pytest

from checker import (
    check_single_service,
    get_langfuse_metrics,
    get_n8n_executions,
    get_qdrant_context_size,
)
from config_manager import ConfigManager


@pytest.mark.asyncio
async def test_service_up(tmp_config: Path, mock_aiohttp: object) -> None:
    url = "http://127.0.0.1:18080/health"
    mock_aiohttp.get(url, status=200, body=b"ok", repeat=True)
    async with aiohttp.ClientSession() as session:
        r = await check_single_service(session, {"name": "TestSvc", "url": url})
    assert r["status"] == "UP"
    assert r["response_time_ms"] is not None


@pytest.mark.asyncio
async def test_service_degraded(tmp_config: Path, mock_aiohttp: object, monkeypatch: pytest.MonkeyPatch) -> None:
    import checker

    url = "http://127.0.0.1:18080/health"
    mock_aiohttp.get(url, status=200, body=b"ok", repeat=True)
    monkeypatch.setattr(checker.time, "time", MagicMock(side_effect=[1000.0, 1003.5]))
    async with aiohttp.ClientSession() as session:
        r = await check_single_service(session, {"name": "TestSvc", "url": url})
    assert r["status"] == "DEGRADED"


@pytest.mark.asyncio
async def test_service_down(tmp_config: Path, mock_aiohttp: object) -> None:
    url = "http://127.0.0.1:18080/health"
    mock_aiohttp.get(url, status=500, body=b"err", repeat=True)
    async with aiohttp.ClientSession() as session:
        r = await check_single_service(session, {"name": "TestSvc", "url": url})
    assert r["status"] == "DOWN"


@pytest.mark.asyncio
async def test_service_timeout(tmp_config: Path, mock_aiohttp: object) -> None:
    url = "http://127.0.0.1:18080/health"
    mock_aiohttp.get(url, exception=asyncio.TimeoutError(), repeat=True)
    async with aiohttp.ClientSession() as session:
        r = await check_single_service(session, {"name": "TestSvc", "url": url})
    assert r["status"] == "DOWN"


@pytest.mark.asyncio
async def test_response_time_included(tmp_config: Path, mock_aiohttp: object) -> None:
    url = "http://127.0.0.1:18080/health"
    mock_aiohttp.get(url, status=200, body=b"x", repeat=True)
    async with aiohttp.ClientSession() as session:
        r = await check_single_service(session, {"name": "TestSvc", "url": url})
    assert isinstance(r.get("response_time_ms"), (int, float))


@pytest.mark.asyncio
async def test_langfuse_metrics_success(monkeypatch: pytest.MonkeyPatch, mock_aiohttp: object) -> None:
    monkeypatch.setenv("LFP", "p")
    monkeypatch.setenv("LFS", "s")
    monkeypatch.setattr(
        ConfigManager,
        "get",
        staticmethod(
            lambda config_path=None: {
                "langfuse_api": {
                    "url": "http://lf.test/metrics",
                    "public_key_env": "LFP",
                    "secret_key_env": "LFS",
                    "daily_budget_limit": 1.0,
                },
                "app": {"health_check_timeout": 5},
            }
        ),
    )
    mock_aiohttp.get(
        "http://lf.test/metrics",
        payload={"data": {"totalCost": 1.5, "totalTraces": 42}},
        repeat=True,
    )
    out = await get_langfuse_metrics()
    assert out == {"cost": 1.5, "traces": 42}


@pytest.mark.asyncio
async def test_langfuse_metrics_error(monkeypatch: pytest.MonkeyPatch, mock_aiohttp: object) -> None:
    monkeypatch.setenv("LFP", "p")
    monkeypatch.setenv("LFS", "s")
    monkeypatch.setattr(
        ConfigManager,
        "get",
        staticmethod(
            lambda config_path=None: {
                "langfuse_api": {
                    "url": "http://lf.test/metrics",
                    "public_key_env": "LFP",
                    "secret_key_env": "LFS",
                    "daily_budget_limit": 1.0,
                },
                "app": {"health_check_timeout": 5},
            }
        ),
    )
    mock_aiohttp.get("http://lf.test/metrics", exception=aiohttp.ClientConnectionError("boom"), repeat=True)
    out = await get_langfuse_metrics()
    assert out == {"cost": 0.0, "traces": 0}


@pytest.mark.asyncio
async def test_qdrant_collections(monkeypatch: pytest.MonkeyPatch, mock_aiohttp: object) -> None:
    monkeypatch.setattr(
        ConfigManager,
        "get",
        staticmethod(
            lambda config_path=None: {
                "qdrant_api": {"url": "http://qd.test/collections"},
                "app": {"health_check_timeout": 5},
            }
        ),
    )
    mock_aiohttp.get(
        "http://qd.test/collections",
        payload={"result": {"collections": [{"name": "docs"}, {"name": "cache"}]}},
        repeat=True,
    )
    s = await get_qdrant_context_size()
    assert "docs" in s and "cache" in s


@pytest.mark.asyncio
async def test_n8n_executions(monkeypatch: pytest.MonkeyPatch, mock_aiohttp: object) -> None:
    monkeypatch.setenv("N8N_API_KEY", "k")
    monkeypatch.setattr(
        ConfigManager,
        "get",
        staticmethod(
            lambda config_path=None: {
                "n8n_api": {
                    "url": "http://n8n.test/api/v1/executions",
                    "header_key": "X-N8N-API-KEY",
                    "api_key_env": "N8N_API_KEY",
                },
                "app": {"health_check_timeout": 5},
            }
        ),
    )
    mock_aiohttp.get(
        "http://n8n.test/api/v1/executions?limit=5",
        payload={
            "data": [
                {
                    "id": 7,
                    "status": "success",
                    "workflowData": {"name": "MyWF"},
                }
            ]
        },
        repeat=True,
    )
    lines = await get_n8n_executions()
    assert any("MyWF" in ln for ln in lines)
