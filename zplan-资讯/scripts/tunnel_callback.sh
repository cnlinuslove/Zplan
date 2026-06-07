#!/bin/bash
# Z-Plan 企微回调隧道 — 将本地 8765 端口暴露到公网
# 用法: ./scripts/tunnel_callback.sh [start|stop|status|url]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$HOME/.openclaw/logs"
PID_FILE="$LOG_DIR/tunnel.pid"
URL_FILE="$LOG_DIR/tunnel_url.txt"
LOCAL_PORT="${LOCAL_PORT:-8765}"
mkdir -p "$LOG_DIR"

# ── 启动（后台）──
start_tunnel() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "[tunnel] 已在运行中"
        show_url
        return
    fi

    echo "[tunnel] 启动隧道 → localhost:${LOCAL_PORT}…"

    # 后台启动 SSH 隧道，只捕获 URL 行
    nohup ssh -o StrictHostKeyChecking=no \
        -o ServerAliveInterval=60 \
        -o ServerAliveCountMax=3 \
        -o ExitOnForwardFailure=yes \
        -R 80:localhost:${LOCAL_PORT} \
        localhost.run 2>&1 \
        | while IFS= read -r line; do
            echo "$line" >> "$LOG_DIR/tunnel_full.log"
            # 捕获 .lhr.life 的 URL
            if [[ "$line" =~ https://[^\ ]+\.lhr\.life ]]; then
                echo "$line" | grep -o 'https://[^ ]*\.lhr\.life' > "$URL_FILE"
                echo "[tunnel] 公网 URL: $(cat "$URL_FILE")"
            fi
          done &

    local pid=$!
    echo "$pid" > "$PID_FILE"

    # 等几秒看 URL 是否出来
    for i in 1 2 3 4 5 6 7 8; do
        sleep 1
        if [ -f "$URL_FILE" ] && [ -s "$URL_FILE" ]; then
            echo ""
            echo "============================================"
            echo "  ✅ 隧道已就绪"
            echo "  回调地址: $(cat "$URL_FILE")/v1/wework/callback"
            echo "============================================"
            echo ""
            echo "后台运行中 (pid=$pid)。查看状态: $0 status"
            return
        fi
    done

    echo "[tunnel] 等待超时，请检查: tail -f $LOG_DIR/tunnel_full.log"
}

# ── 停止 ──
stop_tunnel() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            echo "[tunnel] 已停止 (pid=$pid)"
        fi
        rm -f "$PID_FILE"
    fi
    pkill -f "localhost.run.*${LOCAL_PORT}" 2>/dev/null || true
    rm -f "$URL_FILE"
}

# ── 状态 ──
status_tunnel() {
    echo "=== 回调服务器 ==="
    if curl --noproxy '*' -s "http://127.0.0.1:${LOCAL_PORT}/health" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('✅ 运行中' if d.get('ok') else '❌')" 2>/dev/null; then
        :
    else
        echo "❌ 服务器未运行 → launchctl load ~/Library/LaunchAgents/ai.zplan.wecom-callback.plist"
    fi

    echo ""
    echo "=== 隧道 ==="
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "✅ 隧道运行中 (pid=$(cat "$PID_FILE"))"
        if [ -f "$URL_FILE" ] && [ -s "$URL_FILE" ]; then
            echo "   公网地址: $(cat "$URL_FILE")"
        fi
    else
        echo "❌ 隧道未运行 → $0 start"
    fi
}

# ── 显示 URL ──
show_url() {
    if [ -f "$URL_FILE" ] && [ -s "$URL_FILE" ]; then
        echo "回调地址: $(cat "$URL_FILE")/v1/wework/callback"
    else
        echo "❌ URL 未获取，请先: $0 start"
    fi
}

case "${1:-start}" in
    start)  start_tunnel ;;
    stop)   stop_tunnel ;;
    status) status_tunnel ;;
    url)    show_url ;;
    *)
        echo "用法: $0 {start|stop|status|url}"
        exit 1
        ;;
esac
