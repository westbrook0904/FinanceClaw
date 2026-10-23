package com.financeclaw.infrastructure.agentscope.capability;

import io.agentscope.core.message.ToolResultBlock;
import io.agentscope.core.tool.*;
import java.util.Map;
import java.util.function.Function;
import reactor.core.publisher.Mono;

/** Business declarations handed to the existing SDK Toolkit through the governance boundary. */
public interface ToolContribution {
    void contribute(Registrar registrar);

    interface Registrar {
        void tool(
                String name,
                String description,
                Map<String, Object> schema,
                String scope,
                Function<ToolCallParam, Mono<ToolResultBlock>> action,
                InteractionHandler interaction);

        default void tool(
                String name,
                Map<String, Object> schema,
                String scope,
                Function<ToolCallParam, Mono<ToolResultBlock>> action) {
            tool(name, schema, scope, action, null);
        }

        default void tool(
                String name,
                Map<String, Object> schema,
                String scope,
                Function<ToolCallParam, Mono<ToolResultBlock>> action,
                InteractionHandler interaction) {
            tool(name, "Governed " + name, schema, scope, action, interaction);
        }
    }
}
