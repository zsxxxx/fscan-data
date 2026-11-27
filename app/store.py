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

            -- 采集批次：每次搜索/监控轮询一条，承载来源、代理、结果状态等元数据
            CREATE TABLE IF NOT EXISTS crawl_runs (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                keyword TEXT NOT NULL,
                filters TEXT,
                pages INTEGER DEFAULT 1,
                status TEXT NOT NULL,
                product_count INTEGER DEFAULT 0,
                elapsed REAL DEFAULT 0,
                proxy TEXT,
                error TEXT,
                stale INTEGER DEFAULT 0,
                started_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_time ON crawl_runs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_runs_platform ON crawl_runs(platform, started_at DESC);

            -- 规范化商品观测：一次采集的一个商品一条，保留结构化字段与原始快照
            CREATE TABLE IF NOT EXISTS product_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                platform TEXT NOT NULL,
                sku TEXT,
                title TEXT,
                url TEXT,
                image TEXT,
                shop TEXT,
                price REAL,
                promo_price REAL,
                list_price REAL,
                sales TEXT,
                stock TEXT,
                promo_type TEXT,
                promo_label TEXT,
                coupon TEXT,
                keyword TEXT,
                rank INTEGER,
                source TEXT,
                captured_at TEXT NOT NULL,
                raw TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_obs_run ON product_observations(run_id);
            CREATE INDEX IF NOT EXISTS idx_obs_sku ON product_observations(platform, sku, captured_at DESC);
            CREATE INDEX IF NOT EXISTS idx_obs_keyword ON product_observations(platform, keyword, captured_at DESC);
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
    now = time.time()
    with _LOCK, _connect() as conn:
        # 去重：同一 SKU 在 60 秒内价格与促销完全不变时不重复落库，
        # 避免翻页/重复搜索把历史曲线塞满相同点。
        last = conn.execute(
            """SELECT price, promo_price, captured_at FROM price_history
               WHERE platform=? AND sku=? ORDER BY id DESC LIMIT 1""",
            (platform, sku),
        ).fetchone()
        if last:
            same_price = (
                (last["price"] is None and price is None) or
                (last["price"] is not None and price is not None and abs(last["price"] - price) < 0.005)
            )
            same_promo = (
                (last["promo_price"] is None and promo_price is None) or
                (last["promo_price"] is not None and promo_price is not None
                 and abs(last["promo_price"] - promo_price) < 0.005)
            )
            try:
                recent = now - time.mktime(time.strptime(last["captured_at"], "%Y-%m-%d %H:%M:%S")) < 60
            except Exception:  # noqa: BLE001 - 时间格式异常时视为不重复
                recent = False
            if same_price and same_promo and recent:
                return

        conn.execute(
            """INSERT INTO price_history
               (platform, sku, title, price, promo_price, stock, promo_label, url, captured_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                platform, sku, title, price, promo_price, stock, promo_label, url,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
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


# ------------------------------------------------------------------ 采集批次与观测

def start_run(run: dict) -> None:
    """记录一次采集开始。"""
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO crawl_runs
               (id, platform, keyword, filters, pages, status, product_count, elapsed,
                proxy, error, stale, started_at, finished_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (
                run["id"], run["platform"], run.get("keyword", ""),
                json.dumps(run.get("filters", {}), ensure_ascii=False),
                int(run.get("pages", 1)), run.get("status", "running"),
                int(run.get("product_count", 0)), float(run.get("elapsed", 0)),
                run.get("proxy", ""), run.get("error", ""),
                1 if run.get("stale") else 0,
                run.get("started_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )


def finish_run(
    run_id: str,
    status: str,
    product_count: int = 0,
    elapsed: float = 0.0,
    error: str = "",
    proxy: str = "",
    stale: bool = False,
) -> None:
    """更新采集批次的最终结果。"""
    with _LOCK, _connect() as conn:
        conn.execute(
            """UPDATE crawl_runs SET status=?, product_count=?, elapsed=?, error=?,
               proxy=?, stale=?, finished_at=? WHERE id=?""",
            (
                status, int(product_count), round(float(elapsed), 2), str(error)[:500],
                proxy, 1 if stale else 0,
                time.strftime("%Y-%m-%d %H:%M:%S"), run_id,
            ),
        )


def list_runs(limit: int = 50, platform: str | None = None) -> list[dict]:
    with _LOCK, _connect() as conn:
        if platform:
            rows = conn.execute(
                """SELECT * FROM crawl_runs WHERE platform=?
                   ORDER BY started_at DESC, rowid DESC LIMIT ?""",
                (platform, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM crawl_runs ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        item["stale"] = bool(item.get("stale"))
        try:
            item["filters"] = json.loads(item.get("filters") or "{}")
        except Exception:  # noqa: BLE001
            item["filters"] = {}
        out.append(item)
    return out


def list_skus(platform: str, keyword: str | None = None, limit: int = 50) -> list[str]:
    """取历史观测中出现过的 SKU，用于详情页直采兜底。"""
    sql = "SELECT DISTINCT sku FROM product_observations WHERE platform=? AND sku<>''"
    params: list = [platform]
    if keyword:
        sql += " AND keyword=?"
        params.append(keyword)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _LOCK, _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [r["sku"] for r in rows if r["sku"]]


def record_observation(obs: dict) -> None:
    """写入一条规范化商品观测。"""
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO product_observations
               (run_id, platform, sku, title, url, image, shop, price, promo_price,
                list_price, sales, stock, promo_type, promo_label, coupon, keyword,
                rank, source, captured_at, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                obs.get("run_id", ""), obs.get("platform", ""), obs.get("sku", ""),
                obs.get("title", ""), obs.get("url", ""), obs.get("image", ""),
                obs.get("shop", ""), obs.get("price"), obs.get("promo_price"),
                obs.get("list_price"), obs.get("sales", ""), obs.get("stock", ""),
                obs.get("promo_type", ""), obs.get("promo_label", ""),
                obs.get("coupon", ""), obs.get("keyword", ""), obs.get("rank", 0),
                obs.get("source", "dom"),
                obs.get("captured_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
                json.dumps(obs.get("raw", {}), ensure_ascii=False, default=str),
            ),
        )


def list_observations(
    platform: str | None = None,
    keyword: str | None = None,
    sku: str | None = None,
    run_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    for column, value in (
        ("platform", platform), ("keyword", keyword), ("sku", sku), ("run_id", run_id)
    ):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    sql = "SELECT * FROM product_observations"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _LOCK, _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        try:
            item["raw"] = json.loads(item.get("raw") or "{}")
        except Exception:  # noqa: BLE001
            item["raw"] = {}
        out.append(item)
    return out


init_db()
