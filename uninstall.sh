#!/bin/bash
# uninstall.sh —— 卸载台前调度守卫（停任务、删 plist；状态目录默认保留供排查）
set -euo pipefail
LABEL_GUARD=com.local.stage-manager-guard
LABEL_DAEMON=com.local.stage-manager-daemon
LA=~/Library/LaunchAgents
SMG_HOME="${SMG_HOME:-$HOME/Library/Application Support/StageManagerGuard}"

UID_N=$(id -u)
for L in "$LABEL_GUARD" "$LABEL_DAEMON"; do
  launchctl bootout "gui/$UID_N/$L" 2>/dev/null || true
  rm -f "$LA/$L.plist"
done

echo "已卸载（launchd 任务与 plist 已移除）。"
echo "状态与日志仍在：$SMG_HOME （确认无误后可手动删除）"
read -r -p "现在删除该目录吗？[y/N] " ans
if [ "${ans:-N}" = "y" ] || [ "${ans:-N}" = "Y" ]; then
  rm -rf "$SMG_HOME"
  echo "已删除 $SMG_HOME"
fi
