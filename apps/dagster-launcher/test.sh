#!/usr/bin/env bash
# The self-test drives the launcher against a fake Dagster API (loopback only),
# then the server must start and answer HTTP with a placeholder backend.
set -euo pipefail
docker run --rm --read-only --tmpfs /tmp --network none --entrypoint python3 "$1" /app/selftest.py
config=$(mktemp)
trap 'rm -f "$config"' EXIT
echo '{"owner":"smoke","location":"example","targets":{"t":{"job":"j"}}}' > "$config"
chmod 644 "$config"
"$(dirname "$0")/../../.github/scripts/smoke-http.sh" "$1" 8080 \
  -v "$config:/config/launcher.json:ro" \
  -e LAUNCHER_KEYS=smoke -e DAGSTER_GRAPHQL_URL=http://127.0.0.1:9/graphql
