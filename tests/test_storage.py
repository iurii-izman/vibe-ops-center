"""Tests for ``storage`` SQLite layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from storage import Storage


def test_record_and_get_check(tmp_db: Storage) -> None:
    tmp_db.record_check("svc", "UP", 12.3)
    hist = tmp_db.get_status_history("svc", 10)
    assert len(hist) >= 1
    assert hist[0]["status"] == "UP"


def test_uptime_percent(tmp_db: Storage) -> None:
    for _ in range(9):
        tmp_db.record_check("x", "UP", 1.0)
    tmp_db.record_check("x", "DOWN", None)
    pct = tmp_db.get_uptime_percent("x", 24)
    assert pct == 90.0


def test_cost_history(tmp_db: Storage) -> None:
    tmp_db.record_cost(0.1, 1)
    tmp_db.record_cost(0.2, 2)
    h = tmp_db.get_cost_history(24)
    assert len(h) >= 2
    assert h[-1]["cost"] >= 0.1


def test_record_event(tmp_db: Storage) -> None:
    tmp_db.record_event("sleep", None, "z")
    ev = tmp_db.get_recent_events(5)
    assert any(e["event_type"] == "sleep" for e in ev)


def test_cleanup_old_data(tmp_path: Path) -> None:
    db_path = tmp_path / "c.db"
    st = Storage(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO service_checks (service_name, status, response_time_ms, checked_at) "
        "VALUES ('old', 'UP', 1.0, datetime('now', '-40 days'))"
    )
    conn.commit()
    conn.close()
    deleted = st.cleanup_old_data(days=30)
    assert deleted >= 1


def test_empty_db(tmp_db: Storage) -> None:
    assert tmp_db.get_uptime_percent("none", 24) is None
    assert tmp_db.get_avg_response_time("none", 1) is None
    assert tmp_db.get_cost_history(24) == []
    assert tmp_db.get_recent_events(10) == []
    assert tmp_db.get_status_history("none", 5) == []
    assert tmp_db.get_heal_stats_today() == {"total_heals": 0, "successful": 0, "failed": 0}
