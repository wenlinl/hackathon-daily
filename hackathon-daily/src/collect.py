#!/usr/bin/env python3
"""每天搜索黑客松/编程马拉松活动并写入 Notion。

用法:
    python collect.py                # 正常执行（搜索 + 写入 Notion）
    python collect.py --dry-run      # 只搜索并打印，不写入
    python collect.py --search-only  # 同 --dry-run（兼容）
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from html import escape as html_escape
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
BEIJING_TZ = timezone(timedelta(hours=8))

# Notion 目标（来自之前会话的验证结果）
DAILY_RECORD_PAGE_ID = "33660e0b0bbf80e9aa0ffe29b3ce9444"  # Daily Record 页面
DB_2026_ID = "33660e0b0bbf806ab9e9effb9cebb712"           # "2026" 数据库（日历视图所在）

# 服务器端历史信息库（本地 JSON 文件，随仓库持久化，替代 Notion 数据库）
ARCHIVE_FILE = Path(__file__).resolve().parent.parent / "data" / "archive.json"
# 信息库可浏览页面（GitHub Pages，/docs 目录），每条目带锚点供日报跳转
ARCHIVE_PAGE_URL = os.environ.get(
    "ARCHIVE_PAGE_URL", "https://wenlinl.github.io/hackathon-daily/archive.html"
)
ARCHIVE_HTML = Path(__file__).resolve().parent.parent / "docs" / "archive.html"

# AI 清洗/审核（OpenAI 兼容接口，可指向 DeepSeek 等）


def _env_str(name: str, default: str = "") -> str:
    """读取环境变量，空字符串视为未设置（GitHub Actions 未配置的 secret 会是空串）。"""
    return (os.environ.get(name, "") or "").strip() or default


LLM_API_KEY = _env_str("LLM_API_KEY") or _env_str("OPENAI_API_KEY")
LLM_BASE_URL = _env_str("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = _env_str("LLM_MODEL", "gpt-4o-mini")
_DEEPSEEK_KEY = _env_str("DEEPSEEK_API_KEY")
if not LLM_API_KEY and _DEEPSEEK_KEY:
    LLM_API_KEY = _DEEPSEEK_KEY
    LLM_BASE_URL = "https://api.deepseek.com/v1"
    LLM_MODEL = _env_str("LLM_MODEL", "deepseek-chat")

# 只展示今天起未来 N 天内的活动
LOOKAHEAD_DAYS = 30
# 单日汇总最多展示/入库的活动条数
MAX_EVENTS = 50
# 单日最多做 AI 重新检索核对的活动条数（控制耗时与费用）


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name, "").strip()
    try:
        return int(val)
    except ValueError:
        return default


VERIFY_MAX_EVENTS = _env_int("VERIFY_MAX_EVENTS", 15)

# ---------------------------------------------------------------------------
# 信息核对标准（创建每条信息时按此核对）
# ---------------------------------------------------------------------------
VERIFY_STANDARDS = [
    "真实性：主办方真实存在，活动在其官网/官方公众号/官方开发者平台可查；来源优先官方>权威媒体>聚合/个人",
    "时间准确性：报名开始、报名截止、竞赛时间须与官方页面一致；多个独立来源一致才判'已核对'，冲突标记'信息冲突'",
    "链接有效性：报名/官网链接可访问，域名与主办方匹配；可疑域名或失效链接标记'待确认'",
    "字段完整性：报名时间、报名截止、竞赛时间、地点、主办方五项齐全才算'完整'，缺项在核对说明中列出",
]

# 国内大厂/知名技术微信公众号（搜狗微信索引定向补充）
WECHAT_ACCOUNTS = [
    "腾讯云开发者社区",
    "腾讯技术工程",
    "阿里云开发者",
    "飞桨PaddlePaddle",
    "百度AI",
    "字节跳动技术团队",
    "华为开发者联盟服务",
    "华为云",
    "美团技术团队",
    "京东云开发者",
    "蚂蚁技术AntTech",
    "科大讯飞开放平台",
    "智谱AI",
    "深度求索",
    "机器之心",
    "量子位",
    "InfoQ",
    "开源中国OSCHINA",
    "51CTO技术栈",
    "CSDN",
]

# 小红书（通过搜索引擎 site: 索引抓取公开帖子）
XIAOHONGSHU_QUERIES = [
    "site:xiaohongshu.com 黑客松 报名",
    "site:xiaohongshu.com 黑客松 2026",
    "site:xiaohongshu.com hackathon 报名",
    "site:xiaohongshu.com 编程马拉松",
    "小红书 黑客松 报名 2026",
    "小红书 黑客松 巅峰赛",
]

# 常见城市/地点关键词（用于提取活动地点）
CITY_HINTS = [
    "北京", "上海", "广州", "深圳", "杭州", "成都", "南京", "武汉",
    "西安", "长沙", "苏州", "香港", "张江", "顺德", "厦门", "合肥",
    "重庆", "天津", "青岛", "大连", "郑州", "济南", "福州", "南昌",
    "桂林", "贵阳", "昆明", "兰州", "西宁", "乌鲁木齐", "呼伦贝尔",
    "Shenzhen", "Shanghai", "Beijing", "Hangzhou", "Chengdu",
]

# 国内主办方/平台关键词（用于国内/国外分组）
DOMESTIC_ORG_HINTS = [
    "腾讯", "阿里", "百度", "字节", "华为", "美团", "京东", "小米", "网易",
    "蚂蚁", "飞桨", "paddlepaddle", "智谱", "讯飞", "小红书", "开源中国",
    "oschina", "蓝桥", "天池", "datafountain", "segmentfault", "掘金",
    "juejin", "活动行", "赛氪", "信通院", "联通", "移动", "电信", "科大",
    "沐曦", "真格", "九合创投", "上海国资", "哈工大", "湖南农业大学",
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
    # 课程/教程/广告类（标题出现即排除，避免 Eventbrite 等平台混入课程广告）
    "ux design",
    "learn ",
    "course",
    "tutorial",
    "masterclass",
    "课程",
    "教程",
    # 往期回顾/资料/非活动类
    "斩获",
    "获奖",
    "手册",
    "文档",
    "入门",
    "weekly",
    "活动推荐",
    "大全",
    "全收录",
    "直播",
    "video contest",
    "暑期学校",
    "冬季学校",
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
    calendar_date: str = ""  # 写入日历数据库用的日期（YYYY-MM-DD）
    raw: dict = field(default_factory=dict)
    is_hackathon_site: bool = False  # 来源本身就是黑客松聚合站，跳过关键词过滤
    # 扩充字段（覆盖更多黑客松信息）
    host: str = ""           # 主办方
    themes: str = ""         # 主题/赛道
    prize: str = ""          # 奖金/奖池
    eligibility: str = ""    # 报名条件/参赛对象
    status: str = ""         # 活动状态（报名中/未开始/进行中/已结束）
    format: str = ""         # 形式（线上/线下/混合）
    tags: str = ""           # 标签
    region: str = ""         # 分组：国内/国外/线上
    review_status: str = ""  # AI 审核状态
    review_note: str = ""    # 审核说明
    verify_status: str = ""  # 核对状态：已核对/部分核对/信息冲突/未能核对
    verify_note: str = ""    # 核对说明
    official_url: str = ""   # 核对后确认的官方/权威链接

    def as_markdown(self) -> str:
        parts = [f"### {self.title}"]
        for label, val in (
            ("🏢 主办方", self.host),
            ("📌 主题", self.themes),
            ("🏆 奖金", self.prize),
            ("👥 报名条件", self.eligibility),
            ("📝 报名时间", self.signup_start),
            ("⏳ 报名截止", self.signup_deadline),
            ("⏰ 竞赛时间", self.competition_time),
            ("📍 地点", self.location),
            ("🌐 形式", self.format),
            ("🏷️ 标签", self.tags),
            ("🚦 状态", self.status),
            ("🌏 分组", self.region),
        ):
            if val:
                parts.append(f"{label}：{val}")
        if not self.signup_start:
            parts.append("📝 报名时间：待确认")
        if not self.signup_deadline:
            parts.append("⏳ 报名截止：待确认")
        if not self.competition_time:
            parts.append("⏰ 竞赛时间：待确认")
        if not self.location:
            parts.append("📍 地点：待确认")
        parts.append(f"摘要：{self.snippet[:180] if self.snippet else '待确认'}")
        parts.append(f"🔗 链接：{self.url if self.url else '待确认'}")
        parts.append(f"🗄️ 信息库：{_resolve_archive_url(self.title)}")
        if self.official_url and self.official_url != self.url:
            parts.append(f"✅ 官方链接：{self.official_url}")
        if self.review_status:
            parts.append(f"🔎 审核：{self.review_status}")
        if self.verify_status:
            line = self.verify_status
            if self.verify_note:
                line += f"：{self.verify_note}"
            parts.append(f"🔬 核对：{line}")
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


def _sogou_weixin_query(query: str) -> list[Event]:
    """搜狗微信文章搜索（公开索引，仅公众号文章）。"""
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


def search_sogou_weixin(query: str) -> list[Event]:
    """搜狗微信搜索（关键词入口）。"""
    return _sogou_weixin_query(query)


def search_wechat_accounts() -> list[Event]:
    """定向补充：国内大厂/知名技术公众号的公众号文章（搜狗微信索引）。"""
    events: list[Event] = []
    failures = 0
    for account in WECHAT_ACCOUNTS:
        if failures >= 3:
            print("[warn] 搜狗微信连续失败，停止公众号定向抓取", file=sys.stderr)
            break
        query = f"{account} 黑客松"
        try:
            found = _sogou_weixin_query(query)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[warn] 公众号 {account} 抓取失败: {exc}", file=sys.stderr)
            continue
        if not found:
            failures += 1
        else:
            failures = 0
            for ev in found:
                ev.source = f"公众号({account})"
                events.append(ev)
        time.sleep(2)
    return events


def search_xiaohongshu() -> list[Event]:
    """小红书公开帖子（通过搜索引擎 site: 索引）。"""
    events: list[Event] = []
    seen: set[str] = set()
    for q in XIAOHONGSHU_QUERIES:
        try:
            found = search_bing(q)
            if not found:
                found = search_duckduckgo(q)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 小红书检索失败 {q}: {exc}", file=sys.stderr)
            continue
        for ev in found:
            # 只保留真实来自小红书域名的结果
            if "xiaohongshu.com" not in (ev.url or ""):
                continue
            key = normalize_title(ev.title)
            if not key or key in seen:
                continue
            seen.add(key)
            ev.source = "小红书(搜索引擎索引)"
            events.append(ev)
        time.sleep(1)
    return events


def _fetch_json(url: str, timeout: int = 20, **kwargs) -> dict | list | None:
    """抓取 JSON 接口，失败返回 None。"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, **kwargs)
        if resp.status_code == 200:
            return resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[warn] JSON 抓取失败 {url}: {exc}", file=sys.stderr)
    return None


def _fmt_date_range(start_iso: str, end_iso: str) -> str:
    """把 ISO 起止时间规范为 YYYY-MM-DD ~ YYYY-MM-DD。"""
    start = (start_iso or "")[:10]
    end = (end_iso or "")[:10]
    if start and end:
        return f"{start} ~ {end}"
    return start or end


def _next_data(html: str) -> dict | None:
    """提取 Next.js 页面内嵌的 __NEXT_DATA__ JSON。"""
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


MONTHS_EN = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _en_dates_to_range(text: str) -> str:
    """把英文日期（March 12, 2028 - March 27, 2028）转为 YYYY-MM-DD 区间。"""
    dates: list[str] = []
    for m in re.finditer(r"([A-Za-z]+)\.?\s+(\d{1,2}),\s+(\d{4})", text):
        mon = MONTHS_EN.get(m.group(1).capitalize())
        if not mon:
            continue
        try:
            d = f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"
        except ValueError:
            continue
        if d not in dates:
            dates.append(d)
    if len(dates) >= 2:
        return f"{dates[0]} ~ {dates[-1]}"
    return dates[0] if dates else ""


def search_mlh() -> list[Event]:
    """MLH（Major League Hacking）赛季活动：Inertia 内嵌 JSON。"""
    events: list[Event] = []
    html = _fetch("https://mlh.io/events", timeout=25)
    if not html:
        return events
    m = re.search(r'<script data-page="app" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        return events
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return events
    for ev in data.get("props", {}).get("upcomingEvents") or []:
        title = (ev.get("name") or "").strip()
        if not title:
            continue
        rel = ev.get("url") or ""
        url = urljoin("https://mlh.io", rel) if rel else ""
        starts = (ev.get("startsAt") or "")[:10]
        ends = (ev.get("endsAt") or "")[:10]
        loc = (ev.get("location") or "").strip()
        if not loc:
            loc = "线上" if ev.get("formatType") == "virtual" else "待确认"
        fmt_map = {"virtual": "线上", "physical": "线下", "hybrid": "混合"}
        fmt = fmt_map.get(ev.get("formatType") or "", "")
        status_map = {"pending": "未开始", "in_progress": "进行中", "ended": "已结束"}
        status = status_map.get(ev.get("status") or "", "")
        time_range = _fmt_date_range(starts, ends)
        snippet = f"活动时间：{time_range}。地点：{loc}。"
        events.append(
            Event(
                title=title,
                url=url,
                source="MLH",
                snippet=snippet,
                location=loc,
                competition_time=time_range,
                format=fmt,
                status=status,
                is_hackathon_site=True,
            )
        )
    return events


def search_devfolio() -> list[Event]:
    """Devfolio 黑客松聚合站：__NEXT_DATA__ 内嵌 JSON。"""
    events: list[Event] = []
    html = _fetch("https://devfolio.co/hackathons", timeout=25)
    if not html:
        return events
    data = _next_data(html)
    if not data:
        return events
    try:
        queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
        payload = queries[0]["state"]["data"]
    except (KeyError, IndexError, TypeError):
        return events
    for group in ("open_hackathons", "upcoming_hackathons", "featured_hackathons"):
        for ev in payload.get(group) or []:
            title = (ev.get("name") or "").strip()
            slug = ev.get("slug") or ""
            if not title or not slug:
                continue
            url = f"https://devfolio.co/hackathons/{slug}"
            time_range = _fmt_date_range(ev.get("starts_at", ""), ev.get("ends_at", ""))
            loc = "线上" if ev.get("is_online") else "待确认"
            fmt = "线上" if ev.get("is_online") else "线下"
            status = {"open_hackathons": "报名中", "upcoming_hackathons": "未开始", "featured_hackathons": "报名中"}.get(group, "")
            themes = "、".join(
                (t.get("theme") or {}).get("name", "")
                for t in (ev.get("themes") or [])
                if (t.get("theme") or {}).get("name")
            )
            snippet = f"活动时间：{time_range}。地点：{loc}。"
            events.append(
                Event(
                    title=title,
                    url=url,
                    source="Devfolio",
                    snippet=snippet,
                    location=loc,
                    competition_time=time_range,
                    format=fmt,
                    status=status,
                    themes=themes,
                    is_hackathon_site=True,
                )
            )
    return events


def search_allhackathons() -> list[Event]:
    """All Hackathons 聚合站：静态 HTML 卡片（仅保留有明确日期的活动）。"""
    events: list[Event] = []
    html = _fetch("https://allhackathons.com/hackathons/", timeout=25)
    if not html:
        return events
    soup = BeautifulSoup(html, "lxml")
    for card in soup.select(".row.align-items-center.bg-white"):
        a = card.select_one("a.h5")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        url = urljoin("https://allhackathons.com", a.get("href", ""))
        badge = card.select_one("span.badge")
        mode = badge.get_text(" ", strip=True) if badge else ""
        date_el = a.find_next("p")
        date_txt = date_el.get_text(" ", strip=True) if date_el else ""
        time_range = _en_dates_to_range(date_txt)
        if not time_range:
            continue  # “Date TBD”类无日期活动不进入汇总，避免噪音
        desc_el = card.select_one("p.text-muted.mt-2.mb-0")
        desc = desc_el.get_text(" ", strip=True) if desc_el else ""
        loc = "线上" if "online" in mode.lower() else ("线下" if "in-person" in mode.lower() else "")
        fmt = loc or ""
        tags = "、".join(
            a.get_text(" ", strip=True)
            for a in card.select('a[href^="/themes/"]')
            if a.get_text(" ", strip=True)
        )
        snippet = "。".join(
            x
            for x in (
                f"活动时间：{time_range}",
                f"地点：{mode}",
                desc,
            )
            if x
        )
        events.append(
            Event(
                title=title,
                url=url,
                source="AllHackathons",
                snippet=snippet,
                location=loc,
                competition_time=time_range,
                format=fmt,
                tags=tags,
                is_hackathon_site=True,
            )
        )
    return events


def search_ethglobal() -> list[Event]:
    """ETHGlobal：静态 HTML 活动卡片（仅保留黑客松主活动，排除分会场/聚会）。"""
    events: list[Event] = []
    html = _fetch("https://ethglobal.com/events", timeout=25)
    if not html:
        return events
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    for a in soup.select('a[href^="/events/"]'):
        h2 = a.select_one("h2")
        if not h2:
            continue
        title = h2.get_text(" ", strip=True)
        if not title or title in seen:
            continue
        seen.add(title)
        url = urljoin("https://ethglobal.com", a.get("href", ""))
        slug = (a.get("href") or "").lower()
        if re.search(r"pragma|cowork|happy[- ]hour|meetup|conference|ethconf|party|dinner", slug):
            continue
        date_el = a.select_one("div.text-center")
        date_txt = date_el.get_text(" ", strip=True) if date_el else ""
        m = re.search(r"([A-Za-z]+)\s+(\d{1,2})(?:\s*[—–-]\s*(\d{1,2}))?", date_txt)
        time_str = ""
        if m:
            mon = MONTHS_EN.get(m.group(1).capitalize())
            if mon:
                time_str = f"{mon}月{int(m.group(2))}日"
                if m.group(3):
                    time_str += f"—{int(m.group(3))}日"
        tags = [
            s.get_text(" ", strip=True)
            for s in a.select("span")
            if s.get_text(" ", strip=True)
        ]
        loc = ""
        for t in tags:
            tl = t.lower()
            if tl in ("online", "virtual") or "async" in tl:
                loc = "线上"
            elif t in CITY_HINTS:
                loc = t
            if loc:
                break
        fmt = loc or ""
        snippet = f"活动时间：{time_str or '待确认'}。地点：{loc or '待确认'}。"
        if tags:
            snippet += f"标签：{'/'.join(tags[:5])}。"
        events.append(
            Event(
                title=title,
                url=url,
                source="ETHGlobal",
                snippet=snippet,
                location=loc,
                format=fmt,
                themes="、".join(tags[:5]),
                is_hackathon_site=True,
            )
        )
    return events


def search_hackathoncom() -> list[Event]:
    """Hackathon.com 首页推荐黑客松：静态 HTML 卡片。"""
    events: list[Event] = []
    html = _fetch("https://www.hackathon.com/", timeout=30)
    if not html:
        return events
    soup = BeautifulSoup(html, "lxml")
    for hero in soup.select(".hero"):
        a = hero.select_one("a.hero__title")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        url = urljoin("https://www.hackathon.com", a.get("href", ""))
        starts = ends = ""
        for dblock in hero.select("div.date"):
            title_el = dblock.select_one(".date__title")
            day_el = dblock.select_one(".date__day")
            mon_el = dblock.select_one(".date__month")
            if not (title_el and day_el and mon_el):
                continue
            label = title_el.get_text(" ", strip=True).lower()
            mon = MONTHS_EN.get(mon_el.get_text(" ", strip=True).capitalize())
            if not mon:
                continue
            day = day_el.get_text(" ", strip=True)
            try:
                val = f"{mon}月{int(day)}日"
            except ValueError:
                continue
            if label.startswith("start"):
                starts = val
            elif label.startswith("end"):
                ends = val
        time_str = f"{starts}—{ends}" if starts and ends else (starts or ends)
        loc_el = hero.select_one(".hero__key-info__location")
        loc = loc_el.get_text(" ", strip=True) if loc_el else ""
        if loc.lower() in ("online", "virtual"):
            loc = "线上"
        fmt = loc or ""
        topic_tags = [
            t.get_text(" ", strip=True)
            for t in hero.select(".ht-event-topics__tag")
            if t.get_text(" ", strip=True)
        ]
        snippet = f"活动时间：{time_str or '待确认'}。地点：{loc or '待确认'}。"
        if topic_tags:
            snippet += f"主题：{'、'.join(topic_tags)}。"
        events.append(
            Event(
                title=title,
                url=url,
                source="Hackathon.com",
                snippet=snippet,
                location=loc,
                format=fmt,
                themes="、".join(topic_tags),
                is_hackathon_site=True,
            )
        )
    return events


def search_hackclub() -> list[Event]:
    """Hack Club 高中生黑客松策展列表：__NEXT_DATA__ 内嵌 JSON。"""
    events: list[Event] = []
    html = _fetch("https://hackathons.hackclub.com/", timeout=25)
    if not html:
        return events
    data = _next_data(html)
    if not data:
        return events
    try:
        ev_list = data["props"]["pageProps"]["events"]
    except (KeyError, TypeError):
        return events
    for ev in ev_list or []:
        title = (ev.get("name") or "").strip()
        url = ev.get("website") or ""
        if not title:
            continue
        time_range = _fmt_date_range(ev.get("start", ""), ev.get("end", ""))
        if ev.get("virtual"):
            loc = "线上"
        else:
            loc = "、".join(
                str(x) for x in (ev.get("city"), ev.get("state"), ev.get("country")) if x
            ) or "待确认"
        if ev.get("hybrid"):
            fmt = "混合"
        elif ev.get("virtual"):
            fmt = "线上"
        else:
            fmt = "线下"
        snippet = f"活动时间：{time_range}。地点：{loc}。Hack Club 策展的高中生黑客松。"
        events.append(
            Event(
                title=title,
                url=url,
                source="Hack Club",
                snippet=snippet,
                location=loc,
                competition_time=time_range,
                format=fmt,
                is_hackathon_site=True,
            )
        )
    return events


def search_eventbrite() -> list[Event]:
    """Eventbrite 黑客松搜索页：JSON-LD 结构化数据。"""
    events: list[Event] = []
    html = _fetch("https://www.eventbrite.com/d/online/hackathon/", timeout=25)
    if not html:
        return events
    soup = BeautifulSoup(html, "lxml")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "ItemList":
            continue
        for item in data.get("itemListElement") or []:
            ev = item.get("item") or {}
            title = (ev.get("name") or "").strip()
            if not title:
                continue
            time_range = _fmt_date_range(ev.get("startDate", ""), ev.get("endDate", ""))
            mode = ev.get("eventAttendanceMode") or ""
            loc = "线上" if "OnlineEventAttendanceMode" in mode else "待确认"
            fmt = "线上" if "OnlineEventAttendanceMode" in mode else ("线下" if "OfflineEventAttendanceMode" in mode else "")
            desc = (ev.get("description") or "").strip()
            snippet = f"活动时间：{time_range}。地点：{loc}。"
            if desc:
                snippet += f"{desc[:200]}"
            events.append(
                Event(
                    title=title,
                    url=ev.get("url") or "",
                    source="Eventbrite",
                    snippet=snippet,
                    location=loc,
                    competition_time=time_range,
                    format=fmt,
                )
            )
    return events


def search_segmentfault() -> list[Event]:
    """SegmentFault 思否活动：__NEXT_DATA__ 内嵌活动列表（含报名/活动时间戳）。"""
    events: list[Event] = []
    html = _fetch("https://segmentfault.com/events", timeout=25)
    if not html:
        return events
    data = _next_data(html)
    if not data:
        return events
    try:
        activity = data["props"]["pageProps"]["initialState"]["activity"]
        ev_list = activity.get("newestList") or []
    except (KeyError, TypeError):
        return events
    tz = timezone(timedelta(hours=8))
    for it in ev_list:
        title = (it.get("name") or "").strip()
        if not title:
            continue
        url = urljoin("https://segmentfault.com", it.get("url") or "")
        sign_url = it.get("real_sign_url") or ""
        if sign_url:
            url = sign_url

        def ts_date(key: str) -> str:
            ts = it.get(key)
            if not ts:
                return ""
            try:
                return datetime.fromtimestamp(int(ts), tz=tz).strftime("%Y-%m-%d")
            except (ValueError, OSError):
                return ""

        sign_start = ts_date("sign_start")
        sign_end = ts_date("sign_end")
        start = ts_date("start")
        end = ts_date("end")
        city = (it.get("city_name") or "").strip()
        cat = (it.get("category_name") or "").strip()
        loc = city or ("线上" if "线上" in cat else "待确认")
        fmt = "线上" if "线上" in cat else ("线下" if "线下" in cat else "")
        snippet = (
            f"报名时间：{sign_start or '待确认'} ~ {sign_end or '待确认'}。"
            f"活动时间：{start or '待确认'} ~ {end or '待确认'}。地点：{loc}。"
        )
        events.append(
            Event(
                title=title,
                url=url,
                source="SegmentFault",
                snippet=snippet,
                location=loc,
                signup_start=sign_start,
                signup_deadline=sign_end,
                competition_time=f"{start} ~ {end}" if start and end else "",
                format=fmt,
            )
        )
    return events


def search_datafountain() -> list[Event]:
    """DataFountain 数据竞赛平台：公开 API。"""
    events: list[Event] = []
    url = (
        "https://www.datafountain.cn/api/competitions"
        "?competitionTypeId=all&stateId=all&pageSize=30&page=1"
    )
    data = _fetch_json(url)
    if not data:
        return events
    try:
        competitions = data["cmpt"]["competitions"]
    except (KeyError, TypeError):
        return events
    for c in competitions or []:
        title = (c.get("title") or "").strip()
        if not title:
            continue
        cid = c.get("id")
        url_c = f"https://www.datafountain.cn/competitions/{cid}" if cid else ""
        time_range = _fmt_date_range(c.get("startTime", ""), c.get("endTime", ""))
        reward = (c.get("reward") or "").strip()
        orgs = [
            (o.get("name") or "").strip()
            for o in (c.get("organizers") or [])
            if (o.get("name") or "").strip()
        ][:2]
        host = "、".join(orgs)
        type_label = (c.get("typeLabel") or "").strip()
        snippet = f"赛事时间：{time_range}。奖励：{reward or '待确认'}。主办：{'、'.join(orgs) or '待确认'}。"
        events.append(
            Event(
                title=title,
                url=url_c,
                source="DataFountain",
                snippet=snippet,
                competition_time=time_range,
                host=host,
                prize=reward,
                tags=type_label,
            )
        )
    return events


def search_lanqiao() -> list[Event]:
    """蓝桥杯竞赛：公开 API（分页取前两页）。"""
    events: list[Event] = []
    for page in (1, 2):
        data = _fetch_json(f"https://www.lanqiao.cn/api/v2/contests/?page={page}")
        if not data:
            continue
        for r in (data.get("results") or []):
            title = (r.get("name") or "").strip()
            if not title:
                continue
            url = urljoin("https://www.lanqiao.cn", r.get("html_url") or "")
            time_range = _fmt_date_range(r.get("open_at", ""), r.get("end_at", ""))
            subject = (r.get("subject") or "").strip()
            desc = (r.get("description") or "").strip()
            status = "已结束" if (r.get("status") or "") == "finished" else ""
            snippet = f"活动时间：{time_range}。科目：{subject or '待确认'}。{desc}"
            events.append(
                Event(
                    title=title,
                    url=url,
                    source="蓝桥杯",
                    snippet=snippet,
                    competition_time=time_range,
                    tags=subject,
                    status=status,
                )
            )
    return events


def search_devevent() -> list[Event]:
    """Dev-Event（GitHub 聚合，韩国开发者活动为主）：README Markdown。"""
    events: list[Event] = []
    text = None
    raw_url = "https://raw.githubusercontent.com/brave-people/Dev-Event/master/README.md"
    resp = None
    try:
        resp = requests.get(raw_url, headers=HEADERS, timeout=20)
        if resp.status_code == 200:
            text = resp.text
    except requests.RequestException as exc:
        print(f"[warn] Dev-Event raw 抓取失败: {exc}", file=sys.stderr)
    if text is None:
        api_headers = dict(HEADERS)
        api_headers["Accept"] = "application/vnd.github.raw"
        token = os.environ.get("GITHUB_TOKEN", "")
        if token:
            api_headers["Authorization"] = f"Bearer {token}"
        try:
            resp = requests.get(
                "https://api.github.com/repos/brave-people/Dev-Event/contents/README.md",
                headers=api_headers,
                timeout=25,
            )
            if resp.status_code == 200:
                text = resp.text
        except requests.RequestException as exc:
            print(f"[warn] Dev-Event api 抓取失败: {exc}", file=sys.stderr)
    if not text:
        return events

    entry_re = re.compile(r"-\s+__\[([^\]]+)\]\(([^)]+)\)__\s*$")
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = entry_re.match(lines[i])
        if not m:
            i += 1
            continue
        title = m.group(1).strip()
        url = m.group(2).strip()
        i += 1
        details: list[str] = []
        while i < len(lines) and lines[i].strip() and not entry_re.match(lines[i]):
            details.append(lines[i].strip())
            i += 1
        host = ""
        cat_seg = ""
        date_seg = ""
        for dline in details:
            dl = dline.strip()
            m = re.match(r"[-–]?\s*주최:\s*(.+)", dl)
            if m and not host:
                host = m.group(1).strip()
            m = re.match(r"[-–]?\s*분류:\s*(.+)", dl)
            if m and not cat_seg:
                cat_seg = m.group(1).strip()
            m = re.match(r"[-–]?\s*(?:접수|일시):\s*(.+)", dl)
            if m and not date_seg:
                date_seg = m.group(1).strip()
        fmt = "线上" if "온라인" in cat_seg else ("线下" if "오프라인" in cat_seg else "")
        cat_tags = "、".join(
            x.strip("`")
            for x in re.findall(r"`([^`]+)`", cat_seg)
            if x.strip("`")
        )
        # 韩文日期形如“07. 10(금) ~ 08. 10(월)”→ 规范为“7月10日 ~ 8月10日”
        norm = re.sub(
            r"(\d{1,2})\.\s*(\d{1,2})",
            lambda mm: f"{int(mm.group(1))}月{int(mm.group(2))}日",
            date_seg,
        )
        norm = re.sub(r"\([가-힣]+\)", "", norm)  # 去掉韩文星期后缀
        snippet = f"报名/活动时间：{norm or '待确认'}。"
        events.append(
            Event(
                title=title,
                url=url,
                source="Dev-Event(GitHub聚合)",
                snippet=snippet,
                host=host,
                format=fmt,
                tags=cat_tags,
            )
        )
    return events


def search_all() -> list[Event]:
    seen: set[str] = set()
    results: list[Event] = []
    # 优先跑结构化站点（聚合站信息更准），再跑关键词搜索
    for fn in (
        search_mlh,
        search_devfolio,
        search_allhackathons,
        search_ethglobal,
        search_hackathoncom,
        search_hackclub,
        search_eventbrite,
        search_segmentfault,
        search_datafountain,
        search_lanqiao,
        search_devevent,
        search_wechat_accounts,
        search_xiaohongshu,
    ):
        try:
            found = fn()
        except Exception as exc:  # noqa: BLE001 - 单个源失败不影响整体
            print(f"[warn] {fn.__name__} 失败: {exc}", file=sys.stderr)
            continue
        for ev in found:
            key = normalize_title(ev.title)
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(ev)
        time.sleep(0.5)

    for query in QUERIES:
        for fn in (search_bing, search_duckduckgo, search_sogou_weixin):
            try:
                found = fn(query)
            except Exception as exc:  # noqa: BLE001 - 单个源失败不影响整体
                print(f"[warn] {fn.__name__} 失败: {exc}", file=sys.stderr)
                continue
            for ev in found:
                key = normalize_title(ev.title)
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
    # 专做黑客松的聚合站（MLH/Devfolio/ETHGlobal 等）不再要求标题含关键词
    if not ev.is_hackathon_site and not any(k.lower() in lower for k in RELEVANT_KEYWORDS):
        return False
    if any(h in (ev.title or "").lower() for h in EXCLUDE_TITLE_HINTS):
        return False
    # 标题带往年年份（如 2021 年）的文章基本是往期内容，直接排除
    m_year = re.search(r"(20\d{2})", ev.title or "")
    if m_year and int(m_year.group(1)) < datetime.now(BEIJING_TZ).date().year:
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
    window_end = today + dt.timedelta(days=LOOKAHEAD_DAYS)

    if not ev.location:
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

    # 竞赛时间：优先“比赛/竞赛时间”后的日期区间，保留原文区间（如 8月8日—9日）
    time_match = re.search(r"(?:比赛|竞赛|活动|大赛)\s*时间\s*[:：]?\s*([^。；;]{2,24})", text)
    if time_match:
        ev.competition_time = time_match.group(1).strip().rstrip("，, ")
    if not ev.competition_time:
        # 提取形如“8月8日—9日”“8.8-8.9”的日期区间
        range_match = re.search(
            r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s*[—\-~至到]\s*(\d{1,2})\s*日?",
            text,
        )
        if range_match:
            ev.competition_time = (
                f"{range_match.group(1)}月{range_match.group(2)}日"
                f"—{range_match.group(3)}日"
            )
    if not ev.competition_time:
        dates = extract_dates(text, year)
        future = [d for d in dates if today <= d <= today + dt.timedelta(days=LOOKAHEAD_DAYS)]
        if future:
            ev.competition_time = future[0].strftime("%Y-%m-%d")

    # 报名时间（开始）：优先“报名时间/报名开启/开始报名”，保留原文
    start_match = re.search(
        r"(?:报名\s*时间|报名[^。；;]{0,4}(?:开启|开始|启动|开放))[^。；;]{0,24}",
        text,
    )
    if start_match:
        ev.signup_start = start_match.group(0).strip()
    if not ev.signup_start:
        dates = extract_dates(text, year)
        future = [d for d in dates if today <= d <= today + dt.timedelta(days=LOOKAHEAD_DAYS)]
        if future:
            ev.signup_start = f"约 {future[0].strftime('%m-%d')}"

    # 日历日期：优先竞赛时间，其次报名截止，最后取最早的未来日期；都没有则用今天
    for seg in (ev.competition_time, ev.signup_deadline):
        if seg and "见原文" not in seg and "约 " not in seg:
            dates = extract_dates(seg, year)
            future = [d for d in dates if today <= d <= window_end]
            if future:
                ev.calendar_date = future[0].strftime("%Y-%m-%d")
                break
    if not ev.calendar_date:
        dates = extract_dates(text, year)
        future = [d for d in dates if today <= d <= window_end]
        if future:
            ev.calendar_date = future[0].strftime("%Y-%m-%d")
        else:
            ev.calendar_date = today.strftime("%Y-%m-%d")


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


def classify_region(ev: Event) -> str:
    """按地点/举办方把活动分为 国内 / 国外 / 线上（AI 未给出 region 时的规则兜底）。"""
    text = " ".join(
        x
        for x in (
            ev.title or "",
            ev.snippet or "",
            ev.location or "",
            ev.host or "",
            ev.url or "",
            ev.format or "",
            ev.tags or "",
        )
        if x
    )
    low = text.lower()
    # 线上 / 混合：以线上形式参与的一律归入"线上"
    if ev.format in ("线上", "混合"):
        return "线上"
    online_terms = (
        "线上",
        "online",
        "virtual",
        "everywhere, worldwide",
        "全球线上",
        "async",
    )
    if any(t in low for t in online_terms):
        return "线上"
    # 国内：中文城市 / 国内主办方 / 国内平台来源 / 中文域名
    if ev.location and any(c in ev.location for c in CITY_HINTS):
        return "国内"
    if any(c in text for c in CITY_HINTS):
        return "国内"
    if any(k in (ev.host or "").lower() for k in DOMESTIC_ORG_HINTS):
        return "国内"
    if any(k in low for k in DOMESTIC_ORG_HINTS):
        return "国内"
    if ev.source and any(k in ev.source for k in ("公众号", "小红书", "搜狗")):
        return "国内"
    if ".cn" in low or "weixin.sogou" in low or "xiaohongshu" in low:
        return "国内"
    return "国外"


REGION_ORDER = {"国内": 0, "国外": 1, "线上": 2}


def ai_clean_and_review(events: list[Event]) -> tuple[str, int, int, int]:
    """用 LLM（OpenAI 兼容接口，可接 DeepSeek）对每条信息做清洗与审核。

    返回 (审核模式说明, 通过条数, 待人工确认条数, 剔除条数)。
    未配置 API Key 或调用失败时降级为规则清洗，不影响主流程。
    """
    if not LLM_API_KEY:
        for ev in events:
            ev.review_status = "规则清洗（未配置 AI）"
        return "规则清洗（未配置 AI）", len(events), 0, 0

    mode = f"AI 审核（{LLM_MODEL}）"
    prompt = (
        "你是黑客松/编程马拉松信息审核助手。下面是抓取到的活动信息，请逐条清洗并审核。\n"
        "清洗要求：修正标题里的噪音（如'报名倒计时''今晚截止'等前缀可去掉，保留活动名）；"
        "从摘要中提取主办方、主题/赛道、奖金/奖池、报名条件、报名时间、报名截止、竞赛时间、"
        "地点、形式（线上/线下/混合）、状态、标签。无法确定的字段留空字符串。\n"
        "审核重点（按顺序判断）：\n"
        "① 真实性：是否为真实可报名的黑客松/编程马拉松活动。课程广告、招聘、往期回顾、纯讲座、"
        "会议、视频比赛、夏校等一律 keep=false，reason 写明'非黑客松活动'及具体类型。\n"
        "② 时效性：是否为近期开始的活动（今天起未来约30天内）。活动时间明显已过、或属往期内容（如"
        "标题带往年年份、'决赛直播''获奖名单'等）判 keep=false，reason 写明'活动已结束'。\n"
        "③ 报名可行性：报名截止是否已过。已截止或活动已开始的判 keep=false，reason 写明"
        "'报名已截止/活动已开始'及截止日期；只有无法判断活动时间、也无法判断报名截止是否已过的"
        "才标记 needs_review=true。\n"
        "注意：截止日期是'约数/待确认'、或活动时间取自聚合站结构化数据的，都算信息充分，"
        "确认为真实且时间在近期内的直接 keep=true、needs_review=false。"
        "其他信息明显缺失或可疑（如无法确认真实性）才标记 needs_review=true。"
        "每条的 reason 用一句话中文说明。\n"
        "只输出 JSON，格式：{\"items\":[{\"index\":0,\"keep\":true,\"needs_review\":false,"
        "\"cleaned_title\":\"\",\"host\":\"\",\"themes\":\"\",\"prize\":\"\",\"eligibility\":\"\","
        "\"signup_start\":\"\",\"signup_deadline\":\"\",\"competition_time\":\"\","
        "\"location\":\"\",\"format\":\"\",\"status\":\"\",\"tags\":\"\","
        "\"region\":\"国内或国外或线上\",\"reason\":\"\"}]}"
        " region 字段：按举办方/地点判断，线上活动填\"线上\"，国内主办方或国内城市填\"国内\"，其余填\"国外\"。"
    )
    items = [
        {
            "index": i,
            "title": ev.title,
            "url": ev.url,
            "source": ev.source,
            "snippet": (ev.snippet or "")[:300],
            "signup_start": ev.signup_start,
            "signup_deadline": ev.signup_deadline,
            "competition_time": ev.competition_time,
            "location": ev.location,
        }
        for i, ev in enumerate(events)
    ]

    passed = needs_review = dropped = 0
    ok = False
    batch_size = 15
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        payload = {
            "model": LLM_MODEL,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(batch, ensure_ascii=False)},
            ],
        }
        try:
            resp = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
                json={**payload, "response_format": {"type": "json_object"}},
                timeout=90,
            )
            if resp.status_code not in (200, 201):
                resp = requests.post(
                    f"{LLM_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=90,
                )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
            m = re.search(r"\{.*\}", content, re.S)
            result = json.loads(m.group(0)) if m else {}
            verdicts = result.get("items") if isinstance(result, dict) else result
        except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
            print(f"[warn] AI 审核批次失败: {exc}", file=sys.stderr)
            for ev in events[start : start + batch_size]:
                ev.review_status = "规则清洗（AI 调用失败）"
            continue

        by_index: dict[int, dict] = {}
        for v in verdicts:
            if not isinstance(v, dict):
                continue
            try:
                by_index[int(v.get("index"))] = v
            except (TypeError, ValueError):
                continue
        for offset, ev in enumerate(events[start : start + batch_size]):
            v = by_index.get(start + offset)
            if not v:
                ev.review_status = "规则清洗（AI 无返回）"
                continue
            if not v.get("keep", True):
                ev.review_status = "审核不通过"
                ev.review_note = (v.get("reason") or "").strip()
                dropped += 1
                continue
            if v.get("needs_review"):
                ev.review_status = "待人工确认"
                needs_review += 1
            else:
                ev.review_status = "AI 审核通过"
                passed += 1
            if v.get("reason"):
                ev.review_note = (v.get("reason") or "").strip()
            for field, key in (
                ("title", "cleaned_title"),
                ("host", "host"),
                ("themes", "themes"),
                ("prize", "prize"),
                ("eligibility", "eligibility"),
                ("signup_start", "signup_start"),
                ("signup_deadline", "signup_deadline"),
                ("competition_time", "competition_time"),
                ("location", "location"),
                ("format", "format"),
                ("status", "status"),
                ("tags", "tags"),
                ("region", "region"),
            ):
                val = (v.get(key) or "").strip()
                if val:
                    setattr(ev, field, val)
        ok = True
    if not ok:
        mode = "规则清洗（AI 调用失败）"
    return mode, passed, needs_review, dropped


def _llm_completion(system_prompt: str, user_content: str, timeout: int = 90) -> dict | None:
    """调用 OpenAI 兼容 LLM（DeepSeek 等），返回解析后的 JSON dict。"""
    payload = {
        "model": LLM_MODEL,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
            json={**payload, "response_format": {"type": "json_object"}},
            timeout=timeout,
        )
        if resp.status_code not in (200, 201):
            resp = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
        m = re.search(r"\{.*\}", content, re.S)
        return json.loads(m.group(0)) if m else None
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        print(f"[warn] LLM 调用失败: {exc}", file=sys.stderr)
        return None


def _official_score(url: str, ev: Event) -> int:
    """候选来源打分：与已知链接同域名最高，官方/活动平台域名加分。"""
    if not url:
        return 0
    try:
        from urllib.parse import urlparse

        dom = urlparse(url).netloc.lower().replace("www.", "")
        ev_dom = urlparse(ev.url or "").netloc.lower().replace("www.", "")
    except ValueError:
        return 0
    score = 0
    if ev_dom and dom == ev_dom:
        score += 3
    for kw in (
        "mlh",
        "devfolio",
        "hackathon",
        "event",
        "contest",
        "competition",
        "hack",
        "lu.ma",
        "eventbrite",
        "huodongxing",
        "saikr",
    ):
        if kw in dom:
            score += 1
    return score


def verify_events(events: list[Event]) -> tuple[int, int, int]:
    """AI 重新检索核对：定向搜索→抓候选页→LLM 提取准确字段（尤其时间）。

    返回 (已核对, 部分核对, 未能核对)。未配置 AI Key 时整体标记未核对。
    """
    if not LLM_API_KEY:
        for ev in events:
            ev.verify_status = "未核对（未配置 AI）"
        return 0, 0, len(events)

    targets = events[:VERIFY_MAX_EVENTS]
    for ev in events[VERIFY_MAX_EVENTS:]:
        ev.verify_status = "未核对（超出核对上限）"

    candidates: dict[int, list[dict]] = {}

    def gather(idx: int, ev: Event) -> None:
        urls: list[str] = []
        try:
            for q in (f"{ev.title} 报名 截止", f"{ev.title} hackathon registration"):
                found = search_bing(q)
                for r in found[:3]:
                    if r.url and r.url not in urls:
                        urls.append(r.url)
                if not found:
                    found = search_duckduckgo(q)
                    for r in found[:3]:
                        if r.url and r.url not in urls:
                            urls.append(r.url)
                time.sleep(0.4)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 核对搜索失败 {ev.title[:30]}: {exc}", file=sys.stderr)
        pages: list[dict] = []
        for u in urls[:3]:
            text = _fetch(u, timeout=12)
            if not text:
                continue
            try:
                soup = BeautifulSoup(text, "lxml")
                body = soup.get_text(" ", strip=True)
            except Exception:  # noqa: BLE001
                body = ""
            if body:
                pages.append({"url": u, "text": body[:2500]})
        pages.sort(key=lambda p: _official_score(p["url"], ev), reverse=True)
        candidates[idx] = pages[:3]

    with ThreadPoolExecutor(max_workers=6) as pool:
        for idx, ev in enumerate(targets):
            pool.submit(gather, idx, ev)

    verify_prompt = (
        "你是黑客松信息核对员。针对每条活动，我会给你：当前已知信息，以及搜索引擎抓到的候选网页"
        "（标题/URL/正文片段）。请按以下标准核对：\n"
        "1) 找出最可能是官方报名页或权威来源的链接，填入 official_url；\n"
        "2) 提取准确字段，**尤其是报名开始时间、报名截止时间、竞赛时间**，必须是具体日期"
        "（YYYY-MM-DD 或 X月X日，可从正文推断年份）；已知信息与候选网页一致就保留，"
        "不一致以多个来源交叉验证后的结果为准；\n"
        "3) 判定：有官方/权威来源且时间明确 → confidence=high；有来源但部分字段缺失 → medium；"
        "找不到可靠来源或时间无法确定 → low；候选来源之间时间冲突 → conflict=true；\n"
        "4) 核对标准：主办方真实可查、时间与官方一致、链接可访问、报名时间/报名截止/竞赛时间/"
        "地点/主办方五项尽量齐全，缺项在 note 中列出。\n"
        "只输出 JSON：{\"items\":[{\"index\":0,\"official_url\":\"\","
        "\"fields\":{\"signup_start\":\"\",\"signup_deadline\":\"\",\"competition_time\":\"\","
        "\"location\":\"\",\"host\":\"\",\"prize\":\"\",\"eligibility\":\"\",\"format\":\"\","
        "\"status\":\"\",\"region\":\"国内或国外或线上\"},\"confidence\":\"high\",\"conflict\":false,"
        "\"note\":\"一句话中文说明\"}]}"
        " region 字段：线上活动填\"线上\"，国内主办方或国内城市填\"国内\"，其余填\"国外\"。"
    )

    verified = partial = failed = 0
    for start in range(0, len(targets), 5):
        batch = []
        for idx in range(start, min(start + 5, len(targets))):
            ev = targets[idx]
            batch.append(
                {
                    "index": idx,
                    "title": ev.title,
                    "url": ev.url,
                    "known": {
                        "signup_start": ev.signup_start,
                        "signup_deadline": ev.signup_deadline,
                        "competition_time": ev.competition_time,
                        "location": ev.location,
                        "host": ev.host,
                        "prize": ev.prize,
                        "eligibility": ev.eligibility,
                        "format": ev.format,
                        "status": ev.status,
                        "region": ev.region,
                    },
                    "candidates": candidates.get(idx, []),
                }
            )
        result = _llm_completion(
            verify_prompt, json.dumps(batch, ensure_ascii=False), timeout=120
        )
        if not result:
            for ev in targets[start : start + 5]:
                ev.verify_status = "未能核对（AI 调用失败）"
                failed += 1
            continue
        verdicts = result.get("items") if isinstance(result, dict) else []
        for v in verdicts:
            if not isinstance(v, dict):
                continue
            try:
                idx = int(v.get("index"))
            except (TypeError, ValueError):
                continue
            if idx >= len(targets):
                continue
            ev = targets[idx]
            fields = v.get("fields") or {}
            for field, key in (
                ("signup_start", "signup_start"),
                ("signup_deadline", "signup_deadline"),
                ("competition_time", "competition_time"),
                ("location", "location"),
                ("host", "host"),
                ("prize", "prize"),
                ("eligibility", "eligibility"),
                ("format", "format"),
                ("status", "status"),
                ("region", "region"),
            ):
                val = (fields.get(key) or "").strip()
                if val:
                    setattr(ev, field, val)
            official = (v.get("official_url") or "").strip()
            if official:
                ev.official_url = official
            conf = (v.get("confidence") or "low").lower()
            if v.get("conflict"):
                ev.verify_status = "信息冲突"
                failed += 1
            elif conf == "high":
                ev.verify_status = "已核对"
                verified += 1
            elif conf == "medium":
                ev.verify_status = "部分核对"
                partial += 1
            else:
                ev.verify_status = "未能核对"
                failed += 1
            ev.verify_note = (v.get("note") or "").strip()
    return verified, partial, failed


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


def notion_append_children(page_id: str, children: list[dict]) -> None:
    """向已有页面分批追加子块（Notion 单次上限 100 个）。"""
    for i in range(0, len(children), 90):
        batch = children[i : i + 90]
        resp = requests.patch(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=notion_headers(),
            json={"children": batch},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Notion 追加块失败 {resp.status_code}: {resp.text[:500]}")


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


def build_summary_children(date_str: str, events: list[Event], dup_idx: set[int] | None = None) -> list[dict]:
    """构建当天汇总的 Notion 子块（直接写入数据库记录正文）。"""
    dup_idx = dup_idx or set()
    children: list[dict] = []
    dup_count = len(dup_idx)
    archive_ids = _archive_id_map()
    region_counts: dict[str, int] = {}
    for ev in events:
        region_counts[ev.region or "国内"] = region_counts.get(ev.region or "国内", 0) + 1
    region_text = ""
    if region_counts:
        region_text = " 分组：" + " / ".join(
            f"{k} {region_counts.get(k, 0)} 条" for k in ("国内", "国外", "线上") if region_counts.get(k)
        ) + "。"
    review_counts: dict[str, int] = {}
    for ev in events:
        if ev.review_status:
            review_counts[ev.review_status] = review_counts.get(ev.review_status, 0) + 1
    review_text = ""
    if review_counts:
        review_text = " 审核：" + "，".join(f"{k} {v} 条" for k, v in review_counts.items()) + "。"
    verify_counts: dict[str, int] = {}
    for ev in events:
        if ev.verify_status:
            verify_counts[ev.verify_status] = verify_counts.get(ev.verify_status, 0) + 1
    verify_text = ""
    if verify_counts:
        verify_text = " 核对：" + "，".join(f"{k} {v} 条" for k, v in verify_counts.items()) + "。"
    children.append(
        _text_block(
            "paragraph",
            f"**概览**：共收录 {len(events)} 条活动，覆盖 {date_str} 起未来 30 天内的黑客松/编程马拉松信息。"
            + (f"其中 {dup_count} 条与历史已收录信息重合（已标注）。" if dup_count else "")
            + review_text
            + verify_text
            + region_text,
        )
    )
    children.append(_text_block("heading_2", "活动总览"))
    grouped: dict[str, list[tuple[int, Event]]] = {"国内": [], "国外": [], "线上": []}
    for i, ev in enumerate(events, 1):
        grouped.setdefault(ev.region or "国内", []).append((i, ev))
    for region_name in ("国内", "国外", "线上"):
        items = grouped.get(region_name, [])
        if not items:
            continue
        children.append(_text_block("heading_2", f"{region_name}活动（{len(items)} 条）"))
        for i, ev in items:
            title = f"{i}. {ev.title}"
            if i - 1 in dup_idx:
                title += " ⚠️已收录"
            children.append(_text_block("heading_3", title))
            # 字段列表：固定顺序 + 统一占位，保证标准格式
            for label, val in (
                ("🏢 主办方", ev.host),
                ("📌 主题", ev.themes),
                ("🏆 奖金", ev.prize),
                ("👥 报名条件", ev.eligibility),
                ("🌐 形式", ev.format),
                ("🏷️ 标签", ev.tags),
                ("🚦 状态", ev.status),
            ):
                if val:
                    children.append(_labeled_block("bulleted_list_item", label, val))
            children.append(_labeled_block("bulleted_list_item", "📝 报名时间", ev.signup_start or "待确认"))
            children.append(_labeled_block("bulleted_list_item", "⏳ 报名截止", ev.signup_deadline or "待确认"))
            children.append(_labeled_block("bulleted_list_item", "⏰ 竞赛时间", ev.competition_time or "待确认"))
            children.append(_labeled_block("bulleted_list_item", "📍 地点", ev.location or "待确认"))
            children.append(_labeled_block("bulleted_list_item", "摘要", ev.snippet[:180] if ev.snippet else "待确认"))
            if ev.review_status:
                review_line = ev.review_status
                if ev.review_note:
                    review_line += f"：{ev.review_note}"
                children.append(_labeled_block("bulleted_list_item", "🔎 审核", review_line))
            if ev.verify_status:
                verify_line = ev.verify_status
                if ev.verify_note:
                    verify_line += f"：{ev.verify_note}"
                children.append(_labeled_block("bulleted_list_item", "🔬 核对", verify_line))
            if ev.official_url and ev.official_url != ev.url:
                children.append(_link_block("bulleted_list_item", "✅ 官方链接", "查看详情", ev.official_url))
            children.append(
                _link_block(
                    "bulleted_list_item",
                    "🗄️ 信息库",
                    "打开对应记录",
                    _resolve_archive_url(ev.title, archive_ids),
                )
            )
            if ev.url:
                children.append(_link_block("bulleted_list_item", "🔗 链接", "查看详情", ev.url))
            else:
                children.append(_labeled_block("bulleted_list_item", "🔗 链接", "待确认"))
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
    if dup_count:
        children.append(
            _text_block(
                "bulleted_list_item",
                "⚠️ 标有'已收录'的活动与黑客松信息库中的历史条目重合，可能为同一活动或往期重复信息。",
            )
        )
    return children


def write_daily_summary(date_str: str, events: list[Event], dup_idx: set[int] | None = None) -> None:
    """生成每日邮件正文摘要 data/daily_summary.md（供 send_email.py 发送）。"""
    dup_idx = dup_idx or set()
    region_counts: dict[str, int] = {}
    for ev in events:
        region_counts[ev.region or "国内"] = region_counts.get(ev.region or "国内", 0) + 1
    region_text = " / ".join(
        f"{k} {region_counts.get(k, 0)} 条" for k in ("国内", "国外", "线上") if region_counts.get(k)
    )
    lines = [
        f"# {date_str} 黑客松活动汇总",
        "",
        f"共收录 {len(events)} 条活动（今天起未来 30 天内）"
        + (f"，分组：{region_text}" if region_text else ""),
        "",
        "信息库页面：https://wenlinl.github.io/hackathon-daily/archive.html",
        "",
    ]
    grouped: dict[str, list[tuple[int, Event]]] = {"国内": [], "国外": [], "线上": []}
    for i, ev in enumerate(events, 1):
        grouped.setdefault(ev.region or "国内", []).append((i, ev))
    for region_name in ("国内", "国外", "线上"):
        items = grouped.get(region_name, [])
        if not items:
            continue
        lines.append(f"## {region_name}活动（{len(items)} 条）")
        for i, ev in items:
            tag = " ⚠️已收录" if i - 1 in dup_idx else ""
            lines.append(f"### {i}. {ev.title}{tag}")
            for label, val in (
                ("主办方", ev.host),
                ("主题", ev.themes),
                ("奖金", ev.prize),
                ("报名条件", ev.eligibility),
                ("报名时间", ev.signup_start),
                ("报名截止", ev.signup_deadline),
                ("竞赛时间", ev.competition_time),
                ("地点", ev.location),
                ("形式", ev.format),
                ("标签", ev.tags),
                ("状态", ev.status),
                ("审核", ev.review_status),
                ("核对", ev.verify_status),
            ):
                if val:
                    lines.append(f"- {label}：{val}")
            if ev.url:
                lines.append(f"- 链接：{ev.url}")
            lines.append(f"- 信息库：{_resolve_archive_url(ev.title)}")
            lines.append("")
    summary_file = ARCHIVE_FILE.parent / "daily_summary.md"
    summary_file.write_text("\n".join(lines), encoding="utf-8")
    print(f"[info] 已生成邮件摘要 {summary_file}")


def select_balanced(events: list[Event], limit: int = MAX_EVENTS) -> list[Event]:
    """按 国内/国外/线上 分组比例均衡选取最多 limit 条，避免某一组被截断。"""
    buckets: dict[str, list[Event]] = {"国内": [], "国外": [], "线上": []}
    for ev in events:
        buckets.setdefault(ev.region or "国内", []).append(ev)
    total = len(events)
    if not total:
        return []
    quota: dict[str, int] = {}
    for k, v in buckets.items():
        quota[k] = min(len(v), round(limit * len(v) / total))
    leftover = limit - sum(quota.values())
    for k in ("国内", "国外", "线上"):
        if leftover > 0:
            add = min(leftover, len(buckets[k]) - quota[k])
            quota[k] += add
            leftover -= add
    if leftover < 0:
        for k in ("线上", "国外", "国内"):
            if leftover >= 0:
                break
            cut = min(-leftover, quota[k])
            quota[k] -= cut
            leftover += cut
    selected: list[Event] = []
    for k in ("国内", "国外", "线上"):
        selected.extend(buckets[k][: quota[k]])
    return selected


def query_database_rows(db_id: str) -> list[dict]:
    """查询数据库现有行，用于去重。"""
    rows: list[dict] = []
    cursor = None
    while True:
        payload: dict = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        resp = requests.post(
            f"{NOTION_API}/databases/{db_id}/query",
            headers=notion_headers(),
            json=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"查询数据库 {db_id} 失败 {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return rows


def normalize_title(title: str) -> str:
    """标题归一化：小写、去空白/标点，用于查重比较。"""
    text = (title or "").lower()
    text = re.sub(r"[\s,，。.;；:：!！?？|｜\-—_/\\()（）\[\]【】·×&＆#+*~<>《》「」『』]+", "", text)
    # 去掉常见修饰前缀/后缀，减少误判
    text = re.sub(r"^(2026)?[年]?", "", text)
    return text


def _load_archive() -> dict:
    """读取服务器端历史信息库（archive.json）。"""
    if not ARCHIVE_FILE.exists():
        print(f"[warn] 服务器端信息库不存在 {ARCHIVE_FILE}，按空库处理", file=sys.stderr)
        return {"updated": "", "entries": {}}
    try:
        with open(ARCHIVE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"updated": "", "entries": {}}
    except (OSError, ValueError) as exc:
        print(f"[warn] 读取服务器端信息库失败，按空库处理: {exc}", file=sys.stderr)
        return {"updated": "", "entries": {}}


def _save_archive(data: dict) -> None:
    """原子写入服务器端历史信息库（archive.json）。"""
    ARCHIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ARCHIVE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ARCHIVE_FILE)


def entry_id(title: str) -> str:
    """信息库条目稳定锚点 ID：标题归一化后的哈希（保证一一对应且稳定）。"""
    key = normalize_title(title)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _archive_id_map() -> dict[str, str]:
    """读取信息库，返回 {归一化标题: 锚点ID}。"""
    entries = _load_archive().get("entries", {})
    return {key: entry_id(e.get("title") or key) for key, e in entries.items()}


def _resolve_archive_url(title: str, id_map: dict[str, str] | None = None) -> str:
    """解析某活动在信息库页面中的锚点链接（精确匹配优先，其次包含匹配）。"""
    if id_map is None:
        id_map = _archive_id_map()
    norm = normalize_title(title)
    if norm in id_map:
        return f"{ARCHIVE_PAGE_URL}#entry-{id_map[norm]}"
    for key, eid in id_map.items():
        if len(norm) >= 8 and len(key) >= 8 and (norm in key or key in norm):
            return f"{ARCHIVE_PAGE_URL}#entry-{eid}"
    return f"{ARCHIVE_PAGE_URL}#entry-{entry_id(title)}"


def build_archive_html() -> None:
    """根据服务器端信息库生成可浏览的 archive.html（每条目带锚点，供日报跳转）。"""
    data = _load_archive()
    entries = data.get("entries", {})
    items = sorted(
        (
            {
                "id": entry_id(e.get("title") or key),
                "title": e.get("title") or key,
                "entry": e,
            }
            for key, e in entries.items()
        ),
        key=lambda x: x["entry"].get("first_seen", ""),
        reverse=True,
    )
    cards: list[str] = []
    for it in items:
        e = it["entry"]
        fields: list[str] = []
        for label, val in (
            ("首次收录", e.get("first_seen")),
            ("报名截止", e.get("deadline")),
            ("竞赛时间", e.get("competition_time")),
            ("地点", e.get("location")),
            ("主办方", e.get("host")),
            ("主题", e.get("themes")),
            ("奖金", e.get("prize")),
            ("报名条件", e.get("eligibility")),
            ("形式", e.get("format")),
            ("标签", e.get("tags")),
            ("状态", e.get("status")),
            ("分组", e.get("region")),
            ("来源", e.get("source")),
            ("核对", e.get("review_status")),
        ):
            if val:
                fields.append(f"<li><b>{html_escape(label)}</b>：{html_escape(str(val))}</li>")
        links: list[str] = []
        if e.get("official_url"):
            links.append(f'<a href="{html_escape(e["official_url"])}" target="_blank" rel="noopener">官方链接</a>')
        if e.get("url"):
            links.append(f'<a href="{html_escape(e["url"])}" target="_blank" rel="noopener">来源链接</a>')
        links_html = " · ".join(links) if links else '<span class="muted">无链接</span>'
        summary = e.get("summary") or ""
        cards.append(
            '<section class="card" id="entry-%s">'
            "<h2>%s</h2><p class=\"meta\">ID: entry-%s · %s</p>"
            "<ul>%s</ul><p class=\"sum\">%s</p><p class=\"links\">%s</p></section>"
            % (
                it["id"],
                html_escape(it["title"]),
                it["id"],
                html_escape(str(e.get("first_seen") or "")),
                "".join(fields),
                html_escape(summary)[:300],
                links_html,
            )
        )
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>黑客松信息库</title>
<style>
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f6f7f9;color:#1f2328}}
header{{position:sticky;top:0;background:#fff;border-bottom:1px solid #e2e4e8;padding:16px 24px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}}
h1{{font-size:18px;margin:0}}
input{{flex:1;min-width:240px;padding:8px 12px;border:1px solid #d0d3d8;border-radius:8px;font-size:14px}}
#count{{color:#656d76;font-size:13px;white-space:nowrap}}
main{{max-width:960px;margin:24px auto;padding:0 16px}}
.card{{background:#fff;border:1px solid #e2e4e8;border-radius:12px;padding:16px 20px;margin-bottom:16px}}
.card h2{{margin:0 0 6px;font-size:16px}}
.meta{{color:#656d76;font-size:12px;margin:0 0 8px}}
.card ul{{margin:0 0 8px;padding-left:18px;font-size:14px;line-height:1.7}}
.sum{{color:#57606a;font-size:13px;margin:0 0 8px}}
.links a{{color:#0969da;text-decoration:none;margin-right:8px}}
.links a:hover{{text-decoration:underline}}
.muted{{color:#8b949e}}
</style>
</head>
<body>
<header><h1>黑客松信息库</h1><input id="q" type="search" placeholder="搜索活动名称/地点/主办方…"><span id="count">共 {len(cards)} 条</span></header>
<main>
{"".join(cards)}
</main>
<script>
const q=document.getElementById('q');
q.addEventListener('input',()=>{{
  const k=q.value.trim().toLowerCase();
  let n=0;
  document.querySelectorAll('.card').forEach(c=>{{
    const hit=!k||c.textContent.toLowerCase().includes(k);
    c.style.display=hit?'':'none';
    if(hit)n++;
  }});
  document.getElementById('count').textContent='显示 '+n+' / {len(cards)} 条';
}});
</script>
</body>
</html>"""
    ARCHIVE_HTML.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE_HTML.write_text(page, encoding="utf-8")
    print(f"[info] 已生成信息库页面 {ARCHIVE_HTML}（{len(cards)} 条，含锚点）")


def load_archive_titles() -> set[str]:
    """读取服务器端信息库中已收录的活动标题（归一化后），用于查重。"""
    data = _load_archive()
    return set(data.get("entries", {}).keys())


def find_duplicates(events: list[Event], archive_titles: set[str]) -> set[int]:
    """返回与历史库重合的活动索引。匹配规则：标题归一化后完全一致，或一方包含另一方。"""
    dup_idx: set[int] = set()
    for i, ev in enumerate(events):
        norm = normalize_title(ev.title)
        if not norm:
            continue
        if norm in archive_titles:
            dup_idx.add(i)
            continue
        for hist in archive_titles:
            if len(norm) >= 8 and len(hist) >= 8 and (norm in hist or hist in norm):
                dup_idx.add(i)
                break
    return dup_idx


def write_to_archive(date_str: str, events: list[Event]) -> None:
    """把今天搜到的活动累积写入服务器端信息库 archive.json（按归一化标题去重）。"""
    data = _load_archive()
    entries = data.get("entries", {})
    written = 0
    for ev in events:
        norm = normalize_title(ev.title)
        if not norm or norm in entries:
            continue
        dl = ev.signup_deadline[:10] if ev.signup_deadline and ev.signup_deadline[:4].isdigit() else ""
        entries[norm] = {
            "title": (ev.title or "")[:190],
            "first_seen": date_str,
            "url": ev.url,
            "source": ev.source,
            "location": ev.location,
            "deadline": dl,
            "competition_time": ev.competition_time,
            "host": ev.host,
            "themes": ev.themes,
            "prize": ev.prize,
            "eligibility": ev.eligibility,
            "format": ev.format,
            "tags": ev.tags,
            "status": ev.status,
            "region": ev.region,
            "review_status": ev.review_status,
            "summary": (ev.snippet or "")[:500],
        }
        written += 1
    if written:
        data["entries"] = entries
        data["updated"] = date_str
        _save_archive(data)
    print(f"[info] 信息库新增 {written} 条")


def write_to_calendar(date_str: str, events: list[Event], dup_idx: set[int] | None = None) -> None:
    """把当天汇总直接写入 2026 数据库（日历视图）当天记录，正文包含全部活动详情。"""
    title_prop = "名称"  # 2026 数据库标题属性（已验证）
    try:
        existing = query_database_rows(DB_2026_ID)
    except RuntimeError as exc:
        print(f"[warn] 无法查询 2026 数据库，跳过日历写入: {exc}", file=sys.stderr)
        return

    existing_keys: set[tuple[str, str]] = set()
    summary_ids: list[str] = []
    for row in existing:
        props = row.get("properties", {})
        title = ""
        date = ""
        for p in props.values():
            if p.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in p.get("title", []))
            if p.get("type") == "date" and p.get("date"):
                date = p["date"].get("start", "")
        if title and date:
            existing_keys.add((title.strip(), date))
        # 旧的“当天汇总”条目（含带/不带条数后缀），自动归档
        if "黑客松活动汇总" in title:
            summary_ids.append(row["id"])

    title = f"{date_str} 黑客松活动汇总（{len(events)} 条）"
    # 同日同标题已存在 → 直接跳过（避免把现有记录归档后又不重建）
    if (title, date_str) in existing_keys:
        print(f"[skip] 日历已存在: {title} @ {date_str}")
        return

    for pid in summary_ids:
        try:
            resp = requests.patch(
                f"{NOTION_API}/pages/{pid}",
                headers=notion_headers(),
                json={"archived": True},
                timeout=30,
            )
            if resp.status_code == 200:
                print(f"[ok] 已归档旧汇总日历条目 {pid}")
        except requests.RequestException as exc:
            print(f"[warn] 归档旧条目失败 {pid}: {exc}", file=sys.stderr)

    children = build_summary_children(date_str, events, dup_idx)
    first_batch = children[:90]
    rest = children[90:]
    properties = {
        title_prop: {
            "title": [{"type": "text", "text": {"content": title}}]
        },
        "日期": {"date": {"start": date_str}},
    }
    try:
        page = notion_create_page(
            {"type": "database_id", "database_id": DB_2026_ID},
            properties,
            children=first_batch,
        )
        if rest:
            notion_append_children(page["id"], rest)
        print(f"[ok] 日历写入汇总（含正文）: {title} @ {date_str}")
    except RuntimeError as exc:
        print(f"[warn] 日历写入失败: {exc}", file=sys.stderr)


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
    parser.add_argument("--no-ai", action="store_true", help="跳过 AI 清洗/审核（仅规则清洗）")
    parser.add_argument("--no-verify", action="store_true", help="跳过 AI 重新检索核对")
    parser.add_argument("--build-archive", action="store_true", help="只根据现有信息库重新生成 archive.html")
    args = parser.parse_args()

    if args.check_notion:
        return check_notion()
    if args.build_archive:
        build_archive_html()
        return 0

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
        key = normalize_title(ev.title)
        if key and key not in seen:
            seen.add(key)
            unique.append(ev)

    # 只保留今天起未来 30 天内的活动，并提取时间/地点等字段
    unique = [ev for ev in unique if is_in_future_window(ev, today)]
    for ev in unique:
        extract_fields(ev, today)

    # AI 清洗与审核：真实黑客松？近期开始？报名还来得及？（未配置 Key 时自动降级）
    if not args.no_ai:
        mode, passed, needs_review, dropped = ai_clean_and_review(unique)
        unique = [ev for ev in unique if ev.review_status != "审核不通过"]
        print(
            f"[info] {mode}：通过 {passed} 条，待人工确认 {needs_review} 条，"
            f"剔除 {dropped} 条，剩余 {len(unique)} 条"
        )
        # AI 清洗可能把不同原文归一成同一标题，去重一次
        seen_titles: set[str] = set()
        deduped: list[Event] = []
        for ev in unique:
            key = normalize_title(ev.title)
            if not key or key in seen_titles:
                continue
            seen_titles.add(key)
            deduped.append(ev)
        unique = deduped

    # AI 重新检索核对：定向搜索官方来源并提取准确时间等字段（按核对标准）
    if not args.no_verify:
        verified, partial, failed = verify_events(unique)
        print(
            f"[info] 核对：已核对 {verified}，部分核对 {partial}，"
            f"未能核对 {failed}（共 {len(unique)} 条，上限 {VERIFY_MAX_EVENTS}）"
        )
        # 核对可能修正了时间字段，重新提取一次，保证日历日期准确
        for ev in unique:
            extract_fields(ev, today)

    # 国内/国外/线上分组（AI 未给出时用规则兜底），并按分组顺序排列
    for ev in unique:
        if not ev.region:
            ev.region = classify_region(ev)
    unique.sort(key=lambda ev: (REGION_ORDER.get(ev.region, 3), 0))

    # 按 国内/国外/线上 分组比例均衡选取，避免某组被上限截断
    selected = select_balanced(unique, MAX_EVENTS)
    print(
        f"[info] 共收集 {len(unique)} 条活动（未来 {LOOKAHEAD_DAYS} 天内），"
        f"今日展示 {len(selected)} 条"
    )
    for ev in selected:
        print(
            f"  - {ev.title} | {ev.location or '地点待确认'} | "
            f"{ev.signup_deadline or '截止待确认'} | 日历:{ev.calendar_date}"
        )

    # 查重：与黑客松信息库中的历史条目比对
    archive_titles = load_archive_titles()
    dup_idx = find_duplicates(selected, archive_titles)
    print(f"[info] 查重完成：{len(dup_idx)} 条与历史重合")
    for i in sorted(dup_idx):
        print(f"  ⚠️ 重合: {unique[i].title[:50]}")

    if dry_run:
        print("\n[dry-run] 以下为将写入 Notion 的汇总：")
        print(f"# {date_str} 黑客松活动汇总")
        for i, ev in enumerate(selected):
            tag = " ⚠️已收录" if i in dup_idx else ""
            print(ev.as_markdown().replace(f"### {ev.title}", f"### {ev.title}{tag}"))
            print(f"  日历日期：{ev.calendar_date}")
        return 0

    if not unique:
        print("[info] 未来 30 天内没有搜到活动，仍然创建汇总页面")

    write_to_archive(date_str, selected)
    build_archive_html()
    write_to_calendar(date_str, selected, dup_idx)
    write_daily_summary(date_str, selected, dup_idx)
    print("[done] 全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
