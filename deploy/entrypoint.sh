#!/bin/sh
set -eu
. /app/financeclaw/native-env.sh
case "${FINANCECLAW_PROCESS_ROLE:?Set api, worker or integrations}" in
  api)
    [ "${N_JOBS_PER_WORKER:?API requires N_JOBS_PER_WORKER=0}" = 0 ] || {
      echo "API must not execute native jobs" >&2; exit 1;
    }
    exec /storage/entrypoint.sh "$@"
    ;;
  worker)
    [ "${N_JOBS_PER_WORKER:?Worker requires a positive job count}" -gt 0 ] || exit 1
    exec /storage/queue_entrypoint.sh "$@"
    ;;
  integrations)
    exec python -m financeclaw.integrations "$@"
    ;;
  *) echo "Unknown FinanceClaw process role" >&2; exit 1 ;;
esac
