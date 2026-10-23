package com.financeclaw.infrastructure.agentscope.context;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.message.*;
import io.agentscope.core.state.AgentState;
import io.agentscope.core.util.JsonUtils;
import java.util.*;

/**
 * 单个 attempt 的工作上下文投影及本 Turn 的完整消息轨迹。
 *
 * <p>模型只见有界投影；被压缩的用户问题、回答、工具调用与结果引用仍保存在transcript。 跨 Turn 延续最新已完成的投影，不再按固定条数从问答表截断。所有持久写入由调用方加入 SDK
 * CAS 的执行栅栏事务。
 */
public final class WorkingContextSession {
    public static final String HISTORY_MARKER = "financeclaw_context_history";
    private final RuntimeMapper mapper;
    private final Claim claim;
    private final long grantRevision;
    private final Set<String> inherited = new HashSet<>();
    private final Map<String, Msg> transcript = new LinkedHashMap<>();

    public WorkingContextSession(RuntimeMapper mapper, Claim claim, long grantRevision) {
        this.mapper = mapper;
        this.claim = claim;
        this.grantRevision = grantRevision;
    }

    public String inputId() {
        return "turn-input-" + claim.turn().id();
    }

    public List<Msg> previous() {
        var row = mapper.getLatestCompletedWorkContext(claim, grantRevision);
        var messages = row == null ? List.<Msg>of() : inheritedMessages(decode(row.payload()));
        messages.forEach(m -> inherited.add(m.getId()));
        return List.copyOf(messages);
    }

    public void resume(List<Msg> context, String attempt, long version) {
        var row = mapper.getTurnWorkContext(claim.turn(), attempt, version);
        if (row == null) throw new Blocked("context_projection_unavailable");
        decode(row.transcript()).forEach(m -> transcript.put(m.getId(), m));
        // 原生检查点可能已压缩掉旧片段，使用独立轨迹继续累积而非覆盖已归档内容。
        for (var message : context) {
            if (!message.getId().equals(inputId()) && !transcript.containsKey(message.getId())) {
                inherited.add(message.getId());
            }
        }
        record(context);
    }

    public void record(List<Msg> messages) {
        for (var message : messages) {
            if (message.getRole() == MsgRole.SYSTEM
                    || "financeclaw_work_context".equals(message.getName())
                    || isHistorical(message)
                    || inherited.contains(message.getId())) continue;
            transcript.put(message.getId(), message);
        }
    }

    public static boolean isHistorical(Msg message) {
        return message.getMetadata() != null
                && Boolean.TRUE.equals(message.getMetadata().get(HISTORY_MARKER));
    }

    /** 历史工具记录是背景，不是当前待执行动作。重新命名首次继承的消息与callId， 防止供应商或固定Driver跨Turn复用ID时，把旧回执匹配到新动作或覆盖本轮轨迹。 */
    private List<Msg> inheritedMessages(List<Msg> messages) {
        var result = new ArrayList<Msg>();
        for (var message : messages) {
            if (isHistorical(message)) {
                result.add(message);
                continue;
            }
            var content = new ArrayList<ContentBlock>();
            for (var block : message.getContent()) {
                if (block instanceof ToolUseBlock call) {
                    var metadata =
                            new HashMap<String, Object>(
                                    call.getMetadata() == null ? Map.of() : call.getMetadata());
                    metadata.put("financeclaw_original_call_id", call.getId());
                    block =
                            new ToolUseBlock(
                                    historyId("call", call.getId()),
                                    call.getName(),
                                    call.getInput(),
                                    call.getContent(),
                                    metadata,
                                    call.getState());
                } else if (block instanceof ToolResultBlock value) {
                    block =
                            new ToolResultBlock(
                                    historyId("call", value.getId()),
                                    value.getName(),
                                    value.getOutput(),
                                    value.getMetadata(),
                                    value.getState());
                }
                content.add(block);
            }
            var metadata =
                    new HashMap<String, Object>(
                            message.getMetadata() == null ? Map.of() : message.getMetadata());
            metadata.put(HISTORY_MARKER, true);
            result.add(
                    Msg.builder()
                            .id(historyId("message", message.getId()))
                            .name(message.getName())
                            .role(message.getRole())
                            .content(content)
                            .metadata(metadata)
                            .timestamp(message.getTimestamp())
                            .usage(message.getUsage())
                            .build());
        }
        return List.copyOf(result);
    }

    private String historyId(String kind, String id) {
        return "h" + ExecutionScope.hash(List.of(claim.turn().id(), kind, id)).substring(0, 40);
    }

    public void persist(AgentState state, long stateVersion) {
        record(state.getContext());
        mapper.saveTurnWorkContext(
                claim,
                grantRevision,
                stateVersion,
                encode(state.getContext()),
                encode(new ArrayList<>(transcript.values())));
    }

    public static String encode(List<Msg> messages) {
        return JsonUtils.getJsonCodec().toJson(AgentState.builder().context(messages).build());
    }

    public static List<Msg> decode(String payload) {
        return JsonUtils.getJsonCodec().fromJson(payload, AgentState.class).getContext();
    }
}
