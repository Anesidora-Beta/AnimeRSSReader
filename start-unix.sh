#!/bin/sh
# macOS / Linux 启动脚本
set -e
cd "$(dirname "$0")"

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
  exit 1
fi

exec "$PY" app.py "$@"
