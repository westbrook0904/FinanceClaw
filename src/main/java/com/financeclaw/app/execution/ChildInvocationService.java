package com.financeclaw.app.execution;

import com.financeclaw.app.execution.port.ChildInvocationGateway;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.PrivateAgentTarget;
import com.financeclaw.domain.agent.ResolvedAgentRelease;
import com.financeclaw.domain.execution.ChildInvocation;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.support.PrivateAgentContracts;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.function.Supplier;
import org.springframework.stereotype.Service;

/**
 * 深度为一的研究和私有叶子 Agent 子调用边界。逻辑身份固定为 root Turn 与模型工具调用 ID，attempt 更换不产生新额度。 调用表只保存身份和计数，正文始终复用
 * capability_steps；父 Turn 仍是取消、失败和调度的唯一权威。
 */
@Service
public final class ChildInvocationService {
    private static final List<String> REQUIRED = List.of("market:read", "research:run");
    private final ChildInvocationGateway calls;
    private final CapabilityStepService steps;
    private final ExecutionPolicyService policies;
    private final ReleaseService releases;

    public ChildInvocationService(
            ChildInvocationGateway calls,
            CapabilityStepService steps,
            ExecutionPolicyService policies,
            ReleaseService releases) {
        this.calls = calls;
        this.steps = steps;
        this.policies = policies;
        this.releases = releases;
    }

    /** 只由服务生成的执行句柄；不会从工具输入接受 owner、release、epoch、grant 或子权限。 */
    public record Invocation(
            Claim parent,
            String callId,
            String inputHash,
            String cachedResult,
            List<String> snapshotResults,
            String target,
            String targetAgentId,
            String targetReleaseHash,
            int maxModelCalls) {
        public Invocation(
                Claim parent,
                String callId,
                String inputHash,
                String cachedResult,
                List<String> snapshotResults) {
            this(
                    parent,
                    callId,
                    inputHash,
                    cachedResult,
                    snapshotResults,
                    "market_research",
                    null,
                    null,
                    4);
        }

        public Invocation {
            snapshotResults = List.copyOf(snapshotResults);
        }

        public String namespace() {
            return "child:" + callId;
        }
    }

    /** SDK 装配复用 begin 中已校验的冻结目标，后续模型/检查点回调不重新访问发布指针。 */
    public record PrivateInvocation(Invocation invocation, ResolvedAgentRelease release) {}

    /**
     * 仅执行父发布中显式冻结的同 owner 空工具叶子。目标只按给定 hash 解析，不能读取当前发布 替换已受理目标。控制面清单均不可变；父 gate
     * 与调用绑定则在每次状态/模型/结果访问时复核。
     */
    public PrivateInvocation beginPrivate(
            Claim parent, String callId, Map<String, Object> input, PrivateAgentTarget target) {
        requireKey(callId);
        requireMessage(input);
        if (releases == null || target == null) throw new Blocked("agent_delegation_unavailable");
        var owner = new Owner(parent.turn().tenant(), parent.turn().subject());
        var binding = PrivateAgentContracts.binding(target);
        var parentRelease = releases.resolve(parent.turn().releaseHash());
        parentRelease.requireIdentity(owner, parent.turn().agentId());
        if (parentRelease.plan() == null
                || parentRelease.plan().capabilities().stream().noneMatch(binding::equals)) {
            throw new Blocked("agent_delegation_denied");
        }
        var resolved = releases.resolve(target.releaseHash());
        resolved.requireIdentity(owner, target.agentId());
        PrivateAgentTarget.requireLeaf(owner, resolved);
        int limit = Math.min(4, resolved.definition().maxModelCalls());
        String hash = hash(input);
        return policies.dispatch(
                parent,
                policy -> {
                    requirePrivateScope(parent, policy);
                    var saved = calls.get(parent.turn().id(), callId);
                    if (saved == null) {
                        if (calls.hasSteps(parent.turn().id(), "child:" + callId))
                            throw new Blocked("child_invocation_missing");
                        calls.insertPrivateIfAbsent(
                                parent.turn().id(),
                                callId,
                                hash,
                                target.agentId(),
                                target.releaseHash());
                        saved = calls.get(parent.turn().id(), callId);
                    }
                    var invocation =
                            new Invocation(
                                    parent,
                                    callId,
                                    hash,
                                    null,
                                    List.of(),
                                    "private_agent",
                                    target.agentId(),
                                    target.releaseHash(),
                                    limit);
                    validateBinding(invocation, saved, policy);
                    return new PrivateInvocation(
                            new Invocation(
                                    parent,
                                    callId,
                                    hash,
                                    saved.result(),
                                    List.of(),
                                    "private_agent",
                                    target.agentId(),
                                    target.releaseHash(),
                                    limit),
                            resolved);
                });
    }

    /**
     * 在父栅栏内建立或核对调用绑定。旧结果缺少调用绑定时拒绝复用，不能将无法证明的历史预算当成零。 首次插入后再读权威 JOIN；数据库中缺失受理来源、授权或结果时不会补写当前身份来规避检查。
     */
    public Invocation begin(Claim parent, String callId, Map<String, Object> input) {
        requireKey(callId);
        String hash = hash(input);
        return policies.dispatch(
                parent,
                policy -> {
                    requireScopes(parent, policy);
                    var saved = calls.get(parent.turn().id(), callId);
                    if (saved == null) {
                        if (calls.hasSteps(parent.turn().id(), "child:" + callId)) {
                            throw new Blocked("child_invocation_missing");
                        }
                        calls.insertIfAbsent(parent.turn().id(), callId, hash);
                        saved = calls.get(parent.turn().id(), callId);
                    }
                    var invocation = new Invocation(parent, callId, hash, null, List.of());
                    validateBinding(invocation, saved, policy);
                    var snapshots =
                            saved.status().equals("pending") && saved.toolCalls() > 0
                                    ? calls.snapshots(parent.turn().id(), invocation.namespace())
                                    : List.<String>of();
                    if (snapshots.size() > 10
                            || (saved.status().equals("pending")
                                    && snapshots.size() != saved.toolCalls())) {
                        throw new Blocked("child_invocation_invalid");
                    }
                    return new Invocation(parent, callId, hash, saved.result(), snapshots);
                });
    }

    /**
     * 预留实际模型请求：子计数先作原子 CAS，再扣根计数；两者在同一父 dispatch 事务中提交。 网络失败不返还已提交额度，进程恢复也不能通过重建 ResearchRun
     * 重置四次上限。
     */
    public void reserveModel(Invocation invocation) {
        policies.consumeModel(
                invocation.parent(),
                policy -> {
                    var saved = requirePending(invocation, policy);
                    if (calls.reserveModel(
                                    saved.rootTurnId(),
                                    saved.callId(),
                                    saved.inputHash(),
                                    invocation.maxModelCalls())
                            != 1) {
                        throw new Blocked("child_model_budget_exceeded");
                    }
                });
    }

    /** 仅首次实际行情读取占用子工具额度。成功回执、子计数和行情服务扣减的根计数共享父事务； 相同工具调用回放只返回原结果，仍先检查当前父状态、授权、隐私和调用输入身份。 */
    public Object snapshot(
            Invocation invocation,
            String toolCallId,
            Map<String, Object> input,
            Supplier<Object> localRead) {
        requireKey(toolCallId);
        if (!"market_research".equals(invocation.target())) throw new Blocked("child_tools_denied");
        return policies.dispatch(
                invocation.parent(),
                policy -> {
                    requirePending(invocation, policy);
                    return steps.runHashedWithinDispatch(
                            invocation.parent(),
                            invocation.namespace(),
                            "tool:" + toolCallId,
                            hash(input),
                            () -> {
                                if (calls.reserveTool(
                                                invocation.parent().turn().id(),
                                                invocation.callId(),
                                                invocation.inputHash())
                                        != 1) {
                                    throw new Blocked("child_tool_budget_exceeded");
                                }
                                return localRead.get();
                            });
                });
    }

    /** 结果正文与 completed 标志原子提交；不创建第二份结果，CAS 或结果保存失败会整体回滚。 */
    public Object complete(Invocation invocation, Supplier<Object> result) {
        return policies.dispatch(
                invocation.parent(),
                policy -> {
                    var saved = calls.get(invocation.parent().turn().id(), invocation.callId());
                    validate(invocation, saved, policy);
                    if (saved.status().equals("completed")) {
                        return PayloadJson.parse(saved.result());
                    }
                    Object value =
                            steps.runHashedWithinDispatch(
                                    invocation.parent(),
                                    invocation.namespace(),
                                    "result",
                                    invocation.inputHash(),
                                    result);
                    if (calls.complete(saved.rootTurnId(), saved.callId(), saved.inputHash())
                            != 1) {
                        throw new Blocked("child_invocation_invalid");
                    }
                    return value;
                });
    }

    /** SDK 状态读取、CAS 保存和删除都必须通过该边界，防止取消、遗忘或撤权后的迟到回调访问旧状态。 */
    public <T> T checkpoint(Invocation invocation, Supplier<T> operation) {
        return policies.dispatch(
                invocation.parent(),
                policy -> {
                    requirePending(invocation, policy);
                    return operation.get();
                });
    }

    private ChildInvocation requirePending(Invocation invocation, Policy policy) {
        var saved = calls.get(invocation.parent().turn().id(), invocation.callId());
        validate(invocation, saved, policy);
        if (!saved.status().equals("pending")) {
            throw new Blocked("child_invocation_invalid");
        }
        return saved;
    }

    private void validate(Invocation invocation, ChildInvocation saved, Policy policy) {
        if ("private_agent".equals(invocation.target()))
            requirePrivateScope(invocation.parent(), policy);
        else requireScopes(invocation.parent(), policy);
        validateBinding(invocation, saved, policy);
    }

    /** 已持有父执行门禁且完成一次权限交集检查；begin 不重复解析父发布或查询权限。 */
    private void validateBinding(Invocation invocation, ChildInvocation saved, Policy policy) {
        var turn = invocation.parent().turn();
        if (saved == null
                || !saved.rootTurnId().equals(turn.id())
                || !saved.callId().equals(invocation.callId())
                || !saved.target().equals(invocation.target())
                || !Objects.equals(saved.targetAgentId(), invocation.targetAgentId())
                || !Objects.equals(saved.targetReleaseHash(), invocation.targetReleaseHash())
                || invocation.maxModelCalls() < 1
                || invocation.maxModelCalls() > 4
                || !(invocation.target().equals("private_agent")
                        ? saved.scopes().isEmpty() && saved.toolCalls() == 0
                        : invocation.target().equals("market_research")
                                && saved.scopes().equals(List.of("market:read"))
                                && invocation.maxModelCalls() == 4)
                || !saved.tenant().equals(turn.tenant())
                || !saved.subject().equals(turn.subject())
                || !saved.conversationId().equals(turn.conversationId())
                || !saved.releaseHash().equals(turn.releaseHash())
                || saved.grantRevision() != policy.grantRevision()
                || saved.epoch() < 1) {
            throw new Blocked("child_invocation_invalid");
        }
        if (!saved.inputHash().equals(invocation.inputHash())) {
            throw new Blocked("child_input_conflict");
        }
        boolean completed = saved.status().equals("completed");
        if ((!completed && !saved.status().equals("pending"))
                || completed != (saved.result() != null)
                || (completed && !Objects.equals(saved.resultHash(), saved.inputHash()))) {
            throw new Blocked("child_invocation_invalid");
        }
    }

    private void requireScopes(Claim parent, Policy policy) {
        if (!policies.effectiveScopes(parent, policy).containsAll(REQUIRED)) {
            throw new Blocked("research_denied");
        }
    }

    private void requirePrivateScope(Claim parent, Policy policy) {
        if (!policies.effectiveScopes(parent, policy).contains(PrivateAgentTarget.TOOL_SCOPE)) {
            throw new Blocked("agent_delegation_denied");
        }
    }

    private static void requireMessage(Map<String, Object> input) {
        if (input == null
                || !input.keySet().equals(Set.of("message"))
                || !(input.get("message") instanceof String message)
                || message.isBlank()
                || message.length() > 16384
                || message.indexOf('\0') >= 0
                || !StandardCharsets.UTF_8.newEncoder().canEncode(message)) {
            throw new Blocked("child_invocation_invalid");
        }
    }

    private static String hash(Object input) {
        return PayloadDigest.sha256(PayloadJson.canonical(input));
    }

    private static void requireKey(String key) {
        if (key == null || key.isBlank() || key.length() > 512) {
            throw new Blocked("child_invocation_invalid");
        }
    }
}
