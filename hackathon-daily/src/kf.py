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

import json
import os
import sys
import threading
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ingest  # noqa: E402

KF_API = "https://qyapi.weixin.qq.com/cgi-bin/kf"
CURSOR_FILE = Path(__file__).resolve().parent.parent / "data" / "kf_cursor.json"

_token_cache: dict[str, str | float] = {"token": "", "expire": 0.0}
_seen_msgids: set[str] = set()
_sync_lock = threading.Lock()


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
    try:
        return json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cursors(cursors: dict[str, str]) -> None:
    try:
        CURSOR_FILE.write_text(
            json.dumps(cursors, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 保存客服游标失败: {exc}", file=sys.stderr)


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
            ingest.ingest(
                title=(link.get("title") or "").strip(),
                link=(link.get("url") or "").strip(),
                desc=(link.get("desc") or "").strip(),
            )
        elif msgtype == "text":
            text = ((msg.get("text") or {}).get("content") or "").strip()
            ingest.ingest(text=text)
        else:
            print(f"[info] 客服消息忽略类型: {msgtype}")
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 客服消息处理失败: {exc}", file=sys.stderr)


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
