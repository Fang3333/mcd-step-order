# -*- coding: utf-8 -*-
"""
web_app.py — 麦麦精算师 Web 版（浏览器里点按钮就能玩）

零第三方依赖，用 Python 标准库 http.server 提供本地网页服务。
启动后自动打开浏览器，可视化完成：定位门店 → 选取餐方式 → 选餐 → 计算最省点法。

用法：
    python web_app.py            # 默认 http://127.0.0.1:8899
    python web_app.py --port 9000
"""

import argparse
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from mcd_client import McDonaldsMCP
import combo_matcher as cm

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8899

_client = None
_client_lock = threading.Lock()


def get_client() -> McDonaldsMCP:
    """懒加载全局 MCP 客户端（多线程共享一个连接）。"""
    global _client
    with _client_lock:
        if _client is None:
            _client = McDonaldsMCP()
            _client.initialize()
        return _client


def query_stores(client, be_type, search_type, city=None, keyword=None) -> list:
    args = {"beType": be_type, "searchType": search_type}
    if search_type == 2:
        args["city"] = city
        args["keyword"] = keyword
    data = client.call_tool("query-nearby-stores", args)
    return (data.get("data") or []) if isinstance(data, dict) else []


def drivethru_becode(client, store_code, city, keyword):
    """得来速：beType=5 重查，取该店 beCode。"""
    for s in query_stores(client, 5, 2, city, keyword):
        if s.get("storeCode") == store_code:
            return s.get("beCode")
    return None


def delivery_addresses(client) -> list:
    data = client.call_tool("delivery-query-addresses", {})
    return (data.get("data") or {}).get("addresses") or []


def delivery_stores(client, address_id) -> list:
    data = client.call_tool("delivery-query-stores", {"addressId": address_id, "beType": 2})
    return (data.get("data") or []) if isinstance(data, dict) else []


def build_menu(client, store_code, be_type, order_type, be_code):
    """加载菜单，转成前端友好的三类单品列表。"""
    menu = cm.load_menu(client, store_code, be_type=be_type, order_type=order_type, be_code=be_code)
    result = {}
    for kind in ("burger", "snack", "drink"):
        result[kind] = [{"name": o["name"], "price": o["price"], "image": o.get("image", ""),
                         "freq": o.get("is_frequent", False), "pop": o.get("is_popular", False)}
                        for o in cm.pick_options(menu, kind)]
    return {"menu": result, "card_count": cm.count_card_combos(menu)["count"]}


def _format_plan(plan: dict, menu: dict) -> dict:
    """把方案 dict（plan_order / generate_plans 返回）格式化成前端友好的 JSON。"""
    chosen = []
    for ch in plan.get("chosen", []):
        covered = [{"kind": k, "want": i.get("want", ""), "hit": i["hit"],
                    "is_default": i["is_default"], "extra": i["extra"],
                    "similar": i.get("similar", False)}
                   for k, i in ch["covered"]]
        bonus = [{"kind": b["kind"], "name": b["name"], "price": b["price"]}
                 for b in ch.get("bonus", [])]
        low, high = cm.estimate_meituan(ch["cost"])
        chosen.append({
            "name": ch["combo"]["name"], "cost": ch["cost"], "covered": covered,
            "bonus": bonus, "meituan_low": low, "meituan_high": high,
        })
    rest = [{"kind": r["kind"], "name": r["name"], "qty": r["qty"],
             "price": r["price"], "alternatives": r.get("alternatives", [])}
            for r in plan.get("rest", [])]
    dropped = [{"kind": d["kind"], "name": d["name"],
                "alternatives": d.get("alternatives", [])}
               for d in plan.get("dropped", [])]
    mlow, mhigh = cm.estimate_meituan(plan["plan_total"])
    return {
        "title": plan.get("title", ""),
        "desc": plan.get("desc", ""),
        "vs_base": plan.get("vs_base", 0),
        "card_fee": plan.get("card_fee", 0),
        "chosen": chosen, "rest": rest, "dropped": dropped,
        "combo_total": plan.get("combo_total", 0),
        "rest_total": plan.get("rest_total", 0),
        "plan_total": plan["plan_total"],
        "single_total": plan.get("single_total", 0),
        "saving": plan.get("saving", 0),
        "meituan_low": mlow, "meituan_high": mhigh,
        "card_count": cm.count_card_combos(menu)["count"],
        "has_no_combo": len(chosen) == 0,
    }


def build_plan(client, store_code, be_type, order_type, be_code, burgers, snacks, drinks):
    """计算最省点法（单方案），返回前端友好的 JSON。"""
    menu = cm.load_menu(client, store_code, be_type=be_type, order_type=order_type, be_code=be_code)
    plan = cm.plan_order(client, menu, store_code, burgers, snacks, drinks,
                         be_type=be_type, order_type=order_type, be_code=be_code)
    return _format_plan(plan, menu)


def build_plans(client, store_code, be_type, order_type, be_code,
                burgers, snacks, drinks, budget):
    """生成 3 个点餐方案，返回前端友好的 JSON 数组。"""
    menu = cm.load_menu(client, store_code, be_type=be_type, order_type=order_type, be_code=be_code)
    plans = cm.generate_plans(client, menu, store_code, burgers, snacks, drinks,
                              budget=budget, be_type=be_type, order_type=order_type,
                              be_code=be_code)
    return {"plans": [_format_plan(p, menu) for p in plans],
            "card_count": cm.count_card_combos(menu)["count"]}


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        path = os.path.join(HERE, "index.html")
        with open(path, "r", encoding="utf-8") as f:
            body = f.read().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 禁止缓存：让浏览器每次都拉最新版页面，避免改版后用户看到旧界面
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def _path_qs(self):
        u = urlparse(self.path)
        return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}

    def do_GET(self):
        path, qs = self._path_qs()
        try:
            client = get_client()
            if path == "/":
                self._send_html()
                return
            if path == "/api/stores":
                method = int(qs.get("method", "1"))
                be_type = int(qs.get("be_type", "1"))
                if method == 1:
                    stores = query_stores(client, be_type, 1)
                else:
                    stores = query_stores(client, be_type, 2, qs.get("city"), qs.get("keyword"))
                self._send_json({"stores": [{"storeCode": s.get("storeCode"),
                                             "storeName": s.get("storeName"),
                                             "address": s.get("address"),
                                             "distance": s.get("distance")} for s in stores]})
                return
            if path == "/api/drivethru":
                be_code = drivethru_becode(client, qs.get("store_code"),
                                           qs.get("city"), qs.get("keyword"))
                self._send_json({"be_code": be_code})
                return
            if path == "/api/delivery_addresses":
                addrs = delivery_addresses(client)
                self._send_json({"addresses": [{"addressId": a.get("addressId"),
                                                "contactName": a.get("contactName"),
                                                "fullAddress": a.get("fullAddress")} for a in addrs]})
                return
            if path == "/api/delivery_stores":
                stores = delivery_stores(client, qs.get("address_id"))
                self._send_json({"stores": [{"storeCode": s.get("storeCode"),
                                             "beCode": s.get("beCode"),
                                             "storeName": s.get("storeName")} for s in stores]})
                return
            if path == "/api/menu":
                self._send_json(build_menu(client, qs.get("store_code"),
                                           int(qs.get("be_type", 1)), int(qs.get("order_type", 1)),
                                           qs.get("be_code") or None))
                return
            self._send_json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": str(e)}, 500)

    def do_POST(self):
        path, _ = self._path_qs()
        try:
            client = get_client()
            if path == "/api/plan":
                body = self._read_body()
                self._send_json(build_plan(
                    client, body.get("store_code"), body.get("be_type", 1),
                    body.get("order_type", 1), body.get("be_code") or None,
                    body.get("burgers", []), body.get("snacks", []), body.get("drinks", [])))
                return
            if path == "/api/plans":
                body = self._read_body()
                budget = body.get("budget")
                self._send_json(build_plans(
                    client, body.get("store_code"), body.get("be_type", 1),
                    body.get("order_type", 1), body.get("be_code") or None,
                    body.get("burgers", []), body.get("snacks", []), body.get("drinks", []),
                    budget))
                return
            self._send_json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": str(e)}, 500)

    def log_message(self, *args):  # 关闭请求日志，保持清爽
        pass


def main():
    parser = argparse.ArgumentParser(description="麦麦精算师 Web 版")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    # 云端部署时用 PORT 环境变量；本地默认 8899
    port = int(os.environ.get("PORT", args.port))
    # 绑定 0.0.0.0：本地与云端都可通过对应地址访问
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"🍔 麦麦精算师已启动：{url}")
    print("   浏览器会自动打开，如果没有，请手动访问上面的地址。")
    print("   按 Ctrl+C 退出。")
    # 云端部署时不自动弹浏览器（无界面环境）
    if not os.environ.get("PORT"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
        server.shutdown()


if __name__ == "__main__":
    main()
