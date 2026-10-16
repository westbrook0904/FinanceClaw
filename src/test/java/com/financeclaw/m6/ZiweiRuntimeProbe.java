package com.financeclaw.m6;

import com.financeclaw.application.*;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.agentscope.execution.*;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import java.nio.file.*;
import java.util.*;
import org.springframework.boot.SpringApplication;
import reactor.core.publisher.Flux;

/** Test-only fault driver. Production jar, native AgentScope and MyBatis perform all execution. */
public final class ZiweiRuntimeProbe extends TaskAgentRuntime implements Model {
  private final Properties settings;

  private ZiweiRuntimeProbe(
      TaskService tasks,
      GovernanceService governance,
      RuntimeMapper states,
      com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities capabilities,
      Properties settings) {
    super(tasks, governance, states, capabilities, 60000);
    this.settings = settings;
  }

  public static void main(String[] args) throws Exception {
    var settings = new Properties();
    try (var input = Files.newInputStream(Path.of(args[0]))) {
      settings.load(input);
    }
    var application = new SpringApplication(FinanceClawApplication.class);
    // Command line properties override packaged defaults; no operator environment is inherited.
    String[] properties =
        settings.stringPropertyNames().stream()
            .filter(k -> !k.startsWith("probe."))
            .map(k -> "--" + k + "=" + settings.getProperty(k))
            .toArray(String[]::new);
    try (var context = application.run(properties)) {
      var tasks = context.getBean(TaskService.class);
      if (settings.getProperty("probe.mode").equals("recover")) {
        tasks.recoverExpired();
      } else {
        var claim = Objects.requireNonNull(tasks.claim("m6d-fork", 120000), "queued command");
        var runtime =
            new ZiweiRuntimeProbe(
                tasks,
                context.getBean(GovernanceService.class),
                context.getBean(RuntimeMapper.class),
                context.getBean(
                    com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities
                        .class),
                settings);
        if (settings.getProperty("probe.mode").equals("after-seal")) {
          var result = runtime.execute(claim);
          tasks.seal(claim, result);
          Runtime.getRuntime().halt(73);
        }
        // This launcher is packaged alone; all assembly dependencies must come from the product
        // jar.
        try (var worker =
            new com.financeclaw.worker.TaskWorker(
                context.getBean(com.financeclaw.app.execution.ExecutionLeaseService.class),
                context.getBean(com.financeclaw.app.execution.RecoverTaskService.class),
                context.getBean(com.financeclaw.app.task.ProjectTaskService.class),
                new com.financeclaw.app.execution.ExecuteTaskService(
                    context.getBean(com.financeclaw.app.execution.ExecutionCompletionService.class),
                    runtime),
                false,
                false,
                1,
                120000,
                2000)) {
          worker.execute(claim);
        }
      }
    }
    System.out.println("M6D_PROBE_DONE");
  }

  @Override
  protected Model model(Claim claim) {
    return this;
  }

  @Override
  public String getModelName() {
    return "m6d-scripted-test-only";
  }

  @Override
  public Flux<ChatResponse> stream(
      List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
    long resolved =
        messages.stream()
            .flatMap(m -> m.getContentBlocks(ToolResultBlock.class).stream())
            .filter(r -> !r.isSuspended())
            .count();
    String mode = settings.getProperty("probe.mode");
    if (resolved > 0 && mode.equals("after-artifact")) Runtime.getRuntime().halt(73);
    if ((resolved == 0 && mode.equals("before-tool"))
        || (resolved > 0 && mode.equals("after-tool"))) barrier();
    String[] names = settings.getProperty("probe.tools", "ziwei_yearly_chart").split(",");
    var input = PayloadJson.object(settings.getProperty("probe.input"));
    if (resolved < names.length && names[(int) resolved].equals("ziwei_natal_chart")) {
      input = new TreeMap<>(input);
      input.remove("on_date");
      input.remove("year_offset");
    }
    return Flux.just(
        ChatResponse.builder()
            .id("synthetic")
            .content(
                resolved >= names.length
                    ? List.of(TextBlock.builder().text("合成盘面已核对。仅供传统文化参考。").build())
                    : List.of(
                        com.financeclaw.infrastructure.agentscope.NativeToolCalls.call(
                            "fixed-" + resolved, names[(int) resolved], input)))
            .build());
  }

  private void barrier() {
    try {
      Path marker = Path.of(settings.getProperty("probe.barrier"));
      Files.writeString(marker, "ready");
      long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(45);
      while (!Files.exists(Path.of(marker + ".continue"))) {
        if (System.nanoTime() > deadline) throw new IllegalStateException("probe barrier timeout");
        Thread.sleep(20);
      }
    } catch (Exception e) {
      throw new IllegalStateException("probe barrier failed", e);
    }
  }
}
