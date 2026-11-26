"""Playwright 浏览器会话管理：持久化登录态、登录检测、登录任务调度。

设计要点：
- 每个平台一个独立 user_data_dir，扫码登录一次后长期复用。
- 所有浏览器操作串行化（BROWSER_LOCK），避免多个 persistent context 抢占同一配置目录。
- 登录在后台线程执行并轮询登录状态，前端通过 /api/platforms/{id}/login-task 获取进度。
"""

from __future__ import annotations

import logging
import random
import shutil
import threading
import time
from datetime import datetime
from typing import Optional

from playwright.sync_api import BrowserContext, sync_playwright

from .config import BROWSER_CHANNEL, HEADLESS, Platform
from .proxy_pool import proxy_pool

log = logging.getLogger("app.browser")

# 所有 Playwright 操作必须串行，防止同一 profile 被两个进程同时打开
BROWSER_LOCK = threading.RLock()

# 版本与 UA 保持一致，避免 UA/内核版本不匹配触发风控
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
)
VIEWPORTS = ((1366, 900), (1440, 900), (1536, 864), (1920, 1080))

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--disable-features=IsolateOrigins,site-per-process",
]

# 仅抹掉最明显的自动化标记，不改平台业务逻辑所需的对象
_STEALTH_JS = r"""(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) window.chrome.runtime = {};
    if (!navigator.plugins || navigator.plugins.length === 0) {
      Object.defineProperty(navigator, 'plugins', {
        get: () => [
          { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
          { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
        ],
      });
    }
  } catch (e) { /* 某些内核不允许覆盖时静默跳过 */ }
})();"""


def launch_context(
    playwright,
    platform: Platform,
    headless: bool = True,
    proxy: str = "",
) -> BrowserContext:
    """用平台专属持久化目录启动浏览器上下文。

    proxy 为空时由代理池按平台选取；未配置代理则直连。
    每次启动使用一致的 UA / 视口 / 语言 / 时区，降低指纹波动。
    """
    width, height = random.choice(VIEWPORTS)
    kwargs = dict(
        user_data_dir=str(platform.profile_dir()),
        headless=headless,
        viewport={"width": width, "height": height},
        user_agent=random.choice(USER_AGENTS),
        args=_LAUNCH_ARGS,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        color_scheme="light",
    )
    platform.profile_dir().parent.mkdir(parents=True, exist_ok=True)
    selected = proxy
    if not selected:
        entry = proxy_pool.acquire(platform.id)
        selected = entry.url if entry else ""
    if selected:
        kwargs["proxy"] = {"server": selected}

    channels = [BROWSER_CHANNEL] if BROWSER_CHANNEL else ["", "msedge", "chrome"]
    last_error: Exception | None = None
    for channel in channels:
        attempt = dict(kwargs)
        if channel:
            attempt["channel"] = channel
        try:
            context = playwright.chromium.launch_persistent_context(**attempt)
            context.add_init_script(_STEALTH_JS)
            return context
        except Exception as exc:  # noqa: BLE001 - 尝试下一个内核
            last_error = exc
            log.debug("浏览器内核 %s 启动失败：%s", channel or "chromium", exc)
    raise RuntimeError(f"无法启动浏览器内核：{last_error}")


def human_pause(page, min_ms: int = 250, max_ms: int = 900) -> None:
    """类人短暂停顿，避免固定节奏被识别为脚本。"""
    page.wait_for_timeout(random.randint(min_ms, max_ms))


def human_scroll(page, rounds: int = 2) -> None:
    """分段滚动 + 随机停顿，模拟人工浏览搜索结果。"""
    for _ in range(rounds):
        step = random.randint(420, 900)
        page.mouse.move(random.randint(200, 1100), random.randint(200, 700))
        page.evaluate(f"window.scrollBy(0, {step})")
        human_pause(page, 300, 900)
    human_pause(page, 200, 500)


def _detect_logged_in(context: BrowserContext, platform: Platform) -> bool:
    """根据平台配置的登录态选择器判断当前上下文是否已登录。"""
    for page in list(context.pages):
        for selector in platform.login_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible(timeout=1500):
                    return True
            except Exception:  # noqa: BLE001 - 页面尚未加载完成属正常情况
                continue
    return False


class LoginManager:
    """管理各平台的登录任务与登录态检测。"""

    def __init__(self) -> None:
        self._states: dict[str, dict] = {}
        self._cancel: set[str] = set()
        self._status_cache: dict[str, tuple[float, bool, str]] = {}
        self._cache_ttl = 300.0  # 5 分钟内复用检测结果
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 状态
    def task_state(self, platform_id: str) -> dict:
        with self._lock:
            state = self._states.get(platform_id)
            if state:
                return dict(state)
        return {"status": "idle", "message": "", "platform": platform_id}

    def _set_state(self, platform_id: str, status: str, message: str = "") -> None:
        with self._lock:
            self._states[platform_id] = {
                "platform": platform_id,
                "status": status,
                "message": message,
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            }
        if status in ("success", "failed", "timeout", "cancelled"):
            self._status_cache.pop(platform_id, None)

    # ------------------------------------------------------------ 登录
    def start_login(self, platform: Platform, timeout: int = 300) -> dict:
        """启动后台登录任务，立即返回。"""
        current = self.task_state(platform.id)
        if current.get("status") == "running":
            return current
        with self._lock:
            self._cancel.discard(platform.id)
            self._states[platform.id] = {
                "platform": platform.id,
                "status": "running",
                "message": "正在打开浏览器，请在弹出的窗口中扫码登录…",
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            }
        threading.Thread(
            target=self._run_login,
            args=(platform, timeout),
            name=f"login-{platform.id}",
            daemon=True,
        ).start()
        return self.task_state(platform.id)

    def cancel_login(self, platform_id: str) -> None:
        with self._lock:
            self._cancel.add(platform_id)
        self._set_state(platform_id, "cancelled", "已取消登录")

    def _run_login(self, platform: Platform, timeout: int) -> None:
        with BROWSER_LOCK:
            context: Optional[BrowserContext] = None
            try:
                with sync_playwright() as pw:
                    context = launch_context(pw, platform, headless=False)
                    page = context.pages[0] if context.pages else context.new_page()
                    try:
                        page.goto(platform.login_url, wait_until="domcontentloaded", timeout=60000)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("打开 %s 登录页失败: %s", platform.name, exc)
                    self._set_state(
                        platform.id,
                        "running",
                        "浏览器已打开，请扫码或账号登录；登录完成后会自动检测。",
                    )

                    deadline = time.time() + timeout
                    while time.time() < deadline:
                        if platform.id in self._cancel:
                            self._set_state(platform.id, "cancelled", "已取消登录")
                            return
                        if _detect_logged_in(context, platform):
                            self._set_state(platform.id, "success", "登录成功，登录态已保存")
                            return
                        if not context.pages or page.is_closed():
                            self._set_state(
                                platform.id,
                                "failed",
                                "浏览器窗口已关闭。若已完成登录，请点击「刷新登录态」。",
                            )
                            return
                        time.sleep(2)
                    self._set_state(platform.id, "timeout", "等待登录超时，请重试")
            except Exception as exc:  # noqa: BLE001
                log.exception("登录 %s 失败", platform.id)
                self._set_state(platform.id, "failed", f"登录失败：{exc}")
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:  # noqa: BLE001
                        pass

    # ------------------------------------------------------------ 检测
    def check_status(self, platform: Platform, force: bool = False) -> tuple[bool, str]:
        """用无头浏览器检测登录态，结果带短期缓存。"""
        if not platform.profile_dir().exists():
            self._status_cache.pop(platform.id, None)
            return False, "尚未登录"
        cached = self._status_cache.get(platform.id)
        if not force and cached and time.time() - cached[0] < self._cache_ttl:
            return cached[1], cached[2]

        with BROWSER_LOCK:
            context: Optional[BrowserContext] = None
            try:
                with sync_playwright() as pw:
                    context = launch_context(pw, platform, headless=True)
                    page = context.pages[0] if context.pages else context.new_page()
                    try:
                        page.goto(platform.home_url, wait_until="domcontentloaded", timeout=45000)
                    except Exception as exc:  # noqa: BLE001
                        return False, f"无法打开平台页面：{exc}"
                    page.wait_for_timeout(2500)
                    logged_in = _detect_logged_in(context, platform)
                    detail = "已登录" if logged_in else "未登录，请先点击「提前登录」"
                    self._status_cache[platform.id] = (time.time(), logged_in, detail)
                    return logged_in, detail
            except Exception as exc:  # noqa: BLE001
                log.exception("检测 %s 登录态失败", platform.id)
                return False, f"检测失败：{exc}"
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:  # noqa: BLE001
                        pass

    def clear_login(self, platform: Platform) -> None:
        """删除该平台的持久化登录目录。"""
        if self.task_state(platform.id).get("status") == "running":
            self.cancel_login(platform.id)
            time.sleep(1.0)
        profile = platform.profile_dir()
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
        self._status_cache.pop(platform.id, None)
        self._set_state(platform.id, "idle", "登录态已清除")


login_manager = LoginManager()
