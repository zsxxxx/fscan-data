"""平台 ID 到采集器的映射。未注册的平台返回 None，由 API 明确提示「适配器开发中」。"""

from __future__ import annotations

from ..config import Platform
from .ali1688 import Ali1688Crawler
from .base import BaseCrawler
from .douyin import DouyinCrawler
from .jd import JDCrawler
from .pdd import PDDCrawler
from .taobao import TaobaoCrawler

REGISTRY: dict[str, type[BaseCrawler]] = {
    "taobao": TaobaoCrawler,
    "jd": JDCrawler,
    "pdd": PDDCrawler,
    "douyin": DouyinCrawler,
    "ali1688": Ali1688Crawler,
}


def get_crawler(platform: Platform) -> BaseCrawler | None:
    crawler_cls = REGISTRY.get(platform.id)
    return crawler_cls(platform) if crawler_cls else None
