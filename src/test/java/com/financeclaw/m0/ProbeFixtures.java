package com.financeclaw.m0;

import com.financeclaw.agent.ResumeInputs;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.infrastructure.state.EffectLedger;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.permission.*;
import io.agentscope.core.tool.*;
import java.util.List;
import java.util.Map;
import javax.sql.DataSource;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

final class ProbeFixtures {
  static final String CALL_ID = "call-1";
  static final Map<String, Object> TOOL_INPUT = Map.of("operation", "report-1");

  static PermissionContextState permissions(String tool, boolean ask) {
    var rule =
        new PermissionRule(
            tool, null, ask ? PermissionBehavior.ASK : PermissionBehavior.ALLOW, "m0-policy");
    var builder = PermissionContextState.builder().mode(PermissionMode.DEFAULT);
    if (ask) builder.addAskRule(tool, rule);
    else builder.addAllowRule(tool, rule);
    return builder.build();
  }

  static ChatResponse response(ContentBlock content) {
    return ChatResponse.builder().id("m0-response").content(List.of(content)).build();
  }

  static Model scripted(String tool, String crash) {
    return new Model() {
      @Override
      public String getModelName() {
        return "m0-deterministic";
      }

      @Override
      public Flux<ChatResponse> stream(
          List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
        return Flux.defer(
            () -> {
              boolean resolved =
                  messages.stream()
                      .flatMap(m -> m.getContentBlocks(ToolResultBlock.class).stream())
                      .anyMatch(r -> CALL_ID.equals(r.getId()) && !r.isSuspended());
              if ("before-model".equals(crash) || ("after-tool".equals(crash) && resolved))
                Runtime.getRuntime().halt(74);
              if ("model-failure".equals(crash))
                return Flux.error(new IllegalStateException("injected model failure"));
              if (resolved) return Flux.just(response(TextBlock.builder().text("done").build()));
              // Match a real adapter: SDK parameter validation reads the raw JSON content.
              return Flux.just(
                  response(
                      ToolUseBlock.builder()
                          .id(CALL_ID)
                          .name(tool)
                          .input(TOOL_INPUT)
                          .content(ResumeInputs.canonical(TOOL_INPUT))
                          .build()));
            });
      }
    };
  }

  static Toolkit toolkit(
      SqlSessionTemplate database, ExecutionScope scope, String tool, String crash) {
    Toolkit toolkit = new Toolkit();
    toolkit.registerAgentTool(
        new ToolBase(
            ToolBase.builder()
                .name(tool)
                .description("M0 deterministic tool")
                .inputSchema(
                    Map.of(
                        "type",
                        "object",
                        "properties",
                        Map.of("operation", Map.of("type", "string")),
                        "required",
                        List.of("operation")))) {
          @Override
          public Mono<ToolResultBlock> callAsync(ToolCallParam param) {
            return Mono.fromCallable(
                () -> {
                  if ("before-tool".equals(crash)) Runtime.getRuntime().halt(74);
                  if (tool.equals("request_city"))
                    return ToolResultBlock.suspended(param.getToolUseBlock());
                  if ("tool-failure".equals(crash))
                    throw new IllegalStateException("injected tool failure");
                  String operation = (String) param.getInput().get("operation");
                  String hash =
                      ExecutionScope.hash(List.of(tool, ResumeInputs.canonical(param.getInput())));
                  EffectLedger ledger =
                      new EffectLedger(database.getMapper(RuntimeMapper.class), scope);
                  var previous = ledger.begin(operation, hash);
                  if (previous.isPresent()) return ToolResultBlock.text(previous.get());
                  if ("after-intent".equals(crash)) Runtime.getRuntime().halt(74);
                  database.getMapper(ProbeMapper.class).applyEffect(scope.key(), operation);
                  if ("after-effect".equals(crash)) Runtime.getRuntime().halt(74);
                  ledger.complete(operation, hash, "{\"status\":\"created\"}");
                  return ToolResultBlock.text("created");
                });
          }
        });
    return toolkit;
  }

  static int effects(SqlSessionTemplate database, ExecutionScope scope) {
    return database.getMapper(ProbeMapper.class).countEffects(scope.key());
  }

  // Child JVMs use the same real mappers without bootstrapping a web application.
  static SqlSessionTemplate database(DataSource source) {
    try {
      var configuration = new Configuration();
      configuration.setCacheEnabled(false);
      configuration.setLocalCacheScope(LocalCacheScope.STATEMENT);
      configuration.addMapper(RuntimeMapper.class);
      configuration.addMapper(ProbeMapper.class);
      var factory = new SqlSessionFactoryBean();
      factory.setDataSource(source);
      factory.setConfiguration(configuration);
      return new SqlSessionTemplate(factory.getObject());
    } catch (Exception error) {
      throw new IllegalStateException("Cannot initialize MyBatis test database", error);
    }
  }
}
