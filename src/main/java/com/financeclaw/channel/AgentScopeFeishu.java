package com.financeclaw.channel;

import io.agentscope.core.message.Msg;
import io.agentscope.core.message.MsgRole;
import io.agentscope.extensions.channel.feishu.*;
import io.agentscope.harness.agent.gateway.channel.OutboundAddress;
import java.time.Duration;
import java.util.List;
import org.springframework.stereotype.Component;

/** All provider authentication, serialization and HTTP sending belong to the native SDK. */
@Component
public class AgentScopeFeishu {
  private final FeishuOutboundClient outbound;

  public AgentScopeFeishu(FeishuSettings settings) {
    if (!settings.enabled) {
      outbound = null;
      return;
    }
    var properties =
        FeishuChannelProperties.from(
            "financeclaw",
            java.util.Map.of(
                "appId",
                settings.appId,
                "appSecret",
                settings.appSecret,
                "encryptKey",
                settings.encryptKey,
                "verificationToken",
                settings.token,
                "apiBase",
                settings.baseUrl));
    outbound =
        new FeishuOutboundClient(
            properties.apiBase(),
            new FeishuAccessTokenProvider(
                properties.apiBase(), properties.appId(), properties.appSecret()));
  }

  public void send(String chat, String text) {
    if (outbound == null) throw new IllegalStateException("feishu_disabled");
    outbound
        .send(
            OutboundAddress.direct("financeclaw", chat),
            List.of(
                Msg.builder()
                    .name("financeclaw")
                    .role(MsgRole.ASSISTANT)
                    .textContent(text)
                    .build()))
        .block(Duration.ofSeconds(25));
    // 2.0.3 returns Mono<Void> and only logs nonzero business codes: completion is NOT a receipt.
  }
}
