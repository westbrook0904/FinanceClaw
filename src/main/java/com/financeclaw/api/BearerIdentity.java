package com.financeclaw.api;

import com.financeclaw.domain.Task.Owner;
import jakarta.servlet.*;
import jakarta.servlet.http.*;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * M1 single configured principal. Fail closed with no token; never accept identity from request
 * fields.
 */
@Component
public class BearerIdentity extends OncePerRequestFilter {
  public static final String ATTRIBUTE = BearerIdentity.class.getName();
  private final String token;
  private final Owner owner;

  public BearerIdentity(
      @Value("${financeclaw.auth.token:}") String token,
      @Value("${financeclaw.auth.tenant:local}") String tenant,
      @Value("${financeclaw.auth.subject:developer}") String subject) {
    this.token = token;
    this.owner = new Owner(tenant, subject);
  }

  @Override
  protected boolean shouldNotFilter(HttpServletRequest request) {
    return !request.getRequestURI().startsWith("/v1/");
  }

  @Override
  protected void doFilterInternal(
      HttpServletRequest request, HttpServletResponse response, FilterChain chain)
      throws ServletException, IOException {
    String header = request.getHeader("Authorization");
    if (token.isBlank()
        || header == null
        || !header.startsWith("Bearer ")
        || !MessageDigest.isEqual(
            token.getBytes(StandardCharsets.UTF_8),
            header.substring(7).getBytes(StandardCharsets.UTF_8))) {
      response.setStatus(401);
      response.setContentType("application/json");
      response.getWriter().write("{\"error\":\"unauthorized\"}");
      return;
    }
    request.setAttribute(ATTRIBUTE, owner);
    chain.doFilter(request, response);
  }
}
