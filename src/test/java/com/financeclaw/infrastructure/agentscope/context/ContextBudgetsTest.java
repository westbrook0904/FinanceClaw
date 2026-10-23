package com.financeclaw.infrastructure.agentscope.context;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.mock;

import com.financeclaw.app.ziwei.ZiweiService;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.release.ModelProfile;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.infrastructure.agentscope.capability.*;
import io.agentscope.core.ReActAgent;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.skill.repository.ClasspathSkillRepository;
import io.agentscope.core.tool.*;
import io.agentscope.core.util.JsonUtils;
import io.agentscope.extensions.model.openai.formatter.OpenAIChatFormatter;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.mock.env.MockEnvironment;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/** 使用真实业务声明、SDK Skill 注册和 Formatter，防止默认预算阻断正常发布。 */
class ContextBudgetsTest {
    private static List<ToolSchema> tools;

    @BeforeAll
    static void captureAllBuiltInSchemasIncludingTheNativeSkillTool() throws Exception {
        var toolkit = new Toolkit();
        ToolContribution.Registrar registrar =
                (name, description, schema, scope, action, interaction) ->
                        toolkit.registerAgentTool(
                                new ToolBase(
                                        ToolBase.builder()
                                                .name(name)
                                                .description(description)
                                                .inputSchema(schema)) {
                                    @Override
                                    public Mono<ToolResultBlock> callAsync(ToolCallParam param) {
                                        return Mono.error(
                                                new AssertionError(
                                                        "Schema regression must not call tools"));
                                    }
                                });
        new LocalToolContribution(null, null, null).contribute(registrar);
        var capabilities =
                new BusinessCapabilities(
                        null, null, null, null, null, null, mock(ZiweiService.class));
        capabilities.contribute(null, null, true, registrar);
        capabilities.ziwei(null, registrar);
        var captured = new AtomicReference<List<ToolSchema>>();
        Model model =
                new Model() {
                    @Override
                    public String getModelName() {
                        return "schema-budget-probe";
                    }

                    @Override
                    public Flux<ChatResponse> stream(
                            List<Msg> messages, List<ToolSchema> schemas, GenerateOptions options) {
                        captured.set(List.copyOf(schemas));
                        return Flux.just(
                                ChatResponse.builder()
                                        .id("answer")
                                        .content(List.of(TextBlock.builder().text("done").build()))
                                        .build());
                    }
                };
        // Skill 工具由 SDK 在运行时注册，不能只测初始 Toolkit 中的业务工具。
        try (var repository = new ClasspathSkillRepository("skills");
                var agent =
                        ReActAgent.builder()
                                .name("schema-budget-probe")
                                .model(model)
                                .toolkit(toolkit)
                                .skillRepository(repository)
                                .skillCodeExecutionEnabled(false)
                                .build()) {
            agent.call(new UserMessage("user", "probe")).block(Duration.ofSeconds(10));
        }
        tools = Objects.requireNonNull(captured.get());
    }

    @Test
    void defaultBudgetAdmitsTheRealBuiltInToolsetWithZiweiAndSkills() {
        assertThat(tools)
                .extracting(ToolSchema::getName)
                .contains(
                        "ziwei_natal_chart",
                        "ziwei_yearly_chart",
                        "market_research",
                        "publish_portfolio_report",
                        "read_artifact_range",
                        "load_skill_through_path");
        assertThat(wireBytes(tools))
                .isGreaterThan(16 * 1024)
                .isLessThanOrEqualTo(ContextBudgets.defaults().toolSchemaBytes());
        assertThat(
                        ContextBudgets.defaults()
                                .effectiveWorkBytes(
                                        ReleaseDefinition.OFFLINE, List.of(), "", tools))
                .isGreaterThanOrEqualTo(1024);
    }

    @Test
    void explicitlySmallerSchemaBudgetStillRejectsTheSameToolset() {
        var budgets =
                ContextBudgets.from(
                        new MockEnvironment()
                                .withProperty("financeclaw.context.tool-schema-bytes", "16384"));
        assertThatThrownBy(
                        () ->
                                budgets.effectiveWorkBytes(
                                        ReleaseDefinition.OFFLINE, List.of(), "", tools))
                .isInstanceOf(Blocked.class)
                .hasMessage("tool_schema_budget_exceeded");
    }

    @Test
    void schemaAccountingUsesTheActualWireBoundary() {
        int wire = wireBytes(tools);
        var exact =
                ContextBudgets.from(
                        new MockEnvironment()
                                .withProperty(
                                        "financeclaw.context.tool-schema-bytes",
                                        Integer.toString(wire)));
        assertThat(exact.effectiveWorkBytes(ReleaseDefinition.OFFLINE, List.of(), "", tools))
                .isGreaterThanOrEqualTo(1024);
        var insufficient =
                ContextBudgets.from(
                        new MockEnvironment()
                                .withProperty(
                                        "financeclaw.context.tool-schema-bytes",
                                        Integer.toString(wire - 1)));
        assertThatThrownBy(
                        () ->
                                insufficient.effectiveWorkBytes(
                                        ReleaseDefinition.OFFLINE, List.of(), "", tools))
                .isInstanceOf(Blocked.class)
                .hasMessage("tool_schema_budget_exceeded");
    }

    @Test
    void largeInternalOutputSchemaDoesNotConsumeTheInputToolBudget() {
        var tool =
                ToolSchema.builder()
                        .name("small_input")
                        .description("small input")
                        .parameters(Map.of("type", "object", "properties", Map.of()))
                        .outputSchema(Map.of("description", "x".repeat(50000)))
                        .build();
        assertThat(ContextBudgets.bytes(JsonUtils.getJsonCodec().toJson(List.of(tool))))
                .isGreaterThan(48 * 1024);
        var exact =
                ContextBudgets.from(
                        new MockEnvironment()
                                .withProperty(
                                        "financeclaw.context.tool-schema-bytes",
                                        Integer.toString(wireBytes(List.of(tool)))));
        assertThat(
                        exact.effectiveWorkBytes(
                                ReleaseDefinition.OFFLINE, List.of(), "", List.of(tool)))
                .isGreaterThanOrEqualTo(1024);
    }

    @Test
    void toolsStillReduceTheWorkBudgetToTheRemainingModelCapacity() {
        int capacity = wireBytes(tools) + 1024 + 5000;
        assertThat(
                        ContextBudgets.defaults()
                                .effectiveWorkBytes(profile(capacity), List.of(), "", tools))
                .isEqualTo(5000);
    }

    @Test
    void fittingTheSchemaPartitionCannotBypassAnInsufficientModelWindow() {
        int capacity = wireBytes(tools) + 1024 + 500;
        assertThatThrownBy(
                        () ->
                                ContextBudgets.defaults()
                                        .effectiveWorkBytes(
                                                profile(capacity), List.of(), "", tools))
                .isInstanceOf(Blocked.class)
                .hasMessage("context_budget_exceeded");
    }

    private static ModelProfile profile(int capacity) {
        return new ModelProfile("offline", "", capacity, capacity + 2048, 2048, 0, false);
    }

    private static int wireBytes(List<ToolSchema> schemas) {
        var formatter = new OpenAIChatFormatter();
        var request = formatter.buildRequest("schema-budget-probe", List.of(), true);
        formatter.applyTools(request, schemas);
        return ContextBudgets.bytes(JsonUtils.getJsonCodec().toJson(request.getTools()));
    }
}
