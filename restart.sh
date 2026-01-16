#!/bin/bash

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$PROJECT_DIR/logs.log"
PID_FILE="$PROJECT_DIR/.ghostap.pid"

cd "$PROJECT_DIR"

get_running_pids() {
    ps aux | grep -E "(uv run python -m src\.main|\.venv/bin/python.*-m src\.main)" | grep -v grep | awk '{print $2}'
}

stop_service() {
    echo "正在停止 GhostAP 服务..."
    
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID"
            sleep 2
            if kill -0 "$PID" 2>/dev/null; then
                echo "进程未响应，强制终止..."
                kill -9 "$PID"
            fi
            echo "已停止进程 PID: $PID"
        fi
        rm -f "$PID_FILE"
    fi
    
    PIDS=$(get_running_pids)
    if [ -n "$PIDS" ]; then
        echo "发现残留进程: $PIDS，正在清理..."
        echo "$PIDS" | xargs kill 2>/dev/null
        sleep 1
        PIDS=$(get_running_pids)
        if [ -n "$PIDS" ]; then
            echo "$PIDS" | xargs kill -9 2>/dev/null
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
    nohup bash -c "
        sleep 2
        cd '$PROJECT_DIR'
        
        # 停止服务
        if [ -f '$PID_FILE' ]; then
            PID=\$(cat '$PID_FILE')
            if kill -0 \"\$PID\" 2>/dev/null; then
                kill \"\$PID\"
                sleep 2
                kill -0 \"\$PID\" 2>/dev/null && kill -9 \"\$PID\"
            fi
            rm -f '$PID_FILE'
        fi
        
        # 清理残留进程
        PIDS=\$(ps aux | grep -E '(uv run python -m src\.main|\.venv/bin/python.*-m src\.main)' | grep -v grep | awk '{print \$2}')
        [ -n \"\$PIDS\" ] && echo \"\$PIDS\" | xargs kill 2>/dev/null
        sleep 1
        PIDS=\$(ps aux | grep -E '(uv run python -m src\.main|\.venv/bin/python.*-m src\.main)' | grep -v grep | awk '{print \$2}')
        [ -n \"\$PIDS\" ] && echo \"\$PIDS\" | xargs kill -9 2>/dev/null
        
        # 启动服务
        sleep 1
        nohup uv run python -m src.main > '$LOG_FILE' 2>&1 &
        echo \$! > '$PID_FILE'
        
        echo '✅ 远程重启完成' >> '$LOG_FILE'
    " >> "$LOG_FILE" 2>&1 &
    disown
    echo "✅ 远程重启已触发，服务将在 3 秒后重新启动"
    echo "   可通过 './restart.sh status' 或查看日志确认状态"
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
