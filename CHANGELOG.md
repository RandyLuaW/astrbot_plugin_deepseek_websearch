# 更新日志

所有重要变更记录于此。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-10-20

### 新增

- 首个公开版本。
- `ds_search`：DeepSeek 官方 Search 接口封装成 LLM 工具，注入官方面向模型生成的 `summary` 字段。
- `ds_fetch_page`：抓取指定 URL 正文，供模型在摘要不足时深读单篇。
- `/ds搜` 指令：手动触发搜索，不做模型润色。
- 按 `rerank_score` 过滤低相关结果（默认阈值 1.5），支持标注高权威来源。
- 内置 QPS 限流（默认 4.0）与 429 退避重试。
- 搜索结果注入有总字数与单条截断上限，避免挤占上下文。

### 已知限制

- 只支持 DeepSeek 官方 API。中转站（veloera / new-api）没有 `/v1/tools/search`、`/v1/tools/fetch` 路由，改 `base_url` 不会生效。
- 与框架自带 `provider_settings.web_search` 不可同时启用。
