package com.financeclaw.infrastructure.agentscope.context;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.util.JsonUtils;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.function.UnaryOperator;

/**
 * 每次模型调用前使用的纯工作上下文算法，不读取用户画像、证据记忆或数据库。
 *
 * <p>调用者只能传入 AgentState.context，不能传入已拼接 system 的 ReasoningInput.messages。
 * 仅压缩旧的完整问答或已成功结束的工具轮，保留当前用户消息、最近完整轮及所有未完成/失败工具配对。 摘要作为带内部标记的 USER 数据消息返回；调用者负责在执行栅栏内持久化有效消息，最终
 * HTTP 预算仍须单独检查。
 */
public final class WorkingContextCompactor {
    public static final String SUMMARY_NAME = "financeclaw_work_context";
    private static final String SUMMARY_MARKER = "financeclaw_work_context";
    private final int workContextBytes;
    private final int summaryOutputBytes;
    private final int compactAtPercent;
    private final int retainedRounds;
    private final int summaryInputBytes;
    private final UnaryOperator<List<String>> referenceReducer;

    public WorkingContextCompactor(
            int workContextBytes,
            int summaryOutputBytes,
            int compactAtPercent,
            int retainedRounds) {
        this(
                workContextBytes,
                summaryOutputBytes,
                compactAtPercent,
                retainedRounds,
                workContextBytes);
    }

    public WorkingContextCompactor(
            int workContextBytes,
            int summaryOutputBytes,
            int compactAtPercent,
            int retainedRounds,
            int summaryInputBytes) {
        this(
                workContextBytes,
                summaryOutputBytes,
                compactAtPercent,
                retainedRounds,
                summaryInputBytes,
                UnaryOperator.identity());
    }

    public WorkingContextCompactor(
            int workContextBytes,
            int summaryOutputBytes,
            int compactAtPercent,
            int retainedRounds,
            int summaryInputBytes,
            UnaryOperator<List<String>> referenceReducer) {
        if (workContextBytes < 1
                || summaryOutputBytes < 1
                || summaryOutputBytes >= workContextBytes
                || compactAtPercent < 1
                || compactAtPercent > 100
                || retainedRounds < 1
                || summaryInputBytes <= summaryOutputBytes) {
            throw new IllegalArgumentException("Invalid work context budgets");
        }
        this.workContextBytes = workContextBytes;
        this.summaryOutputBytes = summaryOutputBytes;
        this.compactAtPercent = compactAtPercent;
        this.retainedRounds = retainedRounds;
        this.summaryInputBytes = summaryInputBytes;
        this.referenceReducer = Objects.requireNonNull(referenceReducer);
    }

    @FunctionalInterface
    public interface Summarizer {
        WorkContext summarize(SummaryRequest request);
    }

    /** 只交给摘要模型旧摘要和将被替换的工作消息，绝不包含 system 或未完成动作。 */
    public record SummaryRequest(
            WorkContext previousSummary, List<Msg> completedMessages, int maxOutputBytes) {
        public SummaryRequest {
            Objects.requireNonNull(previousSummary);
            completedMessages = List.copyOf(completedMessages);
        }
    }

    /**
     * @param currentInputId 当前真实用户输入的 Msg.id；恢复执行继续使用原始输入锚点
     * @return 原消息的不可变副本，或一条新摘要和保留消息；不原位修改 SDK 状态
     */
    public List<Msg> compact(List<Msg> workMessages, String currentInputId, Summarizer summarizer) {
        Objects.requireNonNull(summarizer);
        var original = List.copyOf(workMessages);
        if (original.stream().anyMatch(message -> message.getRole() == MsgRole.SYSTEM)) {
            throw new IllegalArgumentException("System and long-term memory cannot be summarized");
        }
        if (currentInputId == null
                || original.stream()
                                .filter(
                                        message ->
                                                currentInputId.equals(message.getId())
                                                        && message.getRole() == MsgRole.USER
                                                        && !isSummary(message))
                                .count()
                        != 1) {
            throw new IllegalArgumentException("A unique current user input is required");
        }
        int originalBytes = bytes(original);
        if ((long) originalBytes * 100 < (long) workContextBytes * compactAtPercent) {
            return original;
        }

        var summaries = original.stream().filter(WorkingContextCompactor::isSummary).toList();
        if (summaries.size() > 1) {
            throw new IllegalArgumentException("Only one work context summary is allowed");
        }
        WorkContext previous =
                summaries.isEmpty()
                        ? WorkContext.empty()
                        : WorkContext.fromJson(summaries.getFirst().getTextContent());
        var messages = original.stream().filter(message -> !isSummary(message)).toList();
        var units = units(messages, currentInputId);
        var complete = units.stream().filter(Unit::complete).toList();
        var removed = new HashSet<Integer>();
        for (int index = 0; index < complete.size() - retainedRounds; index++) {
            removed.addAll(complete.get(index).indices());
        }
        if (removed.isEmpty()) {
            requireWithinBudget(originalBytes);
            return original;
        }
        List<Msg> kept = new ArrayList<>();
        for (int index = 0; index < messages.size(); index++) {
            if (!removed.contains(index)) {
                kept.add(messages.get(index));
            }
        }
        // 连必须保留的消息都放不下时，不调用压缩模型，更不能剪断工具配对或当前请求。
        requireWithinBudget(bytes(kept));
        WorkContext summary = previous;
        List<Msg> batch = new ArrayList<>();
        for (int index = 0; index < complete.size() - retainedRounds; index++) {
            var group = complete.get(index).indices().stream().map(messages::get).toList();
            var proposed = new ArrayList<>(batch);
            proposed.addAll(group);
            if (summaryInputSize(summary, proposed) > summaryInputBytes && !batch.isEmpty()) {
                summary = summarize(summary, batch, summarizer);
                batch = new ArrayList<>();
            }
            batch.addAll(group);
            if (summaryInputSize(summary, batch) > summaryInputBytes) {
                throw new Blocked("context_summary_input_exceeded");
            }
        }
        if (!batch.isEmpty()) {
            summary = summarize(summary, batch, summarizer);
        }
        var compacted = new ArrayList<Msg>();
        compacted.add(summaryMessage(summary));
        compacted.addAll(kept);
        int compactedBytes = bytes(compacted);
        requireWithinBudget(compactedBytes);
        if (compactedBytes >= originalBytes) {
            throw new Blocked("context_compaction_failed");
        }
        return List.copyOf(compacted);
    }

    private WorkContext summarize(WorkContext previous, List<Msg> batch, Summarizer summarizer) {
        var references = new LinkedHashSet<String>();
        previous.evidence().stream()
                .filter(value -> value.startsWith("artifact_ref:"))
                .forEach(references::add);
        for (var message : batch) {
            for (var call : message.getContentBlocks(ToolUseBlock.class)) {
                if ("read_artifact".equals(call.getName())
                        || "read_artifact_range".equals(call.getName())) {
                    artifactReference(call.getInput(), references);
                }
            }
            for (var value : message.getContentBlocks(ToolResultBlock.class)) {
                for (var block : value.getOutput()) {
                    if (block instanceof TextBlock text) {
                        try {
                            Object data = PayloadJson.parse(text.getText());
                            if (data instanceof Map<?, ?> object) {
                                artifactReference(object, references);
                            }
                        } catch (Blocked invalidJson) {
                            // 普通文本不属于产物引用；不把工具正文或其错误内容当成日志输出。
                        }
                    }
                }
            }
        }
        // 文件索引也受摘要预算约束；过多引用由调用方归档成一个可逐层读取的索引文件。
        // 正文仍在原文件/逐Turn轨迹里，不能为了缩短提示而悄悄删除来源。
        if (PayloadJson.canonical(references).getBytes(StandardCharsets.UTF_8).length
                > summaryOutputBytes / 3) {
            references = new LinkedHashSet<>(referenceReducer.apply(List.copyOf(references)));
        }
        int reservedReferenceBytes =
                PayloadJson.canonical(references).getBytes(StandardCharsets.UTF_8).length;
        int textBudget = summaryOutputBytes - reservedReferenceBytes - 64;
        if (textBudget < 64) throw new Blocked("context_compaction_failed");
        WorkContext result = summarizer.summarize(new SummaryRequest(previous, batch, textBudget));
        if (result == null) throw new Blocked("context_compaction_failed");
        result.evidence().stream()
                .filter(value -> !value.startsWith("artifact_ref:"))
                .forEach(references::add);
        try {
            result =
                    new WorkContext(
                            result.goals(),
                            result.tools(),
                            List.copyOf(references),
                            result.nextSteps());
        } catch (IllegalArgumentException invalid) {
            throw new Blocked("context_compaction_failed");
        }
        if (result.json().getBytes(StandardCharsets.UTF_8).length > summaryOutputBytes) {
            throw new Blocked("context_compaction_failed");
        }
        return result;
    }

    private static void artifactReference(Map<?, ?> value, Set<String> references) {
        Object id =
                value.containsKey("artifact_id")
                        ? value.get("artifact_id")
                        : value.get("artifactId");
        if (!(id instanceof String text) || text.isBlank() || text.length() > 256) {
            return;
        }
        var reference = new LinkedHashMap<String, Object>();
        reference.put("artifact_id", id);
        for (String key :
                List.of(
                        "hash",
                        "bytes",
                        "total",
                        "start",
                        "count",
                        "path",
                        "limit",
                        "max_bytes",
                        "nextStart")) {
            Object field = value.get(key);
            if (field instanceof Number || field instanceof String) {
                reference.put(key, field);
            }
        }
        references.add("artifact_ref:" + PayloadJson.canonical(reference));
    }

    private int summaryInputSize(WorkContext previous, List<Msg> batch) {
        return JsonUtils.getJsonCodec()
                .toJson(new SummaryRequest(previous, batch, summaryOutputBytes))
                .getBytes(StandardCharsets.UTF_8)
                .length;
    }

    public static Msg summaryMessage(WorkContext context) {
        return Msg.builder()
                .name(SUMMARY_NAME)
                .role(MsgRole.USER)
                .textContent(context.json())
                .metadata(Map.of(SUMMARY_MARKER, true))
                .build();
    }

    public static boolean isSummary(Msg message) {
        return message.getRole() == MsgRole.USER
                && SUMMARY_NAME.equals(message.getName())
                && message.getMetadata() != null
                && Boolean.TRUE.equals(message.getMetadata().get(SUMMARY_MARKER));
    }

    /** SDK 消息 JSON 的保守 UTF-8 大小，和供应商最终 HTTP 序列化预算是两道独立边界。 */
    public static int bytes(List<Msg> messages) {
        return JsonUtils.getJsonCodec().toJson(messages).getBytes(StandardCharsets.UTF_8).length;
    }

    private void requireWithinBudget(int size) {
        if (size > workContextBytes) {
            throw new Blocked("context_budget_exceeded");
        }
    }

    private record Unit(List<Integer> indices, boolean complete) {}

    private static List<Unit> units(List<Msg> messages, String anchor) {
        List<Unit> result = new ArrayList<>();
        List<Integer> current = new ArrayList<>();
        boolean hasAssistant = false;
        for (int index = 0; index < messages.size(); index++) {
            var message = messages.get(index);
            if (anchor.equals(message.getId())) {
                if (!current.isEmpty()) {
                    result.add(unit(messages, current));
                    current = new ArrayList<>();
                    hasAssistant = false;
                }
                // 当前输入不进入任何可删除单元，后面的已完成工具轮仍可以逐轮压缩。
                continue;
            }
            if (hasAssistant
                            && (message.getRole() == MsgRole.USER
                                    || message.getRole() == MsgRole.ASSISTANT)
                    || (!hasAssistant
                            && message.getRole() == MsgRole.ASSISTANT
                            && current.stream()
                                    .map(messages::get)
                                    .anyMatch(prior -> prior.getRole() == MsgRole.TOOL))) {
                result.add(unit(messages, current));
                current = new ArrayList<>();
                hasAssistant = false;
            }
            current.add(index);
            hasAssistant |= message.getRole() == MsgRole.ASSISTANT;
        }
        if (!current.isEmpty()) {
            result.add(unit(messages, current));
        }
        return result;
    }

    private static Unit unit(List<Msg> all, List<Integer> indices) {
        var messages = indices.stream().map(all::get).toList();
        var calls = new LinkedHashMap<String, ToolUseBlock>();
        var results = new LinkedHashMap<String, ToolResultBlock>();
        boolean valid = true;
        int assistants = 0;
        for (var message : messages) {
            if (message.getRole() == MsgRole.ASSISTANT) {
                assistants++;
            }
            for (var call : message.getContentBlocks(ToolUseBlock.class)) {
                valid &= call.getId() != null && calls.putIfAbsent(call.getId(), call) == null;
            }
            for (var value : message.getContentBlocks(ToolResultBlock.class)) {
                valid &= value.getId() != null && results.putIfAbsent(value.getId(), value) == null;
            }
        }
        valid &= assistants == 1 && calls.keySet().equals(results.keySet());
        for (var call : calls.values()) {
            var value = results.get(call.getId());
            valid &=
                    call.getState() != ToolCallState.ASKING
                            && value != null
                            && !value.isSuspended()
                            && value.getState() == ToolResultState.SUCCESS;
        }
        if (calls.isEmpty()) {
            valid &=
                    messages.stream()
                            .anyMatch(
                                    message ->
                                            message.getRole() == MsgRole.ASSISTANT
                                                    && message.getTextContent() != null
                                                    && !message.getTextContent().isBlank());
        }
        // 同一 callId 跨单元出现时全部保留，防止重复回执或异常历史被剪成孤立结果。
        var ids = new HashSet<>(calls.keySet());
        ids.addAll(results.keySet());
        var contained = new HashSet<>(indices);
        for (int index = 0; valid && index < all.size(); index++) {
            if (!contained.contains(index)) {
                var other = all.get(index);
                valid &=
                        other.getContentBlocks(ToolUseBlock.class).stream()
                                        .noneMatch(call -> ids.contains(call.getId()))
                                && other.getContentBlocks(ToolResultBlock.class).stream()
                                        .noneMatch(value -> ids.contains(value.getId()));
            }
        }
        return new Unit(List.copyOf(indices), valid);
    }
}
