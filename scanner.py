#!/usr/bin/env python3
"""
Earnings IV-crush calendar-spread scanner.

Scans the Russell 1000 for stocks reporting earnings in the next N days
(default 1-3), then runs the backtested screening criteria from the
original trade calculator on each candidate:

    avg_volume_30d >= 1,500,000
    iv30 / rv30 (Yang-Zhang) >= 1.25
    term-structure slope (front DTE -> 45 DTE) <= -0.00406

Tiers (same logic as the original GUI):
    RECOMMENDED : all three pass
    CONSIDER    : slope passes + exactly one of the other two
    AVOID       : anything else

Strategy timing (from the author):
    Entry: last trading day BEFORE the announcement, ~15 min before close.
    Exit : first trading day AFTER the announcement, ~15 min after open.
    Structure: ATM calendar spread, ~30 days between short and long legs.

DISCLAIMER: educational/research use only. Not investment advice.
"""

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from scipy.interpolate import interp1d

try:
    from yfinance.exceptions import YFRateLimitError
except ImportError:  # older/newer yfinance layouts
    class YFRateLimitError(Exception):
        pass

ET = ZoneInfo("America/New_York")
BNE = ZoneInfo("Australia/Brisbane")

# Trade moments in ET: enter 15 min before the close, exit 15 min after the open.
ENTRY_ET_HM = (15, 45)
EXIT_ET_HM = (9, 45)

# DataFrame/CSV column keys (display captions doubling as machine keys —
# referenced from notify.py too, so never inline these strings).
COL_ENTRY = "entry (close-15m)"
COL_EXIT = "exit (open+15m)"

BASE_DIR = Path(__file__).resolve().parent
UNIVERSE_CACHE = BASE_DIR / "russell1000_cache.json"
SCAN_DIR = BASE_DIR / "scans"

WIKI_URL = "https://en.wikipedia.org/wiki/Russell_1000_Index"
ISHARES_IWB_URL = (
    "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
)
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept": "application/json, text/html, */*",
}

# Backtested thresholds from the original calculator — do not tweak casually.
MIN_AVG_VOLUME = 1_500_000
MIN_IV30_RV30 = 1.25
MAX_TS_SLOPE = -0.00406

# Below this, Yahoo's ATM IV is stale/garbage (typically outside US RTH).
STALE_IV_FLOOR = 0.05

# Advisory data-quality / structure flags. PROVISIONAL thresholds — they only
# annotate rows (⚠ line in the email, `flags` column in the CSV) and NEVER
# change the tier. Calibrate them from scans/replay_log.csv once n>=100.
# Evidence (2026-07-15/16 replay): WIT's "17.62% expected move" came from a
# zero-bid ask/2 straddle on a $1.84 stock; HOMB paired IV/RV=2.47 with
# EM=2.3% (internally inconsistent) and moved ~2x its EM by 09:45.
FLAG_WIDE_SPREAD = 0.25      # any leg (ask-bid)/mid above this
FLAG_IV_DIVERGENCE = 0.30    # |call_iv - put_iv| / mean at the front ATM strike
FLAG_EXTREME_IVRV = 3.0      # iv30/rv30 above this is usually junk IV, not signal
FLAG_THIN_STRADDLE = 0.75    # $ front straddle mid below this: costs eat the edge
FLAG_EM_IV_BAND = (0.5, 2.0) # straddle-EM vs IV-implied-EM consistency band
FLAG_FAR_FRONT_DTE = 7       # front expiry further out: different (worse) trade
FLAG_MIN_ATM_OI = 100        # min(call,put) ATM open interest below this
FLAG_SMALL_MCAP = 10e9       # TODO item 2: warn (not filter) below $10B

# NYSE full-day closures — UPDATE ANNUALLY (nyse.com/markets/hours-calendars).
# Early-close half days are intentionally not modeled; no R1000 earnings land there.
NYSE_HOLIDAYS = (
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
)


class StaleQuotesError(ValueError):
    """Option quotes are zeroed/stale — usually the US market is closed."""


class PermanentDataError(ValueError):
    """Deterministic data condition for this run — retrying cannot change it."""


# --------------------------------------------------------------------------
# Core math — copied unchanged from the original backtested calculator.
# --------------------------------------------------------------------------

def filter_dates(dates, today=None):
    if today is None:
        today = datetime.now(ET).date()
    cutoff_date = today + timedelta(days=45)

    sorted_dates = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in dates)

    arr = []
    for i, d in enumerate(sorted_dates):
        if d >= cutoff_date:
            arr = [x.strftime("%Y-%m-%d") for x in sorted_dates[: i + 1]]
            break

    if len(arr) > 0:
        if arr[0] == today.strftime("%Y-%m-%d"):
            return arr[1:]
        return arr

    raise PermanentDataError("No expiration 45+ days out.")


def yang_zhang(price_data, window=30, trading_periods=252, return_last_only=True):
    log_ho = (price_data["High"] / price_data["Open"]).apply(np.log)
    log_lo = (price_data["Low"] / price_data["Open"]).apply(np.log)
    log_co = (price_data["Close"] / price_data["Open"]).apply(np.log)

    log_oc = (price_data["Open"] / price_data["Close"].shift(1)).apply(np.log)
    log_oc_sq = log_oc ** 2

    log_cc = (price_data["Close"] / price_data["Close"].shift(1)).apply(np.log)
    log_cc_sq = log_cc ** 2

    rs = log_ho * (log_ho - log_co) + log_lo * (log_lo - log_co)

    close_vol = log_cc_sq.rolling(window=window, center=False).sum() * (
        1.0 / (window - 1.0)
    )
    open_vol = log_oc_sq.rolling(window=window, center=False).sum() * (
        1.0 / (window - 1.0)
    )
    window_rs = rs.rolling(window=window, center=False).sum() * (1.0 / (window - 1.0))

    k = 0.34 / (1.34 + ((window + 1) / (window - 1)))
    result = (open_vol + k * close_vol + (1 - k) * window_rs).apply(np.sqrt) * np.sqrt(
        trading_periods
    )

    if return_last_only:
        return result.iloc[-1]
    return result.dropna()


def build_term_structure(days, ivs):
    days = np.array(days)
    ivs = np.array(ivs)

    sort_idx = days.argsort()
    days = days[sort_idx]
    ivs = ivs[sort_idx]

    spline = interp1d(days, ivs, kind="linear", fill_value="extrapolate")

    def term_spline(dte):
        if dte < days[0]:
            return ivs[0]
        elif dte > days[-1]:
            return ivs[-1]
        return float(spline(dte))

    return term_spline


def _atm_leg_stats(chain, underlying_price):
    """Bid/ask/IV/OI of the ATM call+put of one chain (no extra HTTP)."""
    calls, puts = chain.calls, chain.puts
    if calls.empty or puts.empty:
        return None

    def leg(df):
        idx = (df["strike"] - underlying_price).abs().idxmin()
        bid = float(df.loc[idx, "bid"]) if pd.notna(df.loc[idx, "bid"]) else 0.0
        ask = float(df.loc[idx, "ask"]) if pd.notna(df.loc[idx, "ask"]) else 0.0
        mid = (bid + ask) / 2.0
        iv = df.loc[idx, "impliedVolatility"]
        oi = df.loc[idx, "openInterest"]
        return {
            "bid": bid, "ask": ask,
            "spread_pct": (ask - bid) / mid if bid > 0 and mid > 0 else None,
            "iv": float(iv) if pd.notna(iv) else None,
            "oi": int(oi) if pd.notna(oi) else 0,
        }

    return {"call": leg(calls), "put": leg(puts)}


def quality_flags(chains, atm_iv, dte_map, underlying_price, iv30, rv30, straddle):
    """Advisory diagnostics: measure everything now, gate on nothing yet.

    Returns (flags_str, extra_metrics). Tier is never touched — thresholds
    are provisional until calibrated from the replay log (TODO)."""
    exps = list(atm_iv)
    front_exp = exps[0]
    front = _atm_leg_stats(chains[front_exp], underlying_price)

    # The calendar's back leg: expiry closest to front+30 DTE (within +20..+45).
    front_dte = dte_map[front_exp]
    in_band = [e for e in exps[1:] if front_dte + 20 <= dte_map[e] <= front_dte + 45]
    back_exp = min(in_band, key=lambda e: abs(dte_map[e] - (front_dte + 30)), default=None)
    back = _atm_leg_stats(chains[back_exp], underlying_price) if back_exp else None

    flags = []
    legs = [front["call"], front["put"]] + ([back["call"], back["put"]] if back else [])
    if any(l["bid"] <= 0 or l["ask"] <= 0 for l in (front["call"], front["put"])):
        flags.append("ZERO_BID")
    spreads = [l["spread_pct"] for l in legs if l["spread_pct"] is not None]
    max_spread = max(spreads) if spreads else None
    if max_spread is not None and max_spread > FLAG_WIDE_SPREAD:
        flags.append("WIDE_SPREAD")

    civ, piv = front["call"]["iv"], front["put"]["iv"]
    iv_div = abs(civ - piv) / ((civ + piv) / 2) if civ and piv else None
    if iv_div is not None and iv_div > FLAG_IV_DIVERGENCE:
        flags.append("IV_DIVERGENCE")

    if rv30 and iv30 / rv30 > FLAG_EXTREME_IVRV:
        flags.append("EXTREME_IVRV")
    if straddle is not None and straddle < FLAG_THIN_STRADDLE:
        flags.append("THIN_PREMIUM")

    # Straddle-implied move vs IV-implied move (~0.8·IV·sqrt(T)): gross
    # disagreement means one of the two inputs is junk (the HOMB pattern).
    em_iv_pct = 0.8 * atm_iv[front_exp] * np.sqrt(front_dte / 365.0) * 100
    if straddle is not None and em_iv_pct > 0:
        ratio = (straddle / underlying_price * 100) / em_iv_pct
        lo, hi = FLAG_EM_IV_BAND
        if not (lo <= ratio <= hi):
            flags.append("EM_IV_MISMATCH")

    if front_dte > FLAG_FAR_FRONT_DTE:
        flags.append("FAR_FRONT")
    if back_exp is None:
        flags.append("NO_BACK_LEG")
    oi_min = min(front["call"]["oi"], front["put"]["oi"])
    if oi_min < FLAG_MIN_ATM_OI:
        flags.append("LOW_OI")

    return "|".join(flags), {
        "front_spread_pct": None if max_spread is None else float(max_spread),
        "iv_pc_div": None if iv_div is None else float(iv_div),
        "em_iv_pct": float(em_iv_pct),
        "back_dte": dte_map.get(back_exp),
        "atm_oi_min": oi_min,
    }


def compute_metrics(symbol):
    """Run the original screening math on one ticker; return raw metrics."""
    stock = yf.Ticker(symbol)

    # One 3mo fetch serves price, rv30 and volume (the old extra
    # history(period='1d') call returned a strict subset of this).
    price_history = stock.history(period="3mo")
    if price_history.empty:
        raise ValueError("no market price")
    underlying_price = price_history["Close"].iloc[-1]

    exp_dates = list(stock.options)
    if not exp_dates:
        raise ValueError("no listed options")

    exp_dates = filter_dates(exp_dates)

    chains = {d: stock.option_chain(d) for d in exp_dates}

    atm_iv = {}
    straddle = None
    for i, (exp_date, chain) in enumerate(chains.items()):
        calls, puts = chain.calls, chain.puts
        if calls.empty or puts.empty:
            if i == 0:
                # An empty front chain would silently skip the stale-quote
                # guard below AND mis-anchor front_dte/slope to the 2nd expiry.
                # Usually a transient yfinance glitch — raise so the retry
                # loop re-fetches instead of computing metrics off-anchor.
                raise ValueError("front-month option chain came back empty")
            continue

        call_idx = (calls["strike"] - underlying_price).abs().idxmin()
        put_idx = (puts["strike"] - underlying_price).abs().idxmin()
        call_iv = calls.loc[call_idx, "impliedVolatility"]
        put_iv = puts.loc[put_idx, "impliedVolatility"]
        atm_iv[exp_date] = (call_iv + put_iv) / 2.0

        if i == 0:
            quotes = [
                calls.loc[call_idx, "bid"], calls.loc[call_idx, "ask"],
                puts.loc[put_idx, "bid"], puts.loc[put_idx, "ask"],
            ]
            if all(pd.isna(q) or q == 0 for q in quotes):
                raise StaleQuotesError(
                    "ATM bid/ask all zero — US market closed or illiquid chain"
                )
            # A zero bid with a live ask makes mid = ask/2 — a fictional
            # straddle that inflates expected_move (the WIT/FNB failure mode).
            # Only trust the mid when every leg has a real two-sided quote.
            if all(pd.notna(q) and q > 0 for q in quotes):
                call_mid = (quotes[0] + quotes[1]) / 2.0
                put_mid = (quotes[2] + quotes[3]) / 2.0
                straddle = call_mid + put_mid

    if not atm_iv:
        raise ValueError("could not determine ATM IV")
    if len(atm_iv) < 2:
        raise ValueError("only one usable expiry — cannot build a term structure")

    today = datetime.now(ET).date()
    dtes, ivs, dte_map = [], [], {}
    for exp_date, iv in atm_iv.items():
        dte = (datetime.strptime(exp_date, "%Y-%m-%d").date() - today).days
        dtes.append(dte)
        ivs.append(iv)
        dte_map[exp_date] = dte

    term_spline = build_term_structure(dtes, ivs)
    if dtes[0] >= 45:
        raise PermanentDataError("front expiry already 45+ DTE")
    ts_slope_0_45 = (term_spline(45) - term_spline(dtes[0])) / (45 - dtes[0])

    rv30 = yang_zhang(price_history)
    iv30 = term_spline(30)
    if iv30 < STALE_IV_FLOOR:
        raise StaleQuotesError(
            f"iv30={iv30:.4f} implausibly low — stale quotes (market closed?)"
        )
    # NaN IVs (illiquid strikes) or NaN rv30 would otherwise compare False
    # against every threshold and come out as a confident AVOID.
    if not np.isfinite([float(iv30), float(rv30), float(ts_slope_0_45)]).all():
        raise ValueError(
            f"non-finite metrics (iv30={iv30}, rv30={rv30}, slope={ts_slope_0_45})"
        )
    vol_ma = price_history["Volume"].rolling(30).mean().dropna()
    if vol_ma.empty:
        raise PermanentDataError("insufficient price history (<30 trading days)")
    avg_volume = vol_ma.iloc[-1]

    flags, diag = quality_flags(
        chains, atm_iv, dte_map, underlying_price, float(iv30), float(rv30), straddle
    )

    # Raw floats — classify() must see unrounded values (rounding first would
    # widen the backtested thresholds); rounding happens at display time.
    return {
        "price": round(float(underlying_price), 2),
        "avg_volume_30d": int(avg_volume),
        "iv30": float(iv30),
        "rv30": float(rv30),
        "iv30_rv30": float(iv30 / rv30),
        "ts_slope_0_45": float(ts_slope_0_45),
        "expected_move_pct": float(straddle / underlying_price * 100)
        if straddle
        else None,
        "front_dte": dtes[0],
        "flags": flags,
        **diag,
    }


def round_metrics(m):
    """Display/CSV copy of a raw metrics dict."""
    def opt(key, nd):
        v = m.get(key)
        return None if v is None else round(v, nd)

    return dict(
        m,
        iv30=round(m["iv30"], 4),
        rv30=round(m["rv30"], 4),
        iv30_rv30=round(m["iv30_rv30"], 3),
        ts_slope_0_45=round(m["ts_slope_0_45"], 5),
        # classify() 判的是生值,而展示值被 round 到 3 位。只留展示值的话,
        # 生值 1.2496(判 CONSIDER)会存成 1.25,渲染出「1.25,差 0 到 1.25」
        # 这种自相矛盾的输出。原值单独留一列给 consider_reason() 用。
        iv30_rv30_raw=float(m["iv30_rv30"]),
        expected_move_pct=opt("expected_move_pct", 2),
        front_spread_pct=opt("front_spread_pct", 3),
        iv_pc_div=opt("iv_pc_div", 3),
        em_iv_pct=opt("em_iv_pct", 2),
    )


def classify(m):
    vol_ok = m["avg_volume_30d"] >= MIN_AVG_VOLUME
    ivrv_ok = m["iv30_rv30"] >= MIN_IV30_RV30
    slope_ok = m["ts_slope_0_45"] <= MAX_TS_SLOPE

    if vol_ok and ivrv_ok and slope_ok:
        tier = "RECOMMENDED"
    elif slope_ok and (vol_ok != ivrv_ok):
        tier = "CONSIDER"
    else:
        tier = "AVOID"

    return tier, vol_ok, ivrv_ok, slope_ok


# --------------------------------------------------------------------------
# Universe: Russell 1000 constituents (Wikipedia, iShares IWB fallback).
# --------------------------------------------------------------------------

def _fetch_russell1000_wikipedia():
    resp = requests.get(WIKI_URL, headers=HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    for table in pd.read_html(StringIO(resp.text)):
        cols = {str(c).strip().lower() for c in table.columns}
        if "symbol" in cols and len(table) > 500:
            sym_col = next(c for c in table.columns if str(c).strip().lower() == "symbol")
            return sorted(table[sym_col].dropna().astype(str).str.strip().str.upper())
    raise ValueError("components table not found on Wikipedia")


def _fetch_russell1000_ishares():
    resp = requests.get(ISHARES_IWB_URL, headers=HTTP_HEADERS, timeout=60)
    resp.raise_for_status()
    lines = resp.text.splitlines()
    header_idx = next(i for i, l in enumerate(lines) if l.startswith("Ticker,"))
    df = pd.read_csv(StringIO("\n".join(lines[header_idx:])))
    # df.get("Asset Class", default) returns a SCALAR when the column is
    # missing, turning the filter into df[True] -> KeyError. Explicit check:
    if "Asset Class" in df.columns:
        df = df[df["Asset Class"] == "Equity"]
    symbols = sorted(df["Ticker"].astype(str).str.strip().str.upper().unique())
    if len(symbols) < 500:
        raise ValueError(f"iShares CSV looks wrong ({len(symbols)} tickers)")
    return symbols


def _read_universe_cache():
    """(as_of, symbols) from the cache, or (None, None) if missing/corrupt."""
    try:
        cached = json.loads(UNIVERSE_CACHE.read_text())
        return cached["as_of"], cached["symbols"]
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def get_russell1000(refresh=False):
    if not refresh:
        as_of, symbols = _read_universe_cache()
        if as_of and symbols:
            age = (date.today() - date.fromisoformat(as_of)).days
            if age < 7:
                return symbols

    symbols, source = None, None
    for fetch, name in [
        (_fetch_russell1000_wikipedia, "wikipedia"),
        (_fetch_russell1000_ishares, "ishares-IWB"),
    ]:
        try:
            symbols, source = fetch(), name
            break
        except Exception as e:
            print(f"  universe source {name} failed: {e}", file=sys.stderr)

    if not symbols:
        _, stale = _read_universe_cache()
        if stale:
            print("  falling back to stale universe cache", file=sys.stderr)
            return stale
        raise RuntimeError("could not fetch Russell 1000 constituents")

    # Atomic write: a kill/power-loss mid-write must not leave truncated JSON
    # that would crash every subsequent run.
    tmp = UNIVERSE_CACHE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {"as_of": date.today().isoformat(), "source": source, "symbols": symbols}
        )
    )
    os.replace(tmp, UNIVERSE_CACHE)
    return symbols


def canon(symbol):
    """Canonical symbol form: BRK.B / BRK-B -> BRK-B (yfinance style)."""
    return symbol.strip().upper().replace(".", "-").replace(" ", "")


# --------------------------------------------------------------------------
# Earnings calendar (Nasdaq public API).
# --------------------------------------------------------------------------

TIMING_LABEL = {
    "time-pre-market": "BMO",
    "time-after-hours": "AMC",
    "time-not-supplied": "?",
}


def _parse_mcap(s):
    try:
        # float() first: tolerates a decimal-formatted string. A parse
        # failure returning 0 silently disables the auto-universe $5B net.
        return int(float(str(s).replace("$", "").replace(",", "")))
    except (ValueError, TypeError):
        return 0


def earnings_for_date(d, attempts=3):
    """Nasdaq calendar for one date. Raises after retries — a swallowed
    failure here would be indistinguishable from 'no earnings today' and
    turn an outage into a false no-signal email."""
    last_err = None
    for i in range(attempts):
        try:
            resp = requests.get(
                NASDAQ_EARNINGS_URL,
                params={"date": d.isoformat()},
                headers=HTTP_HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("data")
            rcode = (payload.get("status") or {}).get("rCode")
            # data:null with rCode 200 is a legitimately empty day;
            # data:null with an error rCode is Nasdaq blocking/throttling us.
            if data is None and rcode not in (None, 200):
                raise RuntimeError(f"nasdaq calendar error rCode={rcode}")
            rows = ((data or {}).get("rows")) or []
            return [
                {
                    "symbol": canon(r.get("symbol", "")),
                    "earnings_date": d,
                    "timing": TIMING_LABEL.get(r.get("time", ""), "?"),
                    "company": r.get("name", ""),
                    "market_cap": _parse_mcap(r.get("marketCap")),
                }
                for r in rows
                if r.get("symbol")
            ]
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"earnings calendar failed for {d}: {last_err}")


def prev_trading_day(d):
    return np.busday_offset(d, -1, roll="forward", holidays=NYSE_HOLIDAYS).item()


def next_trading_day(d):
    return np.busday_offset(d, 1, roll="backward", holidays=NYSE_HOLIDAYS).item()


def is_trading_day(d):
    return d.weekday() < 5 and f"{d:%Y-%m-%d}" not in NYSE_HOLIDAYS


def entry_exit(earnings_date, timing):
    """Author's rule: enter last trading day before the announcement (~15 min
    before close), exit first trading day after it (~15 min after open)."""
    if timing == "BMO":
        return prev_trading_day(earnings_date), earnings_date
    # AMC and unknown: assume after-hours (most common); verify if '?'
    return earnings_date, next_trading_day(earnings_date)


WEEKDAY_CN = "一二三四五六日"


def et_moment_to_bne(d, hm):
    """ET trading date + (hour, minute) -> Brisbane datetime (handles US DST)."""
    return datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=ET).astimezone(BNE)


def fmt_bne(dt):
    return f"{dt:%m-%d}(周{WEEKDAY_CN[dt.weekday()]}) {dt:%H:%M}"


TIER_CN = {
    "RECOMMENDED": "✅ 推荐",
    "CONSIDER": "🟡 可考虑",
    "NO_DATA": "❓ 无数据(报价延迟/盘外)",
    "AVOID": "❌ 回避",
}
TIMING_CN = {"BMO": "盘前", "AMC": "盘后", "?": "时段未知⚠"}


def _is_true(v):
    """vol_ok/ivrv_ok are real bools in-memory but strings after a CSV round-trip."""
    return str(v).strip().lower() in ("true", "1")


def _fmt_wan(x):
    """Volume in 万 (10k) — 1_779_053 -> '177.9万', 1_500_000 -> '150万'.

    A positive value under 500 shares must not render as '0万': this formats
    the *gap* to the volume gate too, and '差 0万 到 150万' reads as "already
    there" for a row that in fact failed the gate.
    """
    v = x / 10_000
    if 0 < abs(v) < 0.05:
        return ("<0.1万" if v > 0 else ">-0.1万")
    return f"{v:.0f}万" if abs(v - round(v)) < 0.05 else f"{v:.1f}万"


def consider_reason(r):
    """Which half of the CONSIDER test failed, and by how much.

    CONSIDER hides two very different situations: IV that isn't actually rich
    (the strategy's whole edge is absent — e.g. IBM at iv30/rv30 0.53) versus
    an edge that's there but sits in a thin name you may not be able to fill.
    The tier alone can't be acted on without knowing which one it is.

    The distance to the threshold matters as much as which one failed: on
    2026-08-13 NU (1.222, a hair under 1.25) and AMAT (0.69, IV *cheaper*
    than realized vol — the inverse of the setup) rendered identically as
    "🟡 可考虑 ← IV 不够贵". They are not the same situation.
    """
    if r.get("tier") != "CONSIDER" or "ivrv_ok" not in r or "vol_ok" not in r:
        return ""
    if not _is_true(r["ivrv_ok"]):
        # 生值优先:展示值 round 到 3 位后会让边界样本的差额算成 0。
        # 旧 CSV 没有这一列,回退到展示值(差额会偏小但不会崩)。
        try:
            ivrv = float(r["iv30_rv30_raw"])
        except (KeyError, TypeError, ValueError):
            try:
                ivrv = float(r["iv30_rv30"])
            except (KeyError, TypeError, ValueError):
                return "  ← IV 不够贵(策略核心边缘缺失)"
        gap = MIN_IV30_RV30 - ivrv
        # Below 1.0 is not "nearly there" — implied is cheaper than realized,
        # so the short front leg is being sold at a discount, not a premium.
        inverted = "，已反向(IV 比已实现波动还便宜)" if ivrv < 1.0 else ""
        return (f"  ← IV 不够贵(策略核心边缘缺失): {ivrv:g}，"
                f"差 {gap:.3g} 到 {MIN_IV30_RV30}{inverted}")
    if not _is_true(r["vol_ok"]):
        try:
            vol = float(r["avg_volume_30d"])
        except (KeyError, TypeError, ValueError):
            return "  ← 成交量不足(边缘在但可能难成交)"
        return (f"  ← 成交量不足(边缘在但可能难成交): {_fmt_wan(vol)}，"
                f"差 {_fmt_wan(MIN_AVG_VOLUME - vol)} 到 {_fmt_wan(MIN_AVG_VOLUME)}")
    return ""


def build_verdict(df, mode="confirm"):
    """One-line bottom line to head the signal list. '' when not needed.

    Four 🟡 可考虑 blocks read like four things to do. When every one of them
    failed on iv30/rv30, the honest summary is "nothing here" — by the
    strategy's own logic a name whose IV isn't rich has no edge to harvest,
    however inverted its term structure looks. Skimming that at 05:15 and
    reading "4 candidates" is the failure mode this prevents.

    Silent when at least one RECOMMENDED name exists: the per-name blocks
    already carry the message there.
    """
    rows = df[df["tier"].isin(["RECOMMENDED", "CONSIDER"])]
    if rows.empty or int((rows["tier"] == "RECOMMENDED").sum()):
        return ""

    n = len(rows)
    n_ivrv = sum(1 for _, r in rows.iterrows() if not _is_true(r.get("ivrv_ok", True)))
    n_vol = n - n_ivrv
    # preview is a heads-up hours before entry; only confirm is a verdict.
    tail = ("" if mode == "confirm"
            else "  (距开仓还有数小时,以开仓前的 confirm 班次为准)")

    if n_vol == 0:
        head = (f"⛔ 结论: 无符合策略的标的。下列 {n} 只全部倒在「IV 不够贵」"
                f"(iv30/rv30 < {MIN_IV30_RV30}),策略的核心边缘不存在。")
    elif n_ivrv == 0:
        head = (f"⚠ 结论: 无推荐标的。下列 {n} 只 IV 边缘尚在,但成交量不足 "
                f"{_fmt_wan(MIN_AVG_VOLUME)},可能难以成交。")
    else:
        head = (f"⚠ 结论: 无推荐标的。下列 {n} 只中,{n_ivrv} 只 IV 不够贵"
                f"(核心边缘缺失)、{n_vol} 只成交量不足。")
    return head + tail + "\n\n"


def build_cn_report(df, today_et, include_no_data=True):
    """Chinese trade-signal summary: one block per candidate worth acting on."""
    tiers = ["RECOMMENDED", "CONSIDER"] + (["NO_DATA"] if include_no_data else [])
    rows = df[df["tier"].isin(tiers)]
    if rows.empty:
        return "本次扫描没有推荐或可考虑的标的。"

    lines = []
    for _, r in rows.iterrows():
        entry_bne = et_moment_to_bne(r[COL_ENTRY], ENTRY_ET_HM)
        exit_bne = et_moment_to_bne(r[COL_EXIT], EXIT_ET_HM)
        lines.append(f"{TIER_CN.get(r['tier'], r['tier'])}  {r['symbol']}  {r['company']}"
                     f"{consider_reason(r)}")
        lines.append(
            f"  财报: {r['earnings_date']:%m-%d} {TIMING_CN.get(r['timing'], r['timing'])}"
            f"  市值 ${r['mcap_$B']}B"
        )
        entry_flag = "  ← 开仓日就是今天(ET)!" if r[COL_ENTRY] == today_et else ""
        lines.append(f"  开仓: 布里斯班 {fmt_bne(entry_bne)}{entry_flag}")
        lines.append(f"  平仓: 布里斯班 {fmt_bne(exit_bne)}")
        if r["tier"] != "NO_DATA":
            # straddle 只在四条腿都有正的双边报价时才计算(见 :328)。拿不到时
            # expected_move_pct 是 None,进 DataFrame 变 NaN,直接插值会渲染成
            # 「预期波动=nan%」—— 看着像算错了,其实是本模块拒绝用 ask/2 造价。
            em = r["expected_move_pct"]
            em_txt = ("不可用(ATM 缺双边报价,未用 ask/2 估算)"
                      if em is None or pd.isna(em) else f"{em}%")
            lines.append(
                f"  IV/RV={r['iv30_rv30']}  期限斜率={r['ts_slope_0_45']}"
                f"  预期波动={em_txt}"
            )
            flags = r.get("flags") if "flags" in r else None
            if isinstance(flags, str) and flags:
                lines.append(f"  ⚠ 数据/结构标记: {flags} (阈值为临时值,谨慎对待该信号)")
            ec = str(r.get("earnings_check", "")) if "earnings_check" in r else ""
            if ec.startswith("MISMATCH"):
                lines.append(f"  ⚠ 财报日期核对不一致({ec}),人工确认后再交易!")
            elif ec == "n/a":
                # Fails-open was invisible: ASML 2026-07-15 had a wrong yf date
                # and the email showed nothing. Surface it.
                lines.append("  ⚠ 财报日期无法二次核对(yf 无数据),下单前人工确认日期")
        else:
            lines.append("  ⚠ 无法算 IV(Yahoo 报价延迟15分钟:盘外、或开盘后约30分钟内"
                         "都会这样),以上仅为候选名单;以开仓日盘中的确认扫描为准")
        lines.append("")
    return "\n".join(lines).rstrip()


def yf_earnings_check(symbol, expected_date):
    """Cross-check the Nasdaq date against yfinance's calendar."""
    try:
        cal = yf.Ticker(symbol).calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not dates:
            return "n/a"
        if any(abs((d - expected_date).days) <= 1 for d in dates):
            return "ok"
        return "MISMATCH yf=" + ", ".join(str(d) for d in dates)
    except Exception:
        return "n/a"


# --------------------------------------------------------------------------
# Scan orchestration.
# --------------------------------------------------------------------------

# When Yahoo rate-limits, pause ALL workers (a shared deadline), not just the
# one that saw the 429 — otherwise 4 workers retry in lockstep and keep the
# block alive.
_RATE_LIMIT_LOCK = threading.Lock()
_rate_limit_until = 0.0


def _looks_rate_limited(err):
    s = str(err).lower()
    return isinstance(err, YFRateLimitError) or "429" in s or "too many requests" in s


def analyze_with_retry(symbol, attempts=3):
    global _rate_limit_until
    last_err = None
    for i in range(attempts):
        wait = _rate_limit_until - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return compute_metrics(symbol), None
        except StaleQuotesError as e:
            return None, f"NO_DATA: {e}"  # not transient — don't retry
        except PermanentDataError as e:
            return None, str(e)  # deterministic — retrying cannot change it
        except Exception as e:
            last_err = e
            if _looks_rate_limited(e):
                with _RATE_LIMIT_LOCK:
                    _rate_limit_until = max(
                        _rate_limit_until, time.monotonic() + 60 * (i + 1)
                    )
            elif i < attempts - 1:
                time.sleep(2 * (i + 1) + random.random())
    return None, str(last_err)


def analyze_candidate(c, verify):
    """Worker task: metrics, plus the earnings-date cross-check — but only
    for actionable tiers (RECOMMENDED/CONSIDER); running it serially on the
    main thread for every AVOID row was pure wall-clock waste."""
    metrics, err = analyze_with_retry(c["symbol"])
    check = None
    if verify and metrics is not None:
        tier, *_ = classify(metrics)
        if tier in ("RECOMMENDED", "CONSIDER"):
            check = yf_earnings_check(c["symbol"], c["earnings_date"])
    return metrics, err, check


def market_hours_warning():
    now = datetime.now(ET)
    # Open side starts at 10:00: Yahoo's delayed feed keeps serving the zeroed
    # pre-open snapshot for ~30 min after the open (verified 2026-07-15).
    is_rth = is_trading_day(now.date()) and (
        now.replace(hour=10, minute=0) <= now <= now.replace(hour=16, minute=15)
    )
    if not is_rth:
        print(
            "\n*** WARNING: US market is closed right now — Yahoo option quotes "
            "are stale/zeroed, so IV screening will mostly return NO_DATA.\n"
            "*** Run during US regular trading hours (Brisbane: ~23:30-06:00 "
            "during US DST), ideally near the close for entry-day signals.\n"
        )


def build_universe_filter(mode, min_mcap, refresh):
    """Return (predicate, description) deciding which reporters to analyze.

    auto  : Wikipedia Russell 1000 list, PLUS any reporter with market cap >=
            min_mcap (catches names the community list is missing/stale on).
    wiki  : strict Wikipedia Russell 1000 list.
    all   : every reporter in the earnings calendar.
    <path>: file with one symbol per line, or a CSV with a Symbol/Ticker column.
    """
    if mode == "all":
        return (lambda r: True), "all reporters"

    if mode in ("auto", "wiki"):
        members = {canon(s) for s in get_russell1000(refresh=refresh)}
        if mode == "wiki":
            return (lambda r: r["symbol"] in members), (
                f"Wikipedia Russell 1000 ({len(members)} symbols)"
            )
        return (
            lambda r: r["symbol"] in members or r["market_cap"] >= min_mcap
        ), (
            f"Wikipedia Russell 1000 ({len(members)}) + market cap >= "
            f"${min_mcap/1e9:.0f}B"
        )

    path = Path(mode)
    text = path.read_text()
    if "," in text.splitlines()[0]:
        df = pd.read_csv(path)
        col = next(c for c in df.columns if str(c).lower() in ("symbol", "ticker"))
        symbols = {canon(s) for s in df[col].dropna().astype(str)}
    else:
        symbols = {canon(l) for l in text.splitlines() if l.strip()}
    return (lambda r: r["symbol"] in symbols), f"{path.name} ({len(symbols)} symbols)"


def scan(min_days, max_days, workers, verify, refresh_universe,
         universe_mode="auto", min_mcap=5_000_000_000,
         entry_on=None, min_entry=None):
    """entry_on/min_entry: skip candidates whose ENTRY date doesn't match
    BEFORE the expensive per-symbol analysis — entry is computable from the
    calendar row alone, and notify.py discards mismatching rows anyway."""
    today_et = datetime.now(ET).date()
    print(f"Scan date (US/Eastern): {today_et}")
    market_hours_warning()

    keep, universe_desc = build_universe_filter(
        universe_mode, min_mcap, refresh_universe
    )
    print(f"Universe: {universe_desc}")

    scan_dates = [
        today_et + timedelta(days=n)
        for n in range(min_days, max_days + 1)
        if is_trading_day(today_et + timedelta(days=n))
    ]
    print(f"Earnings window: {', '.join(str(d) for d in scan_dates)}")

    candidates = []
    for d in scan_dates:
        # Propagates after retries: a swallowed calendar failure would look
        # exactly like a quiet day and notify.py would email 'no signal'.
        rows = earnings_for_date(d)
        in_universe = []
        for r in rows:
            if not keep(r):
                continue
            entry_d, exit_d = entry_exit(r["earnings_date"], r["timing"])
            if entry_on is not None and entry_d != entry_on:
                continue
            if min_entry is not None and entry_d < min_entry:
                continue
            r["entry_date"], r["exit_date"] = entry_d, exit_d
            in_universe.append(r)
        print(f"  {d}: {len(rows)} reporting, {len(in_universe)} to analyze")
        candidates.extend(in_universe)

    if not candidates:
        print("\nNo Russell 1000 earnings in the window. Nothing to do.")
        return None

    print(f"\nAnalyzing {len(candidates)} candidates "
          f"({workers} workers, thresholds: vol>={MIN_AVG_VOLUME:,}, "
          f"iv30/rv30>={MIN_IV30_RV30}, slope<={MAX_TS_SLOPE})...")

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(analyze_candidate, c, verify): c for c in candidates
        }
        for fut in as_completed(futures):
            c = futures[fut]
            metrics, err, check = fut.result()
            entry_d, exit_d = c["entry_date"], c["exit_date"]
            row = {
                "symbol": c["symbol"],
                "earnings_date": c["earnings_date"],
                "timing": c["timing"],
                COL_ENTRY: entry_d,
                COL_EXIT: exit_d,
                "开仓(布里斯班)": fmt_bne(et_moment_to_bne(entry_d, ENTRY_ET_HM)),
                "平仓(布里斯班)": fmt_bne(et_moment_to_bne(exit_d, EXIT_ET_HM)),
                "company": c["company"][:28],
                "mcap_$B": round(c["market_cap"] / 1e9, 1),
            }
            if err and err.startswith("NO_DATA"):
                row["tier"] = "NO_DATA"
                results.append(row)
                print(f"  {c['symbol']:<6} NO_DATA ({err.split(': ', 1)[1]})")
                continue
            if err:
                print(f"  {c['symbol']:<6} skipped: {err}")
                continue
            tier, vol_ok, ivrv_ok, slope_ok = classify(metrics)
            disp = round_metrics(metrics)
            if 0 < c["market_cap"] < FLAG_SMALL_MCAP:
                disp["flags"] = "|".join(
                    f for f in (disp.get("flags", ""), "SMALL_CAP") if f
                )
            row.update(
                tier=tier, **disp,
                vol_ok=vol_ok, ivrv_ok=ivrv_ok, slope_ok=slope_ok,
            )
            if check is not None:
                row["earnings_check"] = check
            results.append(row)
            _em = disp["expected_move_pct"]
            print(f"  {c['symbol']:<6} {tier:<11} iv/rv={disp['iv30_rv30']:<6} "
                  f"slope={disp['ts_slope_0_45']:<9} "
                  f"em={'n/a' if _em is None else f'{_em}%'}")

    if not results:
        print("\nAll candidates failed analysis.")
        return None

    tier_order = {"RECOMMENDED": 0, "CONSIDER": 1, "NO_DATA": 2, "AVOID": 3}
    df = pd.DataFrame(results)
    df["_o"] = df["tier"].map(tier_order)
    sort_cols = ["_o"] + (["iv30_rv30"] if "iv30_rv30" in df.columns else [])
    df = (
        df.sort_values(sort_cols, ascending=[True, False][: len(sort_cols)],
                       na_position="last")
        .drop(columns="_o")
        .reset_index(drop=True)
    )
    lead = ["tier", "symbol", "earnings_date", "timing",
            "开仓(布里斯班)", "平仓(布里斯班)"]
    df = df[lead + [c for c in df.columns if c not in lead]]
    return df


def main():
    # Windows redirects default to the ANSI codepage — force UTF-8 for Chinese/emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="Russell 1000 earnings IV-crush scanner")
    ap.add_argument("--min-days", type=int, default=1,
                    help="earliest earnings date, days from today ET (default 1)")
    ap.add_argument("--max-days", type=int, default=3,
                    help="latest earnings date, days from today ET (default 3)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-verify", action="store_true",
                    help="skip cross-checking earnings dates against yfinance")
    ap.add_argument("--refresh-universe", action="store_true")
    ap.add_argument("--universe", default="auto",
                    help="auto (default: Wikipedia R1000 + mcap>=$5B), wiki "
                         "(strict list), all, or a path to a symbols file")
    ap.add_argument("--min-mcap", type=float, default=5e9,
                    help="market-cap floor for 'auto' universe (default 5e9)")
    ap.add_argument("--tickers",
                    help="comma-separated tickers to check directly, bypassing "
                         "the universe/earnings filter (like the original GUI)")
    args = ap.parse_args()

    if args.tickers:
        for sym in [canon(s) for s in args.tickers.split(",") if s.strip()]:
            metrics, err = analyze_with_retry(sym)
            if err:
                print(f"{sym}: ERROR {err}")
                continue
            tier, *_ = classify(metrics)
            print(f"{sym}: {tier}  {round_metrics(metrics)}")
        return

    df = scan(args.min_days, args.max_days, args.workers,
              verify=not args.no_verify, refresh_universe=args.refresh_universe,
              universe_mode=args.universe, min_mcap=args.min_mcap)
    if df is None:
        return

    print("\n" + "=" * 100)
    print(df.to_string(index=False))
    print("=" * 100)

    today_et = datetime.now(ET).date()
    print("\n========== 中文信号摘要 ==========")
    print(build_cn_report(df, today_et))
    print("==================================")

    SCAN_DIR.mkdir(exist_ok=True)
    out = SCAN_DIR / f"scan_{datetime.now(ET):%Y-%m-%d_%H%M}ET.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n已保存: {out}")
    print("\n提醒: IV 指标盘中会变——开仓日临近美股收盘时(布里斯班早上 ~05:15)"
          "重跑确认后再下单。")


if __name__ == "__main__":
    main()
