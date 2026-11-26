"""FastAPI 入口：前端页面、平台登录、商品搜索 API。"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright

from .browser import BROWSER_LOCK, launch_context, login_manager
from .config import DATA_DIR, HISTORY_FILE, PLATFORMS, PLATFORM_MAP, STATIC_DIR
from .crawlers.base import CaptchaRequiredError, LoginRequiredError, RateLimitedError
from .crawlers.registry import get_crawler
from .guard import CooldownActive, rate_guard
from .proxy_pool import proxy_pool
from .models import (
    MonitorTaskCreate,
    PlatformStatus,
    SearchRequest,
    SearchResponse,
)
from . import store
from .monitor import monitor_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("app.main")

app = FastAPI(title="商品数据扫描", version="2.0.0", description="多平台商品规格与链接采集")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_HISTORY_LOCK = threading.Lock()


@app.on_event("startup")
def on_startup():
    monitor_engine.start()


@app.on_event("shutdown")
def on_shutdown():
    monitor_engine.stop()


def _platform(platform_id: str):
    platform = PLATFORM_MAP.get(platform_id)
    if platform is None:
        raise HTTPException(status_code=404, detail=f"未知平台：{platform_id}")
    return platform


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    return {"ok": True, "service": "商品数据扫描"}


@app.get("/api/platforms")
def platforms():
    """返回前端菜单所需的平台列表（不主动启动浏览器检测登录态）。"""
    return {
        "platforms": [
            {**p.payload(), "profile_exists": p.profile_dir().exists()}
            for p in PLATFORMS
        ]
    }


@app.get("/api/platforms/{platform_id}/status", response_model=PlatformStatus)
def platform_status(platform_id: str, force: bool = False):
    platform = _platform(platform_id)
    logged_in, detail = login_manager.check_status(platform, force=force)
    return PlatformStatus(
        id=platform.id,
        name=platform.name,
        color=platform.color,
        enabled=platform.enabled,
        note=platform.note,
        logged_in=logged_in,
        profile_exists=platform.profile_dir().exists(),
        detail=detail,
        login_task=login_manager.task_state(platform.id),
    )


@app.post("/api/platforms/{platform_id}/login")
def start_login(platform_id: str):
    platform = _platform(platform_id)
    return login_manager.start_login(platform)


@app.get("/api/platforms/{platform_id}/login-task")
def login_task(platform_id: str):
    _platform(platform_id)
    return login_manager.task_state(platform_id)


@app.post("/api/platforms/{platform_id}/login/cancel")
def cancel_login(platform_id: str):
    _platform(platform_id)
    login_manager.cancel_login(platform_id)
    return {"ok": True}


@app.delete("/api/platforms/{platform_id}/login")
def clear_login(platform_id: str):
    platform = _platform(platform_id)
    login_manager.clear_login(platform)
    return {"ok": True, "message": "登录态已清除"}


def _as_number(value) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _sales_number(text: str) -> float:
    match = re.search(r"([\d,.]+)\s*(万|w)?", text or "", re.IGNORECASE)
    if not match:
        return 0.0
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return 0.0
    return value * 10000 if match.group(2) else value


def _apply_filters(products, req: SearchRequest, platform) -> tuple[list, bool]:
    """按平台专属条件做本地补充过滤；条件无命中时回退为未过滤结果。"""
    filters = {k: str(v).strip() for k, v in (req.filters or {}).items() if str(v).strip()}
    original = list(products)
    if not original:
        return [], True
    matched = original
    strict = True

    terms = [t for t in re.split(r"[\s,，;；、]+", (req.specs or "").strip()) if t]
    if terms:
        hits = [
            p for p in matched
            if all(term.lower() in f"{p.title} {p.specs}".lower() for term in terms)
        ]
        if hits:
            matched = hits
        else:
            strict = False

    price_min = _as_number(filters.get("price_min"))
    price_max = _as_number(filters.get("price_max"))
    # 价格区间下拉：如 "100-300" / "10000-"（不限上限）
    price_range = (filters.get("price_range") or "").strip()
    if price_range and "-" in price_range:
        range_min, _, range_max = price_range.partition("-")
        if price_min is None and range_min:
            price_min = _as_number(range_min)
        if price_max is None and range_max:
            price_max = _as_number(range_max)
    if price_min is None:
        price_min = req.min_price
    if price_max is None:
        price_max = req.max_price
    if price_min is not None:
        matched = [p for p in matched if p.price_value is not None and p.price_value >= price_min]
    if price_max is not None:
        matched = [p for p in matched if p.price_value is not None and p.price_value <= price_max]

    # 平台专属文本/勾选条件：多值逗号分隔，任一命中即可；命中不到则保留原结果
    def _any_hit(product, terms: list[str]) -> bool:
        haystack = f"{product.title} {product.shop} {product.specs} {getattr(product, 'promo_label', '')} {getattr(product, 'coupon', '')}".lower()
        return any(term.lower() in haystack for term in terms)

    # 一级 category 已并入搜索关键词，这里只对具体的二级分类与勾选值做本地过滤
    for key in ("brand", "location", "category_sub", "service"):
        value = filters.get(key)
        if not value:
            continue
        terms = [t.strip() for t in str(value).split(",") if t.strip()]
        if not terms:
            continue
        hits = [p for p in matched if _any_hit(p, terms)]
        if hits:
            matched = hits
        else:
            strict = False

    keyword_by_field = {
        "channel": {"self": "自营", "logistics": "物流"},
        "shop_type": {"mall": "天猫", "t": "淘宝"},
        "supplier": {"factory": "工厂", "strong": "实力商家"},
    }
    for key, mapping in keyword_by_field.items():
        value = filters.get(key)
        if not value:
            continue
        needle = mapping.get(value, value)
        hits = [p for p in matched if needle.lower() in f"{p.title} {p.shop}".lower()]
        if hits:
            matched = hits
        else:
            strict = False

    if not matched and original:
        matched = original
        strict = False

    sort = filters.get("sort", "")
    if sort in ("price_asc", "_priceasc", "sort_asc"):
        matched.sort(key=lambda p: (p.price_value is None, p.price_value or 0))
    elif sort in ("price_desc", "_pricedesc", "sort_desc"):
        matched.sort(key=lambda p: (p.price_value is not None, p.price_value or 0), reverse=True)
    elif sort in ("sales_desc", "_sale", "sort_totalsales15_desc"):
        matched.sort(key=lambda p: _sales_number(p.sales), reverse=True)

    if req.max_results and req.max_results > 0:
        matched = matched[: req.max_results]
    return matched, strict


def _append_history(record: dict) -> None:
    try:
        with _HISTORY_LOCK:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            items: list = []
            if HISTORY_FILE.exists():
                items = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            items.insert(0, record)
            HISTORY_FILE.write_text(
                json.dumps(items[:50], ensure_ascii=False, indent=2), encoding="utf-8"
            )
    except Exception as exc:  # noqa: BLE001 - 历史记录失败不影响主流程
        log.warning("保存搜索历史失败：%s", exc)


@app.get("/api/platforms/{platform_id}/cooldown")
def cooldown_status(platform_id: str):
    """查询平台限流冷却剩余秒数。"""
    _platform(platform_id)
    remaining = rate_guard.cooldown_remaining(platform_id)
    return {
        "platform": platform_id,
        "remaining": int(remaining),
        "limited": remaining > 0,
        "fail_count": rate_guard.fail_count(platform_id),
    }


@app.post("/api/platforms/{platform_id}/cooldown/reset")
def reset_cooldown(platform_id: str):
    """手动解除冷却（确认限流已解除时使用）。"""
    _platform(platform_id)
    rate_guard.reset(platform_id)
    return {"ok": True, "message": "已解除冷却"}


@app.get("/api/history")
def history():
    if not HISTORY_FILE.exists():
        return {"items": []}
    try:
        return {"items": json.loads(HISTORY_FILE.read_text(encoding="utf-8"))}
    except Exception:  # noqa: BLE001
        return {"items": []}


@app.post("/api/search", response_model=SearchResponse)
def search(req: SearchRequest):
    platform = _platform(req.platform)
    crawler = get_crawler(platform)
    if not platform.enabled or crawler is None:
        raise HTTPException(
            status_code=501,
            detail=f"{platform.name} 平台适配器开发中，暂不支持采集",
        )

    keyword = req.keyword.strip()
    started = time.time()
    run_id = uuid.uuid4().hex[:12]
    status = "running"
    error = ""
    product_count = 0
    stale = False
    proxy_entry = None
    proxy_url = ""
    store.start_run(
        {
            "id": run_id,
            "platform": platform.id,
            "keyword": keyword,
            "filters": req.filters,
            "pages": req.pages,
            "status": status,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )

    try:
        # 1) 冷却期：优先返回过期缓存，避免用户拿到 429 空结果
        remaining = rate_guard.cooldown_remaining(platform.id)
        if remaining > 0:
            cached = rate_guard.cache_get_stale(platform.id, keyword, req.filters, req.pages)
            if cached is not None:
                status = "stale"
                stale = True
                product_count = int(cached.get("total", len(cached.get("products", []))))
                payload = {**cached, "run_id": run_id, "stale": True}
                payload["message"] = (
                    f"平台限流冷却中（约 {int(remaining)} 秒），展示上次缓存结果"
                )
                return SearchResponse(**payload)
            raise HTTPException(
                status_code=429,
                detail=f"{platform.name} 仍处于限流冷却期，请约 {int(remaining)} 秒后再试",
            )

        # 2) 有效缓存直接返回
        cached = rate_guard.cache_get(platform.id, keyword, req.filters, req.pages)
        if cached is not None:
            log.info("命中缓存：%s / %s", platform.name, keyword)
            rate_guard.cache_hit(platform.id)
            status = "cache"
            product_count = int(cached.get("total", len(cached.get("products", []))))
            return SearchResponse(**{**cached, "run_id": run_id, "stale": False})

        # 3) 自适应节流
        try:
            rate_guard.acquire(platform.id)
        except CooldownActive as exc:
            raise HTTPException(
                status_code=429,
                detail=f"{platform.name} 仍处于限流冷却期，请约 {int(exc.remaining)} 秒后再试",
            ) from exc

        proxy_entry = proxy_pool.acquire(platform.id)
        proxy_url = proxy_entry.url if proxy_entry else ""
        log.info(
            "开始采集：%s / %s / %s 页 / 条件 %s / 出口 %s",
            platform.name, keyword, req.pages, req.filters, proxy_url or "直连",
        )
        with BROWSER_LOCK:
            with sync_playwright() as pw:
                context = launch_context(pw, platform, headless=True, proxy=proxy_url)
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    products = crawler.crawl(page, keyword, req.pages, req.filters, run_id)
                finally:
                    try:
                        context.close()
                    except Exception:  # noqa: BLE001
                        pass

        filtered, strict = _apply_filters(products, req, platform)
        product_count = len(filtered)
        message = ""
        if not products:
            message = "未采集到商品，请确认已登录并检查平台页面结构是否变化"
        elif not strict:
            message = "筛选条件未命中任何商品，已展示未过滤结果"

        _append_history(
            {
                "platform": platform.id,
                "platform_name": platform.name,
                "keyword": keyword,
                "specs": req.specs.strip(),
                "filters": req.filters,
                "count": product_count,
                "elapsed": round(time.time() - started, 2),
                "run_id": run_id,
                "proxy": proxy_url,
                "stale": False,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

        response = SearchResponse(
            platform=platform.id,
            platform_name=platform.name,
            keyword=keyword,
            specs=req.specs.strip(),
            products=filtered,
            total=product_count,
            filtered=strict,
            message=message,
            elapsed=round(time.time() - started, 2),
            run_id=run_id,
        )
        if products:
            rate_guard.mark_success(platform.id)
            if proxy_entry:
                proxy_pool.mark_success(proxy_entry, platform.id)
            rate_guard.cache_put(
                platform.id, keyword, req.filters, req.pages, response.model_dump()
            )
        status = "success"
        return response

    except LoginRequiredError as exc:
        status = "login_required"
        error = str(exc)
        raise HTTPException(status_code=401, detail=error) from exc
    except CaptchaRequiredError as exc:
        status = "captcha"
        error = str(exc)
        rate_guard.mark_limited(platform.id, seconds=600)
        if proxy_entry:
            proxy_pool.mark_failed(proxy_entry, platform.id, seconds=600)
        raise HTTPException(status_code=428, detail=error) from exc
    except RateLimitedError as exc:
        status = "limited"
        error = str(exc)
        rate_guard.mark_limited(platform.id)
        if proxy_entry:
            proxy_pool.mark_failed(proxy_entry, platform.id)
        remaining = rate_guard.cooldown_remaining(platform.id)
        raise HTTPException(
            status_code=429,
            detail=f"{platform.name} 检测到访问频繁，已被临时限流，请约 {int(remaining / 60) + 1} 分钟后再重试",
        ) from exc
    except HTTPException as exc:
        status = "error"
        error = str(exc.detail)
        raise
    except Exception as exc:  # noqa: BLE001
        status = "error"
        error = str(exc)
        log.exception("采集 %s 失败", platform.id)
        if proxy_entry:
            proxy_pool.mark_failed(proxy_entry, platform.id, seconds=60)
        raise HTTPException(status_code=502, detail=f"采集失败：{exc}") from exc
    finally:
        store.finish_run(
            run_id, status, product_count, time.time() - started,
            error=error, proxy=proxy_url, stale=stale,
        )


# ================================================================== 优惠监控 API

@app.get("/api/monitor/tasks")
def monitor_tasks():
    """列出所有监控任务。"""
    return {"tasks": store.list_tasks()}


@app.post("/api/monitor/tasks")
def create_monitor_task(req: MonitorTaskCreate):
    platform = _platform(req.platform)
    task_id = uuid.uuid4().hex[:10]
    task = {
        "id": task_id,
        "platform": req.platform,
        "platform_name": platform.name,
        "keyword": req.keyword.strip(),
        "filters": req.filters,
        "specs": req.specs.strip(),
        "interval": req.interval,
        "enabled": req.enabled,
        "status": "idle",
        "last_error": "",
        "last_run_at": "",
        "run_count": 0,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    store.save_task(task)
    return {"ok": True, "task": task}


@app.put("/api/monitor/tasks/{task_id}")
def update_monitor_task(task_id: str, payload: dict):
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="监控任务不存在")
    task.update({k: v for k, v in payload.items() if k in (
        "keyword", "filters", "specs", "interval", "enabled"
    )})
    store.save_task(task)
    return {"ok": True, "task": task}


@app.delete("/api/monitor/tasks/{task_id}")
def delete_monitor_task(task_id: str):
    store.delete_task(task_id)
    return {"ok": True}


@app.post("/api/monitor/tasks/{task_id}/toggle")
def toggle_monitor_task(task_id: str):
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="监控任务不存在")
    task["enabled"] = not task["enabled"]
    store.save_task(task)
    return {"ok": True, "enabled": task["enabled"]}


@app.get("/api/monitor/alerts")
def monitor_alerts(limit: int = 100, unread_only: bool = False):
    """优惠告警消息列表。"""
    return {"alerts": store.list_alerts(limit=limit, unread_only=unread_only)}


@app.post("/api/monitor/alerts/read/{alert_id}")
def read_alert(alert_id: str):
    store.mark_alert_read(alert_id)
    return {"ok": True}


@app.post("/api/monitor/alerts/read-all")
def read_all_alerts():
    store.mark_all_read()
    return {"ok": True}


@app.delete("/api/monitor/alerts")
def clear_alerts():
    store.clear_alerts()
    return {"ok": True}


# ================================================================== 采集诊断与代理池

@app.get("/api/crawl/runs")
def crawl_runs(limit: int = 50, platform: str | None = None):
    """采集批次列表（含状态、代理、耗时、错误信息）。"""
    return {"runs": store.list_runs(limit=limit, platform=platform)}


@app.get("/api/crawl/observations")
def crawl_observations(
    platform: str | None = None,
    keyword: str | None = None,
    sku: str | None = None,
    run_id: str | None = None,
    limit: int = 100,
):
    """规范化商品观测记录。"""
    return {
        "observations": store.list_observations(
            platform=platform, keyword=keyword, sku=sku, run_id=run_id, limit=limit
        )
    }


@app.get("/api/crawl/diagnostics")
def crawl_diagnostics():
    """限流指标 + 代理健康 + 最近批次，便于定位采集失败原因。"""
    return {
        "guard": rate_guard.stats(),
        "proxies": proxy_pool.status(),
        "runs": store.list_runs(limit=10),
    }


@app.get("/api/proxies")
def list_proxies():
    return {"proxies": proxy_pool.status()}


@app.post("/api/proxies")
def add_proxy(payload: dict):
    urls = payload.get("urls") or []
    if isinstance(urls, str):
        urls = [urls]
    if not urls and payload.get("url"):
        urls = [payload["url"]]
    if not urls:
        raise HTTPException(status_code=400, detail="请提供 url 或 urls")
    added = [proxy_pool.add(str(url), str(payload.get("name", ""))) for url in urls]
    return {"ok": True, "proxies": added}


@app.delete("/api/proxies/{proxy_id}")
def delete_proxy(proxy_id: str):
    if not proxy_pool.remove(proxy_id):
        raise HTTPException(status_code=404, detail="代理不存在")
    return {"ok": True}


@app.post("/api/proxies/{proxy_id}/reset")
def reset_proxy(proxy_id: str):
    proxy_pool.reset(proxy_id)
    return {"ok": True}


@app.post("/api/proxies/probe")
def probe_proxies(payload: dict | None = None):
    proxy_id = (payload or {}).get("id")
    return {"results": proxy_pool.probe(proxy_id)}


@app.get("/api/products/{platform}/{sku}/history")
def product_history(platform: str, sku: str, limit: int = 50):
    """商品价格历史与统计。"""
    return {
        "platform": platform,
        "sku": sku,
        "range": store.get_price_range(platform, sku),
        "series": store.get_price_series(platform, sku, limit=limit),
    }


@app.get("/api/promo-zones/{platform_id}")
def promo_zones(platform_id: str):
    """平台的优惠专区入口。"""
    platform = _platform(platform_id)
    return {
        "platform": platform_id,
        "zones": [
            {"name": name, "type": ptype, "url": url}
            for name, ptype, url in platform.promo_zones
        ],
    }
