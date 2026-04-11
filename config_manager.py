"""YAML configuration singleton with mtime-based reload and environment secrets."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, ClassVar

import yaml

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent
_DEFAULT_CONFIG_PATH = _ROOT / "config.yaml"
_DOTENV_PATH = _ROOT / ".env"

_instance: ConfigManager | None = None


class ConfigManager:
    """Singleton YAML loader with optional auto-reload when the file mtime changes.

    Loads ``config.yaml`` (or a custom path), merges ``.env`` into the environment,
    validates required sections, and caches the parsed dict until the file changes.
    """

    _warned_missing_secrets: ClassVar[set[str]] = set()

    def __init__(self, config_path: Path | str | None = None) -> None:
        self._config_path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        self._cached: dict[str, Any] = {}
        self._mtime: float | None = None
        self._load_dotenv()
        self._read_from_disk(force=True)

    @classmethod
    def _singleton(cls) -> ConfigManager:
        global _instance
        if _instance is None:
            obj = object.__new__(cls)
            _instance = obj
            ConfigManager.__init__(obj, None)
        return _instance

    @classmethod
    def reset_singleton(cls) -> None:
        """Clear the cached singleton (for tests or process restart)."""
        global _instance
        _instance = None
        cls._warned_missing_secrets.clear()

    @classmethod
    def get(cls, config_path: Path | str | None = None) -> dict[str, Any]:
        """Return the cached configuration dict, reloading from disk if the file changed.

        Args:
            config_path: Optional path to a YAML file; if set, switches the singleton path and reloads.

        Returns:
            Shallow copy of the cached root mapping.
        """
        mgr = cls._singleton()
        if config_path is not None and Path(config_path) != mgr._config_path:
            mgr._config_path = Path(config_path)
            mgr._read_from_disk(force=True)
        else:
            mgr._maybe_reload()
        return dict(mgr._cached)

    @classmethod
    def reload(cls, config_path: Path | str | None = None) -> dict[str, Any]:
        """Force a full reload from disk (optionally changing the config file path).

        Args:
            config_path: Optional new YAML path for the singleton.

        Returns:
            Shallow copy of the freshly loaded root mapping.
        """
        mgr = cls._singleton()
        if config_path is not None:
            mgr._config_path = Path(config_path)
        mgr._read_from_disk(force=True)
        return dict(mgr._cached)

    @classmethod
    def get_secret(cls, key: str) -> str:
        """Return the environment variable value for ``key`` (after ``.env`` load).

        Args:
            key: Environment variable name.

        Returns:
            Value string, or empty string if unset (logs a one-time warning per key).
        """
        val = os.environ.get(key, "") or ""
        if not val and key not in cls._warned_missing_secrets:
            logger.warning("Секрет / переменная окружения не задана: %s", key)
            cls._warned_missing_secrets.add(key)
        return val

    def _load_dotenv(self) -> None:
        if not _DOTENV_PATH.is_file():
            return
        try:
            from dotenv import load_dotenv

            load_dotenv(_DOTENV_PATH, override=False)
        except ImportError:
            self._load_dotenv_manual()

    def _load_dotenv_manual(self) -> None:
        try:
            for line in _DOTENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except OSError as e:
            logger.warning("Не удалось прочитать .env: %s", e)

    def _maybe_reload(self) -> None:
        try:
            mtime = os.path.getmtime(self._config_path)
        except OSError:
            return
        if self._mtime is None or mtime != self._mtime:
            self._read_from_disk(force=True)

    def _read_from_disk(self, force: bool = False) -> None:
        try:
            mtime = os.path.getmtime(self._config_path)
        except OSError as e:
            logger.warning("Нет доступа к конфигу %s: %s — оставляем кеш", self._config_path, e)
            return

        if not force and self._mtime is not None and mtime == self._mtime:
            return

        try:
            raw = self._config_path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw) or {}
            if not isinstance(data, dict):
                raise TypeError("Корень config.yaml должен быть mapping")
        except yaml.YAMLError as e:
            logger.warning("Ошибка YAML в %s: %s — используется предыдущий валидный конфиг", self._config_path, e)
            return
        except (OSError, TypeError) as e:
            logger.warning(
                "Ошибка чтения конфига %s: %s — используется предыдущий валидный конфиг", self._config_path, e
            )
            return

        self._cached = data
        self._mtime = mtime
        self._validate_config()

    def _validate_config(self) -> None:
        cfg = self._cached
        errors: list[str] = []

        services = cfg.get("services")
        if not isinstance(services, list):
            errors.append("services должен быть списком")
        else:
            for i, svc in enumerate(services):
                if not isinstance(svc, dict):
                    errors.append(f"services[{i}] должен быть объектом с полями name, url")
                    continue
                if not svc.get("name"):
                    errors.append(f"services[{i}]: отсутствует name")
                if not svc.get("url"):
                    errors.append(f"services[{i}]: отсутствует url")
                for field in ("stop_cmd", "start_cmd"):
                    cmd = svc.get(field)
                    if cmd is None:
                        errors.append(f"services[{i}] ({svc.get('name', '?')}): отсутствует {field}")
                    elif not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
                        errors.append(f"services[{i}] ({svc.get('name', '?')}): {field} должен быть списком строк")

        lf = cfg.get("langfuse_api", {})
        if isinstance(lf, dict):
            limit = lf.get("daily_budget_limit")
            if limit is None:
                errors.append("langfuse_api.daily_budget_limit отсутствует")
            else:
                try:
                    if float(limit) <= 0:
                        errors.append("langfuse_api.daily_budget_limit должен быть числом > 0")
                except (TypeError, ValueError):
                    errors.append("langfuse_api.daily_budget_limit должен быть числом > 0")

        for msg in errors:
            logger.warning("[validate] %s", msg)

    @classmethod
    def validate(cls) -> None:
        """Run schema validation on the currently cached configuration (logs issues)."""
        cls._singleton()._validate_config()
