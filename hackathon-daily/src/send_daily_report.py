#!/usr/bin/env python3
"""每晚 00:00（北京时间）把当天黑客松日报发送给 Ares（aresleng@sina.com）。

数据来源：Notion 2026 日历库当天的"黑客松活动汇总"note 正文；
当天没有日报时发送"今天暂无转发收录的黑客松信息"。

环境变量：
    NOTION_TOKEN    Notion Integration Token
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / EMAIL_TO（默认 aresleng@sina.com）
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402
from send_email import send_markdown_email  # noqa: E402


def today_report_markdown() -> tuple[str, str]:
    """返回 (邮件主题, Markdown 正文)。"""
    today = dt.datetime.now(collect.BEIJING_TZ).date()
    date_str = today.isoformat()
    rows = collect.query_database_rows(collect.DB_2026_ID)
    page_id = ""
    for row in rows:
        props = row.get("properties", {})
        title = "".join(t.get("plain_text", "") for t in props.get("名称", {}).get("title", []))
        date = (props.get("日期", {}).get("date") or {}).get("start", "")
        if "黑客松" in title and date == date_str:
            page_id = row["id"]
            break
    if not page_id:
        return f"黑客松日报 {date_str}", f"今天（{date_str}）暂无转发收录的黑客松信息。"

    lines = [f"# 黑客松日报 {date_str}"]
    for block in collect.notion_get_children(page_id):
        btype = block.get("type", "")
        rich = (block.get(btype) or {}).get("rich_text", []) if btype else []
        text = "".join(t.get("plain_text", "") for t in rich).strip()
        if not text:
            continue
        if btype == "heading_1":
            lines.append(f"# {text}")
        elif btype == "heading_2":
            lines.append(f"## {text}")
        elif btype == "heading_3":
            lines.append(f"### {text}")
        elif btype in ("bulleted_list_item", "numbered_list_item"):
            lines.append(f"- {text}")
        else:
            lines.append(text)
    return f"黑客松日报 {date_str}", "\n".join(lines)


def main() -> int:
    try:
        subject, body = today_report_markdown()
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 生成日报失败: {exc}", file=sys.stderr)
        return 1
    print(body[:600])
    return send_markdown_email(subject, body)


if __name__ == "__main__":
    raise SystemExit(main())
