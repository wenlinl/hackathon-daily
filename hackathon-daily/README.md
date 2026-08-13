# 黑客松每日情报（GitHub Actions 云端定时任务）

每天北京时间 09:00 自动运行：

1. 通过公开网页搜索与结构化站点抓取，检索最近新发布或临近截止的黑客松/编程马拉松活动；
2. 整理成摘要列表（活动名称、主办方、时间、报名/截止日期、来源链接）；
3. 写入 Notion：直接在 **2026 数据库（日历视图）** 中创建当天的一条记录
   （标题：`YYYY-MM-DD 黑客松活动汇总（N 条）`，日期=当天），
   记录的正文包含全部活动详情（报名时间、报名截止、竞赛时间、地点、摘要、来源链接）。

## 信息源

结构化站点（优先，自动解析活动时间/地点）：

- MLH（mlh.io，赛季活动）
- Devfolio（devfolio.co）
- All Hackathons（allhackathons.com）
- ETHGlobal（ethglobal.com，黑客松主活动）
- Hackathon.com（首页推荐活动）
- Hack Club（hackathons.hackclub.com，高中生黑客松）
- Eventbrite（hackathon 搜索页）
- SegmentFault 思否活动
- DataFountain 数据竞赛
- 蓝桥杯竞赛 API
- Dev-Event（GitHub 聚合，韩国开发者活动为主）

关键词搜索（补漏，抓取公开索引）：

- 必应网页搜索
- DuckDuckGo 搜索
- 搜狗微信索引（微信公众号文章）

无法稳定抓取的站点（AWS WAF / 纯前端渲染 / 需登录）已排除：
Devpost、Luma、TAIKAI、HackerEarth、DoraHacks、天池、AI Studio、掘金、
开源中国、活动行、赛氪、牛客网、Unstop。

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
- 所有新搜到的活动会累积写入“黑客松信息库”数据库（按归一化标题去重），并与当日汇总做查重标注（⚠️已收录）。
- GitHub Actions 的定时任务使用 UTC 时区，工作流已配置为北京时间 09:00（UTC 01:00）触发，可能有少量延迟。
- 首次推送后，可在 Actions 页面手动触发一次（workflow_dispatch）验证。
