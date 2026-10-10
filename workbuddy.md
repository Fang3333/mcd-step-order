# workbuddy.md

> 本项目使用腾讯 WorkBuddy 智能体开发，本文件记录开发过程中的对话上下文与联动方式，用于「WorkBuddy 专项奖励」核验。

## 开发背景

参赛者是一名代码初学者，几乎没有编程经验。在 WorkBuddy 智能体的协助下，从零完成了「麦麦精算师」的构思、开发与调试。

## WorkBuddy 的使用方式

### 1. 创意构思
通过自然语言向 WorkBuddy 描述自己的真实痛点：

> "每次点麦当劳都在「想吃的」和「价格」之间徘徊，能不能做一个先选汉堡、再选小食、再选饮品，然后自动推荐最接近的套餐和价格的工具？"

WorkBuddy 帮助把模糊的想法梳理成清晰的产品流程（标签式点餐 → 套餐匹配 → 诚实比价）。

### 2. 接入麦当劳 MCP
在 WorkBuddy 中通过【自定义连接器】配置麦当劳 MCP Server（`mcd-mcp`），WorkBuddy 自动调用 `query-nearby-stores`、`query-meals`、`query-meal-detail` 等工具，帮助确认真实数据结构与字段含义。

### 3. 开发与调试
WorkBuddy 完成了：
- MCP 客户端（`mcd_client.py`）的编写；
- 套餐匹配算法（`combo_matcher.py`）的设计与实现；
- 交互式主程序（`step_order.py`）的编写；
- 端到端联调，修复了「返回文本 JSON 解析」「二件套不含小食时价格对比不公平」等问题。

### 4. 生成 Skill
基于开发成果，WorkBuddy 产出了可在 WorkBuddy 中直接使用的 Skill（`SKILL.md`），实现「在对话里用自然语言完成标签式点餐」。

## 联动说明

本项目同时满足：
1. **真实使用麦当劳 MCP 能力**：核心流程真实调用麦当劳 MCP 的 3 个 Tool；
2. **真实使用腾讯 WorkBuddy 智能体**：项目由 WorkBuddy 全程协助开发，并产出 WorkBuddy Skill（`SKILL.md`）。
