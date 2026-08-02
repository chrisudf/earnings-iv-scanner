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
# 按 ET 排程（CRON_TZ 由 Vixie/ISC cron 支持，Ubuntu 默认即是），夏令时切换
# 交给系统时区库处理 —— 不像 Windows 那边要为每个模式设两个触发时间。
# 时刻取窗口中段：preview 窗口 ET 10:00-10:45，confirm 窗口 ET 14:45-15:35。
if [ "$INSTALL_CRON" = 1 ]; then
    say "安装 crontab 条目"
    if ! command -v crontab >/dev/null; then
        sudo apt-get install -y cron && sudo systemctl enable --now cron
    fi

    LOG="$BASE_DIR/scans/cron.log"
    NEW_BLOCK=$(cat <<EOF
$CRON_MARK  (deploy.sh 生成，勿手改；重跑 deploy.sh 会覆盖本块)
CRON_TZ=America/New_York
15 10 * * 1-5 cd $BASE_DIR && $PY notify.py preview >> $LOG 2>&1
15 15 * * 1-5 cd $BASE_DIR && $PY notify.py confirm >> $LOG 2>&1
# <<< earnings-iv-scanner
EOF
)
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
