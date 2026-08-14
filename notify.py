#!/usr/bin/env python3
"""
Scheduled wrapper around scanner.py: runs the scan at the right ET moment,
builds the Chinese signal report, and emails it via Gmail.

Two modes (each scheduled at TWO Brisbane times; the ET-window check below
picks whichever one lands in the right US-market moment, so US DST changes
never need a schedule edit — Brisbane has no DST):

  preview  ET 10:00-10:45 (Brisbane 00:15 US夏令时 / 01:15 US冬令时)
           Scan today+3 days ahead, email the upcoming candidate list with
           entry/exit Brisbane times, plus a did-you-close safety reminder.
           NOT earlier: Yahoo option quotes are delayed ~15 min, so until
           ~open+30m they still show the zeroed pre-open snapshot and every
           name comes back NO_DATA (verified 2026-07-15).

  confirm  ET 14:45-15:35 (Brisbane 05:15 US夏令时 / 06:15 US冬令时)
           Re-scan and keep only stocks whose ENTRY is today (ET) — i.e.
           you would place the order ~30 minutes after this email arrives.
           (Upper bound 15:35, before the 15:45 entry moment, so a Task
           Scheduler catch-up run cannot email an already-expired signal.)

Both modes ALWAYS email when they run in-window (signal, no-signal, or
error) — so a silent morning reliably means "the run didn't happen"
(PC asleep / task missed), never "there was nothing to say".

Usage:
    python notify.py preview [--force]
    python notify.py confirm [--force]

--force skips the ET-window/weekday gate (for manual testing).

Email config: notify_config.json next to this file:
    {"gmail_user": "...", "gmail_app_password": "...", "send_to": "..."}
Leave gmail_app_password empty to skip email (reports still saved to scans/).
"""

import argparse
import json
import smtplib
import sys
import traceback
from datetime import datetime, time as dtime
from email.header import Header
from email.mime.text import MIMEText

from scanner import (
    BASE_DIR, BNE, COL_ENTRY, ET, ENTRY_ET_HM, SCAN_DIR,
    build_cn_report, build_verdict, et_moment_to_bne, fmt_bne, is_trading_day,
    next_trading_day, scan,
)

CONFIG_PATH = BASE_DIR / "notify_config.json"
LOG_PATH = BASE_DIR / "scans" / "notify_log.txt"

# ET windows chosen so that, of the two scheduled Brisbane firings per mode,
# exactly one falls inside the window in either US DST regime.
WINDOWS = {
    "preview": (dtime(10, 0), dtime(10, 45)),
    # Upper bound BEFORE the 15:45 entry moment: a missed-task catch-up run
    # that starts later must not email an entry time that has already passed.
    "confirm": (dtime(14, 45), dtime(15, 35)),
}


def log(msg):
    LOG_PATH.parent.mkdir(exist_ok=True)
    now = datetime.now(BNE)
    line = f"[{now:%Y-%m-%d %H:%M} BNE / {now.astimezone(ET):%H:%M} ET] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    if not CONFIG_PATH.exists():
        return None
    try:
        # utf-8-sig: tolerate a BOM from Windows editors.
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        log(f"notify_config.json 解析失败({e}),视为未配置邮件")
        return None
    if cfg.get("gmail_user") and cfg.get("gmail_app_password"):
        cfg.setdefault("send_to", cfg["gmail_user"])
        return cfg
    return None


def send_email(subject, body):
    cfg = load_config()
    if not cfg:
        log("邮件未配置(notify_config.json 缺 app password),仅保存本地文件")
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = cfg["gmail_user"]
    msg["To"] = cfg["send_to"]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(cfg["gmail_user"], cfg["gmail_app_password"])
        s.sendmail(cfg["gmail_user"], [cfg["send_to"]], msg.as_string())
    log(f"邮件已发送: {subject}")
    return True


def save_report(mode, text):
    SCAN_DIR.mkdir(exist_ok=True)
    out = SCAN_DIR / f"signal_{datetime.now(ET):%Y-%m-%d_%H%M}ET_{mode}.txt"
    out.write_text(text, encoding="utf-8")
    log(f"报告已保存: {out.name}")


def in_window(mode):
    now_et = datetime.now(ET)
    if not is_trading_day(now_et.date()):
        return False, f"ET 非交易日({now_et:%a},周末或美股假日),跳过"
    lo, hi = WINDOWS[mode]
    if not (lo <= now_et.time() <= hi):
        return False, (f"ET 时间 {now_et:%H:%M} 不在 {mode} 窗口 "
                       f"{lo:%H:%M}-{hi:%H:%M} 内,跳过(这是两个触发时间里"
                       f"不匹配当前美国冬/夏令时的那一个,属正常)")
    return True, ""


def run(mode, force):
    ok, reason = in_window(mode)
    if not ok and not force:
        log(f"{mode}: {reason}")
        return

    today_et = datetime.now(ET).date()
    if mode == "preview":
        max_days = 3
    else:
        # Must reach the NEXT trading day's BMO reporters — their entry is
        # today. Over a weekend/holiday that is 3-4 calendar days out; the
        # old max_days=1 silently missed every Monday-BMO Friday entry.
        max_days = (next_trading_day(today_et) - today_et).days
    # Prefilter by entry date inside scan(): rows we'd discard below are
    # skipped BEFORE their ~10 HTTP calls of per-symbol analysis.
    df = scan(min_days=0, max_days=max_days, workers=4,
              verify=True, refresh_universe=False,
              entry_on=today_et if mode == "confirm" else None,
              min_entry=today_et if mode == "preview" else None)

    exit_reminder = ("⏰ 提醒:若你有持仓,平仓时刻(美股开盘后15分钟)已过约半小时"
                     "——还没平的话请立即处理!\n"
                     if mode == "preview" else "")

    # Level-1 replay of signals whose exit is today (TODO item 3). Runs at
    # preview time (~ET 10:15) when the true 09:45 exit bar is already final.
    # Best-effort: a replay failure must never block the signal email.
    replay_section = ""
    if mode == "preview":
        try:
            from replay import build_replay_report
            replay_section = build_replay_report(today_et)
        except Exception as e:
            log(f"复盘模块异常(不影响信号): {e!r}")

    header = (f"财报 IV 日历价差 · {'未来3天预览' if mode == 'preview' else '今日开仓确认'}\n"
              f"扫描时间: {datetime.now(BNE):%m-%d %H:%M} 布里斯班"
              f" ({datetime.now(ET):%m-%d %H:%M} ET)\n"
              f"{'-' * 46}\n")
    footer = ("\n\n" + "-" * 46 +
              "\n仅供研究参考,不构成投资建议。下单前自行核对财报日期与期权流动性。")

    if df is None:
        # No candidates at all (or every analysis failed). Still email:
        # the preview reminder is a safety net, and a guaranteed daily email
        # makes silence itself a meaningful signal (= the run didn't happen).
        log(f"{mode}: 窗口内没有符合条件的财报,无信号")
        note = ("未来几天没有罗素1000财报候选。" if mode == "preview"
                else "今日无开仓信号(窗口内无候选,或数据源不可用)。")
        body = (header + exit_reminder + ("\n" if exit_reminder else "")
                + replay_section + note + footer)
        save_report(mode, body)
        send_email("【财报IV预览】无候选(含平仓提醒)" if mode == "preview"
                   else "【财报IV】今晨无开仓信号", body)
        return

    # Machine-readable replay log (TODO item 3): the FULL result set, before
    # any tier/entry filtering — threshold tuning needs outcomes for rows the
    # filters would have excluded (AVOID included, NO_DATA as censored),
    # otherwise the sample is survivorship-biased from day one.
    SCAN_DIR.mkdir(exist_ok=True)
    raw_csv = SCAN_DIR / f"{mode}_{today_et}.csv"
    df.to_csv(raw_csv, index=False, encoding="utf-8-sig")
    log(f"复盘用原始结果已存: {raw_csv.name} ({len(df)} 行,含全部 tier)")

    # Drop rows whose entry moment is already in the past (today's BMO names).
    df = df[df[COL_ENTRY] >= today_et]
    if mode == "confirm":
        df = df[(df[COL_ENTRY] == today_et)
                & df["tier"].isin(["RECOMMENDED", "CONSIDER"])]

    n_rec = int((df["tier"] == "RECOMMENDED").sum())
    n_con = int((df["tier"] == "CONSIDER").sum())

    report = build_cn_report(df, today_et, include_no_data=(mode == "preview"))
    # Immediately above the list it describes, so it can't be skimmed past.
    verdict = build_verdict(df, mode)
    body = (header + exit_reminder + ("\n" if exit_reminder else "")
            + replay_section + verdict + report + footer)
    save_report(mode, body)

    if mode == "confirm":
        if df.empty:
            log("confirm: 今日无开仓信号(候选未通过筛选或无候选)")
            send_email("【财报IV】今晨无开仓信号", body)
            return
        syms = ", ".join(df[df["tier"] == "RECOMMENDED"]["symbol"]) or \
               ", ".join(df["symbol"])
        entry_bne = et_moment_to_bne(df.iloc[0][COL_ENTRY], ENTRY_ET_HM)
        subject = (f"【开仓信号】布里斯班 {fmt_bne(entry_bne)} 开仓 "
                   f"推荐{n_rec}只/可考虑{n_con}只: {syms}")
        send_email(subject, body)
    else:
        # Count what the body actually shows — len(df) would count AVOID rows
        # that build_cn_report never renders.
        n_shown = int(df["tier"].isin(["RECOMMENDED", "CONSIDER", "NO_DATA"]).sum())
        subject = f"【财报IV预览】未来3天候选 {n_shown} 只(推荐{n_rec}/可考虑{n_con})"
        send_email(subject, body)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["preview", "confirm"])
    ap.add_argument("--force", action="store_true",
                    help="skip the ET trading-window check (manual testing)")
    args = ap.parse_args()

    try:
        run(args.mode, args.force)
    except Exception:
        err = traceback.format_exc()
        log(f"{args.mode} 运行出错:\n{err}")
        try:
            send_email(f"【财报IV】{args.mode} 扫描出错", err)
        except Exception:
            log("错误邮件也发送失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
