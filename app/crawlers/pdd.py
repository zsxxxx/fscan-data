"""拼多多采集器：使用通用卡片解析器，兼容移动端搜索页类名变化。"""

from __future__ import annotations

from .generic import GenericCrawler

PDD_CONFIG = {
    "cardSelectors": [
        "div[class*='goods-item']", "div[class*='goodsItem']", "li[class*='goods']",
    ],
    "hrefPatterns": ["goods.html", "goods_id"],
    "titleSelectors": ["[class*='goods-name']", "[class*='goodsName']", "[class*='title']"],
    "priceSelectors": ["[class*='price']"],
    "listPriceSelectors": ["[class*='origin']", "[class*='line']", "del"],
    "shopSelectors": ["[class*='shop']", "[class*='mall']"],
    "salesSelectors": ["[class*='sales']", "[class*='sold']"],
    "promoPattern": "百亿补贴|秒杀|补贴|券|满减|特价|免费",
}


class PDDCrawler(GenericCrawler):
    platform_id = "pdd"
    config = PDD_CONFIG
    id_keys = ("goods_id", "goodsId", "id")
    url_template = "https://mobile.yangkeduo.com/goods.html?goods_id={id}"
