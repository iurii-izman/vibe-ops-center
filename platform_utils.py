"""Cross-platform helpers for subprocess paths, home expansion, and process flags."""

from __future__ import annotations

import os
import platform
import subprocess
from typing import Any

CURRENT_OS = platform.system()


def get_kill_command(process_name: str) -> list[str]:
    """Build a command to force-kill a process by name."""
    if CURRENT_OS == "Windows":
        return ["taskkill", "/IM", process_name, "/F"]
    return ["pkill", "-f", process_name]


def get_port_kill_command(port: int) -> list[str]:
    """Build a command to kill whatever is listening on ``port``."""
    if CURRENT_OS == "Windows":
        ps = (
            f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | "
            "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
        )
        return ["powershell", "-Command", ps]
    return ["bash", "-c", f"lsof -ti:{port} | xargs kill -9 2>/dev/null || true"]


def get_process_flags() -> dict[str, Any]:
    """Flags for ``subprocess.run`` / ``Popen`` to hide console or detach (OS-specific)."""
    if CURRENT_OS == "Windows":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS}  # type: ignore[attr-defined]
    return {"start_new_session": True}


def get_process_flags_no_detach() -> dict[str, Any]:
    """Like ``get_process_flags`` but without ``DETACHED_PROCESS`` (e.g. ``subprocess.run``)."""
    if CURRENT_OS == "Windows":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {"start_new_session": True}


def resolve_home_path(path: str) -> str:
    """Replace ``%USERPROFILE%`` and expand ``~`` to the real home directory."""
    home = os.path.expanduser("~")
    out = path.replace("%USERPROFILE%", home)
    return os.path.expanduser(out)


def get_docker_desktop_path() -> str | None:
    """Return path to Docker Desktop binary if present, else ``None``."""
    if CURRENT_OS == "Windows":
        path = os.path.join(os.environ.get("ProgramFiles", ""), "Docker", "Docker", "Docker Desktop.exe")
        return path if os.path.isfile(path) else None
    if CURRENT_OS == "Darwin":
        path = "/Applications/Docker.app/Contents/MacOS/Docker"
        return path if os.path.isfile(path) else None
    return None


def default_compose_file() -> str:
    """Default ``docker-compose.yml`` under ``~/ai-stack`` (expanded)."""
    return os.path.join(os.path.expanduser("~"), "ai-stack", "docker-compose.yml")
