"""Tests for ``resilience`` retry and circuit breaker."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from resilience import CircuitBreaker, CircuitState, retry_async


async def _noop_sleep(*_a: object, **_k: object) -> None:
    return None


@pytest.mark.asyncio
async def test_retry_success_first_try(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    @retry_async(max_retries=2, base_delay=0.01, max_delay=0.05)
    async def ok() -> str:
        return "yes"

    assert await ok() == "yes"


@pytest.mark.asyncio
async def test_retry_success_after_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr("resilience.random.uniform", lambda *_a, **_k: 0.0)
    n = {"v": 0}

    @retry_async(max_retries=3, base_delay=0.01, max_delay=0.05)
    async def flaky() -> str:
        n["v"] += 1
        if n["v"] < 3:
            raise aiohttp.ClientConnectionError("x")
        return "ok"

    assert await flaky() == "ok"


@pytest.mark.asyncio
async def test_retry_all_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr("resilience.random.uniform", lambda *_a, **_k: 0.0)

    @retry_async(max_retries=2, base_delay=0.01, max_delay=0.05)
    async def bad() -> None:
        raise aiohttp.ClientConnectionError("e")

    with pytest.raises(aiohttp.ClientConnectionError):
        await bad()


@pytest.mark.asyncio
async def test_retry_all_failures_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr("resilience.random.uniform", lambda *_a, **_k: 0.0)

    @retry_async(max_retries=1, base_delay=0.01, max_delay=0.05, fallback=lambda: 7)
    async def bad() -> int:
        raise aiohttp.ClientConnectionError("e")

    assert await bad() == 7


def test_circuit_breaker_opens() -> None:
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_circuit_breaker_half_open(monkeypatch: pytest.MonkeyPatch) -> None:
    t = {"now": 0.0}
    monkeypatch.setattr("resilience.time.monotonic", lambda: t["now"])
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    t["now"] = 0.1
    assert not cb.is_open()
    assert cb.state == CircuitState.HALF_OPEN


def test_circuit_breaker_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    t = {"now": 0.0}
    monkeypatch.setattr("resilience.time.monotonic", lambda: t["now"])
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
    cb.record_failure()
    cb.record_failure()
    t["now"] = 0.1
    assert not cb.is_open()
    cb.record_success({"name": "n", "url": "u", "status": "UP", "response_time_ms": 1.0})
    assert cb.state == CircuitState.CLOSED
