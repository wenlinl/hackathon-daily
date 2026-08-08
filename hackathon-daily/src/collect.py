#!/usr/bin/env python3
"""每天搜索黑客松/编程马拉松活动并写入 Notion。

用法:
    python collect.py                # 正常执行（搜索 + 写入 Notion）
    python collect.py --dry-run      # 只搜索并打印，不写入
    python collect.py --search-only  # 同 --dry-run（兼容）
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Notion 目标（来自之前会话的验证结果）
DAILY_RECORD_PAGE_ID = "33660e0b0bbf80e9aa0ffe29b3ce9444"  # Daily Record 页面

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

QUERIES = [
    "黑客松 2026 报名",
    "黑客马拉松 2026 报名 截止",
    "hackathon 2026 registration",
    "编程马拉松 2026 报名",
    "黑客松 近期 报名",
    "hackathon 报名 截止日期",
]

# 标题/摘要必须命中这些关键词之一才保留（过滤无关结果）
RELEVANT_KEYWORDS = [
    "黑客松",
    "黑客马拉松",
    "hackathon",
    "Hackathon",
    "编程马拉松",
    "创新马拉松",
    "编程大赛",
]

# 明确排除的无关来源/内容特征
EXCLUDE_TITLE_HINTS = [
    "ecoflow",
    "登录",
    "log in",
    "sign in",
    "đà nẵng",
    "bản đồ",
    "danh sách",
    "phường",
    "quận",
    # 非黑客松活动的常见噪音（标题或摘要中出现即排除）
    "美赛",
    "ctb",
    "牛津",
    "夏校",
    "研学",
    "仲裁",
    "招生简章",
    "高研班",
    "数据集",
    "讲座",
    "奖学金",
    "夏令营",
    "冬令营",
    # 回顾/科普/招聘类（非可报名活动）
    "回顾",
    "收官",
    "全景",
    "是什么",
    "招聘",
    "岗位",
    "科普",
]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Event:
    title: str
    url: str = ""
    source: str = ""
    snippet: str = ""
    deadline: str = ""
    published: str = ""
    host: str = ""
    raw: dict = field(default_factory=dict)

    def as_markdown(self) -> str:
        title_link = f"[{self.title}]({self.url})" if self.url else f"**{self.title}**"
        meta = []
        if self.host:
            meta.append(f"主办：{self.host}")
        if self.deadline:
            meta.append(f"截止：{self.deadline}")
        if self.published:
            meta.append(f"发布：{self.published}")
        if self.source:
            meta.append(f"来源：{self.source}")
        if self.snippet:
            meta.append(f"摘要：{self.snippet[:200]}")
        return f"- {title_link}" + (f"（{'；'.join(meta)}）" if meta else "")


# ---------------------------------------------------------------------------
# 搜索（公开网页）
# ---------------------------------------------------------------------------

def _fetch(url: str, timeout: int = 15) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        if resp.status_code == 200:
            return resp.text
    except requests.RequestException as exc:
        print(f"[warn] 抓取失败 {url}: {exc}", file=sys.stderr)
    return None


def search_bing(query: str) -> list[Event]:
    """必应网页搜索。"""
    events: list[Event] = []
    url = f"https://www.bing.com/search?q={quote_plus(query)}&count=15&setlang=zh-hans"
    html = _fetch(url)
    if not html:
        return events
    soup = BeautifulSoup(html, "lxml")
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        link = a.get("href", "")
        snippet = li.select_one(".b_caption p, .b_lineclamp2, p")
        snippet_txt = snippet.get_text(" ", strip=True) if snippet else ""
        if not title or not link:
            continue
        events.append(
            Event(
                title=title,
                url=link,
                source="Bing",
                snippet=snippet_txt,
            )
        )
    return events


def search_duckduckgo(query: str) -> list[Event]:
    """DuckDuckGo HTML 搜索（作为必应的备选）。"""
    events: list[Event] = []
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}&kl=cn-zh"
    html = _fetch(url)
    if not html:
        return events
    soup = BeautifulSoup(html, "lxml")
    for result in soup.select(".result"):
        a = result.select_one(".result__a")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        link = a.get("href", "")
        if link.startswith("//duckduckgo.com/l/?uddg="):
            from urllib.parse import unquote, urlparse, parse_qs

            parsed = parse_qs(urlparse(link).query)
            link = unquote(parsed.get("uddg", [""])[0])
        snippet_el = result.select_one(".result__snippet")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        if title and link:
            events.append(Event(title=title, url=link, source="DuckDuckGo", snippet=snippet))
    return events


def search_sogou_weixin(query: str) -> list[Event]:
    """搜狗微信搜索（公开索引，仅公众号文章）。"""
    events: list[Event] = []
    url = f"https://weixin.sogou.com/weixin?type=2&query={quote_plus(query)}"
    html = _fetch(url)
    if not html:
        return events
    soup = BeautifulSoup(html, "lxml")
    for li in soup.select("ul.news-list li"):
        a = li.select_one("h3 a")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        rel = a.get("href", "")
        link = urljoin("https://weixin.sogou.com", rel) if rel else ""
        account = li.select_one(".account")
        host = account.get_text(" ", strip=True) if account else ""
        snippet_el = li.select_one(".txt-info")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        if title:
            events.append(
                Event(
                    title=title,
                    url=link,
                    source="微信公众号(搜狗索引)",
                    snippet=snippet,
                    host=host,
                )
            )
    return events


def search_github(query: str = "hackathon") -> list[Event]:
    """GitHub 上的 hackathon 活动仓库/讨论（公开信息）。"""
    events: list[Event] = []
    url = (
        "https://api.github.com/search/repositories"
        f"?q={quote_plus(query + ' hackathon')}&sort=updated&order=desc&per_page=10"
    )
    try:
        resp = requests.get(url, headers={**HEADERS, "Accept": "application/vnd.github+json"}, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("items", [])[:10]:
                events.append(
                    Event(
                        title=item.get("name", ""),
                        url=item.get("html_url", ""),
                        source="GitHub",
                        snippet=(item.get("description") or ""),
                        published=(item.get("updated_at") or "")[:10],
                    )
                )
    except (requests.RequestException, ValueError) as exc:
        print(f"[warn] GitHub 搜索失败: {exc}", file=sys.stderr)
    return events


def search_all() -> list[Event]:
    seen: set[str] = set()
    results: list[Event] = []
    for query in QUERIES:
        for fn in (search_bing, search_duckduckgo, search_sogou_weixin):
            try:
                found = fn(query)
            except Exception as exc:  # noqa: BLE001 - 单个源失败不影响整体
                print(f"[warn] {fn.__name__} 失败: {exc}", file=sys.stderr)
                continue
            for ev in found:
                key = (ev.title or "").strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                results.append(ev)
            time.sleep(1)
    # 附加 GitHub 公开仓库信息
    for ev in search_github():
        key = (ev.title or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            results.append(ev)
    return results


def is_relevant(ev: Event) -> bool:
    """关键词过滤：标题或摘要必须包含黑客松相关词，并排除明显无关内容。"""
    text = (ev.title or "") + " " + (ev.snippet or "")
    lower = text.lower()
    if not any(k.lower() in lower for k in RELEVANT_KEYWORDS):
        return False
    if any(h in (ev.title or "").lower() for h in EXCLUDE_TITLE_HINTS):
        return False
    return True


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_create_page(parent: dict, properties: dict, children: list[dict] | None = None) -> dict:
    payload = {"parent": parent, "properties": properties}
    if children:
        payload["children"] = children
    resp = requests.post(f"{NOTION_API}/pages", headers=notion_headers(), json=payload, timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Notion 创建失败 {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def _text_block(block_type: str, text: str) -> dict:
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def write_daily_summary(date_str: str, events: list[Event]) -> str:
    """在 Daily Record 下创建当天的一篇整合笔记：汇总所有黑客松信息并附来源链接。"""
    title = f"{date_str} 黑客松活动汇总"
    children: list[dict] = [
        _text_block("heading_1", title),
        _text_block("paragraph", f"共收录 {len(events)} 条黑客松/编程马拉松相关信息（截至 {date_str}）。"),
    ]
    # 按来源分组，方便浏览
    by_source: dict[str, list[Event]] = {}
    for ev in events:
        by_source.setdefault(ev.source or "其他", []).append(ev)
    for source, evs in by_source.items():
        children.append(_text_block("heading_2", source))
        for ev in evs:
            item = f"{ev.title}（来源：{ev.source}；摘要：{ev.snippet[:120]}）"
            if ev.url:
                item += f" 链接：{ev.url}"
            children.append(_text_block("bulleted_list_item", item[:1900]))
    page = notion_create_page(
        {"type": "page_id", "page_id": DAILY_RECORD_PAGE_ID},
        {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
        children=children,
    )
    url = page.get("url", "")
    print(f"[ok] 已创建汇总页面: {url}")
    return url


def validate_config() -> None:
    if not NOTION_TOKEN:
        print("[error] 缺少环境变量 NOTION_TOKEN", file=sys.stderr)
        sys.exit(2)


def check_notion() -> int:
    """验证 token 与目标页面/数据库是否可访问，并打印结构。"""
    validate_config()
    print("[info] 检查 Daily Record 页面…")
    resp = requests.get(f"{NOTION_API}/pages/{DAILY_RECORD_PAGE_ID}", headers=notion_headers(), timeout=30)
    if resp.status_code != 200:
        print(
            f"[error] 无法读取 Daily Record：{resp.status_code} {resp.text[:300]}",
            file=sys.stderr,
        )
        print("请确认：Daily Record 页面已分享给你的 Notion Integration。", file=sys.stderr)
        return 1
    print("[ok] Daily Record 可访问")

    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="黑客松每日活动收集")
    parser.add_argument("--dry-run", action="store_true", help="只搜索并打印，不写入 Notion")
    parser.add_argument("--search-only", action="store_true", help="同 --dry-run")
    parser.add_argument("--check-notion", action="store_true", help="只验证 Notion 连接与结构")
    args = parser.parse_args()

    if args.check_notion:
        return check_notion()

    dry_run = args.dry_run or args.search_only
    if not dry_run:
        validate_config()

    # 使用北京时间（Asia/Shanghai, UTC+8）确定"当天"，避免 GitHub Actions 的 UTC 偏差
    BEIJING_TZ = timezone(timedelta(hours=8))
    today = datetime.now(BEIJING_TZ).date()
    date_str = today.isoformat()
    print(f"[info] 日期: {date_str}，开始搜索…")

    events = search_all()
    events = [ev for ev in events if is_relevant(ev)]
    # 简单去重 + 排序（有发布日期/截止日期的靠前）
    unique: list[Event] = []
    seen: set[str] = set()
    for ev in events:
        key = ev.title.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(ev)

    print(f"[info] 共收集 {len(unique)} 条活动")
    for ev in unique[:30]:
        print("  -", ev.title, "|", ev.url)

    if dry_run:
        print("\n[dry-run] 以下为将写入 Notion 的汇总：")
        print(f"# {date_str} 黑客松活动汇总")
        for ev in unique[:30]:
            print(ev.as_markdown())
        return 0

    if not unique:
        print("[info] 今天没有搜到活动，仍然创建汇总页面")

    write_daily_summary(date_str, unique[:30])
    print("[done] 全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
