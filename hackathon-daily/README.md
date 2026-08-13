# 黑客松每日情报（GitHub Actions 云端定时任务）

每天北京时间 09:00 自动运行：

1. 通过公开网页搜索与结构化站点抓取，检索最近新发布或临近截止的黑客松/编程马拉松活动；
2. **AI 清洗与审核**（可选，需配置 LLM Key）：逐条判断是否为真实黑客松、是否近期开始、报名是否还来得及，并提取主办方/主题/奖金/报名条件等字段；未配置 Key 时自动降级为规则清洗；
3. **AI 重新检索核对**（可选）：对每条活动做定向搜索→抓取候选官方页→LLM 交叉核对，重点校正**报名时间/报名截止/竞赛时间**等字段，并标记核对状态（已核对/部分核对/信息冲突/未能核对）；
4. 与服务器端历史信息库（`data/archive.json`）查重，重合条目标注"⚠️已收录"，新活动累积入库；
5. 整理成摘要列表（活动名称、主办方、主题、奖金、报名条件、报名/截止/竞赛时间、地点、形式、状态、标签、来源链接、审核与核对状态）；
6. 写入 Notion：直接在 **2026 数据库（日历视图）** 中创建当天的一条记录
   （标题：`YYYY-MM-DD 黑客松活动汇总（N 条）`，日期=当天），
   记录的正文包含全部活动详情（报名时间、报名截止、竞赛时间、地点、摘要、来源链接、审核状态）。

## 信息核对标准（创建每条信息时核对）

1. **真实性**：主办方真实存在，活动在其官网/官方公众号/官方开发者平台可查；来源优先官方 > 权威媒体 > 聚合/个人；
2. **时间准确性**：报名开始、报名截止、竞赛时间须与官方页面一致；多个独立来源一致才判"已核对"，冲突标记"信息冲突"；
3. **链接有效性**：报名/官网链接可访问，域名与主办方匹配；可疑或失效链接标记"待确认"；
4. **字段完整性**：报名时间、报名截止、竞赛时间、地点、主办方五项齐全才算"完整"，缺项在核对说明中列出。

核对由 LLM 执行：先按活动名定向搜索，抓取候选官方页，再让 LLM 提取准确时间等字段并与已知信息交叉比对；每批 5 条、每日上限 `VERIFY_MAX_EVENTS`（默认 15）条，避免超时。

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
- 国内大厂/技术微信公众号定向搜索（腾讯云开发者社区、阿里云开发者、飞桨、字节跳动技术团队、华为开发者联盟、美团技术团队、机器之心、量子位、InfoQ、开源中国、CSDN 等 20 个）
- 小红书公开帖子（通过搜索引擎 `site:xiaohongshu.com` 索引）

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
| `DEEPSEEK_API_KEY`（或 `LLM_API_KEY`） | 可选：AI 清洗/审核用的 LLM API Key（DeepSeek/OpenAI 兼容） |
| `LLM_BASE_URL` / `LLM_MODEL` | 可选：自定义模型地址与模型名（默认 DeepSeek） |

## 服务器端历史信息库

- 文件：`data/archive.json`（随仓库持久化，替代 Notion 数据库）
- 用途：累积存储所有收录过的黑客松活动（按归一化标题去重），作为每日查重基准
- 每次云端运行结束后，工作流会自动把更新后的 `archive.json` 提交回仓库
- 可浏览页面：`docs/archive.html`（GitHub Pages 发布：<https://wenlinl.github.io/hackathon-daily/archive.html>），每条记录带锚点 `#entry-<id>`，支持搜索过滤

## 日报 ↔ 信息库一一对应

- 每条信息库记录有稳定锚点 ID：`sha1(归一化标题)[:12]`
- 日报中每条活动都带 **"🗄️ 信息库 → 打开对应记录"** 链接，点击直接跳到信息库页面中该活动的锚点记录
- 页面由 GitHub Actions 的 `deploy-pages` job 自动发布，每次运行后自动更新

## 本地试跑

```bash
python3 -m pip install -r requirements.txt
export NOTION_TOKEN=secret_xxx
python3 src/collect.py --dry-run
```

`--dry-run` 只搜索并打印结果，不写入 Notion。

## 说明与限制

- 微信公众号和小红书内容较封闭：本脚本通过公开搜索引擎索引抓取公开可见信息，无法读取需要登录的账号内部内容。
- AI 审核聚焦三件事：是否真实黑客松、是否近期开始、报名是否还来得及；拿不准的条目标记"待人工确认"而非直接剔除。
- 所有新搜到的活动会累积写入服务器端 `data/archive.json`（按归一化标题去重），并与当日汇总做查重标注（⚠️已收录）。
- GitHub Actions 的定时任务使用 UTC 时区，工作流已配置为北京时间 09:00（UTC 01:00）触发，可能有少量延迟。
- 首次推送后，可在 Actions 页面手动触发一次（workflow_dispatch）验证。
