#!/bin/bash

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$PROJECT_DIR/logs.log"
PID_FILE="$PROJECT_DIR/.ghostap.pid"
RESTART_SCRIPT="$PROJECT_DIR/.restart_worker.sh"

cd "$PROJECT_DIR"

get_running_pids() {
    ps aux | grep -E "(uv run python -m src\.main|\.venv/bin/python.*-m src\.main)" | grep -v grep | awk '{print $2}'
}

stop_service() {
    echo "正在停止 GhostAP 服务..."
    
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            # 先尝试优雅停止（进程本身 + 进程组，确保子进程(ACP agent等)不残留）
            kill "$PID" 2>/dev/null || true
            kill -- -"$PID" 2>/dev/null || true
            sleep 2
            if kill -0 "$PID" 2>/dev/null; then
                echo "进程未响应，强制终止..."
                kill -9 "$PID" 2>/dev/null || true
                kill -9 -- -"$PID" 2>/dev/null || true
            fi
            echo "已停止进程 PID: $PID"
        fi
        rm -f "$PID_FILE"
    fi
    
    PIDS=$(get_running_pids)
    if [ -n "$PIDS" ]; then
        echo "发现残留进程: $PIDS，正在清理..."
        # 同时杀进程本身与进程组，避免遗留子进程
        echo "$PIDS" | xargs kill 2>/dev/null || true
        for p in $PIDS; do
            kill -- -"$p" 2>/dev/null || true
        done
        sleep 1
        PIDS=$(get_running_pids)
        if [ -n "$PIDS" ]; then
            echo "$PIDS" | xargs kill -9 2>/dev/null || true
            for p in $PIDS; do
                kill -9 -- -"$p" 2>/dev/null || true
            done
        fi
    fi
    
    echo "✅ 服务已停止"
}

start_service() {
    echo "正在启动 GhostAP 服务..."
    nohup uv run python -m src.main > "$LOG_FILE" 2>&1 &
    PID=$!
    echo $PID > "$PID_FILE"
    sleep 2
    
    RUNNING_PIDS=$(get_running_pids)
    if [ -n "$RUNNING_PIDS" ]; then
        echo "✅ GhostAP 服务已启动"
        echo "   进程: $RUNNING_PIDS"
        echo "   日志: $LOG_FILE"
    else
        echo "❌ 启动失败，请检查日志: $LOG_FILE"
        exit 1
    fi
}

show_status() {
    PIDS=$(get_running_pids)
    if [ -n "$PIDS" ]; then
        echo "✅ GhostAP 正在运行"
        echo "   进程列表:"
        ps aux | grep -E "(uv run python -m src\.main|\.venv/bin/python.*-m src\.main)" | grep -v grep
    else
        echo "❌ GhostAP 未运行"
    fi
}

remote_restart() {
    echo "🔄 触发远程重启..."
    
    cat > "$RESTART_SCRIPT" << 'WORKER_EOF'
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
        kill "$PID" 2>/dev/null || true
        kill -- -"$PID" 2>/dev/null || true
        sleep 2
        kill -0 "$PID" 2>/dev/null && {
            kill -9 "$PID" 2>/dev/null || true
            kill -9 -- -"$PID" 2>/dev/null || true
        }
    fi
    rm -f "$PID_FILE"
fi

PIDS=$(ps aux | grep -E "(uv run python -m src\.main|\.venv/bin/python.*-m src\.main)" | grep -v grep | awk '{print $2}')
[ -n "$PIDS" ] && echo "$PIDS" | xargs kill 2>/dev/null || true
[ -n "$PIDS" ] && for p in $PIDS; do kill -- -"$p" 2>/dev/null || true; done
sleep 1
PIDS=$(ps aux | grep -E "(uv run python -m src\.main|\.venv/bin/python.*-m src\.main)" | grep -v grep | awk '{print $2}')
[ -n "$PIDS" ] && echo "$PIDS" | xargs kill -9 2>/dev/null || true
[ -n "$PIDS" ] && for p in $PIDS; do kill -9 -- -"$p" 2>/dev/null || true; done

sleep 1

nohup uv run python -m src.main >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

echo "$(date '+%Y-%m-%d %H:%M:%S') [RESTART] 重启完成，新PID: $(cat $PID_FILE)" >> "$LOG_FILE"

rm -f "$0"
WORKER_EOF

    chmod +x "$RESTART_SCRIPT"
    
    setsid "$RESTART_SCRIPT" </dev/null >/dev/null 2>&1 &
    
    echo "✅ 远程重启已触发"
    echo "   服务将在 3 秒后重新启动"
    echo "   查看日志: tail -f $LOG_FILE"
}

case "${1:-restart}" in
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        stop_service
        start_service
        ;;
    remote-restart|rr)
        remote_restart
        ;;
    status)
        show_status
        ;;
    *)
        echo "用法: $0 {start|stop|restart|remote-restart|status}"
        echo "  start          - 启动服务"
        echo "  stop           - 停止服务"
        echo "  restart        - 本地重启（停止后立即启动）"
        echo "  remote-restart - 远程重启（适用于通过机器人执行）"
        echo "  status         - 查看服务状态"
        exit 1
        ;;
esac
