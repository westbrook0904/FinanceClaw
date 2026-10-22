FROM eclipse-temurin:21.0.12_8-jre-jammy@sha256:61d6c7b34d36aee3f45d043101259f97f3c6d428dc2a6f75513789983c5e254f
WORKDIR /app
RUN groupadd --gid 10001 financeclaw && useradd --uid 10001 --gid 10001 --no-create-home financeclaw
COPY --chown=10001:10001 target/financeclaw-java-0.1.0-SNAPSHOT.jar /app/financeclaw.jar
COPY --chmod=755 ops/container-entrypoint.sh /app/entrypoint.sh
USER 10001:10001
ENV JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=70 -XX:+ExitOnOutOfMemoryError"
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=8s --start-period=60s --retries=6 CMD curl --fail --silent http://127.0.0.1:8080/actuator/health/readiness || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["standalone"]
