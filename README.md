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
斜率是**硬性必过项**——斜率不过,另两条再好也是 `AVOID`。

`CONSIDER` 混了两种完全不同的情况,邮件里会直接标出是哪条没过**以及差多远**：
- `← IV 不够贵(策略核心边缘缺失): 1.222，差 0.028 到 1.25`：IV 相对已实现波动
  并不贵,卖近月没有优势——**根本没肉**
- `← IV 不够贵...: 0.69，差 0.56 到 1.25，已反向(IV 比已实现波动还便宜)`：
  iv30/rv30 < 1.0 时额外标注。这不是"就差一点",是**反过来了**
- `← 成交量不足(边缘在但可能难成交): 26.7万，差 123.3万 到 150万`：IV 确实贵
  但流动性不够——**有肉但难吃到**

差距必须显示出来:2026-08-13 的 confirm 里 NU(1.222,差 0.028)和 AMAT(0.69,反向)
渲染成了一模一样的「🟡 可考虑 ← IV 不够贵」,而这两者根本不是一回事。

**结论行**:当推荐=0 时,信号列表顶部会加一句结论,例如
`⛔ 结论: 无符合策略的标的。下列 4 只全部倒在「IV 不够贵」...`。
理由是四条 🟡 可考虑 排在一起,凌晨五点扫一眼极易读成"有 4 个候选可以下单",
而正确读法是"今晚没有符合策略的标的"。有任何推荐标的时这行静默(逐条信息已经够了);
preview 班次会附加"距开仓还有数小时,以 confirm 班次为准",因为它不是最终判断。

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

## 自动运行 + 邮件信号(notify.py)

已注册两个 Windows 计划任务(`taskschd.msc` 里叫 `EarningsIV-Preview` / `EarningsIV-Confirm`),
每天自动跑 `notify.py`,结果保存到 `scans/signal_*.txt` 并邮件发送(中文,含布里斯班分钟级开平仓时间):

| 任务 | 布里斯班触发时间 | 对应 ET 时刻 | 作用 |
|---|---|---|---|
| Preview | 00:15 和 01:15 | 开盘后 ~45 分钟 | 未来 3 天候选预览 + 平仓安全网提醒 |
| Confirm | 05:15 和 06:15 | 收盘前 ~45 分钟 | 只列**今天开仓**的票,通过筛选才值得下单 |

Preview 不能再早:Yahoo 期权报价延迟 15 分钟,开盘后约 30 分钟内拿到的还是
盘前清零快照,全部会误判成 NO_DATA(2026-07-15 实测踩过这个坑)。

每个任务设两个触发时间是因为美国有夏令时而布里斯班没有:脚本自带 ET 时段检查,
两个触发里只有一个会真正执行,另一个自动跳过。夏令时切换无需改任何配置。

**收信流程**:午夜后收到 Preview 邮件 → 知道明早可能开仓什么 → 早上 ~05:20(冬令时 ~06:20)
收到 Confirm 邮件 → 若有 `✅ 推荐` 且 `earnings_check` 无 MISMATCH → 在开仓时刻
(夏令时 05:45,冬令时 06:45,= ET 15:45)手动下单 ATM 日历价差 → 当晚 23:45(冬令时
00:45,= ET 09:45)平仓;Preview 邮件里附平仓安全网提醒(它比平仓时刻晚半小时到)。

**邮件配置**(一次性)。`notify_config.json` 支持两种发信通道,有 `resend_api_key`
就走它,否则回退 Gmail SMTP:

```json
{
  "resend_api_key": "re_xxxxxxxx",
  "mail_from": "onboarding@resend.dev",
  "send_to": "you@gmail.com"
}
```

- **Resend(HTTPS 443,服务器上必须用这个)**:去 https://resend.com 注册,
  注册邮箱要和 `send_to` 一致,拿 API key 填进 `resend_api_key`。`mail_from` 不填
  默认用 `onboarding@resend.dev`(Resend 的公共发件地址,不需要你有域名)。
  ⚠️ 免费版未验证域名时**只能发给注册账号的那个邮箱**——单收件人正好够用,
  以后要加第二个收件人就得验证一个自有域名、并把 `mail_from` 换成该域名下的地址。
- **Gmail SMTP(端口 465)**:去 https://myaccount.google.com/apppasswords 生成应用
  专用密码填 `gmail_app_password`。Windows 本地可用;**droplet 上不通**(见下文 DO 封端口)。

`send_to` 可以写成逗号分隔的多个地址。两个都不配则只存本地文件不发邮件。

**注意**:电脑需处于开机或睡眠状态(任务已设置"唤醒运行"+"错过后尽快补跑";
补跑时若已错过 ET 窗口会自动跳过,不会发过期信号)。运行日志在 `scans/notify_log.txt`。

两个班次在窗口内运行时**一定会发邮件**(有信号/无信号/出错都发):早晨没收到
邮件只有一个含义——任务没跑(电脑睡死/错过窗口),不存在"没信号所以没发"。
Preview 邮件开头附**昨日信号自动复盘**(以真实 ET 09:45 出场价评分,含累计统计),
原始扫描结果与复盘明细存 `scans/{preview,confirm}_日期.csv` 和 `scans/replay_log.csv`。

手动测试:`python notify.py confirm --force`(跳过时段检查)。

## 部署到 Linux 服务器(deploy.sh)

跑在 droplet 上就不用担心笔记本睡死漏掉信号。两条命令(把分支名和 IP 换成实际的):

```bash
git clone -b feat/refactor https://github.com/chrisudf/earnings-iv-scanner.git /root/earnings-iv-scanner
```

`notify_config.json` 在 `.gitignore` 里,不会跟着 git 走,要从本地单独传(在**本地**机器上跑):

```bash
scp notify_config.json root@<droplet-ip>:/root/earnings-iv-scanner/
```

然后在服务器上:

```bash
bash /root/earnings-iv-scanner/deploy.sh
```

`deploy.sh` 会建 venv、装依赖、校验邮件配置、写 crontab、最后 `--force` 跑一次冒烟测试
(会真发一封邮件)。**幂等**——重复跑不重复装、不重复加 cron 行,改完代码 `git pull`
后再跑一遍即可。`--no-cron` 只建环境不碰 crontab。

**cron 和 Windows 一样需要双触发**。⚠️ **Debian/Ubuntu 的 vixie-cron 不支持
`CRON_TZ`**(Ubuntu 24.04 的 cron 3.0pl1-184 二进制里没这个字符串,写了被静默忽略
—— 2026-08-03-05 踩过这个坑,连续三天所有任务都按服务器本地时间触发、落在 ET 窗口外
全部跳过,一封信号都没发)。排程只能按服务器本地时区解释。

所以 deploy.sh 从 ET 目标时刻**反推**本地触发时刻,每个模式两个(美国冬/夏令时各一个),
由 `notify.py` 的 ET 窗口检查跳过不匹配的那个。服务器在布里斯班时算出来是:

```
15 0,1 * * 2-6  ...notify.py preview   # ET 10:15 = AEST 00:15(夏)/01:15(冬)
15 5,6 * * 2-6  ...notify.py confirm   # ET 15:15 = AEST 05:15(夏)/06:15(冬)
```

星期是 `2-6` 而非 `1-5`:ET 周一 10:15 已经是布里斯班周二凌晨,跨了日期,deploy.sh
会自动平移。换服务器/换时区不用改脚本,重跑 deploy.sh 会按新时区重算。

**不要用 `timedatectl set-timezone` 改全局时区**来"解决"这个问题 —— 同一个 crontab
里的其他任务可能是按现有本地时区换算过的,改时区会把它们一起推移。

旧 crontab 每次部署自动备份到 `scans/crontab.backup.<时间戳>`。

**迁移注意**:

- 别长期两边同时开——同一封信收两遍,而且 `scans/replay_log.csv` 的累计复盘统计
  会在两台机器上各记一份、互相对不上。建议并行一天做对照,确认服务器信号与本地
  一致后再 `Disable-ScheduledTask -TaskName EarningsIV-Preview`(和 `-Confirm`)。
- 服务器 IP 在数据中心机房,Yahoo 的限速表现可能和家用宽带不同。首次冒烟测试若出现
  大批 `NO_DATA` 或超时,那是 IP 被限速而非代码问题。
- 服务器日志:`scans/notify_log.txt`(脚本自己的)和 `scans/cron.log`(cron 捕获的
  stdout/stderr,含 traceback)。

### ⚠️ DigitalOcean 封了出网 SMTP 端口(2026-08-18 起)

**症状**:cron 按点触发了、扫描跑完了、报告也存进 `scans/` 了,但一封邮件都没有。
`scans/notify_log.txt` 里是:

```
File "notify.py", line 99, in send_email
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
OSError: [Errno 101] Network is unreachable
[... BNE / ... ET] 错误邮件也发送失败
```

最后那行是关键——连报错通知本身也发不出去,所以外部**完全静默**,不会有任何提示。

**病因**:droplet 的出网 25 / 465 / 587 端口被 DigitalOcean 在网络层封了,不是代码、
不是 Gmail 应用专用密码过期。在 droplet 上强制 IPv4 实测:

| 目标 | 结果 |
| --- | --- |
| smtp.gmail.com:25 / 465 / 587 | 超时 |
| smtp.mail.yahoo.com:465 | 超时 |
| smtp.qq.com:465 | 超时 |
| smtp.office365.com:587 | 超时 |
| www.google.com:443 | OK |
| query1.finance.yahoo.com:443 | OK |
| api.resend.com:443 | OK |

**所有**服务商的 SMTP 端口全挂而 443 完好 → 端口级封锁。DO 默认对账号封 outbound
SMTP,需开工单申请解封(批得慢且常被拒)。

已排除的:`ufw inactive`;`iptables OUTPUT` policy ACCEPT;nftables 里只有 Docker
规则,没有拦出网的;机器没重启(up 162 天);cron 服务 active、crontab 条目也在。

**排查时注意报错会指错方向**:droplet 只有 link-local 的 IPv6、没有全局 v6 地址,
而 `smtp.gmail.com` 有 AAAA 记录。`socket.create_connection` 会先试 IPv6 立即拿到
`Network is unreachable`,再试 IPv4 静默超时,最后 `raise exceptions[0]` 抛出**第一个**
异常。所以你看到的是 IPv6 的错,真正的病因是 IPv4 那条路上端口被封。别去修 IPv6。

**时间线**:2026-08-15 05:15 最后一次发信成功 → 2026-08-18 00:15 第一次失败,周末期间
DO 侧生效。

**修法**:改走 Resend 的 HTTPS API(443 通)。`notify.py` 的 `send_email()` 已按
`resend_api_key` 是否存在自动选通道,只需在 droplet 上把 key 写进 `notify_config.json`:

```bash
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path("/root/earnings-iv-scanner/notify_config.json")
c = json.loads(p.read_text())
c["resend_api_key"] = "re_xxxxxxxx"     # 换成你的
c["mail_from"] = "onboarding@resend.dev"
p.write_text(json.dumps(c, indent=2))
EOF
chmod 600 /root/earnings-iv-scanner/notify_config.json
cd /root/earnings-iv-scanner && .venv/bin/python notify.py confirm --force   # 冒烟,会真发一封
```

`deploy.sh` 的配置校验也会拦下"服务器上只配了 Gmail SMTP"这种情况并给出提示。
不改 crontab、不改窗口逻辑——坏的只有最后一步发信。

## 数据源

- 成分股：Wikipedia "Russell 1000 Index"（iShares IWB 有反爬墙，不可用）
- 财报日历：Nasdaq 公开 API（含 BMO/AMC 标注和市值）
- 财报日期交叉核对：yfinance `Ticker.calendar`（不一致时输出 `MISMATCH`，务必人工确认）
- 期权链 / 历史价格：yfinance（Yahoo 数据有 15 分钟延迟）

## 已知局限与风险

- 交易日推算内置 2026–27 年 NYSE 全日休市表（`scanner.py` 的 `NYSE_HOLIDAYS`,
  **每年更新一次**）；早收盘半日（感恩节次日等）未建模。
- Nasdaq 日历偶尔有公司改期不及时；`earnings_check` 为 MISMATCH 或 n/a 的票
  **人工确认日期后再交易**（yfinance 也可能是错的一方——2026-07-15 ASML 就是
  yf 错、Nasdaq 对，不要无条件信任任何一边）。
- **AMC 当晚被行权风险**：期权行权截止（OCC ~17:30 ET）在盘后财报公布**之后**。
  15:45 卖出的 ATM call，若 16:05 股价大幅跳涨，可能**当晚就被行权**——你在
  布里斯班睡觉，醒来时持有 short stock（远月 long call 做 cover，但 IBKR 的
  保证金/强平机制需提前了解）。09:45 出场避开的是 pin risk，避不开这个。
- yfinance 非官方 API，Yahoo 改版可能导致失效。
- 筛选指标是"扫描时刻"的快照，不等于开仓时刻的值——下单前重跑确认。
- 信号带 `⚠ 数据/结构标记`（ZERO_BID/WIDE_SPREAD/IV_DIVERGENCE 等）的票要
  格外谨慎：这些标记专抓"指标看着漂亮、期权链根本不可交易"的情况（如 WIT）。
