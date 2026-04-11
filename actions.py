from __future__ import annotations

import json
import logging
import subprocess
import time
from typing import Any

from config_manager import ConfigManager
from platform_utils import (
    default_compose_file,
    get_docker_desktop_path,
    get_process_flags,
    get_process_flags_no_detach,
    resolve_home_path,
)
from resilience import retry_sync

logger = logging.getLogger(__name__)


def _services_list(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw = config.get("services", [])
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, dict) and s.get("name")]


def get_startup_order(config: dict[str, Any]) -> list[str]:
    """Return service names in dependency order (dependencies before dependents).

    Args:
        config: Parsed ``config.yaml`` root dict.

    Returns:
        Ordered list of service names, or YAML order if ``depends_on`` has a cycle.
    """
    services = _services_list(config)
    if not services:
        return []
    names = [str(s["name"]) for s in services]
    name_set = set(names)

    depends_on: dict[str, list[str]] = {}
    for s in services:
        n = str(s["name"])
        raw = s.get("depends_on")
        if raw is None:
            deps: list[str] = []
        elif isinstance(raw, list):
            deps = [str(x) for x in raw if str(x) in name_set]
        else:
            deps = []
        depends_on[n] = deps

    in_degree = {n: len(depends_on.get(n, [])) for n in names}
    rev: dict[str, list[str]] = {n: [] for n in names}
    for n in names:
        for dep in depends_on.get(n, []):
            rev.setdefault(dep, []).append(n)

    queue = sorted([n for n in names if in_degree[n] == 0])
    order: list[str] = []
    while queue:
        u = queue.pop(0)
        order.append(u)
        for v in sorted(rev.get(u, [])):
            in_degree[v] -= 1
            if in_degree[v] == 0:
                queue.append(v)
        queue.sort()

    if len(order) != len(names):
        logger.warning("Circular depends_on in services; fallback to YAML order")
        return names
    return order


def get_shutdown_order(config: dict[str, Any]) -> list[str]:
    """Return service names in reverse startup order for safe shutdown.

    Args:
        config: Parsed ``config.yaml`` root dict.

    Returns:
        Names in reverse topological order relative to :func:`get_startup_order`.
    """
    return list(reversed(get_startup_order(config)))


def _service_by_name(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(s["name"]): s for s in _services_list(config)}


def _stop_one_service(name: str, config: dict[str, Any]) -> str:
    svc = _service_by_name(config).get(name)
    if not svc:
        return f"skip {name}: not in config"
    stop_cmd = svc.get("stop_cmd")
    if not isinstance(stop_cmd, list) or not stop_cmd:
        return f"skip {name}: no stop_cmd"
    logger.info("stop service name=%s", name)
    return _run_cmd([str(x) for x in stop_cmd])


def _start_one_service(name: str, config: dict[str, Any]) -> str:
    svc = _service_by_name(config).get(name)
    if not svc:
        return f"skip {name}: not in config"
    start_cmd = svc.get("start_cmd")
    if not isinstance(start_cmd, list) or not start_cmd:
        return f"skip {name}: no start_cmd"
    stype = str(svc.get("type", "process"))
    background = stype == "process"
    logger.info("start service name=%s background=%s", name, background)
    return _run_cmd([str(x) for x in start_cmd], background=background)


def _command_timeout() -> int:
    try:
        return int(ConfigManager.get().get("app", {}).get("command_timeout", 60))
    except (TypeError, ValueError):
        return 60


def _resolve_env(cmd_list: list[str]) -> list[str]:
    return [resolve_home_path(str(x)) if isinstance(x, str) else str(x) for x in cmd_list]


@retry_sync(max_retries=2, delay=2.0)
def _run_cmd_foreground_once(cmd_list: list[str], timeout_sec: int) -> subprocess.CompletedProcess[bytes]:
    flags = get_process_flags_no_detach()
    return subprocess.run(cmd_list, capture_output=True, timeout=timeout_sec, **flags)


def _run_cmd(cmd_list: list[str], background: bool = False) -> str:
    """Run a command list. If background=True, launch detached and return immediately (no retry)."""
    cmd_list = _resolve_env(cmd_list)
    timeout_sec = _command_timeout()
    try:
        if background:
            subprocess.Popen(
                cmd_list,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **get_process_flags(),
            )
            return f"Started in background: {cmd_list[0]}"
        result = _run_cmd_foreground_once(cmd_list, timeout_sec)
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if result.returncode != 0:
            msg = stderr or stdout or f"exit code {result.returncode}"
            logger.error("_run_cmd failed (%s): %s", cmd_list, msg)
            return f"Error: {msg}"
        return f"OK: {stdout}" if stdout else "OK"
    except subprocess.TimeoutExpired as e:
        logger.error("_run_cmd timeout (%s): %s", cmd_list, e)
        return f"Error: command timed out ({timeout_sec}s)"
    except subprocess.CalledProcessError as e:
        logger.error("_run_cmd CalledProcessError (%s): %s", cmd_list, e)
        return f"Error: {e}"
    except FileNotFoundError as e:
        logger.error("_run_cmd command not found (%s): %s", cmd_list, e)
        return f"Error: command not found - {e}"
    except OSError as e:
        logger.error("_run_cmd OSError (%s): %s", cmd_list, e)
        return f"Exception: {str(e)}"


def restart_service(service_name: str) -> str:
    """Stop and then start a configured service by name.

    Args:
        service_name: ``services[].name`` value from config.

    Returns:
        Human-readable combined stop/start output, or an error string.
    """
    logger.info("restart_service name=%s", service_name)
    try:
        config = ConfigManager.get()
        services = config.get("services", [])
        if not isinstance(services, list):
            return "Error: invalid services in config"

        target: dict[str, Any] | None = None
        for s in services:
            if isinstance(s, dict) and s.get("name") == service_name:
                target = s
                break

        if not target:
            return f"Error: Service '{service_name}' not found in config"

        results: list[str] = []

        stop_cmd = target.get("stop_cmd")
        if stop_cmd and isinstance(stop_cmd, list):
            stop_result = _run_cmd([str(x) for x in stop_cmd])
            results.append(f"Stop: {stop_result}")
            time.sleep(1)

        start_cmd = target.get("start_cmd")
        if start_cmd and isinstance(start_cmd, list):
            start_result = _run_cmd([str(x) for x in start_cmd], background=True)
            results.append(f"Start: {start_result}")

        return " | ".join(results) if results else "Error: no stop/start commands defined"

    except (KeyError, TypeError) as e:
        logger.error("restart_service config error for '%s': %s", service_name, e)
        return f"Exception: {str(e)}"
    except Exception as e:
        logger.error("Error restarting '%s': %s", service_name, e)
        return f"Exception: {str(e)}"


def toggle_sleep_mode(sleep: bool) -> str:
    """Stop all stack services and Docker compose, or bring them back up.

    Args:
        sleep: ``True`` to shut down, ``False`` to wake.

    Returns:
        Concatenated command results, or an exception string.
    """
    logger.info("toggle_sleep_mode sleep=%s", sleep)
    try:
        compose_file = default_compose_file()
        results: list[str] = []
        config = ConfigManager.get()

        if sleep:
            for name in get_shutdown_order(config):
                r = _stop_one_service(name, config)
                results.append(f"{name} stop: {r}")
                time.sleep(0.3)
            r = _run_cmd(["docker", "compose", "-f", compose_file, "down"])
            results.append(f"Docker down: {r}")
        else:
            docker_status = ensure_docker_running()
            if "Started" in docker_status:
                results.append(docker_status)
                time.sleep(5)

            r = _run_cmd(["docker", "compose", "-f", compose_file, "up", "-d"])
            results.append(f"Docker up: {r}")

            for name in get_startup_order(config):
                r = _start_one_service(name, config)
                results.append(f"{name} start: {r}")
                time.sleep(0.3)

        return " | ".join(results)

    except Exception as e:
        logger.error("Sleep mode error: %s", e)
        return f"Exception: {str(e)}"


def ensure_docker_running() -> str:
    """Verify ``docker info`` succeeds; on failure try to launch Docker Desktop if installed.

    Returns:
        Status message such as ``Docker OK`` or ``Error: ...``.
    """
    try:
        res = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
            **get_process_flags_no_detach(),
        )
        if res.returncode != 0:
            docker_desktop = get_docker_desktop_path()
            if docker_desktop:
                subprocess.Popen([docker_desktop], **get_process_flags())
                return "Started Docker Desktop (waiting 5s...)"
            return "Error: Docker Desktop not found"
        return "Docker OK"
    except subprocess.TimeoutExpired as e:
        logger.warning("ensure_docker_running timeout: %s", e)
        return "Error: docker info timed out"
    except FileNotFoundError as e:
        logger.warning("ensure_docker_running: %s", e)
        return "Error: docker not found"
    except OSError as e:
        logger.warning("ensure_docker_running: %s", e)
        return f"Error: {e}"


def inspect_containers() -> list[dict[str, Any]]:
    """List all containers via ``docker ps -a --format '{{json .}}'``.

    Returns:
        List of dicts with keys ``name``, ``status``, ``ports``, ``image``, or empty on error.
    """
    cmd_list = _resolve_env(["docker", "ps", "-a", "--format", "{{json .}}"])
    timeout_sec = _command_timeout()
    try:
        result = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            **get_process_flags_no_detach(),
        )
        if result.returncode != 0:
            logger.error("inspect_containers: %s", result.stderr)
            return []
        out: list[dict[str, Any]] = []
        for line in (result.stdout or "").strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw_name = obj.get("Names") or obj.get("name") or ""
            if isinstance(raw_name, list):
                nm = "/".join(str(x) for x in raw_name).lstrip("/")
            else:
                nm = str(raw_name).lstrip("/")
            out.append(
                {
                    "name": nm,
                    "status": str(obj.get("Status", "")),
                    "ports": str(obj.get("Ports", "")),
                    "image": str(obj.get("Image", "")),
                }
            )
        return out
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.error("inspect_containers: %s", e)
        return []


def inspect_containers_text() -> str:
    """Format :func:`inspect_containers` as TSV lines for RichLog output.

    Returns:
        Multiline string, or a placeholder when Docker returns nothing.
    """
    rows = inspect_containers()
    if not rows:
        return "(no containers or docker error)"
    lines = [f"{r['name']}\t{r['status']}\t{r['ports']}\t{r['image']}" for r in rows]
    return "\n".join(lines)


def get_container_logs(container_name: str, tail: int = 50) -> str:
    """Fetch recent container logs with timestamps.

    Args:
        container_name: Docker container name or id.
        tail: ``--tail`` line count.

    Returns:
        Log text or an error string from the underlying command runner.
    """
    name = container_name.strip()
    if not name:
        return "Error: empty container name"
    return _run_cmd(
        ["docker", "logs", "--tail", str(int(tail)), "--timestamps", name],
        background=False,
    )


def get_container_stats(container_name: str) -> dict[str, Any]:
    """Parse one-shot ``docker stats --no-stream --format '{{json .}}'``.

    Args:
        container_name: Docker container name or id.

    Returns:
        Dict with ``cpu_percent``, ``memory_usage``, ``memory_limit``, ``net_io`` strings, or empty dict.
    """
    name = container_name.strip()
    if not name:
        return {}
    timeout_sec = _command_timeout()
    cmd = _resolve_env(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", name],
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            **get_process_flags_no_detach(),
        )
        if result.returncode != 0 or not (result.stdout or "").strip():
            return {}
        line = (result.stdout or "").strip().splitlines()[-1]
        obj = json.loads(line)
        mem = str(obj.get("MemUsage", ""))
        mem_limit = ""
        if " / " in mem:
            parts = mem.split(" / ", 1)
            mem = parts[0].strip()
            mem_limit = parts[1].strip() if len(parts) > 1 else ""
        return {
            "cpu_percent": str(obj.get("CPUPerc", "")).strip(),
            "memory_usage": mem,
            "memory_limit": mem_limit,
            "net_io": str(obj.get("NetIO", "")).strip(),
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, ValueError) as e:
        logger.debug("get_container_stats(%s): %s", name, e)
        return {}


def docker_logs_tail(container_name: str, lines: int = 50) -> str:
    """Backward-compatible alias for :func:`get_container_logs`.

    Args:
        container_name: Docker container name or id.
        lines: Same as ``tail`` for :func:`get_container_logs`.

    Returns:
        Log text or error string.
    """
    return get_container_logs(container_name, tail=lines)


def fire_test_webhook() -> str:
    """Spawn the configured test webhook command (non-blocking).

    Returns:
        PID message on success, or an error string.
    """
    try:
        config = ConfigManager.get()
        cmd_str = config.get("tests", {}).get("replyline_webhook", "")
        if not cmd_str:
            return "Error: no replyline_webhook in config"

        parts = cmd_str.split(maxsplit=1)
        cmd_list = _resolve_env(parts)
        result = subprocess.Popen(cmd_list, **get_process_flags_no_detach())
        return f"Webhook fired (PID: {result.pid})"
    except (OSError, ValueError) as e:
        logger.error("fire_test_webhook: %s", e)
        return f"Exception: {str(e)}"
    except Exception as e:
        logger.error("fire_test_webhook: %s", e)
        return f"Exception: {str(e)}"


def switch_profile(mode: str) -> str:
    """Run ``scripts.profile_{mode}_cmd`` from config (e.g. cloud/local switch script).

    Args:
        mode: Profile key suffix, e.g. ``"cloud"`` or ``"local"``.

    Returns:
        ``OK: ...`` or an error string.
    """
    try:
        config = ConfigManager.get()
        scripts = config.get("scripts", {})
        if not isinstance(scripts, dict):
            return "Error: invalid scripts section in config"

        cmd_list = scripts.get(f"profile_{mode}_cmd")

        if not cmd_list or not isinstance(cmd_list, list):
            return f"Error: no profile_{mode}_cmd in config scripts section"

        cmd_list = _resolve_env([str(x) for x in cmd_list])
        result = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            timeout=30,
            **get_process_flags_no_detach(),
        )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            logger.error("switch_profile failed (%s): %s", mode, stderr or result.stdout)
            return f"Error: {stderr}"
        return f"OK: Switched to {mode}"
    except subprocess.TimeoutExpired as e:
        logger.error("switch_profile timeout: %s", e)
        return "Error: profile switch timed out (30s)"
    except subprocess.CalledProcessError as e:
        logger.error("switch_profile CalledProcessError: %s", e)
        return f"Error: {e}"
    except FileNotFoundError as e:
        logger.error("switch_profile: %s", e)
        return f"Error: {e}"
    except OSError as e:
        logger.error("switch_profile: %s", e)
        return f"Exception: {str(e)}"


if __name__ == "__main__":
    print("=== Docker status ===")
    for row in inspect_containers():
        print(row)
    print("\n=== Restart Ollama ===")
    print(restart_service("Ollama"))
