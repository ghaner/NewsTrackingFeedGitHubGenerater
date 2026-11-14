# -*- coding: utf-8 -*-
"""
通用「网址 → RSS」生成器（配置驱动，零依赖，抗反爬）
====================================================
把任意「按更新排序的列表页」做成标准 RSS 2.0，每个网址一个 <name>.xml。
全部逻辑由仓库根目录的 sources.json 驱动：新增一个网址 = 在 sources 里加一条配置，
无需改代码（简单站点用 parser:"generic" + 正则；复杂站点可加一个专用 parser 函数）。

设计要点（本 skill 方法论核心）：
  - 仅 Python 标准库（urllib + re + xml），默认无需 pip install；
  - 可选 Playwright 浏览器抓取（fetch:"browser"）用于过 Cloudflare / cdndefend JS 算力挑战、
    以及等待 SPA 渲染完再取 HTML；
  - 反爬韧性：命中挑战页特征或提取 0 条时，跳过该源、保留上一次成功的 feed，订阅不中断；
  - 输出标准 RSS 2.0，由 GitHub Pages 以 https://<用户>.github.io/<仓库>/<name>.xml 提供公网订阅 URL。

用法：python generate_feed.py
输出：仓库根目录 <name>.xml（每源一个文件）

字段说明（seedog 示例）：
  poster 海报图URL(绝对) | link 详情绝对链接 | title 片名
  meta 年份/类型/地区/语言/主演 | cats 分类 | douban 豆瓣链接(绝对) | douban_rate 豆瓣评分
"""

import os
import re
import json
import datetime
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

# ===================== 路径解析（无论从哪个工作目录调用都正确） =====================
# 本脚本约定放在项目 src/ 下；CONFIG_PATH = 同目录的 sources.json；REPO_ROOT = 上一层（仓库根）。
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
CONFIG_PATH = os.path.join(SCRIPT_DIR, "sources.json")


# ===================== 反爬挑战页特征（命中则跳过该源，保留旧 feed） =====================
# 这些是常见 JS 算力挑战 / Cloudflare 质询页面的指纹。命中说明没拿到真实内容。
CHALLENGE_MARKERS = [
    "cdndefend", "verifying your browser", "just a moment",
    "cf-mitigated", "challenges.cloudflare.com",
]


# ===================== 通用工具 =====================
TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(html):
    """去掉 HTML 标签，返回纯文本。"""
    if html is None:
        return ""
    return TAG_RE.sub("", html).strip()


# ===================== 解析器（PARSERS 字典可扩展） =====================
def parse_seedog(html, cfg):
    """seedog.cc / sidhub.cc 专用解析（已在真实页面验证）。返回 item 列表。

    每个影片在源码里是一个 <div class="cover">…</div> 块；块内 <ul><li>…</li></ul>
    依次承载：标题 / 年份类型地区语言主演 / 分类 / 豆瓣。
    """
    base = cfg.get("base", "")
    COVER_RE = re.compile(r'<div class="cover">(.*?</ul>\s*</div>)', re.S)
    UL_RE = re.compile(r"<ul>(.*?)</ul>", re.S)
    LI_RE = re.compile(r"<li>(.*?)</li>", re.S)
    IMG_RE = re.compile(r'<img[^>]*src="([^"]+)"', re.S)
    IMAGE_HREF_RE = re.compile(r'class="image"[^>]*href="([^"]+)"', re.S)
    TITLE_RE = re.compile(r'header-anchor">#</a>\s*([^<]+)</h2>', re.S)
    HREF_RE = re.compile(r'href="([^"]+)"', re.S)

    items = []
    for block in COVER_RE.findall(html):
        poster = IMG_RE.search(block)
        poster = poster.group(1) if poster else ""

        detail = IMAGE_HREF_RE.search(block)
        detail_rel = detail.group(1) if detail else ""
        detail_abs = detail_rel if detail_rel.startswith("http") else base + detail_rel

        title_m = TITLE_RE.search(block)
        title = strip_tags(title_m.group(1)) if title_m else "(未知片名)"

        ul = UL_RE.search(block)
        meta = cats = douban_link = douban_rate = ""
        if ul:
            lis = LI_RE.findall(ul.group(1))
            # lis[0]=含 h2 标题; lis[1]=年份/类型/地区/语言/主演; lis[2]=分类; lis[3]=豆瓣
            if len(lis) > 1:
                meta = strip_tags(lis[1])
            if len(lis) > 2:
                cats = strip_tags(lis[2])
            if len(lis) > 3:
                douban = lis[3]
                dm = HREF_RE.search(douban)
                douban_link = dm.group(1) if dm else ""
                rm = re.search(r">([^<]*)</a>", douban)
                douban_rate = rm.group(1).strip() if rm else ""

        rate = douban_rate if (douban_rate and douban_rate != "0") else "暂无"
        items.append({
            "poster": poster,
            "link": detail_abs,
            "title": title,
            "meta": meta,
            "cats": cats,
            "douban": douban_link,
            "douban_rate": douban_rate,
            "rate": rate,
        })
    return items


def _rfc822(iso_str):
    """把 ISO 8601 时间转成 RSS 要求的 RFC822；失败返回空串（该条不写 pubDate）。"""
    if not iso_str:
        return ""
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        return dt.strftime("%a, %d %b %Y %H:%M:%S %z")
    except Exception:
        return ""


def parse_event_json(text, cfg):
    """事件时间轴解析器：读取 news-timeline-builder 产出的 event.json，
    把每个时间轴节点渲染成一条 feed 条目（配合 fetch:"file"）。

    与 generic 的区别：来源不是网页而是结构化 JSON，因此不需要 item_block/fields 正则，
    直接按 timeline 数组生成条目；节点自带时间、置信度、冲突标记与多来源链接。
    order:"desc"（默认，最新在前）/ "asc"。
    """
    data = json.loads(text)
    nodes = data.get("timeline") or []
    nodes = sorted(nodes, key=lambda n: n.get("timestamp_sort") or 0,
                   reverse=(cfg.get("order", "desc") == "desc"))
    items = []
    for n in nodes:
        label = n.get("event_label") or "(未命名进展)"
        ts = (n.get("timestamp_local") or "")[:10]
        facts = n.get("facts") or {}
        lines = []
        for k, vers in facts.items():
            if isinstance(vers, list) and vers:
                vals = " / ".join(str(v.get("value", "")) for v in vers)
                if len(vers) > 1:
                    vals = "【存在分歧】" + vals
            else:
                vals = str(vers)
            lines.append("%s：%s" % (k, vals))
        sources = n.get("sources") or []
        link = sources[0] if sources else (cfg.get("link_fallback") or "")
        node_id = n.get("node_id") or label
        items.append({
            "title": ("[%s] %s" % (ts, label)) if ts else label,
            "label": label,
            "link": link,
            "guid": "%s#%s" % (link, node_id),
            "summary": n.get("summary") or "",
            "facts": "；".join(lines) if lines else "（该节点无结构化事实）",
            "confidence": str(n.get("confidence", "")),
            "conflict": "是" if n.get("conflict") else "否",
            "sources_text": "、".join(sources) if sources else "（无）",
            "published": ts,
            "pubDate": _rfc822(n.get("timestamp_local") or ""),
            "category": "存在冲突" if n.get("conflict") else "事件进展",
        })
    return items


def parse_generic(html, cfg):
    """通用配置驱动解析：item_block 捕获条目块，fields 定义各字段正则。
    适用结构较规整的站点；字段支持 group / strip_tags / abs(补全绝对链接) / i(忽略大小写)。

    item_block 中一个捕获组 = 一个条目块；fields 里每个字段用一次正则 search 提取。
    """
    ib = re.compile(cfg["item_block"], re.S)
    field_cfgs = cfg.get("fields", {})
    base = cfg.get("base", "")
    items = []
    for block in ib.findall(html):
        item = {}
        for fname, fc in field_cfgs.items():
            flags = re.S | (re.I if fc.get("i") else 0)
            m = re.search(fc["pattern"], block, flags)
            val = ""
            if m:
                val = m.group(fc.get("group", 1))
                if fc.get("strip_tags"):
                    val = strip_tags(val)
                if fc.get("abs"):
                    val = val if val.startswith("http") else base + val
            item[fname] = val
        if "douban_rate" in item:
            item["rate"] = item["douban_rate"] if (item["douban_rate"] and item["douban_rate"] != "0") else "暂无"
        items.append(item)
    return items


# 注册解析器：新增专用站点时，在这里加一个函数并登记到字典即可（无需改其它代码）。
PARSERS = {
    "seedog": parse_seedog,
    "generic": parse_generic,
    "event_json": parse_event_json,
}


# ===================== 网络与构建 =====================
def fetch(url, ua, timeout):
    """urllib 直连（fetch:"http"，默认）。最快，适合无 JS 挑战的站点。"""
    req = Request(url, headers={"User-Agent": ua, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    enc = resp.headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def fetch_browser(url, ua, timeout, max_wait=40, wait_selector=None, settle=5):
    """用 Playwright 无头浏览器跑 JS 通过反爬挑战（cdndefend / Cloudflare 等），返回渲染后的真实 HTML。
    仅在源配置 fetch:"browser" 时调用；playwright 未安装或抓取失败会抛异常，由上层捕获后跳过该源。

    wait_selector: 挑战解除后，再等待该 CSS 选择器出现（SPA 站点用它确认内容已渲染）。
    settle: 额外稳定等待秒数，确保异步数据写入 DOM。
    """
    import time
    from playwright.sync_api import sync_playwright

    html = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            user_agent=ua,
            locale="zh-CN",
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        # 抹掉 webdriver 特征，降低被识别为自动化的概率
        page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        # 轮询等待挑战解除（JS 算力挑战通常需要几秒）
        deadline = time.time() + max_wait
        while time.time() < deadline:
            html = page.content()
            low = html.lower()
            if not any(m in low for m in CHALLENGE_MARKERS):
                break
            time.sleep(2)
        # SPA 站点：等内容选择器出现 + 稳定时间，否则拿到的是空壳
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=timeout * 1000)
            except Exception:
                pass
        if settle:
            time.sleep(settle)
        html = page.content()
        browser.close()
    return html


def build_rss(items, cfg):
    """根据 items 与配置拼出标准 RSS 2.0 字符串。"""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    tpl = cfg.get("template", "<div>{title}<br>{link}</div>")
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
             '  <channel>']
    parts.append('    <title>%s</title>' % escape(cfg["feed_title"]))
    # channel<link> 必须是公网地址；file 模式下 cfg["url"] 是本地路径，不可直接写入，
    # 由 home_url（站点首页）/ feed_url（feed 自身地址）提供，缺省则省略 <link>。
    home = cfg.get("home_url") or cfg.get("url", "")
    if str(home).startswith("http"):
        parts.append('    <link>%s</link>' % escape(home))
    if cfg.get("feed_url"):
        parts.append('    <atom:link href="%s" rel="self" type="application/rss+xml"/>' % escape(cfg["feed_url"]))
    parts.append('    <description>%s</description>' % escape(cfg.get("feed_desc", "")))
    parts.append('    <language>zh-CN</language>')
    parts.append('    <lastBuildDate>%s</lastBuildDate>' % now)
    parts.append('    <ttl>%d</ttl>' % cfg.get("ttl", 60))

    for it in items:
        # template 中的 {字段名} 替换为提取结果；escape 防止破坏 XML
        desc = tpl.format(**{k: escape(str(v)) for k, v in it.items()})
        parts.append('    <item>')
        parts.append('      <title>%s</title>' % escape(it.get("title", "")))
        parts.append('      <link>%s</link>' % escape(it.get("link", "")))
        # guid：优先用条目自带 guid（同源多节点时避免重复），否则回落 link
        parts.append('      <guid isPermaLink="false">%s</guid>' % escape(it.get("guid") or it.get("link", "")))
        if it.get("pubDate"):
            parts.append('      <pubDate>%s</pubDate>' % escape(it["pubDate"]))
        if it.get("category"):
            parts.append('      <category>%s</category>' % escape(it["category"]))
        parts.append('      <description><![CDATA[%s]]></description>' % desc)
        parts.append('    </item>')

    parts.append('  </channel>')
    parts.append('</rss>')
    return "\n".join(parts)


def process_source(cfg, ua, timeout):
    """处理单个源：抓取 → 反爬校验 → 解析 → 落盘。任何一步失败都安全跳过（保留旧 feed）。"""
    name = cfg["name"]
    print("\n=== 处理源：%s (%s) ===" % (name, cfg.get("url", "")))
    if cfg.get("enabled") is False:
        print("  ⏭️  已在配置中禁用（enabled:false），跳过。")
        return
    parser = PARSERS.get(cfg.get("parser", "generic"))
    if parser is None:
        print("  ❌ 未知 parser: %s，跳过。" % cfg.get("parser"))
        return

    fetch_mode = cfg.get("fetch", "http")
    try:
        if fetch_mode == "browser":
            print("  🌐 使用浏览器抓取（攻克 JS 挑战）...")
            html = fetch_browser(
                cfg["url"], ua, timeout,
                max_wait=cfg.get("max_wait", 40),
                wait_selector=cfg.get("wait_selector"),
                settle=cfg.get("settle", 5),
            )
        elif fetch_mode == "file":
            # 文件模式：url 是仓库内相对路径（如 data/event.json），读本地结构化数据，不联网
            path = cfg["url"] if os.path.isabs(cfg["url"]) else os.path.join(REPO_ROOT, cfg["url"])
            print("  📄 读取本地数据：%s" % path)
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()
        else:
            html = fetch(cfg["url"], ua, timeout)
    except Exception as e:
        print("  ⚠️  抓取失败：%s（跳过，保留旧 feed）" % e)
        return

    low = html.lower()
    if any(m in low for m in CHALLENGE_MARKERS):
        print("  ⚠️  命中反爬挑战页（如 cdndefend/Cloudflare），无法获取真实内容，跳过。")
        return

    items = parser(html, cfg)
    if cfg.get("limit"):
        items = items[: cfg["limit"]]
    print("      提取到 %d 条" % len(items))
    if not items:
        print("  ⚠️  未提取到条目（可能页面结构已变或被拦），跳过，保留旧 feed。")
        return

    rss = build_rss(items, cfg)
    out_path = os.path.join(REPO_ROOT, name + ".xml")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(rss)
    print("  ✅ 已写入 %s（%d 条）" % (out_path, len(items)))


def main():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        conf = json.load(f)
    ua = conf.get("ua", "Mozilla/5.0")
    timeout = conf.get("timeout", 40)
    sources = conf.get("sources", [])
    print("读取配置：%d 个源" % len(sources))
    for cfg in sources:
        process_source(cfg, ua, timeout)
    print("\n全部源处理完毕。")


if __name__ == "__main__":
    main()
