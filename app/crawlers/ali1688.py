"""1688 采集器：使用通用卡片解析器，兼容 offer 搜索页类名变化。"""

from __future__ import annotations

from .generic import GenericCrawler

ALI1688_CONFIG = {
    "cardSelectors": [
        "div[class*='offer']", "div[class*='item']", "li[class*='item']",
    ],
    "hrefPatterns": ["detail.1688.com/offer", "offer/"],
    "titleSelectors": ["[class*='title']", "[class*='subject']", "[class*='name']"],
    "priceSelectors": ["[class*='price']"],
    "listPriceSelectors": ["[class*='origin']", "[class*='line']", "del"],
    "shopSelectors": ["[class*='company']", "[class*='supplier']", "[class*='shop']"],
    "salesSelectors": ["[class*='sold']", "[class*='sale']", "[class*='sales']"],
    "promoPattern": "促销|包邮|优惠券|满减|特价|一件代发|限时",
}


class Ali1688Crawler(GenericCrawler):
    platform_id = "ali1688"
    config = ALI1688_CONFIG
    id_keys = ("offer_id", "offerId", "id", "item_id")
    url_template = "https://detail.1688.com/offer/{id}.html"
