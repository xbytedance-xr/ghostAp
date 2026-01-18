#!/bin/bash
sleep 3

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$PROJECT_DIR/logs.log"
PID_FILE="$PROJECT_DIR/.ghostap.pid"

cd "$PROJECT_DIR"

echo "$(date '+%Y-%m-%d %H:%M:%S') [RESTART] 开始重启..." >> "$LOG_FILE"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID" 2>/dev/null
        sleep 2
        kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null
    fi
    rm -f "$PID_FILE"
fi

PIDS=$(ps aux | grep -E "(uv run python -m src\.main|\.venv/bin/python.*-m src\.main)" | grep -v grep | awk '{print $2}')
[ -n "$PIDS" ] && echo "$PIDS" | xargs kill 2>/dev/null
sleep 1
PIDS=$(ps aux | grep -E "(uv run python -m src\.main|\.venv/bin/python.*-m src\.main)" | grep -v grep | awk '{print $2}')
[ -n "$PIDS" ] && echo "$PIDS" | xargs kill -9 2>/dev/null

sleep 1

nohup uv run python -m src.main >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

echo "$(date '+%Y-%m-%d %H:%M:%S') [RESTART] 重启完成，新PID: $(cat $PID_FILE)" >> "$LOG_FILE"

rm -f "$0"
