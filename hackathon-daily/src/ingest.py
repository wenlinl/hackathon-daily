#!/usr/bin/env python3
"""微信转发消息 → 分析 → Notion 收录引擎。

收到一条转发来的微信文章（标题/描述/链接、全文或长图），做 AI 字段提取与类型分类，
写入 Notion Activities 数据库，并追加到当天的 Notion 黑客松日报。
手动转发内容视为用户已审核：不做真实性/时效性/地点拦截，一律收录。

用法：
    python ingest.py --title "标题" --link "https://mp.weixin.qq.com/s/..." --desc "描述"
    python ingest.py --text "文章全文……"
    python ingest.py --dry-run --title ...   # 只分析不写入
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402  复用 collect.py 的 Notion/AI/字段能力


def _today() -> dt.date:
    return dt.datetime.now(collect.BEIJING_TZ).date()


def _build_event(title: str, link: str, desc: str, text: str) -> collect.Event | None:
    title = (title or "").strip()
    if not title and (text or "").strip():
        title = collect.extract_title_from_text(text)
    if not title:
        return None
    snippet = (text or desc or "").strip()
    if len(snippet) < 20:
        snippet = f"{title} 黑客松活动信息。"
    snippet_limit = 6000 if (text or "").strip() else 1200
    ev = collect.Event(
        title=title[:190],
        url=(link or "").strip(),
        snippet=snippet[:snippet_limit],
        source="微信转发-手动收录",
    )
    collect.extract_fields(ev, _today())
    return ev


def _append_to_today_note(date_str: str, ev: collect.Event) -> None:
    """把活动追加到当天 Notion 黑客松日报；当天没有日报则创建一条。"""
    try:
        rows = collect.query_database_rows(collect.DB_2026_ID)
    except RuntimeError as exc:
        print(f"[warn] 无法读取 2026 数据库，跳过日报写入: {exc}", file=sys.stderr)
        return
    page_id = ""
    for row in rows:
        props = row.get("properties", {})
        title = "".join(t.get("plain_text", "") for t in props.get("名称", {}).get("title", []))
        date = (props.get("日期", {}).get("date") or {}).get("start", "")
        if "黑客松活动汇总" in title and date == date_str:
            page_id = row["id"]
            break
    children = [collect._text_block("heading_3", ev.title)]
    children.extend(collect.build_event_children(ev))
    if page_id:
        # 防重复：日报里已有同名活动则跳过追加（游标丢失重拉旧消息时避免重复）
        existing_titles = set()
        for block in collect.notion_get_children(page_id):
            btype = block.get("type")
            if btype == "heading_3":
                txt = "".join(
                    t.get("plain_text", "")
                    for t in (block.get(btype) or {}).get("rich_text", [])
                )
                existing_titles.add(collect.normalize_title(txt))
        if collect.normalize_title(ev.title) in existing_titles:
            print(f"[info] 当天日报已包含「{ev.title[:30]}」，跳过追加")
            return
        collect.notion_append_children(page_id, children)
        print(f"[ok] 已追加到当天日报 {page_id}")
    else:
        props = {
            "名称": {"title": [{"type": "text", "text": {"content": f"{date_str} 黑客松活动汇总（手动）"}}]},
            "日期": {"date": {"start": date_str}},
        }
        page = collect.notion_create_page(
            {"type": "database_id", "database_id": collect.DB_2026_ID},
            props,
            children=children[:90],
        )
        if len(children) > 90:
            collect.notion_append_children(page["id"], children[90:])
        print(f"[ok] 已创建当天日报 {page.get('url')}")


def ingest(title: str = "", link: str = "", desc: str = "", text: str = "", dry_run: bool = False) -> dict:
    """分析并收录一条转发来的微信文章。

    返回 {"ok": bool, "title": str, "message": str}：
    - ok=True  成功入库，message 形如 "已添加：{标题}"
    - ok=False 失败/未收录，message 包含失败原因（用于微信回执）
    """
    today = _today()
    date_str = today.isoformat()
    ev = _build_event(title, link, desc, text)
    if ev is None:
        print("[error] 缺少标题", file=sys.stderr)
        return {"ok": False, "title": "", "message": "添加失败：缺少标题（转发内容不完整）"}
    print(f"[info] 收到转发：{ev.title}")

    try:
        # 手动转发：用户已审核，不做拦截，仅做字段提取与类型分类
        collect.ai_extract_fields(ev)
        if not ev.region:
            ev.region = collect.classify_region(ev)
        ev.review_status = "用户已审核（手动转发）"
        print(ev.as_markdown())

        if dry_run:
            print("[dry-run] 不写入 Notion")
            return {"ok": True, "title": ev.title, "message": f"已添加：{ev.title}（dry-run）"}

        urls = collect.upsert_notion_archive(date_str, [ev])
        ev.archive_url = urls.get(collect.normalize_title(ev.title), "")
        _append_to_today_note(date_str, ev)
        print("[done] 已收录到 Notion 黑客松数据库与当天日报")
        return {"ok": True, "title": ev.title, "message": f"已添加：{ev.title}"}
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 收录失败: {exc}", file=sys.stderr)
        return {"ok": False, "title": getattr(ev, "title", ""), "message": f"添加失败：{exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="微信转发文章收录引擎")
    parser.add_argument("--title", default="", help="文章标题")
    parser.add_argument("--link", default="", help="文章链接")
    parser.add_argument("--desc", default="", help="文章描述/摘要")
    parser.add_argument("--text", default="", help="文章全文（文本消息时用）")
    parser.add_argument("--dry-run", action="store_true", help="只分析不写入")
    args = parser.parse_args()
    result = ingest(args.title, args.link, args.desc, args.text, args.dry_run)
    print(result["message"])
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
