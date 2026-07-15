#!/bin/bash

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
PROJECT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd -P)"
LOG_FILE="$PROJECT_DIR/logs.log"
PID_FILE="$PROJECT_DIR/.ghostap.pid"
RESTART_SCRIPT="$PROJECT_DIR/.restart_worker.sh"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"

RESTART_GRACE_DELAY="${GHOSTAP_RESTART_GRACE_DELAY:-1}"
TERM_GRACE_DELAY="${GHOSTAP_TERM_GRACE_DELAY:-0.8}"
RESIDUAL_GRACE_DELAY="${GHOSTAP_RESIDUAL_GRACE_DELAY:-0.2}"
START_CHECK_DELAY="${GHOSTAP_START_CHECK_DELAY:-0.3}"
LOG_MODE="${GHOSTAP_LOG_MODE:-truncate}"
STARTED_PID=""
PROJECT_LAUNCHCTL_ID=$(printf '%s' "$PROJECT_DIR" | cksum | awk '{print $1}')
LAUNCHCTL_LABEL="${GHOSTAP_LAUNCHCTL_LABEL:-com.ghostap.local.${PROJECT_LAUNCHCTL_ID}}"
RESTART_LAUNCHCTL_LABEL="${GHOSTAP_RESTART_LAUNCHCTL_LABEL:-${LAUNCHCTL_LABEL}.restart}"
CODEX_ACP_NPM_PACKAGE="${GHOSTAP_CODEX_ACP_NPM_PACKAGE:-@agentclientprotocol/codex-acp@1.1.2}"
PREPARE_CODEX_ACP="${GHOSTAP_PREPARE_CODEX_ACP:-1}"
TUI2ACP_NPM_PACKAGE="${GHOSTAP_TUI2ACP_NPM_PACKAGE:-tui2acp}"
PREPARE_TUI2ACP="${GHOSTAP_PREPARE_TUI2ACP:-1}"
SYNC_PYTHON_DEPENDENCIES="${GHOSTAP_SYNC_PYTHON_DEPENDENCIES:-1}"
PREPARE_EMPLOYEE_SANDBOX="${GHOSTAP_PREPARE_EMPLOYEE_SANDBOX:-1}"

cd "$PROJECT_DIR"

get_running_pids() {
    local pid command
    while read -r pid command; do
        [ -n "$pid" ] || continue
        if pid_is_ghostap_service "$pid" "$command"; then
            echo "$pid"
        fi
    done < <(ps -axo pid=,command= 2>/dev/null)
}

pid_belongs_to_project() {
    local pid="$1"
    local process_cwd=""
    case "$(uname -s)" in
        Linux)
            process_cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null) || return 1
            ;;
        Darwin)
            command -v lsof >/dev/null 2>&1 || return 1
            process_cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)
            ;;
        *)
            command -v pwdx >/dev/null 2>&1 || return 1
            process_cwd=$(pwdx "$pid" 2>/dev/null | sed 's/^[^:]*:[[:space:]]*//')
            ;;
    esac
    [ -n "$process_cwd" ] && [ "$process_cwd" = "$PROJECT_DIR" ]
}

pid_is_ghostap_service() {
    local pid="$1"
    local process_command="${2:-}"
    local command_kind=""
    if [ -z "$process_command" ]; then
        process_command=$(ps -p "$pid" -o command= 2>/dev/null) || return 1
    fi
    pid_belongs_to_project "$pid" || return 1
    case "$process_command" in
        "$PYTHON_BIN -m src.main"|".venv/bin/python -m src.main")
            command_kind="python"
            ;;
        "uv run python -m src.main"|*/uv\ run\ python\ -m\ src.main)
            command_kind="uv"
            ;;
        *) return 1 ;;
    esac
    if [ "$(uname -s)" = "Linux" ]; then
        local process_exe expected_exe
        process_exe=$(readlink -f "/proc/$pid/exe" 2>/dev/null) || return 1
        if [ "$command_kind" = "python" ]; then
            expected_exe=$(readlink -f "$PYTHON_BIN" 2>/dev/null) || return 1
        else
            expected_exe=$(command -v uv 2>/dev/null) || return 1
            expected_exe=$(readlink -f "$expected_exe" 2>/dev/null) || return 1
        fi
        [ "$process_exe" = "$expected_exe" ] || return 1
    fi
    return 0
}

log_restart() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [RESTART] $*" >> "$LOG_FILE"
}

start_service_process() {
    local mode="${1:-truncate}"
    local detach_cmd=()
    if command -v setsid >/dev/null 2>&1; then
        detach_cmd=(setsid)
    elif command -v launchctl >/dev/null 2>&1; then
        launchctl remove "$LAUNCHCTL_LABEL" >/dev/null 2>&1 || true
        unset VIRTUAL_ENV
        if [ -x "$PYTHON_BIN" ]; then
            launchctl submit -l "$LAUNCHCTL_LABEL" -- /bin/bash -lc "cd '$PROJECT_DIR' && exec '$PYTHON_BIN' -m src.main >> '$LOG_FILE' 2>&1"
        else
            launchctl submit -l "$LAUNCHCTL_LABEL" -- /bin/bash -lc "cd '$PROJECT_DIR' && exec uv run python -m src.main >> '$LOG_FILE' 2>&1"
        fi
        STARTED_PID=""
        return
    fi

    unset VIRTUAL_ENV
    if [ -x "$PYTHON_BIN" ]; then
        if [ "$mode" = "append" ]; then
            nohup "${detach_cmd[@]}" "$PYTHON_BIN" -m src.main >> "$LOG_FILE" 2>&1 &
        else
            nohup "${detach_cmd[@]}" "$PYTHON_BIN" -m src.main > "$LOG_FILE" 2>&1 &
        fi
    else
        if [ "$mode" = "append" ]; then
            nohup "${detach_cmd[@]}" uv run python -m src.main >> "$LOG_FILE" 2>&1 &
        else
            nohup "${detach_cmd[@]}" uv run python -m src.main > "$LOG_FILE" 2>&1 &
        fi
    fi
    STARTED_PID=$!
    disown "$STARTED_PID" 2>/dev/null || true
}

service_command_label() {
    if [ -x "$PYTHON_BIN" ]; then
        echo "$PYTHON_BIN -m src.main"
    else
        echo "uv run python -m src.main"
    fi
}

venv_has_stale_entrypoint_shebang() {
    [ -d "$PROJECT_DIR/.venv/bin" ] || return 1
    local script first_line interpreter
    while IFS= read -r -d '' script; do
        IFS= read -r first_line < "$script" || continue
        case "$first_line" in
            '#!'*/.venv/bin/python*)
                interpreter="${first_line#\#!}"
                case "$interpreter" in
                    "$PROJECT_DIR"/.venv/bin/python*) ;;
                    *) return 0 ;;
                esac
                ;;
        esac
    done < <(find "$PROJECT_DIR/.venv/bin" -maxdepth 1 -type f -perm -u+x -print0)
    return 1
}

prepare_python_dependencies() {
    if [ "$SYNC_PYTHON_DEPENDENCIES" = "0" ]; then
        log_restart "python dependency sync skipped"
        return
    fi
    if ! command -v uv >/dev/null 2>&1; then
        echo "❌ 未找到 uv，无法同步 Python 依赖"
        log_restart "python dependency sync failed missing uv"
        return 1
    fi

    echo "同步 GhostAP Python 依赖..."
    if venv_has_stale_entrypoint_shebang; then
        echo "检测到项目目录迁移，正在重建虚拟环境入口脚本..."
        if uv sync --group dev --reinstall >/dev/null 2>&1; then
            log_restart "python dependencies reinstalled stale entrypoint shebang"
            return
        fi
        echo "❌ Python 虚拟环境入口脚本修复失败"
        log_restart "python dependency reinstall failed stale entrypoint shebang"
        return 1
    elif uv sync --check --group dev >/dev/null 2>&1; then
        log_restart "python dependencies already synchronized"
        return
    fi
    if uv sync --group dev >/dev/null 2>&1; then
        log_restart "python dependencies synced"
    else
        echo "❌ Python 依赖同步失败"
        log_restart "python dependency sync failed"
        return 1
    fi
}

run_privileged() {
    if [ "$(id -u)" = "0" ]; then
        "$@"
        return
    fi
    if ! command -v sudo >/dev/null 2>&1; then
        return 1
    fi
    if [ -t 0 ]; then
        sudo "$@"
    else
        sudo -n "$@"
    fi
}

install_linux_bubblewrap() {
    if command -v apt-get >/dev/null 2>&1; then
        run_privileged apt-get update >/dev/null 2>&1 && \
            run_privileged apt-get install -y bubblewrap >/dev/null 2>&1
    elif command -v dnf >/dev/null 2>&1; then
        run_privileged dnf install -y bubblewrap >/dev/null 2>&1
    elif command -v yum >/dev/null 2>&1; then
        run_privileged yum install -y bubblewrap >/dev/null 2>&1
    elif command -v zypper >/dev/null 2>&1; then
        run_privileged zypper --non-interactive install bubblewrap >/dev/null 2>&1
    elif command -v pacman >/dev/null 2>&1; then
        run_privileged pacman -S --needed --noconfirm bubblewrap >/dev/null 2>&1
    elif command -v apk >/dev/null 2>&1; then
        run_privileged apk add bubblewrap >/dev/null 2>&1
    else
        return 1
    fi
}

prepare_employee_sandbox_dependency() {
    if [ "$PREPARE_EMPLOYEE_SANDBOX" = "0" ]; then
        log_restart "employee sandbox preparation skipped"
        return
    fi

    case "$(uname -s)" in
        Linux)
            if [ ! -x /usr/bin/bwrap ]; then
                echo "安装员工 Channel 隔离依赖 bubblewrap..."
                install_linux_bubblewrap || true
            fi
            if [ -x /usr/bin/bwrap ]; then
                log_restart "employee sandbox ready mechanism=bubblewrap"
            else
                echo "⚠️  bubblewrap 自动安装失败，员工 Channel 将记录未验证降级"
                log_restart "employee sandbox unavailable mechanism=bubblewrap"
            fi
            ;;
        Darwin)
            if [ -x /usr/bin/sandbox-exec ]; then
                log_restart "employee sandbox ready mechanism=seatbelt"
            else
                echo "⚠️  macOS 系统 sandbox-exec 不可用，员工 Channel 将 fail-closed"
                log_restart "employee sandbox unavailable mechanism=seatbelt"
            fi
            ;;
        *)
            echo "⚠️  当前平台没有受支持的员工 Channel 文件系统沙箱"
            log_restart "employee sandbox unavailable mechanism=unsupported"
            ;;
    esac
}

codex_native_acp_available() {
    command -v codex >/dev/null 2>&1 || return 1
    codex acp serve --help 2>&1 | grep -Eiq "(acp serve|acp.*server)"
}

prepare_codex_acp_dependency() {
    if [ "$PREPARE_CODEX_ACP" = "0" ]; then
        log_restart "codex acp fallback preheat skipped"
        return
    fi
    if codex_native_acp_available; then
        log_restart "codex native acp serve available"
        return
    fi
    if ! command -v npx >/dev/null 2>&1; then
        echo "⚠️  未找到 npx，Codex ACP fallback 可能无法启动"
        log_restart "codex acp fallback missing npx"
        return
    fi

    echo "准备 Codex ACP fallback 依赖..."
    if npx --yes "$CODEX_ACP_NPM_PACKAGE" --version >/dev/null 2>&1; then
        log_restart "codex acp fallback ready package=$CODEX_ACP_NPM_PACKAGE"
    else
        echo "⚠️  Codex ACP fallback 依赖预热失败，后续 /codex 可能启动失败"
        log_restart "codex acp fallback preheat failed package=$CODEX_ACP_NPM_PACKAGE"
    fi
}

prepare_tui2acp_dependency() {
    if [ "$PREPARE_TUI2ACP" = "0" ]; then
        log_restart "tui2acp preheat skipped"
        return
    fi
    if command -v tui2acp >/dev/null 2>&1; then
        log_restart "tui2acp already available"
        return
    fi
    if ! command -v npm >/dev/null 2>&1; then
        echo "⚠️  未找到 npm，tui2acp 无法自动安装"
        log_restart "tui2acp missing npm"
        return
    fi

    echo "准备 tui2acp 依赖..."
    if npm install -g "$TUI2ACP_NPM_PACKAGE" >/dev/null 2>&1; then
        log_restart "tui2acp installed package=$TUI2ACP_NPM_PACKAGE"
        echo "✅ tui2acp 已安装"
    else
        echo "⚠️  tui2acp 安装失败，后续 /tui2acp 可能无法使用"
        log_restart "tui2acp install failed package=$TUI2ACP_NPM_PACKAGE"
    fi
}

stop_service() {
    echo "正在停止 GhostAP 服务..."
    log_restart "stop begin"
    
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null && pid_is_ghostap_service "$PID"; then
            # 先尝试优雅停止（进程本身 + 进程组，确保子进程(ACP agent等)不残留）
            kill "$PID" 2>/dev/null || true
            kill -- -"$PID" 2>/dev/null || true
            sleep "$TERM_GRACE_DELAY"
            if kill -0 "$PID" 2>/dev/null; then
                echo "进程未响应，强制终止..."
                kill -9 "$PID" 2>/dev/null || true
                kill -9 -- -"$PID" 2>/dev/null || true
            fi
            echo "已停止进程 PID: $PID"
        elif kill -0 "$PID" 2>/dev/null; then
            echo "忽略不属于当前项目的 PID 文件记录: $PID"
            log_restart "stale pid file ignored pid=$PID"
        fi
        rm -f "$PID_FILE"
    fi
    if command -v launchctl >/dev/null 2>&1; then
        launchctl remove "$LAUNCHCTL_LABEL" >/dev/null 2>&1 || true
    fi
    
    PIDS=$(get_running_pids)
    if [ -n "$PIDS" ]; then
        echo "发现残留进程: $(echo $PIDS | tr '\n' ' ')，正在清理..."
        # 同时杀进程本身与进程组，避免遗留子进程
        echo "$PIDS" | xargs kill 2>/dev/null || true
        for p in $PIDS; do
            kill -- -"$p" 2>/dev/null || true
        done
        sleep "$RESIDUAL_GRACE_DELAY"
        PIDS=$(get_running_pids)
        if [ -n "$PIDS" ]; then
            echo "$PIDS" | xargs kill -9 2>/dev/null || true
            for p in $PIDS; do
                kill -9 -- -"$p" 2>/dev/null || true
            done
        fi
    fi
    
    echo "✅ 服务已停止"
    log_restart "stop done"
}

start_service() {
    echo "正在启动 GhostAP 服务..."
    local start_log_mode="$LOG_MODE"
    if [ "$start_log_mode" != "append" ]; then
        : > "$LOG_FILE"
        start_log_mode="append"
    fi
    prepare_python_dependencies || exit 1
    prepare_employee_sandbox_dependency
    prepare_codex_acp_dependency
    prepare_tui2acp_dependency
    log_restart "start begin cmd=$(service_command_label)"
    start_service_process "$start_log_mode"
    PID="$STARTED_PID"
    if [ -n "$PID" ]; then
        echo $PID > "$PID_FILE"
    fi
    sleep "$START_CHECK_DELAY"
    
    RUNNING_PIDS=$(get_running_pids)
    if [ -n "$RUNNING_PIDS" ]; then
        if [ -z "$PID" ]; then
            PID=$(echo "$RUNNING_PIDS" | awk 'NR==1 {print $1}')
            echo $PID > "$PID_FILE"
        fi
        echo "✅ GhostAP 服务已启动"
        echo "   进程: $RUNNING_PIDS"
        echo "   启动命令: $(service_command_label)"
        echo "   日志: $LOG_FILE"
        log_restart "start spawned pid=$PID running=$RUNNING_PIDS"
    else
        echo "❌ 启动失败，请检查日志: $LOG_FILE"
        log_restart "start failed pid=$PID"
        exit 1
    fi
}

show_status() {
    PIDS=$(get_running_pids)
    if [ -n "$PIDS" ]; then
        echo "✅ GhostAP 正在运行"
        echo "   进程列表:"
        for PID in $PIDS; do
            ps -p "$PID" -o pid=,command=
        done
    else
        echo "❌ GhostAP 未运行"
    fi
}

remote_restart() {
    echo "🔄 触发远程重启..."
    
    cat > "$RESTART_SCRIPT" << WORKER_EOF
#!/bin/bash
PROJECT_DIR="$PROJECT_DIR"
LOG_FILE="$LOG_FILE"
PID_FILE="$PID_FILE"
RESTART_GRACE_DELAY="$RESTART_GRACE_DELAY"

cd "\$PROJECT_DIR"

sleep "\$RESTART_GRACE_DELAY"

echo "\$(date '+%Y-%m-%d %H:%M:%S') [RESTART] remote worker begin" >> "\$LOG_FILE"
GHOSTAP_LOG_MODE=append "\$PROJECT_DIR/restart.sh" restart >> "\$LOG_FILE" 2>&1
STATUS=\$?
NEW_PID="-"
[ -f "\$PID_FILE" ] && NEW_PID=\$(cat "\$PID_FILE")
echo "\$(date '+%Y-%m-%d %H:%M:%S') [RESTART] remote worker done status=\$STATUS pid=\$NEW_PID" >> "\$LOG_FILE"

rm -f "\$0"
exit "\$STATUS"
WORKER_EOF

    chmod +x "$RESTART_SCRIPT"
    
    if command -v setsid >/dev/null 2>&1; then
        setsid "$RESTART_SCRIPT" </dev/null >/dev/null 2>&1 &
    elif command -v launchctl >/dev/null 2>&1; then
        launchctl remove "$RESTART_LAUNCHCTL_LABEL" >/dev/null 2>&1 || true
        launchctl submit -l "$RESTART_LAUNCHCTL_LABEL" -- /bin/bash "$RESTART_SCRIPT" >/dev/null 2>&1 &
    else
        nohup "$RESTART_SCRIPT" </dev/null >/dev/null 2>&1 &
    fi
    WORKER_PID=$!
    disown "$WORKER_PID" 2>/dev/null || true
    
    echo "✅ 远程重启已触发"
    echo "   服务将在 ${RESTART_GRACE_DELAY} 秒后重新启动"
    echo "   查看日志: tail -f $LOG_FILE"
}

if [ "${GHOSTAP_RESTART_LIBRARY_ONLY:-0}" = "1" ]; then
    return 0 2>/dev/null || exit 0
fi

case "${1:-restart}" in
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        stop_service
        LOG_MODE="${GHOSTAP_LOG_MODE:-append}"
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
