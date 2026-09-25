#!/usr/bin/env bash
# Starts the HTTP server with a placeholder controller; nothing is contacted.
set -euo pipefail
exec "$(dirname "$0")/../../.github/scripts/smoke-http.sh" "$1" 3000 \
  -e UNIFI_MCP_HTTP_ENABLED=true -e UNIFI_MCP_HTTP_TRANSPORT=streamable-http \
  -e UNIFI_MCP_HOST=0.0.0.0 -e UNIFI_MCP_PORT=3000 \
  -e UNIFI_MCP_ENABLE_DNS_REBINDING_PROTECTION=false \
  -e UNIFI_HOST=127.0.0.9 -e UNIFI_USERNAME=placeholder -e UNIFI_PASSWORD=placeholder
