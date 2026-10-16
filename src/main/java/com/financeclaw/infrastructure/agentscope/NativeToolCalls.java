package com.financeclaw.infrastructure.agentscope;

import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.ToolUseBlock;
import java.util.Map;

public final class NativeToolCalls {
  private NativeToolCalls() {}

  public static ToolUseBlock call(String id, String name, Map<String, Object> input) {
    return ToolUseBlock.builder()
        .id(id)
        .name(name)
        .input(input)
        .content(PayloadJson.canonical(input))
        .build();
  }
}
