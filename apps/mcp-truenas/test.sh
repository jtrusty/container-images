#!/usr/bin/env bash
# Starts the HTTP server with a placeholder backend; nothing is contacted.
set -euo pipefail
exec "$(dirname "$0")/../../.github/scripts/smoke-http.sh" "$1" 8080 --user 65534:65534 \
  -e TRUENAS_MCP_LISTEN=:8080 -e TRUENAS_MCP_TARGET=127.0.0.1:9 \
  -e TRUENAS_MCP_ALLOW_PLAINTEXT=true
