"""DeepSeek 搜索插件离线单测。

不依赖 AstrBot 运行时，也不发真实网络请求：内置 astrbot 模块最小 mock，
所有 HTTP 调用用 monkeypatch 替换。

跑法（需要 aiohttp，用 AstrBot 自己的 venv）：
    AstrBot/.venv/Scripts/python.exe tests/test_plugin.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "main.py"


# ----------------------------------------------------------------------
# astrbot 最小 mock（必须在导入 main.py 前装好）
# ----------------------------------------------------------------------


class _Logger:
    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _install_astrbot_mock() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    astrbot.logger = _Logger()

    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object

    class _Filter:
        @staticmethod
        def llm_tool(*a, **k):
            def deco(fn):
                return fn

            return deco

        @staticmethod
        def command(*a, **k):
            def deco(fn):
                return fn

            return deco

    event.filter = _Filter

    star = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    star.Context = Context
    star.Star = Star

    api.event = event
    api.star = star

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = star


_install_astrbot_mock()

spec = importlib.util.spec_from_file_location("ds_search_main", PLUGIN_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules["ds_search_main"] = mod
spec.loader.exec_module(mod)

Plugin = mod.DeepSeekSearchPlugin

# ----------------------------------------------------------------------
# 测试脚手架
# ----------------------------------------------------------------------

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}  {detail}")


def make_plugin(**overrides):
    cfg = dict(mod.DEFAULT_CONFIG)
    cfg["api_key"] = "test-key"
    cfg.update(overrides)
    return Plugin(None, cfg)


def fake_post(capture: dict, ok=True, data=None):
    async def _post(path, payload):
        capture["path"] = path
        capture["payload"] = payload
        return ok, (data if data is not None else {"results": []})

    return _post


def item(title, score=2.0, auth=2, summary="S" * 100, snippet="P" * 50, url=None, **kw):
    d = {
        "title": title,
        "url": url or f"https://example.com/{title}",
        "snippet": snippet,
        "summary": summary,
        "content": "",
        "site_name": "示例站",
        "date": "2026-10-01T00:00:00Z",
        "rerank_score": score,
        "authority": auth,
    }
    d.update(kw)
    return d


# ----------------------------------------------------------------------
# 用例
# ----------------------------------------------------------------------


def test_format_prefers_summary():
    p = make_plugin()
    out = p._format_results([item("A", summary="MODEL_SUMMARY", snippet="HUMAN_SNIPPET")], "q")
    check("注入用 summary 而非 snippet", "MODEL_SUMMARY" in out and "HUMAN_SNIPPET" not in out, out[:200])


def test_format_filters_low_score():
    p = make_plugin(min_rerank_score=1.5)
    out = p._format_results([item("good", score=2.2), item("bad", score=0.8)], "q")
    check("低相关结果被过滤", "good" in out and "bad" not in out, out[:200])
    check("提示过滤条数", "已过滤 1 条" in out, out[:120])


def test_format_all_filtered():
    p = make_plugin(min_rerank_score=1.5)
    out = p._format_results([item("bad", score=0.1)], "q")
    check("全部低相关时给出降级提示", "低于阈值" in out, out[:160])


def test_format_empty():
    p = make_plugin()
    out = p._format_results([], "q")
    check("空结果提示", "没有找到结果" in out, out)


def test_format_authority_mark():
    p = make_plugin(mark_authority=True)
    out = p._format_results([item("x", auth=4)], "q")
    check("高权威标注", "高权威" in out, out[:200])
    p2 = make_plugin(mark_authority=False)
    out2 = p2._format_results([item("x", auth=4)], "q")
    check("关闭标注后不出现", "高权威" not in out2, "")


def test_format_truncates():
    p = make_plugin(max_context_chars=500)
    items = [item(f"t{i}", summary="X" * 400) for i in range(6)]
    out = p._format_results(items, "q")
    check("超出字数上限时截断", len(out) < 2000 and "未展开" in out, f"len={len(out)}")


def test_no_api_key():
    p = make_plugin(api_key="")

    async def run():
        return await p.ds_search(None, "测试")

    out = asyncio.run(run())
    check("未配 key 时友好报错", "未配置" in out, out[:120])


def test_query_truncated():
    p = make_plugin()
    cap: dict = {}
    p._post = fake_post(cap)
    long_q = "问" * 300

    async def run():
        return await p.ds_search(None, long_q, count=3, ori_question="原话" * 100)

    asyncio.run(run())
    check("query 截断到 128", len(cap["payload"]["query"]) == 128, str(len(cap["payload"]["query"])))
    check("ori_question 截断到 128", len(cap["payload"]["ori_question"]) == 128, "")
    check("count 透传", cap["payload"]["count"] == 3, str(cap["payload"]))
    check("请求打到 /v1/tools/search", cap["path"] == "/v1/tools/search", cap["path"])


def test_count_clamped():
    p = make_plugin(max_count=10)
    cap: dict = {}
    p._post = fake_post(cap)

    async def run():
        await p.ds_search(None, "q", count=999)

    asyncio.run(run())
    check("count 被 max_count 收紧", cap["payload"]["count"] == 10, str(cap["payload"]["count"]))


def test_count_zero_uses_default():
    p = make_plugin(default_count=5)
    cap: dict = {}
    p._post = fake_post(cap)

    async def run():
        await p.ds_search(None, "q", count=0)

    asyncio.run(run())
    check("count=0 时用配置默认值", cap["payload"]["count"] == 5, str(cap["payload"]["count"]))


def test_fetch_url_validation():
    p = make_plugin()

    async def run(u):
        return await p.ds_fetch_page(None, u)

    out = asyncio.run(run("ftp://x.com/a"))
    check("非法协议被拒", "网址格式不对" in out, out[:120])
    out2 = asyncio.run(run(""))
    check("空网址被拒", "网址格式不对" in out2, out2[:120])


def test_fetch_success():
    p = make_plugin()
    cap: dict = {}
    p._post = fake_post(cap, data={"title": "标题", "content": "# 正文\n内容"})

    async def run():
        return await p.ds_fetch_page(None, "https://example.com/a")

    out = asyncio.run(run())
    check("抓取走 /v1/tools/fetch", cap["path"] == "/v1/tools/fetch", cap["path"])
    check("标题与正文都返回", "标题" in out and "正文" in out, out[:150])


def test_fetch_empty_content():
    p = make_plugin()
    p._post = fake_post({}, data={"title": "空页", "content": ""})

    async def run():
        return await p.ds_fetch_page(None, "https://example.com/a")

    out = asyncio.run(run())
    check("空正文有明确说明", "没有可读正文" in out, out[:120])


def test_post_without_key_short_circuits():
    p = make_plugin(api_key="")

    async def run():
        return await p._post("/v1/tools/search", {"query": "x"})

    ok, msg = asyncio.run(run())
    check("_post 无 key 直接失败", ok is False and "未配置" in msg, str(msg))


def test_need_content_passthrough():
    p = make_plugin()
    cap: dict = {}
    p._post = fake_post(cap)

    async def run():
        await p.ds_search(None, "q", need_content=True)

    asyncio.run(run())
    check("need_content 透传", cap["payload"]["need_content"] is True, str(cap["payload"]))


def test_disabled_switch():
    p = make_plugin(enabled=False)

    async def run():
        return await p.ds_search(None, "q")

    out = asyncio.run(run())
    check("总开关关闭时不搜索", "已关闭" in out, out[:120])


def test_tool_disabled_switch():
    p = make_plugin(enable_search_tool=False)

    async def run():
        return await p.ds_search(None, "q")

    out = asyncio.run(run())
    check("单工具开关关闭时不搜索", "已关闭" in out, out[:120])


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"运行 {len(tests)} 个用例\n")
    for fn in tests:
        print(f"- {fn.__name__}")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            FAILED.append(fn.__name__)
            print(f"  [ERROR] {fn.__name__}: {exc}")
    print(f"\n通过 {len(PASSED)} / 失败 {len(FAILED)}")
    if FAILED:
        print("失败项：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
