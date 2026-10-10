# -*- coding: utf-8 -*-
"""
combo_matcher.py — “标签式点餐”套餐匹配算法

核心思路（解决“想吃 vs 价格”的纠结）：
    用户分三步（标签式）选出想吃的「汉堡 / 小食 / 饮品」，
    程序自动在麦当劳当前在售的「套餐」里寻找最接近的组合，
    把每个套餐的匹配程度、价格、以及和「单点合计」的差价全部列出来，
    让用户一眼看清：是单点更划算，还是某个套餐更接近自己的选择。

麦当劳套餐的真实结构（来自 query-meal-detail）：
    一个套餐由若干「轮次(rounds)」组成，例如：
      round 1  猪柳蛋麦满分            —— 主食（汉堡）
      round 2  选择早餐套餐小食        —— 小食
      round 3  选择早餐套餐饮料        —— 饮品
    每一轮里有多个「可选项(choices)」，其中 isDefault=1 的是默认项。

因此“匹配”的本质是：
    把用户选的三样东西，分别和每个套餐三轮的可选项做模糊比对，
    算出“命中了几样”，并按命中数、省下的钱来排序。
"""

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

# --------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------- #
def normalize(s: str) -> str:
    """名称归一化：去空白/换行、全角转半角、去掉括号里的内容。

    例如 “麦咖啡™热奶铁(中杯)” -> “麦咖啡热奶铁”
    """
    if not s:
        return ""
    s = str(s)
    s = s.replace("\n", "").replace(" ", "").replace("\u3000", "")
    # 全角字符转半角
    s = s.translate(str.maketrans(
        "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
        "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
        "０１２３４５６７８９（）（）",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789()()"))
    # 去掉括号及括号内内容
    s = re.sub(r"[（(].*?[)）]", "", s)
    return s


def _name_variants(name: str) -> list:
    """生成名称的常见变体（处理“汉堡/堡”这类一字之差）。"""
    n = normalize(name)
    variants = [n]
    if "汉堡" in n:
        variants.append(n.replace("汉堡", "堡"))
    if "堡" in n and "汉堡" not in n:
        variants.append(n.replace("堡", "汉堡"))
    return variants


def fuzzy_match(a: str, b: str) -> bool:
    """模糊匹配两个名称：归一化后，相等、或互为子串，即视为命中。

    例如 “可口可乐” 与 “可乐” 命中；“中薯条” 与 “薯条” 命中；
    “麦辣鸡腿汉堡” 与 “麦辣鸡腿堡” 这类“汉堡/堡”一字之差也命中。
    """
    if not a or not b:
        return False
    for va in _name_variants(a):
        for vb in _name_variants(b):
            if va == vb:
                return True
            x, y = (va, vb) if len(va) >= len(vb) else (vb, va)
            if len(y) >= 2 and y in x:
                return True
    return False


# 单品名的常见营销后缀，匹配套餐名时先剥掉，避免“麦辣鸡腿汉堡单品精选”
# 匹配不上“麦辣鸡腿汉堡四件套”这类漏配
_SKU_SUFFIXES = ["单品精选", "单品", "精选"]


def core_name(name: str) -> str:
    """去掉单品名里的营销后缀，取核心名。"""
    n = normalize(name)
    changed = True
    while changed and n:
        changed = False
        for suf in _SKU_SUFFIXES:
            if n.endswith(suf) and len(n) > len(suf):
                n = n[: -len(suf)]
                changed = True
    return n


def parse_yuan(price) -> float:
    """把麦当劳返回的价格（可能是字符串“18”、数字 1800 分等）统一转为「元」。

    麦当劳菜单里的 currentPrice 是「元」为单位的字符串，
    而 calculate-price 返回的是「分」。这里统一按传入单位处理：
    调用方应自行确保单位，本函数只做字符串→浮点转换。
    """
    try:
        return round(float(price), 2)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------- #
# 分类与套餐识别
# --------------------------------------------------------------------- #
# 用于判断「分类名称」属于“主食/小食/饮品”的关键词（按优先级）
DRINK_KEYWORDS = ["饮", "咖啡", "现磨", "豆浆", "奶", "茶", "汁", "可乐"]
BURGER_KEYWORDS = ["堡", "卷", "饭", "面", "粥", "满分", "松饼", "汉堡", "牛", "鱼", "鸡肉"]
SNACK_KEYWORDS = ["小食", "薯", "派", "翅", "鸡", "甜品", "冰淇淋", "甜", "脆", "块", "炸鸡"]

# 用于判断「餐品名称」属于哪类的关键词（比分类名更可靠，优先级：饮 -> 主食 -> 小食）
# 注意：饮品词要精确，避免误伤（如“脆汁鸡”含“汁”、“柠檬麦旋风”含“柠”）
DRINK_NAME_KEYWORDS = ["咖啡", "美式", "奶铁", "黑巧", "豆浆", "可乐", "雪碧", "红茶",
                       "苹果汁", "牛奶", "纯悦", "怡泉", "现磨", "卡布奇诺", "燕麦", "美汁源"]
BURGER_NAME_KEYWORDS = ["堡", "卷", "饭", "面", "粥", "满分", "松饼", "汉堡"]
SNACK_NAME_KEYWORDS = ["薯", "鸡", "翅", "块", "条", "柳", "派", "冰淇淋", "旋风", "沙拉",
                       "芝士", "玉米", "饼干", "酱", "脆汁鸡", "麦乐鸡", "麦辣鸡", "鸡腿"]


def classify_category(category_name: str) -> str:
    """判断一个菜单分类属于主食/小食/饮品/其他（按 饮->主食->小食 优先级）。"""
    name = normalize(category_name)
    for kw in DRINK_KEYWORDS:
        if kw in name:
            return "drink"
    for kw in BURGER_KEYWORDS:
        if kw in name:
            return "burger"
    for kw in SNACK_KEYWORDS:
        if kw in name:
            return "snack"
    return "other"


def classify_by_name(meal_name: str) -> str:
    """根据餐品名称判断它属于主食/小食/饮品/其他。

    相比分类名，餐品名更能反映食物本质（例如“鸡肉汉堡/卷”分类里
    既有汉堡也有鸡肉卷，但都是主食）。优先级：饮品 -> 主食 -> 小食。
    """
    name = normalize(meal_name)
    for kw in DRINK_NAME_KEYWORDS:
        if kw in name:
            return "drink"
    for kw in BURGER_NAME_KEYWORDS:
        if kw in name:
            return "burger"
    for kw in SNACK_NAME_KEYWORDS:
        if kw in name:
            return "snack"
    return "other"


def is_combo(name: str, tags: list) -> bool:
    """判断一个餐品是不是“套餐”。

    麦当劳套餐命名多样，需覆盖：套餐 / 三件套二件套四件套 / 组合 /
    随心配(1+1) / 随心拼(7件任选) / 开心乐园餐 / 单人餐双人餐 等。
    """
    n = name or ""
    t = tags or []
    combo_kw = ["套餐", "件套", "组合", "随心配", "随心拼", "乐园餐",
                "单人餐", "双人餐", "1+1"]
    for kw in combo_kw:
        if kw in n:
            return True
    for tag in t:
        if "件套" in tag or "套餐" in tag or "组合" in tag or "随心配" in tag:
            return True
    return False


# --------------------------------------------------------------------- #
# 菜单加载与整理
# --------------------------------------------------------------------- #
def load_menu(client, store_code: str, be_type: int = 1, order_type: int = 1,
              be_code: str = None) -> dict:
    """调用 query-meals 获取菜单，整理成统一结构。

    返回：
      {
        "categories": [{"name", "kind", "items": [{code,name,price,tags,is_combo}]}],
        "combos": [...],   # 所有套餐
        "singles": [...],  # 所有单品
      }
    """
    args = {"storeCode": store_code, "orderType": order_type, "beType": be_type}
    if be_code:
        args["beCode"] = be_code
    data = client.call_tool("query-meals", args)
    inner = data.get("data", {}) or {}
    categories_raw = inner.get("categories", []) or []
    meals_map = inner.get("meals", {}) or {}
    # 常点餐品（麦当劳返回"我的常点"，可能只有一个）
    frequent_raw = inner.get("frequent") or {}
    freq_code = frequent_raw.get("code") if isinstance(frequent_raw, dict) else None
    frequent_codes = [freq_code] if freq_code else []

    categories = []
    combos = []
    singles = []
    for cat in categories_raw:
        cat_name = normalize(cat.get("name", ""))
        cat_kind = classify_category(cat_name)
        items = []
        for m in cat.get("meals", []) or []:
            code = m.get("code")
            detail = meals_map.get(code, {}) or {}
            name = detail.get("name", code)
            price = parse_yuan(detail.get("currentPrice", 0))
            original_price = parse_yuan(detail.get("originalPrice", 0))
            discount_type = detail.get("discountType") or ""
            tags = m.get("tags", []) or []
            combo = is_combo(name, tags)
            # 优先按餐品名称归类（更准），名称判断不出来时再用分类名兜底
            kind = classify_by_name(name)
            if kind == "other":
                kind = cat_kind
            item = {
                "code": code,
                "name": name,
                "price": price,
                "original_price": original_price,
                "discount_type": discount_type,
                "image": detail.get("image", ""),
                "tags": tags,
                "is_combo": combo,
                "category": cat_name,
                "kind": kind,
            }
            items.append(item)
            (combos if combo else singles).append(item)
        categories.append({"name": cat_name, "kind": cat_kind, "items": items})

    return {"categories": categories, "combos": combos, "singles": singles,
            "frequent_codes": frequent_codes}


def pick_options(menu: dict, kind: str) -> list:
    """从菜单中取出指定种类（burger/snack/drink）的「单品」选项列表。

    按「常点 > 人气 > 价格」排序，并附带 is_frequent / is_popular 标记。
    """
    seen = set()
    result = []
    freq_codes = set(menu.get("frequent_codes", []) or [])
    for s in menu["singles"]:
        if s["kind"] == kind and s["code"] not in seen:
            seen.add(s["code"])
            item = dict(s)
            item["is_frequent"] = s["code"] in freq_codes
            item["is_popular"] = "人气" in " ".join(s.get("tags", []) or [])
            result.append(item)
    # 常点最前、人气其次，同档按价格升序
    result.sort(key=lambda x: (not x["is_frequent"], not x["is_popular"], x["price"]))
    return result


# --------------------------------------------------------------------- #
# 套餐详情与匹配
# --------------------------------------------------------------------- #
def get_combo_rounds(client, store_code: str, combo_code: str,
                     be_type: int = 1, order_type: int = 1,
                     be_code: str = None) -> list:
    """调用 query-meal-detail 获取套餐的轮次(rounds)结构。"""
    args = {"storeCode": store_code, "orderType": order_type,
            "beType": be_type, "code": combo_code}
    if be_code:
        args["beCode"] = be_code
    data = client.call_tool("query-meal-detail", args)
    inner = data.get("data", {}) or {}
    return inner.get("rounds", []) or []


def _round_kind(round_name: str) -> str:
    """根据轮次名称判断它是主食/小食/饮品轮。

    麦当劳套餐的轮次名称有两种：
      1. 轮次标签，如“小食”“选择套餐内饮料” —— 直接含关键词；
      2. 主食轮的餐品名，如“板烧鸡腿堡”“猪柳蛋麦满分” —— 需按餐品名归类。
    """
    name = normalize(round_name)
    if "饮料" in name or "饮" in name:
        return "drink"
    if "小食" in name:
        return "snack"
    # 其余按餐品名称归类（主食轮通常是餐品名）
    return classify_by_name(name)


def _parse_diff(diff_price) -> float:
    """把套餐选项的差价字符串（如 "+ ¥8"、"- ¥1.5"、"+ ¥0"）解析为元。"""
    if diff_price is None:
        return 0.0
    s = str(diff_price)
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return 0.0
    try:
        return round(float(m.group()), 2)
    except ValueError:
        return 0.0


def match_one_combo(rounds: list, want_burger: str, want_snack: str, want_drink: str) -> dict:
    """把用户想吃的三样东西与一个套餐的轮次做比对，返回匹配结果。

    返回 dict：
      {
        "score": 0-3,                  # 命中了几样
        "burger": {"matched": bool, "offer": "套餐默认主食", "hit": "命中的选项", "extra": 加价元},
        "snack":  {...同上...},
        "drink":  {...同上...},
      }
    说明：
      - offer  = 套餐这一轮“默认自带”的是什么（isDefault=1）
      - hit    = 用户选中的东西在套餐里对应的选项名（命中时才有）
      - extra  = 若命中的是“非默认项”，需要额外加多少钱（元）
    """
    want = {"burger": want_burger, "snack": want_snack, "drink": want_drink}
    result = {"score": 0,
              "burger": {"matched": False, "offer": "", "hit": "", "extra": 0.0},
              "snack": {"matched": False, "offer": "", "hit": "", "extra": 0.0},
              "drink": {"matched": False, "offer": "", "hit": "", "extra": 0.0}}

    for rnd in rounds:
        rname = rnd.get("name", "")
        kind = _round_kind(rname)
        choices = rnd.get("choices", []) or []
        default_choices = [c for c in choices if c.get("isDefault") == 1] or choices
        default_names = [c.get("name", "") for c in default_choices]
        target = want[kind]

        # 找命中的选项（优先默认项命中）
        hit_choice = None
        for c in choices:
            if fuzzy_match(target, c.get("name", "")):
                hit_choice = c
                break

        result[kind]["offer"] = " / ".join(default_names) if default_names else ""
        if hit_choice:
            result[kind]["matched"] = True
            result[kind]["hit"] = hit_choice.get("name", "")
            # 命中项若不是默认项，记录需加价金额
            result[kind]["extra"] = _parse_diff(hit_choice.get("diffPrice"))
            result["score"] += 1
    return result


def find_and_rank_combos(client, menu: dict, store_code: str,
                         want_burger: str, want_snack: str, want_drink: str,
                         be_type: int = 1, order_type: int = 1,
                         be_code: str = None, detail_cache: dict = None) -> list:
    """核心：在套餐里寻找与用户选择最接近的组合，并按匹配度排序。

    返回一个列表，每项：
      {
        "combo": {code,name,price,...},
        "match": {score, burger, snack, drink},
        "single_total": 单点三样合计价(元),
        "saving": 单点合计 - 套餐价 (元，>0 表示套餐更省),
      }
    """
    if detail_cache is None:
        detail_cache = {}

    # 用“汉堡名称”粗筛候选套餐：套餐名里出现汉堡关键词的才可能包含该主食
    burger_key = normalize(want_burger)
    # 取汉堡名称中的“核心词”（去掉常见后缀如“汉堡”本身，便于命中套餐名）
    candidates = []
    for combo in menu["combos"]:
        cname = normalize(combo["name"])
        # 命中条件：套餐名与汉堡名互为子串
        if fuzzy_match(burger_key, cname):
            candidates.append(combo)
    # 若名称粗筛为空，则退回全部套餐
    if not candidates:
        candidates = menu["combos"]

    results = []
    for combo in candidates:
        code = combo["code"]
        if code not in detail_cache:
            detail_cache[code] = get_combo_rounds(
                client, store_code, code, be_type, order_type, be_code)
        rounds = detail_cache[code]
        if not rounds:
            continue
        match = match_one_combo(rounds, want_burger, want_snack, want_drink)

        # ---- 价格核算（诚实、符合直觉）----
        # 1) 换品加价：命中了但非默认的项，需要额外加的钱
        extra_total = 0.0
        for kind in ("burger", "snack", "drink"):
            if match[kind]["matched"]:
                extra_total += match[kind]["extra"]
        # 2) 缺项单点：用户想吃的、但套餐里没有的项，需要另单点
        missing_total = 0.0
        for kind, wanted in (("burger", want_burger), ("snack", want_snack),
                             ("drink", want_drink)):
            if wanted and not match[kind]["matched"]:
                missing_total += _lookup_price(menu, wanted)
        # 3) 三样全单点合计
        single_total = 0.0
        for wanted in (want_burger, want_snack, want_drink):
            if wanted:
                single_total += _lookup_price(menu, wanted)

        combo_price = combo["price"]
        total_plan = round(combo_price + extra_total + missing_total, 2)
        saving = round(single_total - total_plan, 2)
        results.append({
            "combo": combo,
            "match": match,
            "combo_price": combo_price,
            "extra_total": round(extra_total, 2),
            "missing_total": round(missing_total, 2),
            "total_plan": total_plan,
            "single_total": round(single_total, 2),
            "saving": saving,
        })

    # 排序：命中数降序 -> 省的越多越靠前 -> 方案实付低的靠前
    results.sort(key=lambda r: (-r["match"]["score"], -r["saving"], r["total_plan"]))

    # 去重：麦当劳菜单里常有多个「同名称+同价」的套餐条目，只保留一个
    seen = set()
    deduped = []
    for r in results:
        key = (r["combo"]["name"], r["combo_price"], r["match"]["score"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def _lookup_price(menu: dict, name: str) -> float:
    """按名称在单品里查找价格（模糊），找不到返回 0。"""
    for s in menu["singles"]:
        if fuzzy_match(name, s["name"]):
            return s["price"]
    # 再在套餐里找（用户可能选了个套餐名里的主食）
    for c in menu["combos"]:
        if fuzzy_match(name, c["name"]):
            return c["price"]
    return 0.0


# --------------------------------------------------------------------- #
# 多选 + 整体方案推荐（plan_order）
# --------------------------------------------------------------------- #
def match_combo_to_cart(rounds: list, burger_counter: Counter,
                        snack_counter: Counter, drink_counter: Counter,
                        relax: bool = False) -> dict:
    """判断一个套餐能覆盖「购物清单」里的哪些项（每类至多覆盖 1 项）。

    relax=True 时为「灵活搭配」模式：精确匹配不上时，允许用同类相似品替代
    （如你要「薯条」，套餐默认是「脆薯饼」也算命中，hit 带 similar 标记）。

    返回 dict：{"burger": info或None, "snack": ..., "drink": ...}
    其中 info = {"want": 清单项名, "hit": 套餐里命中的选项名, "extra": 换品加价,
                "is_default": bool, "similar": 是否为相似替代}
    """
    result = {"burger": None, "snack": None, "drink": None}
    counters = {"burger": burger_counter, "snack": snack_counter, "drink": drink_counter}
    for rnd in rounds:
        kind = _round_kind(rnd.get("name", ""))
        if kind not in counters:
            continue  # 无法归类的主食轮（如'other'），跳过
        counter = counters[kind]
        choices = rnd.get("choices", []) or []
        # 在该类清单里找一个能命中的项
        for want_name, qty in list(counter.items()):
            if qty <= 0:
                continue
            hit = None
            similar = False
            for c in choices:
                if fuzzy_match(want_name, c.get("name", "")):
                    hit = c
                    break
            if hit is None and relax:
                # 宽松模式：允许同类相似品替代（如 薯条→脆薯饼）
                for c in choices:
                    if _is_similar_name(want_name, c.get("name", "")):
                        hit = c
                        similar = True
                        break
            if hit:
                result[kind] = {
                    "want": want_name,
                    "hit": hit.get("name", ""),
                    "extra": _parse_diff(hit.get("diffPrice")),
                    "is_default": hit.get("isDefault") == 1,
                    "similar": similar,
                }
                break
    return result


def _is_similar_name(a: str, b: str) -> bool:
    """两个名称是否属同类相似品（共享某个核心关键词，如都含「薯」「鸡」「牛」）。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    return any(k in na and k in nb for k in SIMILAR_KEYWORDS)


def _combo_bonus(rounds: list, cov: dict, counters: dict) -> list:
    """算出一个套餐里「用户没点、但套餐自带」的东西（含估算价值）。

    例如用户只要了汉堡+薯条，四件套还自带一杯饮品 → bonus 里就是那杯饮品。
    """
    bonus = []
    for rnd in rounds:
        kind = _round_kind(rnd.get("name", ""))
        if kind not in counters:
            continue
        # 该轮已被用户的选择命中（且用的就是默认项）→ 不算多含
        info = cov.get(kind)
        choices = rnd.get("choices", []) or []
        defaults = [c for c in choices if c.get("isDefault") == 1]
        if info:
            # 命中了：若命中的是非默认项（换品），原默认项被替换掉，不算多含
            continue
        # 该轮完全没被用户的清单用到 → 套餐自带的默认项就是“多含”
        for d in defaults:
            bonus.append({"kind": kind, "name": d.get("name", ""),
                          "price": _lookup_price_from_choice(d)})
    return bonus


def _lookup_price_from_choice(choice: dict) -> float:
    """从套餐轮次的选项里尽量解析出一个参考价（没有就 0）。"""
    for key in ("price", "currentPrice", "salePrice"):
        v = choice.get(key)
        if v not in (None, ""):
            try:
                return round(float(str(v).replace("¥", "").replace("+", "").strip()), 2)
            except (TypeError, ValueError):
                pass
    return 0.0


def _is_card_combo(combo: dict) -> bool:
    """判断套餐是否享「会员卡专享价/随单购卡」（discount_type 或标签含“卡”）。"""
    dt = combo.get("discount_type", "") or ""
    tags = " ".join(combo.get("tags", []) or [])
    return "卡" in dt or "卡" in tags


def plan_order(client, menu: dict, store_code: str,
               burgers: list, snacks: list, drinks: list,
               be_type: int = 1, order_type: int = 1,
               be_code: str = None, detail_cache: dict = None,
               card_only: bool = False, relax: bool = False) -> dict:
    """多选模式下的「整体最优点法」推荐（贪心算法，结果可解释）。

    burgers / snacks / drinks：名称列表，可含重复（重复即表示数量）。

    返回 dict：
      {
        "chosen": 选中的套餐列表（可重复），每项含 combo/cov/cost/saving 等,
        "rest":   需单点补足的项,
        "combo_total": 套餐总价,
        "rest_total":  单点补足总价,
        "single_total":全单点总价,
        "plan_total":  套餐+单点 总价,
        "saving":      全单点 - 方案总价（>0 表示方案更省）,
      }
    """
    if detail_cache is None:
        detail_cache = {}
    # 防御：过滤空名字，避免空项混入清单
    burgers = [b for b in burgers if b]
    snacks = [s for s in snacks if s]
    drinks = [d for d in drinks if d]
    burger_counter = Counter(burgers)
    snack_counter = Counter(snacks)
    drink_counter = Counter(drinks)

    # 候选套餐：含任一汉堡名称的套餐（模糊匹配，先剥“单品精选”等后缀）；没选汉堡则用全部套餐
    cands = []
    if burger_counter:
        for bname in burger_counter:
            core = core_name(bname)
            for combo in menu["combos"]:
                if fuzzy_match(core, combo["name"]) or fuzzy_match(bname, combo["name"]):
                    cands.append(combo)
    if not cands:
        cands = menu["combos"]
    if card_only:
        card_cands = [c for c in cands if _is_card_combo(c)]
        # 卡价套餐里没有匹配汉堡的，就退回全部卡价套餐（随单开卡专享的那批）
        cands = card_cands or [c for c in menu["combos"] if _is_card_combo(c)]
    # 按 code 去重
    seen = set()
    uniq = []
    for c in cands:
        if c["code"] in seen:
            continue
        seen.add(c["code"])
        uniq.append(c)
    cands = uniq

    # 预取候选套餐详情（并发拉取，避免串行等待导致“计算”很慢）
    codes_to_fetch = [c["code"] for c in cands if c["code"] not in detail_cache]
    if codes_to_fetch:
        def _fetch(code):
            return code, get_combo_rounds(client, store_code, code, be_type, order_type, be_code)
        # 并发度 8，显著缩短大量套餐详情的拉取时间
        with ThreadPoolExecutor(max_workers=8) as ex:
            for code, rounds in ex.map(_fetch, codes_to_fetch):
                detail_cache[code] = rounds

    chosen = []
    # 贪心：反复选「当前最省」的套餐，直到没有套餐能再省钱
    while True:
        best = None
        for combo in cands:
            rounds = detail_cache.get(combo["code"], []) or []
            if not rounds:
                continue
            cov = match_combo_to_cart(rounds, burger_counter, snack_counter,
                                      drink_counter, relax=relax)
            covered = []
            value = 0.0
            extra = 0.0
            for kind in ("burger", "snack", "drink"):
                info = cov[kind]
                if info:
                    covered.append((kind, info))
                    value += _lookup_price(menu, info["want"])
                    extra += info["extra"]
            if not covered:
                continue
            combo_cost = combo["price"] + extra
            saving = value - combo_cost
            if saving > 0 and (best is None or saving > best["saving"]):
                counters = {"burger": burger_counter, "snack": snack_counter,
                            "drink": drink_counter}
                bonus = _combo_bonus(rounds, cov, counters)
                best = {"combo": combo, "cov": cov, "covered": covered,
                        "value": round(value, 2), "extra": round(extra, 2),
                        "cost": round(combo_cost, 2), "saving": round(saving, 2),
                        "bonus": bonus}
        if best is None:
            break
        # 从清单里扣掉被覆盖的项
        for kind, info in best["covered"]:
            cnt = {"burger": burger_counter, "snack": snack_counter,
                   "drink": drink_counter}[kind]
            if info["want"] in cnt and cnt[info["want"]] > 0:
                cnt[info["want"]] -= 1
        chosen.append(best)
        if len(chosen) > 200:  # 安全上限，防死循环
            break

    # 剩余单点
    rest = []
    rest_total = 0.0
    for kind, counter in (("burger", burger_counter), ("snack", snack_counter),
                          ("drink", drink_counter)):
        for name, qty in counter.items():
            if qty > 0:
                p = _lookup_price(menu, name)
                rest.append({"kind": kind, "name": name, "qty": qty, "price": p})
                rest_total += p * qty

    combo_total = round(sum(c["cost"] for c in chosen), 2)
    single_total = round(sum(_lookup_price(menu, n)
                             for n in burgers + snacks + drinks), 2)
    plan_total = round(combo_total + rest_total, 2)
    saving = round(single_total - plan_total, 2)

    return {
        "chosen": chosen,
        "rest": rest,
        "combo_total": combo_total,
        "rest_total": round(rest_total, 2),
        "single_total": single_total,
        "plan_total": plan_total,
        "saving": saving,
    }


# --------------------------------------------------------------------- #
# 美团参考价估算（无开放接口，采用透明估算）
# --------------------------------------------------------------------- #
# 估算参数（可自行调整）：
#   美团「到店自取/团购」常见折扣下限设为官方价的 85%；
#   美团「外卖」在官方价基础上通常加配送费+包装费，上限设为 +5 元。
MEITUAN_MIN_RATIO = 0.85
MEITUAN_MAX_EXTRA = 5.0


def estimate_meituan(price: float) -> tuple:
    """估算一个价格在美团的参考到手价区间 (low, high)。

    美团没有开放接口，无法获取实时价格，因此这里基于常见规律做**估算**：
      - 下限 low  = 官方价 × 0.85（到店自取/团购常有折扣）
      - 上限 high = 官方价 + 5 元（外卖加配送费、包装费）
    区间仅供参考，实际以美团 App 实时价格为准。
    """
    if price <= 0:
        return (0.0, 0.0)
    low = round(price * MEITUAN_MIN_RATIO, 1)
    high = round(price + MEITUAN_MAX_EXTRA, 1)
    return (low, high)


# 麦当劳会员卡参考价（来自公开信息，价格会浮动，以官方为准）
CARD_PRICE = {
    "麦金卡": 19.0,    # 约 19 元/30 天，含超值四件套、免外送费、早餐 6 折等
    "早餐卡": 11.9,    # 约 11.9 元/30 天，早餐套餐 6 折起
}


def count_card_combos(menu: dict) -> dict:
    """统计菜单里享受「卡优惠」的套餐数量，用于买卡提示。

    判断依据：餐品 discount_type 含「卡」，或标签含「卡」。
    """
    total = 0
    for c in menu["combos"]:
        dt = c.get("discount_type", "") or ""
        tags = " ".join(c.get("tags", []) or [])
        if "卡" in dt or "卡" in tags:
            total += 1
    return {"count": total, "total_combos": len(menu["combos"])}


# --------------------------------------------------------------------- #
# 三方案生成（预算驱动）
# --------------------------------------------------------------------- #
def _build_single_plan(menu: dict, burgers: list, snacks: list, drinks: list) -> dict:
    """方案2「口味贴合」：用户选什么就点什么（全单点，不换套餐）。"""
    rest = []
    total = 0.0
    for kind, items in (("burger", burgers), ("snack", snacks), ("drink", drinks)):
        for n, qty in Counter(items).items():
            p = _lookup_price(menu, n)
            rest.append({"kind": kind, "name": n, "qty": qty, "price": p})
            total += p * qty
    total = round(total, 2)
    return {
        "chosen": [], "rest": rest, "dropped": [],
        "combo_total": 0.0, "rest_total": total,
        "plan_total": total, "single_total": total, "saving": 0.0,
    }


def _build_budget_plan(menu: dict, burgers: list, snacks: list, drinks: list,
                       budget: float) -> dict:
    """方案3「预算取舍」：在预算内保留尽量多（主食 > 饮品 > 小食 优先级）。"""
    single_total = round(sum(_lookup_price(menu, n) for n in burgers + snacks + drinks), 2)
    # 预算充足或无预算 → 直接全单点
    if budget is None or single_total <= budget:
        return _build_single_plan(menu, burgers, snacks, drinks)

    # 预算不足：按 小食 → 饮品 → 汉堡 顺序，每类内从贵到便宜舍弃
    keep = {"burger": list(burgers), "snack": list(snacks), "drink": list(drinks)}
    dropped = []
    for kind in ("snack", "drink", "burger"):
        keep[kind].sort(key=lambda n: -_lookup_price(menu, n))
        while keep[kind]:
            total = sum(_lookup_price(menu, n)
                        for k in ("burger", "snack", "drink") for n in keep[k])
            if total <= budget:
                break
            dropped.append({"kind": kind, "name": keep[kind].pop(0)})

    rest = []
    total = 0.0
    for kind in ("burger", "snack", "drink"):
        for n, qty in Counter(keep[kind]).items():
            p = _lookup_price(menu, n)
            rest.append({"kind": kind, "name": n, "qty": qty, "price": p})
            total += p * qty
    total = round(total, 2)
    return {
        "chosen": [], "rest": rest, "dropped": dropped,
        "combo_total": 0.0, "rest_total": total,
        "plan_total": total, "single_total": single_total,
        "saving": round(single_total - total, 2),
    }


def _trim_to_budget(menu: dict, burgers: list, snacks: list, drinks: list,
                    budget: float) -> tuple:
    """预算不足时砍单：按 小食 → 饮品 → 汉堡 顺序舍弃（每类内从贵到便宜）。

    返回 (保留下来的三类清单 dict, 舍弃清单 list)。
    """
    keep = {"burger": list(burgers), "snack": list(snacks), "drink": list(drinks)}
    dropped = []
    for kind in ("snack", "drink", "burger"):
        keep[kind].sort(key=lambda n: -_lookup_price(menu, n))
        while keep[kind]:
            total = sum(_lookup_price(menu, n)
                        for k in ("burger", "snack", "drink") for n in keep[k])
            if total <= budget:
                break
            dropped.append({"kind": kind, "name": keep[kind].pop(0)})
    return keep, dropped


def _build_card_plan(client, menu: dict, store_code: str,
                     burgers: list, snacks: list, drinks: list,
                     budget: float, be_type: int, order_type: int,
                     be_code: str, detail_cache: dict) -> dict:
    """方案4「开卡更省」：随单开麦金卡（约¥19/月）享卡价套餐。

    融合「不一定全要」逻辑：给了预算且预算不够时，先砍单再凑卡价套餐。
    总价 = 卡价套餐 + 单点补足 + 卡费（首月）。
    """
    burgers = [b for b in burgers if b]
    snacks = [s for s in snacks if s]
    drinks = [d for d in drinks if d]
    dropped = []
    if budget is not None:
        single_all = round(sum(_lookup_price(menu, n)
                               for n in burgers + snacks + drinks), 2)
        if single_all > budget:
            keep, dropped = _trim_to_budget(menu, burgers, snacks, drinks, budget)
            burgers, snacks, drinks = keep["burger"], keep["snack"], keep["drink"]

    plan = plan_order(client, menu, store_code, burgers, snacks, drinks,
                      be_type, order_type, be_code, detail_cache, card_only=True)
    card_fee = CARD_PRICE["麦金卡"]
    plan["card_fee"] = card_fee
    plan["plan_total"] = round(plan["plan_total"] + card_fee, 2)
    plan["dropped"] = dropped
    plan["saving"] = round(plan["single_total"] - plan["plan_total"], 2)
    return plan


def generate_plans(client, menu: dict, store_code: str,
                   burgers: list, snacks: list, drinks: list,
                   budget: float = None, be_type: int = 1, order_type: int = 1,
                   be_code: str = None, detail_cache: dict = None) -> list:
    """生成 4 个点餐方案。

    - 方案1「严格套餐」：贪心用套餐打包，最省钱；每个套餐标注“多含了什么+价值”；
    - 方案2「精准单点」：舍弃套餐优惠，只点你要的（内容最精准，但可能更贵）；
    - 方案3「灵活搭配」：套餐优先，没覆盖的项给出相似替代品（可换可不换）；
    - 方案4「开卡更省」：随单开麦金卡（约¥19/月）享卡价套餐，融合“不一定全要”的取舍逻辑。

    返回 4 个方案 dict（结构同 plan_order，另含 title/desc/dropped/vs_base/bonus）。
    """
    p1 = plan_order(client, menu, store_code, burgers, snacks, drinks,
                    be_type, order_type, be_code, detail_cache)
    p1["title"] = "严格套餐"
    p1["desc"] = "买套餐打包价，多含的东西也列给你"

    p2 = _build_single_plan(menu, burgers, snacks, drinks)
    p2["title"] = "精准单点"
    p2["desc"] = "舍弃套餐优惠，只点你要的"

    p3 = plan_order(client, menu, store_code, burgers, snacks, drinks,
                    be_type, order_type, be_code, detail_cache, relax=True)
    p3["title"] = "灵活搭配"
    p3["desc"] = "套餐优先，缺的可用相似品替代"
    # 给单点补足的每一项附上同类相似替代建议
    for r in p3.get("rest", []):
        r["alternatives"] = find_similar(menu, r["name"], r["kind"])

    p4 = _build_card_plan(client, menu, store_code, burgers, snacks, drinks,
                          budget, be_type, order_type, be_code, detail_cache)
    p4["title"] = "开卡更省"
    p4["desc"] = "随单开麦金卡享卡价套餐，可舍弃部分"
    for r in p4.get("rest", []):
        r["alternatives"] = find_similar(menu, r["name"], r["kind"])
    for d in p4.get("dropped", []):
        d["alternatives"] = find_similar(menu, d["name"], d["kind"])

    # 以方案1（严格套餐）为基线，标注其他方案相对它的差价
    base = p1["plan_total"]
    for p in (p1, p2, p3, p4):
        p["vs_base"] = round(p["plan_total"] - base, 2)

    # 给「舍弃」项附上同类相似替代建议（方案4 的 dropped 已带）
    for d in p3.get("dropped", []):
        d["alternatives"] = find_similar(menu, d["name"], d["kind"])

    return [p1, p2, p3, p4]


# --------------------------------------------------------------------- #
# 相似替代品推荐
# --------------------------------------------------------------------- #
# 用于判断「同类相似品」的核心关键词（两个餐品名共享某个词即视为相似）
SIMILAR_KEYWORDS = ["薯", "鸡", "牛", "鱼", "堡", "卷", "翅", "块", "条", "派",
                    "冰淇淋", "麦旋风", "咖啡", "可乐", "雪碧", "茶", "黑巧", "豆浆",
                    "柠檬", "沙拉", "芝士", "玉米", "饼干", "粥", "松饼", "满分"]


def find_similar(menu: dict, name: str, kind: str, top: int = 3) -> list:
    """在同类单品里找「相似替代品」，返回 [{name, price, image}, ...]。

    相似判断：与目标名称共享某个核心关键词（如都含「薯」/「鸡」/「牛」）。
    """
    n = normalize(name)
    scored = []
    for s in menu["singles"]:
        if s["kind"] != kind or s["name"] == name:
            continue
        sn = normalize(s["name"])
        common = [k for k in SIMILAR_KEYWORDS if k in n and k in sn]
        if common:
            scored.append((len(common), s["price"], s))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [{"name": s["name"], "price": s["price"], "image": s.get("image", "")}
            for _, _, s in scored[:top]]
