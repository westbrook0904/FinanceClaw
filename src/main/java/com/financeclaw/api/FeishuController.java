package com.financeclaw.api;

import com.financeclaw.channel.*;
import com.financeclaw.domain.Task.Problem;
import jakarta.servlet.http.HttpServletRequest;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import org.springframework.web.bind.annotation.*;

/**
 * 飞书回调、渠道绑定和投递观测入口。
 *
 * <p>回调先验证签名并限制正文大小，再交给渠道服务做幂等去重；普通业务请求依赖统一认证过滤器。
 */
@RestController
public class FeishuController {
    private final FeishuIngress ingress;
    private final FeishuService service;

    public FeishuController(FeishuIngress ingress, FeishuService service) {
        this.ingress = ingress;
        this.service = service;
    }

    @GetMapping("/v1/feishu/deliveries")
    public Object deliveries(HttpServletRequest r) {
        return service.deliveries(TaskController.owner(r));
    }

    @PostMapping("/api/channels/feishu/financeclaw/callback")
    public Object callback(HttpServletRequest r) throws IOException {
        byte[] bytes = r.getInputStream().readNBytes(FeishuIngress.MAX_CALLBACK_BYTES + 1);
        if (bytes.length > FeishuIngress.MAX_CALLBACK_BYTES) {
            throw new Problem(413, "callback_too_large");
        }
        var root =
                ingress.verify(
                        new String(bytes, StandardCharsets.UTF_8),
                        r.getHeader("X-Lark-Request-Timestamp"),
                        r.getHeader("X-Lark-Request-Nonce"),
                        r.getHeader("X-Lark-Signature"));
        return service.receive(root);
    }

    @PostMapping("/v1/conversations/{conversation}/feishu-link")
    public Map<String, Object> link(HttpServletRequest r, @PathVariable String conversation) {
        return Map.of(
                "command",
                "/bind " + service.link(TaskController.owner(r), conversation),
                "expires_in_seconds",
                300);
    }
}
