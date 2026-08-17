# 黑客松每日情报（GitHub Actions 云端定时任务）

每天北京时间 09:00 自动运行：

1. 通过公开网页搜索与结构化站点抓取，检索最近新发布或临近截止的黑客松/编程马拉松活动；
2. **AI 清洗与审核**（可选，需配置 LLM Key）：逐条判断是否为真实黑客松、是否近期开始、报名是否还来得及，并提取主办方/主题/奖金/报名条件等字段；未配置 Key 时自动降级为规则清洗；
3. **AI 重新检索核对**（可选）：对每条活动做定向搜索→抓取候选官方页→**第二个 AI** 交叉核对（可用 `VERIFY_LLM_*` 配置独立模型），重点校正**报名时间/报名截止/竞赛时间**等字段，并标记核对状态（已核对/部分核对/信息冲突/未能核对）；
4. **剔除纯海外活动**：只保留"国内"与"线上"（线上含海外举办的在线黑客松，均可参与），国外线下黑客松不收录；
5. 与 **Notion hackathons 数据库**（含服务器端 `data/archive.json` 镜像）查重，重合条目标注"⚠️已收录"；新活动写入/更新到该数据库（每条活动一条记录），`archive.json` 仅作镜像备份；
6. 整理成摘要列表（活动名称、主办方、主题、奖金、报名条件、报名/截止/竞赛时间、地点、形式、状态、标签、来源链接、审核与核对状态），并**按 国内 / 线上 分组**（纯海外已剔除）；
7. 写入 Notion：直接在 **2026 数据库（日历视图）** 中创建当天的一条记录
   （标题：`YYYY-MM-DD 黑客松活动汇总（N 条）`，日期=当天），
   记录的正文包含全部活动详情（报名时间、报名截止、竞赛时间、地点、摘要、来源链接、审核状态）。
8. **发送日报邮件**：把当天摘要通过 SMTP 发到指定邮箱（默认 `aresleng@sina.com`；发件邮箱为 `lengwenlin@163.com`；未配置 SMTP 时跳过）。

## 信息核对标准（创建每条信息时核对）

1. **真实性**：主办方真实存在，活动在其官网/官方公众号/官方开发者平台可查；来源优先官方 > 权威媒体 > 聚合/个人；
2. **时间准确性**：报名开始、报名截止、竞赛时间须与官方页面一致；多个独立来源一致才判"已核对"，冲突标记"信息冲突"；
3. **链接有效性**：报名/官网链接可访问，域名与主办方匹配；可疑或失效链接标记"待确认"；
4. **字段完整性**：报名时间、报名截止、竞赛时间、地点、主办方五项齐全才算"完整"，缺项在核对说明中列出。

核对由 LLM 执行：先按活动名定向搜索，抓取候选官方页，再让 LLM 提取准确时间等字段并与已知信息交叉比对；**默认对当天全部活动逐一核对**，如遇超时可在仓库 Variables 设置 `VERIFY_MAX_EVENTS` 限制条数。

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
| `VERIFY_LLM_API_KEY` / `VERIFY_LLM_BASE_URL` / `VERIFY_LLM_MODEL` | 可选：核对阶段用的"第二个 AI"（不配置则沿用主 AI） |
| `SMTP_HOST` | 可选：SMTP 服务器（当前为 `smtp.office365.com`，Outlook），配置后每天发送日报邮件 |
| `SMTP_PORT` | 可选：当前 `587`（STARTTLS） |
| `SMTP_USER` / `SMTP_PASSWORD` | 可选：发件邮箱账号（`lengwenlin@outlook.com`）与密码/应用专用密码 |
| `EMAIL_TO`（Variables） | 可选：收件人，默认 `aresleng@sina.com` |

邮件正文取自 `data/daily_summary.md`（运行中生成，不入库），内容与日报一致：按国内/国外/线上分组，含时间、地点、链接与信息库跳转。

## 黑客松存档数据库（Notion）

- 数据库：Notion **hackathons** 数据库（`3bb60e0b-0bbf-8179-aa88-d98a77a635ef`），每条活动一条记录
- 属性：名称(title)、日期(date)、报名截止(date)、地点(rich_text)、来源链接(url)、摘要(rich_text)、状态(status)
- 每次运行：已存在的活动**更新**记录（刷新时间/地点/链接/摘要/状态），新活动**新增**记录
- **库与日报同标准清理**：非黑客松（课程广告/招聘/回顾等）、过期超窗、纯海外的记录自动归档（软删除），镜像 `archive.json` 同步裁剪
- 每条记录的 **note 正文**包含该活动的详细内容（主办方、主题、奖金、报名条件、报名/截止/竞赛时间、地点、审核/核对状态、官方与来源链接）
- AI 核对优先处理**缺少报名截止时间**的活动，尽量从官方来源补全截止日期；仅有估算值时写入摘要标注"报名截止（约）"
- 日报中每条活动带 **"🗄️ 信息库 → 打开对应记录"** 链接，直接跳到该活动在 Notion 数据库中的记录，实现一一对应
- `data/archive.json` 保留为服务器端镜像（查重/备份），不再生成网站页面

## 手动补充微信文章全文

微信正文无法稳定自动抓取时，可以把文章**标题 + 正文全文 + 链接**直接发给助手，助手会写入 `hackathon-daily/manual/articles.json`；下次运行自动并入当天日报和信息库，AI 会从中提取截止时间/地点/主办方等字段。也可以自己按此格式追加：

```json
{
  "title": "活动名称",
  "url": "https://mp.weixin.qq.com/s/...",
  "account": "公众号名称",
  "text": "文章正文全文……",
  "processed": false
}
```

已并入当天流程的条目会被标记 `processed: true`，不会重复入库。

## 微信转发 → 自动收录（企业微信 WeCom，推荐）

个人微信没有官方 API，但**企业微信官方回调**可以实现"转发即收录"：

1. 注册企业微信（免费），创建**自建应用**，拿到 `corpId` / `Secret` / `AgentId`；
2. 把接收端部署到公网（见下），拿到形如 `https://xxx/wecom` 的真实地址；
3. 在自建应用里配置**接收消息服务器**：URL 填该地址，并生成 `Token` 和 `EncodingAESKey`（保存时企微会发验证请求，必须能正常回显才通过）；
4. 把自建应用通过**微信插件**加到你的微信联系人，置顶；
5. 以后在公众号看到有用黑客松，直接**转发给该联系人**；
6. 接收端收到 link 消息 → `src/ingest.py` 用 AI 提取字段 → 写入 Notion 黑客松数据库 + 当天日报。

> ⚠️ 回调 URL 必须指向**你自己的、正在运行接收端的公网服务**，不能填一个没有服务的域名。
> 企微保存时会对 URL 发 GET 验证（带 `echostr`），接收端解密回显后才算通过；
> 提示"openapi回调地址请求不通过"通常就是"地址指向的服务不在线/不对"。

### 云端部署（推荐，电脑关机也能跑）

**方式一：Render（免费，最快）**

1. 把仓库推到 GitHub（本仓库已带 `Dockerfile` 和 `render.yaml`）；
2. 注册 [render.com](https://render.com)，New + → Blueprint，选本仓库；
3. 服务起来后，把面板里 `NOTION_TOKEN`、`DEEPSEEK_API_KEY` 两个环境变量填上（与 GitHub Actions 里同一批值）；
4. 得到形如 `https://hackathon-daily-wecom.onrender.com/wecom` 的地址，填进企微后台。

免费档闲置会休眠，首次回调有冷启动延迟；企微会重试，个人使用一般能成功。

**方式二：自己的服务器/VPS**

```bash
docker build -t hackathon-daily-wecom hackathon-daily/
docker run -d --name wecom -p 8000:8000 \
  -e WECOM_CORP_ID=... -e WECOM_TOKEN=... -e WECOM_AES_KEY=... \
  -e NOTION_TOKEN=... -e DEEPSEEK_API_KEY=... \
  hackathon-daily-wecom
```

再把你的域名（如 `dianji.com`）解析到该服务器，URL 填 `https://dianji.com/wecom`。

### 本地临时验证（电脑开着时）

```bash
python -m pip install -r requirements.txt
WECOM_CORP_ID=... WECOM_TOKEN=... WECOM_AES_KEY=... \
NOTION_TOKEN=... DEEPSEEK_API_KEY=... python src/wecom_receiver.py
# 另开终端，启动临时隧道拿到公网地址：
cloudflared tunnel --url http://localhost:8000
```

把 cloudflared 输出的 `https://xxx.trycloudflare.com/wecom` 填进企微后台保存；
验证通过后可先测试转发，确认整条链路正常，再部署到云端。

> ⚠️ 注册时**必须选"企业"形态**，不要选"个人组建团队"。团队形态没有管理后台，
> 无法创建自建应用、也无法开微信客服（登录 work.weixin.qq.com 会提示"团队用户"）。
> 个人免费注册未认证企业即可：手机企业微信 → 创建/加入企业 → 类型选"企业"，名称随意，
> 通讯录 100 人上限足够自用；创建后再登录管理后台 → 应用管理 → 创建自建应用。
> 注意：自建应用不支持从个人微信直接转发公众号文章，需要在企业微信内把链接粘贴/转发给应用；
> 想要"个人微信直接转发"体验，用**微信客服**方案（需把接收端改成客服回调格式）。

### 微信客服方案（个人微信一键转发，推荐）

不用复制链接，在**个人微信**里直接把公众号文章转发给客服号即可：

1. 企业微信管理后台 → 应用管理 → 微信客服 → 注册开通（未认证企业也可用，累计上限 100 个客户，自己用足够）；
2. 到 [kf.weixin.qq.com](https://kf.weixin.qq.com)（微信客服管理后台）→ 客服账号 → 添加客服账号，记下 `open_kfid`；
3. 开发配置 → 接收消息/事件回调：URL 填 `https://你的地址/wecom-kf`，生成 `Token` 和 `EncodingAESKey`；
4. 开发配置 → 获取 `corpSecret`（微信客服的 Secret，不是自建应用的 Secret）；
5. 客服账号 → 获取客服链接/二维码，在个人微信打开 → 开始会话；
6. 以后看到公众号文章 → 个人微信直接**转发给该客服号** → 接收端收到事件后经 `sync_msg` 拉取正文 → AI 提取入库。

接收端环境变量（对应 `/wecom-kf` 路由）：

```bash
WECOM_CORP_ID=... WECOM_KF_TOKEN=... WECOM_KF_AES_KEY=... WECOM_KF_SECRET=... \
NOTION_TOKEN=... DEEPSEEK_API_KEY=... python src/wecom_receiver.py
```

也可以单独调用引擎：

```bash
python src/ingest.py --title "标题" --link "https://mp.weixin.qq.com/s/..." --desc "摘要"
python src/ingest.py --text "文章全文……"
```

> 个人微信的第三方自动化（itchat/Wechaty 非官方通道）违反微信条款且有封号风险，本项目不做。

## 本地试跑

```bash
python3 -m pip install -r requirements.txt
export NOTION_TOKEN=secret_xxx
python3 src/collect.py --dry-run
```

`--dry-run` 只搜索并打印结果，不写入 Notion。

## 说明与限制

- 微信公众号内容封闭：搜狗索引只返回标题+摘要；文章正文在 mp.weixin.qq.com（本身无需登录），但搜狗跳转链接有反爬（验证码/JS 挑战），云端 IP 基本无法通过。脚本会尽力抓正文（`WECHAT_BODY_LIMIT`，默认 5 篇/次），抓到就用正文提取字段，抓不到退回摘要。小红书同理只能拿到公开索引部分。
- AI 审核聚焦三件事：是否真实黑客松、是否近期开始、报名是否还来得及；拿不准的条目标记"待人工确认"而非直接剔除。
- 所有新搜到的活动会累积写入服务器端 `data/archive.json`（按归一化标题去重），并与当日汇总做查重标注（⚠️已收录）。
- GitHub Actions 的定时任务使用 UTC 时区，工作流已配置为北京时间 09:00（UTC 01:00）触发，可能有少量延迟。
- 首次推送后，可在 Actions 页面手动触发一次（workflow_dispatch）验证。
