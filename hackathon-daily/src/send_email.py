#!/usr/bin/env python3
"""每天把黑客松日报以邮件发送给指定收件人（SMTP，兼容 SSL 465 / STARTTLS 587）。

环境变量：
    SMTP_HOST       SMTP 服务器（必填才会发送，如 smtp.qq.com / smtp.163.com）
    SMTP_PORT       默认 465（SSL）；填 587 则用 STARTTLS
    SMTP_USER       发件邮箱账号
    SMTP_PASSWORD   授权码/密码（第三方客户端授权码）
    EMAIL_TO        收件人，默认 aresleng@sina.com
    EMAIL_FROM      发件人显示，默认同 SMTP_USER

未配置 SMTP_HOST 时直接跳过（不影响主流程）。
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape as html_escape
from pathlib import Path

try:
    import certifi
except ImportError:  # pragma: no cover
    certifi = None  # type: ignore

SUMMARY_FILE = Path(__file__).resolve().parent.parent / "data" / "daily_summary.md"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, "") or "").strip() or default


def _markdown_to_html(text: str) -> str:
    """把摘要 Markdown 转成简单 HTML（仅支持标题/列表/普通段落）。"""
    out: list[str] = ["<div style='font-family:-apple-system,PingFang SC,Microsoft YaHei,sans-serif;"
                      "line-height:1.6;font-size:14px;color:#1f2328'>"]
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            out.append(f"<h3>{html_escape(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            out.append(f"<h2 style='border-bottom:1px solid #eee;padding-bottom:4px'>"
                       f"{html_escape(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            out.append(f"<h1>{html_escape(stripped[2:])}</h1>")
        elif stripped.startswith("- "):
            out.append(f"<li>{html_escape(stripped[2:])}</li>")
        else:
            out.append(f"<p>{html_escape(stripped)}</p>")
    out.append("</div>")
    return "\n".join(out)


def send_markdown_email(subject: str, body: str) -> int:
    """用已配置的 SMTP 发送一封 Markdown 正文邮件。返回 0 成功 / 1 失败。"""
    host = _env("SMTP_HOST")
    if not host:
        print("[info] 未配置 SMTP_HOST，跳过邮件发送（配置后每天自动发送）")
        return 0
    port = int(_env("SMTP_PORT", "465") or 465)
    user = _env("SMTP_USER")
    password = _env("SMTP_PASSWORD")
    email_to = _env("EMAIL_TO", "aresleng@sina.com")
    email_from = _env("EMAIL_FROM", user)
    if not user or not password:
        print("[warn] 缺少 SMTP_USER / SMTP_PASSWORD，跳过邮件发送")
        return 0
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(_markdown_to_html(body), "html", "utf-8"))

    try:
        if certifi:
            context = ssl.create_default_context(cafile=certifi.where())
        else:
            context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as server:
                server.login(user, password)
                server.sendmail(email_from, [email_to], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls(context=context)
                server.login(user, password)
                server.sendmail(email_from, [email_to], msg.as_string())
        print(f"[ok] 日报邮件已发送到 {email_to}（主题：{subject}）")
        return 0
    except Exception as exc:  # noqa: BLE001 - 发送失败明确报错
        print(f"[error] 邮件发送失败: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    if not SUMMARY_FILE.exists():
        print("[warn] 未找到日报摘要文件，跳过邮件发送")
        return 0
    body = SUMMARY_FILE.read_text(encoding="utf-8")
    subject = body.splitlines()[0].lstrip("# ").strip() if body else "黑客松活动汇总"
    return send_markdown_email(subject, body)


if __name__ == "__main__":
    raise SystemExit(main())
