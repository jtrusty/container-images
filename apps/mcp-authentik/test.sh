#!/usr/bin/env bash
# The server checks its backend at start-up (GET /api/v3/admin/version/) and
# exits if it can't reach it, so run a stub backend on a private network that
# answers with a version. Nothing outside the runner is contacted.
set -euo pipefail
image=$1
net="smoke-net-$$" stub="authentik-stub-$$"
cleanup() { docker rm -f "$stub" >/dev/null 2>&1 || true; docker network rm "$net" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker network create "$net" >/dev/null
docker run -d --name "$stub" --network "$net" \
  docker.io/library/python:3.13-alpine@sha256:79e7a9b9ff1cbceff819f856fb374477792a5967759d94df266de7b7b4120e6f \
  python -c '
import json, http.server
body = json.dumps({"version_current": "0.0.0-stub", "version_latest": "0.0.0-stub",
                   "version_latest_valid": True, "build_hash": "", "outdated": False,
                   "outpost_outdated": False}).encode()
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
http.server.HTTPServer(("0.0.0.0", 8000), H).serve_forever()
' >/dev/null

"$(dirname "$0")/../../.github/scripts/smoke-http.sh" "$image" 3000 --user 1000:1000 --network "$net" \
  -e MCP_TRANSPORT=http -e MCP_PORT=3000 -e MCP_HOST=0.0.0.0 \
  -e AUTHENTIK_URL="http://${stub}:8000" -e AUTHENTIK_TOKEN=placeholder \
  -e AUTHENTIK_ACCESS_TIER=read-only
