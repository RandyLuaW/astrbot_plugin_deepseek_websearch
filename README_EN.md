# DeepSeek official Search / Fetch as LLM tools

> AstrBot plugin · lets the model decide when to search, how many results, and whether to read one page in depth

## The problem

AstrBot's built-in web search goes through framework backends (tavily / bocha / brave / exa…), each needing its own key, with a generic injection format.

This plugin talks to the **DeepSeek official Search / Fetch APIs** directly:

- Those endpoints are **model-oriented**: the returned `summary` is already written for a model to read — no second summarization pass needed
- Results carry `rerank_score`, so noise can be filtered (official guidance: ≥1.5 is high quality)
- The user's original wording can be passed along for disambiguation
- Built-in 5 QPS throttling with 429 backoff

The chat model doesn't matter — search capability is independent of the provider.

## Features

| Name | Type | Purpose |
|---|---|---|
| `ds_search` | LLM tool | Web search returning summaries |
| `ds_fetch_page` | LLM tool | Fetch a URL's body text (when summaries aren't enough) |
| `/ds搜 keyword` | command | Manual search, no model polish — handy for debugging |

## Install

### Option 1: Plugin marketplace (recommended)

Search "DeepSeek 联网搜索" in the AstrBot WebUI plugin marketplace.

### Option 2: Download from Releases

Grab the zip from [Releases](../../releases), then WebUI → Plugins → Upload.

### Option 3: Clone manually

```bash
cd AstrBot/data/plugins
git clone https://github.com/RandyLuaW/astrbot_plugin_deepseek_websearch.git
```

Requires `httpx` (bundled with AstrBot).

## Configuration

### Basics

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Master switch |
| `api_key` | string | empty | DeepSeek API key |
| `base_url` | string | `https://api.deepseek.com` | Only change for a self-hosted gateway |

### Tool toggles

| Key | Type | Default | Description |
|---|---|---|---|
| `enable_search_tool` | bool | `true` | Enable `ds_search` |
| `enable_fetch_tool` | bool | `true` | Enable `ds_fetch_page` |
| `enable_command` | bool | `true` | Enable the `/ds搜` command |

### Result quality

| Key | Type | Default | Description |
|---|---|---|---|
| `default_count` | int | `5` | Results when the model doesn't specify |
| `max_count` | int | `10` | Hard cap (official limit is 50) |
| `min_rerank_score` | float | `1.5` | Drop results below this score; 0 disables filtering |
| `mark_authority` | bool | `true` | Tag `authority>=3` results as high-authority |
| `max_context_chars` | int | `4000` | Total characters injected into context |
| `max_item_content_chars` | int | `1500` | Per-item body truncation |

### Request control

| Key | Type | Default | Description |
|---|---|---|---|
| `request_timeout` | int | `30` | Per-request timeout in seconds |
| `qps_limit` | float | `4.0` | Requests per second (official cap is 5) |
| `debug` | bool | `false` | Log queries, result counts, retry reasons |

## Important limitation

**Official DeepSeek API only.** Third-party gateways (veloera / new-api) do not expose `/v1/tools/search` or `/v1/tools/fetch` — measured: POST returns 405, GET returns the SPA HTML. Pointing `base_url` at a gateway will not work.

Typical setup: **chat through a gateway (cheap), search through the official API (direct, pay-per-call)**. The plugin's `api_key` is independent of the chat provider's key.

## Relationship with the built-in web search

Don't run both. The framework's `provider_settings.web_search=true` injects its own search tool; having it alongside `ds_search` makes the model oscillate between two tools and wastes schema tokens.

Turn `provider_settings.web_search` off when using this plugin.

## Notes

- Pay-per-call: Search and Fetch are billed separately (~¥5 / 1000 calls). Keep `max_count` modest.
- The official account-level limit is 5 QPS; `qps_limit` defaults to 4.0 for headroom.
- `ds_fetch_page` fetches static HTML only — no JS execution, so client-rendered pages may come back empty.

## License

MIT
