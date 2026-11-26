"""请求守卫：节流、限流冷却、结果缓存。

目的：降低触发平台反爬/限流的概率，并在限流后给出明确的恢复时间。
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .config import DATA_DIR

CACHE_DIR = DATA_DIR / "cache"

# 同一平台两次搜索之间最小间隔（秒）
MIN_INTERVAL = 8.0
# 命中限流后平台级基础冷却时间（秒），实际按连续触发次数渐进翻倍
COOLDOWN_SECONDS = 180.0
# 最大冷却时间（秒）
MAX_COOLDOWN_SECONDS = 1800.0
# 搜索结果缓存有效期（秒）
CACHE_TTL = 600.0


class RateLimitGuard:
    """平台级请求节流 + 限流冷却 + 结果缓存。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_request: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._fail_count: dict[str, int] = {}
        self._stats: dict[str, dict] = {}

    def _interval(self, platform_id: str) -> float:
        """基础间隔随连续失败次数放大，并加入 ±20% 抖动。"""
        fail = self._fail_count.get(platform_id, 0)
        base = MIN_INTERVAL * (1 + min(fail, 4) * 0.4)
        return base * random.uniform(0.85, 1.2)

    def _bump(self, platform_id: str, key: str, amount: float = 1) -> None:
        stats = self._stats.setdefault(
            platform_id, {"requests": 0, "limited": 0, "cache_hits": 0, "stale_hits": 0, "waited": 0.0}
        )
        if key == "waited":
            stats[key] += amount
        else:
            stats[key] += int(amount)

    # ---------------------------------------------------------- 节流
    def acquire(self, platform_id: str) -> float:
        """获取请求许可，返回实际等待秒数。"""
        with self._lock:
            now = time.time()
            while True:
                cooldown_end = self._cooldown_until.get(platform_id, 0)
                if now < cooldown_end:
                    raise CooldownActive(platform_id, cooldown_end - now)

                last = self._last_request.get(platform_id, 0)
                wait = self._interval(platform_id) - (now - last)
                if wait <= 0:
                    self._last_request[platform_id] = now
                    self._bump(platform_id, "requests")
                    return 0.0
                # 节流的等待在锁内完成，保证全局串行
                time.sleep(wait)
                self._bump(platform_id, "waited", wait)
                now = time.time()
                cooldown_end = self._cooldown_until.get(platform_id, 0)
                if now >= cooldown_end:
                    self._last_request[platform_id] = now
                    self._bump(platform_id, "requests")
                    return wait

    def mark_limited(self, platform_id: str, seconds: float = 0) -> None:
        """标记平台进入限流冷却期，连续触发时冷却时间渐进翻倍。"""
        with self._lock:
            self._bump(platform_id, "limited")
            count = self._fail_count.get(platform_id, 0) + 1
            self._fail_count[platform_id] = count
            if not seconds:
                seconds = min(COOLDOWN_SECONDS * (2 ** (count - 1)), MAX_COOLDOWN_SECONDS)
            self._cooldown_until[platform_id] = time.time() + seconds
            self._last_request[platform_id] = time.time() + seconds

    def cooldown_remaining(self, platform_id: str) -> float:
        with self._lock:
            return max(0.0, self._cooldown_until.get(platform_id, 0) - time.time())

    def fail_count(self, platform_id: str) -> int:
        with self._lock:
            return self._fail_count.get(platform_id, 0)

    def reset(self, platform_id: str | None = None) -> None:
        """重置冷却。成功采集一次后自动调用 fail_count 清零。"""
        with self._lock:
            if platform_id:
                self._cooldown_until.pop(platform_id, None)
                self._last_request.pop(platform_id, None)
                self._fail_count.pop(platform_id, None)
            else:
                self._cooldown_until.clear()
                self._last_request.clear()
                self._fail_count.clear()

    def mark_success(self, platform_id: str) -> None:
        """采集成功，重置连续失败计数。"""
        with self._lock:
            self._fail_count.pop(platform_id, None)

    # ---------------------------------------------------------- 缓存
    @staticmethod
    def _cache_key(platform_id: str, keyword: str, filters: dict, pages: int) -> str:
        raw = f"{platform_id}|{keyword}|{json.dumps(filters, sort_keys=True, ensure_ascii=False)}|{pages}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def cache_get(self, platform_id: str, keyword: str, filters: dict, pages: int) -> Optional[dict]:
        path = CACHE_DIR / f"{self._cache_key(platform_id, keyword, filters, pages)}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - payload.get("_ts", 0) > CACHE_TTL:
                path.unlink(missing_ok=True)
                return None
            return payload.get("data")
        except Exception:  # noqa: BLE001
            return None

    def cache_put(self, platform_id: str, keyword: str, filters: dict, pages: int, data: Any) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = CACHE_DIR / f"{self._cache_key(platform_id, keyword, filters, pages)}.json"
            path.write_text(
                json.dumps({"_ts": time.time(), "data": data}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    def cache_get_stale(
        self, platform_id: str, keyword: str, filters: dict, pages: int
    ) -> Optional[dict]:
        """限流期间读取过期缓存，返回 None 表示无历史结果。"""
        path = CACHE_DIR / f"{self._cache_key(platform_id, keyword, filters, pages)}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            data = payload.get("data")
            if data is not None:
                self._bump(platform_id, "stale_hits")
            return data
        except Exception:  # noqa: BLE001
            return None

    def cache_hit(self, platform_id: str) -> None:
        with self._lock:
            self._bump(platform_id, "cache_hits")

    def stats(self) -> dict:
        """运行指标快照，供诊断接口使用。"""
        with self._lock:
            out: dict[str, dict] = {}
            for platform_id, values in self._stats.items():
                out[platform_id] = {
                    **values,
                    "waited": round(values["waited"], 1),
                    "cooldown_remaining": int(self.cooldown_remaining(platform_id)),
                    "fail_count": self._fail_count.get(platform_id, 0),
                }
            return out


class CooldownActive(Exception):
    """平台处于限流冷却期。"""

    def __init__(self, platform_id: str, remaining: float):
        self.platform_id = platform_id
        self.remaining = remaining
        super().__init__(f"冷却中，剩余 {int(remaining)} 秒")


rate_guard = RateLimitGuard()
