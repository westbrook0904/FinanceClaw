package com.financeclaw.m5;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.bootstrap.DeploymentAccess;
import com.financeclaw.infrastructure.release.EnvironmentCredentials;
import com.financeclaw.infrastructure.release.EnvironmentReleaseSource;
import org.junit.jupiter.api.Test;
import org.springframework.mock.env.MockEnvironment;
import org.springframework.mock.web.*;

class DeploymentAccessTest {
  @Test
  void mcpCredentialsResolveFromSpringConfigWithoutEnteringReleaseSnapshots() {
    var env = new MockEnvironment().withProperty("ROLLINGGO_TOKEN", "m5-secret-fixture");
    var release = new ReleaseService(new EnvironmentReleaseSource(env));
    var credentials = new EnvironmentCredentials(env);
    assertThat(credentials.resolve("ROLLINGGO_TOKEN")).isEqualTo("m5-secret-fixture");
    assertThat(release.require(release.currentHash()).toString())
        .doesNotContain("m5-secret-fixture");
    assertThatThrownBy(() -> credentials.resolve("../unexpected"))
        .isInstanceOf(RuntimeException.class);
    assertThatThrownBy(() -> credentials.resolve("MISSING_TOKEN"))
        .isInstanceOf(RuntimeException.class);
  }

  private MockEnvironment role(String name) {
    return new MockEnvironment()
        .withProperty("financeclaw.deployment.role", name)
        .withProperty("spring.flyway.enabled", "false")
        .withProperty("financeclaw.worker.enabled", Boolean.toString(name.equals("worker")))
        .withProperty(
            "financeclaw.integrations.enabled", Boolean.toString(name.equals("integrations")));
  }

  @Test
  void backgroundRolesCannotAcceptBusinessRequestsOrFeishuCallbacks() throws Exception {
    for (String name : new String[] {"worker", "integrations"}) {
      var filter = new DeploymentAccess(role(name));
      for (String path :
          new String[] {
            "/v1/conversations", "/api/channels/feishu/financeclaw/callback", "/actuator/env"
          }) {
        var response = new MockHttpServletResponse();
        var chain = new MockFilterChain();
        filter.doFilter(new MockHttpServletRequest("POST", path), response, chain);
        assertThat(response.getStatus()).isEqualTo(404);
        assertThat(chain.getRequest()).isNull();
      }
      var chain = new MockFilterChain();
      filter.doFilter(
          new MockHttpServletRequest("GET", "/actuator/health/readiness"),
          new MockHttpServletResponse(),
          chain);
      assertThat(chain.getRequest()).isNotNull();
    }
  }

  @Test
  void misconfiguredConsumersAndMigrationPrivilegesFailStartup() {
    assertThatThrownBy(
            () ->
                new DeploymentAccess(
                    role("api").withProperty("financeclaw.worker.enabled", "true")))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(
            () ->
                new DeploymentAccess(role("worker").withProperty("spring.flyway.enabled", "true")))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(() -> new DeploymentAccess(role("unknown")))
        .isInstanceOf(IllegalArgumentException.class);
  }

  @Test
  void apiAndStandaloneContinueThroughExistingIdentityFilter() throws Exception {
    for (String name : new String[] {"api", "standalone"}) {
      var chain = new MockFilterChain();
      new DeploymentAccess(role(name))
          .doFilter(
              new MockHttpServletRequest("POST", "/v1/conversations"),
              new MockHttpServletResponse(),
              chain);
      assertThat(chain.getRequest()).isNotNull();
    }
  }
}
