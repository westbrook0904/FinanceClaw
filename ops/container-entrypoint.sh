#!/bin/sh
set -eu
role=${1:-api}
if [ "$#" -gt 0 ]; then shift; fi
case "$role" in
  api|worker|integrations)
    exec java -jar /app/financeclaw.jar "--spring.profiles.active=$role" "$@" ;;
  memory-worker)
    exec java -Dloader.main=com.financeclawmemory.MemoryWorkerApplication -cp /app/financeclaw.jar org.springframework.boot.loader.launch.PropertiesLauncher "$@" ;;
  migrate)
    exec java -Dloader.main=com.financeclawmigrate.MigrationApplication -cp /app/financeclaw.jar org.springframework.boot.loader.launch.PropertiesLauncher "$@" ;;
  *) echo 'Expected api, worker, integrations, memory-worker or migrate' >&2; exit 2 ;;
esac
