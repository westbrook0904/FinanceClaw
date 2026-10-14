#!/usr/bin/env sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ -z "${JAVA_HOME:-}" ]; then
    if [ -d "$task_root/.tools/jdk/Contents/Home" ]; then
        JAVA_HOME="$task_root/.tools/jdk/Contents/Home"
    else
        JAVA_HOME="$task_root/.tools/jdk"
    fi
    export JAVA_HOME
fi
if [ ! -x "$JAVA_HOME/bin/java" ]; then
    echo 'Run python3 scripts/bootstrap-jdk.py or set JAVA_HOME to Temurin 21.0.12+8.' >&2
    exit 1
fi
export MAVEN_USER_HOME="$task_root/.cache/maven-user"
cd "$task_root"
exec ./mvnw --settings "$task_root/.mvn/settings.xml" "-Dmaven.repo.local=$task_root/.cache/m2" "$@"
