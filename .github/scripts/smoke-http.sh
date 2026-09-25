#!/usr/bin/env bash
# Start an image the way it is meant to run (as the image's own user, which
# must not be root; read-only root filesystem; only /tmp writable) and require
# that its HTTP port answers.
# Any HTTP status counts: this proves the process starts and listens, not
# that it can reach a real backend.
#
# usage: smoke-http.sh IMAGE PORT [extra docker run args...]
set -euo pipefail
image=$1 port=$2
shift 2
name="smoke-$$"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$name" --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges -p "127.0.0.1:${port}:${port}" "$@" "$image" >/dev/null

for _ in $(seq 1 30); do
  if ! docker inspect -f '{{.State.Running}}' "$name" | grep -q true; then
    echo "container exited:"; docker logs "$name"; exit 1
  fi
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/" || true)
  if [[ "$code" != "000" ]]; then
    # Read the uid from the host: distroless images have no `id` binary.
    uid=$(ps -o uid= -p "$(docker inspect -f '{{.State.Pid}}' "$name")" | tr -d ' ')
    if [[ "$uid" == 0 || "$uid" == root ]]; then
      echo "the image's default user is root; it must run non-root"; exit 1
    fi
    echo "ok: port ${port} answered HTTP ${code}, running as uid ${uid}"
    exit 0
  fi
  sleep 1
done
echo "no HTTP answer on port ${port} after 30s:"; docker logs "$name"; exit 1
