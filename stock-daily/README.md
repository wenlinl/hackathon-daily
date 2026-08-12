# A股每日复盘 + 自选股监控（GitHub Actions 定时任务）

每个交易日北京时间 18:00 自动运行：

1. 拉取大盘指数（上证、深证、创业板、科创50、沪深300、北证50）与两市成交额；
2. 统计市场情绪：涨停/跌停/炸板家数、各市场涨跌家数；
3. 汇总行业板块涨跌幅前五、概念板块涨幅前五；
4. 拉取龙虎榜（盘后披露，自动取最近交易日）净买入前十；
5. 按 `config/watchlist.json` 监控自选股现价、涨跌幅、最高/最低、成交额；
6. 抓取当日财经要闻；
7. 写入 Notion：在日历数据库中创建当天记录
   （标题：`YYYY-MM-DD 股市复盘与自选股`，日期=当天，正文含全部内容）。

数据来源为东方财富公开接口，无需 API Key。

## 需要的密钥

在 GitHub 仓库 Settings → Secrets and variables → Actions 中添加：

| Secret 名称 | 内容 |
| --- | --- |
| `NOTION_TOKEN` | Notion Integration Token（`secret_...`） |
| `NOTION_DATABASE_ID`（可选） | Notion 日历数据库 ID；不填则写入与 hackathon-daily 共用的 2026 数据库 |

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
