#!/usr/bin/env bash
# The CLI and its tools must run, then the MCP server must answer HTTP.
# No database is needed: in stateful mode `gbrain serve` only starts when a
# session opens.
set -euo pipefail
docker run --rm --read-only --tmpfs /tmp "$1" \
  sh -c 'gbrain --version && git --version && ps --version && supergateway --help >/dev/null'
exec "$(dirname "$0")/../../.github/scripts/smoke-http.sh" "$1" 8080
