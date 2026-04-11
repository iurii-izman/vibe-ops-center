"""SQLite persistence for service checks, cost samples, and operational events."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = _ROOT / "vibe_ops.db"


class Storage:
    """SQLite-backed store for health checks, Langfuse cost history, and audit events.

    All methods are synchronous; call from async code via ``asyncio.to_thread``.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS service_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_time_ms REAL,
                    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_service_checks_name_time
                    ON service_checks (service_name, checked_at);

                CREATE TABLE IF NOT EXISTS cost_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cost REAL NOT NULL,
                    traces INTEGER NOT NULL,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    service_name TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.commit()

    def record_check(self, service_name: str, status: str, response_time_ms: float | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO service_checks (service_name, status, response_time_ms) VALUES (?, ?, ?)",
                (service_name, status, response_time_ms),
            )
            conn.commit()

    def record_cost(self, cost: float, traces: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO cost_history (cost, traces) VALUES (?, ?)",
                (cost, traces),
            )
            conn.commit()

    def record_event(
        self,
        event_type: str,
        service_name: str | None = None,
        details: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (event_type, service_name, details) VALUES (?, ?, ?)",
                (event_type, service_name, details),
            )
            conn.commit()

    def get_uptime_percent(self, service_name: str, hours: int = 24) -> float | None:
        """None если за период нет ни одной проверки (первый запуск / пустая история)."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'UP' THEN 1 ELSE 0 END) AS up_cnt
                FROM service_checks
                WHERE service_name = ?
                  AND checked_at >= datetime('now', ?)
                """,
                (service_name, f"-{int(hours)} hours"),
            )
            row = cur.fetchone()
            if not row or row["total"] is None or int(row["total"]) == 0:
                return None
            total = int(row["total"])
            up_cnt = int(row["up_cnt"] or 0)
            return round(100.0 * up_cnt / total, 1)

    def get_avg_response_time(self, service_name: str, hours: int = 1) -> float | None:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT AVG(response_time_ms) AS avg_ms
                FROM service_checks
                WHERE service_name = ?
                  AND response_time_ms IS NOT NULL
                  AND checked_at >= datetime('now', ?)
                """,
                (service_name, f"-{int(hours)} hours"),
            )
            row = cur.fetchone()
            if not row or row["avg_ms"] is None:
                return None
            return round(float(row["avg_ms"]), 1)

    def get_cost_history(self, hours: int = 24) -> list[dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT cost, traces, recorded_at
                FROM cost_history
                WHERE recorded_at >= datetime('now', ?)
                ORDER BY recorded_at ASC
                """,
                (f"-{int(hours)} hours",),
            )
            return [
                {"cost": float(r["cost"]), "traces": int(r["traces"]), "recorded_at": str(r["recorded_at"])}
                for r in cur.fetchall()
            ]

    def get_heal_stats_today(self) -> dict[str, int]:
        """Счётчики heal / heal_success / heal_failed за календарный день (UTC date в SQLite)."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT event_type, COUNT(*) AS cnt
                FROM events
                WHERE date(created_at) = date('now')
                  AND event_type IN ('heal', 'heal_success', 'heal_failed')
                GROUP BY event_type
                """
            )
            counts = {str(r["event_type"]): int(r["cnt"]) for r in cur.fetchall()}
        total = int(counts.get("heal", 0))
        ok = int(counts.get("heal_success", 0))
        fail = int(counts.get("heal_failed", 0))
        return {"total_heals": total, "successful": ok, "failed": fail}

    def get_recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT event_type, service_name, details, created_at
                FROM events
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            )
            return [
                {
                    "event_type": r["event_type"],
                    "service_name": r["service_name"],
                    "details": r["details"],
                    "created_at": str(r["created_at"]),
                }
                for r in cur.fetchall()
            ]

    def get_status_history(self, service_name: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT service_name, status, response_time_ms, checked_at
                FROM service_checks
                WHERE service_name = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (service_name, int(limit)),
            )
            return [
                {
                    "service_name": r["service_name"],
                    "status": r["status"],
                    "response_time_ms": r["response_time_ms"],
                    "checked_at": str(r["checked_at"]),
                }
                for r in cur.fetchall()
            ]

    def cleanup_old_data(self, days: int = 30) -> int:
        cutoff = f"-{int(days)} days"
        total = 0
        with self._connect() as conn:
            for table, col in (
                ("service_checks", "checked_at"),
                ("cost_history", "recorded_at"),
                ("events", "created_at"),
            ):
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE {col} < datetime('now', ?)",
                    (cutoff,),
                )
                total += cur.rowcount
            conn.commit()
        logger.info("cleanup_old_data: удалено строк: %s (старше %s дней)", total, days)
        return total

    def close(self) -> None:
        """Release resources on application shutdown.

        Connections are opened per operation; this hook exists for symmetry and future pooling.
        """
        logger.debug("storage.close()")
