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

cp -p "$CONFIG" "$CONFIG.bak.$(date +%F-%H%M)"
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
