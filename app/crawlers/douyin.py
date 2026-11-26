"""抖音采集器：使用通用卡片解析器，兼容搜索页与电商卡片混排。"""

from __future__ import annotations

from .generic import GenericCrawler

DOUYIN_CONFIG = {
    "cardSelectors": [
        "div[class*='search-result-card']", "div[class*='product-card']",
        "div[class*='ecommerce']", "li[class*='product']",
    ],
    "hrefPatterns": ["haohuo.jinritemai.com", "/video/", "douyin.com/item/"],
    "titleSelectors": ["[class*='title']", "[class*='name']", "[class*='desc']"],
    "priceSelectors": ["[class*='price']"],
    "listPriceSelectors": ["[class*='origin']", "[class*='line']", "del"],
    "shopSelectors": ["[class*='shop']", "[class*='author']", "[class*='store']"],
    "salesSelectors": ["[class*='sales']", "[class*='sold']"],
    "promoPattern": "限时|秒杀|补贴|券|满减|特价|9.9|免费",
}


class DouyinCrawler(GenericCrawler):
    platform_id = "douyin"
    config = DOUYIN_CONFIG
    id_keys = ("goods_id", "goodsId", "item_id", "id")
    url_template = "https://www.douyin.com/video/{id}"
