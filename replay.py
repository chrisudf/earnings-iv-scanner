#!/usr/bin/env python3
"""Level-1 replay: score past confirm signals against what actually happened.

Runs inside the preview job (ET ~10:15). Finds rows in scans/confirm_*.csv
whose EXIT date is today, fetches the entry-day close and today's REAL
09:45 ET price (1-minute bars; falls back to the official open when 1m data
is unavailable), grades the realized move against the signal's expected
move, appends everything to scans/replay_log.csv, and returns a Chinese
summary block for the preview email.

Grading uses the true 09:45 exit moment, not the open: in the 2026-07-15/16
manual replay, CTAS (+1.99% open -> +6.13% @09:45) and HOMB (+2.05% ->
+4.44%) both looked fine at the open and had breached EM by the actual exit.

ALL tiers are scored (AVOID/NO_DATA included) — threshold tuning later needs
outcomes for the rows the filter would have excluded, otherwise the sample
is survivorship-biased. NO_DATA / missing-EM rows are logged as censored (❓).
"""

from datetime import timedelta

import pandas as pd
import yfinance as yf

from scanner import COL_ENTRY, COL_EXIT, ET, SCAN_DIR

REPLAY_LOG = SCAN_DIR / "replay_log.csv"

# ratio = |realized move| / expected move
GRADE_BANDS = [(0.5, "✅"), (0.85, "🟢"), (1.15, "🟠")]  # above the last -> 🔴


def grade(ratio):
    if ratio is None:
        return "❓"
    for cutoff, emoji in GRADE_BANDS:
        if ratio <= cutoff:
            return emoji
    return "🔴"


def _real_prices(symbol, entry_d, exit_d):
    """(entry_close, exit_open, px_0945) — px_0945 None if 1m data missing."""
    t = yf.Ticker(symbol)
    end = (pd.Timestamp(exit_d) + timedelta(days=1)).strftime("%Y-%m-%d")
    daily = t.history(start=entry_d, end=end, auto_adjust=False)
    daily.index = [str(i)[:10] for i in daily.index]
    if entry_d not in daily.index or exit_d not in daily.index:
        raise ValueError(f"missing daily bars for {symbol}")
    entry_close = float(daily.loc[entry_d, "Close"])
    exit_open = float(daily.loc[exit_d, "Open"])

    px_0945 = None
    try:
        m1 = t.history(start=exit_d, end=end, interval="1m")
        if m1 is not None and not m1.empty:
            et_idx = m1.index.tz_convert(ET)
            bar = m1[(et_idx.strftime("%Y-%m-%d") == exit_d)
                     & (et_idx.strftime("%H:%M") == "09:45")]
            if not bar.empty:
                px_0945 = float(bar["Open"].iloc[0])
    except Exception:
        pass  # 1m data is best-effort; the open is the fallback
    return entry_close, exit_open, px_0945


def _signals_exiting(today_str):
    """Latest confirm_*.csv row per symbol whose exit date == today."""
    frames = []
    for path in sorted(SCAN_DIR.glob("confirm_*.csv")):
        try:
            df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
        except Exception:
            continue
        if COL_EXIT in df.columns:
            df = df[df[COL_EXIT] == today_str]
            if not df.empty:
                frames.append(df)
    if not frames:
        return None
    allrows = pd.concat(frames, ignore_index=True)
    return allrows.drop_duplicates(subset="symbol", keep="last")


def build_replay_report(today_et):
    """Chinese '昨日信号复盘' block for the preview email ('' if nothing due)."""
    today_str = str(today_et)
    rows = _signals_exiting(today_str)
    if rows is None or rows.empty:
        return ""

    scored, lines = [], ["—— 信号复盘(平仓日=今天,以真实 09:45 出场价为准) ——"]
    for _, r in rows.iterrows():
        sym, tier = r["symbol"], r.get("tier", "?")
        rec = {"exit_date": today_str, "symbol": sym, "tier": tier,
               "timing": r.get("timing"), "entry_date": r.get(COL_ENTRY),
               "em_pct": None, "flags": r.get("flags"),
               "front_dte": r.get("front_dte")}
        try:
            em = float(r["expected_move_pct"])
        except (KeyError, TypeError, ValueError):
            em = None
        rec["em_pct"] = em
        try:
            entry_close, exit_open, px_0945 = _real_prices(
                sym, r[COL_ENTRY], today_str
            )
            px_exit = px_0945 if px_0945 is not None else exit_open
            move = (px_exit / entry_close - 1) * 100
            gap_open = (exit_open / entry_close - 1) * 100
            ratio = abs(move) / em if em else None
            g = grade(ratio)
            rec.update(entry_close=round(entry_close, 2),
                       gap_open_pct=round(gap_open, 2),
                       px_exit=round(px_exit, 2),
                       exit_px_is_0945=px_0945 is not None,
                       move_pct=round(move, 2),
                       ratio=None if ratio is None else round(ratio, 2),
                       grade=g)
            if tier in ("RECOMMENDED", "CONSIDER"):
                em_txt = f"{em}%" if em else "?(EM缺失)"
                src = "" if px_0945 is not None else "(1m缺失,用开盘价)"
                lines.append(f"{g} {sym} [{tier}] 实际{move:+.2f}%{src} "
                             f"(开盘{gap_open:+.2f}%) vs 预期{em_txt}")
        except Exception as e:
            rec.update(grade="ERR", error=str(e)[:120])
            if tier in ("RECOMMENDED", "CONSIDER"):
                lines.append(f"⚠ {sym} 复盘取价失败: {e}")
        scored.append(rec)

    log_df = pd.DataFrame(scored)
    SCAN_DIR.mkdir(exist_ok=True)
    log_df.to_csv(REPLAY_LOG, mode="a", index=False, encoding="utf-8-sig",
                  header=not REPLAY_LOG.exists())

    n_other = int((~rows["tier"].isin(["RECOMMENDED", "CONSIDER"])).sum())
    if n_other:
        lines.append(f"(另有 {n_other} 条 AVOID/NO_DATA 已记入 replay_log,"
                     f"供以后校准阈值)")

    # Cumulative actionable-tier stats from the whole log.
    try:
        full = pd.read_csv(REPLAY_LOG, encoding="utf-8-sig", dtype=str)
        act = full[full["tier"].isin(["RECOMMENDED", "CONSIDER"])]
        counts = act["grade"].value_counts()
        total = int(counts.sum())
        parts = "  ".join(f"{g}{int(counts.get(g, 0))}"
                          for g in ("✅", "🟢", "🟠", "🔴", "❓"))
        lines.append(f"累计(推荐+可考虑, n={total}): {parts}")
    except Exception:
        pass

    lines.append("注: 跳空 vs 预期波动是代理指标,不等于日历价差实际盈亏。")
    return "\n".join(lines) + "\n" + "-" * 46 + "\n"
