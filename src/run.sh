#!/bin/bash
# 供 cron 调用（无参数时执行一轮检查并写日志）；
# 也可带参数动态管理，例如：
#   ./src/run.sh --add-sub C1-101 a@example.com 10
#   ./src/run.sh --remove-sub C1-101 a@example.com
#   ./src/run.sh --list-subs
#   ./src/run.sh --test-email a@example.com
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT" || exit 1
PY="${PYTHON:-python3}"

if [ "$#" -gt 0 ]; then
    # 带参数：直接执行并把输出显示在终端（便于交互/查看结果）
    exec "$PY" "$SCRIPT_DIR/main.py" "$@"
else
    # 无参数：定时检查，输出追加到日志
    mkdir -p "$PROJECT_ROOT/log"
    exec "$PY" "$SCRIPT_DIR/main.py" --once >> "$PROJECT_ROOT/log/run.log" 2>&1
fi
