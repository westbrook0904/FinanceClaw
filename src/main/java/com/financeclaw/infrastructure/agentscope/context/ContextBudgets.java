package com.financeclaw.infrastructure.agentscope.context;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.release.ModelProfile;
import io.agentscope.core.message.Msg;
import io.agentscope.core.model.ToolSchema;
import io.agentscope.core.util.JsonUtils;
import io.agentscope.extensions.model.openai.formatter.OpenAIChatFormatter;
import java.nio.charset.StandardCharsets;
import java.util.List;
import org.springframework.core.env.Environment;

/**
 * 模型请求各部分的保守 UTF-8 字节预算。
 *
 * <p>currentInput 和 toolResult 是 workContext 的子预算，长期记忆只占 system 中独立的一块。 这里为上下文压缩预留余量；最终 HTTP JSON
 * 仍由 BudgetedTransport 做完整检查，不能用局部预算替代。
 */
public record ContextBudgets(
        int systemBytes,
        int longMemoryBytes,
        int workContextBytes,
        int currentInputBytes,
        int toolSchemaBytes,
        int toolResultBytes,
        int summaryBytes,
        int compactAtPercent,
        int retainedRounds) {
    public ContextBudgets {
        if (systemBytes < 1
                || longMemoryBytes < 1
                || workContextBytes < 1024
                || currentInputBytes < 1
                || toolSchemaBytes < 1
                || toolResultBytes < 4096
                || summaryBytes < 256
                || compactAtPercent < 1
                || compactAtPercent > 95
                || retainedRounds < 1
                || summaryBytes >= workContextBytes) {
            throw new IllegalArgumentException("Invalid context budgets");
        }
    }

    public static ContextBudgets defaults() {
        // 当前内置工具含五项紫微 Schema，实际发送约 40 KiB；分区上限需容纳正常发布。
        return new ContextBudgets(8192, 16384, 24576, 8192, 49152, 8192, 2048, 80, 1);
    }

    public static ContextBudgets from(Environment env) {
        var d = defaults();
        return new ContextBudgets(
                value(env, "system-bytes", d.systemBytes),
                value(env, "long-memory-bytes", d.longMemoryBytes),
                value(env, "work-context-bytes", d.workContextBytes),
                value(env, "current-input-bytes", d.currentInputBytes),
                value(env, "tool-schema-bytes", d.toolSchemaBytes),
                value(env, "tool-result-bytes", d.toolResultBytes),
                value(env, "summary-bytes", d.summaryBytes),
                value(env, "compact-at-percent", d.compactAtPercent),
                value(env, "retained-rounds", d.retainedRounds));
    }

    private static int value(Environment env, String name, int fallback) {
        return env.getProperty("financeclaw.context." + name, Integer.class, fallback);
    }

    public void checkAdmission(ModelProfile model, String memory, String input) {
        require(bytes(memory) <= longMemoryBytes, "long_memory_budget_exceeded");
        require(bytes(input) <= currentInputBytes, "current_input_budget_exceeded");
        require((long) bytes(memory) + bytes(input) < capacity(model), "context_budget_exceeded");
    }

    /** 固定部分不可丢弃；剩余容量收紧工作上下文上限，工具 Schema 一并占用模型窗口。 */
    public int effectiveWorkBytes(
            ModelProfile model, List<Msg> system, String memory, List<ToolSchema> tools) {
        int systemSize = bytes(system.stream().map(Msg::getTextContent).reduce("", String::concat));
        require(systemSize - bytes(memory) <= systemBytes, "system_context_budget_exceeded");
        // 使用与 ModelFactory 相同的 SDK Formatter，避免把内部 outputSchema 等字段当作输入。
        var formatter = new OpenAIChatFormatter();
        var request = formatter.buildRequest(model.name(), List.of(), true);
        formatter.applyTools(request, tools);
        int schemas = bytes(JsonUtils.getJsonCodec().toJson(request.getTools()));
        require(schemas <= toolSchemaBytes, "tool_schema_budget_exceeded");
        // 额外为role/name/call-id及HTTP JSON字段留出余量。传输层在发送前还会独立验算。
        long available = capacity(model) - systemSize - schemas - 1024;
        require(available >= 1024, "context_budget_exceeded");
        return (int) Math.min(workContextBytes, available);
    }

    public static long capacity(ModelProfile model) {
        return Math.min(model.inputLimit(), model.window() - model.outputReserve())
                - model.margin();
    }

    public static int bytes(String text) {
        return text == null ? 0 : text.getBytes(StandardCharsets.UTF_8).length;
    }

    private static void require(boolean condition, String code) {
        if (!condition) throw new Blocked(code);
    }
}
