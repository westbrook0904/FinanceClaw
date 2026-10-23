package com.financeclaw.m6;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.*;
import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.agentscope.context.WorkingContextSession;
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
    private final Set<String> observedReceipts = new HashSet<>();

    ZiweiRuntimeProbe(
            ExecutionPolicyService policy,
            ExecutionLeaseService leases,
            ReleaseService releases,
            Credentials credentials,
            ExecutionContextGateway executionContext,
            ArtifactService artifacts,
            ReportService reports,
            RuntimeMapper states,
            com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities capabilities,
            Properties settings) {
        super(
                policy,
                leases,
                releases,
                credentials,
                executionContext,
                artifacts,
                reports,
                states,
                capabilities,
                60000);
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
            var leases = context.getBean(ExecutionLeaseService.class);
            var recovery = context.getBean(RecoverTaskService.class);
            var completion = context.getBean(ExecutionCompletionService.class);
            if (settings.getProperty("probe.mode").equals("recover")) {
                recovery.recoverExpired();
            } else {
                var claim =
                        Objects.requireNonNull(leases.claim("m6d-fork", 120000), "queued command");
                var runtime =
                        new ZiweiRuntimeProbe(
                                context.getBean(ExecutionPolicyService.class),
                                leases,
                                context.getBean(ReleaseService.class),
                                context.getBean(Credentials.class),
                                context.getBean(ExecutionContextGateway.class),
                                context.getBean(ArtifactService.class),
                                context.getBean(ReportService.class),
                                context.getBean(RuntimeMapper.class),
                                context.getBean(
                                        com.financeclaw.infrastructure.agentscope.capability
                                                .BusinessCapabilities.class),
                                settings);
                if (settings.getProperty("probe.mode").equals("after-seal")) {
                    var result = runtime.execute(claim);
                    completion.seal(claim, result);
                    Runtime.getRuntime().halt(73);
                }
                // This launcher is packaged alone; all assembly dependencies must come from the
                // product
                // jar.
                try (var worker =
                        new com.financeclaw.worker.TaskWorker(
                                leases,
                                recovery,
                                new ExecuteTaskService(completion, runtime),
                                context.getBean(com.financeclaw.app.task.WorkSignals.class),
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
        String[] names = settings.getProperty("probe.tools", "ziwei_yearly_chart").split(",");
        var requestedTools = Set.of(names);
        // 固定模型的进度不能依赖可见历史长度：已完成批次可能已被工作上下文压缩。
        // 保存本进程已观察的 callId；恢复的首轮仍从 SDK 检查点中读取真实回执。
        messages.stream()
                .filter(message -> !WorkingContextSession.isHistorical(message))
                .flatMap(m -> m.getContentBlocks(ToolResultBlock.class).stream())
                .filter(r -> !r.isSuspended())
                .filter(r -> requestedTools.contains(r.getName()))
                .map(ToolResultBlock::getId)
                .forEach(observedReceipts::add);
        long resolved = observedReceipts.size();
        String mode = settings.getProperty("probe.mode");
        if (resolved > 0 && mode.equals("after-artifact")) Runtime.getRuntime().halt(73);
        if ((resolved == 0 && mode.equals("before-tool"))
                || (resolved > 0 && mode.equals("after-tool"))) barrier();
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
                                        ? List.of(
                                                TextBlock.builder()
                                                        .text("合成盘面已核对。仅供传统文化参考。")
                                                        .build())
                                        : List.of(
                                                com.financeclaw.infrastructure.agentscope
                                                        .NativeToolCalls.call(
                                                        "fixed-" + resolved,
                                                        names[(int) resolved],
                                                        input)))
                        .build());
    }

    private void barrier() {
        try {
            Path marker = Path.of(settings.getProperty("probe.barrier"));
            Files.writeString(marker, "ready");
            long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(45);
            while (!Files.exists(Path.of(marker + ".continue"))) {
                if (System.nanoTime() > deadline)
                    throw new IllegalStateException("probe barrier timeout");
                Thread.sleep(20);
            }
        } catch (Exception e) {
            throw new IllegalStateException("probe barrier failed", e);
        }
    }
}
