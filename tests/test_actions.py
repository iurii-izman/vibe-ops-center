"""Tests for ``actions`` orchestration helpers."""

from __future__ import annotations

import pytest

import actions
from config_manager import ConfigManager
from platform_utils import resolve_home_path


def test_resolve_env() -> None:
    home = __import__("os").path.expanduser("~")
    out = actions._resolve_env(["noop", "%USERPROFILE%/ai-stack/foo", "~/other"])
    assert "%USERPROFILE%" not in out[1]
    assert home in out[1] or out[1].startswith(home)
    assert "~" not in out[2]


def test_resolve_home_path_direct() -> None:
    import os

    got = os.path.normpath(resolve_home_path("%USERPROFILE%/x"))
    want = os.path.normpath(os.path.join(os.path.expanduser("~"), "x"))
    assert got == want


def test_restart_service_found(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], bool]] = []

    def fake_run(cmd: list[str], background: bool = False) -> str:
        calls.append((list(cmd), background))
        if background:
            return "Started in background: echo"
        return "OK"

    cfg = {
        "services": [
            {
                "name": "Alpha",
                "stop_cmd": ["echo", "stop"],
                "start_cmd": ["echo", "start"],
                "type": "process",
            }
        ]
    }
    monkeypatch.setattr(ConfigManager, "get", staticmethod(lambda config_path=None: cfg))
    monkeypatch.setattr(actions, "_run_cmd", fake_run)
    monkeypatch.setattr(actions.time, "sleep", lambda _s: None)
    out = actions.restart_service("Alpha")
    assert "Stop:" in out and "Start:" in out
    assert calls[0][1] is False
    assert calls[1][1] is True


def test_restart_service_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ConfigManager, "get", staticmethod(lambda config_path=None: {"services": []}))
    msg = actions.restart_service("Missing")
    assert "not found" in msg.lower()


def test_startup_order() -> None:
    cfg = {
        "services": [
            {"name": "A", "depends_on": ["B"]},
            {"name": "B", "depends_on": []},
        ]
    }
    assert actions.get_startup_order(cfg) == ["B", "A"]


def test_shutdown_order() -> None:
    cfg = {
        "services": [
            {"name": "A", "depends_on": ["B"]},
            {"name": "B", "depends_on": []},
        ]
    }
    assert actions.get_shutdown_order(cfg) == ["A", "B"]
