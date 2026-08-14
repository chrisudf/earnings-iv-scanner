# TODO — earnings-iv-scanner

(v2, 2026-07-18:合并 2026-07-18 code review 38 条确认发现 + 7/15–16 信号
真实数据复盘的结论;v1 基于 7/15–17 三批 31 条信号的人工复盘)

## 已落地(fixes.patch + improvements.patch,详见 review 文件夹)

- [x] 每次 notify 运行把**过滤前全量结果**(含 AVOID/NO_DATA)存
      `scans/{preview|confirm}_YYYY-MM-DD.csv` —— 只记幸存信号 = 幸存者偏差,
      永远调不了阈值
- [x] Level 1 自动复盘:preview 班次(ET ~10:15)对"平仓日=今天"的 confirm
      信号取**真实 09:45 价**(1 分钟线,缺失退回开盘价)评分 ✅🟢🟠🔴,
      追加 `scans/replay_log.csv`,复盘段置于 preview 邮件开头 + 累计统计。
      为什么必须用 09:45 而不是开盘价:7/15–16 实测 CTAS(开盘+1.99% →
      09:45 +6.13%)和 HOMB(+2.05% → +4.44%)开盘看是赢、真实出场已击穿
- [x] 数据质量/结构**咨询性标记**(只加 ⚠ 和 CSV 列,不改 tier;阈值是
      临时值,写在 scanner.py FLAG_* 常量里):ZERO_BID、WIDE_SPREAD、
      IV_DIVERGENCE、EXTREME_IVRV(>3)、THIN_PREMIUM、EM_IV_MISMATCH
      (straddle-EM vs 0.8·IV·√T 一致性,HOMB 模式)、FAR_FRONT(>7)、
      NO_BACK_LEG、LOW_OI、SMALL_CAP(<$10B,即 v1 第 2 条的警告版)
- [x] zero-bid 跨式守卫(WIT "17.62% 预期波动" 的根因:ATM call bid=0/ask
      0.15,mid=ask/2 全是虚构)
- [x] NYSE 假日(2026–27 内置,**每年更新** scanner.py NYSE_HOLIDAYS);
      危险面在出场侧:周五 AMC 遇假日周一会多扛一夜
- [x] earnings_check="n/a" 在邮件中可见(ASML 7/15:yf 日期错、Nasdaq 对、
      邮件却零提示);MISMATCH 措辞改为"人工确认"——yf 也可能是错的一方,
      自动"勿交易"会误杀正确信号

## 1. 阈值校准(等 replay_log n≥100,当前全部只记录不过滤)

- [ ] 用 replay_log + confirm CSV 把 FLAG_* 临时阈值换成数据支持的值。
      规则:每个阈值**预登记一个候选值**再看数据;按**时间**切 out-of-sample
      (财报按季聚集,100 条 ≈ 1–2 个季度的同一波动率环境,有效 n 远小于
      100);同时调多个阈值 = 多重检验,任何调出来的线要活过下一个财报季
- [ ] **主指标 = 每笔 P&L(% of debit)+ bootstrap CI,胜率降为次要**。
      理由:BLK 一笔(+7.27% vs EM 4.4%)吃掉数笔小赢,胜率是错的目标函数;
      且 EM 虚高的票(WIT)按"跳空<EM"永远算赢——用胜率会奖励坏数据
- [ ] IV/RV>3 单独出现只标 EXTREME 不硬砍;IV/RV>3 且 (WIDE_SPREAD 或
      IV_DIVERGENCE) 才归 BAD_DATA,且 BAD_DATA 连坐 iv30/slope(同一条
      spline 全被污染)。EM<1.5% 不是数据问题是"没肉"——THIN_PREMIUM 用
      绝对金额:straddle mid ≥ ~$0.75–1.00 或 debit ≥ 8–10× 往返成本
- [ ] 校准后决定:哪些 flag 升级为硬过滤 / confirm 里降级到"人工核实"段

## 2. 复盘实现方法(Level 1 已实现 / Level 2 待做)

### Level 1 — 跳空 vs 预期波动(已实现,replay.py)

**原理**:不追踪期权价格,只追踪标的。日历价差 = 收盘前"卖出财报跳空",
收的权利金里含市场定价的预期波动(EM)。实际波动 < EM → 大概率赚;
> EM → 大概率亏。

**数据来源(全部可回溯的免费数据,两边拼)**:
- 信号侧:`scans/confirm_日期.csv`(confirm 班次扫描时自己写,含 symbol/
  tier/EM/开平仓日期/flags)
- 价格侧:次日 preview 班次(ET ~10:15)从 yfinance 拉**股票**数据——
  日线取开仓日收盘价,**1 分钟线取平仓日 09:45 真实价**(缺失退回开盘价,
  `exit_px_is_0945` 列标注)。股票分钟线可回看 ~30 天:错过一班,30 天内
  随时可重建(这是 Level 1 只看标的的原因——只有股票数据能事后补)

**计算与输出**:ratio = |实际波动| / EM → ✅<0.5、🟢<0.85、🟠<1.15、
🔴≥1.15、EM 缺失 ❓(分档在 replay.py GRADE_BANDS);逐条追加
`scans/replay_log.csv`(含原始 ratio、开盘跳空、09:45 价),复盘段 +
累计统计放 preview 邮件开头。为什么用 09:45 不用开盘:7/15-16 实测
CTAS(开盘 +1.99% → 09:45 +6.13%)、HOMB(+2.05% → +4.44%)。

**解读规则(重要)**:两端可靠、中间带失真——
- ✅(<0.5)和 🔴(>1.15)当真话听:近月权利金几乎全收 / 近月被打穿
- 🟢🟠(约 0.6~1.3×EM)当"未知":真实盈亏取决于**远月 IV 崩多少**,
  Level 1 完全看不见这个分量(它才是利润来源)。且斜率筛选(≤−0.00406)
  专挑期限结构倒挂最狠的票,这类票财报后远月 IV 回落也最狠——策略的
  选股条件本身放大中间带失真。ratio 原始值已存列,以后可重新分桶
- [ ] Level 1 评分限 front_dte ≤ ~5(EM 含时间扩散,front_dte 大的票
      系统性宽松——WIT/LEVI 都是月度期权,front_dte 35 时 EM 根本不是
      财报跳空;front_dte 列已在 CSV 里,评分时过滤即可)
- [ ] 复盘评分前核对财报**真的发生了**(公司会改期;`get_earnings_dates()`
      事后核对,否则把无事发生的一天记成 ✅)

### Level 2 — 纸面期权 P&L(待做;能回答 🟢🟠 中间带的真实盈亏)

**为什么必须现场活捉**:免费世界**没有历史期权数据**,Yahoo 只给当前链
("7/15 15:15 WIT 八月 call 的 bid"事后永远拿不回来)。错过一班 = 数据点
永久丢失 → 记 `days_late` 列显式标注补跑,别静默跳过。

- [ ] **采集(confirm 班次,ET ~15:15,开仓前 30 分钟)**:拉实时链,记入
      `scans/positions.csv`:symbol、日期、ATM 行权价、front/back 到期日
      (back = front+20~45 天里最接近 +30 的,quality_flags 已在算)、
      **每腿 bid 和 ask**(不是只记 mid)、每腿 IV/OI、spot。
      实现:复用 scanner.py 的 `_atm_leg_stats()`;合约身份用
      (expiry, strike) 或 contractSymbol 存下来,次日按同一身份取价
- [ ] **定价(次日 preview 班次,ET ~10:15,平仓后 30 分钟)**:再拉链,
      对同两张合约取价,算**三档成交假设**:
      ① mid-to-mid(上界)② 穿价差(下界:开仓买 ask 卖 bid,平仓反向)
      ③ mid ± 25–50% 半价差(现实,耐心限价单)——**三档都扣佣金**
      (IBKR ~$2.6+/spread 往返,对小 debit 是大数)。
      结果追加 `scans/paper_pnl.csv`,邮件累计统计从胜率切到 P&L 口径
- [ ] **已知偏差(记录、不修)**:Yahoo 延迟 15 分钟 → "开仓价"实为
      15:00 快照(比真实 15:45 早 45 分钟,恰逢财报前 IV 爬坡段),
      "平仓价"实为 10:00 快照(比 09:45 晚 15 分钟)。每行记采集时间戳,
      偏差方向系统性,样本内一致即可比较
- [ ] **升级路径**:IBKR API(OPRA 订阅,真实时 NBBO)替换 Yahoo 侧;
      或直接用 IBKR paper 账户自动开平仓——成交记录本身就是带滑点模拟的
      Level 2(paper 对价差单成交偏乐观,三档假设照算)

## 3. 交易规则(代码外,写下来贴屏幕上)

- [ ] 单笔 debit ≤ 账户 1–2%
- [ ] **同夜并发上限 3–5 单 + 当夜总 debit 封顶**。7/15 一晚 9 条信号
      (5 推荐 + 4 可考虑),vol-crush 失败的成分同夜相关;ELV(−9.88% vs
      EM 6.5%)和 BLK 同一晚——没有任何筛选能防住这类真尾部,只有仓位管理
- [ ] timing='?' 的票:确认邮件里视为"人工核实",不直接下单(半自愈:BMO
      错标 AMC 的票 confirm 时 IV 多半已 crush 被筛掉,但 muted print 会漏)

## 4. 排期靠后

- [ ] README 补一段 **AMC 当晚被行权风险**:OCC 行权截止 ~17:30 ET 在 AMC
      公布之后;15:45 卖出的 ATM call,16:05 跳 +8% 可能当晚被行权,醒来时
      short stock(long back-month 做 cover,IBKR 保证金机制要心里有数)。
      09:45 出场避开 pin risk,避不开这个
- [ ] timing='?' 第二数据源:Finnhub 免费日历有显式 hour 字段(bmo/amc),
      比 yfinance 干净;监控 '?' 占比作为 Nasdaq schema 漂移的金丝雀
- [ ] IBKR 数据/paper 通道:正确终点(OPRA 订阅拿真 NBBO;paper 账户天然
      跑 Level 2),但先在免费栈上验证策略;注意 IBKR paper 对非流动 spread
      的成交模拟过于乐观,三档成交假设照样要

## 明确不做(评审结论,免得以后重新想一遍)

- 早收盘半日建模(半日几乎没有 R1000 财报;错过的是入场不是坏交易)
- ex-div 核对(1 晚持仓 + 财报前 extrinsic 高,分红行权概率低)
- workers 4→8 / 共享 Session / HTTP 缓存(65 分钟窗口内 20–40s 无感,
  加并发是唯一有限流下行风险的改动)
- 期权链懒加载重构(定时任务都在盘中跑,收益小、动"原样复制"的数学区风险大)
- Nasdaq 日历并行拉取(省几秒,还提高被反爬概率)

## 背景数据

### v1(2026-07-15–17,31 条信号,人工复盘)
- 不过滤: 23/31 有利(74%),🔴 3 条(BLK、MAN、FNB)
- 数据质量过滤剔除: CBSH(IV/RV 5.14)、FFIN(8.36)、FNB(预期波动 0.77%)
- 市值 ≥$10B 再剔除: CAG、HOMB、VIST、ALV、MAN;≥$20B 版本弃用

### v2 修正(2026-07-18,7/15–16 两批 12 条按真实数据重放)
- **v1 漏记 ELV**:开盘 −7.97% / 09:45 −9.88% vs EM 6.5%,明确 🔴 →
  74% 有利率被高估;且 ELV/BLK 用任何数据质量或市值过滤都拦不住(真尾部)
- 按真实 09:45 度量,🔴 从 2 条变 4 条(ELV、BLK、CTAS、HOMB)——开盘价
  代理系统性乐观
- WIT 的"赢"不可度量(EM 17.62% 是 zero-bid 虚构),v1 里"$20B 误杀 WIT
  赢单"的论据被同一缺陷污染——市值线结论需在新样本上重估
- "误杀"名单里 FHN 按 09:45(−1.71%)确实有利,按开盘(−3.58% vs 3.7%)
  是边缘案例
