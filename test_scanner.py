#!/usr/bin/env python3
"""Unit tests for the pure functions in scanner.py — no network needed.

Run with pytest (`python -m pytest test_scanner.py`) or standalone
(`python test_scanner.py`).

These guard the two places where a silent regression costs real money:
classify() (tier decisions vs the backtested thresholds) and
entry_exit()/trading-day math (a wrong date means opening a position
AFTER the announcement).
"""
from datetime import date

import pandas as pd

from scanner import (
    COL_ENTRY, COL_EXIT, PermanentDataError, _fmt_wan, _parse_mcap,
    build_cn_report, build_term_structure, build_verdict, canon, classify,
    consider_reason, entry_exit, filter_dates, next_trading_day,
    prev_trading_day, round_metrics,
)


def m(vol=2_000_000, ivrv=1.5, slope=-0.01):
    return {"avg_volume_30d": vol, "iv30_rv30": ivrv, "ts_slope_0_45": slope}


# -- classify ---------------------------------------------------------------

def test_classify_all_pass():
    assert classify(m())[0] == "RECOMMENDED"


def test_classify_exact_thresholds_pass():
    assert classify(m(vol=1_500_000, ivrv=1.25, slope=-0.00406))[0] == "RECOMMENDED"


def test_classify_consider_slope_plus_vol():
    assert classify(m(ivrv=1.0))[0] == "CONSIDER"


def test_classify_consider_slope_plus_ivrv():
    assert classify(m(vol=1))[0] == "CONSIDER"


def test_classify_avoid_slope_only():
    assert classify(m(vol=1, ivrv=1.0))[0] == "AVOID"


def test_classify_avoid_without_slope():
    assert classify(m(slope=0.0))[0] == "AVOID"


def test_classify_sees_raw_not_rounded():
    # 1.2496 must FAIL the 1.25 gate: rounding to 3 dp before comparing
    # would let it pass (the pre-review behavior).
    assert classify(m(ivrv=1.2496))[0] == "CONSIDER"
    assert classify(m(slope=-0.0040551))[0] == "AVOID"


# -- entry/exit dates -------------------------------------------------------

def test_entry_exit_bmo():
    assert entry_exit(date(2026, 7, 17), "BMO") == (date(2026, 7, 16), date(2026, 7, 17))


def test_entry_exit_amc_friday_exits_monday():
    assert entry_exit(date(2026, 7, 17), "AMC") == (date(2026, 7, 17), date(2026, 7, 20))


def test_entry_exit_unknown_timing_behaves_like_amc():
    assert entry_exit(date(2026, 7, 15), "?") == entry_exit(date(2026, 7, 15), "AMC")


def test_entry_exit_monday_bmo_enters_friday():
    assert entry_exit(date(2026, 7, 20), "BMO")[0] == date(2026, 7, 17)


def test_trading_days_skip_nyse_holidays():
    # 2026-01-19 is MLK Monday; 2026-09-07 is Labor Day.
    assert prev_trading_day(date(2026, 1, 20)) == date(2026, 1, 16)
    assert next_trading_day(date(2026, 9, 4)) == date(2026, 9, 8)
    # Friday-AMC before a holiday Monday must exit Tuesday, not the holiday.
    assert entry_exit(date(2026, 9, 4), "AMC") == (date(2026, 9, 4), date(2026, 9, 8))


# -- filter_dates -----------------------------------------------------------

def test_filter_dates_strips_today_and_cuts_at_first_45d():
    today = date(2026, 7, 1)  # cutoff = 2026-08-15
    dates = ["2026-07-01", "2026-07-10", "2026-08-14", "2026-08-21", "2026-09-18"]
    assert filter_dates(dates, today=today) == ["2026-07-10", "2026-08-14", "2026-08-21"]


def test_filter_dates_no_45d_expiry_raises_permanent():
    try:
        filter_dates(["2026-07-10"], today=date(2026, 7, 1))
        raise AssertionError("expected PermanentDataError")
    except PermanentDataError:
        pass


# -- small helpers ----------------------------------------------------------

def test_canon():
    assert canon("brk.b") == "BRK-B"
    assert canon(" BF.B ") == "BF-B"


def test_parse_mcap():
    assert _parse_mcap("$1,234,567") == 1_234_567
    assert _parse_mcap("$4,001,900,565.00") == 4_001_900_565
    assert _parse_mcap("N/A") == 0
    assert _parse_mcap(None) == 0


def test_term_structure_clamps_outside_knots():
    spline = build_term_structure([10, 45], [0.6, 0.4])
    assert spline(5) == 0.6
    assert spline(60) == 0.4
    assert abs(spline(27.5) - 0.5) < 1e-9


# -- consider_reason / build_verdict ----------------------------------------

def _con(ivrv=1.0, vol=2_000_000, ivrv_ok=False, vol_ok=True, tier="CONSIDER"):
    return {"tier": tier, "iv30_rv30": ivrv, "avg_volume_30d": vol,
            "ivrv_ok": ivrv_ok, "vol_ok": vol_ok}


def test_consider_reason_shows_distance_to_threshold():
    # NU on 2026-08-13: a hair under the gate.
    assert "差 0.028 到 1.25" in consider_reason(_con(ivrv=1.222))


def test_consider_reason_uses_raw_ratio_not_display_value():
    # classify() judges the raw ratio but the DataFrame carries it rounded to
    # 3dp, so a row that failed at 1.2496 stored 1.25 and rendered
    # "1.25，差 0 到 1.25" — a gap of zero on a row that did not pass.
    row = _con(ivrv=1.25)          # what round_metrics() stored
    row["iv30_rv30_raw"] = 1.2496  # what classify() actually saw
    out = consider_reason(row)
    assert "差 0" not in out.replace("差 0.0004", ""), out
    assert "差 0.0004 到 1.25" in out, out


def test_consider_reason_falls_back_when_raw_column_absent():
    # Rows replayed from a CSV written before iv30_rv30_raw existed.
    assert "差 0.028 到 1.25" in consider_reason(_con(ivrv=1.222))


def test_round_metrics_keeps_the_raw_ratio():
    m = {"iv30": 0.5, "rv30": 0.4, "iv30_rv30": 1.2496, "ts_slope_0_45": -0.01}
    out = round_metrics(m)
    assert out["iv30_rv30"] == 1.25 and out["iv30_rv30_raw"] == 1.2496


def test_fmt_wan_never_zeroes_a_positive_gap():
    # A gap under 500 shares used to print "0万", so a row that failed the
    # volume gate read as "差 0万 到 150万" — i.e. already there.
    assert _fmt_wan(300) == "<0.1万"
    assert _fmt_wan(0) == "0万"
    assert _fmt_wan(1_500_000) == "150万"
    assert _fmt_wan(1_779_053) == "177.9万"


def test_consider_reason_volume_gap_stays_nonzero_at_the_boundary():
    out = consider_reason(_con(vol=1_499_700, ivrv_ok=True, vol_ok=False))
    assert "差 <0.1万 到 150万" in out, out


def _report_row(**kw):
    row = {"tier": "RECOMMENDED", "symbol": "YMM", "company": "Full Truck",
           "earnings_date": date(2026, 8, 19), "timing": "BMO", "mcap_$B": 9.2,
           COL_ENTRY: date(2026, 8, 18), COL_EXIT: date(2026, 8, 19),
           "iv30_rv30": 1.965, "ts_slope_0_45": -0.01642,
           "expected_move_pct": 5.0}
    row.update(kw)
    return row


def test_report_explains_a_missing_expected_move():
    # straddle stays None when an ATM leg has no two-sided quote (ZERO_BID),
    # and None in a float column is NaN — which rendered as "预期波动=nan%",
    # indistinguishable from a computation bug. YMM/ZTO, 2026-08-19.
    df = pd.DataFrame([_report_row(expected_move_pct=None),
                       _report_row(symbol="TGT", expected_move_pct=7.08)])
    out = build_cn_report(df, date(2026, 8, 18))
    assert "nan" not in out, out
    assert "预期波动=不可用(ATM 缺双边报价,未用 ask/2 估算)" in out
    assert "预期波动=7.08%" in out


def test_replay_report_renders_a_missing_expected_move_as_unknown():
    """The end-to-end path grade(NaN) alone does not cover.

    build_replay_report() normalises a NaN expected move to None. Drop that
    and grade() still returns ❓ (it guards non-finite), but `em_txt` at
    replay.py:128 is `f"{em}%" if em else "?(EM缺失)"` — and NaN is truthy,
    so the replay block goes back to saying "预期nan%" and a non-finite
    em_pct lands in replay_log.csv.
    """
    import pathlib
    import tempfile

    import replay

    rows = pd.DataFrame([{
        "symbol": "YMM", "tier": "RECOMMENDED", "timing": "BMO",
        COL_ENTRY: "2026-08-18", COL_EXIT: "2026-08-19",
        "expected_move_pct": float("nan"),   # ZERO_BID name
        "flags": "ZERO_BID|LOW_OI", "front_dte": 3,
    }])
    tmp = pathlib.Path(tempfile.mkdtemp())
    saved = (replay._signals_exiting, replay._real_prices,
             replay.SCAN_DIR, replay.REPLAY_LOG)
    try:
        replay._signals_exiting = lambda _today: rows
        replay._real_prices = lambda *_a: (100.0, 103.2, 103.2)
        replay.SCAN_DIR = tmp
        replay.REPLAY_LOG = tmp / "replay_log.csv"
        out = replay.build_replay_report(date(2026, 8, 19))
        logged = pd.read_csv(tmp / "replay_log.csv")
    finally:
        (replay._signals_exiting, replay._real_prices,
         replay.SCAN_DIR, replay.REPLAY_LOG) = saved

    assert "nan" not in out.lower(), out
    assert "?(EM缺失)" in out, out
    # ❓ twice: once on the signal line, once in the cumulative tally.
    assert "❓ YMM [RECOMMENDED]" in out, out
    assert "❓1" in out, out
    assert pd.isna(logged["em_pct"].iloc[0]) and pd.isna(logged["ratio"].iloc[0])
    assert logged["grade"].iloc[0] == "❓"


def test_verdict_silent_when_only_avoid_rows():
    # README used to say the verdict appears whenever recommendations are 0;
    # it is also silent when nothing reached CONSIDER either.
    assert build_verdict(pd.DataFrame([_con(tier="AVOID")]), "confirm") == ""
    assert build_verdict(pd.DataFrame([_con(tier="NO_DATA")]), "confirm") == ""


def test_consider_reason_marks_inverted_ivrv():
    # AMAT on 2026-08-13: IV *cheaper* than realized — not "nearly there".
    out = consider_reason(_con(ivrv=0.69))
    assert "差 0.56 到 1.25" in out and "已反向" in out
    # 1.0 is the boundary: at or above it, no inversion warning.
    assert "已反向" not in consider_reason(_con(ivrv=1.0))


def test_consider_reason_volume_gap_in_wan():
    out = consider_reason(_con(vol=266_689, ivrv_ok=True, vol_ok=False))
    assert "26.7万" in out and "差 123.3万 到 150万" in out


def test_consider_reason_survives_csv_roundtrip_strings():
    # vol_ok/ivrv_ok come back as strings after a CSV round-trip.
    r = _con(ivrv=1.222)
    r["ivrv_ok"], r["vol_ok"] = "False", "True"
    assert "差 0.028 到 1.25" in consider_reason(r)


def test_verdict_silent_when_a_recommendation_exists():
    df = pd.DataFrame([_con(tier="RECOMMENDED"), _con(ivrv=0.69)])
    assert build_verdict(df, "confirm") == ""


def test_verdict_silent_when_nothing_actionable():
    assert build_verdict(pd.DataFrame([_con(tier="AVOID")]), "confirm") == ""


def test_verdict_flags_all_ivrv_failures():
    # The 2026-08-13 confirm: 4 CONSIDER, every one failing on iv30/rv30.
    df = pd.DataFrame([_con(ivrv=v) for v in (1.222, 1.178, 1.041, 0.69)])
    out = build_verdict(df, "confirm")
    assert out.startswith("⛔") and "4 只全部倒在" in out
    # preview is hours before entry, so it must not read as a final verdict.
    assert "以开仓前的 confirm 班次为准" in build_verdict(df, "preview")


def test_verdict_distinguishes_volume_only_and_mixed():
    vol_only = pd.DataFrame([_con(ivrv_ok=True, vol_ok=False)])
    assert "成交量不足 150万" in build_verdict(vol_only, "confirm")
    mixed = pd.DataFrame([_con(ivrv=0.69), _con(ivrv_ok=True, vol_ok=False)])
    out = build_verdict(mixed, "confirm")
    assert "1 只 IV 不够贵" in out and "1 只成交量不足" in out


def test_replay_grade_bands():
    from replay import grade
    # Calibrated on the 2026-07-15/16 replay: CTAS 6.13/5.69=1.08 -> 🟠,
    # HOMB 4.44/2.30=1.93 -> 🔴, ELV 9.88/6.50=1.52 -> 🔴, FHN 1.71/3.70 -> ✅.
    assert grade(None) == "❓"
    # A ZERO_BID name has no expected move; NaN must not grade as 🔴, which
    # would let "no data" count as "moved far more than expected" in the
    # cumulative stats the thresholds are tuned on.
    assert grade(float("nan")) == "❓"
    assert grade(0.46) == "✅"
    assert grade(0.7) == "🟢"
    assert grade(1.08) == "🟠"
    assert grade(1.52) == "🔴"
    assert grade(1.93) == "🔴"


if __name__ == "__main__":
    import sys
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except Exception:
                failed += 1
                print(f"  FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)
