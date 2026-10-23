package com.financeclaw.infrastructure.agentscope;

import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.ToolUseBlock;
import java.util.Map;

/**
 * 构造具有统一 callId、工具名和输入内容的 AgentScope ToolUseBlock。 输入同时保留结构化 Map 和规范化 JSON
 * 文本，便于工具派发及动作摘要使用一致数据。调用身份来自上层执行链，本工具方法不生成授权或持久检查点。
 */
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
