#!/bin/bash
# run.sh — 一键启动：构建(如需) → 后端服务 → 原生 WebView 窗口
# 用法: ./run.sh [--web] [设备/镜像路径]
#   --web        不开原生窗口，用系统默认浏览器打开
#   路径参数     启动后直接打开指定块设备或镜像文件
set -e
cd "$(dirname "$0")"

WEB=0
OPEN_PATH=""
for arg in "$@"; do
  case "$arg" in
    --web) WEB=1 ;;
    *) OPEN_PATH=$arg ;;
  esac
done

# 依赖检查与构建
if [ ! -f build/libe2fsbridge.dylib ] || [ src/bridge.c -nt build/libe2fsbridge.dylib ]; then
  echo "==> 构建桥接库…"
  make build/libe2fsbridge.dylib
fi

if [ "$WEB" = 0 ]; then
  if [ ! -x build/E2fsBrowser ] || [ webview/main.swift -nt build/E2fsBrowser ]; then
    echo "==> 编译 WebView 窗口…"
    make build/E2fsBrowser
  fi
fi

# 启动后端
LOG=build/server.log
python3 backend/server.py ${OPEN_PATH:+--open "$OPEN_PATH"} >"$LOG" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null' EXIT

# 等待端口就绪
PORT=""
for i in $(seq 1 50); do
  PORT=$(grep -m1 -o 'E2FS_BROWSER_PORT=[0-9]*' "$LOG" 2>/dev/null | cut -d= -f2)
  [ -n "$PORT" ] && break
  if ! kill -0 $SERVER_PID 2>/dev/null; then
    echo "后端启动失败:"; cat "$LOG"; exit 1
  fi
  sleep 0.1
done
if [ -z "$PORT" ]; then
  echo "等待后端超时:"; cat "$LOG"; exit 1
fi

URL="http://127.0.0.1:$PORT/"
echo "==> 后端就绪: $URL  (PID $SERVER_PID, 日志 $LOG)"

# 打开前端窗口
if [ "$WEB" = 1 ]; then
  echo "==> 使用系统浏览器打开（Ctrl-C 退出并关闭后端）"
  ( sleep 0.5; open "$URL" ) &
  wait $SERVER_PID
else
  echo "==> 打开原生窗口（关闭窗口即退出）"
  ./build/E2fsBrowser "$URL"
fi
