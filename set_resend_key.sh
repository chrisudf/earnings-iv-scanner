#!/usr/bin/env bash
# 把 Resend API key 写进 notify_config.json,然后冒烟测试。
# key 用 read -s 静默读入:不回显、不进 bash history、不出现在进程列表里。
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$BASE_DIR/notify_config.json"
[ -f "$CONFIG" ] || { echo "找不到 $CONFIG" >&2; exit 1; }

read -rsp "粘贴 Resend API key (re_... , 输入不回显): " RESEND_KEY
echo
[ -n "$RESEND_KEY" ] || { echo "没有输入,已取消" >&2; exit 1; }
case "$RESEND_KEY" in
    re_*) ;;
    *) echo "警告: key 不是 re_ 开头,继续但请确认没粘错" >&2 ;;
esac

# 备份写到仓库**外面**:.gitignore 只挡 notify_config.json 这个确切文件名,
# 放在仓库里的 .bak 会以 untracked 出现,一个 git add -A 就把凭据提交进去了。
# 用 umask 而非 cp -p —— cp -p 会原样保留源文件可能过宽的权限位。
BACKUP_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/earnings-iv-scanner"
mkdir -p "$BACKUP_DIR" && chmod 700 "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/notify_config.json.bak.$(date +%F-%H%M%S)"
(umask 077 && cat "$CONFIG" > "$BACKUP")
echo "已备份原配置到 $BACKUP"
RESEND_KEY="$RESEND_KEY" python3 - "$CONFIG" <<'PY'
import json, os, sys
p = sys.argv[1]
with open(p, encoding="utf-8-sig") as f:
    c = json.load(f)
c["resend_api_key"] = os.environ["RESEND_KEY"]   # 走环境变量,不进命令行参数
c.setdefault("mail_from", "onboarding@resend.dev")
c.setdefault("send_to", c.get("gmail_user", ""))
with open(p, "w", encoding="utf-8") as f:
    json.dump(c, f, indent=2, ensure_ascii=False)
print("已写入。发件: " + c["mail_from"] + "  收件: " + c["send_to"])
PY
chmod 600 "$CONFIG"

echo
echo "==> 冒烟测试(--force 跳过 ET 窗口,会真发一封邮件)"
cd "$BASE_DIR" && .venv/bin/python notify.py confirm --force
