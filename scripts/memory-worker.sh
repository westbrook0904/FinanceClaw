#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# The Boot launcher selects a separate application with only memory mappers and model adapters.
task_java="${JAVA_HOME:-.tools/jdk/Contents/Home}/bin/java"
if [[ ! -x "$task_java" ]]; then
  task_java=".tools/jdk/bin/java"
fi
exec "$task_java" \
  -Dloader.main=com.financeclawmemory.MemoryWorkerApplication \
  -cp target/financeclaw-java-0.1.0-SNAPSHOT.jar \
  org.springframework.boot.loader.launch.PropertiesLauncher "$@"
