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
import re
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
DB_2026_ID = "33660e0b0bbf806ab9e9effb9cebb712"           # "2026" 数据库（日历视图所在）

# 只展示今天起未来 N 天内的活动
LOOKAHEAD_DAYS = 30

# 常见城市/地点关键词（用于提取活动地点）
CITY_HINTS = [
    "北京", "上海", "广州", "深圳", "杭州", "成都", "南京", "武汉",
    "西安", "长沙", "苏州", "香港", "张江", "顺德", "厦门", "合肥",
    "重庆", "天津", "青岛", "大连", "郑州", "济南", "福州", "南昌",
    "桂林", "贵阳", "昆明", "兰州", "西宁", "乌鲁木齐", "呼伦贝尔",
    "Shenzhen", "Shanghai", "Beijing", "Hangzhou", "Chengdu",
]

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
    location: str = ""
    signup_start: str = ""
    signup_deadline: str = ""
    competition_time: str = ""
    raw: dict = field(default_factory=dict)

    def as_markdown(self) -> str:
        parts = [f"### {self.title}"]
        if self.url:
            parts.append(f"链接：{self.url}")
        if self.competition_time:
            parts.append(f"⏰ 竞赛时间：{self.competition_time}")
        if self.location:
            parts.append(f"📍 地点：{self.location}")
        if self.signup_start:
            parts.append(f"📝 报名时间：{self.signup_start}")
        if self.signup_deadline:
            parts.append(f"⏳ 报名截止：{self.signup_deadline}")
        if self.host:
            parts.append(f"🏢 主办：{self.host}")
        if self.snippet:
            parts.append(f"摘要：{self.snippet[:180]}")
        if self.source:
            parts.append(f"来源：{self.source}")
        return "\n".join(parts)


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


def extract_dates(text: str, year: int) -> list[dt.date]:
    """从文本中提取日期（支持 2026年8月10日 / 8月10日 / 2026-08-10 / 8.10 等）。"""
    found: list[dt.date] = []
    patterns = [
        r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?",
        r"(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})",
        r"(\d{1,2})[-/.月](\d{1,2})日?",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text):
            try:
                if len(m.groups()) == 3:
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                else:
                    y, mo, d = year, int(m.group(1)), int(m.group(2))
                found.append(dt.date(y, mo, d))
            except ValueError:
                continue
    return found


def extract_fields(ev: Event, today: dt.date) -> None:
    """从标题+摘要中尽力提取地点、报名截止、竞赛时间等字段。"""
    text = f"{ev.title}。{ev.snippet}"
    year = today.year

    # 地点：优先“地点：xx”或“在XX举办/举行”，否则匹配常见城市
    loc_match = re.search(r"(?:地点|地址|城市)\s*[:：]\s*([^，。;；、]{2,12})", text)
    if loc_match:
        ev.location = loc_match.group(1).strip()
    if not ev.location:
        for city in CITY_HINTS:
            if city in text:
                ev.location = city
                break

    # 报名截止：优先“报名截止[:：]日期/时间”，否则取报名相关的最近未来日期
    dl_match = re.search(r"报名[^。；;]{0,8}截止[^。；;]{0,20}", text)
    if dl_match:
        seg = dl_match.group(0)
        dates = extract_dates(seg, year)
        if dates:
            future = [d for d in dates if d >= today]
            ev.signup_deadline = (future[0] if future else dates[-1]).strftime("%Y-%m-%d")
    if not ev.signup_deadline:
        dates = extract_dates(text, year)
        future = [d for d in dates if today <= d <= today + dt.timedelta(days=LOOKAHEAD_DAYS)]
        if future:
            ev.signup_deadline = f"约 {future[0].strftime('%m-%d')}"

    # 竞赛时间：优先“比赛/竞赛时间”，否则取标题/摘要中最早的未来日期
    time_match = re.search(r"(?:比赛|竞赛|活动|大赛)\s*时间\s*[:：]?\s*([^。；;，,]{2,20})", text)
    if time_match:
        ev.competition_time = time_match.group(1).strip()
    if not ev.competition_time:
        dates = extract_dates(text, year)
        future = [d for d in dates if today <= d <= today + dt.timedelta(days=LOOKAHEAD_DAYS)]
        if future:
            ev.competition_time = future[0].strftime("%Y-%m-%d")

    # 报名时间（开始）
    start_match = re.search(r"报名[^。；;]{0,4}(?:开启|开始|启动|开放)[^。；;]{0,16}", text)
    if start_match:
        seg = start_match.group(0)
        dates = extract_dates(seg, year)
        if dates:
            ev.signup_start = dates[0].strftime("%Y-%m-%d")
    if not ev.signup_start and ev.signup_deadline:
        ev.signup_start = "见原文"


def is_in_future_window(ev: Event, today: dt.date, days: int = LOOKAHEAD_DAYS) -> bool:
    """只保留今天起未来 days 天内的活动；无法判断日期的一律保留并标记。"""
    text = f"{ev.title}。{ev.snippet}"
    dates = extract_dates(text, today.year)
    if not dates:
        return True  # 无法判断日期，保留（人工确认）
    # 只要存在一个日期在今天~未来 days 天内，就保留
    if any(today <= d <= today + dt.timedelta(days=days) for d in dates):
        return True
    # 全是过去日期 → 剔除；全部未来但超出窗口 → 剔除
    return False


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


def _rich_text(content: str, bold: bool = False, code: bool = False) -> list[dict]:
    return [{"type": "text", "text": {"content": content}, "annotations": {"bold": bold, "code": code}}]


def _labeled_block(block_type: str, label: str, content: str, warn: bool = False) -> dict:
    prefix = "⚠️ " if warn else ""
    rich = [
        {"type": "text", "text": {"content": f"{prefix}{label} "}, "annotations": {"bold": True}},
        {"type": "text", "text": {"content": content}},
    ]
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": rich},
    }


def _link_block(block_type: str, label: str, text: str, url: str) -> dict:
    """生成带文字链接的块：点击文字跳转，不直接展示长 URL。"""
    rich = [
        {"type": "text", "text": {"content": f"{label} "}, "annotations": {"bold": True}},
        {"type": "text", "text": {"content": text, "link": {"url": url}}},
    ]
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": rich},
    }


def write_daily_summary(date_str: str, events: list[Event]) -> str:
    """在 Daily Record 下创建当天的一篇整合笔记（Notion 原生格式）。"""
    title = f"{date_str} 黑客松活动汇总"
    children: list[dict] = []
    children.append(_text_block("heading_1", title))
    children.append(
        _text_block(
            "paragraph",
            f"**概览**：共收录 {len(events)} 条活动，覆盖 {date_str} 起未来 30 天内的黑客松/编程马拉松信息。",
        )
    )
    children.append(_text_block("heading_2", "活动总览"))
    for i, ev in enumerate(events, 1):
        children.append(_text_block("heading_3", f"{i}. {ev.title}"))
        # 字段列表
        if ev.competition_time:
            children.append(_labeled_block("bulleted_list_item", "⏰ 竞赛时间", ev.competition_time))
        if ev.location:
            children.append(_labeled_block("bulleted_list_item", "📍 地点", ev.location))
        if ev.signup_start and ev.signup_start != "见原文":
            children.append(_labeled_block("bulleted_list_item", "📝 报名时间", ev.signup_start))
        if ev.signup_deadline:
            children.append(_labeled_block("bulleted_list_item", "⏳ 报名截止", ev.signup_deadline))
        if ev.host:
            children.append(_labeled_block("bulleted_list_item", "🏢 主办方", ev.host))
        if ev.url:
            children.append(_link_block("bulleted_list_item", "🔗 来源链接", "查看详情", ev.url))
        if ev.snippet:
            children.append(_labeled_block("bulleted_list_item", "摘要", ev.snippet[:180]))
    children.append(_text_block("heading_2", "说明"))
    children.append(
        _text_block(
            "bulleted_list_item",
            "⚠️ 时间、地点等字段由搜索引擎摘要自动提取，请以报名页面为准。",
        )
    )
    children.append(
        _text_block(
            "bulleted_list_item",
            "💡 点击来源链接可查看完整报名信息；无法判断日期的条目已保留供人工确认。",
        )
    )
    page = notion_create_page(
        {"type": "page_id", "page_id": DAILY_RECORD_PAGE_ID},
        {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
        children=children,
    )
    url = page.get("url", "")
    print(f"[ok] 已创建汇总页面: {url}")
    return url


def write_to_calendar(date_str: str, events: list[Event]) -> None:
    """把当天汇总写入 2026 数据库（日期=当天），使其出现在 Notion 日历中。"""
    title_prop = "名称"  # 2026 数据库标题属性（已验证）
    properties = {
        title_prop: {
            "title": [
                {
                    "type": "text",
                    "text": {"content": f"{date_str} 黑客松活动汇总（{len(events)} 条）"},
                }
            ]
        },
        "日期": {"date": {"start": date_str}},
    }
    try:
        notion_create_page({"type": "database_id", "database_id": DB_2026_ID}, properties)
        print(f"[ok] 已写入 2026 数据库日历: {date_str}")
    except RuntimeError as exc:
        print(f"[warn] 写入 2026 数据库失败: {exc}", file=sys.stderr)


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

    # 只保留今天起未来 30 天内的活动，并提取时间/地点等字段
    unique = [ev for ev in unique if is_in_future_window(ev, today)]
    for ev in unique:
        extract_fields(ev, today)

    print(f"[info] 共收集 {len(unique)} 条活动（未来 {LOOKAHEAD_DAYS} 天内）")
    for ev in unique[:30]:
        print(f"  - {ev.title} | {ev.location or '地点待确认'} | {ev.signup_deadline or '截止待确认'}")

    if dry_run:
        print("\n[dry-run] 以下为将写入 Notion 的汇总：")
        print(f"# {date_str} 黑客松活动汇总")
        for ev in unique[:30]:
            print(ev.as_markdown())
        return 0

    if not unique:
        print("[info] 未来 30 天内没有搜到活动，仍然创建汇总页面")

    write_daily_summary(date_str, unique[:30])
    write_to_calendar(date_str, unique[:30])
    print("[done] 全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
