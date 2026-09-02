#!/usr/bin/env python3
"""微信转发消息 → 分析 → Notion 收录引擎。

收到一条转发来的微信文章（标题/描述/链接、全文或长图），做 AI 字段提取与类型分类，
写入 Notion Activities-Public 数据库。
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


def ingest(
    title: str = "",
    link: str = "",
    desc: str = "",
    text: str = "",
    dry_run: bool = False,
    image_bytes: bytes | None = None,
) -> dict:
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
        if image_bytes and not ev.source_image_url:
            ev.source_image_url = collect.upload_source_image(image_bytes, ev.title)
        print(ev.as_markdown())

        if dry_run:
            print("[dry-run] 不写入 Notion")
            return {"ok": True, "title": ev.title, "message": f"已添加：{ev.title}（dry-run）"}

        urls = collect.upsert_notion_archive(date_str, [ev])
        ev.archive_url = urls.get(collect.normalize_title(ev.title), "")
        print("[done] 已收录到 Notion Activities-Public 数据库")

        # 最终步骤：同步到飞书知识库（食刻Shike / public-Activity，可选）
        try:
            import feishu_sync

            feishu_sync.sync_events([ev], source="微信转发-手动收录")
        except ImportError:
            pass  # feishu_sync.py 不存在时静默跳过
        except Exception as exc:  # noqa: BLE001 飞书失败不影响 Notion 主流程
            print(f"[warn] 飞书同步失败（不影响主流程）: {exc}", file=sys.stderr)
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
