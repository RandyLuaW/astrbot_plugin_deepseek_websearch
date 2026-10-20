"""DeepSeek 网页搜索与抓取：把官方 Search / Fetch 接口包成 LLM 工具。

背景
----
DeepSeek 于 2026-10 开放 Search / Fetch 两个**独立 REST 接口**（不是模型内置工具）：

    POST https://api.deepseek.com/v1/tools/search
    POST https://api.deepseek.com/v1/tools/fetch

它们只借用了 OpenAI 的鉴权头与错误体格式，业务结构完全自定义，因此**无法通过
配置 provider 接入**——必须在框架层包成 function calling 工具，交给模型自主调用。
本插件就干这件事。

设计要点
--------
1. **用 summary 不用 snippet**：Search 同时返回 snippet(100~200 字，给人看的) 与
   summary(600~800 字，官方明确说明是"面向模型生成的")。注入上下文一律用 summary，
   用错字段要么信息不足要么白烧 token。
2. **默认 need_content=false**：content 字段仅在显式要求时才有值。模型看摘要不够时
   可再调 ds_fetch_page 抓单页正文，比一次性拉 10 篇全文省一个数量级。
3. **按 rerank_score 过滤**：官方口径 >=1.5 视为高质量结果。低于阈值的直接丢弃，
   顺带把上下文长度压下来。authority>=3 标注"高权威"，帮助模型判断该信谁。
4. **传 ori_question**：模型把用户问题压缩成 query 时容易丢歧义，回传用户原话
   (1~128 字) 能让搜索引擎正确消歧。这是官方文档里被低估的字段。
5. **QPS 硬限制**：账号级 Search / Fetch 各 5 QPS，超限 429。内置滑动窗口限流
   + 429/5xx 退避重试，避免模型一轮并发多工具时打爆。
6. **计费安全垫**：Search 仅在返回非空 results 时计费，Fetch 返回 404（无可提取正文）
   也不计费。所以过滤空结果天然省钱，失败不心疼。

与 AstrBot 核心搜索的关系
------------------------
核心 `astrbot/core/tools/web_search_tools.py` 自带 tavily/bocha/brave/firecrawl/
baidu_ai_search/exa/anysearch 七个后端，**没有 deepseek**。两者不要同时开启：
`provider_settings.web_search=true` 时模型会同时看到两套搜索工具，容易乱选、重复搜。
本插件的工具名刻意加了 `ds_` 前缀，便于区分。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

import aiohttp

from astrbot import logger
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_deepseek_websearch"
DEFAULT_BASE_URL = "https://api.deepseek.com"

DEFAULT_CONFIG: dict = {
    "enabled": True,
    "api_key": "",
    "base_url": DEFAULT_BASE_URL,
    "request_timeout": 30,
    "qps_limit": 4.0,
    "default_count": 5,
    "max_count": 10,
    "min_rerank_score": 1.5,
    "mark_authority": True,
    "max_context_chars": 4000,
    "max_item_content_chars": 1500,
    "enable_search_tool": True,
    "enable_fetch_tool": True,
    "enable_command": True,
    "debug": False,
}


class _RateLimiter:
    """滑动窗口限流：1 秒内最多放行 qps 个请求。"""

    def __init__(self, qps: float) -> None:
        self.qps = max(0.5, float(qps))
        self._ts: deque[float] = deque()
        self._lock = asyncio.Lock()

    def _trim(self, now: float) -> None:
        while self._ts and now - self._ts[0] > 1.0:
            self._ts.popleft()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._trim(now)
            if len(self._ts) >= self.qps:
                wait = 1.0 - (now - self._ts[0]) + 0.01
                if wait > 0:
                    await asyncio.sleep(wait)
                now = time.monotonic()
                self._trim(now)
            self._ts.append(time.monotonic())


class DeepSeekSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.cfg: dict = dict(DEFAULT_CONFIG)
        if config:
            for k, v in dict(config).items():
                if k in DEFAULT_CONFIG:
                    self.cfg[k] = v
        self._limiter = _RateLimiter(self.cfg["qps_limit"])
        self._session: aiohttp.ClientSession | None = None

    # ---------------- 基础设施 ----------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=float(self.cfg["request_timeout"])),
                trust_env=True,
            )
        return self._session

    async def terminate(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _api_key(self) -> str:
        return str(self.cfg.get("api_key") or "").strip()

    def _endpoint(self, path: str) -> str:
        base = str(self.cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        return f"{base}{path}"

    async def _post(self, path: str, payload: dict[str, Any]) -> tuple[bool, Any]:
        """调用 DeepSeek REST 接口。返回 (是否成功, 数据或错误说明)。"""
        key = self._api_key()
        if not key:
            return False, "未配置 DeepSeek API Key，请到插件配置里填写 api_key"

        url = self._endpoint(path)
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        last_err = ""
        for attempt in range(3):
            await self._limiter.acquire()
            try:
                session = await self._get_session()
                async with session.post(url, json=payload, headers=headers) as resp:
                    status = resp.status
                    text = await resp.text()
                    if status == 200:
                        try:
                            import json

                            return True, json.loads(text)
                        except Exception:
                            return True, {}
                    if status == 401:
                        return False, "DeepSeek API Key 无效或已过期（401）"
                    if status == 404:
                        # Fetch 侧：无可提取正文，且不计费
                        return False, "该网页没有可提取的正文内容（404，未计费）"
                    if status == 429:
                        last_err = "触发限流（429）"
                        await asyncio.sleep(0.8 * (attempt + 1))
                        continue
                    if status >= 500:
                        last_err = f"服务端错误（{status}）"
                        await asyncio.sleep(0.6 * (attempt + 1))
                        continue
                    last_err = f"请求失败（{status}）{text[:160]}"
                    break
            except TimeoutError:
                last_err = f"请求超时（{self.cfg['request_timeout']}s）"
            except asyncio.TimeoutError:
                last_err = f"请求超时（{self.cfg['request_timeout']}s）"
            except Exception as exc:  # noqa: BLE001
                last_err = f"请求异常：{exc}"
            if self.cfg.get("debug"):
                logger.warning(f"[{PLUGIN_NAME}] {path} 第 {attempt + 1} 次失败：{last_err}")
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
        return False, last_err

    # ---------------- 结果整形 ----------------

    def _format_results(self, results: list[dict], query: str) -> str:
        min_score = float(self.cfg.get("min_rerank_score") or 0)
        kept: list[dict] = []
        dropped = 0
        for item in results:
            if not item.get("url"):
                continue
            score = item.get("rerank_score")
            if isinstance(score, (int, float)) and score < min_score:
                dropped += 1
                continue
            kept.append(item)

        if not kept:
            if results:
                return f"搜索「{query}」返回 {len(results)} 条结果，但相关度均低于阈值 {min_score}。可换更具体的关键词重试，或直接使用已知信息回答。"
            return f"搜索「{query}」没有找到结果。"

        limit = int(self.cfg.get("max_context_chars") or 4000)
        max_item_content = int(self.cfg.get("max_item_content_chars") or 1500)
        mark_auth = bool(self.cfg.get("mark_authority", True))

        lines: list[str] = []
        used = 0
        for idx, item in enumerate(kept, 1):
            title = (item.get("title") or "无标题").strip()
            url = item.get("url") or ""
            # 优先 summary（面向模型生成），退回 snippet
            body = (item.get("summary") or item.get("snippet") or "").strip()
            meta: list[str] = []
            if item.get("site_name"):
                meta.append(str(item["site_name"]))
            if item.get("date"):
                meta.append(str(item["date"])[:10])
            score = item.get("rerank_score")
            if isinstance(score, (int, float)):
                meta.append(f"相关度{score:g}")
            auth = item.get("authority")
            if mark_auth and isinstance(auth, int):
                if auth >= 3:
                    meta.append("高权威")
                elif auth < 0:
                    meta.append("权威未知")

            block = f"[{idx}] {title}\n{url}\n" + (f"{' | '.join(meta)}\n" if meta else "") + body
            content = (item.get("content") or "").strip()
            if content:
                block += "\n正文摘录：" + content[:max_item_content]

            if used + len(block) > limit:
                lines.append(f"（其余 {len(kept) - idx + 1} 条结果因长度限制未展开）")
                break
            lines.append(block)
            used += len(block)

        header = f"搜索「{query}」共 {len(kept)} 条可用结果"
        if dropped:
            header += f"（已过滤 {dropped} 条低相关）"
        header += "。引用时请附上链接，不要编造未出现在结果里的内容。"
        return header + "\n\n" + "\n\n".join(lines)

    # ---------------- LLM 工具 ----------------

    @filter.llm_tool(name="ds_search")
    async def ds_search(
        self,
        event: AstrMessageEvent,
        query: str,
        count: int = 0,
        need_content: bool = False,
        ori_question: str = "",
    ) -> str:
        """联网搜索最新信息。当你需要可能超出训练数据时效的事实（新闻、价格、版本发布、赛事结果、实时政策等），或对方明确要求"查一下""搜一下""网上怎么说"时调用。返回结构化搜索结果（标题、链接、摘要），失败时返回原因，请据此自然作答，不要暴露工具名。

        Args:
            query(string): 必填。搜索词，1~128 个字符。支持 site:域名、-site:域名、lang:zh、after:2025-01-01、before:2025-12-31 等语法，可组合使用。
            count(number): 可选。返回条数，1~10，默认取插件配置值（通常 5）。需要交叉验证时用 6~8，简单事实用 3。传 0 表示用默认值。
            need_content(boolean): 可选。是否同时返回网页正文。默认 false 只给摘要；只有当你需要精确原文（引文、代码、条款）时才设为 true，否则会占用大量上下文。
            ori_question(string): 可选。用户的原始问题原话（1~128 字）。当你的 query 是压缩过的关键词时强烈建议填写，能显著提升搜索准确度。
        """
        if not self.cfg.get("enabled", True) or not self.cfg.get("enable_search_tool", True):
            return "联网搜索功能当前已关闭，请直接基于已有知识回答。"
        q = (query or "").strip()
        if not q:
            return "搜索词为空，请提供要搜索的内容。"

        max_count = int(self.cfg.get("max_count") or 10)
        default_n = int(self.cfg.get("default_count") or 5)
        # schema 里 count 是 JSON number，模型可能传浮点、字符串或 null
        try:
            n = int(count) if count not in (None, "") else default_n
        except (TypeError, ValueError):
            n = default_n
        if n <= 0:
            n = default_n
        n = max(1, min(n, max_count, 50))

        want_content = need_content
        if isinstance(want_content, str):
            want_content = want_content.strip().lower() in ("1", "true", "yes", "y", "是")

        payload: dict[str, Any] = {
            "query": q[:128],
            "count": n,
            "need_content": bool(want_content),
        }
        oq = (ori_question or "").strip()
        if oq:
            payload["ori_question"] = oq[:128]

        ok, data = await self._post("/v1/tools/search", payload)
        if not ok:
            return f"联网搜索失败：{data}"
        results = (data or {}).get("results") or []
        if self.cfg.get("debug"):
            logger.info(f"[{PLUGIN_NAME}] search query={q!r} count={n} 返回 {len(results)} 条")
        return self._format_results(results, q)

    @filter.llm_tool(name="ds_fetch_page")
    async def ds_fetch_page(self, event: AstrMessageEvent, url: str) -> str:
        """抓取指定网页的正文（转成 Markdown）。通常在 ds_search 给出的链接里，摘要不足以回答问题时，用本工具深入读某一篇。不要用它去读搜索结果之外的随机网址。

        Args:
            url(string): 必填。要抓取的网页地址，必须是含域名的 http/https 链接，长度不超过 8192 字符。
        """
        if not self.cfg.get("enabled", True) or not self.cfg.get("enable_fetch_tool", True):
            return "网页抓取功能当前已关闭。"
        u = (url or "").strip()
        if not (u.startswith("http://") or u.startswith("https://")):
            return "网址格式不对，需要 http:// 或 https:// 开头的完整链接。"

        ok, data = await self._post("/v1/tools/fetch", {"url": u[:8192]})
        if not ok:
            return f"网页抓取失败：{data}"

        title = ((data or {}).get("title") or "").strip() or "无标题"
        content = ((data or {}).get("content") or "").strip()
        if not content:
            return f"《{title}》没有可读正文，可能是图片站、需要登录或被反爬拦截。"

        limit = int(self.cfg.get("max_context_chars") or 4000) * 2
        truncated = len(content) > limit
        body = content[:limit]
        tail = "\n\n（正文过长已截断，如需更多内容请针对具体段落再提问）" if truncated else ""
        return f"《{title}》\n{u}\n\n{body}{tail}"

    # ---------------- 手动指令 ----------------

    @filter.command("ds搜")
    async def ds_search_command(self, event: AstrMessageEvent, query: str = "") -> Any:
        """手动联网搜索：/ds搜 关键词"""
        if not self.cfg.get("enable_command", True):
            yield event.plain_result("手动搜索指令已关闭。")
            return
        q = (query or "").strip()
        if not q:
            # 支持引用消息当搜索词
            q = (event.message_str or "").replace("/ds搜", "").strip()
        if not q:
            yield event.plain_result("用法：/ds搜 关键词")
            return

        n = int(self.cfg.get("default_count") or 5)
        ok, data = await self._post(
            "/v1/tools/search", {"query": q[:128], "count": n, "need_content": False}
        )
        if not ok:
            yield event.plain_result(f"搜索失败：{data}")
            return

        results = (data or {}).get("results") or []
        if not results:
            yield event.plain_result(f"没有搜到「{q}」相关结果。")
            return

        min_score = float(self.cfg.get("min_rerank_score") or 0)
        lines = [f"「{q}」搜索结果（{len(results)} 条）："]
        shown = 0
        for item in results:
            score = item.get("rerank_score")
            if isinstance(score, (int, float)) and score < min_score:
                continue
            shown += 1
            if shown > 8:
                break
            title = (item.get("title") or "无标题").strip()
            summary = (item.get("summary") or item.get("snippet") or "").strip()
            if len(summary) > 120:
                summary = summary[:120] + "…"
            lines.append(f"\n{shown}. {title}\n{item.get('url', '')}\n{summary}")
        if shown == 0:
            lines.append("\n（结果相关度均低于阈值，可换更具体的关键词）")
        yield event.plain_result("\n".join(lines))
