# MCP_INTEGRATION.md

## 本项目实际使用的麦当劳 MCP 能力

本项目「麦麦精算师」基于麦当劳中国官方 MCP Server（`https://mcp.mcd.cn`，Streamable HTTP 协议）开发，真实调用了以下工具完成核心业务。

### 使用的 MCP Server

| 项 | 值 |
|---|---|
| Server 名称 | `mcd-mcp` |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 认证方式 | `Authorization: Bearer <MCP_TOKEN>` |

### 使用的 Tool 与调用流程

整个「标签式点餐」流程共调用多类核心工具，形成一条完整链路（按三种取餐方式分路）：

```
① 定位门店（收藏/搜索）
   query-nearby-stores   beType=1，searchType=1(收藏) 或 2(搜索)
        │  返回 storeCode / storeName
        ▼
② 选取餐方式（选完门店后）
   ├─ 堂食自提：beType=1, orderType=1（无 beCode）
   ├─ 得来速：  query-nearby-stores(beType=5) 重查 → 取该店 beCode
   └─ 麦乐送：  delivery-query-addresses（选地址）
                → delivery-query-stores（查可配送门店 → 取 beCode）
        ▼
③ query-meals          拉取该门店当前在售菜单
        │  返回 categories（分类）+ meals（餐品名称/价格/标签）
        ▼
④ query-meal-detail    按套餐编码查询套餐内部「轮次」结构
        │  返回 rounds（主食轮 / 小食轮 / 饮品轮，含可替换选项与差价）
        ▼
（匹配算法）          把用户选的汉堡/小食/饮品与每个套餐轮次做模糊比对
        ▼
输出结果              匹配度、价格、换品加价、缺项、省多少钱
```

| Tool | 用途 | 关键入参 | 关键出参 |
|---|---|---|---|
| `query-nearby-stores` | 定位门店（收藏/搜索/得来速） | `beType`、`searchType`、`city`、`keyword` | `storeCode`、`beCode`、`storeName`、`distance` |
| `delivery-query-addresses` | 外卖场景查配送地址 | （无） | `addresses[].addressId`、`contactName`、`fullAddress` |
| `delivery-query-stores` | 外卖场景查可配送门店 | `addressId`、`beType` | `storeCode`、`beCode`、`storeName` |
| `query-meals` | 获取门店在售菜单 | `storeCode`、`orderType`、`beType`、`beCode` | `categories[].meals[].code`、`meals[].name`、`meals[].currentPrice`、`meals[].tags` |
| `query-meal-detail` | 获取套餐内部组成与可替换项 | `storeCode`、`orderType`、`beType`、`code`、`beCode` | `rounds[].name`、`rounds[].choices[].name`、`isDefault`、`diffPrice` |

> 注：三种取餐方式的 `beType` / `orderType` / `beCode` 传递规则不同：
> - 堂食自提 `beType=1`、`orderType=1`，不传 `beCode`；
> - 得来速 `beType=5`、`orderType=1`，必传 `beCode`（从 `beType=5` 的 `query-nearby-stores` 获取）；
> - 麦乐送 `beType=2`、`orderType=2`，必传 `beCode`（从 `delivery-query-stores` 获取）。

### 业务价值

麦当劳菜单里的「套餐」价格往往比单点更划算，但套餐内容固定，用户常常纠结于：

- 我想吃的汉堡、小食、饮品，能不能凑成一个套餐？
- 套餐里的小食/饮品不是我想吃的，换成我要的要不要加钱？
- 到底是点套餐划算，还是直接单点三样划算？

本工具通过「标签式选品 → 自动匹配套餐 → 诚实核算价格」，把这三个问题一次性回答清楚：

1. **标签式**：先选汉堡、再选小食、再选饮品，符合真实点餐心智；
2. **全量匹配**：列出所有相关的套餐，标注命中几样、哪些有偏差、换品需加多少钱；
3. **诚实比价**：区分「套餐实付（含换品加价 + 缺项另点）」与「全单点」，直接给出省钱结论。

这样用户无需在 App 里反复翻菜单、心算差价，就能在「想吃」和「价格」之间快速做出选择。
