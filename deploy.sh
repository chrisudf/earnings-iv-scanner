#!/usr/bin/env bash
#
# 把扫描器部署到一台 Linux 服务器（DigitalOcean droplet 等）上跑。
#
#   bash deploy.sh          # 建 venv、装依赖、装 cron
#   bash deploy.sh --no-cron # 只建环境，不碰 crontab
#
# 幂等：重复跑不会重复装依赖，也不会重复往 crontab 里加行。
# 前置：仓库已 clone 到本机，notify_config.json 已用 scp 传进来（它在
# .gitignore 里，不会跟着 git 走）。
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$BASE_DIR/.venv"
PY="$VENV/bin/python"
CRON_MARK="# >>> earnings-iv-scanner"   # 用于识别本脚本写过的 cron 块
INSTALL_CRON=1
[ "${1:-}" = "--no-cron" ] && INSTALL_CRON=0

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31m错误: %s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. 系统依赖 ------------------------------------------------------------
if ! python3 -c 'import venv' 2>/dev/null; then
    say "安装 python3-venv"
    sudo apt-get update -qq && sudo apt-get install -y python3-venv
fi

# --- 2. virtualenv + Python 依赖 --------------------------------------------
if [ ! -x "$PY" ]; then
    say "创建 virtualenv"
    python3 -m venv "$VENV"
fi

say "安装/更新 Python 依赖"
"$VENV/bin/pip" install --quiet --upgrade pip
# 有 lock 文件就用 lock（精确复现），否则用带上下界的 requirements.txt
if [ -f "$BASE_DIR/requirements.lock" ]; then
    "$VENV/bin/pip" install --quiet -r "$BASE_DIR/requirements.lock"
else
    "$VENV/bin/pip" install --quiet -r "$BASE_DIR/requirements.txt"
fi

# --- 3. 邮件配置检查 --------------------------------------------------------
CONFIG="$BASE_DIR/notify_config.json"
if [ ! -f "$CONFIG" ]; then
    die "缺 notify_config.json。在本地机器上跑：
    scp notify_config.json root@<本机IP>:$BASE_DIR/
不传的话脚本只会把信号存成本地文件，不发邮件。"
fi
"$PY" -c "
import json,sys
c=json.load(open('$CONFIG'))
missing=[k for k in ('gmail_user','gmail_app_password','send_to') if not c.get(k)]
sys.exit('notify_config.json 缺字段: '+', '.join(missing) if missing else 0)
" || die "邮件配置不完整"
chmod 600 "$CONFIG"   # 里面是 Gmail 应用专用密码

mkdir -p "$BASE_DIR/scans"

# --- 4. cron ----------------------------------------------------------------
# Debian/Ubuntu 的 vixie-cron **不支持 CRON_TZ**（Ubuntu 24.04 的 cron
# 3.0pl1-184 二进制里没有这个字符串；写了会被静默忽略）。排程一律按服务器本地
# 时区解释，所以这里像 Windows 那边一样为每个模式设两个触发时刻 —— 美国夏令时
# 和冬令时各一个，notify.py 的 ET 窗口检查会跳过不匹配当前时制的那一个。
#
# 不改服务器全局时区（timedatectl）：同一个 crontab 里可能有别的任务是按现有
# 本地时区换算过的，改时区会把它们一起推移。
#
# 触发时刻由下面的 Python 从 ET 目标时刻反推，不硬编码某个时区。取窗口中段：
# preview 窗口 ET 10:00-10:45，confirm 窗口 ET 14:45-15:35。
if [ "$INSTALL_CRON" = 1 ]; then
    say "安装 crontab 条目"
    if ! command -v crontab >/dev/null; then
        sudo apt-get install -y cron && sudo systemctl enable --now cron
    fi

    LOG="$BASE_DIR/scans/cron.log"
    SCHEDULE=$("$PY" - "$BASE_DIR" "$PY" "$LOG" <<'PYEOF'
import subprocess, sys
from datetime import datetime
from zoneinfo import ZoneInfo

base_dir, py, log = sys.argv[1:4]
ET = ZoneInfo("America/New_York")
TARGETS = {"preview": (10, 15), "confirm": (15, 15)}   # ET hh:mm,窗口中段
PROBES = ((2026, 1, 15), (2026, 7, 15))                # 一个冬令时日期 + 一个夏令时日期

tzname = subprocess.run(["timedatectl", "show", "--property=Timezone", "--value"],
                        capture_output=True, text=True).stdout.strip()
if not tzname:
    sys.exit("无法确定服务器时区")
tz = ZoneInfo(tzname)

for mode, (h, m) in TARGETS.items():
    variants = {}
    for y, mo, d in PROBES:
        loc = datetime(y, mo, d, h, m, tzinfo=ET).astimezone(tz)
        shift = (loc.date() - datetime(y, mo, d).date()).days
        variants.setdefault((loc.minute, shift), set()).add(loc.hour)
    for (minute, shift), hours in sorted(variants.items()):
        # ET 的周一到周五是 cron 的 1-5；本地时间跨日则整体平移
        dow = {0: "1-5", 1: "2-6", -1: "0-4"}[shift]
        hrs = ",".join(str(x) for x in sorted(hours))
        print(f"# {mode}: ET {h:02d}:{m:02d} -> {tzname} {hrs}:{minute:02d} "
              f"(两个小时值分别对应美国冬/夏令时,只有一个会真正执行)")
        print(f"{minute} {hrs} * * {dow} cd {base_dir} && {py} notify.py {mode} >> {log} 2>&1")
PYEOF
) || die "推导 cron 触发时刻失败"

    NEW_BLOCK=$(printf '%s\n%s\n%s\n' \
        "$CRON_MARK  (deploy.sh 生成，勿手改；重跑 deploy.sh 会覆盖本块)" \
        "$SCHEDULE" \
        "# <<< earnings-iv-scanner")
    # 先剔掉旧块（如果有），再追加新块，保证不重复
    OLD=$(crontab -l 2>/dev/null || true)
    if [ -n "$OLD" ]; then
        printf '%s\n' "$OLD" > "$BASE_DIR/scans/crontab.backup.$(date +%F-%H%M)"
    fi
    printf '%s\n' "$OLD" \
        | sed "\|^$CRON_MARK|,\|^# <<< earnings-iv-scanner|d" \
        | { cat; printf '%s\n' "$NEW_BLOCK"; } \
        | crontab -
    crontab -l | sed -n "\|^$CRON_MARK|,\|^# <<< earnings-iv-scanner|p"
fi

# --- 5. 冒烟测试 ------------------------------------------------------------
say "冒烟测试（--force 跳过 ET 窗口检查，会真的发一封邮件）"
cd "$BASE_DIR" && "$PY" notify.py confirm --force

say "完成。日志：$BASE_DIR/scans/notify_log.txt 和 scans/cron.log"
echo "确认 droplet 连跑正常后，再到 Windows 上停掉本地任务："
echo "  Disable-ScheduledTask -TaskName EarningsIV-Preview"
echo "  Disable-ScheduledTask -TaskName EarningsIV-Confirm"
