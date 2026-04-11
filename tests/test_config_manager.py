"""Tests for ``config_manager``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from config_manager import ConfigManager
from tests.conftest import MINIMAL_VALID_YAML


def test_load_valid_config(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(MINIMAL_VALID_YAML, encoding="utf-8")
    ConfigManager.reset_singleton()
    cfg = ConfigManager.reload(p)
    assert isinstance(cfg, dict)
    assert cfg["services"][0]["name"] == "TestSvc"


def test_config_caching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(MINIMAL_VALID_YAML, encoding="utf-8")
    ConfigManager.reset_singleton()
    ConfigManager.reload(p)
    calls: list[bool] = []
    orig_fn = ConfigManager._read_from_disk

    def spy(self: object, force: bool = False) -> None:
        calls.append(force)
        return orig_fn(self, force)

    monkeypatch.setattr(ConfigManager, "_read_from_disk", spy)
    ConfigManager.get()
    ConfigManager.get()
    assert calls == []


def test_config_reload_on_change(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(MINIMAL_VALID_YAML, encoding="utf-8")
    ConfigManager.reset_singleton()
    c1 = ConfigManager.reload(p)
    p.write_text(
        MINIMAL_VALID_YAML.replace("TestSvc", "RenamedSvc"),
        encoding="utf-8",
    )
    os.utime(p, None)
    c2 = ConfigManager.get()
    assert c1["services"][0]["name"] == "TestSvc"
    assert c2["services"][0]["name"] == "RenamedSvc"


def test_invalid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(MINIMAL_VALID_YAML, encoding="utf-8")
    ConfigManager.reset_singleton()
    ConfigManager.reload(p)
    good = ConfigManager.get()
    p.write_text("{ not yaml: [[", encoding="utf-8")
    os.utime(p, None)
    ConfigManager.get()
    still = ConfigManager.get()
    assert still == good


def test_validation(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    bad = yaml.safe_load(MINIMAL_VALID_YAML)
    bad["services"][0].pop("url", None)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(bad), encoding="utf-8")
    ConfigManager.reset_singleton()
    ConfigManager.reload(p)
    ConfigManager.validate()
    assert any("url" in r.message.lower() or "services" in r.message.lower() for r in caplog.records)


def test_get_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    ConfigManager.reset_singleton()
    monkeypatch.setenv("VIBE_OPS_TEST_SECRET", "secret-value")
    assert ConfigManager.get_secret("VIBE_OPS_TEST_SECRET") == "secret-value"
