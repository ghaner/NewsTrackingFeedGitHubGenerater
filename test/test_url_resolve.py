# -*- coding: utf-8 -*-
"""
test_url_resolve.py —— 链接还原逻辑的离线测试（不联网）

功能：用伪造的页面 HTML 验证 resolve_google_link 的三级兜底与退化行为，
      确保解析失败时不会丢条目、也不会把 Google 自身域名误判成原文。
运行：python test/test_url_resolve.py
"""

# ===== 参数配置区 =====
GOOGLE_LINK = "https://news.google.com/rss/articles/CBMiabc?oc=5"
REAL_LINK = "https://www.lmtw.com/mzw/content/detail/id/260004"
# =====================

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import fetch_articles as fa

_passed = 0
_failed = 0


def check(name, cond, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  PASS  %s" % name)
    else:
        _failed += 1
        print("  FAIL  %s %s" % (name, extra))


def with_page(html_text):
    """临时替换网络抓取，返回固定页面。"""
    def _fake(url, timeout=20):
        if isinstance(html_text, Exception):
            raise html_text
        return html_text
    return _fake


def main():
    orig = fa._fetch_url
    orig_batch = fa._batchexecute

    # 1) Google 域名识别
    check("识别 google 跳转链接", fa._is_google_link(GOOGLE_LINK) is True)
    check("普通媒体链接不误判", fa._is_google_link(REAL_LINK) is False)
    check("gstatic 视为 Google", fa._is_google_link("https://lh3.gstatic.com/x") is True)

    # 2) 方式一：data-n-au
    fa._fetch_url = with_page('<c-wiz data-n-au="%s"><a href="/x">y</a></c-wiz>' % REAL_LINK)
    fa._batchexecute = lambda *a, **k: None
    check("data-n-au 还原成功", fa.resolve_google_link(GOOGLE_LINK) == REAL_LINK,
          fa.resolve_google_link(GOOGLE_LINK))

    # 3) 回归：页面里只有统计脚本链接时，不得把它当原文（真踩过的坑）
    fa._fetch_url = with_page('<script src="https://www.google-analytics.com/analytics.js"></script>')
    check("不回落到 analytics 脚本",
          fa.resolve_google_link(GOOGLE_LINK) is None)

    # 3b) 文章链接判定：静态资源与统计域名一律拒绝
    check("拒绝 .js 资源", fa._looks_like_article("https://x.com/a/analytics.js") is False)
    check("拒绝 analytics 域名", fa._looks_like_article("https://www.google-analytics.com/x") is False)
    check("拒绝 google 自身", fa._looks_like_article("https://news.google.com/rss/articles/x") is False)
    check("接受正常文章页", fa._looks_like_article(REAL_LINK) is True)

    # 3c) data-n-au 若指向静态资源，必须拒绝而不是直接返回
    fa._fetch_url = with_page('<c-wiz data-n-au="https://cdn.x.com/app.js"></c-wiz>')
    fa._batchexecute = lambda *a, **k: None
    check("data-n-au 为静态资源时拒绝", fa.resolve_google_link(GOOGLE_LINK) is None)

    # 4) 方式三：用页面签名调 batchexecute
    fa._fetch_url = with_page('<div data-n-a-sg="SIG123" data-n-a-ts="1700000000"></div>')
    fa._batchexecute = lambda link, sg, ts, timeout=15: REAL_LINK if sg == "SIG123" else None
    check("batchexecute 兜底拿原文", fa.resolve_google_link(GOOGLE_LINK) == REAL_LINK)

    # 5) 全部失败 → 返回 None（调用方保留原链接，不丢条目）
    fa._fetch_url = with_page(RuntimeError("network down"))
    fa._batchexecute = lambda *a, **k: None
    check("全链路失败返回 None", fa.resolve_google_link(GOOGLE_LINK) is None)

    # 6) 非 Google 链接原样返回，不发请求
    fa._fetch_url = lambda u, timeout=20: (_ for _ in ()).throw(AssertionError("不该发请求"))
    check("非 Google 链接直接返回", fa.resolve_google_link(REAL_LINK) == REAL_LINK)

    # 7) _filter 退化行为：解析失败仍保留条目，并标记 link_kind
    fa._fetch_url = with_page(RuntimeError("network down"))
    fa._batchexecute = lambda *a, **k: None
    rows = fa._filter([("标题A", GOOGLE_LINK, "2026-11-14T10:00:00+08:00", "摘要A")], None, resolve=True)
    check("解析失败条目仍保留", len(rows) == 1)
    check("解析失败标记为 google_redirect", rows and rows[0]["link_kind"] == "google_redirect")
    check("解析失败保留原链接", rows and rows[0]["url"] == GOOGLE_LINK)

    # 8) 关闭解析时不发请求、url 原样
    rows = fa._filter([("标题B", GOOGLE_LINK, "2026-11-14T10:00:00+08:00", "摘要B")], None, resolve=False)
    check("--no-resolve 保留原链接", rows and rows[0]["url"] == GOOGLE_LINK)
    check("--no-resolve 不标 redirect", rows and rows[0]["link_kind"] == "original")

    fa._fetch_url = orig
    fa._batchexecute = orig_batch

    print("\n结果：%d 通过，%d 失败" % (_passed, _failed))
    return 0 if _failed == 0 else 1


# --- 出口 ---
if __name__ == "__main__":
    sys.exit(main())
