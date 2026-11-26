"""平台商品采集适配器。"""

from .base import BaseCrawler
from .jd import JDCrawler
from .taobao import TaobaoCrawler

__all__ = ["BaseCrawler", "JDCrawler", "TaobaoCrawler"]
