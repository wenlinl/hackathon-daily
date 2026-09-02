#!/usr/bin/env python3
"""飞书知识库同步（可选最终步骤）。

收录流程（微信转发 / 每日收集）写完 Notion Activities-Public 后，
把同一条活动同步写入飞书知识库「食刻Shike」下的 public-Activity：

- 目标为文档节点（docx）：在节点下为每条活动新建一个子文档，写入详情；
- 目标为多维表格（bitable）：向表格追加一条记录（自动映射字段）；
- 未配置凭据时安全跳过，不影响 Notion 主流程。

环境变量：
    FEISHU_APP_ID / FEISHU_APP_SECRET   必填才启用（飞书自建应用）
    FEISHU_WIKI_SPACE_ID                可选，显式指定知识库 ID
    FEISHU_WIKI_NODE_TOKEN              可选，显式指定目标节点 token
    FEISHU_BITABLE_TABLE_ID             可选，目标为多维表格时指定表 ID
    FEISHU_SPACE_TITLE                  可选，默认「食刻Shike」
    FEISHU_TARGET_TITLE                 可选，默认「public-Activity」

应用权限（scope）：
    wiki:wiki:readonly  查看知识库
    wiki:wiki           编辑知识库（创建节点）
    docx:document       读写文档
    bitable:app         读写多维表格（目标为表格时需要）
"""

from __future__ import annotations

import os
import re
import sys
import time
import datetime as dt
from pathlib import Path
from typing import Any

import requests

FEISHU_BASE = "https://open.feishu.cn/open-apis"
_TIMEOUT = 30
_AUTH_ERR_PREFIX = "99991"  # 飞书鉴权/令牌类错误码前缀，用于自动续期重试


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, "") or "").strip() or default


def _load_dotenv() -> None:
    """自动加载 hackathon-daily/.env 中的 FEISHU_* 变量（已设置的环境变量优先）。"""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key.startswith("FEISHU_") and key not in os.environ:
            os.environ[key] = val


_load_dotenv()


APP_ID = _env("FEISHU_APP_ID")
APP_SECRET = _env("FEISHU_APP_SECRET")
WIKI_SPACE_ID = _env("FEISHU_WIKI_SPACE_ID") or _env("FEISHU_SPACE_ID")
WIKI_NODE_TOKEN = _env("FEISHU_WIKI_NODE_TOKEN")
BITABLE_TABLE_ID = _env("FEISHU_BITABLE_TABLE_ID")
SPACE_TITLE = _env("FEISHU_SPACE_TITLE", "食刻Shike")
TARGET_TITLE = _env("FEISHU_TARGET_TITLE", "public-Activity")

_BITABLE_TYPE_OPTIONS = ("竞赛", "创投路演", "其他")
_BITABLE_STATUS_OPTIONS = ("已丢弃", "未开始", "已报名", "进行中", "完成")

_token_cache: dict[str, Any] = {"token": "", "expire_at": 0.0}


def _norm_title(title: str) -> str:
    """标题归一化（与 collect.normalize_title 规则一致，避免循环导入）。"""
    text = (title or "").lower()
    text = re.sub(
        r"[\s,，。.;；:：!！?？|｜\-—_/\\()（）\[\]【】·×&＆#+*~<>《》「」『』]+",
        "",
        text,
    )
    return re.sub(r"^(2026)?[年]?", "", text)


def get_tenant_access_token() -> str:
    """获取/缓存飞书 tenant_access_token。"""
    now = time.time()
    if _token_cache["token"] and _token_cache["expire_at"] > now + 120:
        return _token_cache["token"]
    if not APP_ID or not APP_SECRET:
        raise RuntimeError("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
    resp = requests.post(
        f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书鉴权失败: code={data.get('code')} msg={data.get('msg')}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expire_at"] = now + int(data.get("expire", 7200))
    return _token_cache["token"]


def _api(method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
    """调用飞书开放接口；令牌失效时自动续期重试一次。"""

    def call(token: str) -> dict:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        resp = requests.request(
            method,
            f"{FEISHU_BASE}{path}",
            headers=headers,
            json=payload,
            params=params,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    data = call(get_tenant_access_token())
    code = str(data.get("code", ""))
    if code.startswith(_AUTH_ERR_PREFIX):
        _token_cache["token"] = ""
        data = call(get_tenant_access_token())
    if data.get("code") != 0:
        raise RuntimeError(f"飞书接口 {path} 失败: code={data.get('code')} msg={data.get('msg')}")
    return data.get("data", {})


def find_space(title: str = SPACE_TITLE) -> str:
    """按名称查找知识库，返回 space_id；也可用 FEISHU_WIKI_SPACE_ID 直接指定。"""
    if WIKI_SPACE_ID:
        return WIKI_SPACE_ID
    page_token = ""
    while True:
        params: dict[str, Any] = {"page_size": 50}
        if page_token:
            params["page_token"] = page_token
        data = _api("GET", "/wiki/v2/spaces", params=params)
        for item in data.get("items", []):
            name = item.get("name") or ""
            if name == title or (title and title in name):
                return item["space_id"]
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
    raise RuntimeError(f"未找到飞书知识库「{title}」，请检查 FEISHU_WIKI_SPACE_ID 或知识库名称")


def _norm_node(item: dict) -> dict:
    """兼容搜索/目录接口的字段差异（doc_token ↔ obj_token）。"""
    return {
        "node_token": item.get("node_token") or item.get("token") or "",
        "obj_token": item.get("obj_token") or item.get("doc_token") or "",
        "obj_type": item.get("obj_type") or "",
        "title": item.get("title") or "",
        "space_id": item.get("space_id") or "",
    }


def find_node(space_id: str, title: str = TARGET_TITLE) -> dict:
    """在知识库中查找目标节点（public-Activity），返回标准化节点信息。"""
    if WIKI_NODE_TOKEN:
        node = {
            "node_token": WIKI_NODE_TOKEN,
            "obj_token": "",
            "obj_type": "",
            "title": TARGET_TITLE,
        }
        try:
            data = _api("GET", f"/wiki/v2/spaces/{space_id}/nodes/{WIKI_NODE_TOKEN}")
            node = _norm_node(data.get("node", {}))
            if not node.get("node_token"):
                node["node_token"] = WIKI_NODE_TOKEN
            if not node.get("title"):
                node["title"] = TARGET_TITLE
        except RuntimeError:
            pass  # 节点详情不可用时按文档处理
        return node

    # 1) 优先用知识库搜索接口
    try:
        data = _api(
            "POST",
            "/wiki/v2/search",
            payload={"query": title, "page_size": 50, "space_id": space_id},
        )
        for item in data.get("items", []):
            node = _norm_node(item)
            name = node["title"]
            if name == title or (title and title.lower() in name.lower()):
                return node
    except (RuntimeError, requests.exceptions.HTTPError):
        pass  # 搜索不可用时退回目录遍历

    # 2) 目录遍历（BFS）
    def list_children(parent: str = "") -> list[dict]:
        params: dict[str, Any] = {"page_size": 50}
        if parent:
            params["parent_node_token"] = parent
        return _api("GET", f"/wiki/v2/spaces/{space_id}/nodes", params=params).get("items", [])

    stack = list_children()
    while stack:
        node = _norm_node(stack.pop())
        name = node["title"]
        if name == title or (title and title.lower() in name.lower()):
            return node
        stack.extend(list_children(node["node_token"]))
    raise RuntimeError(f"未在知识库中找到目标节点「{title}」，请检查 FEISHU_WIKI_NODE_TOKEN 或节点名称")


def _heading(text: str) -> dict:
    return {
        "block_type": 3,
        "heading1": {"elements": [{"text_run": {"content": (text or "")[:190]}}], "style": {}},
    }


def _para(text: str, bold_prefix: str = "") -> dict:
    elements = []
    if bold_prefix:
        elements.append(
            {"text_run": {"content": bold_prefix, "text_element_style": {"bold": True}}}
        )
    elements.append({"text_run": {"content": (text or "")[:1900]}})
    return {"block_type": 2, "text": {"elements": elements, "style": {}}}


def _link_para(label: str, url: str) -> dict:
    elements = [{"text_run": {"content": f"{label}：", "text_element_style": {"bold": True}}}]
    if url:
        elements.append(
            {
                "text_run": {
                    "content": url[:1900],
                    "text_element_style": {"link": {"url": url}},
                }
            }
        )
    return {"block_type": 2, "text": {"elements": elements, "style": {}}}


def _divider() -> dict:
    return {"block_type": 18, "divider": {}}


def build_doc_blocks(ev: Any) -> list[dict]:
    """把活动字段转成飞书文档块（标题 + 字段段落 + 摘要 + 链接）。"""
    blocks = [_heading(getattr(ev, "title", "") or "活动")]
    pairs = (
        ("🏷️ 类型", getattr(ev, "event_type", "")),
        ("🏢 主办方", getattr(ev, "host", "")),
        ("📌 主题", getattr(ev, "themes", "")),
        ("🏆 奖金", getattr(ev, "prize", "")),
        ("👥 报名条件", getattr(ev, "eligibility", "")),
        ("📝 报名时间", getattr(ev, "signup_start", "")),
        ("⏳ 报名截止", getattr(ev, "signup_deadline", "")),
        ("⏰ 竞赛时间", getattr(ev, "competition_time", "")),
        ("📍 地点", getattr(ev, "location", "")),
        ("🌐 形式", getattr(ev, "format", "")),
        ("🚦 状态", getattr(ev, "status", "")),
        ("🌏 分组", getattr(ev, "region", "")),
    )
    for label, val in pairs:
        if val:
            blocks.append(_para(str(val), bold_prefix=f"{label}："))
    blocks.append(_divider())
    snippet = (getattr(ev, "snippet", "") or "").strip() or "待确认"
    blocks.append(_para(snippet[:180], bold_prefix="摘要："))
    url = getattr(ev, "url", "") or ""
    if url:
        blocks.append(_link_para("来源链接", url))
    official = getattr(ev, "official_url", "") or ""
    if official and official != url:
        blocks.append(_link_para("官方链接", official))
    archive = getattr(ev, "archive_url", "") or ""
    if archive:
        blocks.append(_link_para("Notion 记录", archive))
    return blocks


def create_wiki_child(space_id: str, parent_node_token: str, title: str) -> dict:
    """在目标节点下新建一个子文档节点，返回节点信息。"""
    data = _api(
        "POST",
        f"/wiki/v2/spaces/{space_id}/nodes",
        payload={
            "obj_type": "docx",
            "parent_node_token": parent_node_token,
            "title": (title or "")[:100],
        },
    )
    return data.get("node", {})


def append_doc_blocks(document_id: str, blocks: list[dict]) -> None:
    """向文档追加内容块（根块 id 即 document_id，每批不超过 40 块）。"""
    for i in range(0, len(blocks), 40):
        _api(
            "POST",
            f"/docx/v1/documents/{document_id}/blocks/{document_id}/children",
            payload={"children": blocks[i : i + 40]},
        )


def _existing_child_titles(space_id: str, parent_node_token: str) -> set[str]:
    """列出目标节点下已有子文档标题（归一化），用于去重。"""
    titles: set[str] = set()
    page_token = ""
    while True:
        params: dict[str, Any] = {"page_size": 50, "parent_node_token": parent_node_token}
        if page_token:
            params["page_token"] = page_token
        data = _api("GET", f"/wiki/v2/spaces/{space_id}/nodes", params=params)
        for item in data.get("items", []):
            t = item.get("title") or ""
            if t:
                titles.add(_norm_title(t))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
    return titles


def _bitable_field_map(app_token: str, table_id: str) -> dict[str, int]:
    data = _api(
        "GET",
        f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        params={"page_size": 100},
    )
    return {f["field_name"]: f.get("type", 1) for f in data.get("items", [])}


def _pick_field(meta: dict[str, int], keywords: tuple[str, ...]) -> str | None:
    for name in meta:
        if any(k in name for k in keywords):
            return name
    return None


def _bitable_table_id(app_token: str) -> str:
    if BITABLE_TABLE_ID:
        return BITABLE_TABLE_ID
    data = _api("GET", f"/bitable/v1/apps/{app_token}/tables", params={"page_size": 50})
    items = data.get("items", [])
    if not items:
        raise RuntimeError("多维表格中没有数据表")
    return items[0]["table_id"]


def _event_summary(ev: Any) -> str:
    parts = []
    for label, val in (
        ("竞赛时间", getattr(ev, "competition_time", "")),
        ("主办方", getattr(ev, "host", "")),
        ("奖金", getattr(ev, "prize", "")),
        ("地点", getattr(ev, "location", "")),
        ("类型", getattr(ev, "event_type", "")),
        ("分组", getattr(ev, "region", "")),
    ):
        if val:
            parts.append(f"{label}：{val}")
    snippet = getattr(ev, "snippet", "") or ""
    if snippet:
        parts.append(snippet[:500])
    return "；".join(parts)[:5000]


def _norm_date_str(s: str) -> str:
    """从字符串中提取 YYYY-MM-DD；带"约"前缀的估算日期也支持。"""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s or "")
    return m.group(0) if m else ""


def _date_to_ms(s: str) -> int | None:
    """YYYY-MM-DD → 飞书日期字段 ms 时间戳（Asia/Shanghai 零点）。"""
    s = _norm_date_str(s)
    if not s:
        return None
    try:
        y, m, d = (int(x) for x in s.split("-"))
    except ValueError:
        return None
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    beijing = dt.timezone(dt.timedelta(hours=8))
    return int(dt.datetime(y, m, d, tzinfo=beijing).timestamp() * 1000)


def _map_status(status: str) -> str:
    """把活动状态映射到飞书表格"状态"选项（已丢弃/未开始/已报名/进行中/完成）。"""
    status = (status or "").strip()
    if not status:
        return ""
    if status in _BITABLE_STATUS_OPTIONS:
        return status
    if "丢弃" in status:
        return "已丢弃"
    if "结束" in status or "完成" in status:
        return "完成"
    if "进行" in status:
        return "进行中"
    if "报名" in status or "招募" in status or "申请" in status:
        return "未开始"
    if "发布" in status:
        return "未开始"
    return ""


def _bitable_fields(meta: dict[str, int], ev: Any, today: str = "") -> dict[str, Any]:
    title_field = _pick_field(meta, ("名称", "标题", "活动", "项目", "title")) or next(
        iter(meta), "名称"
    )
    summary_field = _pick_field(meta, ("摘要", "说明", "备注", "详情", "summary"))
    link_field = _pick_field(meta, ("链接", "网址", "url"))
    type_field = _pick_field(meta, ("类型",))
    status_field = _pick_field(meta, ("状态",))
    date_field = _pick_field(meta, ("日期",))
    deadline_field = _pick_field(meta, ("报名截止", "截止"))
    submit_field = _pick_field(meta, ("提交日期", "提交"))
    location_field = _pick_field(meta, ("地点",))
    remark_field = _pick_field(meta, ("备注", "说明"))

    fields: dict[str, Any] = {title_field: (getattr(ev, "title", "") or "")[:500]}
    if summary_field:
        fields[summary_field] = _event_summary(ev)
    url = getattr(ev, "url", "") or ""
    if link_field and url:
        if meta.get(link_field) == 15:  # 超链接字段
            fields[link_field] = {"text": "查看详情", "link": url}
        else:
            fields[link_field] = url
    event_type = (getattr(ev, "event_type", "") or "").strip()
    if type_field:
        if event_type == "黑客松":
            event_type = "竞赛"
        if event_type in _BITABLE_TYPE_OPTIONS:
            fields[type_field] = event_type
    if status_field:
        st = _map_status(getattr(ev, "status", "") or "")
        if st:
            fields[status_field] = st
    if date_field:
        ms = _date_to_ms(getattr(ev, "calendar_date", "") or today)
        if ms:
            fields[date_field] = ms
    if deadline_field:
        ms = _date_to_ms(getattr(ev, "signup_deadline", "") or "")
        if ms:
            fields[deadline_field] = ms
    if submit_field and today:
        ms = _date_to_ms(today)
        if ms:
            fields[submit_field] = ms
    location = getattr(ev, "location", "") or ""
    if location_field and location:
        fields[location_field] = location[:500]
    if remark_field:
        notes = []
        review = getattr(ev, "review_status", "") or ""
        if review:
            notes.append(f"审核：{review}")
        source = getattr(ev, "source", "") or ""
        if source:
            notes.append(f"来源：{source}")
        official = getattr(ev, "official_url", "") or ""
        if official:
            notes.append(f"官方：{official}")
        if notes:
            fields[remark_field] = "；".join(notes)[:500]
    return fields


def _bitable_existing_titles(
    app_token: str, table_id: str, title_field: str
) -> set[str]:
    titles: set[str] = set()
    page_token = ""
    while True:
        params: dict[str, Any] = {"page_size": 200}
        if page_token:
            params["page_token"] = page_token
        data = _api(
            "GET",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            params=params,
        )
        for rec in data.get("items", []):
            val = (rec.get("fields") or {}).get(title_field)
            if isinstance(val, str):
                titles.add(_norm_title(val))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
    return titles


def _bitable_add_record(app_token: str, table_id: str, fields: dict[str, Any]) -> dict:
    """新建飞书表格记录，返回创建的 record（含 record_id）。"""
    data = _api(
        "POST",
        f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
        payload={"fields": fields},
    )
    return data.get("record") or {}


_ATTACHMENT_KEYWORDS = ("附件", "素材", "文件", "图片", "材料", "attachment", "file", "image", "img")


def _pick_attachment_field(meta: dict[str, int]) -> str | None:
    """从表格字段中找附件类型（type=17）字段，优先名字含"附件/素材/文件/图片"的。"""
    for name, ftype in meta.items():
        if ftype == 17 and any(k in name.lower() for k in _ATTACHMENT_KEYWORDS):
            return name
    for name, ftype in meta.items():
        if ftype == 17:
            return name
    return None


def _download_bytes(url: str, timeout: int = 180) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _filename_from_url(url: str) -> str:
    name = (url or "").rstrip("/").split("/")[-1]
    name = re.sub(r"[?&#].*$", "", name).strip()
    return name or "source.jpg"


def _upload_bitable_file(app_token: str, file_bytes: bytes, filename: str) -> str:
    """上传文件到飞书 bitable 空间（parent=app_token），返回 file_token。"""

    def call(token: str) -> dict:
        resp = requests.post(
            f"{FEISHU_BASE}/drive/v1/medias/upload_all",
            headers={"Authorization": f"Bearer {token}"},
            data={
                "file_name": filename,
                "parent_type": "bitable_file",
                "parent_node": app_token,
                "size": str(len(file_bytes)),
            },
            files={"file": (filename, file_bytes)},
            timeout=180,
        )
        resp.raise_for_status()
        return resp.json()

    data = call(get_tenant_access_token())
    code = str(data.get("code", ""))
    if code.startswith(_AUTH_ERR_PREFIX):
        _token_cache["token"] = ""
        data = call(get_tenant_access_token())
    if data.get("code") != 0:
        raise RuntimeError(f"飞书上传素材失败: code={data.get('code')} msg={data.get('msg')}")
    token = (data.get("data") or {}).get("file_token") or ""
    if not token:
        raise RuntimeError("飞书上传素材未返回 file_token")
    return token


def _bitable_find_record(
    app_token: str, table_id: str, title_field: str, norm_title: str
) -> dict | None:
    """按归一化标题查找飞书表格记录。"""
    page_token = ""
    while True:
        params: dict[str, Any] = {"page_size": 200}
        if page_token:
            params["page_token"] = page_token
        data = _api(
            "GET",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            params=params,
        )
        for rec in data.get("items", []):
            val = (rec.get("fields") or {}).get(title_field)
            if isinstance(val, str) and _norm_title(val) == norm_title:
                return rec
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
    return None


def _merge_attachments(fields_val: Any) -> list[dict]:
    out: list[dict] = []
    if isinstance(fields_val, list):
        for item in fields_val:
            if isinstance(item, dict) and item.get("file_token"):
                out.append({"file_token": item["file_token"]})
    return out


def attach_material_to_record(
    app_token: str,
    table_id: str,
    title_field: str,
    norm_title: str,
    source_url: str,
) -> bool:
    """把原始素材文件上传并挂到飞书记录的附件字段（已存在则跳过）。"""
    record = _bitable_find_record(app_token, table_id, title_field, norm_title)
    if not record:
        raise RuntimeError("飞书表格中未找到对应记录")
    meta = _bitable_field_map(app_token, table_id)
    att_field = _pick_attachment_field(meta)
    if not att_field:
        raise RuntimeError("飞书表格没有附件类型字段")
    existing = _merge_attachments((record.get("fields") or {}).get(att_field))
    filename = _filename_from_url(source_url)
    data = _download_bytes(source_url)
    token = _upload_bitable_file(app_token, data, filename)
    if token in {item.get("file_token") for item in existing}:
        print(f"[info] 附件已存在，跳过：{filename[:60]}")
        return False
    existing.append({"file_token": token, "name": filename})
    _api(
        "PUT",
        f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record['record_id']}",
        payload={"fields": {att_field: existing}},
    )
    print(f"[ok] 原始素材已挂到飞书附件：{filename[:80]}")
    return True


def sync_event(ev: Any, source: str = "", today: str = "") -> str:
    """同步一条活动到飞书知识库，返回页面 URL；未配置凭据时返回空串。"""
    if not APP_ID or not APP_SECRET:
        print(
            "[warn] 飞书同步跳过：未配置 FEISHU_APP_ID/FEISHU_APP_SECRET，"
            "活动仅写入 Notion。请检查 Render 环境变量或 hackathon-daily/.env 的 FEISHU_* 配置。",
            file=sys.stderr,
        )
        return ""
    norm = _norm_title(getattr(ev, "title", "") or "")
    if not norm:
        return ""

    space_id = find_space()
    target = find_node(space_id)
    node_token = target.get("node_token") or ""
    obj_type = (target.get("obj_type") or "").lower()
    obj_token = target.get("obj_token") or ""
    title = getattr(ev, "title", "") or ""

    if obj_type == "bitable":
        table_id = _bitable_table_id(obj_token)
        title_field = _pick_field(
            _bitable_field_map(obj_token, table_id), ("名称", "标题", "活动", "项目", "title")
        ) or "名称"
        if norm in _bitable_existing_titles(obj_token, table_id, title_field):
            print(f"[info] 飞书多维表格已存在：{title[:40]}（跳过）")
            return f"https://feishu.cn/base/{obj_token}"
        meta = _bitable_field_map(obj_token, table_id)
        fields = _bitable_fields(meta, ev, today=today)
        att_field = _pick_attachment_field(meta)
        src_url = getattr(ev, "source_image_url", "") or ""
        uploaded: dict | None = None
        if att_field and src_url:
            try:
                filename = _filename_from_url(src_url)
                file_bytes = _download_bytes(src_url)
                token = _upload_bitable_file(obj_token, file_bytes, filename)
                uploaded = {"file_token": token, "name": filename}
                print(f"[info] 原始素材已上传飞书附件：{filename[:80]}")
            except Exception as exc:  # noqa: BLE001 素材上传失败不影响记录入库
                print(f"[warn] 原始素材上传失败（不影响入库）: {exc}", file=sys.stderr)
        record = _bitable_add_record(obj_token, table_id, fields)
        if uploaded and record.get("record_id"):
            _api(
                "PUT",
                f"/bitable/v1/apps/{obj_token}/tables/{table_id}/records/{record['record_id']}",
                payload={"fields": {att_field: [uploaded]}},
            )
            print(f"[ok] 附件已挂到新记录 {record['record_id']}")
        print(f"[info] 已写入飞书多维表格：{title[:40]}（{source or '手动收录'}）")
        return f"https://feishu.cn/base/{obj_token}"

    # 文档/未知类型：在目标节点下新建子文档写入详情
    if not node_token:
        raise RuntimeError("未解析到飞书目标节点 token")
    try:
        existing = _existing_child_titles(space_id, node_token)
    except RuntimeError:
        existing = set()
    if norm in existing:
        print(f"[info] 飞书知识库已存在：{title[:40]}（跳过）")
        return f"https://feishu.cn/wiki/{node_token}"
    node = create_wiki_child(space_id, node_token, title)
    doc_id = node.get("obj_token") or node.get("doc_token") or ""
    if not doc_id:
        raise RuntimeError("飞书子文档创建成功但未返回文档 ID")
    append_doc_blocks(doc_id, build_doc_blocks(ev))
    new_node = node.get("node_token") or ""
    print(f"[info] 已写入飞书知识库：{title[:40]}（{source or '手动收录'}）")
    return f"https://feishu.cn/wiki/{new_node}" if new_node else f"https://feishu.cn/wiki/{node_token}"


def sync_events(events: list[Any], source: str = "") -> dict[str, str]:
    """批量同步；单条失败不影响其余，返回 {归一化标题: 飞书 URL}。"""
    if not APP_ID or not APP_SECRET:
        print(
            "[warn] 飞书同步跳过：未配置 FEISHU_APP_ID/FEISHU_APP_SECRET，"
            "活动仅写入 Notion。请检查 Render 环境变量或 hackathon-daily/.env 的 FEISHU_* 配置。",
            file=sys.stderr,
        )
        return {}
    beijing = dt.timezone(dt.timedelta(hours=8))
    today = dt.datetime.now(beijing).date().isoformat()
    urls: dict[str, str] = {}
    for ev in events:
        try:
            url = sync_event(ev, source=source, today=today)
            if url:
                urls[_norm_title(getattr(ev, "title", "") or "")] = url
        except Exception as exc:  # noqa: BLE001 单条失败不影响整体
            print(
                f"[warn] 飞书同步失败 {getattr(ev, 'title', '?')[:40]}: {exc}",
                file=sys.stderr,
            )
    if urls:
        print(f"[info] 飞书知识库同步完成：{len(urls)} 条 → {SPACE_TITLE} / {TARGET_TITLE}")
    return urls


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="飞书知识库同步自检")
    parser.add_argument("--check", action="store_true", help="校验凭据并查找知识库/目标节点")
    args = parser.parse_args()
    if args.check:
        space_id = find_space()
        node = find_node(space_id)
        print(f"知识库: {SPACE_TITLE} space_id={space_id}")
        print(
            f"目标节点: {TARGET_TITLE} node_token={node.get('node_token')} "
            f"obj_type={node.get('obj_type')} obj_token={node.get('obj_token')}"
        )
