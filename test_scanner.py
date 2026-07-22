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

from scanner import (
    PermanentDataError, _parse_mcap, build_term_structure, canon, classify,
    entry_exit, filter_dates, next_trading_day, prev_trading_day,
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


def test_replay_grade_bands():
    from replay import grade
    # Calibrated on the 2026-07-15/16 replay: CTAS 6.13/5.69=1.08 -> 🟠,
    # HOMB 4.44/2.30=1.93 -> 🔴, ELV 9.88/6.50=1.52 -> 🔴, FHN 1.71/3.70 -> ✅.
    assert grade(None) == "❓"
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
