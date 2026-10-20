# DeepSeek 官方搜索 / 抓取接口的 LLM 工具封装

> AstrBot 插件 · 让模型自己决定什么时候搜、搜几条、要不要深读某一篇

## 它解决什么问题

AstrBot 自带的联网搜索走的是框架内那套后端（tavily / bocha / brave / exa…），要单独配 key，且结果注入方式是通用的。

这个插件直接用 **DeepSeek 官方 Search / Fetch 接口**：

- 接口本身是**面向模型生成**的——返回的 `summary` 字段已经是给模型看的摘要，不需要再二次提炼
- 自带 `rerank_score`，可以按相关度剔除噪音（官方口径 1.5 以上为高质量）
- 能回传用户原话做消歧，比只给一个关键词准
- 内置 5 QPS 限流 + 429 退避重试

对话模型是谁无所谓，搜索能力独立于 provider。

## 功能一览

| 名称 | 类型 | 作用 |
|---|---|---|
| `ds_search` | LLM 工具 | 联网搜索，返回带 summary 的结果列表 |
| `ds_fetch_page` | LLM 工具 | 抓取指定 URL 的正文（摘要不够时深读一篇） |
| `/ds搜 关键词` | 指令 | 手动搜索，不做模型润色，方便调试 |

## 安装

### 方式一：插件市场（推荐）

WebUI 插件市场搜索「DeepSeek 联网搜索」。

### 方式二：Releases 下载

从 [Releases](../../releases) 下载 zip，WebUI → 插件 → 上传安装。

### 方式三：手动克隆

```bash
cd AstrBot/data/plugins
git clone https://github.com/RandyLuaW/astrbot_plugin_deepseek_websearch.git
```

依赖：`httpx`（AstrBot 自带）。

## 配置项

### 基础

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 总开关，关闭后模型看不到搜索/抓取工具 |
| `api_key` | string | 空 | DeepSeek API Key（platform.deepseek.com 申请），可与对话同一把 |
| `base_url` | string | `https://api.deepseek.com` | 接口地址，走自建中转时才需要改 |

### 工具开关

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable_search_tool` | bool | `true` | 启用 `ds_search` |
| `enable_fetch_tool` | bool | `true` | 启用 `ds_fetch_page`（建议开：摘要不够时模型自己深读一篇，比一次拉多条全文省） |
| `enable_command` | bool | `true` | 启用 `/ds搜` 指令 |

### 结果质量

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `default_count` | int | `5` | 模型未指定条数时的默认值（建议 3~6，太多挤占上下文） |
| `max_count` | int | `10` | 单次条数上限（官方上限 50，这里收紧） |
| `min_rerank_score` | float | `1.5` | 相关度阈值，低于此值直接丢弃；设 0 不过滤 |
| `mark_authority` | bool | `true` | 给 `authority>=3` 的结果标注「高权威」 |
| `max_context_chars` | int | `4000` | 搜索结果注入上下文的总字数上限 |
| `max_item_content_chars` | int | `1500` | 单条结果附带正文时的截断长度 |

### 请求控制

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `request_timeout` | int | `30` | 单次请求超时（秒），Fetch 抓大页面较慢，不建议低于 20 |
| `qps_limit` | float | `4.0` | 每秒请求上限（官方账号级 5 QPS，这里留余量） |
| `debug` | bool | `false` | 输出查询词、返回条数、重试原因 |

## 重要限制

**只认 DeepSeek 官方 API。** 中转站（veloera / new-api 系）没有 `/v1/tools/search`、`/v1/tools/fetch` 这两个路由，实测 POST 返回 405、GET 落到 SPA HTML。把 `base_url` 指向中转站不会生效。

所以典型用法是：**聊天走中转站（便宜），搜索走官方（直连、按次计费）**。插件配置里的 `api_key` 与对话 provider 是独立的，可以填不同的 key。

## 与框架自带 web_search 的关系

两者**不要同时开**。框架的 `provider_settings.web_search=true` 会注入自己的搜索工具，和本插件的 `ds_search` 并存会让模型在两套工具之间摇摆，也浪费 schema token。

用本插件时把 `provider_settings.web_search` 关掉。

## 注意事项

- 计费按次：Search / Fetch 各自计费（官方约 ¥5/千次），别把 `max_count` 开太大
- 官方限额是账号级 5 QPS，`qps_limit` 默认 4.0 留了余量
- `ds_fetch_page` 只抓正文，不执行 JS；纯前端渲染的页面可能拿不到内容

## License

MIT
