#!/usr/bin/env bash
# The baked extensions must load offline, then the HTTP server must answer.
set -euo pipefail
docker run --rm --network none --entrypoint python "$1" -c "import duckdb; \
c = duckdb.connect(config={'extension_directory': '/opt/duckdb/extensions', 'autoinstall_known_extensions': False}); \
[c.execute(f'LOAD {e}') for e in ('httpfs', 'postgres', 'ducklake')]; print('extensions load offline')"
exec "$(dirname "$0")/../../.github/scripts/smoke-http.sh" "$1" 8000
