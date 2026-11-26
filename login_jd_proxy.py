"""用代理 + 有头浏览器登录京东（解除 IP 封禁后需重新登录）。

运行后弹出浏览器显示京东登录页，用手机京东 APP 扫码登录。
登录态保存到 data/profiles/jd_proxy_login，供采集复用。
检测放宽：识别京东登录 cookie(jk/pin) 或离开 passport 页即认为成功。
"""
from __future__ import annotations

import time

from playwright.sync_api import sync_playwright

PROXY = "http://127.0.0.1:65202"
PROFILE = r"E:\python_WorkSpace\data-scan\data\profiles\jd_proxy_login"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
LOGIN_COOKIE_NAMES = {"jk", "jkv", "pin", "jwtoken", "thor", "sessionid", "jdtoken"}


def is_logged_in(ctx, page) -> bool:
    # cookie 判定
    try:
        names = {c["name"].lower() for c in ctx.cookies()}
        if names & LOGIN_COOKIE_NAMES:
            return True
    except Exception:  # noqa: BLE001
        pass
    # 页面判定：离开 passport 且页面含“我的京东/退出”等登录态元素
    try:
        url = page.url or ""
        if "passport.jd.com" not in url and "login.aspx" not in url:
            body = page.locator("body").inner_text(timeout=2500) or ""
            if any(k in body for k in ("我的京东", "退出登录", "我的订单", "购物车")):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


print("=" * 60)
print("京东登录（代理模式）")
print("=" * 60)
print("已弹出浏览器，请用手机京东 APP 扫码登录。等待 8 分钟。")
print("=" * 60)

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE,
        headless=False,
        channel="msedge",
        proxy={"server": PROXY},
        viewport={"width": 1366, "height": 900},
        user_agent=UA,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://passport.jd.com/uc/login", wait_until="domcontentloaded", timeout=60000)
    print("已打开登录页，请扫码……")

    deadline = time.time() + 480  # 8 分钟
    logged_in = False
    while time.time() < deadline:
        if is_logged_in(ctx, page):
            print("✅ 检测到登录成功")
            logged_in = True
            break
        time.sleep(3)

    if not logged_in:
        print("⏰ 超时，未检测到登录")
    else:
        page.wait_for_timeout(1500)
        # 登录后验证搜索
        try:
            page.goto(
                "https://search.jd.com/Search?keyword=%E6%9C%BA%E6%A2%B0%E9%94%AE%E7%9B%98&enc=utf-8",
                wait_until="domcontentloaded", timeout=60000,
            )
            page.wait_for_timeout(5000)
            cards = page.locator("div[data-sku].plugin_goodsCardWrapper").count()
            print("搜索页商品卡片数:", cards)
        except Exception as exc:  # noqa: BLE001
            print("搜索页检查失败:", exc)
    ctx.close()
    print("浏览器已关闭")
