"""平台注册表与全局路径配置。

新增平台只需在此追加一个 Platform 定义，并在 crawlers/registry.py 注册适配器。
每个平台可定义自己的搜索条件（search_fields），前端据此动态渲染表单。
筛选条件全部为「选择式」：select 单选 / multiselect 下拉多选 / cascade 两级联动分类。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROFILES_DIR = DATA_DIR / "profiles"
HISTORY_FILE = DATA_DIR / "searches.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"


@dataclass(frozen=True)
class SearchField:
    """平台专属搜索条件定义（全部选择式，不手输）。"""

    key: str
    label: str
    type: str = "select"  # select | multiselect | cascade
    placeholder: str = ""
    options: tuple[tuple[str, str], ...] = ()
    default: str = ""
    # cascade 两级联动：一级 options + 二级映射（一级值 -> 二级选项）
    cascade: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)

    def payload(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "placeholder": self.placeholder,
            "options": [{"value": value, "label": label} for value, label in self.options],
            "default": self.default,
            "cascade": {
                k: [{"value": v, "label": lbl} for v, lbl in opts]
                for k, opts in self.cascade.items()
            } if self.cascade else {},
        }


@dataclass(frozen=True)
class Platform:
    """一个电商平台的接入定义。"""

    id: str
    name: str
    color: str
    home_url: str
    login_url: str
    search_url: str
    fallback_search_url: str = ""       # 主页面改版/风控时的备用搜索入口
    detail_url_template: str = ""       # SKU 详情页直采入口，绕开搜索页限流
    login_selectors: tuple[str, ...] = ()
    enabled: bool = True
    note: str = ""
    search_fields: tuple[SearchField, ...] = ()
    # 字段 key -> 搜索 URL 参数名；value_maps 用于把前端选项值转成平台参数值
    url_params: dict[str, str] = field(default_factory=dict)
    value_maps: dict[str, dict[str, str]] = field(default_factory=dict)
    # 优惠专区入口：(名称, 类型标签, URL)
    promo_zones: tuple[tuple[str, str, str], ...] = ()

    def profile_dir(self) -> Path:
        return PROFILES_DIR / self.id

    def build_search_url(
        self, keyword: str, page: int = 1, filters: dict | None = None, url_template: str = ""
    ) -> str:
        # 分类（取最具体的一级或二级）+ 品牌作为关键词附加词，让平台检索更精准
        extra_terms: list[str] = []
        flt = filters or {}
        sub = str(flt.get("category_sub", "")).strip()
        cat = str(flt.get("category", "")).strip()
        if sub:
            extra_terms.append(sub)
        elif cat:
            extra_terms.append(cat)
        brand = flt.get("brand")
        if brand:
            extra_terms.extend(t.strip() for t in str(brand).split(",") if t.strip())
        # 通用附加条件作为关键词补充，避免平台参数差异导致筛选失效
        # 多选值为逗号分隔，逐项拆成独立关键词
        for key in ("promo_type", "ship_from"):
            value = str(flt.get(key, "")).strip()
            for term in value.split(","):
                term = term.strip()
                if term and term not in extra_terms:
                    extra_terms.append(term)
        q = keyword
        if extra_terms:
            q = keyword + "%20" + "%20".join(quote(t, safe="") for t in extra_terms)
        template = url_template or self.search_url
        url = template.format(q=q, page=page)
        pairs: list[str] = []
        for key, param in self.url_params.items():
            value = flt.get(key)
            if value in (None, ""):
                continue
            mapped_values = [
                self.value_maps.get(key, {}).get(str(v), str(v))
                for v in str(value).split(",")
                if v != ""
            ]
            if not mapped_values:
                continue
            pairs.append(f"{param}={quote(','.join(mapped_values), safe='')}")
        if pairs:
            url += ("&" if "?" in url else "?") + "&".join(pairs)
        return url

    def payload(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "enabled": self.enabled,
            "note": self.note,
            "home_url": self.home_url,
            "login_url": self.login_url,
            "search_fields": [f.payload() for f in self.search_fields],
            "promo_zones": [
                {"name": name, "type": ptype, "url": url}
                for name, ptype, url in self.promo_zones
            ],
        }


# ---- 通用选择式字段 ----
PRICE_RANGE_FIELD = SearchField(
    "price_range", "价格区间", "select", options=(
        ("", "不限价格"),
        ("0-50", "¥0 - ¥50"),
        ("50-100", "¥50 - ¥100"),
        ("100-300", "¥100 - ¥300"),
        ("300-500", "¥300 - ¥500"),
        ("500-1000", "¥500 - ¥1000"),
        ("1000-3000", "¥1000 - ¥3000"),
        ("3000-5000", "¥3000 - ¥5000"),
        ("5000-10000", "¥5000 - ¥10000"),
        ("10000-", "¥10000 以上"),
        ("custom", "自定义价格区间"),
    ),
)
SORT_DEFAULT = (
    ("", "综合排序"),
    ("sales_desc", "销量优先"),
    ("price_asc", "价格从低到高"),
    ("price_desc", "价格从高到低"),
)

def _brand_pairs(*names: str) -> tuple[tuple[str, str], ...]:
    """品牌去重并保持顺序，供下拉多选使用。"""
    return tuple((name, name) for name in dict.fromkeys(names))


# 覆盖数码、电脑、家电、美妆、服饰、食品、母婴、家居、汽车、图书等常见品牌。
# 品牌是开放集合，后续可继续向此列表补充；前端下拉支持滚动勾选。
BRAND_OPTIONS = _brand_pairs(
    # 手机 / 数码 / 影音
    "苹果", "Apple", "华为", "Huawei", "小米", "Xiaomi", "红米", "Redmi", "荣耀", "Honor",
    "OPPO", "vivo", "一加", "OnePlus", "realme", "魅族", "Meizu", "中兴", "ZTE", "努比亚",
    "nubia", "三星", "Samsung", "诺基亚", "Nokia", "摩托罗拉", "Motorola", "谷歌", "Google",
    "索尼", "Sony", "佳能", "Canon", "尼康", "Nikon", "富士", "Fujifilm", "松下", "Panasonic",
    "大疆", "DJI", "漫步者", "Edifier", "Bose", "JBL", "森海塞尔", "Sennheiser", "铁三角",
    "Audio-Technica", "Beats", "华为FreeBuds", "AirPods", "Apple Watch", "小米手环",
    # 电脑 / 办公 / 外设
    "联想", "Lenovo", "ThinkPad", "戴尔", "Dell", "惠普", "HP", "华硕", "ASUS", "宏碁", "Acer",
    "神舟", "Hasee", "机械革命", "Mechrevo", "雷蛇", "Razer", "微软", "Microsoft", "罗技",
    "Logitech", "雷柏", "Rapoo", "双飞燕", "A4Tech", "LG", "飞利浦", "Philips", "明基", "BenQ",
    "爱普生", "Epson", "兄弟", "Brother", "方正", "清华同方", "长城", "浪潮", "华为MateBook",
    "荣耀MagicBook", "红米RedmiBook", "华硕ROG", "微星", "MSI", "技嘉", "Gigabyte", "七彩虹",
    "影驰", "索泰", "蓝宝石", "迪兰", "海盗船", "Corsair", "金士顿", "Kingston", "芝奇",
    "G.Skill", "西部数据", "WD", "希捷", "Seagate", "三星SSD", "长江存储", "致态", "铠侠",
    # 家电 / 厨电 / 生活电器
    "海尔", "Haier", "美的", "Midea", "格力", "Gree", "海信", "Hisense", "TCL", "长虹",
    "Changhong", "创维", "Skyworth", "康佳", "Konka", "西门子", "Siemens", "博世", "Bosch",
    "夏普", "Sharp", "戴森", "Dyson", "云米", "Viomi", "方太", "Fotile", "老板", "Robam",
    "华帝", "Vatti", "帅康", "樱花", "九阳", "Joyoung", "苏泊尔", "Supor", "格兰仕", "Galanz",
    "欧姆龙", "Omron", "科沃斯", "Ecovacs", "石头", "Roborock", "添可", "Tineco", "小熊",
    "德尔玛", "北鼎", "膳魔师", "象印", "虎牌", "松下家电", "米家", "智米", "云鲸", "追觅",
    "Dreame", "莱克", "小狗吸尘器", "飞利浦家电", "夏普家电",
    # 美妆 / 个护 / 洗护
    "欧莱雅", "L'Oreal", "雅诗兰黛", "Estee Lauder", "兰蔻", "Lancome", "资生堂", "Shiseido",
    "SK-II", "海蓝之谜", "La Mer", "赫莲娜", "HR", "迪奥", "Dior", "香奈儿", "Chanel",
    "圣罗兰", "YSL", "阿玛尼", "Armani", "珀莱雅", "自然堂", "百雀羚", "薇诺娜", "完美日记",
    "花西子", "毛戈平", "高夫", "妮维雅", "Nivea", "曼秀雷敦", "多芬", "Dove", "潘婷",
    "Pantene", "海飞丝", "Head & Shoulders", "清扬", "Clear", "沙宣", "Sassoon", "云南白药",
    "高露洁", "Colgate", "佳洁士", "Crest", "舒适达", "Sensodyne", "李施德林", "Listerine",
    "欧舒丹", "L'Occitane", "祖玛珑", "Jo Malone", "宝格丽", "Bvlgari", "古驰", "Gucci",
    "普拉达", "Prada", "爱马仕", "Hermes", "巴黎世家", "Balenciaga", "纪梵希", "Givenchy",
    "范思哲", "Versace", "杜嘉班纳", "DOLCE&GABBANA", "Tom Ford", "TF", "阿玛尼美妆",
    "MAC", "魅可", "NARS", "丝芙兰", "Sephora", "屈臣氏", "名创优品", "MINISO",
    # 服饰 / 鞋包 / 箱包
    "耐克", "Nike", "阿迪达斯", "Adidas", "新百伦", "New Balance", "安踏", "Anta", "李宁",
    "Li-Ning", "特步", "Xtep", "匹克", "Peak", "361度", "乔丹", "中国乔丹", "斐乐", "FILA",
    "彪马", "Puma", "匡威", "Converse", "万斯", "Vans", "亚瑟士", "ASICS", "斯凯奇",
    "Skechers", "哥伦比亚", "Columbia", "北面", "The North Face", "始祖鸟", "Arc'teryx",
    "迪卡侬", "Decathlon", "优衣库", "Uniqlo", "海澜之家", "HLA", "太平鸟", "Peacebird",
    "波司登", "Bosideng", "雅戈尔", "Youngor", "七匹狼", "劲霸", "柒牌", "罗蒙", "报喜鸟",
    "森马", "Semir", "美特斯邦威", "Metersbonwe", "以纯", "真维斯", "班尼路", "卡宾", "GXG",
    "杰克琼斯", "Jack & Jones", "ONLY", "VERO MODA", "艾格", "拉夏贝尔", "歌莉娅", "欧时力",
    "茵曼", "裂帛", "韩都衣舍", "三彩", "秋水伊人", "百丽", "Belle", "他她", "Tata", "天美意",
    "思加图", "星期六", "达芙妮", "骆驼", "Camel", "木林森", "红蜻蜓", "奥康", "意尔康",
    "金利来", "Goldlion", "皮尔卡丹", "Pierre Cardin", "梦特娇", "路易威登", "LV", "巴宝莉",
    "Burberry", "蔻驰", "Coach", "迈克科尔斯", "Michael Kors", "Tory Burch", "菲拉格慕",
    "Ferragamo", "卡地亚", "Cartier", "劳力士", "Rolex", "欧米茄", "Omega", "浪琴",
    "Longines", "天梭", "Tissot", "西铁城", "Citizen", "卡西欧", "Casio", "精工", "Seiko",
    "新秀丽", "Samsonite", "外交官", "Diplomat", "海关", "小米箱包",
    # 食品 / 生鲜 / 酒水
    "三只松鼠", "百草味", "良品铺子", "洽洽", "卫龙", "海底捞", "蒙牛", "伊利", "光明",
    "三元", "雀巢", "Nestle", "达能", "Danone", "可口可乐", "Coca-Cola", "百事可乐",
    "Pepsi", "农夫山泉", "娃哈哈", "康师傅", "统一", "今麦郎", "五粮液", "茅台", "泸州老窖",
    "剑南春", "洋河", "汾酒", "青岛啤酒", "雪花啤酒", "燕京啤酒", "金龙鱼", "福临门",
    "鲁花", "海天", "李锦记", "太太乐", "好丽友", "Orion", "亿滋", "Mondelez", "玛氏",
    "Mars", "德芙", "士力架", "Snickers", "费列罗", "Ferrero", "好时", "Hershey's", "乐事",
    "Lay's", "品客", "Pringles", "旺旺", "徐福记", "阿尔卑斯", "曼妥思", "Mentos", "绿箭",
    "益达", "麦德龙", "山姆", "Costco", "盒马", "永辉", "大润发", "家家悦", "胖东来",
    # 母婴 / 玩具 / 乐器
    "帮宝适", "Pampers", "好奇", "Huggies", "花王", "Kao", "尤妮佳", "Unicharm", "美赞臣",
    "Enfamil", "雅培", "Abbott", "惠氏", "Wyeth", "飞鹤", "金领冠", "君乐宝", "合生元",
    "澳优", "a2", "爱他美", "Aptamil", "诺优能", "美素佳儿", "Friso", "佳贝艾特", "Kabrita",
    "巴布豆", "巴拉巴拉", "Balabala", "安踏儿童", "李宁儿童", "耐克儿童", "阿迪达斯儿童",
    "乐高", "LEGO", "美泰", "Mattel", "孩之宝", "Hasbro", "迪士尼", "Disney", "三丽鸥",
    "Sanrio", "万代", "Bandai", "奥迪双钻", "布鲁可", "费雪", "Fisher-Price", "澳贝",
    "好孩子", "Goodbaby", "小龙哈彼", "贝亲", "Pigeon", "新安怡", "Avent", "美德乐",
    "Medela", "雅马哈", "Yamaha", "卡西欧乐器", "珠江钢琴", "星海钢琴", "罗兰", "Roland",
    # 家居 / 建材 / 灯饰
    "宜家", "IKEA", "顾家家居", "全友家居", "欧派", "OPPEIN", "索菲亚", "尚品宅配", "志邦",
    "金牌厨柜", "好莱客", "皮阿诺", "大自然", "圣象", "生活家", "欧普照明", "Opple",
    "雷士照明", "NVC", "公牛", "BULL", "正泰", "CHINT", "德力西", "Delixi", "施耐德",
    "Schneider", "罗格朗", "Legrand", "汉斯格雅", "Hansgrohe", "科勒", "Kohler", "TOTO",
    "恒洁", "箭牌", "九牧", "JOMOO", "惠达", "东鹏", "马可波罗", "诺贝尔", "冠珠",
    "蒙娜丽莎", "欧神诺", "多乐士", "立邦", "三棵树", "晨光", "得力", "齐心", "广博",
    # 汽车 / 出行
    "米其林", "Michelin", "马牌", "Continental", "邓禄普", "Dunlop", "固特异", "Goodyear",
    "倍耐力", "Pirelli", "普利司通", "Bridgestone", "韩泰", "Hankook", "锦湖", "Kumho",
    "壳牌", "Shell", "美孚", "Mobil", "嘉实多", "Castrol", "3M", "龟牌", "Turtle Wax",
    "化学小子", "固特威", "车仆", "途虎", "法雷奥", "Valeo", "德尔福", "Delphi", "盖茨",
    "Gates", "舍弗勒", "Schaeffler", "比亚迪", "特斯拉", "Tesla", "蔚来", "理想", "小鹏",
    # 图书 / 文娱
    "商务印书馆", "中华书局", "人民文学出版社", "中信出版社", "机械工业出版社",
    "电子工业出版社", "清华大学出版社", "北京大学出版社", "三联书店", "上海译文出版社",
    "译林出版社", "作家出版社", "新东方", "得到", "樊登读书",
)

# ---- 五平台通用附加筛选 ----
COMMON_FILTER_FIELDS = (
    SearchField("promo_type", "优惠类型", "multiselect", options=(
        ("优惠券", "优惠券"), ("满减", "满减"), ("秒杀", "秒杀"),
        ("补贴", "补贴"), ("包邮", "包邮"), ("新品", "新品"),
    )),
    SearchField("ship_from", "发货地", "multiselect", options=(
        ("北京", "北京"), ("上海", "上海"), ("广东", "广东"),
        ("浙江", "浙江"), ("江苏", "江苏"), ("福建", "福建"), ("山东", "山东"),
        ("四川", "四川"), ("湖北", "湖北"),
    )),
)

# ---- 京东：参照京东搜索页左侧真实分类树 ----
JD_CATEGORY_TREE = {
    "家用电器": (("电视", "电视"), ("空调", "空调"), ("冰箱", "冰箱"), ("洗衣机", "洗衣机"),
              ("厨卫大电", "厨卫大电"), ("厨房小电", "厨房小电"), ("生活电器", "生活电器"), ("个护健康", "个护健康")),
    "电脑/办公/文具": (("电脑整机", "电脑整机"), ("笔记本", "笔记本"), ("平板电脑", "平板电脑"),
                  ("游戏本", "游戏本"), ("一体机", "一体机"), ("台式机", "台式机"), ("服务器", "服务器"),
                  ("显示器", "显示器"), ("CPU", "CPU"), ("主板", "主板"), ("显卡", "显卡"),
                  ("内存", "内存"), ("硬盘", "硬盘"), ("固态硬盘", "固态硬盘"), ("鼠标", "鼠标"),
                  ("键盘", "键盘"), ("键鼠套装", "键鼠套装"), ("U盘", "U盘"), ("移动硬盘", "移动硬盘"),
                  ("摄像头", "摄像头"), ("路由器", "路由器"), ("交换机", "交换机"),
                  ("打印机", "打印机"), ("投影仪", "投影仪"), ("复印机", "复印机"),
                  ("扫描仪", "扫描仪"), ("碎纸机", "碎纸机"), ("笔类", "笔类"), ("本册", "本册"), ("纸品", "纸品")),
    "手机/运营商/数码": (("手机", "手机"), ("游戏手机", "游戏手机"), ("老人机", "老人机"),
                   ("儿童手表", "儿童手表"), ("对讲机", "对讲机"), ("摄影摄像", "摄影摄像"),
                   ("数码相机", "数码相机"), ("影音娱乐", "影音娱乐"), ("耳机耳麦", "耳机耳麦"),
                   ("智能设备", "智能设备"), ("智能手表", "智能手表")),
    "厨卫大电/生活电器": (("油烟机", "油烟机"), ("燃气灶", "燃气灶"), ("热水器", "热水器"),
                    ("消毒柜", "消毒柜"), ("洗碗机", "洗碗机"), ("电饭煲", "电饭煲"),
                    ("微波炉", "微波炉"), ("电烤箱", "电烤箱")),
    "食品/酒类/生鲜/特产": (("零食", "零食"), ("坚果炒货", "坚果炒货"), ("饮料冲调", "饮料冲调"),
                     ("粮油调味", "粮油调味"), ("生鲜", "生鲜"), ("水果", "水果"),
                     ("中外名酒", "中外名酒"), ("进口食品", "进口食品")),
    "美妆/个护清洁/宠物": (("面部护肤", "面部护肤"), ("彩妆", "彩妆"), ("香水", "香水"),
                    ("面膜", "面膜"), ("洗发护发", "洗发护发"), ("口腔护理", "口腔护理"),
                    ("身体护理", "身体护理"), ("宠物生活", "宠物生活")),
    "母婴/玩具乐器": (("奶粉", "奶粉"), ("纸尿裤", "纸尿裤"), ("童装童鞋", "童装童鞋"),
                ("玩具", "玩具"), ("乐器", "乐器"), ("孕产用品", "孕产用品"), ("喂养用品", "喂养用品")),
    "家居/家具/家装/厨具": (("家纺", "家纺"), ("生活用品", "生活用品"), ("家具", "家具"),
                     ("灯具", "灯具"), ("厨具", "厨具"), ("家装建材", "家装建材"), ("收纳用品", "收纳用品")),
    "运动/户外": (("运动鞋服", "运动鞋服"), ("健身训练", "健身训练"), ("户外装备", "户外装备"),
             ("自行车", "自行车"), ("垂钓", "垂钓"), ("露营", "露营")),
    "男装/女装/童装/内衣": (("男装", "男装"), ("女装", "女装"), ("童装", "童装"), ("内衣", "内衣"),
                    ("鞋靴", "鞋靴"), ("箱包", "箱包"), ("钟表", "钟表"), ("珠宝首饰", "珠宝首饰")),
    "图书/文娱/教育/电子书": (("文学", "文学"), ("经管励志", "经管励志"), ("科技", "科技"),
                     ("教育", "教育"), ("童书", "童书"), ("电子书", "电子书"), ("文具", "文具")),
    "汽车/汽车用品": (("车载电器", "车载电器"), ("维修保养", "维修保养"),
                 ("汽车装饰", "汽车装饰"), ("安全自驾", "安全自驾")),
    "医药/保健/生物": (("中西药品", "中西药品"), ("营养保健", "营养保健"),
                  ("医疗器械", "医疗器械"), ("隐形眼镜", "隐形眼镜")),
    "京东国际/进口商品": (("进口美妆", "进口美妆"), ("进口母婴", "进口母婴"),
                   ("进口食品", "进口食品"), ("进口数码", "进口数码")),
    "艺术/礼品/鲜花": (("艺术品", "艺术品"), ("礼品", "礼品"),
                 ("鲜花", "鲜花"), ("农资绿植", "农资绿植")),
    "二手/奢侈品/回收": (("二手商品", "二手商品"), ("奢侈品", "奢侈品"), ("回收", "回收")),
    "房产/装修/家政/企业服务": (("房产", "房产"), ("装修", "装修"),
                       ("家政", "家政"), ("企业服务", "企业服务")),
}

# ---- 淘宝：参照淘宝分类体系 ----
TAOBAO_CATEGORY_TREE = {
    "女装/男装/内衣": (("连衣裙", "连衣裙"), ("衬衫", "衬衫"), ("T恤", "T恤"),
                 ("外套", "外套"), ("牛仔裤", "牛仔裤"), ("羽绒服", "羽绒服"), ("内衣", "内衣")),
    "鞋靴/箱包/配饰": (("运动鞋", "运动鞋"), ("单鞋", "单鞋"), ("靴子", "靴子"),
                 ("双肩包", "双肩包"), ("单肩包", "单肩包"), ("钱包", "钱包"), ("皮带", "皮带")),
    "美妆/护肤/香水": (("面部护肤", "面部护肤"), ("彩妆", "彩妆"), ("香水", "香水"),
                 ("面膜", "面膜"), ("洗发护发", "洗发护发"), ("美容工具", "美容工具")),
    "手机/数码/电脑": (("手机", "手机"), ("笔记本", "笔记本"), ("平板电脑", "平板电脑"),
                 ("相机", "相机"), ("耳机", "耳机"), ("智能手表", "智能手表"), ("配件", "配件")),
    "家电/生活电器": (("电视", "电视"), ("空调", "空调"), ("冰箱", "冰箱"),
                 ("洗衣机", "洗衣机"), ("厨房电器", "厨房电器"), ("生活电器", "生活电器")),
    "家居/家装/家纺": (("床品", "床品"), ("家具", "家具"), ("灯具", "灯具"),
                 ("厨具", "厨具"), ("收纳", "收纳"), ("装饰", "装饰")),
    "母婴/玩具/童装": (("奶粉", "奶粉"), ("纸尿裤", "纸尿裤"), ("童装", "童装"),
                 ("玩具", "玩具"), ("孕产", "孕产"), ("童鞋", "童鞋")),
    "食品/生鲜/特产": (("零食", "零食"), ("坚果", "坚果"), ("生鲜", "生鲜"),
                 ("茶叶", "茶叶"), ("粮油", "粮油"), ("进口食品", "进口食品")),
    "运动/户外/ fitness": (("运动鞋服", "运动鞋服"), ("健身器材", "健身器材"),
                       ("户外装备", "户外装备"), ("自行车", "自行车"), ("瑜伽", "瑜伽")),
    "图书/文娱/教育": (("文学", "文学"), ("童书", "童书"), ("教辅", "教辅"),
                 ("经管", "经管"), ("文具", "文具"), ("音像", "音像")),
    "汽车/摩托/配件": (("车载电器", "车载电器"), ("汽车装饰", "汽车装饰"),
                 ("维修保养", "维修保养"), ("轮胎", "轮胎")),
    "宠物/宠物用品": (("狗粮", "狗粮"), ("猫粮", "猫粮"), ("宠物玩具", "宠物玩具"), ("宠物清洁", "宠物清洁")),
}

# ---- 拼多多 ----
PDD_CATEGORY_TREE = {
    "女装/男装/内衣": (("连衣裙", "连衣裙"), ("T恤", "T恤"), ("衬衫", "衬衫"), ("外套", "外套"), ("内衣", "内衣")),
    "家居生活": (("家纺", "家纺"), ("收纳", "收纳"), ("厨具", "厨具"), ("日用", "日用"), ("家具", "家具")),
    "食品生鲜": (("零食", "零食"), ("水果", "水果"), ("粮油", "粮油"), ("饮料", "饮料"), ("生鲜", "生鲜")),
    "数码电器": (("手机", "手机"), ("耳机", "耳机"), ("充电器", "充电器"), ("小家电", "小家电"), ("平板", "平板")),
    "美妆个护": (("护肤", "护肤"), ("彩妆", "彩妆"), ("洗发", "洗发"), ("口腔", "口腔"), ("面膜", "面膜")),
    "母婴玩具": (("纸尿裤", "纸尿裤"), ("童装", "童装"), ("玩具", "玩具"), ("奶粉", "奶粉"), ("童鞋", "童鞋")),
    "鞋包配饰": (("运动鞋", "运动鞋"), ("单鞋", "单鞋"), ("箱包", "箱包"), ("帽子", "帽子"), ("袜子", "袜子")),
    "运动户外": (("运动服", "运动服"), ("健身", "健身"), ("户外", "户外"), ("骑行", "骑行")),
    "农资绿植": (("种子", "种子"), ("肥料", "肥料"), ("花卉", "花卉"), ("园艺工具", "园艺工具")),
}

# ---- 抖音 ----
DOUYIN_CATEGORY_TREE = {
    "美妆护肤": (("面部护肤", "面部护肤"), ("彩妆", "彩妆"), ("香水", "香水"),
            ("面膜", "面膜"), ("个护", "个护"), ("美妆工具", "美妆工具")),
    "服饰鞋包": (("女装", "女装"), ("男装", "男装"), ("鞋靴", "鞋靴"),
            ("箱包", "箱包"), ("内衣", "内衣"), ("配饰", "配饰")),
    "食品生鲜": (("零食", "零食"), ("生鲜", "生鲜"), ("饮料", "饮料"),
            ("粮油", "粮油"), ("特产", "特产")),
    "数码家电": (("手机", "手机"), ("数码配件", "数码配件"), ("家电", "家电"),
            ("智能设备", "智能设备"), ("电脑", "电脑")),
    "家居日用": (("家纺", "家纺"), ("厨具", "厨具"), ("收纳", "收纳"),
            ("家具", "家具"), ("日用", "日用")),
    "母婴亲子": (("奶粉", "奶粉"), ("纸尿裤", "纸尿裤"), ("童装", "童装"),
            ("玩具", "玩具"), ("孕产", "孕产")),
    "运动户外": (("运动鞋服", "运动鞋服"), ("健身", "健身"), ("户外", "户外"), ("骑行", "骑行")),
    "图书文具": (("图书", "图书"), ("文具", "文具"), ("教辅", "教辅"), ("音像", "音像")),
}

# ---- 1688 ----
ALI1688_CATEGORY_TREE = {
    "服装服饰": (("女装", "女装"), ("男装", "男装"), ("童装", "童装"),
            ("内衣", "内衣"), ("鞋靴", "鞋靴"), ("服装配饰", "服装配饰")),
    "鞋靴箱包": (("女鞋", "女鞋"), ("男鞋", "男鞋"), ("箱包", "箱包"), ("皮带", "皮带")),
    "美妆日化": (("护肤", "护肤"), ("彩妆", "彩妆"), ("洗护", "洗护"), ("日化", "日化")),
    "数码电子": (("手机配件", "手机配件"), ("电脑配件", "电脑配件"), ("数码产品", "数码产品"), ("智能设备", "智能设备")),
    "家居百货": (("家居用品", "家居用品"), ("厨具", "厨具"), ("家纺", "家纺"), ("收纳", "收纳")),
    "食品饮料": (("休闲食品", "休闲食品"), ("饮料", "饮料"), ("粮油", "粮油"), ("生鲜", "生鲜")),
    "包装耗材": (("包装盒", "包装盒"), ("塑料袋", "塑料袋"), ("标签", "标签"), ("胶带", "胶带")),
    "五金工具": (("手动工具", "手动工具"), ("电动工具", "电动工具"), ("紧固件", "紧固件"), ("刀具", "刀具")),
    "纺织面料": (("面料", "面料"), ("纱线", "纱线"), ("辅料", "辅料"), ("家纺面料", "家纺面料")),
    "办公文具": (("文具", "文具"), ("办公用品", "办公用品"), ("教学用品", "教学用品")),
}


def _category_field(label: str, tree: dict) -> SearchField:
    """由分类树生成两级联动分类字段。"""
    return SearchField(
        "category", label, "cascade",
        options=tuple((k, k) for k in tree),
        cascade=tree,
    )


PLATFORMS: tuple[Platform, ...] = (
    Platform(
        id="taobao",
        name="淘宝",
        color="#ff6a00",
        home_url="https://www.taobao.com",
        login_url="https://login.taobao.com/member/login.jhtml",
        search_url="https://s.taobao.com/search?q={q}",
        fallback_search_url="https://s.taobao.com/search?q={q}&tab=mall",
        detail_url_template="https://item.taobao.com/item.htm?id={sku}",
        login_selectors=(".site-nav-user", ".site-nav-login-info-nick", "[class*='site-nav-user']"),
        search_fields=(
            PRICE_RANGE_FIELD,
            _category_field("商品分类（两级）", TAOBAO_CATEGORY_TREE),
            SearchField("sort", "排序方式", "select", options=SORT_DEFAULT),
            SearchField("shop_type", "店铺类型", "multiselect", options=(
                ("mall", "天猫"), ("t", "淘宝集市"))),
            SearchField("brand", "品牌", "multiselect", options=BRAND_OPTIONS),
            SearchField("service", "服务特色", "multiselect", options=(
                ("包邮", "包邮"), ("天猫", "天猫"), ("新品", "新品"), ("二手", "二手"), ("同城", "同城"))),
        ),
        url_params={"sort": "sort", "shop_type": "tab"},
        value_maps={"shop_type": {"mall": "mall", "t": "t"}},
        promo_zones=(
            ("聚划算", "促销", "https://ju.taobao.com"),
            ("天天特卖", "促销", "https://tejia.taobao.com"),
            ("淘宝优惠券", "优惠券", "https://www.taobao.com"),
            ("0元试用", "0元试用", "https://try.taobao.com"),
        ),
    ),
    Platform(
        id="jd",
        name="京东",
        color="#e1251b",
        home_url="https://www.jd.com",
        login_url="https://passport.jd.com/uc/login",
        search_url="https://search.jd.com/Search?keyword={q}&enc=utf-8&page={page}",
        fallback_search_url="https://so.m.jd.com/ware/search.action?keyword={q}",
        detail_url_template="https://item.m.jd.com/product/{sku}.html",
        login_selectors=("#ttbar-login .nickname", ".nickname", ".user-login", "[class*='user-name']"),
        search_fields=(
            PRICE_RANGE_FIELD,
            _category_field("商品分类（两级）", JD_CATEGORY_TREE),
            SearchField("sort", "排序方式", "select", options=(
                ("", "综合排序"), ("sort_totalsales15_desc", "销量优先"),
                ("sort_asc", "价格从低到高"), ("sort_desc", "价格从高到低"))),
            SearchField("channel", "配送 / 店铺", "select", options=(
                ("", "全部"), ("self", "京东自营"), ("logistics", "京东物流"))),
            SearchField("brand", "品牌", "multiselect", options=BRAND_OPTIONS),
            SearchField("service", "服务特色", "multiselect", options=(
                ("自营", "自营"), ("京东物流", "京东物流"), ("新品", "新品"),
                ("秒杀", "秒杀"), ("优惠券", "优惠券"), ("试用", "试用"))),
        ),
        url_params={"sort": "sort", "channel": "shop_type"},
        value_maps={"channel": {"self": "1", "logistics": "2"}},
        promo_zones=(
            ("京东秒杀", "促销", "https://miaosha.jd.com"),
            ("京东优惠券", "优惠券", "https://coupon.jd.com"),
            ("京东试用", "0元试用", "https://try.jd.com"),
            ("京东PLUS", "促销", "https://plus.jd.com"),
        ),
    ),
    Platform(
        id="pdd",
        name="拼多多",
        color="#e02e24",
        home_url="https://www.pinduoduo.com",
        login_url="https://mobile.yangkeduo.com/login.html",
        search_url="https://mobile.yangkeduo.com/search_result.html?search_key={q}&page={page}",
        detail_url_template="https://mobile.yangkeduo.com/goods.html?goods_id={sku}",
        enabled=True,
        search_fields=(
            PRICE_RANGE_FIELD,
            _category_field("商品分类（两级）", PDD_CATEGORY_TREE),
            SearchField("sort", "排序方式", "select", options=SORT_DEFAULT),
            SearchField("subsidy", "活动类型", "select", options=(
                ("", "全部"), ("100yi", "百亿补贴"), ("brand", "品牌好货"))),
            SearchField("location", "发货地", "multiselect", options=(
                ("浙江", "浙江"), ("广东", "广东"), ("江苏", "江苏"),
                ("山东", "山东"), ("福建", "福建"), ("上海", "上海"), ("北京", "北京"))),
            SearchField("brand", "品牌", "multiselect", options=BRAND_OPTIONS),
        ),
        url_params={"sort": "sort", "subsidy": "activity_type"},
        promo_zones=(
            ("百亿补贴", "促销", "https://mobile.yangkeduo.com/official_subsidy.html"),
            ("限时秒杀", "促销", "https://mobile.yangkeduo.com/flash_sale.html"),
            ("0元购", "0元购", "https://mobile.yangkeduo.com"),
        ),
    ),
    Platform(
        id="douyin",
        name="抖音",
        color="#161823",
        home_url="https://www.douyin.com",
        login_url="https://www.douyin.com",
        search_url="https://www.douyin.com/search/{q}?type=general&page={page}",
        detail_url_template="https://www.douyin.com/video/{sku}",
        enabled=True,
        search_fields=(
            PRICE_RANGE_FIELD,
            _category_field("商品分类（两级）", DOUYIN_CATEGORY_TREE),
            SearchField("sort", "排序方式", "select", options=SORT_DEFAULT),
            SearchField("source", "商品来源", "select", options=(
                ("", "全部"), ("live", "直播商品"), ("video", "短视频商品"))),
            SearchField("brand", "品牌", "multiselect", options=BRAND_OPTIONS),
        ),
        url_params={"sort": "sort_type", "source": "source"},
        promo_zones=(
            ("抖音限时购", "促销", "https://www.douyin.com/search/限时购"),
            ("9.9元购", "促销", "https://www.douyin.com/search/9块9"),
            ("0元试用", "0元试用", "https://www.douyin.com/search/免费试用"),
        ),
    ),
    Platform(
        id="ali1688",
        name="1688",
        color="#ff6600",
        home_url="https://www.1688.com",
        login_url="https://login.1688.com/member/signin.htm",
        search_url="https://s.1688.com/selloffer/offer_search.htm?keywords={q}&beginPage={page}",
        detail_url_template="https://detail.1688.com/offer/{sku}.html",
        enabled=True,
        search_fields=(
            PRICE_RANGE_FIELD,
            _category_field("商品分类（两级）", ALI1688_CATEGORY_TREE),
            SearchField("moq", "起订量", "select", options=(
                ("", "不限"), ("1", "1 件起批"), ("2", "2 件起批"),
                ("5", "5 件起批"), ("10", "10 件起批"), ("50", "50 件起批"), ("100", "100 件起批"))),
            SearchField("sort", "排序方式", "select", options=SORT_DEFAULT),
            SearchField("supplier", "供应商类型", "select", options=(
                ("", "全部"), ("factory", "源头工厂"), ("strong", "实力商家"))),
            SearchField("one_piece", "代发方式", "select", options=(
                ("", "全部"), ("yes", "支持一件代发"))),
            SearchField("brand", "品牌", "multiselect", options=BRAND_OPTIONS),
        ),
        url_params={"moq": "min_order", "sort": "sortType", "supplier": "supplierType", "one_piece": "isMixed"},
        promo_zones=(
            ("1688伙拼", "促销", "https://huopin.1688.com"),
            ("1688优惠", "优惠券", "https://www.1688.com"),
            ("0元拿样", "0元试用", "https://www.1688.com"),
        ),
    ),
)

# 为所有平台统一追加通用筛选条件
PLATFORMS = tuple(
    replace(platform, search_fields=platform.search_fields + COMMON_FILTER_FIELDS)
    for platform in PLATFORMS
)

PLATFORM_MAP: dict[str, Platform] = {p.id: p for p in PLATFORMS}

# 浏览器内核：默认使用 Playwright 自带 Chromium；可用环境变量切换为系统已装浏览器。
# 例：BROWSER_CHANNEL=msedge 或 BROWSER_CHANNEL=chrome
BROWSER_CHANNEL: str = os.getenv("BROWSER_CHANNEL", "").strip()
HEADLESS: bool = os.getenv("BROWSER_HEADLESS", "0") not in ("0", "false", "False")
# 代理配置：SEARCH_PROXY 单个代理（向后兼容），SEARCH_PROXIES 逗号分隔代理池。
# 每项可写成 "http://127.0.0.1:7890" 或 "名称=http://127.0.0.1:7890"。
PROXY: str = os.getenv("SEARCH_PROXY", "").strip()


def _parse_proxy_list(raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        value = chunk.strip()
        if not value:
            continue
        if "=" in value and "://" in value:
            name, _, url = value.partition("=")
            out.append((name.strip() or url.strip(), url.strip()))
        else:
            out.append((value, value))
    return out


PROXY_LIST: list[tuple[str, str]] = _parse_proxy_list(os.getenv("SEARCH_PROXIES", ""))
if PROXY and not PROXY_LIST:
    PROXY_LIST = [("default", PROXY)]
