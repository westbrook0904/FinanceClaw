#!/bin/sh
set -eu
role=${1:-standalone}
if [ "$#" -gt 0 ]; then shift; fi
case "$role" in
  standalone)
    exec java -jar /app/financeclaw.jar "--spring.profiles.active=$role" "$@" ;;
  memory-worker)
    exec java -Dloader.main=com.financeclawmemory.MemoryWorkerApplication -cp /app/financeclaw.jar org.springframework.boot.loader.launch.PropertiesLauncher "$@" ;;
  init-db)
    exec java -Dloader.main=com.financeclawsetup.DatabaseInitializationApplication -cp /app/financeclaw.jar org.springframework.boot.loader.launch.PropertiesLauncher "$@" ;;
  privacy-recovery)
    exec java -Dloader.main=com.financeclawprivacy.PrivacyRecoveryApplication -cp /app/financeclaw.jar org.springframework.boot.loader.launch.PropertiesLauncher "$@" ;;
  *) echo 'Expected standalone, memory-worker, init-db or privacy-recovery' >&2; exit 2 ;;
esac
