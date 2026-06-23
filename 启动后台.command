#!/bin/zsh

set -u

PROJECT_DIR="/Users/gxunn/Documents/test1/self-media-review"
PORT="8000"

cd "$PROJECT_DIR" || exit 1

existing_pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1)"
if [[ -n "$existing_pid" ]]; then
  process_cwd="$(lsof -a -p "$existing_pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
  if [[ "$process_cwd" == "$PROJECT_DIR" ]]; then
    echo "正在关闭旧版后台（进程 $existing_pid）……"
    kill "$existing_pid"
    for _ in {1..30}; do
      lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
      sleep 0.2
    done
  else
    echo "端口 $PORT 正被其他程序占用，为避免误关程序，本次没有继续。"
    echo "占用程序目录：${process_cwd:-未知}"
    echo ""
    read "?按回车键关闭窗口……"
    exit 1
  fi
fi

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo "没有找到项目运行环境，请先按 README 完成安装。"
  echo ""
  read "?按回车键关闭窗口……"
  exit 1
fi

echo "正在启动自媒体复盘后台……"
echo "启动成功后，请打开：http://127.0.0.1:8000"
echo "这个窗口需要保持打开。"
echo ""

exec "$PROJECT_DIR/.venv/bin/python" -m uvicorn app.main:app --reload --host 127.0.0.1 --port "$PORT"
