# Earnings IV-Crush Calendar-Spread Scanner

把原来的单票 GUI 计算器（`trade calculator/calculator.py`）改写成的**罗素 1000 自动扫描器**：
自动找出未来 1–3 天内发财报的成分股，逐一跑原作者回测过的三条筛选标准，按推荐等级输出。

> **免责声明**：仅供学习研究，不构成投资建议。

## 策略逻辑（来自原作者，未改动）

**结构**：ATM 日历价差 — 卖近月平值、买远月平值（同行权价），近远月到期日相差约 30 天。

**筛选标准**（三条硬指标，阈值来自回测，勿随意改动）：

| 指标 | 含义 | 阈值 |
|---|---|---|
| `avg_volume_30d` | 30 日平均成交量（流动性门槛） | ≥ 1,500,000 |
| `iv30_rv30` | 30 天隐含波动率 ÷ Yang-Zhang 30 天已实现波动率（IV 是否偏贵） | ≥ 1.25 |
| `ts_slope_0_45` | IV 期限结构斜率，近月 → 45 天（财报升水导致的倒挂程度） | ≤ −0.00406 |

**分级**（与原 GUI 一致）：三条全过 = `RECOMMENDED`；斜率过 + 另两条只过一条 = `CONSIDER`；其余 = `AVOID`。

**开平仓时机**：
- 开仓：财报公布前最后一个交易日，收盘前约 15 分钟（ET 15:45）
- 平仓：财报公布后第一个交易日，开盘后约 15 分钟内（ET 09:45 前）
- 扫描器已按 BMO（盘前）/ AMC（盘后）自动推算出每只票的开仓日和平仓日
- timing 为 `?`（交易所未标注盘前/盘后）时按 AMC 处理，下单前务必自行核实

## 用法

```bash
cd earnings-iv-scanner
.venv/bin/python scanner.py                  # 默认扫描未来 1-3 天
.venv/bin/python scanner.py --min-days 0     # 包含今天（AMC 财报当天开仓）
.venv/bin/python scanner.py --tickers LEVI,PEP   # 直查个股（等价于原 GUI）
.venv/bin/python scanner.py --universe all   # 不做成分股过滤，扫全部报告者
```

结果打印到终端并保存 CSV 到 `scans/`。

### 宇宙（universe）说明

- `auto`（默认）：Wikipedia 罗素 1000 名单 **∪** 市值 ≥ $5B 的财报公司。
  Wikipedia 名单是社区维护、可能滞后（实测漏了 LEVI），市值兜底能补上这类漏网之鱼。
- `wiki`：严格按 Wikipedia 名单。
- `all`：不过滤（成交量阈值本身就会过滤掉小票）。
- 也可传一个文件路径：每行一个代码，或带 Symbol/Ticker 列的 CSV。

名单缓存在 `russell1000_cache.json`（7 天有效），`--refresh-universe` 强制刷新。

## ⚠️ 重要：必须在美股盘中运行

Yahoo 在收盘后会把期权 bid/ask 清零、IV 字段变成垃圾值。盘外运行时，
扫描器仍会给出**候选名单和开平仓日期**（标记 `NO_DATA`），但 IV 筛选无法进行。

**布里斯班时间对照**（美国夏令时期间）：
- 美股盘中 ≈ 23:30 – 06:00 AEST
- 建议流程：**晚上 23:30 后跑一次**拿到完整信号 → 对于次日开仓的票，
  **早上 05:30–05:45 再跑一次确认**（IV 指标盘中会变，作者的回测基于临近收盘的数据），
  确认通过就在 05:45–06:00（= ET 15:45–16:00）下单。

## 数据源

- 成分股：Wikipedia "Russell 1000 Index"（iShares IWB 有反爬墙，不可用）
- 财报日历：Nasdaq 公开 API（含 BMO/AMC 标注和市值）
- 财报日期交叉核对：yfinance `Ticker.calendar`（不一致时输出 `MISMATCH`，务必人工确认）
- 期权链 / 历史价格：yfinance（Yahoo 数据有 15 分钟延迟）

## 已知局限

- 交易日推算只跳过周末，未处理美股节假日（节假日前后自行核对开平仓日）。
- Nasdaq 日历偶尔有公司改期不及时；`earnings_check` 列为 MISMATCH 的票不要交易。
- yfinance 非官方 API，Yahoo 改版可能导致失效。
- 筛选指标是"扫描时刻"的快照，不等于开仓时刻的值——下单前重跑确认。
