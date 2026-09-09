# A股每日复盘 + 自选股监控（GitHub Actions 定时任务）

每个交易日北京时间 18:00（盘后，龙虎榜已披露）自动运行：

1. 拉取大盘指数（上证、深证、创业板、科创50、沪深300、北证50）与两市成交额；
2. 统计市场情绪：涨停/跌停/炸板家数、各市场涨跌家数；
3. 汇总行业板块涨跌幅前五、概念板块涨幅前五；
4. 拉取龙虎榜（盘后披露，自动取最近交易日）净买入前十；
5. 按 `config/watchlist.json` 监控自选股现价、涨跌幅、最高/最低、成交额；
6. 抓取当日财经要闻；
7. 写入 Notion：在日历数据库中创建当天记录
   （标题：`YYYY-MM-DD 股市复盘与自选股`，日期=当天，正文含全部内容）。

数据来源为东方财富公开接口，无需 API Key。

## 同花顺数据源（可选）

如已注册[同花顺官方金融数据服务](https://fuyao.aicubes.cn/)并创建 API Key，
把它设为环境变量 `HITHINK_FINANCE_API_KEY` 后，脚本会自动切换数据源：

- 大盘指数、自选股行情 → 同花顺行情快照
- 涨停池 → 同花顺涨停池（带涨停原因、连板数）
- 龙虎榜 → 同花顺龙虎榜（含机构/游资净额口径）
- 热股榜 → 同花顺热股榜（新增板块）
- 涨跌家数、板块涨跌、财经要闻 → 仍由东方财富补充

未设置 Key 时自动回退到东方财富数据源，功能与之前一致。
本地可用 `python3 src/collect.py --check-ths` 校验 Key。

## 需要的密钥

在 GitHub 仓库 Settings → Secrets and variables → Actions 中添加：

| Secret 名称 | 内容 |
| --- | --- |
| `NOTION_TOKEN` | Notion Integration Token（`secret_...`） |
| `NOTION_DATABASE_ID`（可选） | Notion 日历数据库 ID；不填则写入与 hackathon-daily 共用的 2026 数据库 |
| `HITHINK_FINANCE_API_KEY`（可选） | 同花顺官方数据 API Key；不填则使用东方财富数据源 |

数据库需要包含 **名称**（title）和 **日期**（date）两个属性，并已分享给该 Notion Integration。

## 本地试跑

```bash
python3 -m pip install -r requirements.txt
python3 src/collect.py --dry-run          # 只采集并打印，不写入 Notion
python3 src/collect.py --check-notion     # 校验 Notion token 与数据库
export NOTION_TOKEN=secret_xxx
python3 src/collect.py                    # 采集并写入 Notion
```

自选股清单编辑 `config/watchlist.json`：

```json
{
  "watchlist": [
    {"name": "贵州茅台", "code": "600519"}
  ]
}
```

也可用 `--watchlist 其他路径.json` 指定清单。

## 说明与限制

- 东方财富公开接口有频率限制，脚本已内置请求间隔、自动重试和最近交易日回退。
- 龙虎榜为盘后披露（约 17:00 后）；若在盘后运行仍为空，会回退到最近一个有效交易日。
- 定时任务按周一至周五（北京时间 18:00 = UTC 10:00）触发；法定节假日运行会取最近交易日数据。
- 数据仅供研究参考，不构成投资建议。

## 板块扫描（sector_scan.py）

在每日复盘之外，`src/sector_scan.py` 每天从全量行业+概念板块里，按
[docs/sector-workflow.md](docs/sector-workflow.md) 的流程筛选"低位启动 + 资金异动"候选：

1. 拉取大盘环境（指数 20 日线、量能、涨跌停/炸板率）并给出仓位上限参考；
2. 全量板块快照预筛（当日涨幅 -1.5%~+6%，当日收跌时看 5 日涨幅 ≥0.5%；成交额/量比/60 日涨幅/合成板块过滤）；
3. 逐板块拉资金流日线，计算距一年高点回撤、5/20/60 日涨幅、主力连续净流入天数；
4. 四维打分（位置/量价/资金/拥挤度 各 0–2 分，满分 8），总分 ≥4 进候选榜；
5. 输出候选榜、入选理由、与昨日对比、排除说明、待人工逻辑体检，并写入 Notion
   当天页面（标题 `YYYY-MM-DD 板块扫描候选`，与复盘记录同一日历数据库）。

运行方式：

```bash
python3 src/sector_scan.py --dry-run            # 只采集打印
python3 src/sector_scan.py                      # 写本地历史并写 Notion（需 NOTION_TOKEN）
python3 src/sector_scan.py --min-score 5 --top 15
python3 src/sector_scan.py --max-boards 40      # 控制逐板块请求量
python3 src/sector_scan.py --json               # 仅输出候选 JSON
```

说明：

- 数据源为东方财富公开接口（无需 Key）。东财对连续请求限流较严，脚本内置分页、
  请求间隔随机抖动、连续失败冷却；首次运行约 3–6 分钟，之后本地运行会复用
  `data/board_cache.json` 只做增量更新。
- 机器分不含"逻辑"维度——催化是否可持续、利好能否落到利润，仍需按报告里的
  "待人工逻辑体检"清单自行判断。
- "与昨日对比"依赖本地 `data/scan_history.json`。GitHub Actions 每次全新环境、
  不持久化该文件，因此对比仅在本地连续运行时有效；如需在云端保留跨日状态可再扩展。
- `data/` 目录已在 `.gitignore` 中，本地状态不提交到仓库。
- 板块扫描由独立定时任务 `sector-scan` 运行：每个交易日北京时间 16:00（UTC 08:00），
  早于 18:00 的复盘任务；不依赖龙虎榜，收盘后即可跑。
