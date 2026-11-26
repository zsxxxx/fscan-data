"""人工辅助解除京东风控验证。

运行后弹出有头浏览器，用户手动完成「快速验证」滑块/点击，
验证凭证会保存到 data/profiles/jd，供后续无头采集复用。
"""
from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

PROFILE = r"E:\python_WorkSpace\data-scan\data\profiles\jd"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

print("=" * 60)
print("京东风控验证辅助工具")
print("=" * 60)
print("即将打开浏览器，请在窗口中完成「快速验证」（点击/滑块）。")
print("验证通过后会自动跳转京东首页，本脚本检测到商品页即关闭。")
print("=" * 60)

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE,
        headless=False,          # 有头，用户可交互
        channel="msedge",
        viewport={"width": 1366, "height": 900},
        user_agent=UA,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://www.jd.com", wait_until="domcontentloaded", timeout=60000)
    print("已打开，请在浏览器中完成验证……（最多等待 180 秒）")

    deadline = time.time() + 180
    verified = False
    while time.time() < deadline:
        try:
            url = page.url
            title = page.title()
            if "risk_handler" not in url and "验证" not in title:
                body = page.locator("body").inner_text(timeout=3000) or ""
                if "京东" in title and len(body) > 500:
                    print("✅ 验证已通过，已进入京东正常页面")
                    verified = True
                    break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)

    if not verified:
        print("⏰ 等待超时或验证未完成。请重新运行本脚本再试。")
    else:
        page.wait_for_timeout(2000)
    ctx.close()
    print("浏览器已关闭，验证凭证已保存。现在可以重新执行采集。")
