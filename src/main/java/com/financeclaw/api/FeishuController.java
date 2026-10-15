package com.financeclaw.api;

import com.financeclaw.channel.*;
import com.financeclaw.domain.Task.Problem;
import jakarta.servlet.http.HttpServletRequest;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import org.springframework.web.bind.annotation.*;

@RestController
public class FeishuController {
  private final FeishuIngress ingress;
  private final FeishuService service;
  private final FeishuDelivery delivery;

  public FeishuController(FeishuIngress ingress, FeishuService service, FeishuDelivery delivery) {
    this.ingress = ingress;
    this.service = service;
    this.delivery = delivery;
  }

  @GetMapping("/v1/feishu/deliveries")
  public Object deliveries(HttpServletRequest r) {
    return delivery.list(TaskController.owner(r));
  }

  @PostMapping("/api/channels/feishu/financeclaw/callback")
  public Object callback(HttpServletRequest r) throws IOException {
    byte[] bytes = r.getInputStream().readNBytes(65537);
    if (bytes.length > 65536) throw new Problem(413, "callback_too_large");
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
