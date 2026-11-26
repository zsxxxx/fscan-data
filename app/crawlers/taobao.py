"""淘宝 / 天猫搜索结果采集。

淘宝前端类名经常改版，因此优先通过商品详情链接锚点定位卡片；
若 DOM 解析不到，再回退到页面注入的 JSON 数据（window.__INIT_DATA__ 等）。
"""

from __future__ import annotations

from .base import BaseCrawler, objects_from_json

TAOBAO_ITEMS_JS = r"""() => {
  const out = [];
  const seen = new Set();
  const anchors = Array.from(document.querySelectorAll(
    'a[href*="item.taobao.com/item.htm"], a[href*="detail.tmall.com/item.htm"], ' +
    'a[href*="item.taobao.com"], a[href*="detail.tmall.com"]'
  ));
  for (const a of anchors) {
    const href = a.href;
    if (!href || seen.has(href)) continue;
    let node = a;
    for (let i = 0; i < 6 && node.parentElement; i++) {
      node = node.parentElement;
      const innerLinks = node.querySelectorAll('a[href*="item.taobao.com"], a[href*="detail.tmall.com"]').length;
      if (innerLinks <= 2 && node.querySelectorAll('img').length > 0) break;
    }
    const title = (a.innerText || a.textContent || '').trim() || (node.innerText || '').split('\n')[0].trim();
    if (!title || title.length < 4) continue;
    const priceEl = node.querySelector('[class*="price"]');
    const img = node.querySelector('img');
    const shopEl = node.querySelector('[class*="shop"]');
    const salesEl = node.querySelector('[class*="sales"], [class*="realSales"]');
    seen.add(href);
    out.push({
      title,
      url: href,
      image: img
        ? (img.getAttribute('src') || img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || '')
        : '',
      price: priceEl ? priceEl.innerText.trim() : '',
      shop: shopEl ? shopEl.innerText.trim() : '',
      sales: salesEl ? salesEl.innerText.trim() : ''
    });
  }
  return out;
}"""

INIT_DATA_JS = r"""() => {
  const keys = ['__INIT_DATA__', '__INIT_DATA', '__initData', '__pageData', 'pageData'];
  for (const k of keys) {
    if (window[k]) {
      try { return JSON.parse(JSON.stringify(window[k])); } catch (e) { return null; }
    }
  }
  return null;
}"""


class TaobaoCrawler(BaseCrawler):
    platform_id = "taobao"

    def prepare(self, page) -> None:
        page.wait_for_timeout(4000)
        for _ in range(2):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(900)

    def extract(self, page) -> list[dict]:
        items = page.evaluate(TAOBAO_ITEMS_JS) or []
        if items:
            return items

        data = page.evaluate(INIT_DATA_JS)
        if not data:
            return []
        out: list[dict] = []
        for obj in objects_from_json(data):
            title = obj.get("raw_title") or obj.get("title") or obj.get("name") or ""
            item_id = obj.get("item_id") or obj.get("itemId") or obj.get("id") or ""
            url = (
                f"https://item.taobao.com/item.htm?id={item_id}"
                if item_id and str(item_id).isdigit()
                else (obj.get("url") or "")
            )
            out.append(
                {
                    "title": str(title),
                    "url": str(url),
                    "image": str(obj.get("pic_url") or obj.get("img") or ""),
                    "price": str(obj.get("view_price") or obj.get("price") or obj.get("priceInt") or ""),
                    "shop": str(obj.get("nick") or obj.get("shop") or ""),
                    "sales": str(obj.get("sales") or obj.get("real_sales") or ""),
                    "specs": str(obj.get("props") or ""),
                }
            )
        return out
