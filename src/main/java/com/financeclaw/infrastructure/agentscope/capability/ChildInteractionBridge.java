package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.agent.port.AgentAccessGateway;
import com.financeclaw.app.execution.CapabilityStepService;
import com.financeclaw.app.execution.ChildInvocationService;
import com.financeclaw.app.execution.ChildInvocationService.Invocation;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.agent.*;
import com.financeclaw.infrastructure.agentscope.ResumeInputs;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.state.AgentState;
import java.util.*;
import org.springframework.stereotype.Component;

/** Durable links from root interactions to exact child SDK questions. No child queue or lease. */
@Component
public final class ChildInteractionBridge
        implements com.financeclaw.app.interaction.port.ChildInteractionCheckpoint {
    private final ChildInvocationService calls;
    private final CapabilityStepService steps;
    private final GovernanceMapper db;
    private final RuntimeMapper states;
    private final AgentAccessGateway access;

    public ChildInteractionBridge(
            ChildInvocationService calls,
            CapabilityStepService steps,
            GovernanceMapper db,
            RuntimeMapper states,
            AgentAccessGateway access) {
        this.calls = calls;
        this.steps = steps;
        this.db = db;
        this.states = states;
        this.access = access;
    }

    public List<Pending> save(
            Invocation invocation,
            PrivateAgentTarget target,
            ToolUseBlock rootCall,
            long childVersion,
            List<Pending> pending) {
        if (pending.isEmpty() || target.profile() != PrivateAgentTarget.Profile.CONTROLLED)
            throw new Blocked("child_interaction_invalid");
        return calls.checkpoint(
                invocation,
                () -> {
                    var result = new ArrayList<Pending>();
                    var ids = new ArrayList<String>();
                    for (var question : pending) {
                        String id =
                                "ci-"
                                        + ExecutionScope.hash(
                                                List.of(
                                                        invocation.parent().scope().key(),
                                                        invocation.callId(),
                                                        Long.toString(childVersion),
                                                        question.callId(),
                                                        question.actionHash()));
                        var body = new TreeMap<String, Object>();
                        body.put("id", id);
                        body.put("rootCallId", rootCall.getId());
                        body.put("rootActionHash", ResumeInputs.actionHash(rootCall));
                        body.put("targetAgentId", target.agentId());
                        body.put("targetReleaseHash", target.releaseHash());
                        body.put("scopes", target.scopes());
                        body.put("profile", target.profile().name());
                        body.put(
                                "childScope",
                                target.scope(invocation.parent().scope(), invocation.callId())
                                        .key());
                        body.put("childVersion", childVersion);
                        body.put("childCallId", question.callId());
                        body.put("childTool", question.tool());
                        body.put("childActionHash", question.actionHash());
                        body.put("kind", question.kind());
                        body.put("schema", question.schema());
                        body.put("question", question.question());
                        String hash = actionHash(body);
                        body.put("actionHash", hash);
                        steps.run(
                                invocation.parent(),
                                invocation.namespace(),
                                "interaction:" + id,
                                body,
                                () -> body);
                        ids.add(id);
                        result.add(
                                new Pending(
                                        question.kind(),
                                        id,
                                        rootCall.getName(),
                                        hash,
                                        question.schema(),
                                        question.question()));
                    }
                    steps.run(
                            invocation.parent(),
                            invocation.namespace(),
                            "waiting:" + invocation.parent().command().attemptId(),
                            ids,
                            () -> ids);
                    return List.copyOf(result);
                });
    }

    public List<Pending> pending(Claim parent, PrivateAgentTarget target, ToolUseBlock rootCall) {
        var index =
                steps.find(
                        parent,
                        "child:" + rootCall.getId(),
                        "waiting:" + parent.command().attemptId());
        if (index == null) return List.of();
        var ids = PayloadJson.scopes(index.result());
        var result = new ArrayList<Pending>();
        for (String id : ids) {
            var body = envelope(parent.turn().id(), id);
            requireRoot(body, target, rootCall);
            result.add(
                    new Pending(
                            string(body, "kind"),
                            id,
                            rootCall.getName(),
                            string(body, "actionHash"),
                            string(body, "schema"),
                            string(body, "question")));
        }
        return result;
    }

    public boolean delegated(Interaction interaction) {
        return interaction.tool().startsWith("delegate_") && interaction.callId().startsWith("ci-");
    }

    /** Called only after the root owner, policy, grant and waiting attempt have been checked. */
    public InteractionBinding.Checkpoint checkpoint(InteractionBinding binding) {
        var checked = checked(binding.claim(), binding.interaction());
        return new InteractionBinding.Checkpoint(
                binding.interaction().callId(),
                checked.rootCall().getName(),
                string(checked.body(), "actionHash"),
                checked.childCall().getState() == ToolCallState.ASKING,
                checked.childCall().getInput());
    }

    private record Checked(
            Map<String, Object> body,
            PrivateAgentTarget target,
            ToolUseBlock rootCall,
            ToolUseBlock childCall,
            ExecutionScope childScope,
            AgentState childState,
            long childVersion) {}

    private Checked checked(Claim source, Interaction interaction) {
        var body = envelope(source.turn().id(), interaction.callId());
        var target = target(body);
        var owner = new Owner(source.turn().tenant(), source.turn().subject());
        access.requireInvocation(owner, target.agentId(), target.releaseHash());
        var rootScope = source.scope();
        var rootCall =
                ResumeInputs.pending(
                        new MyBatisAgentStateStore(states, rootScope),
                        rootScope,
                        interaction.stateVersion(),
                        string(body, "rootCallId"));
        requireRoot(body, target, rootCall);
        var scope = target.scope(rootScope, rootCall.getId());
        long version = ((Number) body.get("childVersion")).longValue();
        if (!scope.key().equals(body.get("childScope"))) throw new Blocked("checkpoint_mismatch");
        var store = new MyBatisAgentStateStore(states, scope);
        var childCall = ResumeInputs.pending(store, scope, version, string(body, "childCallId"));
        if (!childCall.getName().equals(body.get("childTool"))
                || !ResumeInputs.actionHash(childCall).equals(body.get("childActionHash"))
                || !actionHash(body).equals(body.get("actionHash"))
                || !interaction.actionHash().equals(body.get("actionHash"))
                || !interaction.tool().equals(rootCall.getName())
                || !interaction.kind().equals(body.get("kind"))
                || !interaction.schema().equals(body.get("schema")))
            throw new Blocked("checkpoint_mismatch");
        var state =
                store.getVersioned(
                        scope.userId(), scope.sessionId(), "agent_state", AgentState.class);
        return new Checked(body, target, rootCall, childCall, scope, state.value(), version);
    }

    /**
     * Clone only the exact answered child checkpoint. Parent SDK will dispatch the original
     * delegate tool again; it then consumes these native replies, including multiple ASK decisions.
     */
    public void prepare(
            Claim current,
            Claim source,
            CapabilityBinding binding,
            List<Interaction> interactions) {
        if (interactions.isEmpty()) return;
        var first = checked(source, interactions.getFirst());
        var target = com.financeclaw.support.PrivateAgentContracts.from(binding);
        if (!target.equals(first.target())) throw new Blocked("checkpoint_mismatch");
        var started =
                calls.beginPrivate(
                        current, first.rootCall().getId(), first.rootCall().getInput(), target);
        var invocation = started.invocation();
        calls.checkpoint(
                invocation,
                () -> {
                    var replies = new ArrayList<Msg>();
                    var approvalIds = new ArrayList<String>();
                    var answeredIds = new HashSet<String>();
                    for (var interaction : interactions) {
                        if (!interaction.status().equals("answered")
                                || !interaction.live()
                                || !interaction.turnId().equals(current.turn().id())
                                || !interaction.attemptId().equals(source.command().attemptId()))
                            throw new Blocked("stale_interaction");
                        var item = checked(source, interaction);
                        if (!item.target().equals(target)
                                || !item.rootCall().getId().equals(first.rootCall().getId())
                                || item.childVersion() != first.childVersion())
                            throw new Blocked("checkpoint_mismatch");
                        if (!answeredIds.add(item.childCall().getId()))
                            throw new Blocked("stale_interaction");
                        var answer = PayloadJson.object(interaction.response());
                        var original = new MyBatisAgentStateStore(states, item.childScope());
                        if (interaction.kind().equals("approval")) {
                            replies.add(
                                    ResumeInputs.approval(
                                            original,
                                            item.childScope(),
                                            item.childVersion(),
                                            item.childCall().getId(),
                                            string(item.body(), "childActionHash"),
                                            "approve".equals(answer.get("decision"))));
                            approvalIds.add(interaction.id());
                        } else if (item.childCall().getName().equals("request_city")) {
                            var fields = (Map<?, ?>) answer.get("answer");
                            replies.add(
                                    ResumeInputs.clarification(
                                            item.childCall(), (String) fields.get("city")));
                        } else throw new Blocked("unsupported_interaction");
                    }
                    // Every question in this child checkpoint must have an answer; omission is not
                    // consent.
                    var expected =
                            new com.financeclaw.infrastructure.agentscope.AgentStateInspector(
                                            first.childState())
                                    .pending().stream()
                                            .filter(
                                                    call ->
                                                            call.getState() == ToolCallState.ASKING
                                                                    || call.getName()
                                                                            .equals("request_city"))
                                            .toList();
                    if (!answeredIds.equals(
                            new HashSet<>(expected.stream().map(ToolUseBlock::getId).toList())))
                        throw new Blocked("stale_interaction");
                    var scope = target.scope(current.scope(), first.rootCall().getId());
                    var state = first.childState();
                    var resumedContext = new ArrayList<>(state.getContext());
                    replies.stream()
                            .filter(message -> message.getRole() == MsgRole.TOOL)
                            .forEach(resumedContext::add);
                    var copied =
                            AgentState.builder()
                                    .userId(scope.userId())
                                    .sessionId(scope.sessionId())
                                    .context(resumedContext)
                                    .summary(state.getSummary())
                                    .permissionContext(state.getPermissionContext())
                                    .toolContext(state.getToolContext())
                                    .tasksContext(state.getTasksContext())
                                    .planModeContext(state.getPlanModeContext())
                                    .build();
                    var destination = new PrivateAgentStateStore(states, scope, calls, invocation);
                    var present =
                            destination.getVersioned(
                                    scope.userId(),
                                    scope.sessionId(),
                                    "agent_state",
                                    AgentState.class);
                    if (!present.isPresent()
                            && destination.saveIfVersion(
                                            scope.userId(),
                                            scope.sessionId(),
                                            "agent_state",
                                            copied,
                                            0)
                                    <= 0) throw new Blocked("checkpoint_mismatch");
                    var marker =
                            Map.<String, Object>of(
                                    "approvals",
                                    approvalIds,
                                    "sourceScope",
                                    first.childScope().key(),
                                    "sourceVersion",
                                    first.childVersion());
                    steps.run(
                            current,
                            invocation.namespace(),
                            "resume:" + current.command().attemptId(),
                            marker,
                            () -> marker);
                    return null;
                });
    }

    public List<Msg> resumeInputs(Invocation invocation) {
        return calls.checkpoint(
                invocation,
                () -> {
                    var marker =
                            steps.find(
                                    invocation.parent(),
                                    invocation.namespace(),
                                    "resume:" + invocation.parent().command().attemptId());
                    if (marker == null) return null;
                    var data = PayloadJson.object(marker.result());
                    var target =
                            new PrivateAgentTarget(
                                    invocation.targetAgentId(), invocation.targetReleaseHash(),
                                    invocation.profile(), invocation.toolScopes());
                    var scope = target.scope(invocation.parent().scope(), invocation.callId());
                    var store = new MyBatisAgentStateStore(states, scope);
                    var saved =
                            store.getVersioned(
                                    scope.userId(),
                                    scope.sessionId(),
                                    "agent_state",
                                    AgentState.class);
                    if (!saved.isPresent()) throw new Blocked("checkpoint_mismatch");
                    var replies = new ArrayList<Msg>();
                    var answered = new HashSet<String>();
                    for (String id :
                            PayloadJson.scopes(PayloadJson.canonical(data.get("approvals")))) {
                        var interaction = db.getInteractionById(id);
                        if (interaction == null
                                || !interaction.live()
                                || !interaction.status().equals("answered")
                                || !interaction.kind().equals("approval")
                                || !interaction.turnId().equals(invocation.parent().turn().id()))
                            throw new Blocked("stale_interaction");
                        var body = envelope(interaction.turnId(), interaction.callId());
                        if (!target(body).equals(target)
                                || !invocation.callId().equals(body.get("rootCallId"))
                                || !Objects.equals(data.get("sourceScope"), body.get("childScope"))
                                || !Objects.equals(
                                        data.get("sourceVersion"), body.get("childVersion"))
                                || !actionHash(body).equals(interaction.actionHash())
                                || !interaction.actionHash().equals(body.get("actionHash"))
                                || !interaction.schema().equals(body.get("schema"))
                                || !answered.add(string(body, "childCallId")))
                            throw new Blocked("checkpoint_mismatch");
                        // Msg metadata is untyped JSON. Rebuild native ConfirmResult objects from
                        // the live answer and the exact copied SDK action, never from metadata
                        // maps.
                        replies.add(
                                ResumeInputs.approval(
                                        store,
                                        scope,
                                        saved.version(),
                                        string(body, "childCallId"),
                                        string(body, "childActionHash"),
                                        "approve"
                                                .equals(
                                                        PayloadJson.object(interaction.response())
                                                                .get("decision"))));
                    }
                    var expected =
                            new com.financeclaw.infrastructure.agentscope.AgentStateInspector(
                                            saved.value())
                                    .pending().stream()
                                            .filter(call -> call.getState() == ToolCallState.ASKING)
                                            .map(ToolUseBlock::getId)
                                            .collect(java.util.stream.Collectors.toSet());
                    if (!answered.equals(expected)) throw new Blocked("stale_interaction");
                    return List.copyOf(replies);
                });
    }

    public void requireApproved(Invocation invocation, ToolUseBlock call) {
        calls.checkpoint(
                invocation,
                () -> {
                    var marker =
                            steps.find(
                                    invocation.parent(),
                                    invocation.namespace(),
                                    "resume:" + invocation.parent().command().attemptId());
                    if (marker == null) throw new Blocked("child_approval_required");
                    var data = PayloadJson.object(marker.result());
                    for (String id :
                            PayloadJson.scopes(PayloadJson.canonical(data.get("approvals")))) {
                        var interaction = db.getInteractionById(id);
                        if (interaction == null
                                || !interaction.turnId().equals(invocation.parent().turn().id())
                                || !interaction.live()
                                || !interaction.status().equals("answered")
                                || !interaction.kind().equals("approval")) continue;
                        var body = envelope(invocation.parent().turn().id(), interaction.callId());
                        if (body.get("rootCallId").equals(invocation.callId())
                                && body.get("targetReleaseHash")
                                        .equals(invocation.targetReleaseHash())
                                && body.get("childCallId").equals(call.getId())
                                && body.get("childActionHash").equals(ResumeInputs.actionHash(call))
                                && "approve"
                                        .equals(
                                                PayloadJson.object(interaction.response())
                                                        .get("decision"))) return null;
                    }
                    throw new Blocked("child_approval_required");
                });
    }

    public String rootCallId(Interaction interaction) {
        return string(envelope(interaction.turnId(), interaction.callId()), "rootCallId");
    }

    private Map<String, Object> envelope(String root, String id) {
        String value = db.getChildInteractionEnvelope(root, id);
        if (value == null) throw new Blocked("child_interaction_missing");
        var body = PayloadJson.object(value);
        if (!id.equals(body.get("id"))) throw new Blocked("checkpoint_mismatch");
        return body;
    }

    private static PrivateAgentTarget target(Map<String, Object> body) {
        return new PrivateAgentTarget(
                string(body, "targetAgentId"),
                string(body, "targetReleaseHash"),
                PrivateAgentTarget.Profile.valueOf(string(body, "profile")),
                PayloadJson.scopes(PayloadJson.canonical(body.get("scopes"))));
    }

    private static void requireRoot(
            Map<String, Object> body, PrivateAgentTarget target, ToolUseBlock rootCall) {
        if (!rootCall.getId().equals(body.get("rootCallId"))
                || !rootCall.getName().equals(PrivateAgentTarget.toolName(target.agentId()))
                || !ResumeInputs.actionHash(rootCall).equals(body.get("rootActionHash")))
            throw new Blocked("checkpoint_mismatch");
    }

    private static String actionHash(Map<String, Object> body) {
        return ExecutionScope.hash(
                List.of(
                                "id",
                                "rootActionHash",
                                "targetAgentId",
                                "targetReleaseHash",
                                "childScope",
                                "childVersion",
                                "childActionHash")
                        .stream()
                        .map(key -> Objects.toString(body.get(key), ""))
                        .toList());
    }

    private static String string(Map<String, Object> body, String key) {
        if (!(body.get(key) instanceof String value)) throw new Blocked("checkpoint_mismatch");
        return value;
    }
}
