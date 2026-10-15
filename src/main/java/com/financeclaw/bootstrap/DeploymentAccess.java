package com.financeclaw.bootstrap;

import jakarta.servlet.*;
import jakarta.servlet.http.*;
import java.io.IOException;
import java.util.Set;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/** Background roles expose health only; profile mismatches fail during startup. */
@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
public class DeploymentAccess extends OncePerRequestFilter {
  private final String role;

  public DeploymentAccess(Environment env) {
    role = env.getProperty("financeclaw.deployment.role", "standalone");
    if (!Set.of("standalone", "api", "worker", "integrations").contains(role))
      throw new IllegalArgumentException("Unknown deployment role");
    if (!role.equals("standalone")) {
      boolean worker = env.getProperty("financeclaw.worker.enabled", Boolean.class, true);
      boolean integrations =
          env.getProperty("financeclaw.integrations.enabled", Boolean.class, true);
      if (worker != role.equals("worker") || integrations != role.equals("integrations"))
        throw new IllegalArgumentException("Deployment role does not match enabled consumers");
      if (env.getProperty("spring.flyway.enabled", Boolean.class, true))
        throw new IllegalArgumentException("Run migrations separately before deployment roles");
    }
  }

  @Override
  protected void doFilterInternal(
      HttpServletRequest request, HttpServletResponse response, FilterChain chain)
      throws ServletException, IOException {
    String path = request.getRequestURI();
    if ((role.equals("worker") || role.equals("integrations"))
        && !(path.equals("/actuator/health") || path.startsWith("/actuator/health/"))) {
      response.setStatus(404);
      return;
    }
    chain.doFilter(request, response);
  }
}
