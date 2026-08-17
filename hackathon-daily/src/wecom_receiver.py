#!/usr/bin/env python3
"""企业微信（WeCom）自建应用消息回调接收器。

把公众号文章转发给企业微信里的自建应用联系人后，微信会推送 link 消息；
这里接收回调 → 调用 ingest 引擎 → 分析并写入 Notion。

运行：
    WECOM_TOKEN=xxx WECOM_AES_KEY=xxx WECOM_CORP_ID=xxx python wecom_receiver.py
    （默认 0.0.0.0:8000，配公网隧道 cloudflared/ngrok 后填进企业微信“接收消息服务器URL”）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from flask import Flask, request
from wechatpy.enterprise import parse_message
from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.exceptions import InvalidSignatureException

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ingest  # noqa: E402

app = Flask(__name__)

TOKEN = os.environ.get("WECOM_TOKEN", "")
AES_KEY = os.environ.get("WECOM_AES_KEY", "")
CORP_ID = os.environ.get("WECOM_CORP_ID", "")


def _crypto() -> WeChatCrypto:
    return WeChatCrypto(TOKEN, AES_KEY, CORP_ID)


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

    if message.type == "link":
        ingest.ingest(
            title=getattr(message, "title", ""),
            link=getattr(message, "url", ""),
            desc=getattr(message, "description", ""),
        )
    elif message.type == "text":
        ingest.ingest(text=getattr(message, "content", ""))
    else:
        print(f"[info] 忽略非文章消息类型: {message.type}")
    return "success"


if __name__ == "__main__":
    if not (TOKEN and AES_KEY and CORP_ID):
        print("[warn] 缺少 WECOM_TOKEN / WECOM_AES_KEY / WECOM_CORP_ID 环境变量", file=sys.stderr)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
