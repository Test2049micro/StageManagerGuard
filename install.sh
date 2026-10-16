#!/bin/bash
# install.sh —— 安装台前调度守卫（检测端 + 执行端两个 LaunchAgent）
#
# 用法：./install.sh            安装/更新
#       ./install.sh --uninstall 卸载（等价于 uninstall.sh）
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL_GUARD=com.local.stage-manager-guard
LABEL_DAEMON=com.local.stage-manager-daemon
LA=~/Library/LaunchAgents
SMG_HOME="${SMG_HOME:-$HOME/Library/Application Support/StageManagerGuard}"

if [ "${1:-}" = "--uninstall" ]; then
  exec "$HERE/uninstall.sh"
fi

# 1) 构建探测工具
"$HERE/build.sh"

# 2) 解析解释器（launchd 不认 shebang，必须写绝对路径）
PYTHON="$(command -v python3)"
[ -x "$PYTHON" ] || { echo "找不到 python3，请先安装 Command Line Tools (xcode-select --install)"; exit 1; }
# 校验最低版本（脚本用了 3.7+ 语法；launchd 环境下版本漂移会静默失败）
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || { echo "python3 版本过低（$("$PYTHON" -V)），需要 >= 3.9"; exit 1; }

mkdir -p "$SMG_HOME"

# 3) 渲染 plist 模板（替换用户目录 / 解释器 / 安装路径 / 状态目录）
render() {
  sed -e "s|@BINDIR@|$HERE|g" \
      -e "s|@PYTHON@|$PYTHON|g" \
      -e "s|@SMG_HOME@|$SMG_HOME|g" \
      -e "s|@USER@|$(id -un)|g" \
      "$1" > "$2"
  plutil -lint "$2" >/dev/null
}
render "$HERE/resources/$LABEL_GUARD.plist.template"  "$LA/$LABEL_GUARD.plist"
render "$HERE/resources/$LABEL_DAEMON.plist.template" "$LA/$LABEL_DAEMON.plist"

# 4) 换载（先 bootout 旧的，避免两个 daemon 同时点开关互相抵消）
UID_N=$(id -u)
for L in "$LABEL_GUARD" "$LABEL_DAEMON"; do
  launchctl bootout "gui/$UID_N/$L" 2>/dev/null || true
done
sleep 1
launchctl bootstrap "gui/$UID_N" "$LA/$LABEL_GUARD.plist"
launchctl bootstrap "gui/$UID_N" "$LA/$LABEL_DAEMON.plist"

# 5) 自检：guard 能数屏；daemon 能活过 5 秒
sleep 5
if ! "$PYTHON" -u "$HERE/src/stage_manager_guard.py" --probe | grep -q '"screens": *[0-9]'; then
  echo "警告：探测失败，检查 bin/DisplayProbe 是否被 Gatekeeper 拦截"
fi
if ! pgrep -f sm_daemon.py >/dev/null; then
  echo "警告：daemon 未在运行，查看日志 $SMG_HOME/daemon.launchd.log"
fi

echo
echo "已安装。还差一步（只能你本人做）："
echo "  系统设置 › 隐私与安全性 › 辅助功能 → 勾选运行 daemon 的解释器："
echo "    $PYTHON"
echo "  （macOS 按二进制授权；勾选后 daemon 才点得动开关。"
echo "   验证：sm_daemon 日志里不再出现 AppleEvent timed out 即成功。）"
echo
echo "状态与日志目录：$SMG_HOME"
echo "卸载：$HERE/uninstall.sh"
