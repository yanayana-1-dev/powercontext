#!/usr/bin/env bash
# Start the Mini KB app (assumes seekdb already running on 127.0.0.1:2881).
set -e
cd "$(dirname "$0")/.."
if [ ! -d node_modules ]; then
  npm install --registry=https://registry.npmmirror.com
fi
echo "Make sure seekdb is reachable (default 127.0.0.1:2881)."
exec node server/server.mjs
