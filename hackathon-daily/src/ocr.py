#!/usr/bin/env python3
"""图片文字提取：调用 OpenAI 兼容视觉模型（默认火山方舟 ARK doubao-seed）。

环境变量：
    VISION_API_KEY    必填，火山方舟/其它视觉模型 API Key
    VISION_BASE_URL   默认 https://ark.cn-beijing.volces.com/api/v3
    VISION_MODEL      默认 doubao-seed-2-1-turbo-260628
"""

from __future__ import annotations

import base64
import io
import json
import os
import ssl
import sys
import urllib.request

from PIL import Image

try:
    import certifi

    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover
    _CTX = ssl.create_default_context()

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

# 长截图（如 1206×34000 像素）超出视觉模型尺寸限制会返回 400，
# 这里按高度切段识别再拼接。
CHUNK_HEIGHT = 3000
CHUNK_OVERLAP = 120
MAX_WIDTH = 4000


def _call_vision(image_bytes: bytes, mime: str = "image/png") -> str:
    """调用一次视觉模型；失败返回空串。"""
    api_key = os.environ.get("VISION_API_KEY", "")
    base_url = os.environ.get("VISION_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("VISION_MODEL", "")
    if not api_key or not model:
        print("[warn] 未配置 VISION_API_KEY / VISION_MODEL，无法识别图片", file=sys.stderr)
        return ""

    b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = (
        "这是一张微信公众号文章的长截图。请完整准确地提取全文文字内容，"
        "保留所有与黑客松/编程马拉松活动相关的信息（活动名称、主办方、报名时间、报名截止、"
        "竞赛时间、地点、奖金、报名方式、链接等）。直接输出提取出的文字，不要加任何解释或格式包装。"
    )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
        "max_tokens": 4000,
    }
    # 火山方舟 seed 系列是推理模型，关掉 thinking 提速
    if "volces.com" in base_url:
        payload["thinking"] = {"type": "disabled"}

    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180, context=_CTX) as resp:
            data = json.load(resp)
        text = (data["choices"][0]["message"]["content"] or "").strip()
        return text
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 视觉识别失败: {exc}", file=sys.stderr)
        return ""


def extract_text_from_image(image_bytes: bytes, mime: str = "image/png") -> str:
    """把长图/截图的文字提取为纯文本；超长图自动切段；失败返回空串。"""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 图片解析失败: {exc}", file=sys.stderr)
        return ""

    width, height = img.size
    if height <= CHUNK_HEIGHT and width <= MAX_WIDTH:
        text = _call_vision(image_bytes, mime)
        if text:
            print(f"[info] 图片识别出 {len(text)} 字")
        return text

    # 超长图：按高度切段（带重叠），逐段识别后拼接
    print(f"[info] 长图 {width}x{height}，切段识别…")
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    parts: list[str] = []
    y = 0
    while y < height:
        top = max(0, y - CHUNK_OVERLAP)
        bottom = min(height, y + CHUNK_HEIGHT)
        crop = img.crop((0, top, width, bottom))
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=90)
        text = _call_vision(buf.getvalue(), "image/jpeg")
        if text:
            parts.append(text)
        y += CHUNK_HEIGHT
    joined = "\n".join(parts).strip()
    print(f"[info] 长图识别完成，共 {len(parts)} 段，{len(joined)} 字")
    return joined
