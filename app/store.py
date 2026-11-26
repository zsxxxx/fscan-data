"""SQLite 数据存储：价格历史、优惠告警。

轻量、零配置。数据库文件位于 data/store.db。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from .config import DATA_DIR

DB_PATH = DATA_DIR / "store.db"

_LOCK = threading.RLock()


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表（幂等）。"""
    with _LOCK, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                sku TEXT NOT NULL,
                title TEXT,
                price REAL,
                promo_price REAL,
                stock TEXT,
                promo_label TEXT,
                url TEXT,
                captured_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_price_sku ON price_history(platform, sku, captured_at);

            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                task_id TEXT,
                platform TEXT NOT NULL,
                sku TEXT,
                title TEXT,
                url TEXT,
                image TEXT,
                alert_type TEXT,
                message TEXT,
                old_price REAL,
                new_price REAL,
                discount REAL,
                promo_label TEXT,
                created_at TEXT NOT NULL,
                is_read INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts(created_at DESC);

            CREATE TABLE IF NOT EXISTS monitor_tasks (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                keyword TEXT NOT NULL,
                filters TEXT,
                specs TEXT,
                interval INTEGER,
                enabled INTEGER,
                status TEXT,
                last_error TEXT,
                last_run_at TEXT,
                run_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )


# ------------------------------------------------------------------ 价格历史

def record_price(
    platform: str,
    sku: str,
    title: str,
    price: Optional[float],
    promo_price: Optional[float],
    stock: str,
    promo_label: str,
    url: str,
) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO price_history
               (platform, sku, title, price, promo_price, stock, promo_label, url, captured_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                platform, sku, title, price, promo_price, stock, promo_label, url,
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )


def get_price_range(platform: str, sku: str) -> dict:
    """返回历史最低/最高/均价与数据点数。"""
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT MIN(COALESCE(promo_price, price)) AS low,
                      MAX(price) AS high,
                      AVG(COALESCE(promo_price, price)) AS avg,
                      COUNT(*) AS n
               FROM price_history WHERE platform=? AND sku=?""",
            (platform, sku),
        ).fetchone()
    if not row or not row["n"]:
        return {"low": None, "high": None, "avg": None, "count": 0}
    return {
        "low": row["low"],
        "high": row["high"],
        "avg": round(row["avg"], 2) if row["avg"] is not None else None,
        "count": row["n"],
    }


def get_price_series(platform: str, sku: str, limit: int = 50) -> list[dict]:
    """返回价格历史序列（时间正序）。"""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT price, promo_price, promo_label, captured_at
               FROM price_history WHERE platform=? AND sku=?
               ORDER BY id DESC LIMIT ?""",
            (platform, sku, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_last_price(platform: str, sku: str) -> Optional[dict]:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT price, promo_price, captured_at FROM price_history
               WHERE platform=? AND sku=? ORDER BY id DESC LIMIT 1""",
            (platform, sku),
        ).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------ 告警

def add_alert(alert: dict) -> str:
    alert_id = alert.get("id") or uuid.uuid4().hex[:12]
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO alerts
               (id, task_id, platform, sku, title, url, image, alert_type, message,
                old_price, new_price, discount, promo_label, created_at, is_read)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (
                alert_id, alert.get("task_id", ""), alert.get("platform", ""),
                alert.get("sku", ""), alert.get("title", ""), alert.get("url", ""),
                alert.get("image", ""), alert.get("alert_type", ""), alert.get("message", ""),
                alert.get("old_price"), alert.get("new_price"), alert.get("discount"),
                alert.get("promo_label", ""),
                alert.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    return alert_id


def list_alerts(limit: int = 100, unread_only: bool = False) -> list[dict]:
    with _LOCK, _connect() as conn:
        sql = "SELECT * FROM alerts"
        if unread_only:
            sql += " WHERE is_read=0"
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_alert_read(alert_id: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("UPDATE alerts SET is_read=1 WHERE id=?", (alert_id,))


def mark_all_read() -> None:
    with _LOCK, _connect() as conn:
        conn.execute("UPDATE alerts SET is_read=1")


def clear_alerts() -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM alerts")


# ------------------------------------------------------------------ 监控任务

def save_task(task: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO monitor_tasks
               (id, platform, keyword, filters, specs, interval, enabled, status,
                last_error, last_run_at, run_count, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task["id"], task["platform"], task["keyword"],
                json.dumps(task.get("filters", {}), ensure_ascii=False),
                task.get("specs", ""), task.get("interval", 300),
                1 if task.get("enabled", True) else 0,
                task.get("status", "idle"), task.get("last_error", ""),
                task.get("last_run_at", ""), task.get("run_count", 0),
                task.get("created_at", ""),
            ),
        )


def list_tasks() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute("SELECT * FROM monitor_tasks ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d["enabled"])
        try:
            d["filters"] = json.loads(d.get("filters") or "{}")
        except Exception:  # noqa: BLE001
            d["filters"] = {}
        out.append(d)
    return out


def get_task(task_id: str) -> Optional[dict]:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM monitor_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    try:
        d["filters"] = json.loads(d.get("filters") or "{}")
    except Exception:  # noqa: BLE001
        d["filters"] = {}
    return d


def delete_task(task_id: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM monitor_tasks WHERE id=?", (task_id,))


init_db()
