# -*- coding: utf-8 -*-
"""
step_order.py — “麦麦精算师”主程序

一个交互式命令行工具，帮用户解决“想吃啥 vs 多少钱”的纠结：

    第 1 步  定位门店（收藏门店 / 搜索附近，可切换）
    第 2 步  选完门店后选取餐方式：堂食自提 / 得来速 / 麦乐送外卖
    第 3 步  标签式选餐：汉堡 → 小食 → 饮品（都支持多选/数量）
    第 4 步  自动算出“最省的点法”：点哪些套餐 + 需单点补足什么
    第 5 步  展示美团预估差价 + 买卡对比提示

用法：
    python step_order.py              # 交互模式（双击 start.bat 即为此）
    python step_order.py --demo       # 演示模式（自动选择，适合截图/测试）

环境要求：
    - Python 3.8+（零第三方依赖）
    - Token 通过环境变量 MCD_TOKEN 或同目录 token.txt 提供
"""

import argparse
import sys

from mcd_client import McDonaldsMCP
import combo_matcher as cm

KIND_LABEL = {"burger": "主食/汉堡", "snack": "小食", "drink": "饮品"}
KIND_FIELD = {"burger": "burgers", "snack": "snacks", "drink": "drinks"}


# --------------------------------------------------------------------- #
# 通用输入
# --------------------------------------------------------------------- #
def pick_number(prompt: str, lo: int, hi: int, default: int = 1) -> int:
    """读取一个范围内的整数输入，支持默认值。"""
    while True:
        try:
            raw = input(f"{prompt} [{lo}-{hi}]（回车默认{default}）：").strip()
        except EOFError:
            return default
        if raw == "":
            return default
        if raw.isdigit() and lo <= int(raw) <= hi:
            return int(raw)
        print("  输入无效，请重新输入。")


def parse_idx(raw: str) -> list:
    """解析用户的多选输入，如 "2 5 5" -> [2, 5, 5]（重复编号=多份）。"""
    result = []
    for token in raw.replace(",", " ").replace("，", " ").split():
        if token.isdigit():
            result.append(int(token))
    return result


# --------------------------------------------------------------------- #
# 门店定位
# --------------------------------------------------------------------- #
def query_stores(client: McDonaldsMCP, be_type: int, search_type: int,
                 city: str = None, keyword: str = None) -> list:
    """查询门店（query-nearby-stores），统一返回门店数组。"""
    args = {"beType": be_type, "searchType": search_type}
    if search_type == 2:
        args["city"] = city
        args["keyword"] = keyword
    data = client.call_tool("query-nearby-stores", args)
    return (data.get("data") or []) if isinstance(data, dict) else []


def choose_store(client: McDonaldsMCP, demo: bool = False):
    """第 1 步：定位门店（收藏门店 / 搜索附近 可切换）。

    返回 (store, locate_ctx)：
      store      = {storeCode, storeName, address, ...}
      locate_ctx = {method, city, keyword}  供后续得来速/外卖使用
    """
    print("\n[1/5] 定位门店")
    if demo:
        method = 2
    else:
        print("  1. 我的收藏门店（推荐）")
        print("  2. 搜索附近门店")
        method = pick_number("  请选择定位方式", 1, 2, default=1)

    city = keyword = None
    stores = []
    if method == 1:
        stores = query_stores(client, 1, 1)
        if stores:
            print(f"  你的收藏门店（{len(stores)} 家）：")
        else:
            print("  ⚠ 暂无收藏门店，自动改为搜索附近。")
            method = 2
    if method == 2:
        if demo:
            city, keyword = "青岛", "黄岛"
        else:
            city = input("  你在哪个城市？：").strip() or "北京"
            keyword = input("  附近地标/地名？：").strip()
        stores = query_stores(client, 1, 2, city, keyword)
        print(f"  找到 {len(stores)} 家门店：")

    if not stores:
        print("  ⚠ 未找到门店，程序结束。")
        sys.exit(1)
    for i, s in enumerate(stores[:10], 1):
        dist = s.get("distance")
        dist_txt = f"，约{dist}米" if dist else ""
        print(f"    {i}. {s.get('storeName')}（{s.get('address')}{dist_txt}）")
    idx = 1 if demo else pick_number("  请选择门店", 1, min(len(stores), 10), default=1)
    store = stores[idx - 1]
    print(f"  ✅ 已选门店：{store.get('storeName')}")
    return store, {"method": method, "city": city, "keyword": keyword}


# --------------------------------------------------------------------- #
# 取餐方式
# --------------------------------------------------------------------- #
def choose_address(client: McDonaldsMCP, demo: bool = False):
    """外卖：查询并让用户选择配送地址，返回 addressId（无地址返回 None）。"""
    data = client.call_tool("delivery-query-addresses", {})
    addrs = (data.get("data") or {}).get("addresses") or []
    if not addrs:
        return None
    print(f"  你的配送地址（{len(addrs)} 个）：")
    for i, a in enumerate(addrs[:10], 1):
        print(f"    {i}. {a.get('fullAddress')}（{a.get('contactName')}）")
    idx = 1 if demo else pick_number("  请选择收货地址", 1, min(len(addrs), 10), default=1)
    return addrs[idx - 1].get("addressId")


def query_delivery_stores(client: McDonaldsMCP, address_id: str, be_type: int = 2) -> list:
    """外卖：根据地址查可配送门店。"""
    data = client.call_tool("delivery-query-stores", {"addressId": address_id, "beType": be_type})
    return (data.get("data") or []) if isinstance(data, dict) else []


def choose_pickup_method(client: McDonaldsMCP, store: dict, locate_ctx: dict, demo: bool = False):
    """第 2 步：选完门店后选取餐方式。

    返回 (be_type, order_type, be_code, store_code)，用于后续菜单查询。
    """
    store_code = store.get("storeCode")
    store_name = store.get("storeName")
    print(f"\n[2/5] 选择取餐方式（门店：{store_name}）")
    print("  1. 堂食自提（到店取）")
    print("  2. 得来速（开车车道取）")
    print("  3. 麦乐送外卖（送到家）")
    method = 1 if demo else pick_number("  请选择取餐方式", 1, 3, default=1)

    if method == 1:
        print("  ✅ 取餐方式：堂食自提")
        return 1, 1, None, store_code

    if method == 2:
        # 得来速：需 beType=5 重查，取该店的 beCode
        city = locate_ctx.get("city")
        keyword = locate_ctx.get("keyword")
        if not city:
            if demo:
                city, keyword = "青岛", "黄岛"
            else:
                city = input("  为了获取得来速车道信息，请输入城市：").strip() or "北京"
                keyword = input("  附近地标/地名：").strip()
        stores5 = query_stores(client, 5, 2, city, keyword)
        be_code = None
        for s in stores5:
            if s.get("storeCode") == store_code:
                be_code = s.get("beCode")
                break
        if be_code:
            print("  ✅ 取餐方式：得来速")
            return 5, 1, be_code, store_code
        print("  ⚠ 这家店不在得来速门店列表中（可能不支持车道取餐），已改回堂食自提。")
        return 1, 1, None, store_code

    # 外卖（麦乐送）
    print("  🛵 麦乐送外卖需要配送地址……")
    address_id = choose_address(client, demo)
    if not address_id:
        print("  ⚠ 暂无配送地址（请在麦当劳 App 添加），已改回堂食自提。")
        return 1, 1, None, store_code
    dstores = query_delivery_stores(client, address_id, 2)
    if not dstores:
        print("  ⚠ 该地址暂无可配送门店，已改回堂食自提。")
        return 1, 1, None, store_code
    print(f"  该地址可配送门店（{len(dstores)} 家）：")
    for i, s in enumerate(dstores[:10], 1):
        print(f"    {i}. {s.get('storeName')}")
    idx = 1 if demo else pick_number("  请选择配送门店", 1, min(len(dstores), 10), default=1)
    dstore = dstores[idx - 1]
    print(f"  ✅ 取餐方式：麦乐送外卖（配送门店：{dstore.get('storeName')}）")
    return 2, 2, dstore.get("beCode"), dstore.get("storeCode")


# --------------------------------------------------------------------- #
# 标签式多选
# --------------------------------------------------------------------- #
def choose_items_multi(menu: dict, demo: bool = False) -> dict:
    """标签式三步多选：汉堡 -> 小食 -> 饮品，返回 {burgers:[], snacks:[], drinks:[]}。"""
    cart = {"burgers": [], "snacks": [], "drinks": []}
    step_no = {"burger": "a", "snack": "b", "drink": "c"}
    for kind in ["burger", "snack", "drink"]:
        options = cm.pick_options(menu, kind)
        label = KIND_LABEL[kind]
        print(f"\n[{step_no[kind]}] 请选择你想吃的【{label}】（可多选，重复编号=多份）：")
        if not options:
            print(f"  ⚠ 当前菜单没有独立的【{label}】单品，跳过。")
            continue
        for i, o in enumerate(options, 1):
            tags = " ".join(o["tags"][:2])
            print(f"    {i:>2}. {o['name']}  ¥{o['price']}  {tags}")
        if demo:
            idxs = [1]
        else:
            raw = input("  输入编号，多个用空格隔开（如：2 5 或 2 2，回车=跳过）：").strip()
            idxs = parse_idx(raw) if raw else []
        for idx in idxs:
            if 1 <= idx <= len(options):
                cart[KIND_FIELD[kind]].append(options[idx - 1]["name"])
        if cart[KIND_FIELD[kind]]:
            summary = "、".join(f"{n}×{c}" if c > 1 else n
                               for n, c in cm.Counter(cart[KIND_FIELD[kind]]).items())
            print(f"  ✅ 已选{label}：{summary}")
        else:
            print(f"  ➖ 本步跳过（未选{label}）")
    return cart


# --------------------------------------------------------------------- #
# 结果展示
# --------------------------------------------------------------------- #
def meituan_diff_text(price: float) -> str:
    """生成「官方价 vs 美团估算区间」的差价描述。"""
    low, high = cm.estimate_meituan(price)
    if price < low:
        return f"官方 ¥{price} 更便宜（美团预估约贵 ¥{round(low - price, 1)}~{round(high - price, 1)}）"
    if price > high:
        return f"美团预估更便宜 ¥{round(price - high, 1)}~{round(price - low, 1)}"
    return f"官方价在美团区间内（美团到店可能再省 ¥{round(price - low, 1)}）"


def show_plan(plan: dict, cart: dict, menu: dict):
    """展示「整体最优点法」方案。"""
    print("\n" + "=" * 56)
    print("  🍔 推荐点法（尽量用套餐，缺的再单点）")
    print("=" * 56)

    parts = []
    for kind, field in (("burger", "burgers"), ("snack", "snacks"), ("drink", "drinks")):
        for n, c in cm.Counter(cart[field]).items():
            parts.append(f"{n}×{c}" if c > 1 else n)
    print(f"  你的清单：{' + '.join(parts)}\n")

    if plan["chosen"]:
        print("  📦 建议点这些套餐：")
        for i, ch in enumerate(plan["chosen"], 1):
            combo = ch["combo"]
            covered_desc = []
            for kind, info in ch["covered"]:
                lab = KIND_LABEL[kind]
                if info["is_default"]:
                    covered_desc.append(f"{lab}「{info['hit']}」")
                else:
                    covered_desc.append(f"{lab}「{info['hit']}」换品+¥{info['extra']}")
            print(f"    {i}. {combo['name']}  ¥{ch['cost']}")
            print(f"       覆盖：{'、'.join(covered_desc)}")
            low, high = cm.estimate_meituan(ch["cost"])
            print(f"       🛵 美团预估 ¥{low}~¥{high}（估算）｜{meituan_diff_text(ch['cost'])}")
    else:
        print("  📦 没有太合适的套餐，全部单点。")
        print("      💡 建议：这类情况可去美团/外卖平台查同款套餐，往往更划算。")

    if plan["rest"]:
        print("\n  ➕ 还需单点补足：")
        for r in plan["rest"]:
            qty = f"×{r['qty']}" if r["qty"] > 1 else ""
            print(f"    - {KIND_LABEL[r['kind']]}「{r['name']}」{qty}  ¥{r['price'] * r['qty']}")

    print("\n" + "-" * 56)
    print(f"  方案总价：套餐 ¥{plan['combo_total']} + 单点 ¥{plan['rest_total']} = ¥{plan['plan_total']}")
    print(f"  全单点：  ¥{plan['single_total']}")
    if plan["saving"] > 0:
        print(f"  💰 用套餐更划算，省 ¥{plan['saving']}")
    elif plan["saving"] < 0:
        print(f"  💰 这次全单点更划算（套餐覆盖不了你的选择），少花 ¥{-plan['saving']}")
    else:
        print(f"  💰 两种点法价格相同")

    mlow, mhigh = cm.estimate_meituan(plan["plan_total"])
    print(f"  🛵 美团预估方案价：¥{mlow}~¥{mhigh}（估算）｜{meituan_diff_text(plan['plan_total'])}")

    card = cm.count_card_combos(menu)
    if card["count"] > 0:
        print("\n" + "-" * 56)
        print(f"  💳 本店有 {card['count']} 个套餐享会员卡专享价：")
        print(f"     · 麦金卡约 ¥{cm.CARD_PRICE['麦金卡']}/月（含超值四件套、免外送费）")
        print(f"     · 早餐卡约 ¥{cm.CARD_PRICE['早餐卡']}/月（早餐套餐 6 折起）")
        print(f"     若你常吃，买卡后再点这些套餐可能更省，建议在麦当劳 App 对比。")

    print("  提示：餐品信息、价格以麦当劳官方实时结果为准；美团为估算值。\n")


def meituan_compare(plan_total: float, demo: bool = False):
    """可选：输入美团精确到手价做精确对比（默认已展示估算区间）。"""
    if demo:
        return
    print("-" * 56)
    print("  🛵 上面是美团「估算区间」。若你已打开美团看到精确价，可输入对比：")
    try:
        raw = input("  输入美团到手总价（元，直接回车跳过）：").strip()
    except EOFError:
        return
    if not raw:
        return
    try:
        mt = float(raw)
    except ValueError:
        print("  输入的不是数字，已跳过。")
        return
    if plan_total < mt:
        print(f"  ✅ 麦当劳官方更省：¥{plan_total} vs 美团 ¥{mt}，便宜 ¥{round(mt - plan_total, 2)}")
    elif plan_total > mt:
        print(f"  ⚠️ 美团更省：美团 ¥{mt} vs 麦当劳 ¥{plan_total}，便宜 ¥{round(plan_total - mt, 2)}")
    else:
        print(f"  ➖ 两边价格相同：¥{plan_total}")
    print()


# --------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="麦麦精算师")
    parser.add_argument("--demo", action="store_true", help="演示模式：自动选择")
    args = parser.parse_args()

    client = McDonaldsMCP()
    try:
        client.initialize()
    except Exception as e:
        print(f"  ❌ 连接麦当劳 MCP 失败：{e}")
        sys.exit(1)

    # 1. 定位门店
    store, locate_ctx = choose_store(client, demo=args.demo)

    # 2. 选取餐方式
    be_type, order_type, be_code, store_code = choose_pickup_method(
        client, store, locate_ctx, demo=args.demo)

    # 3. 拉取菜单
    print("\n[3/5] 正在拉取菜单……")
    menu = cm.load_menu(client, store_code, be_type=be_type, order_type=order_type, be_code=be_code)
    print(f"  当前在售：套餐 {len(menu['combos'])} 个，单品 {len(menu['singles'])} 个。")

    # 4. 标签式多选
    print("\n[4/5] 开始标签式点餐（先汉堡 → 再小食 → 再饮品，可多选）")
    cart = choose_items_multi(menu, demo=args.demo)
    if not (cart["burgers"] or cart["snacks"] or cart["drinks"]):
        print("\n  你什么都没选，程序结束。")
        sys.exit(0)

    # 5. 计算最优点法
    print("\n[5/5] 正在为你计算最省的点法……")
    plan = cm.plan_order(
        client, menu, store_code,
        cart["burgers"], cart["snacks"], cart["drinks"],
        be_type=be_type, order_type=order_type, be_code=be_code)

    show_plan(plan, cart, menu)
    meituan_compare(plan["plan_total"], demo=args.demo)


if __name__ == "__main__":
    main()
