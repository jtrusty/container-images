#!/usr/bin/env bash
# The toolchain must work for the image's default (unprivileged) user, with
# Chromium's system libraries present.
set -euo pipefail
docker run --rm --entrypoint /bin/bash "$1" -c '
  set -e
  [ "$(id -u)" != 0 ]
  node --version && npm --version && pnpm --version && uv --version && uv python find 3.10
  go version && kubectl version --client && git --version
  for lib in libnss3.so libgbm.so.1 libasound.so.2 libatomic.so.1; do ldconfig -p | grep -q "$lib"; done
  echo ok'
