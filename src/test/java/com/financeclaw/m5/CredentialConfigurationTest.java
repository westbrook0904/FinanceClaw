package com.financeclaw.m5;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.infrastructure.release.EnvironmentCredentials;
import com.financeclaw.infrastructure.release.EnvironmentReleaseSource;
import org.junit.jupiter.api.Test;
import org.springframework.mock.env.MockEnvironment;

class CredentialConfigurationTest {
    @Test
    void mcpCredentialsResolveFromSpringConfigWithoutEnteringReleaseSnapshots() {
        var env = new MockEnvironment().withProperty("ROLLINGGO_TOKEN", "m5-secret-fixture");
        var release =
                com.financeclaw.fixture.TestReleases.create(new EnvironmentReleaseSource(env));
        var credentials = new EnvironmentCredentials(env);
        assertThat(credentials.resolve("ROLLINGGO_TOKEN")).isEqualTo("m5-secret-fixture");
        assertThat(release.require(release.currentHash()).toString())
                .doesNotContain("m5-secret-fixture");
        assertThatThrownBy(() -> credentials.resolve("../unexpected"))
                .isInstanceOf(RuntimeException.class);
        assertThatThrownBy(() -> credentials.resolve("MISSING_TOKEN"))
                .isInstanceOf(RuntimeException.class);
    }
}
