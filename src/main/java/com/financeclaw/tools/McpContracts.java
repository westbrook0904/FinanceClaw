package com.financeclaw.tools;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.agent.CapabilityBinding;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.tool.AgentTool;
import io.agentscope.core.tool.mcp.McpClientWrapper;
import io.agentscope.core.tool.mcp.McpTool;
import io.modelcontextprotocol.spec.McpSchema;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Set;

/**
 * AgentScope 2.0.3 可表达的有效工具契约。保守包含描述和输出结构，不宣称保留 MCP 原始 JSON Schema 的全部关键字。 不删除 description/title
 * 等键，避免误删同名业务属性；数组顺序变化也视为需要新发布。
 */
public final class McpContracts {
    public static final String FORMAT = "mcp-sdk-effective/1";

    private McpContracts() {}

    /** 与 SDK 2.0.3 McpClientManager 的无 preset 注册映射一致；保留原生调用实现。 */
    public static McpTool nativeTool(McpSchema.Tool source, McpClientWrapper client) {
        if (source == null
                || source.name() == null
                || !source.name().matches("[A-Za-z][A-Za-z0-9_.-]{0,127}")) {
            throw new Blocked("mcp_contract_invalid");
        }
        return new McpTool(
                source.name(),
                source.description() == null ? "" : source.description(),
                McpTool.convertMcpSchemaToParameters(source.inputSchema(), Set.of()),
                source.outputSchema(),
                client,
                null,
                client.getName(),
                source.annotations() != null
                        && Boolean.TRUE.equals(source.annotations().readOnlyHint()));
    }

    public static String json(AgentTool tool) {
        var body = new LinkedHashMap<String, Object>();
        body.put("format", FORMAT);
        body.put("name", tool.getName());
        body.put("description", tool.getDescription());
        body.put("inputSchema", tool.getParameters());
        body.put("outputSchema", tool.getOutputSchema());
        body.put("strict", tool.getStrict());
        body.put("readOnly", tool.isReadOnly());
        String json;
        try {
            json = PayloadJson.canonical(body);
        } catch (RuntimeException invalid) {
            throw new Blocked("mcp_contract_invalid");
        }
        if (json.getBytes(StandardCharsets.UTF_8).length > CapabilityBinding.MAX_CONTRACT_BYTES) {
            throw new Blocked("mcp_discovery_limit_exceeded");
        }
        return json;
    }

    public static CapabilityBinding binding(String service, AgentTool tool) {
        String json = json(tool);
        var binding =
                new CapabilityBinding(
                        "mcp:" + service + "/" + tool.getName(),
                        "mcp",
                        tool.getName(),
                        List.of(scope(service)),
                        List.of("function:read_artifact_range"),
                        json,
                        PayloadDigest.sha256(json));
        binding.validate();
        return binding;
    }

    public static String scope(String service) {
        return "taibu".equals(service) ? "taibu:read" : "travel:read";
    }

    public static void requireMatch(CapabilityBinding expected, String service, AgentTool actual) {
        if (expected == null) {
            throw new Blocked("mcp_contract_changed");
        }
        var current = binding(service, actual);
        if (!current.equals(expected)) {
            throw new Blocked("mcp_contract_changed");
        }
    }
}
