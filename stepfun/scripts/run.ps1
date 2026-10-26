# Start the Mini KB app on Windows (assumes seekdb already running on 127.0.0.1:2881).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path node_modules)) {
  npm install --registry=https://registry.npmmirror.com
}
Write-Host "Make sure seekdb is reachable (default 127.0.0.1:2881)."
node server/server.mjs
