"""平台 ID 到采集器的映射。未注册的平台返回 None，由 API 明确提示「适配器开发中」。"""

from __future__ import annotations

from ..config import Platform
from .base import BaseCrawler
from .jd import JDCrawler
from .taobao import TaobaoCrawler

REGISTRY: dict[str, type[BaseCrawler]] = {
    "taobao": TaobaoCrawler,
    "jd": JDCrawler,
}


def get_crawler(platform: Platform) -> BaseCrawler | None:
    crawler_cls = REGISTRY.get(platform.id)
    return crawler_cls(platform) if crawler_cls else None
