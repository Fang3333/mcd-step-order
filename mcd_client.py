# -*- coding: utf-8 -*-
"""
mcd_client.py — 麦当劳 MCP 客户端（零第三方依赖，仅用 Python 标准库）

说明：
    麦当劳中国开放了远程托管的 MCP Server（Streamable HTTP 协议），
    本文件实现了一个最小可用的 MCP 客户端，负责：
      1. 与 MCP Server 握手（initialize）
      2. 调用麦当劳提供的各种工具（tools/call）

使用方式：
    from mcd_client import McDonaldsMCP
    client = McDonaldsMCP(token="你的Token")
    result = client.call_tool("query-nearby-stores", {"beType": 1, "searchType": 2, "city": "北京", "keyword": "中关村"})

Token 获取：
    1. 打开 https://open.mcd.cn/mcp 用手机号登录
    2. 点击右上角【控制台】→【激活】获取 MCP Token
    3. Token 请通过环境变量 MCD_TOKEN 传入，切勿硬编码进代码（避免泄露）
"""

import json
import os
import re
import urllib.request
import urllib.error

# 麦当劳 MCP Server 官方接入地址
MCP_URL = "https://mcp.mcd.cn"

# MCP 协议版本（当前麦当劳服务端使用 2024-11-05）
PROTOCOL_VERSION = "2024-11-05"


class McDonaldsMCP:
    """麦当劳 MCP 客户端，封装了 Streamable HTTP 的握手与工具调用。"""

    def __init__(self, token: str = None, url: str = MCP_URL):
        # Token 优先取传入值，其次读环境变量 MCD_TOKEN，最后读同目录 token.txt
        self.token = token or os.environ.get("MCD_TOKEN", "") or self._load_local_token()
        self.url = url
        if not self.token:
            raise ValueError(
                "未提供 MCP Token。请设置环境变量 MCD_TOKEN，"
                "或直接传入 token 参数，或在程序同目录放置 token.txt。"
            )
        self._session_id = None

    @staticmethod
    def _load_local_token() -> str:
        """从程序同目录的 token.txt 读取 Token（方便双击直接运行）。

        token.txt 已被 .gitignore 忽略，不会提交到仓库。
        """
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "token.txt")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except OSError:
                return ""
        return ""

    # ------------------------------------------------------------------ #
    # 底层 HTTP 请求
    # ------------------------------------------------------------------ #
    def _post(self, payload: dict) -> str:
        """向 MCP Server 发送一个 JSON-RPC 请求，返回响应文本。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        # Streamable HTTP 要求同时接受 JSON 与 SSE 两种响应格式
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("Authorization", "Bearer " + self.token)
        if self._session_id:
            req.add_header("mcp-session-id", self._session_id)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._session_id = resp.headers.get("mcp-session-id") or self._session_id
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # 401=Token无效，429=请求过频，这些信息对用户排查很有用
            detail = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"MCP 请求失败 (HTTP {e.code}): {detail}") from e

    def _rpc(self, method: str, params: dict, rid: int = 1) -> dict:
        """发送 JSON-RPC 请求并解析出 result。"""
        text = self._post({
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params,
        })
        obj = json.loads(text)
        if "error" in obj:
            raise RuntimeError(f"MCP 返回错误: {obj['error']}")
        return obj.get("result", {})

    # ------------------------------------------------------------------ #
    # 高层接口
    # ------------------------------------------------------------------ #
    def initialize(self) -> dict:
        """与 MCP Server 握手，返回服务器信息。"""
        result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcd-step-order", "version": "1.0.0"},
        }, rid=1)
        # 握手后需发送 initialized 通知（部分服务端要求）
        try:
            self._post({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            })
        except Exception:
            pass
        return result

    def list_tools(self) -> list:
        """列出麦当劳 MCP Server 当前提供的所有工具。"""
        result = self._rpc("tools/list", {}, rid=1)
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        """调用指定的麦当劳工具，返回其 content 里的文本内容。

        麦当劳工具的统一返回格式为：
            result.content[0].text  —— 一段包含结构化 JSON 数据的文本
        本方法会尽量把 text 里的 JSON 解析成 Python 对象返回，
        方便后续程序化处理。
        """
        result = self._rpc("tools/call", {
            "name": name,
            "arguments": arguments,
        }, rid=1)
        content = result.get("content", [])
        if not content:
            return {}
        text = content[0].get("text", "")
        # 尝试从文本中提取 JSON（麦当劳返回的 text 可能带有说明前缀）
        return self._extract_json(text)

    @staticmethod
    def _extract_json(text: str):
        """从麦当劳工具返回的文本中提取真正的 JSON 数据。

        麦当劳返回的 text 可能带说明前缀，形如：
            '## Response Structure\n...\n{"success":true,"data":{...}}'
        因此这里优先定位 "## Original Response" 标记段，否则用
        {"success" 关键字定位真正的 JSON 对象，再做截取。
        """
        if not text:
            return {}
        seg = text
        marker = "## Original Response"
        idx = text.find(marker)
        if idx != -1:
            seg = text[idx + len(marker):]
        # 优先用 {"success" 定位（麦当劳统一响应格式），避免说明文字里的花括号干扰
        m = re.search(r'\{"success"', seg)
        if m:
            start = m.start()
            end = seg.rfind("}")
            if end > start:
                try:
                    return json.loads(seg[start:end + 1])
                except json.JSONDecodeError:
                    pass
        # 回退：第一个 '{' 到最后一个 '}'
        start = seg.find("{")
        end = seg.rfind("}")
        if start == -1 or end == -1 or end < start:
            return {"_raw": text}
        try:
            return json.loads(seg[start:end + 1])
        except json.JSONDecodeError:
            return {"_raw": text}


def _demo():
    """一个最小示例：列出所有工具并查询附近门店。"""
    import sys
    if "MCD_TOKEN" not in os.environ:
        print("请先设置环境变量 MCD_TOKEN，例如：")
        print('  Windows:  set MCD_TOKEN=你的Token')
        print('  Mac/Linux: export MCD_TOKEN=你的Token')
        sys.exit(1)
    client = McDonaldsMCP()
    client.initialize()
    tools = client.list_tools()
    print(f"麦当劳 MCP 共提供 {len(tools)} 个工具：")
    for t in tools:
        print("  -", t["name"])


if __name__ == "__main__":
    _demo()
