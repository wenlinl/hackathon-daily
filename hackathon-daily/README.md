# 黑客松每日情报（GitHub Actions 云端定时任务）

每天北京时间 09:00 自动运行：

1. 通过公开网页搜索（必应 + 搜狗微信搜索）检索最近新发布或临近截止的黑客松/编程马拉松活动；
2. 整理成摘要列表（活动名称、主办方、时间、报名/截止日期、来源链接）；
3. 写入 Notion：在 **Daily Record** 页面下新建当天的一篇整合笔记（标题：`YYYY-MM-DD 黑客松活动汇总`），
   把所有查到的黑客松信息总结到这一篇笔记里，按来源分组，并附上每条信息的来源链接。

## 需要的密钥

在 GitHub 仓库 Settings → Secrets and variables → Actions 中添加：

| Secret 名称 | 内容 |
| --- | --- |
| `NOTION_TOKEN` | Notion Integration Token（`secret_...`） |

## 本地试跑

```bash
python3 -m pip install -r requirements.txt
export NOTION_TOKEN=secret_xxx
python3 src/collect.py --dry-run
```

`--dry-run` 只搜索并打印结果，不写入 Notion。

## 说明与限制

- 微信公众号和小红书内容较封闭：本脚本通过公开搜索引擎索引抓取公开可见信息，无法读取需要登录的账号内部内容。
- 结果统一写入一篇笔记（不写入 2026 数据库），便于人工查阅。
- GitHub Actions 的定时任务使用 UTC 时区，工作流已配置为北京时间 09:00（UTC 01:00）触发，可能有少量延迟。
- 首次推送后，可在 Actions 页面手动触发一次（workflow_dispatch）验证。
