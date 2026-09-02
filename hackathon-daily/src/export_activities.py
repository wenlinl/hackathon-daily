#!/usr/bin/env python3
"""导出 Notion Activities-Public 数据库 → 飞书同步数据文件 activities-public.json。

用途：在把活动写入 Notion 后，刷新 assets/飞书知识库/activities-public.json，
供 tools/feishu-wiki/sync-activities-base.js 同步到飞书多维表格。

用法：
    python3 src/export_activities.py --output /path/to/activities-public.json
    python3 src/export_activities.py --output ... --stdout   # 同时打印记录数
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402


def _plain_text(prop: dict) -> str:
    return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))


def _date(prop: dict):
    """Notion date 属性 → {start, end, tz}，空日期返回 None。"""
    d = (prop or {}).get("date")
    if not d or not d.get("start"):
        return None
    return {
        "start": d.get("start"),
        "end": d.get("end"),
        "tz": d.get("time_zone"),
    }


def _select(prop: dict):
    sel = (prop or {}).get("select")
    return (sel or {}).get("name") if sel else None


def _status(prop: dict):
    st = (prop or {}).get("status")
    return (st or {}).get("name") if st else None


def row_to_export(row: dict) -> dict:
    props = row.get("properties", {})
    title = "".join(t.get("plain_text", "") for t in props.get("名称", {}).get("title", []))
    url = (props.get("来源链接") or {}).get("url")
    return {
        "id": row["id"],
        "名称": title,
        "提交日期": _date(props.get("提交日期")),
        "优先级": _select(props.get("优先级")),
        "地点": _plain_text(props.get("地点", {})),
        "备注": _plain_text(props.get("备注", {})),
        "摘要": _plain_text(props.get("摘要", {})),
        "类型": _select(props.get("类型")),
        "状态": _status(props.get("状态")),
        "日期": _date(props.get("日期")),
        "报名截止": _date(props.get("报名截止")),
        "来源链接": url or "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="输出 JSON 文件路径")
    parser.add_argument("--stdout", action="store_true", help="额外打印导出记录数")
    args = parser.parse_args()

    rows = collect.query_database_rows(collect.DB_HACKATHON_ID)
    records = [row_to_export(r) for r in rows]
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(records, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"[done] 已导出 {len(records)} 条 → {out}")
    if args.stdout:
        print(json.dumps(records, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
