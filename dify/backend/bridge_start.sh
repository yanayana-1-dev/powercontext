#!/bin/bash
# 雾隐镇编年史 · 桥接服务启动（WSL/Linux）
# 用法: bash /mnt/d/AAAK/game/oceanbase/03-ecosystem/dify/backend/bridge_start.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
fi
# 读取 .env（如存在）
if [ -f .env ]; then set -a; source .env; set +a; fi
exec uvicorn bridge:app --host 0.0.0.0 --port "${BRIDGE_PORT:-9010}" --timeout-keep-alive 120
