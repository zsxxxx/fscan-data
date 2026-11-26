"""优惠监控引擎：后台定时轮询任务，采集实时数据并生成优惠告警。

设计要点：
- 后台单线程调度器，按任务间隔轮询；不同平台任务交错执行。
- 复用 rate_guard 节流/冷却，避免触发平台限流。
- 复用现有 crawler 采集链路，不重复造轮子。
- 告警落 SQLite，前端可轮询读取。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Optional

from playwright.sync_api import sync_playwright

from . import store
from .browser import BROWSER_LOCK, launch_context
from .config import PLATFORM_MAP
from .crawlers.base import CaptchaRequiredError, LoginRequiredError, RateLimitedError
from .crawlers.registry import get_crawler
from .guard import CooldownActive, rate_guard
from .proxy_pool import proxy_pool

log = logging.getLogger("app.monitor")

TICK_SECONDS = 10  # 调度器心跳


class MonitorEngine:
    """优惠监控调度器。"""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._running_tasks: set[str] = set()
        self._lock = threading.Lock()
        self._last_run: dict[str, float] = {}

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="deal-monitor", daemon=True)
        self._thread.start()
        log.info("优惠监控调度器已启动")

    def stop(self) -> None:
        self._stop.set()
        log.info("优惠监控调度器已停止")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                log.exception("监控调度异常：%s", exc)
            self._stop.wait(TICK_SECONDS)

    def _tick(self) -> None:
        tasks = store.list_tasks()
        now = time.time()
        for task in tasks:
            if self._stop.is_set():
                break
            if not task.get("enabled"):
                continue
            task_id = task["id"]
            with self._lock:
                if task_id in self._running_tasks:
                    continue
                last = self._last_run.get(task_id, 0)
                if now - last < int(task.get("interval", 300)):
                    continue
                self._running_tasks.add(task_id)
                self._last_run[task_id] = now
            threading.Thread(
                target=self._run_task, args=(task,), name=f"monitor-{task_id}", daemon=True
            ).start()

    # ------------------------------------------------------------ 任务执行
    def _run_task(self, task: dict) -> None:
        task_id = task["id"]
        platform_id = task["platform"]
        platform = PLATFORM_MAP.get(platform_id)
        run_id = uuid.uuid4().hex[:12]
        status = "running"
        error = ""
        product_count = 0
        proxy_entry = None
        proxy_url = ""
        started = time.time()
        store.start_run(
            {
                "id": run_id,
                "platform": platform_id,
                "keyword": task.get("keyword", ""),
                "filters": task.get("filters", {}),
                "pages": 1,
                "status": status,
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        try:
            if platform is None or not platform.enabled:
                raise RuntimeError(f"平台 {platform_id} 不可用")
            crawler = get_crawler(platform)
            if crawler is None:
                raise RuntimeError(f"平台 {platform_id} 无采集适配器")

            # 冷却期内跳过
            if rate_guard.cooldown_remaining(platform_id) > 0:
                log.info("任务 %s 跳过：%s 处于冷却期", task_id, platform.name)
                status = "skipped"
                return

            try:
                rate_guard.acquire(platform_id)
            except CooldownActive:
                status = "skipped"
                return

            proxy_entry = proxy_pool.acquire(platform_id)
            proxy_url = proxy_entry.url if proxy_entry else ""
            with BROWSER_LOCK:
                with sync_playwright() as pw:
                    context = launch_context(pw, platform, headless=True, proxy=proxy_url)
                    page = context.pages[0] if context.pages else context.new_page()
                    try:
                        products = crawler.crawl(
                            page, task["keyword"], 1, task.get("filters", {}), run_id
                        )
                    finally:
                        try:
                            context.close()
                        except Exception:  # noqa: BLE001
                            pass

            self._analyze(task, products)
            product_count = len(products)
            store.save_task({
                **task,
                "status": "idle",
                "last_error": "",
                "last_run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "run_count": int(task.get("run_count", 0)) + 1,
            })
            rate_guard.mark_success(platform_id)
            if proxy_entry:
                proxy_pool.mark_success(proxy_entry, platform_id)
            status = "success"
            log.info("监控任务 %s 完成，采集 %d 条", task_id, len(products))

        except LoginRequiredError as exc:
            status = "login_required"
            error = str(exc)
            self._task_error(task, error)
        except CaptchaRequiredError as exc:
            status = "captcha"
            error = str(exc)
            rate_guard.mark_limited(platform_id, seconds=600)
            if proxy_entry:
                proxy_pool.mark_failed(proxy_entry, platform_id, seconds=600)
            self._task_error(task, error)
        except RateLimitedError as exc:
            status = "limited"
            error = str(exc)
            rate_guard.mark_limited(platform_id)
            if proxy_entry:
                proxy_pool.mark_failed(proxy_entry, platform_id)
            self._task_error(task, error)
        except Exception as exc:  # noqa: BLE001
            status = "error"
            error = str(exc)
            log.exception("监控任务 %s 失败", task_id)
            self._task_error(task, error)
        finally:
            store.finish_run(
                run_id, status, product_count, time.time() - started,
                error=error, proxy=proxy_url,
            )
            with self._lock:
                self._running_tasks.discard(task_id)

    def _task_error(self, task: dict, message: str) -> None:
        store.save_task({
            **task,
            "status": "error",
            "last_error": message[:200],
            "last_run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    # ------------------------------------------------------------ 优惠分析
    def _analyze(self, task: dict, products) -> None:
        """对比历史价格，生成告警。"""
        platform_id = task["platform"]
        platform_name = PLATFORM_MAP.get(platform_id).name if platform_id in PLATFORM_MAP else platform_id

        for product in products:
            sku = product.sku or product.url
            if not sku:
                continue

            effective = product.promo_price if product.promo_price is not None else product.price_value
            if effective is None:
                continue

            # 与上一次采集价对比
            last = store.get_last_price(platform_id, sku)
            old_price = None
            if last:
                old_price = last.get("promo_price") or last.get("price")

            alert_type = ""
            message = ""
            discount = None

            # 0元购 / 0元试用
            if product.is_free_buy:
                alert_type = "free_buy"
                message = f"{product.title} 出现 0元购"
            elif product.is_free_trial:
                alert_type = "free_trial"
                message = f"{product.title} 出现 0元试用"
            # 降价：与上次对比
            elif old_price and effective < old_price:
                alert_type = "price_drop"
                discount = round(old_price - effective, 2)
                message = f"{product.title} 降价 ¥{discount}（¥{old_price} → ¥{effective}）"
            # 历史低价
            elif product.history_low and effective <= product.history_low:
                alert_type = "low_price"
                message = f"{product.title} 触及历史低价 ¥{effective}"
            # 促销/优惠券
            elif product.promo_type in ("促销", "优惠券"):
                alert_type = "coupon" if product.promo_type == "优惠券" else "promo"
                message = f"{product.title} {product.promo_label or product.promo_type}"

            if alert_type:
                store.add_alert({
                    "task_id": task["id"],
                    "platform": platform_id,
                    "platform_name": platform_name,
                    "sku": sku,
                    "title": product.title,
                    "url": product.url,
                    "image": product.image,
                    "alert_type": alert_type,
                    "message": message,
                    "old_price": old_price,
                    "new_price": effective,
                    "discount": discount,
                    "promo_label": product.promo_label,
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })


monitor_engine = MonitorEngine()
