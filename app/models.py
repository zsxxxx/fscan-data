"""API 数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    platform: str = Field(..., description="平台 ID")
    keyword: str = Field(..., min_length=1, description="搜索关键词")
    specs: str = Field("", description="规格/要求，多个词用空格或逗号分隔，需全部命中")
    min_price: Optional[float] = Field(None, ge=0, description="最低价（兼容旧调用）")
    max_price: Optional[float] = Field(None, ge=0, description="最高价（兼容旧调用）")
    filters: dict[str, str] = Field(default_factory=dict, description="平台专属搜索条件")
    pages: int = Field(1, ge=1, description="抓取页数（不设上限）")
    max_results: int = Field(100, ge=0, description="最多返回条数，默认 100，0 表示不限制")


class Product(BaseModel):
    """商品数据：基础信息 + 价格体系 + 优惠信息。"""

    platform: str
    platform_name: str
    rank: int = 0
    sku: str = ""
    title: str
    price: str = ""                      # 当前展示价（原价/标价）
    price_value: Optional[float] = None  # 当前价数值

    # ---- 价格体系 ----
    list_price: Optional[float] = None   # 划线价 / 日常价
    promo_price: Optional[float] = None  # 优惠后到手价
    history_low: Optional[float] = None  # 历史最低价（本地采集库存）
    history_high: Optional[float] = None
    discount_amount: Optional[float] = None   # 优惠金额
    discount_rate: Optional[float] = None     # 折扣力度（0-1）

    # ---- 库存与销量 ----
    stock: str = ""                      # 库存文本（如"有货"/"仅剩3件"）
    stock_count: Optional[int] = None    # 库存数（可解析时）
    sales: str = ""

    # ---- 优惠信息 ----
    promo_type: str = ""                 # 降价/促销/优惠券/0元试用/0元购
    promo_label: str = ""                # 优惠标签原文
    coupon: str = ""                     # 优惠券信息
    is_free_trial: bool = False          # 0元试用
    is_free_buy: bool = False            # 0元购

    image: str = ""
    url: str = ""
    shop: str = ""
    specs: str = ""
    keyword: str = ""
    run_id: str = ""                     # 所属采集批次
    source: str = "dom"                  # 解析来源：dom / json / fallback
    crawled_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


class SearchResponse(BaseModel):
    platform: str
    platform_name: str
    keyword: str
    specs: str = ""
    products: list[Product] = Field(default_factory=list)
    total: int = 0
    filtered: bool = True
    message: str = ""
    elapsed: float = 0.0
    run_id: str = ""
    stale: bool = False                  # 限流期间返回的缓存结果
    detail_fallback: bool = False        # 搜索页限流后改用详情页直采


class PlatformStatus(BaseModel):
    id: str
    name: str
    color: str
    enabled: bool
    note: str = ""
    logged_in: bool = False
    profile_exists: bool = False
    detail: str = ""
    login_task: dict = Field(default_factory=dict)


# ------------------------------------------------------------------ 优惠监控

class MonitorTask(BaseModel):
    """一个优惠监控任务。"""

    id: str
    platform: str
    platform_name: str = ""
    keyword: str
    filters: dict[str, str] = Field(default_factory=dict)
    specs: str = ""
    interval: int = 300            # 轮询间隔（秒），最小 60
    enabled: bool = True
    created_at: str = ""
    last_run_at: str = ""
    run_count: int = 0
    status: str = "idle"           # idle / running / error
    last_error: str = ""


class MonitorTaskCreate(BaseModel):
    platform: str
    keyword: str = Field(..., min_length=1)
    filters: dict[str, str] = Field(default_factory=dict)
    specs: str = ""
    interval: int = Field(300, ge=60, le=86400)
    enabled: bool = True


class DealAlert(BaseModel):
    """优惠告警消息。"""

    id: str
    task_id: str = ""
    platform: str
    platform_name: str = ""
    sku: str = ""
    title: str
    url: str = ""
    image: str = ""
    alert_type: str = ""           # price_drop / promo / coupon / free_trial / free_buy / low_price
    message: str = ""
    old_price: Optional[float] = None
    new_price: Optional[float] = None
    discount: Optional[float] = None
    promo_label: str = ""
    created_at: str = ""


class PriceHistoryPoint(BaseModel):
    """价格历史数据点。"""

    price: float
    promo_price: Optional[float] = None
    captured_at: str
    promo_label: str = ""


# ------------------------------------------------------------------ 采集诊断

class CrawlRun(BaseModel):
    """一次采集批次的元数据。"""

    id: str
    platform: str
    keyword: str
    filters: dict[str, str] = Field(default_factory=dict)
    pages: int = 1
    status: str = ""
    product_count: int = 0
    elapsed: float = 0.0
    proxy: str = ""
    error: str = ""
    stale: bool = False
    started_at: str = ""
    finished_at: str = ""


class ProductObservation(BaseModel):
    """规范化商品观测记录。"""

    id: int = 0
    run_id: str = ""
    platform: str
    sku: str = ""
    title: str = ""
    url: str = ""
    image: str = ""
    shop: str = ""
    price: Optional[float] = None
    promo_price: Optional[float] = None
    list_price: Optional[float] = None
    sales: str = ""
    stock: str = ""
    promo_type: str = ""
    promo_label: str = ""
    coupon: str = ""
    keyword: str = ""
    rank: int = 0
    source: str = "dom"
    captured_at: str = ""
