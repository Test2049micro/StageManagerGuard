#!/bin/bash
# build.sh —— 编译显示器探测工具（macOS 自带 swiftc，无需 Xcode 完整安装）
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HERE/bin"
if command -v swiftc >/dev/null 2>&1; then
  swiftc -O "$HERE/src/DisplayProbe.swift" -o "$HERE/bin/DisplayProbe"
else
  xcrun -sdk macosx swiftc -O "$HERE/src/DisplayProbe.swift" -o "$HERE/bin/DisplayProbe"
fi
echo "built: $HERE/bin/DisplayProbe"
