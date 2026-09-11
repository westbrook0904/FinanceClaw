#!/bin/sh
set -eu
# The probe replaces only synthetic quote data; product API and policies remain the image code.
. /app/financeclaw/native-env.sh
export LANGSERVE_GRAPHS='{"finance_agent":"/project/experiments/stage10/product_fixture.py:finance_agent"}'
case "$FINANCECLAW_PROCESS_ROLE" in
  api)
    [ "$N_JOBS_PER_WORKER" = 0 ]
    exec /storage/entrypoint.sh
    ;;
  worker)
    [ "$N_JOBS_PER_WORKER" -gt 0 ]
    exec /storage/queue_entrypoint.sh
    ;;
  *) exit 1 ;;
esac
