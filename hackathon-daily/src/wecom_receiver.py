#!/usr/bin/env python3
"""企业微信（WeCom）消息回调接收器。

把公众号文章转发给企业微信里的自建应用联系人后，微信会推送 link 消息；
这里接收回调 → 调用 ingest 引擎 → 分析并写入 Notion。

企微回调要求 5 秒内响应，因此收到消息后立即回 "success"，AI 分析与写
Notion 放到后台线程执行；同一消息重复推送时按 URL/内容去重。

支持两条链路：
- /wecom     自建应用回调（企业微信内给应用发消息）
- /wecom-kf  微信客服回调（个人微信直接转发文章给客服号；正文经 sync_msg 拉取）

本地运行：
    WECOM_CORP_ID=xxx WECOM_TOKEN=xxx WECOM_AES_KEY=xxx \
    NOTION_TOKEN=xxx DEEPSEEK_API_KEY=xxx python src/wecom_receiver.py
    （默认 0.0.0.0:8000，配公网隧道 cloudflared/ngrok 后填进企微后台）

生产运行（云端）：
    gunicorn -w 1 -b 0.0.0.0:8000 --timeout 60 wecom_receiver:app
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from flask import Flask, request
from wechatpy.enterprise import parse_message
from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.exceptions import InvalidSignatureException

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ingest  # noqa: E402
import kf  # noqa: E402

app = Flask(__name__)

TOKEN = os.environ.get("WECOM_TOKEN", "")
AES_KEY = os.environ.get("WECOM_AES_KEY", "")
CORP_ID = os.environ.get("WECOM_CORP_ID", "")
KF_TOKEN = os.environ.get("WECOM_KF_TOKEN", "")
KF_AES_KEY = os.environ.get("WECOM_KF_AES_KEY", "")

# 回调必须 5 秒内响应，AI 分析+写 Notion 较慢 → 后台线程处理；
# 企微失败会重试推送，同一消息去重避免重复入库。
_seen: set[str] = set()
_seen_lock = threading.Lock()
_SEEN_MAX = 500


def _kf_catchup_loop() -> None:
    """后台兜底：每 10 分钟补拉一次客服消息（防止回调丢失导致漏收）。

    配合 keepalive 定时请求（GitHub Actions 每 5 分钟 ping /health）保持免费实例常醒。
    """
    while True:
        try:
            open_ids = [
                x.strip()
                for x in os.environ.get("WECOM_KF_OPEN_IDS", "").split(",")
                if x.strip()
            ]
            for open_kfid in open_ids:
                try:
                    kf.sync(open_kfid, "")
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] 兜底同步失败 {open_kfid}: {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 兜底同步循环异常: {exc}", file=sys.stderr)
        time.sleep(600)


def _start_kf_catchup() -> None:
    if os.environ.get("WECOM_KF_OPEN_IDS", "").strip():
        threading.Thread(target=_kf_catchup_loop, daemon=True).start()
        print("[info] 客服兜底同步线程已启动（每 10 分钟）")


_start_kf_catchup()


def _crypto() -> WeChatCrypto:
    return WeChatCrypto(TOKEN, AES_KEY, CORP_ID)


def _kf_crypto() -> WeChatCrypto:
    return WeChatCrypto(KF_TOKEN, KF_AES_KEY, CORP_ID)


def _dedup(key: str) -> bool:
    """返回 True 表示已处理过，应跳过本次推送。"""
    global _seen
    with _seen_lock:
        if key in _seen:
            return True
        _seen.add(key)
        if len(_seen) > _SEEN_MAX:
            _seen = set(list(_seen)[-_SEEN_MAX // 2 :])
        return False


def _handle(message: Any) -> None:
    try:
        if message.type == "link":
            result = ingest.ingest(
                title=getattr(message, "title", ""),
                link=getattr(message, "url", ""),
                desc=getattr(message, "description", ""),
            )
        elif message.type == "text":
            result = ingest.ingest(text=getattr(message, "content", ""))
        else:
            print(f"[info] 忽略非文章消息类型: {message.type}")
            return
        if result:
            print(f"[info] 处理结果：{result['message']}")
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 后台处理失败: {exc}", file=sys.stderr)


def _handle_kf(open_kfid: str, cb_token: str) -> None:
    try:
        kf.sync(open_kfid, cb_token)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 客服消息拉取失败: {exc}", file=sys.stderr)


@app.route("/health")
def health() -> str:
    return "ok"


@app.route("/wecom-kf", methods=["GET", "POST"])
def wecom_kf() -> tuple[str, int] | str:
    signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")

    if request.method == "GET":
        # 微信客服后台配置回调时的 URL 验证
        echostr = request.args.get("echostr", "")
        try:
            return _kf_crypto().check_signature(signature, timestamp, nonce, echostr)
        except InvalidSignatureException:
            return "invalid signature", 403

    try:
        xml = _kf_crypto().decrypt_message(request.data, signature, timestamp, nonce)
        root = ET.fromstring(xml)
        event = root.findtext("Event") or ""
        cb_token = root.findtext("Token") or ""
        open_kfid = root.findtext("OpenKfId") or ""
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 客服回调解析失败: {exc}", file=sys.stderr)
        return "error", 400

    if event == "kf_msg_or_event" and open_kfid:
        threading.Thread(target=_handle_kf, args=(open_kfid, cb_token), daemon=True).start()
    else:
        print(f"[info] 忽略客服事件: event={event} open_kfid={open_kfid}")
    return "success"


@app.route("/wecom", methods=["GET", "POST"])
def wecom() -> tuple[str, int] | str:
    signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")

    if request.method == "GET":
        # 企业微信后台配置时的 URL 验证
        echostr = request.args.get("echostr", "")
        try:
            return _crypto().check_signature(signature, timestamp, nonce, echostr)
        except InvalidSignatureException:
            return "invalid signature", 403

    try:
        xml = _crypto().decrypt_message(request.data, signature, timestamp, nonce)
        message = parse_message(xml)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 回调解析失败: {exc}", file=sys.stderr)
        return "error", 400

    key = getattr(message, "url", "") or hashlib.sha256(
        getattr(message, "content", "").encode("utf-8", "ignore")
    ).hexdigest()
    if _dedup(key):
        print("[info] 重复推送，跳过")
        return "success"
    threading.Thread(target=_handle, args=(message,), daemon=True).start()
    return "success"


if __name__ == "__main__":
    if not (TOKEN and AES_KEY and CORP_ID):
        print("[warn] 缺少 WECOM_TOKEN / WECOM_AES_KEY / WECOM_CORP_ID 环境变量", file=sys.stderr)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
