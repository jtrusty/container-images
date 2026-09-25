#!/usr/bin/env bash
# The added tools must be on PATH and run.
set -euo pipefail
docker run --rm --entrypoint /bin/sh "$1" -c 'gh --version && tirith --version'
