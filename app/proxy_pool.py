"""代理池：多出口 IP 轮换、健康检查、失败冷却与平台粘性会话。

设计目标：
- 单一 SEARCH_PROXY 仍可用，但支持 SEARCH_PROXIES 配置多个出口。
- 同一平台在一段时间内固定使用同一出口（cookie/IP 绑定更稳定）。
- 连续失败自动降低优先级并进入冷却，探测成功后恢复。
- 运行状态保存在 data/proxies.json（运行时数据，不进 git）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .config import DATA_DIR, PROXY_LIST

log = logging.getLogger("app.proxy_pool")

PROXY_FILE = DATA_DIR / "proxies.json"
STICKY_TTL = 600.0          # 平台粘性出口最长保持 10 分钟
PROBE_URL = "https://myip.ipip.net"
MAX_FAIL_COUNT = 3


@dataclass
class ProxyEntry:
    id: str
    name: str
    url: str
    fail_count: int = 0
    cooldown_until: float = 0.0
    last_used: float = 0.0
    last_success: float = 0.0
    healthy: bool = True
    ip: str = ""

    def payload(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "healthy": self.healthy,
            "fail_count": self.fail_count,
            "cooldown_remaining": max(0, int(self.cooldown_until - time.time())),
            "last_used": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_used)) if self.last_used else "",
            "last_success": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_success)) if self.last_success else "",
            "ip": self.ip,
        }


class ProxyPool:
    """线程安全的代理池。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, ProxyEntry] = {}
        self._sticky: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------ 持久化
    def _load(self) -> None:
        records: list[dict] = []
        if PROXY_FILE.exists():
            try:
                records = json.loads(PROXY_FILE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                records = []
        # 环境变量优先，持久化列表兜底；按 URL 去重
        pairs = list(PROXY_LIST) + [
            (r.get("name") or r.get("url"), r.get("url")) for r in records if r.get("url")
        ]
        seen: set[str] = set()
        for name, url in pairs:
            if not url or url in seen:
                continue
            seen.add(url)
            entry = ProxyEntry(id=uuid.uuid4().hex[:10], name=name or url, url=url)
            self._entries[entry.id] = entry

    def _persist(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = [
                {"name": e.name, "url": e.url, "healthy": e.healthy, "fail_count": e.fail_count}
                for e in self._entries.values()
            ]
            PROXY_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("保存代理配置失败：%s", exc)

    # ------------------------------------------------------------ 管理
    def add(self, url: str, name: str = "") -> dict:
        url = (url or "").strip()
        if not url:
            raise ValueError("代理地址不能为空")
        with self._lock:
            for entry in self._entries.values():
                if entry.url == url:
                    return entry.payload()
            entry = ProxyEntry(id=uuid.uuid4().hex[:10], name=name or url, url=url)
            self._entries[entry.id] = entry
            self._persist()
            return entry.payload()

    def remove(self, proxy_id: str) -> bool:
        with self._lock:
            if proxy_id not in self._entries:
                return False
            del self._entries[proxy_id]
            self._sticky = {k: v for k, v in self._sticky.items() if v != proxy_id}
            self._persist()
            return True

    def reset(self, proxy_id: str | None = None) -> None:
        with self._lock:
            targets = [self._entries[proxy_id]] if proxy_id and proxy_id in self._entries else list(self._entries.values())
            for entry in targets:
                entry.fail_count = 0
                entry.cooldown_until = 0.0
                entry.healthy = True
            self._sticky.clear()

    def status(self) -> list[dict]:
        with self._lock:
            return [entry.payload() for entry in self._entries.values()]

    def has_entries(self) -> bool:
        with self._lock:
            return bool(self._entries)

    # ------------------------------------------------------------ 选取与反馈
    def acquire(self, platform_id: str) -> Optional[ProxyEntry]:
        """按平台粘性 + 最近使用最少选取一个可用代理；无代理返回 None（直连）。"""
        with self._lock:
            now = time.time()
            candidates = [
                e for e in self._entries.values()
                if e.healthy and e.cooldown_until <= now
            ]
            if not candidates:
                return None
            sticky_id = self._sticky.get(platform_id)
            sticky = next((e for e in candidates if e.id == sticky_id), None)
            if sticky and now - sticky.last_used < STICKY_TTL:
                chosen = sticky
            else:
                chosen = min(candidates, key=lambda e: (e.last_used, e.fail_count))
                self._sticky[platform_id] = chosen.id
            chosen.last_used = now
            return chosen

    @staticmethod
    def _entry_id(entry_or_id) -> str:
        return entry_or_id if isinstance(entry_or_id, str) else entry_or_id.id

    def mark_success(self, entry_or_id, platform_id: str | None = None) -> None:
        with self._lock:
            entry = self._entries.get(self._entry_id(entry_or_id))
            if not entry:
                return
            entry.fail_count = 0
            entry.cooldown_until = 0.0
            entry.healthy = True
            entry.last_success = time.time()
            if platform_id:
                self._sticky[platform_id] = entry.id

    def mark_failed(self, entry_or_id, platform_id: str | None = None, seconds: float | None = None) -> None:
        with self._lock:
            entry = self._entries.get(self._entry_id(entry_or_id))
            if not entry:
                return
            entry.fail_count += 1
            if seconds is None:
                seconds = min(120.0 * (2 ** (entry.fail_count - 1)), 1800.0)
            entry.cooldown_until = time.time() + seconds
            if entry.fail_count >= MAX_FAIL_COUNT:
                entry.healthy = False
            if platform_id and self._sticky.get(platform_id) == entry.id:
                self._sticky.pop(platform_id, None)

    # ------------------------------------------------------------ 健康检查
    def probe(self, proxy_id: str | None = None) -> list[dict]:
        """用外网 IP 查询验证代理可用性并回写出口 IP。"""
        with self._lock:
            targets = (
                [self._entries[proxy_id]] if proxy_id and proxy_id in self._entries
                else list(self._entries.values())
            )
        results: list[dict] = []
        for entry in targets:
            try:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": entry.url, "https": entry.url})
                )
                with opener.open(PROBE_URL, timeout=6) as response:
                    body = response.read(512).decode("utf-8", errors="ignore")
                ip_match = None
                for token in body.replace("：", ":").split():
                    if token.startswith("IP"):
                        ip_match = token.split(":")[-1]
                        break
                with self._lock:
                    entry.healthy = True
                    entry.fail_count = 0
                    entry.cooldown_until = 0.0
                    entry.ip = ip_match or body.strip()[:80]
                    payload = entry.payload()
                results.append({"id": entry.id, "ok": True, "ip": entry.ip, "entry": payload})
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self.mark_failed(entry.id, seconds=120)
                    payload = entry.payload()
                results.append({"id": entry.id, "ok": False, "error": str(exc), "entry": payload})
        return results


proxy_pool = ProxyPool()
