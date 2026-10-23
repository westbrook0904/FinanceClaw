package com.financeclaw.m4;

import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.DockerImageName;

final class M4Database {
    static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>(
                            DockerImageName.parse("pgvector/pgvector:0.8.1-pg16")
                                    .asCompatibleSubstituteFor("postgres"))
                    .withDatabaseName("m4")
                    .withUsername("m4")
                    .withPassword("test-only");

    static {
        PG.start();
    }

    private M4Database() {}
}
