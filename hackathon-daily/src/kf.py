#!/usr/bin/env python3
"""微信客服（WeChat KF）消息拉取与收录。

微信客服的回调事件（kf_msg_or_event）只通知"有新消息"，不含正文；
需要再调用官方 sync_msg 接口按 open_kfid + cursor 拉取具体消息。
这里拉取 link / text 消息后交给 ingest 引擎分析并写入 Notion。

环境变量：
    WECOM_CORP_ID     企业微信 CorpID
    WECOM_KF_SECRET   微信客服 corpSecret（kf.weixin.qq.com 开发配置获取）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402
import ingest  # noqa: E402
import ocr  # noqa: E402

KF_API = "https://qyapi.weixin.qq.com/cgi-bin/kf"
CURSOR_FILE = Path(__file__).resolve().parent.parent / "data" / "kf_cursor.json"
# 游标持久化页面（Notion，Render 容器磁盘重启会丢本地文件，故写入 Notion 兜底）
CURSOR_PAGE_ID = os.environ.get("WECOM_KF_CURSOR_PAGE_ID", "")

_token_cache: dict[str, str | float] = {"token": "", "expire": 0.0}
_seen_msgids: set[str] = set()
_sync_lock = threading.Lock()

# 微信客服 48 小时内最多回复 5 条/客户，逐条回执会被限流（errcode 95001）。
# 改为按批次合并成一条汇总回执：短延迟（8s）内的事件合并发送。
_reply_buffer: dict[tuple[str, str], list[str]] = {}
_reply_lock = threading.Lock()
_flush_timer: threading.Timer | None = None
_REPLY_DEBOUNCE_SECONDS = 8.0


def get_access_token() -> str:
    """获取微信客服 access_token（带 5 分钟提前量缓存）。"""
    now = time.time()
    if _token_cache["token"] and now < float(_token_cache["expire"]) - 60:
        return str(_token_cache["token"])
    corp_id = os.environ.get("WECOM_CORP_ID", "")
    secret = os.environ.get("WECOM_KF_SECRET", "")
    if not (corp_id and secret):
        raise RuntimeError("缺少 WECOM_CORP_ID / WECOM_KF_SECRET")
    resp = requests.get(
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        params={"corpid": corp_id, "corpsecret": secret},
        timeout=20,
    )
    data = resp.json()
    if data.get("errcode", 0) != 0:
        raise RuntimeError(f"gettoken 失败: {data}")
    _token_cache["token"] = data["access_token"]
    _token_cache["expire"] = now + float(data.get("expires_in", 7200))
    return str(_token_cache["token"])


def _load_cursors() -> dict[str, str]:
    """读取游标：优先本地文件，其次 Notion 页面（跨重启持久）。"""
    try:
        local = json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
    except Exception:
        local = {}
    if local:
        return {k: v for k, v in local.items() if isinstance(v, str)}
    if not CURSOR_PAGE_ID:
        return {}
    try:
        for block in collect.notion_get_children(CURSOR_PAGE_ID):
            btype = block.get("type", "")
            rich = (block.get(btype) or {}).get("rich_text", []) if btype else []
            text = "".join(t.get("plain_text", "") for t in rich).strip()
            if text.startswith("{"):
                data = json.loads(text)
                if isinstance(data, dict):
                    return {k: v for k, v in data.items() if isinstance(v, str)}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 读取 Notion 游标失败: {exc}", file=sys.stderr)
    return {}


def _save_cursors(cursors: dict[str, str]) -> None:
    """保存游标：写本地文件 + 写入 Notion 页面（重启后可用）。"""
    try:
        CURSOR_FILE.write_text(
            json.dumps(cursors, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 保存客服游标失败: {exc}", file=sys.stderr)
    if not CURSOR_PAGE_ID or not cursors:
        return
    text = json.dumps(cursors, ensure_ascii=False)
    try:
        children = collect.notion_get_children(CURSOR_PAGE_ID)
        para_id = ""
        for block in children:
            if block.get("type") == "paragraph":
                para_id = block["id"]
                break
        payload = {
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:1990]}}]}
        }
        if para_id:
            requests.patch(
                f"{collect.NOTION_API}/blocks/{para_id}",
                headers=collect.notion_headers(),
                json=payload,
                timeout=30,
            )
        else:
            requests.patch(
                f"{collect.NOTION_API}/blocks/{CURSOR_PAGE_ID}/children",
                headers=collect.notion_headers(),
                json={"children": [{"object": "block", "type": "paragraph", **payload}]},
                timeout=30,
            )
        print(f"[info] 客服游标已持久化到 Notion: {text[:36]}...")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 写入 Notion 游标失败: {exc}", file=sys.stderr)


def _handle_msg(msg: dict) -> None:
    msgid = msg.get("msgid", "")
    if not msgid or msgid in _seen_msgids:
        return
    _seen_msgids.add(msgid)
    if len(_seen_msgids) > 2000:
        _seen_msgids.clear()
    if msg.get("origin") != 3:
        return  # 只处理微信客户发来的消息
    msgtype = msg.get("msgtype", "")
    try:
        if msgtype == "link":
            link = msg.get("link", {}) or {}
            result = ingest.ingest(
                title=(link.get("title") or "").strip(),
                link=(link.get("url") or "").strip(),
                desc=(link.get("desc") or "").strip(),
            )
        elif msgtype == "text":
            text = ((msg.get("text") or {}).get("content") or "").strip()
            result = ingest.ingest(text=text)
        elif msgtype == "image":
            media_id = ((msg.get("image") or {}).get("media_id") or "")
            image_bytes = _download_media(media_id)
            if not image_bytes:
                result = {"ok": False, "title": "", "message": "添加失败：图片下载失败"}
            else:
                text = ocr.extract_text_from_image(image_bytes)
                if not text:
                    result = {
                        "ok": False,
                        "title": "",
                        "message": "未能从图片中识别出文字，请直接转发文章链接或粘贴正文",
                    }
                else:
                    result = ingest.ingest(text=text, image_bytes=image_bytes)
        else:
            print(f"[info] 客服消息忽略类型: {msgtype}")
            return
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 客服消息处理失败: {exc}", file=sys.stderr)
        result = {
            "ok": False,
            "title": "",
            "message": f"添加失败：{exc}",
        }
    if result:
        external_userid = msg.get("external_userid", "")
        open_kfid = msg.get("open_kfid", "")
        print(f"[info] 回执：{result['message']}")
        if external_userid and open_kfid:
            _queue_reply(external_userid, open_kfid, result["message"])


def _queue_reply(external_userid: str, open_kfid: str, text: str) -> None:
    with _reply_lock:
        _reply_buffer.setdefault((external_userid, open_kfid), []).append(text)
        global _flush_timer
        if _flush_timer is None or not _flush_timer.is_alive():
            _flush_timer = threading.Timer(_REPLY_DEBOUNCE_SECONDS, _flush_replies)
            _flush_timer.daemon = True
            _flush_timer.start()


def _flush_replies() -> None:
    global _flush_timer
    with _reply_lock:
        items = list(_reply_buffer.items())
        _reply_buffer.clear()
        _flush_timer = None
    for (external_userid, open_kfid), msgs in items:
        if not msgs:
            continue
        if len(msgs) == 1:
            body = msgs[0]
        else:
            ok = [m for m in msgs if m.startswith("已添加")]
            fail = [m for m in msgs if not m.startswith("已添加")]
            lines = []
            if ok:
                titles = [m[len("已添加："):] for m in ok]
                lines.append(f"✅ 已收录 {len(ok)} 条：" + "；".join(titles))
            if fail:
                lines.append(f"⚠️ 未收录 {len(fail)} 条：" + "；".join(fail))
            body = "\n".join(lines)
        _send_kf_message(external_userid, open_kfid, body[:1900])


def _download_media(media_id: str) -> bytes | None:
    """通过企微 media/get 下载图片消息内容。"""
    if not media_id:
        return None
    for attempt in range(3):
        try:
            token = get_access_token()
            resp = requests.get(
                "https://qyapi.weixin.qq.com/cgi-bin/media/get",
                params={"access_token": token, "media_id": media_id},
                timeout=90,
            )
            ctype = resp.headers.get("Content-Type", "")
            if resp.status_code != 200 or ctype.startswith("application/json") or ctype.startswith("text/"):
                print(f"[warn] 下载媒体失败: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
                return None
            return resp.content
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 下载媒体异常（第 {attempt + 1} 次）: {exc}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return None


def _send_kf_message(external_userid: str, open_kfid: str, text: str) -> bool:
    """通过微信客服 send_msg 接口回复客户；返回是否成功。"""
    if not text:
        return False
    try:
        token = get_access_token()
        resp = requests.post(
            f"{KF_API}/send_msg",
            params={"access_token": token},
            json={
                "touser": external_userid,
                "open_kfid": open_kfid,
                "msgtype": "text",
                "text": {"content": text[:1900]},
            },
            timeout=20,
        )
        data = resp.json()
        if data.get("errcode", 0) != 0:
            print(f"[warn] 客服回复失败: {data}", file=sys.stderr)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 客服回复异常: {exc}", file=sys.stderr)
        return False


def sync(open_kfid: str, cb_token: str = "") -> None:
    """拉取指定客服账号的新消息并收录。事件回调触发时调用。"""
    with _sync_lock:
        token = get_access_token()
        cursors = _load_cursors()
        cursor = cursors.get(open_kfid, "")
        while True:
            body: dict = {"token": cb_token, "open_kfid": open_kfid, "limit": 1000}
            if cursor:
                body["cursor"] = cursor
            resp = requests.post(
                f"{KF_API}/sync_msg",
                params={"access_token": token},
                json=body,
                timeout=30,
            )
            data = resp.json()
            if data.get("errcode", 0) != 0:
                print(f"[error] sync_msg 失败: {data}", file=sys.stderr)
                return
            for msg in data.get("msg_list", []) or []:
                _handle_msg(msg)
            next_cursor = data.get("next_cursor", "")
            if next_cursor:
                cursors[open_kfid] = next_cursor
                _save_cursors(cursors)
                cursor = next_cursor
            if not data.get("has_more"):
                break


def main() -> int:
    """命令行入口：python kf.py --sync 补拉所有已配置客服账号的新消息。

    环境变量 WECOM_KF_OPEN_IDS 用逗号分隔多个 open_kfid。
    """
    parser = argparse.ArgumentParser(description="微信客服消息同步（兜底补拉）")
    parser.add_argument("--sync", action="store_true", help="拉取并处理所有客服账号的新消息")
    args = parser.parse_args()
    if not args.sync:
        parser.print_help()
        return 1
    open_ids = [x.strip() for x in os.environ.get("WECOM_KF_OPEN_IDS", "").split(",") if x.strip()]
    if not open_ids:
        print("[warn] 未配置 WECOM_KF_OPEN_IDS，无可同步的客服账号", file=sys.stderr)
        return 1
    for open_kfid in open_ids:
        print(f"[info] 兜底同步 {open_kfid}")
        sync(open_kfid, "")
    print("[done] 兜底同步完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
