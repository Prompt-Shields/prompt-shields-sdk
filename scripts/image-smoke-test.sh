#!/usr/bin/env bash
# Smoke-test a built gateway or collector image under the restrictions the
# Helm chart applies: read-only root filesystem (tmpfs /tmp only), all
# capabilities dropped, no privilege escalation, the image's own non-root
# user. Used by .github/workflows/images.yml; runs locally too.
#
#   scripts/image-smoke-test.sh gateway   <image>
#   scripts/image-smoke-test.sh collector <image>
set -euo pipefail

kind="${1:?usage: $0 gateway|collector <image>}"
image="${2:?usage: $0 gateway|collector <image>}"
name="ps-smoke-${kind}-$$"
restricted=(--read-only --tmpfs "/tmp:rw,noexec,nosuid,size=64m"
  --cap-drop ALL --security-opt no-new-privileges)

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

user="$(docker image inspect -f '{{.Config.User}}' "$image")"
if [ "$user" != "10001:10001" ]; then
  echo "::error::$image runs as '$user', expected 10001:10001"
  exit 1
fi

wait_http() { # url expected-status
  local code=""
  for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$1" || true)"
    [ "$code" = "$2" ] && return 0
    sleep 1
  done
  echo "::error::$1 answered '$code', expected $2"
  docker logs "$name" || true
  return 1
}

case "$kind" in
  gateway)
    docker run -d --name "$name" "${restricted[@]}" -p 127.0.0.1:18787:8787 "$image"
    wait_http http://127.0.0.1:18787/v1/health 200
    # SIGTERM drains and exits 0 (src/psShutdown.ts).
    docker stop -t 30 "$name" >/dev/null
    status="$(docker inspect -f '{{.State.ExitCode}}' "$name")"
    if [ "$status" != "0" ]; then
      echo "::error::gateway exited $status on SIGTERM"
      docker logs "$name" || true
      exit 1
    fi
    ;;
  collector)
    # No database: liveness answers, readiness reports 503.
    docker run -d --name "$name" "${restricted[@]}" -p 127.0.0.1:18000:8000 \
      -e PS_DATABASE_URL=postgresql+asyncpg://nobody:x@127.0.0.1:1/none \
      -e PS_READYZ_TIMEOUT_SECONDS=1 \
      -e PS_COLLECTOR_METRICS_ENABLED=true \
      "$image"
    wait_http http://127.0.0.1:18000/partner/v1/health 200
    wait_http http://127.0.0.1:18000/readyz 503
    wait_http http://127.0.0.1:18000/metrics 200
    # Migrations run from the same image (alembic reads its scripts only).
    docker run --rm "${restricted[@]}" "$image" alembic -c packages/db/alembic.ini heads
    # The worker entrypoint exists and refuses to idle with nothing enabled.
    set +e
    docker run --rm "${restricted[@]}" "$image" python -m collector.workers
    status=$?
    set -e
    if [ "$status" != "2" ]; then
      echo "::error::python -m collector.workers exited $status, expected 2"
      exit 1
    fi
    ;;
  *)
    echo "unknown image kind: $kind" >&2
    exit 2
    ;;
esac
echo "$kind image smoke test passed"
