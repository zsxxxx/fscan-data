"""拼多多 / 抖音 / 1688 的通用搜索卡片解析器。

这些平台的页面类名变动频繁，因此采用「卡片选择器 → 链接锚点回退 → 页面 JSON 兜底」
三级策略。各平台差异通过 CONFIG 配置，解析逻辑共用。
"""

from __future__ import annotations

import json
from typing import Any

from .base import BaseCrawler, objects_from_json

# 页面注入数据兜底（与淘宝同一套键名）
INIT_DATA_JS = r"""() => {
  const keys = ['__INIT_DATA__', '__INIT_DATA', '__initData', '__pageData', 'pageData', '__NEXT_DATA__'];
  for (const k of keys) {
    if (window[k]) {
      try { return JSON.parse(JSON.stringify(window[k])); } catch (e) { return null; }
    }
  }
  const scripts = document.querySelectorAll('script[type="application/json"]');
  for (const s of scripts) {
    try { return JSON.parse(s.textContent || ''); } catch (e) { /* 继续 */ }
  }
  return null;
}"""

_JS_BODY = r"""() => {
  const cfg = __CFG__;
  const text = (el) => {
    if (!el) return '';
    return (el.getAttribute('title') || el.innerText || el.textContent || '')
      .replace(/\s+/g, ' ').trim();
  };
  const pickText = (root, selectors) => {
    for (const s of selectors) {
      const t = text(root.querySelector(s));
      if (t) return t;
    }
    return '';
  };
  const pickImage = (root) => {
    const img = root.querySelector('img');
    if (!img) return '';
    return img.getAttribute('src') || img.getAttribute('data-src') ||
      img.getAttribute('data-lazy-src') || '';
  };
  const priceFrom = (root, selectors) => {
    for (const s of selectors) {
      const el = root.querySelector(s);
      if (!el) continue;
      const m = text(el).match(/[\d,]+(?:\.\d{1,2})?/);
      if (m) return m[0].replace(/,/g, '');
    }
    return '';
  };
  const leafs = (root) => Array.from(root.querySelectorAll('span,div,a,p'))
    .filter((e) => e.children.length === 0 && text(e).length < 80);
  const matchLeaf = (root, pattern) => {
    const re = new RegExp(pattern);
    const el = leafs(root).find((e) => re.test(text(e)));
    return el ? text(el) : '';
  };
  const anchorOf = (root) => {
    const anchors = Array.from(root.querySelectorAll('a[href]'));
    return anchors.find((a) => cfg.hrefPatterns.some((p) => a.href.includes(p))) ||
      (cfg.hrefPatterns.some((p) => root.href && root.href.includes(p)) ? root : null);
  };

  let nodes = cfg.cardSelectors.length
    ? Array.from(document.querySelectorAll(cfg.cardSelectors.join(',')))
    : [];
  if (!nodes.length) {
    const anchors = Array.from(document.querySelectorAll('a[href]'))
      .filter((a) => cfg.hrefPatterns.some((p) => a.href.includes(p)));
    const seen = new Set();
    nodes = [];
    for (const a of anchors) {
      let node = a;
      for (let i = 0; i < 5 && node.parentElement; i++) {
        const parent = node.parentElement;
        const linkCount = parent.querySelectorAll('a[href]').length;
        node = parent;
        if (linkCount <= 3 && parent.querySelector('img')) break;
      }
      if (!seen.has(node)) {
        seen.add(node);
        nodes.push(node);
      }
    }
  }

  const out = [];
  const used = new Set();
  for (const node of nodes) {
    const a = anchorOf(node);
    const url = a ? a.href : '';
    const title = pickText(node, cfg.titleSelectors) || (a ? text(a) : '');
    if (!title || title.length < 4 || used.has(url || title)) continue;
    used.add(url || title);
    let sku = '';
    const m = url.match(/[?&](?:goods_id|goodsId|offerId|item_id|itemId|id)=(\d{4,})/) ||
      url.match(/\/(\d{6,})(?:[./?#]|$)/);
    if (m) sku = m[1];
    out.push({
      title,
      sku,
      url,
      image: pickImage(node),
      price: priceFrom(node, cfg.priceSelectors),
      list_price: priceFrom(node, cfg.listPriceSelectors),
      promo_label: matchLeaf(node, cfg.promoPattern),
      coupon: matchLeaf(node, '券'),
      stock: matchLeaf(node, '仅剩|库存|现货|售罄'),
      shop: pickText(node, cfg.shopSelectors) || matchLeaf(node, '店|工厂|供应商'),
      sales: pickText(node, cfg.salesSelectors) || matchLeaf(node, '已售|销量|人付款|人收货')
    });
  }
  return out.filter((x) => x.title);
}"""


def build_items_js(config: dict[str, Any]) -> str:
    """把平台配置注入通用 JS，返回可传给 page.evaluate 的表达式。"""
    return _JS_BODY.replace("__CFG__", json.dumps(config, ensure_ascii=False))


class GenericCrawler(BaseCrawler):
    """平台通用适配器：通过类属性配置选择器，无需重复解析代码。"""

    platform_id: str = ""
    config: dict[str, Any] = {}
    id_keys: tuple[str, ...] = ("goods_id", "goodsId", "offerId", "item_id", "id")
    url_template: str = ""

    def prepare(self, page) -> None:
        page.wait_for_timeout(3500)
        selectors = self.config.get("cardSelectors") or []
        if selectors:
            try:
                page.wait_for_selector(selectors[0], timeout=12000)
            except Exception:  # noqa: BLE001 - 选择器改版时继续走锚点回退
                pass
        for _ in range(2):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(800)

    def extract(self, page) -> list[dict]:
        items = page.evaluate(build_items_js(self.config)) or []
        if items:
            return items

        data = page.evaluate(INIT_DATA_JS)
        if not data:
            return []
        out: list[dict] = []
        for obj in objects_from_json(data):
            title = str(
                obj.get("goods_name") or obj.get("goodsName") or obj.get("title")
                or obj.get("raw_title") or obj.get("name") or ""
            )
            if not title:
                continue
            item_id = next((obj[k] for k in self.id_keys if obj.get(k)), "")
            url = str(obj.get("url") or "")
            if not url and item_id and self.url_template:
                url = self.url_template.format(id=item_id)
            out.append(
                {
                    "title": title,
                    "sku": str(item_id),
                    "url": url,
                    "image": str(obj.get("image") or obj.get("pic_url") or obj.get("img") or ""),
                    "price": str(obj.get("price") or obj.get("minPrice") or obj.get("priceInt") or ""),
                    "list_price": str(obj.get("originalPrice") or obj.get("marketPrice") or ""),
                    "promo_label": str(obj.get("promo_label") or obj.get("activity") or ""),
                    "coupon": str(obj.get("coupon") or ""),
                    "stock": str(obj.get("stock") or ""),
                    "shop": str(obj.get("shop") or obj.get("nick") or obj.get("company") or ""),
                    "sales": str(obj.get("sales") or obj.get("salesTip") or ""),
                }
            )
        return out
