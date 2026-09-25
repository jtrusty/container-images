#!/usr/bin/env bash
# Starts the HTTP server with a placeholder backend; nothing is contacted.
set -euo pipefail
exec "$(dirname "$0")/../../.github/scripts/smoke-http.sh" "$1" 3000 --user 1000:1000 \
  -e MCP_TRANSPORT=http -e MCP_PORT=3000 -e MCP_HOST=0.0.0.0 \
  -e AUTHENTIK_URL=http://127.0.0.1:9 -e AUTHENTIK_TOKEN=placeholder \
  -e AUTHENTIK_ACCESS_TIER=read-only
