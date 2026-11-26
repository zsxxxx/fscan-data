"""采集器基类与通用清洗工具。"""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import quote, urljoin

from ..config import Platform
from ..models import Product
from .. import store

_PRICE_RE = re.compile(r"(?:¥|￥|\$)?\s*([\d,]+(?:\.\d{1,2})?)")

# 优惠类型识别关键词（按优先级）
PROMO_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("0元购", ("0元购", "零元购", "免费领", "0元抢")),
    ("0元试用", ("0元试用", "免费试用", "试用0元", "免费领用")),
    ("优惠券", ("优惠券", "领券", "券后", "满减", "红包")),
    ("促销", ("秒杀", "限时", "直降", "满减", "补贴", "特价", "拼团", "百亿补贴")),
    ("降价", ("降价", "降", "跌价", "历史低价", "新低")),
)


def detect_promo_type(text: str) -> str:
    """根据文本识别优惠类型。"""
    value = text or ""
    for promo_type, keywords in PROMO_RULES:
        if any(k in value for k in keywords):
            return promo_type
    return ""


def parse_stock(text: str) -> tuple[str, int | None]:
    """解析库存文本，返回 (展示文本, 数量)。"""
    value = clean_text(text)
    if not value:
        return "", None
    match = re.search(r"(\d+)\s*(件|个|件库存|库存)", value)
    count = int(match.group(1)) if match else None
    return value, count


class LoginRequiredError(Exception):
    """平台要求登录后才能查看搜索结果。"""


class RateLimitedError(Exception):
    """平台触发访问频率限制。"""


def clean_text(value: Any) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_price(text: Any) -> tuple[str, float | None]:
    """返回 (展示文本, 数值)。"""
    display = clean_text(text)
    if not display:
        return "", None
    match = _PRICE_RE.search(display)
    if not match:
        return display, None
    try:
        return display, float(match.group(1).replace(",", ""))
    except ValueError:
        return display, None


def abs_url(href: Any, base: str = "") -> str:
    value = clean_text(href)
    if not value:
        return ""
    if value.startswith("//"):
        return "https:" + value
    return urljoin(base, value) if base else value


def looks_like_product(obj: dict) -> bool:
    """粗略判断一个 JSON 对象是否是商品数据。"""
    title_keys = ("title", "raw_title", "name", "itemTitle", "wareName", "productName")
    price_keys = ("price", "view_price", "priceInt", "jdPrice", "salePrice")
    return (
        len(obj) >= 3
        and any(k in obj for k in title_keys)
        and any(k in obj for k in price_keys)
    )


def objects_from_json(data: Any) -> list[dict]:
    """递归扫描页面注入的 JSON，找出疑似商品对象。"""
    found: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if looks_like_product(node):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return found


def dedupe(products: Iterable[Product]) -> list[Product]:
    seen: set[str] = set()
    out: list[Product] = []
    for product in products:
        key = product.url or (product.title, product.price)
        if key in seen:
            continue
        seen.add(key)
        out.append(product)
    return out


class BaseCrawler:
    """所有平台适配器的基类。"""

    platform_id: str = ""

    def __init__(self, platform: Platform):
        self.platform = platform

    def build_url(self, keyword: str, page: int = 1, filters: dict | None = None) -> str:
        return self.platform.build_search_url(quote(keyword), page, filters)

    def prepare(self, page) -> None:
        """页面加载后的等待/滚动逻辑。"""
        page.wait_for_timeout(2500)

    def extract(self, page) -> list[dict]:
        """从当前页面提取原始商品字典。"""
        return []

    def to_product(self, raw: dict, keyword: str, rank: int) -> Product:
        display, value = parse_price(raw.get("price", ""))
        title = clean_text(raw.get("title", ""))
        sku = clean_text(raw.get("sku", ""))
        promo_label = clean_text(raw.get("promo_label", ""))
        promo_text = f"{promo_label} {clean_text(raw.get('promo', ''))}"

        # 划线价（原价）
        list_price = None
        if raw.get("list_price"):
            _, list_price = parse_price(raw["list_price"])

        # 优惠后价：京东搜索页展示的当前价本身就是到手价（补贴价/券后价）。
        # 因此当当前价低于划线价时，到手价 = 当前价。
        promo_price = None
        if raw.get("promo_price"):
            _, explicit = parse_price(raw["promo_price"])
            if explicit is not None:
                promo_price = explicit
        if (
            promo_price is None
            and list_price is not None
            and value is not None
            and 0 < value < list_price
        ):
            promo_price = value

        # 兜底：仅从"券后价/到手价/补贴价¥XX"这种紧邻格式提取，且必须不高于当前价
        if promo_price is None and value is not None and promo_text:
            m = re.search(r"(?:券后价?|到手价?|补贴价)[￥¥]?\s*(\d+(?:\.\d{1,2})?)", promo_text)
            if m:
                candidate = float(m.group(1))
                if 0 < candidate <= value:
                    promo_price = candidate

        # 合理性校验：优惠后价不得高于当前价，否则视为解析错误
        if promo_price is not None and value is not None and promo_price > value:
            promo_price = None

        effective = promo_price if promo_price is not None else value
        discount_amount = None
        discount_rate = None
        if effective is not None and list_price is not None and list_price > 0:
            discount_amount = round(list_price - effective, 2)
            discount_rate = round((list_price - effective) / list_price, 4)

        # 库存
        stock_text, stock_count = parse_stock(str(raw.get("stock", "")))

        # 优惠类型
        promo_type = detect_promo_type(promo_text)
        coupon = clean_text(raw.get("coupon", ""))
        if coupon and not promo_type:
            promo_type = "优惠券"

        # 历史价格（本地采集库）
        history_low = None
        history_high = None
        if sku:
            price_range = store.get_price_range(self.platform.id, sku)
            history_low = price_range["low"]
            history_high = price_range["high"]
            store.record_price(
                platform=self.platform.id,
                sku=sku,
                title=title,
                price=value,
                promo_price=promo_price,
                stock=stock_text,
                promo_label=promo_label,
                url=abs_url(raw.get("url", "")),
            )

        return Product(
            platform=self.platform.id,
            platform_name=self.platform.name,
            rank=rank,
            sku=sku,
            title=title,
            price=display,
            price_value=value,
            list_price=list_price,
            promo_price=promo_price,
            history_low=history_low,
            history_high=history_high,
            discount_amount=discount_amount,
            discount_rate=discount_rate,
            stock=stock_text,
            stock_count=stock_count,
            sales=clean_text(raw.get("sales", "")),
            promo_type=promo_type,
            promo_label=promo_label,
            coupon=coupon,
            is_free_trial="0元试用" in promo_text or "免费试用" in promo_text,
            is_free_buy="0元购" in promo_text or "零元购" in promo_text,
            image=abs_url(raw.get("image", "")),
            url=abs_url(raw.get("url", "")),
            shop=clean_text(raw.get("shop", "")),
            specs=clean_text(raw.get("specs", "")),
            keyword=keyword,
        )

    @staticmethod
    def _login_required(page) -> bool:
        """识别被平台跳转到登录页或搜索结果要求登录的情况。"""
        url = (page.url or "").lower()
        if any(marker in url for marker in ("passport.jd.com", "login.taobao.com", "login.1688.com")):
            return True
        try:
            body = page.locator("body").inner_text(timeout=3000) or ""
        except Exception:  # noqa: BLE001
            return False
        return "亲，请登录" in body or "请先登录" in body

    @staticmethod
    def _rate_limited(page) -> bool:
        """识别平台访问频繁/验证码拦截。"""
        try:
            body = page.locator("body").inner_text(timeout=3000) or ""
        except Exception:  # noqa: BLE001
            return False
        return any(
            marker in body
            for marker in ("访问频繁", "无法搜索，请稍后", "请输入验证码", "滑块验证")
        )

    def crawl(self, page, keyword: str, pages: int = 1, filters: dict | None = None) -> list[Product]:
        products: list[Product] = []
        for page_no in range(1, pages + 1):
            if page_no > 1:
                page.wait_for_timeout(3000)  # 翻页间隔，降低触发限流概率
            page.goto(self.build_url(keyword, page_no, filters), wait_until="domcontentloaded", timeout=60000)
            self.prepare(page)
            if self._login_required(page):
                raise LoginRequiredError(
                    f"{self.platform.name} 需要登录才能查看搜索结果，请先点击「提前登录」"
                )
            if self._rate_limited(page):
                raise RateLimitedError(
                    f"{self.platform.name} 检测到访问频繁，已被临时限流，请稍等几分钟再重试"
                )
            raw_items = self.extract(page)
            for raw in raw_items:
                product = self.to_product(raw, keyword, len(products) + 1)
                if product.title:
                    products.append(product)
        return dedupe(products)
