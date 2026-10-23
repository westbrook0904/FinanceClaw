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
 * 将配置的 Bearer token 映射为可信主体，缺少凭据时拒绝所有业务接口。
 *
 * <p>主体只来自服务端认证配置，绝不接受正文中的 tenant/subject。当前配置用于单主体部署， 多用户生产认证需要替换此边界，而不是复用一个 token 让客户端自行选择主体。
 */
@Component
public class BearerIdentity extends OncePerRequestFilter {
    public static final String ATTRIBUTE = BearerIdentity.class.getName();
    private static final String BEARER_PREFIX = "Bearer ";
    private final byte[] tokenBytes;
    private final Owner owner;

    public BearerIdentity(
            @Value("${financeclaw.auth.token:}") String token,
            @Value("${financeclaw.auth.tenant:local}") String tenant,
            @Value("${financeclaw.auth.subject:developer}") String subject) {
        this.tokenBytes = token.isBlank() ? new byte[0] : token.getBytes(StandardCharsets.UTF_8);
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
        if (tokenBytes.length == 0
                || header == null
                || !header.startsWith(BEARER_PREFIX)
                || !MessageDigest.isEqual(
                        tokenBytes,
                        header.substring(BEARER_PREFIX.length())
                                .getBytes(StandardCharsets.UTF_8))) {
            response.setStatus(401);
            response.setContentType("application/json");
            response.getWriter().write("{\"error\":\"unauthorized\"}");
            return;
        }
        request.setAttribute(ATTRIBUTE, owner);
        chain.doFilter(request, response);
    }
}
