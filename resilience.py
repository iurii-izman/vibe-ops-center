"""Retry with backoff and per-service circuit breaker."""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from collections.abc import Awaitable, Callable
from enum import Enum, auto
from typing import Any, TypeVar

import aiohttp

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


class CircuitBreaker:
    """Protect downstream calls with CLOSED / OPEN / HALF_OPEN failure semantics.

    After ``failure_threshold`` consecutive failures the breaker opens; after
    ``recovery_timeout`` seconds it allows a single probe (HALF_OPEN).

    Args:
        failure_threshold: Number of failures before opening the circuit.
        recovery_timeout: Seconds before transitioning OPEN → HALF_OPEN.
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_timeout = float(recovery_timeout)
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._last_result: dict[str, Any] | None = None

    @property
    def state(self) -> CircuitState:
        return self._state

    def is_open(self) -> bool:
        if self._state != CircuitState.OPEN:
            return False
        if time.monotonic() - self._opened_at >= self.recovery_timeout:
            self._state = CircuitState.HALF_OPEN
            return False
        return True

    def record_success(self, result: dict[str, Any]) -> None:
        self._last_result = dict(result)
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
            return
        if self._consecutive_failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()

    def circuit_open_view(self, name: str) -> dict[str, Any]:
        """Synthetic row when OPEN and recovery not elapsed."""
        base = dict(self._last_result) if self._last_result else {}
        base.setdefault("name", name)
        base.setdefault("url", "")
        base["status"] = "CIRCUIT_OPEN"
        base["response_time_ms"] = None
        return base


def retry_async(
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    fallback: Callable[[], Awaitable[T]] | Callable[[], T] | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorate an async function to retry on transient network errors.

    Args:
        max_retries: Number of *retry attempts* after the first failure (total tries = max_retries + 1).
        base_delay: Initial backoff base in seconds (exponential factor 2**attempt).
        max_delay: Cap on sleep between attempts.
        fallback: Optional sync/async callable invoked after all attempts fail instead of re-raising.

    Returns:
        A decorator that wraps async callables.
    """

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc: BaseException | None = None
            for attempt in range(max_retries + 1):
                try:
                    return await fn(*args, **kwargs)
                except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
                    last_exc = e
                    if attempt >= max_retries:
                        logger.debug(
                            "Retry %s/%s for %s: %s (giving up)",
                            attempt + 1,
                            max_retries,
                            fn.__name__,
                            e,
                        )
                        if fallback is not None:
                            out = fallback()
                            if asyncio.iscoroutine(out):
                                return await out  # type: ignore[return-value]
                            return out  # type: ignore[return-value]
                        raise
                    delay = min(max_delay, base_delay * (2**attempt) + random.uniform(0.0, 0.5))
                    logger.debug(
                        "Retry %s/%s for %s: %s (sleep %.2fs)",
                        attempt + 1,
                        max_retries,
                        fn.__name__,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
            raise last_exc  # pragma: no cover

        return wrapper

    return decorator


def retry_sync(
    *,
    max_retries: int = 2,
    delay: float = 2.0,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorate a sync function to retry on subprocess or OS errors.

    Args:
        max_retries: Number of retries after the first failure.
        delay: Fixed sleep in seconds between attempts.

    Returns:
        A decorator for synchronous callables.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            import subprocess

            last_exc: BaseException | None = None
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except (subprocess.TimeoutExpired, OSError) as e:
                    last_exc = e
                    if attempt >= max_retries:
                        logger.debug(
                            "retry_sync %s/%s for %s: %s (giving up)",
                            attempt + 1,
                            max_retries,
                            fn.__name__,
                            e,
                        )
                        raise
                    logger.debug(
                        "retry_sync %s/%s for %s: %s (sleep %.2fs)",
                        attempt + 1,
                        max_retries,
                        fn.__name__,
                        e,
                        delay,
                    )
                    time.sleep(delay)
            raise last_exc  # pragma: no cover

        return wrapper

    return decorator
