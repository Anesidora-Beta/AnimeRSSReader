#!/bin/bash
# macOS 双击运行（首次使用请先在终端执行一次 chmod +x start-mac.command）
cd "$(dirname "$0")" || exit 1

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
      PY="$c"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "未找到 Python 3.9+，请先安装：https://www.python.org/downloads/"
  echo
  read -r -p "按回车关闭窗口…" _
  exit 1
fi

"$PY" app.py "$@"
echo
echo "服务已停止，可以关闭这个窗口。"
