from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from config_manager import ConfigManager
from resilience import CircuitBreaker, retry_async

logger = logging.getLogger(__name__)

SERVICE_BREAKERS: dict[str, CircuitBreaker] = {}


def _app_float(key: str, default: float) -> float:
    try:
        return float(ConfigManager.get().get("app", {}).get(key, default))
    except (TypeError, ValueError):
        return default


def _app_int(key: str, default: int) -> int:
    try:
        return int(ConfigManager.get().get("app", {}).get(key, default))
    except (TypeError, ValueError):
        return default


def _breaker_for(name: str) -> CircuitBreaker:
    raw = ConfigManager.get().get("circuit_breaker", {})
    if not isinstance(raw, dict):
        raw = {}
    try:
        ft = int(raw.get("failure_threshold", 5))
    except (TypeError, ValueError):
        ft = 5
    try:
        rt = float(raw.get("recovery_timeout", 60.0))
    except (TypeError, ValueError):
        rt = 60.0
    existing = SERVICE_BREAKERS.get(name)
    if existing is None or existing.failure_threshold != ft or existing.recovery_timeout != rt:
        existing = CircuitBreaker(failure_threshold=ft, recovery_timeout=rt)
        SERVICE_BREAKERS[name] = existing
    return existing


@retry_async(max_retries=3, base_delay=1.0, max_delay=10.0)
async def _http_probe_service(
    session: aiohttp.ClientSession,
    name: str,
    url: str,
    timeout_total: float,
    degraded_after: float,
) -> dict[str, Any]:
    start_time = time.time()
    client_timeout = aiohttp.ClientTimeout(total=timeout_total)
    status = "DOWN"
    response_time_ms: float | None = None
    async with session.get(str(url), timeout=client_timeout) as response:
        elapsed = time.time() - start_time
        if response.status == 200:
            if elapsed > degraded_after:
                status = "DEGRADED"
            else:
                status = "UP"
            response_time_ms = round(elapsed * 1000, 1)
        else:
            status = "DOWN"
    if status == "DOWN":
        response_time_ms = None
    return {"name": name, "url": str(url), "status": status, "response_time_ms": response_time_ms}


async def check_single_service(session: aiohttp.ClientSession, service: dict[str, Any]) -> dict[str, Any]:
    """Run one HTTP health probe with retries and a per-service circuit breaker.

    Args:
        session: Shared ``aiohttp`` client session.
        service: Service dict from config (must include ``name`` and optional ``url``).

    Returns:
        Dict with ``name``, ``url``, ``status`` (UP/DEGRADED/DOWN/CIRCUIT_OPEN), and ``response_time_ms``.
    """
    name = str(service.get("name", "Unknown"))
    url = service.get("url")
    timeout_total = _app_float("health_check_timeout", 10.0)
    degraded_after = _app_float("degraded_threshold", 2.0)

    if not url:
        return {"name": name, "url": "", "status": "DOWN", "response_time_ms": None}

    br = _breaker_for(name)
    if br.is_open():
        return br.circuit_open_view(name)

    try:
        result = await _http_probe_service(session, name, str(url), timeout_total, degraded_after)
        if result.get("status") in ("UP", "DEGRADED"):
            br.record_success(result)
        else:
            br.record_failure()
        return result
    except (aiohttp.ClientError, aiohttp.ClientPayloadError, asyncio.TimeoutError, ConnectionError) as e:
        br.record_failure()
        logger.debug("check_single_service %s (%s) after retries: %s", name, url, e)
        return {"name": name, "url": str(url), "status": "DOWN", "response_time_ms": None}


async def check_services() -> list[dict[str, Any]]:
    """Check every configured service in parallel.

    Returns:
        List of status dicts from :func:`check_single_service` (empty if no services).
    """
    config = ConfigManager.get()
    services = config.get("services", [])

    if not isinstance(services, list) or not services:
        return []

    async with aiohttp.ClientSession() as session:
        tasks = [check_single_service(session, s) for s in services if isinstance(s, dict)]
        results: list[dict[str, Any]] = await asyncio.gather(*tasks)

    return results


@retry_async(max_retries=3, base_delay=1.0, max_delay=10.0)
async def _n8n_fetch(exec_url: str, headers: dict[str, str], client_timeout: aiohttp.ClientTimeout) -> list[str]:
    async with aiohttp.ClientSession() as session:
        async with session.get(exec_url, headers=headers, timeout=client_timeout) as response:
            if response.status != 200:
                return [f"n8n API error: {response.status}"]
            try:
                data = await response.json()
            except (aiohttp.ClientPayloadError, aiohttp.ContentTypeError, ValueError) as e:
                logger.warning("get_n8n_executions: некорректный JSON ответ: %s", e)
                return ["n8n API is unreachable"]
            if not isinstance(data, dict):
                return ["n8n API is unreachable"]
            executions = data.get("data", [])
            if not isinstance(executions, list):
                return ["n8n API is unreachable"]
            results: list[str] = []
            for exec_data in executions:
                if not isinstance(exec_data, dict):
                    continue
                try:
                    exec_id = exec_data.get("id", "?")
                    status = str(exec_data.get("status", "UNKNOWN")).upper()
                    wf = exec_data.get("workflowData") or {}
                    if not isinstance(wf, dict):
                        wf = {}
                    workflow_name = str(wf.get("name", "Unknown Workflow"))
                    results.append(f"[{exec_id}] {workflow_name} - [{status}]")
                except (KeyError, TypeError) as e:
                    logger.warning("get_n8n_executions: пропуск записи: %s", e)
            return results if results else ["No recent executions"]


async def get_n8n_executions() -> list[str]:
    """Fetch recent n8n executions from the configured API.

    Returns:
        Human-readable lines, or a single error/unreachable message.
    """
    config = ConfigManager.get()
    n8n_config = config.get("n8n_api")
    if not isinstance(n8n_config, dict):
        return ["n8n API is unreachable"]

    url = n8n_config.get("url")
    header_key = str(n8n_config.get("header_key", "X-N8N-API-KEY"))
    env_name = str(n8n_config.get("api_key_env", "N8N_API_KEY"))
    api_key = ConfigManager.get_secret(env_name)
    headers = {header_key: api_key}

    timeout_total = _app_float("health_check_timeout", 10.0)
    client_timeout = aiohttp.ClientTimeout(total=timeout_total)
    exec_url = f"{url}?limit=5" if "?" not in str(url) else f"{url}&limit=5"

    try:
        return await _n8n_fetch(exec_url, headers, client_timeout)
    except (aiohttp.ClientError, aiohttp.ClientPayloadError, asyncio.TimeoutError, ConnectionError) as e:
        logger.debug("get_n8n_executions after retries: %s", e)
        return ["n8n API is unreachable"]


@retry_async(max_retries=3, base_delay=1.0, max_delay=10.0)
async def _langfuse_fetch(
    url: str,
    public_key: str,
    secret_key: str,
    client_timeout: aiohttp.ClientTimeout,
) -> dict[str, float | int]:
    auth = aiohttp.BasicAuth(public_key, secret_key)
    async with aiohttp.ClientSession() as session:
        async with session.get(str(url), auth=auth, timeout=client_timeout) as response:
            if response.status != 200:
                logger.warning("get_langfuse_metrics: HTTP %s", response.status)
                return {"cost": 0.0, "traces": 0}
            try:
                data = await response.json()
            except (aiohttp.ClientPayloadError, aiohttp.ContentTypeError, ValueError) as e:
                logger.warning("get_langfuse_metrics: JSON ошибка: %s", e)
                return {"cost": 0.0, "traces": 0}
            if not isinstance(data, dict):
                return {"cost": 0.0, "traces": 0}
            inner = data.get("data")
            if isinstance(inner, dict):
                cost = inner.get("totalCost", 0.0)
                traces = inner.get("totalTraces", 0)
            else:
                cost = data.get("totalCost", 0.0)
                traces = data.get("totalTraces", 0)
            try:
                cost_f = float(cost)
            except (TypeError, ValueError):
                cost_f = 0.0
            try:
                traces_i = int(traces)
            except (TypeError, ValueError):
                traces_i = 0
            return {"cost": cost_f, "traces": traces_i}


async def get_langfuse_metrics() -> dict[str, float | int]:
    """Fetch Langfuse cost and trace totals from the metrics API.

    Returns:
        ``{"cost": float, "traces": int}`` or zeros on error/offline.
    """
    config = ConfigManager.get()
    lf_config = config.get("langfuse_api")
    if not isinstance(lf_config, dict):
        return {"cost": 0.0, "traces": 0}

    url = lf_config.get("url")
    pub_env = str(lf_config.get("public_key_env", "LANGFUSE_PUBLIC_KEY"))
    sec_env = str(lf_config.get("secret_key_env", "LANGFUSE_SECRET_KEY"))
    public_key = ConfigManager.get_secret(pub_env)
    secret_key = ConfigManager.get_secret(sec_env)

    timeout_total = _app_float("health_check_timeout", 10.0)
    client_timeout = aiohttp.ClientTimeout(total=timeout_total)

    try:
        return await _langfuse_fetch(str(url), public_key, secret_key, client_timeout)
    except (aiohttp.ClientError, aiohttp.ClientPayloadError, asyncio.TimeoutError, ConnectionError) as e:
        logger.debug("get_langfuse_metrics after retries: %s", e)
        return {"cost": 0.0, "traces": 0}


@retry_async(max_retries=3, base_delay=1.0, max_delay=10.0)
async def _qdrant_fetch(url: str, client_timeout: aiohttp.ClientTimeout) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get(str(url), timeout=client_timeout) as response:
            if response.status != 200:
                logger.warning("get_qdrant_context_size: HTTP %s", response.status)
                return "Qdrant: offline"
            try:
                data = await response.json()
            except (aiohttp.ClientPayloadError, aiohttp.ContentTypeError, ValueError) as e:
                logger.warning("get_qdrant_context_size: JSON ошибка: %s", e)
                return "Qdrant: offline"
            if not isinstance(data, dict):
                return "Qdrant: offline"
            result = data.get("result", {})
            if not isinstance(result, dict):
                return "Qdrant: offline"
            collections = result.get("collections", [])
            if not collections:
                return "Collections: 0"
            if not isinstance(collections, list):
                return "Qdrant: offline"
            names: list[str] = []
            for c in collections:
                if isinstance(c, dict) and c.get("name"):
                    names.append(str(c["name"]))
            return f"Collections: {', '.join(names)}" if names else "Collections: 0"


async def get_qdrant_context_size() -> str:
    """List Qdrant collection names from the collections API.

    Returns:
        A short summary string such as ``Collections: a, b`` or ``Qdrant: offline``.
    """
    config = ConfigManager.get()
    qdrant_config = config.get("qdrant_api")
    if not isinstance(qdrant_config, dict):
        return "Qdrant: offline"

    url = qdrant_config.get("url")
    timeout_total = _app_float("health_check_timeout", 10.0)
    client_timeout = aiohttp.ClientTimeout(total=timeout_total)

    try:
        return await _qdrant_fetch(str(url), client_timeout)
    except (aiohttp.ClientError, aiohttp.ClientPayloadError, asyncio.TimeoutError, ConnectionError) as e:
        logger.debug("get_qdrant_context_size after retries: %s", e)
        return "Qdrant: offline"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = asyncio.run(check_services())
    for res in results:
        logger.info("[%s] %s (%s) rt=%s", res["status"], res["name"], res["url"], res.get("response_time_ms"))
