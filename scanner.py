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
import random
import sys
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

ET = ZoneInfo("America/New_York")
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


class StaleQuotesError(ValueError):
    """Option quotes are zeroed/stale — usually the US market is closed."""


# --------------------------------------------------------------------------
# Core math — copied unchanged from the original backtested calculator.
# --------------------------------------------------------------------------

def filter_dates(dates):
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

    raise ValueError("No expiration 45+ days out.")


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


def compute_metrics(symbol):
    """Run the original screening math on one ticker; return raw metrics."""
    stock = yf.Ticker(symbol)
    exp_dates = list(stock.options)
    if not exp_dates:
        raise ValueError("no listed options")

    exp_dates = filter_dates(exp_dates)

    chains = {d: stock.option_chain(d) for d in exp_dates}

    hist_1d = stock.history(period="1d")
    if hist_1d.empty:
        raise ValueError("no market price")
    underlying_price = hist_1d["Close"].iloc[-1]

    atm_iv = {}
    straddle = None
    for i, (exp_date, chain) in enumerate(chains.items()):
        calls, puts = chain.calls, chain.puts
        if calls.empty or puts.empty:
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
            call_mid = (quotes[0] + quotes[1]) / 2.0
            put_mid = (quotes[2] + quotes[3]) / 2.0
            if pd.notna(call_mid) and pd.notna(put_mid):
                straddle = call_mid + put_mid

    if not atm_iv:
        raise ValueError("could not determine ATM IV")

    today = datetime.now(ET).date()
    dtes, ivs = [], []
    for exp_date, iv in atm_iv.items():
        dtes.append((datetime.strptime(exp_date, "%Y-%m-%d").date() - today).days)
        ivs.append(iv)

    term_spline = build_term_structure(dtes, ivs)
    if dtes[0] >= 45:
        raise ValueError("front expiry already 45+ DTE")
    ts_slope_0_45 = (term_spline(45) - term_spline(dtes[0])) / (45 - dtes[0])

    price_history = stock.history(period="3mo")
    rv30 = yang_zhang(price_history)
    iv30 = term_spline(30)
    if iv30 < STALE_IV_FLOOR:
        raise StaleQuotesError(
            f"iv30={iv30:.4f} implausibly low — stale quotes (market closed?)"
        )
    avg_volume = price_history["Volume"].rolling(30).mean().dropna().iloc[-1]

    return {
        "price": round(float(underlying_price), 2),
        "avg_volume_30d": int(avg_volume),
        "iv30": round(float(iv30), 4),
        "rv30": round(float(rv30), 4),
        "iv30_rv30": round(float(iv30 / rv30), 3),
        "ts_slope_0_45": round(float(ts_slope_0_45), 5),
        "expected_move_pct": round(float(straddle / underlying_price * 100), 2)
        if straddle
        else None,
        "front_dte": dtes[0],
    }


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
    df = df[df.get("Asset Class", "Equity") == "Equity"]
    return sorted(df["Ticker"].astype(str).str.strip().str.upper().unique())


def get_russell1000(refresh=False):
    if UNIVERSE_CACHE.exists() and not refresh:
        cached = json.loads(UNIVERSE_CACHE.read_text())
        age = (date.today() - date.fromisoformat(cached["as_of"])).days
        if age < 7:
            return cached["symbols"]

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
        if UNIVERSE_CACHE.exists():
            print("  falling back to stale universe cache", file=sys.stderr)
            return json.loads(UNIVERSE_CACHE.read_text())["symbols"]
        raise RuntimeError("could not fetch Russell 1000 constituents")

    UNIVERSE_CACHE.write_text(
        json.dumps(
            {"as_of": date.today().isoformat(), "source": source, "symbols": symbols}
        )
    )
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
        return int(str(s).replace("$", "").replace(",", ""))
    except (ValueError, TypeError):
        return 0


def earnings_for_date(d):
    resp = requests.get(
        NASDAQ_EARNINGS_URL,
        params={"date": d.isoformat()},
        headers=HTTP_HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    rows = ((resp.json().get("data") or {}).get("rows")) or []
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


def prev_trading_day(d):
    return np.busday_offset(d, -1, roll="forward").item()


def next_trading_day(d):
    return np.busday_offset(d, 1, roll="backward").item()


def entry_exit(earnings_date, timing):
    """Author's rule: enter last trading day before the announcement (~15 min
    before close), exit first trading day after it (~15 min after open)."""
    if timing == "BMO":
        return prev_trading_day(earnings_date), earnings_date
    # AMC and unknown: assume after-hours (most common); verify if '?'
    return earnings_date, next_trading_day(earnings_date)


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

def analyze_with_retry(symbol, attempts=3):
    last_err = None
    for i in range(attempts):
        try:
            return compute_metrics(symbol), None
        except StaleQuotesError as e:
            return None, f"NO_DATA: {e}"  # not transient — don't retry
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(2 * (i + 1) + random.random())
    return None, str(last_err)


def market_hours_warning():
    now = datetime.now(ET)
    is_rth = now.weekday() < 5 and (
        now.replace(hour=9, minute=30) <= now <= now.replace(hour=16, minute=15)
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
         universe_mode="auto", min_mcap=5_000_000_000):
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
        if (today_et + timedelta(days=n)).weekday() < 5
    ]
    print(f"Earnings window: {', '.join(str(d) for d in scan_dates)}")

    candidates = []
    for d in scan_dates:
        try:
            rows = earnings_for_date(d)
        except Exception as e:
            print(f"  WARNING: earnings calendar failed for {d}: {e}", file=sys.stderr)
            continue
        in_universe = [r for r in rows if keep(r)]
        print(f"  {d}: {len(rows)} reporting, {len(in_universe)} in universe")
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
            pool.submit(analyze_with_retry, c["symbol"]): c for c in candidates
        }
        for fut in as_completed(futures):
            c = futures[fut]
            metrics, err = fut.result()
            entry_d, exit_d = entry_exit(c["earnings_date"], c["timing"])
            row = {
                "symbol": c["symbol"],
                "earnings_date": c["earnings_date"],
                "timing": c["timing"],
                "entry (close-15m)": entry_d,
                "exit (open+15m)": exit_d,
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
            row.update(
                tier=tier, **metrics,
                vol_ok=vol_ok, ivrv_ok=ivrv_ok, slope_ok=slope_ok,
            )
            if verify:
                row["earnings_check"] = yf_earnings_check(
                    c["symbol"], c["earnings_date"]
                )
            results.append(row)
            print(f"  {c['symbol']:<6} {tier:<11} iv/rv={metrics['iv30_rv30']:<6} "
                  f"slope={metrics['ts_slope_0_45']:<9} em={metrics['expected_move_pct']}%")

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
            "entry (close-15m)", "exit (open+15m)"]
    df = df[lead + [c for c in df.columns if c not in lead]]
    return df


def main():
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
            print(f"{sym}: {tier}  {metrics}")
        return

    df = scan(args.min_days, args.max_days, args.workers,
              verify=not args.no_verify, refresh_universe=args.refresh_universe,
              universe_mode=args.universe, min_mcap=args.min_mcap)
    if df is None:
        return

    print("\n" + "=" * 100)
    print(df.to_string(index=False))
    print("=" * 100)

    SCAN_DIR.mkdir(exist_ok=True)
    out = SCAN_DIR / f"scan_{datetime.now(ET):%Y-%m-%d_%H%M}ET.csv"
    df.to_csv(out, index=False)
    print(f"\nSaved: {out}")
    print("\nReminder: IV metrics move intraday — re-run near the US close on the "
          "entry day to confirm signals before placing the trade.")


if __name__ == "__main__":
    main()
