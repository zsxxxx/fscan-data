"""京东搜索结果采集。

京东搜索页 2026 年改版后使用 div[data-sku].plugin_goodsCardWrapper 卡片，
价格拆成整数/小数多个节点，商品链接由 SKU 构造。旧版 #J_goodsList 保留兼容。
"""

from __future__ import annotations

from .base import BaseCrawler

# 新版卡片：data-sku + plugin_goodsCardWrapper
JD_NEW_ITEMS_JS = r"""() => {
  const cards = Array.from(document.querySelectorAll('div[data-sku].plugin_goodsCardWrapper'));
  return cards.map((card) => {
    const sku = card.getAttribute('data-sku') || '';
    const titleEl = card.querySelector('span[title]') || card.querySelector('[class*="goods_title"] span');
    const title = (titleEl?.getAttribute('title') || titleEl?.textContent || '').trim();
    const img = card.querySelector('img[src*="360buyimg"], img[data-src*="360buyimg"]') || card.querySelector('img');

    // 价格：统一从 [class*="_price_"] 容器提取，整数 + 可选小数
    let price = '';
    const priceBox = card.querySelector('[class*="_price_"]');
    if (priceBox) {
      const intEl = Array.from(priceBox.querySelectorAll('*')).find(e => e.children.length === 0 && /^\d+$/.test((e.textContent||'').trim()) && !(e.className||'').includes('decimal'));
      const decEl = priceBox.querySelector('[class*="_decimal_"]');
      if (intEl) {
        price = intEl.textContent.trim() + (decEl ? '.' + (decEl.textContent||'').trim() : '');
      } else {
        price = (priceBox.innerText || '').replace(/[^\d.]/g, '');
      }
    }

    // 销量：包含「已售」「人已买」的叶子节点
    let sales = '';
    const leafs = Array.from(card.querySelectorAll('span,div')).filter(e => e.children.length === 0);
    const salesEl = leafs.find(e => /已售|人已买|已购买/.test(e.textContent || ''));
    if (salesEl) sales = (salesEl.textContent || '').trim();

    // 店铺：包含「店」「旗舰店」的叶子节点
    let shop = '';
    const shopEl = leafs.find(e => /店$|旗舰店|自营/.test((e.textContent || '').trim()) && (e.textContent || '').trim().length < 30);
    if (shopEl) shop = (shopEl.textContent || '').trim();

    // 优惠标签：拼接所有含促销关键词的叶子文本
    const promoKeywords = /优惠券|券后|满减|补贴|秒杀|限时|直降|特价|降价|0元|免费|到手价|新品|包邮|赠品|立减/;
    const promoTexts = leafs.filter(e => promoKeywords.test(e.textContent || '')).map(e => (e.textContent||'').trim());
    const promo_label = Array.from(new Set(promoTexts)).join(' ').slice(0, 120);

    // 划线价：含「¥」且被划线的价格（通常 class 含 gray/original/line）
    let list_price = '';
    const lineEl = card.querySelector('[class*="_gray_"], [class*="original"], [class*="line"]');
    if (lineEl) {
      const m = (lineEl.textContent || '').match(/[\d.]+/);
      if (m) list_price = m[0];
    }

    // 库存提示
    let stock = '';
    const stockEl = leafs.find(e => /仅剩|库存|有货|现货|无货|售罄/.test(e.textContent || ''));
    if (stockEl) stock = (stockEl.textContent || '').trim();

    // 优惠券文本
    let coupon = '';
    const couponEl = leafs.find(e => /券/.test(e.textContent || '') && (e.textContent||'').trim().length < 30);
    if (couponEl) coupon = (couponEl.textContent || '').trim();

    return {
      title,
      sku,
      url: sku ? 'https://item.jd.com/' + sku + '.html' : '',
      image: img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '',
      price,
      list_price,
      promo_label,
      coupon,
      stock,
      shop,
      sales,
      specs: sku ? 'SKU:' + sku : ''
    };
  }).filter(x => x.title);
}"""

# 旧版卡片兼容
JD_OLD_ITEMS_JS = r"""() => {
  const items = Array.from(document.querySelectorAll('#J_goodsList li.gl-item'));
  return items.map((li) => {
    const a = li.querySelector('.p-name a') || li.querySelector('a[href*="item.jd.com"]');
    const img = li.querySelector('.p-img img');
    const priceEl = li.querySelector('.p-price i') || li.querySelector('.p-price strong');
    const shopEl = li.querySelector('.p-shop a') || li.querySelector('.curr-shop') || li.querySelector('.p-shop span');
    const salesEl = li.querySelector('.p-commit a') || li.querySelector('.p-commit strong') || li.querySelector('.p-commit');
    return {
      title: a ? (a.innerText || a.textContent || '').trim() : (li.querySelector('.p-name')?.innerText || '').trim(),
      url: a ? a.href : '',
      image: img ? (img.getAttribute('data-lazy-img') || img.getAttribute('data-src') || img.getAttribute('src') || '') : '',
      price: priceEl ? priceEl.innerText.trim() : '',
      shop: shopEl ? shopEl.innerText.trim() : '',
      sales: salesEl ? salesEl.innerText.trim() : ''
    };
  }).filter(x => x.title);
}"""


class JDCrawler(BaseCrawler):
    platform_id = "jd"

    def prepare(self, page) -> None:
        page.wait_for_timeout(4000)
        try:
            page.wait_for_selector(
                "#J_goodsList li.gl-item, div[data-sku].plugin_goodsCardWrapper",
                timeout=15000,
            )
        except Exception:  # noqa: BLE001 - 页面结构变化或反爬时继续尝试解析
            pass
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)

    def extract(self, page) -> list[dict]:
        items = page.evaluate(JD_NEW_ITEMS_JS) or []
        if not items:
            items = page.evaluate(JD_OLD_ITEMS_JS) or []
        return items
