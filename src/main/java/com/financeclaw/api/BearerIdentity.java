package com.financeclaw.api;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import jakarta.servlet.*;
import jakarta.servlet.http.*;
import java.io.IOException;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * 将配置的 Bearer token 映射为可信主体，缺少凭据时拒绝所有业务接口。
 *
 * <p>主体和角色只来自服务端认证目录，绝不接受正文中的 tenant/subject 或角色；兼容 token 仅具有普通调用权限。
 */
@Component
public class BearerIdentity extends OncePerRequestFilter {
    public static final String ATTRIBUTE = BearerIdentity.class.getName();
    private static final String BEARER_PREFIX = "Bearer ";
    public static final String PRINCIPAL_ATTRIBUTE = ATTRIBUTE + ".principal";
    private final TrustedIdentityDirectory directory;

    @Autowired
    public BearerIdentity(Environment environment) {
        this(new TrustedIdentityDirectory(environment));
    }

    public BearerIdentity(TrustedIdentityDirectory directory) {
        this.directory = java.util.Objects.requireNonNull(directory);
    }

    public BearerIdentity(
            @Value("${financeclaw.auth.token:}") String token,
            @Value("${financeclaw.auth.tenant:local}") String tenant,
            @Value("${financeclaw.auth.subject:developer}") String subject) {
        this(new TrustedIdentityDirectory(token, tenant, subject, "[]"));
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
        var principal =
                header != null && header.startsWith(BEARER_PREFIX)
                        ? directory.authenticate(header.substring(BEARER_PREFIX.length()))
                        : null;
        if (principal == null) {
            response.setStatus(401);
            response.setContentType("application/json");
            response.getWriter().write("{\"error\":\"unauthorized\"}");
            return;
        }
        request.setAttribute(ATTRIBUTE, principal.owner());
        request.setAttribute(PRINCIPAL_ATTRIBUTE, principal);
        chain.doFilter(request, response);
    }

    public static Owner requirePermission(HttpServletRequest request, String permission) {
        var principal = request.getAttribute(PRINCIPAL_ATTRIBUTE);
        if (!(principal instanceof TrustedIdentityDirectory.Principal trusted)
                || !trusted.permissions().contains(permission)) throw new Problem(403, "forbidden");
        return trusted.owner();
    }
}
