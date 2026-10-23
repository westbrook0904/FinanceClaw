package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.agentscope.ResumeInputs;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.support.ToolSchemas;
import io.agentscope.core.message.*;
import java.util.*;
import reactor.core.publisher.Mono;

/**
 * 注册城市补充、示例目录检索、本地报告创建及 Artifact 分页读取工具。 报告通过审批交互和 ReportService 发布，产物读取只使用当前 claim 的 owner。结果正文交给
 * Artifact 用例归档，工具仅返回引用，避免无界内容进入模型上下文。
 */
public final class LocalToolContribution implements ToolContribution {
    public static final Map<String, Object> CITY =
            ToolSchemas.required(
                    "city", Map.of("type", "string", "minLength", 1, "maxLength", 100));
    public static final Map<String, Object> TITLE =
            ToolSchemas.required(
                    "title", Map.of("type", "string", "minLength", 1, "maxLength", 200));
    public static final Map<String, Object> EMPTY =
            Map.of("type", "object", "properties", Map.of(), "additionalProperties", false);
    public static final Set<String> NAMES =
            Set.of("request_city", "create_report", "search_catalog", "read_artifact");

    private final ArtifactService artifacts;
    private final ReportService reports;
    private final Claim claim;

    public LocalToolContribution(ArtifactService artifacts, ReportService reports, Claim claim) {
        this.artifacts = artifacts;
        this.reports = reports;
        this.claim = claim;
    }

    @Override
    public void contribute(Registrar registrar) {
        registrar.tool(
                "request_city",
                EMPTY,
                null,
                p -> Mono.just(ToolResultBlock.suspended(p.getToolUseBlock())),
                new CityInteraction());
        registrar.tool(
                "search_catalog",
                CITY,
                "catalog:read",
                p ->
                        Mono.fromCallable(
                                () ->
                                        archived(
                                                List.of(
                                                        Map.of(
                                                                "city",
                                                                p.getInput().get("city"),
                                                                "name",
                                                                "示例酒店",
                                                                "source",
                                                                "local-fixture")))));
        registrar.tool(
                "create_report",
                TITLE,
                "report:write",
                p ->
                        Mono.fromCallable(
                                () ->
                                        ToolResultBlock.text(
                                                reports.report(
                                                        claim,
                                                        p.getToolUseBlock().getId(),
                                                        ResumeInputs.actionHash(
                                                                p.getToolUseBlock()),
                                                        (String) p.getInput().get("title")))),
                new ApprovalInteractionHandler(
                        call -> "是否创建报告：" + Objects.toString(call.getInput().get("title"), "")));
        registrar.tool(
                "read_artifact",
                Map.of(
                        "type",
                        "object",
                        "properties",
                        Map.of(
                                "path",
                                Map.of("type", "string", "maxLength", 256),
                                "artifact_id",
                                Map.of("type", "string"),
                                "start",
                                Map.of("type", "integer", "minimum", 0),
                                "limit",
                                Map.of("type", "integer", "minimum", 1, "maximum", 100)),
                        "required",
                        List.of("artifact_id"),
                        "additionalProperties",
                        false),
                "artifact:read",
                p ->
                        Mono.fromCallable(
                                () ->
                                        ToolResultBlock.text(
                                                PayloadJson.canonical(
                                                        artifacts.read(
                                                                new Owner(
                                                                        claim.turn().tenant(),
                                                                        claim.turn().subject()),
                                                                (String)
                                                                        p.getInput()
                                                                                .get("artifact_id"),
                                                                ((Number)
                                                                                p.getInput()
                                                                                        .getOrDefault(
                                                                                                "start",
                                                                                                0))
                                                                        .intValue(),
                                                                ((Number)
                                                                                p.getInput()
                                                                                        .getOrDefault(
                                                                                                "limit",
                                                                                                20))
                                                                        .intValue(),
                                                                (String)
                                                                        p.getInput()
                                                                                .getOrDefault(
                                                                                        "path",
                                                                                        ""))))));
    }

    private static final class CityInteraction implements InteractionHandler {
        @Override
        public Pending pending(ToolUseBlock call) {
            return new Pending(
                    "input",
                    call.getId(),
                    call.getName(),
                    ResumeInputs.actionHash(call),
                    PayloadJson.canonical(CITY),
                    "请输入城市");
        }

        @Override
        public Msg resume(ToolUseBlock call, Object answer) {
            return ResumeInputs.clarification(call, (String) ((Map<?, ?>) answer).get("city"));
        }
    }

    private ToolResultBlock archived(Object result) {
        return archived(result, "artifact:read");
    }

    private ToolResultBlock archived(Object result, String scope) {
        var a = artifacts.archive(claim, result, scope);
        return ToolResultBlock.text(
                PayloadJson.canonical(
                        Map.of("artifact_id", a.id(), "hash", a.hash(), "bytes", a.bytes())));
    }
}
