package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.ziwei.ZiweiService;
import com.financeclaw.domain.Governance.Pending;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.infrastructure.agentscope.ResumeInputs;
import com.financeclaw.infrastructure.ziwei.ZiweiSchemas;
import com.financeclaw.ziwei.ZiweiDates;
import io.agentscope.core.message.*;
import java.time.Duration;
import reactor.core.publisher.Mono;

/** Only native registration, suspension, and resumed ToolResultBlock translation. */
public final class ZiweiToolContribution implements ToolContribution, InteractionHandler {
    private final ZiweiService service;
    private final Claim claim;

    public ZiweiToolContribution(ZiweiService service, Claim claim) {
        this.service = service;
        this.claim = claim;
        service.require(claim);
    }

    @Override
    public void contribute(Registrar registrar) {
        for (var level : ZiweiDates.Level.values()) {
            var name = level.toolName();
            registrar.tool(
                    name,
                    ZiweiSchemas.description(name),
                    ZiweiSchemas.schema(name),
                    "ziwei:read",
                    param ->
                            Mono.fromCallable(
                                    () ->
                                            result(
                                                    param.getToolUseBlock(),
                                                    service.run(
                                                            claim,
                                                            param.getToolUseBlock().getName(),
                                                            param.getToolUseBlock().getId(),
                                                            param.getInput()))),
                    this);
        }
    }

    @Override
    public Pending pending(ToolUseBlock call) {
        return service.pending(
                claim,
                call.getName(),
                call.getId(),
                ResumeInputs.actionHash(call),
                call.getInput());
    }

    @Override
    public Msg resume(ToolUseBlock call, Object answer) {
        var result =
                Mono.fromCallable(
                                () ->
                                        result(
                                                call,
                                                service.resume(
                                                        claim,
                                                        call.getName(),
                                                        call.getId(),
                                                        call.getInput(),
                                                        answer)))
                        .block(Duration.ofSeconds(20));
        return ResumeInputs.result(call, result);
    }

    private static ToolResultBlock result(ToolUseBlock call, ZiweiService.Result outcome) {
        return outcome.suspended()
                ? ToolResultBlock.suspended(call)
                : ToolResultBlock.text(outcome.text());
    }
}
